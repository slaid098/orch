from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest

from orch.exceptions.worker import AgentError
from orch.schemas.config.types import Config, LabelsConfig, RepoConfig, WatchdogConfig, WorkerConfig
from orch.services.agent import session
from orch.services.feedback.service import IncomingComment


class RecordingStdin(io.StringIO):
    def close(self) -> None:
        """Keep the buffer readable after the session closes stdin."""


class DelayedStream:
    def __init__(self, lines: list[str], delay: float = 0.2) -> None:
        self._lines = lines
        self._delay = delay

    def __iter__(self) -> DelayedStream:
        return self

    def __next__(self) -> str:
        if not self._lines:
            raise StopIteration
        time.sleep(self._delay)
        return self._lines.pop(0)


class FakePopen:
    def __init__(
        self,
        args: list[str],
        *,
        stdout_lines: list[str] | None = None,
        delayed: bool = False,
        **kwargs: object,
    ) -> None:
        del kwargs
        self.args = args
        self.stdin = RecordingStdin()
        lines = stdout_lines or []
        self.stdout = DelayedStream(lines) if delayed else iter(lines)
        self.stderr = iter([])
        self.returncode: int | None = None
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class FakeFeed:
    def __init__(self, items: list[IncomingComment] | None = None) -> None:
        self.items = list(items or [])
        self.cancelled = False
        self.cancel_reason = ""

    def drain(self) -> list[IncomingComment]:
        items, self.items = self.items, []
        return items


class FakeWatchdog:
    def __init__(self, verdicts: list[object] | None = None) -> None:
        self.verdicts = list(verdicts or [])
        self.events: list[dict[str, object]] = []

    @property
    def stop_reason(self) -> str | None:
        return None

    def observe(self, event: dict[str, object]) -> None:
        self.events.append(event)

    def check(self) -> object | None:
        return self.verdicts.pop(0) if self.verdicts else None


def _config(tmp_path: Path, *, watchdog: bool = False) -> Config:
    return Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
        watchdog=WatchdogConfig(enabled=watchdog, api_key="key"),
    )


def test_run_session_sends_prompt_and_writes_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePopen(
        ["docker"],
        stdout_lines=['{"type":"result","subtype":"success"}\n'],
    )
    monkeypatch.setattr(session.subprocess, "Popen", lambda *args, **kwargs: fake)

    result = session.run_session(
        "container",
        _config(tmp_path),
        tmp_path,
        "Do the thing",
        time.monotonic() + 10,
        command="claude",
    )

    assert result.returncode == 0
    assert not result.timed_out
    payload = json.loads(fake.stdin.getvalue().splitlines()[0])
    assert payload["message"]["content"][0]["text"] == "Do the thing"
    assert '"subtype":"success"' in (tmp_path / "events.jsonl").read_text(encoding="utf-8")


def test_run_session_injects_feedback_into_open_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePopen(
        ["docker"],
        stdout_lines=['{"type":"result","subtype":"success"}\n'],
        delayed=True,
    )
    monkeypatch.setattr(session.subprocess, "Popen", lambda *args, **kwargs: fake)
    feed = FakeFeed(
        [
            IncomingComment(
                id=1,
                author="alice",
                body="Also update the docs.",
                created_at="2026-11-02T10:00:00Z",
            )
        ]
    )

    result = session.run_session(
        "container",
        _config(tmp_path),
        tmp_path,
        "Do the thing",
        time.monotonic() + 10,
        command="claude",
        feedback=feed,  # type: ignore[arg-type]
    )

    assert result.returncode == 0
    sent = fake.stdin.getvalue()
    assert "Do the thing" in sent
    assert "Also update the docs." in sent
    assert "@alice" in sent


def test_run_session_stops_when_issue_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePopen(["docker"])
    monkeypatch.setattr(session.subprocess, "Popen", lambda *args, **kwargs: fake)
    stopped: list[str] = []
    monkeypatch.setattr(session, "stop_container", stopped.append)
    feed = FakeFeed()
    feed.cancelled = True
    feed.cancel_reason = "issue closed"

    result = session.run_session(
        "container",
        _config(tmp_path),
        tmp_path,
        "Do the thing",
        time.monotonic() + 10,
        command="claude",
        feedback=feed,  # type: ignore[arg-type]
    )

    assert result.cancelled
    assert result.cancel_reason == "issue closed"
    assert stopped == ["container"]


def test_run_session_reports_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePopen(["docker"])
    monkeypatch.setattr(session.subprocess, "Popen", lambda *args, **kwargs: fake)
    stopped: list[str] = []
    monkeypatch.setattr(session, "stop_container", stopped.append)

    result = session.run_session(
        "container",
        _config(tmp_path),
        tmp_path,
        "Do the thing",
        time.monotonic() - 1,
        command="claude",
    )

    assert result.timed_out
    assert stopped == ["container"]


def test_run_session_waits_for_the_final_turn_after_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePopen(
        ["docker"],
        stdout_lines=['{"type":"result","subtype":"success"}\n'],
    )
    monkeypatch.setattr(session.subprocess, "Popen", lambda *args, **kwargs: fake)
    completion = tmp_path / "handoff.json"
    completion.write_text("{}", encoding="utf-8")

    started = time.monotonic()
    result = session.run_session(
        "container",
        _config(tmp_path),
        tmp_path,
        "Do the thing",
        time.monotonic() + 10,
        command="claude",
        completion_path=completion,
    )

    assert result.returncode == 0
    assert time.monotonic() - started >= 1.8


def test_run_session_reports_popen_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise OSError("docker missing")

    monkeypatch.setattr(session.subprocess, "Popen", fail)

    with pytest.raises(AgentError, match="Could not start"):
        session.run_session(
            "container",
            _config(tmp_path),
            tmp_path,
            "Do the thing",
            time.monotonic() + 10,
            command="claude",
        )


def test_watchdog_loop_queues_nudge_and_stops() -> None:
    verdicts = [
        type("V", (), {"action": "nudge"})(),
        type("V", (), {"action": "stop"})(),
    ]
    watchdog = FakeWatchdog(verdicts)
    stop_event = threading.Event()
    queue: object = __import__("queue").Queue()

    thread = threading.Thread(
        target=session._watchdog_loop,
        args=(watchdog, 0.01, stop_event, queue),
    )
    thread.start()
    thread.join(timeout=2)
    stop_event.set()

    assert not thread.is_alive()
    assert queue.qsize() == 2


def test_normalize_event_shapes() -> None:
    events = session._normalize_event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ]
            },
        }
    )
    assert events == [
        {"kind": "assistant", "text": "hello"},
        {"kind": "tool_use", "name": "Bash", "input": {"command": "ls"}},
    ]

    results = session._normalize_event(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "is_error": True,
                        "content": [{"type": "text", "text": "boom"}],
                    }
                ]
            },
        }
    )
    assert results == [{"kind": "tool_result", "is_error": True, "content": "boom"}]
    assert session._normalize_event({"type": "result", "subtype": "success"}) == [
        {"kind": "result", "subtype": "success", "is_error": False, "usage": {}}
    ]
    assert session._normalize_event({"type": "system", "subtype": "init"}) == [
        {"kind": "system", "subtype": "init"}
    ]
    assert session._normalize_event({"type": "unknown"}) == []
    assert session._normalize_event(
        {"type": "assistant", "message": {"content": "plain"}}
    ) == [{"kind": "assistant", "text": "plain"}]


def test_content_text_variants() -> None:
    assert session._content_text("x") == "x"
    assert session._content_text(None) == ""
    assert session._content_text(123) == "123"
    assert session._content_text(
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    ) == "a\nb"


def test_send_handles_missing_and_broken_stdin() -> None:
    class Broken:
        def write(self, _value: str) -> None:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

    process = type("P", (), {"stdin": None})()
    assert not session._send(process, threading.Lock(), {})

    process = type("P", (), {"stdin": Broken()})()
    assert not session._send(process, threading.Lock(), {})


def test_close_stdin_ignores_errors() -> None:
    class Broken:
        def close(self) -> None:
            raise BrokenPipeError

    session._close_stdin(type("P", (), {"stdin": None})())
    session._close_stdin(type("P", (), {"stdin": Broken()})())


def test_stop_swallows_docker_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_container: str) -> None:
        raise RuntimeError("gone")

    monkeypatch.setattr(session, "stop_container", fail)

    session._stop("container")


def test_pump_streams_write_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_seen = threading.Event()
    activity_at = [0.0]
    result_at = [0.0]
    watchdog = FakeWatchdog()
    session._pump_stdout(
        io.StringIO('{"type":"result","subtype":"success"}\n'),
        tmp_path / "events.jsonl",
        result_seen,
        activity_at,
        result_at,
        watchdog,  # type: ignore[arg-type]
    )
    assert result_seen.is_set()
    assert activity_at[0] > 0
    assert result_at[0] > 0
    assert watchdog.events == [
        {"kind": "result", "subtype": "success", "is_error": False, "usage": {}}
    ]

    session._pump_stderr(io.StringIO("stderr\n"), tmp_path / "stderr.log")
    assert (tmp_path / "stderr.log").read_text(encoding="utf-8") == "stderr\n"

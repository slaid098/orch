from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from orch.schemas.config.types import WatchdogConfig
from orch.services.watchdog import clef
from orch.services.watchdog import service as watchdog
from orch.services.watchdog.service import Trigger


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        del args
        return False


@pytest.fixture(autouse=True)
def _clear_clef_cache():
    clef._CACHE.clear()
    yield
    clef._CACHE.clear()


def _config(**overrides: Any) -> WatchdogConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "confidence_threshold": 0.7,
        "silence_minutes": 10,
        "min_run_minutes": 15,
        "max_nudges": 2,
        "cooldown_minutes": 10,
        "check_seconds": 60,
        "max_context_chars": 6000,
    }
    values.update(overrides)
    return WatchdogConfig(**values)


def _watchdog(clock: _FakeClock, **overrides: Any) -> watchdog.Watchdog:
    return watchdog.Watchdog(
        _config(**overrides),
        issue_number=7,
        issue_title="feat(watchdog): supervise stalled runs",
        workspace_path=Path("/work"),
        started_at=clock(),
        clock=clock,
    )


def _classification(
    state: str = "looping",
    confidence: float = 0.9,
    *,
    wait_helpful: float = 0.2,
) -> clef.Classification:
    return clef.Classification(
        state=state,
        confidence=confidence,
        probabilities={state: confidence},
        wait_helpful=wait_helpful,
    )


def _patch_classify(
    monkeypatch: pytest.MonkeyPatch,
    result: clef.Classification | None,
) -> list[str]:
    calls: list[str] = []

    def fake(config: WatchdogConfig, state: str) -> clef.Classification | None:
        del config
        calls.append(state)
        return result

    monkeypatch.setattr(watchdog, "classify", fake)
    return calls


def _observe_repeated_tool(run: watchdog.Watchdog, count: int = 5) -> None:
    for _ in range(count):
        run.observe(
            {
                "kind": "tool_use",
                "name": "Bash",
                "input": {"command": "pytest -q tests/test_x.py"},
            }
        )


def _observe_repeated_error(run: watchdog.Watchdog, count: int = 3) -> None:
    for _ in range(count):
        run.observe(
            {
                "kind": "tool_result",
                "is_error": True,
                "content": "ModuleNotFoundError: No module named 'orch'",
            }
        )


def test_no_check_before_min_run(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    calls = _patch_classify(monkeypatch, _classification("working"))
    run = _watchdog(clock)

    clock.advance(14 * 60)
    run.observe({"kind": "assistant", "text": "still working"})

    assert run.check() is None
    assert calls == []


def test_silence_trigger_builds_state(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    calls = _patch_classify(monkeypatch, _classification("working"))
    run = _watchdog(clock)

    clock.advance(16 * 60)
    verdict = run.check()

    assert verdict is not None
    assert verdict.action == "wait"
    assert calls
    assert "Trigger: silence" in calls[0]
    assert "no events for" in calls[0]


def test_repeated_tool_triggers_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    calls = _patch_classify(monkeypatch, _classification("working"))
    run = _watchdog(clock)

    clock.advance(15 * 60)
    _observe_repeated_tool(run)

    assert run.check() is not None
    assert "Trigger: repeat_tool" in calls[0]
    assert "pytest -q tests/test_x.py" in calls[0]


def test_repeated_error_triggers_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    calls = _patch_classify(monkeypatch, _classification("working"))
    run = _watchdog(clock)

    clock.advance(15 * 60)
    _observe_repeated_error(run)

    assert run.check() is not None
    assert "Trigger: repeat_error" in calls[0]
    assert "ModuleNotFoundError" in calls[0]


@pytest.mark.parametrize("state", ["working", "waiting_external"])
def test_working_states_wait(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    clock = _FakeClock()
    _patch_classify(monkeypatch, _classification(state, 0.95))
    run = _watchdog(clock)

    clock.advance(16 * 60)
    verdict = run.check()

    assert verdict is not None
    assert verdict.action == "wait"
    assert verdict.state == state
    assert verdict.nudge_text == ""
    assert run.nudges == 0
    assert run.stop_reason is None


def test_looping_nudges_then_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    _patch_classify(monkeypatch, _classification("looping", 0.92))
    run = _watchdog(clock, max_nudges=2, cooldown_minutes=0)

    clock.advance(15 * 60)
    _observe_repeated_tool(run)

    first = run.check()
    assert first is not None
    assert first.action == "nudge"
    assert "Do not run the same command" in first.nudge_text
    assert "pytest -q tests/test_x.py" in first.nudge_text
    assert run.nudges == 1

    clock.advance(61)
    second = run.check()
    assert second is not None
    assert second.action == "nudge"
    assert run.nudges == 2

    clock.advance(61)
    third = run.check()
    assert third is not None
    assert third.action == "stop"
    assert run.nudges == 2
    assert run.stop_reason is not None
    assert "nudge limit 2" in run.stop_reason
    assert run.check() is None


def test_low_confidence_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    _patch_classify(monkeypatch, _classification("looping", 0.4))
    run = _watchdog(clock)

    clock.advance(16 * 60)
    verdict = run.check()

    assert verdict is not None
    assert verdict.action == "wait"
    assert "below threshold" in verdict.reason
    assert run.nudges == 0
    assert run.stop_reason is None


def test_classifier_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    calls = _patch_classify(monkeypatch, None)
    run = _watchdog(clock)

    clock.advance(16 * 60)
    assert run.check() is None
    assert calls
    assert run.nudges == 0
    assert run.stop_reason is None


def test_needs_human_sets_stop_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    _patch_classify(monkeypatch, _classification("needs_human", 0.95))
    run = _watchdog(clock)

    clock.advance(16 * 60)
    verdict = run.check()

    assert verdict is not None
    assert verdict.action == "stop"
    assert run.stop_reason is not None
    assert "needs_human" in run.stop_reason
    assert run.nudges == 0


def test_cooldown_blocks_second_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    calls = _patch_classify(monkeypatch, _classification("working"))
    run = _watchdog(clock, silence_minutes=5, cooldown_minutes=10)

    clock.advance(16 * 60)
    assert run.check() is not None

    clock.advance(61)
    assert run.check() is None
    assert len(calls) == 1


def test_check_seconds_blocks_rapid_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    calls = _patch_classify(monkeypatch, _classification("working"))
    run = _watchdog(clock, silence_minutes=5, cooldown_minutes=0)

    clock.advance(16 * 60)
    assert run.check() is not None

    clock.advance(30)
    assert run.check() is None
    assert len(calls) == 1

    clock.advance(31)
    assert run.check() is not None
    assert len(calls) == 2


def test_disabled_watchdog_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    calls = _patch_classify(monkeypatch, _classification("working"))
    run = _watchdog(clock, enabled=False)

    clock.advance(60 * 60)
    assert run.check() is None
    assert calls == []


def test_build_state_contains_context_and_truncates() -> None:
    clock = _FakeClock()
    run = _watchdog(clock, max_context_chars=500)

    clock.advance(120)
    _observe_repeated_tool(run, count=1)
    for index in range(40):
        run.observe({"kind": "assistant", "text": f"step {index} " + "x" * 60})
    _observe_repeated_error(run, count=1)

    state = run.build_state(
        Trigger(
            name="repeat_error",
            detail="same error repeated 3 times: ModuleNotFoundError: boom",
        )
    )

    assert len(state) <= 500
    assert "Issue #7" in state
    assert "Trigger: repeat_error" in state
    assert "Repeated tools:" in state
    assert "Repeated errors:" in state
    assert "Last error: ModuleNotFoundError" in state
    assert state.count("\n- [") <= 20


def test_check_without_trigger_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    calls = _patch_classify(monkeypatch, _classification("working"))
    run = _watchdog(clock, silence_minutes=60)

    clock.advance(15 * 60)
    assert run.check() is None
    assert calls == []


def test_unknown_state_with_high_confidence_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    _patch_classify(monkeypatch, _classification("mystery", 0.99))
    run = _watchdog(clock)

    clock.advance(16 * 60)
    verdict = run.check()

    assert verdict is not None
    assert verdict.action == "wait"
    assert "no watchdog action policy" in verdict.reason


def test_observe_handles_all_event_kinds_and_caps_buffer() -> None:
    clock = _FakeClock()
    run = _watchdog(clock)

    run.observe({"kind": "tool_result", "is_error": False, "content": "ok"})
    run.observe({"kind": "assistant", "text": None})
    run.observe({"kind": "result", "subtype": "success"})
    run.observe({"kind": "result"})
    run.observe({"kind": "unknown"})
    run.observe({"kind": "tool_use", "name": "Glob", "input": ["a", "b"]})
    run.observe({"kind": "tool_use", "name": "Read", "input": {"foo": "bar"}})
    for index in range(205):
        run.observe({"kind": "assistant", "text": f"event {index}"})

    state = run.build_state(Trigger("silence", "no events"))

    assert "Recent events:" in state
    assert len(run._events) == watchdog._MAX_EVENTS


def test_helpers_degrade_gracefully() -> None:
    class BadValue:
        def __str__(self) -> str:
            raise TypeError("no string form")

    assert "BadValue" in watchdog._tool_signature("Tool", BadValue())
    assert "BadValue" in watchdog._preview(BadValue())
    assert watchdog._format_duration(3661) == "1h 1m"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"answers": "bad"},
        {"answers": {"agent_state": "bad"}},
    ],
)
def test_parse_rejects_malformed_payloads(raw: object) -> None:
    assert clef._parse(raw) is None


def test_parse_defaults_invalid_numbers() -> None:
    result = clef._parse(
        {
            "answers": {
                "agent_state": {
                    "choice": "working",
                    "confidence": "bad",
                    "probabilities": {"working": "bad"},
                },
                "wait_helpful": {"noul": "bad"},
            }
        }
    )

    assert result is not None
    assert result.confidence == 0.0
    assert result.probabilities == {"working": 0.0}
    assert result.wait_helpful == 0.0


def test_classify_sends_payload_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[urllib.request.Request, float]] = []
    response = {
        "answers": {
            "agent_state": {
                "type": "choice",
                "choice": "looping",
                "confidence": 0.8177,
                "probabilities": {"working": 0.15, "looping": 0.8177},
            },
            "wait_helpful": {"type": "noul", "noul": 0.35},
        },
        "usage": {"total_duration": 1},
    }

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _FakeResponse:
        captured.append((request, timeout))
        return _FakeResponse(response)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    config = _config(api_key="secret-token")

    result = clef.classify(config, "agent state text")

    assert result == clef.Classification(
        state="looping",
        confidence=0.8177,
        probabilities={"working": 0.15, "looping": 0.8177},
        wait_helpful=0.35,
    )
    assert len(captured) == 1
    request, timeout = captured[0]
    assert request.full_url == config.endpoint
    assert request.get_method() == "POST"
    assert timeout == 30
    headers = {key.lower(): value for key, value in request.headers.items()}
    assert headers["authorization"] == "Bearer secret-token"
    assert headers["content-type"] == "application/json"

    payload = json.loads(request.data)
    assert payload["model"] == "clef"
    assert payload["state"] == "agent state text"
    questions = payload["questions"]
    assert questions["agent_state"]["type"] == "choice"
    assert questions["agent_state"]["instructions"]
    assert set(questions["agent_state"]["criteria"]) == {
        "working",
        "looping",
        "stuck",
        "waiting_external",
        "needs_human",
    }
    assert "зацикливание" in questions["agent_state"]["criteria"]["working"]
    assert questions["wait_helpful"] == {
        "type": "noul",
        "instructions": (
            "Поможет ли ожидание ещё 10 минут без вмешательства? Вероятность 0..1."
        ),
    }


def test_classify_defaults_wait_helpful_and_omits_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[urllib.request.Request] = []
    response = {
        "answers": {
            "agent_state": {
                "choice": "working",
                "confidence": 0.8,
                "probabilities": {},
            }
        }
    }

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _FakeResponse:
        del timeout
        captured.append(request)
        return _FakeResponse(response)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = clef.classify(_config(), "state without wait answer")

    assert result is not None
    assert result.wait_helpful == 0.0
    headers = {key.lower(): value for key, value in captured[0].headers.items()}
    assert "authorization" not in headers


def test_classify_caches_by_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[urllib.request.Request] = []
    response = {
        "answers": {
            "agent_state": {
                "choice": "stuck",
                "confidence": 0.75,
                "probabilities": {"stuck": 0.75},
            },
            "wait_helpful": {"noul": 0.1},
        }
    }

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _FakeResponse:
        del timeout
        calls.append(request)
        return _FakeResponse(response)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    first = clef.classify(_config(), "cached state")
    second = clef.classify(_config(), "cached state")

    assert first == second
    assert len(calls) == 1


def test_classify_returns_none_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[urllib.request.Request] = []

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _FakeResponse:
        del timeout
        attempts.append(request)
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(clef.time, "sleep", lambda _: None)

    assert clef.classify(_config(), "failing state") is None
    assert len(attempts) == 2


def test_classify_returns_none_on_unusable_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[urllib.request.Request] = []

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _FakeResponse:
        del timeout
        calls.append(request)
        return _FakeResponse({"answers": {"agent_state": {"choice": ""}}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(clef.time, "sleep", lambda _: None)

    assert clef.classify(_config(), "unusable state") is None
    assert len(calls) == 2

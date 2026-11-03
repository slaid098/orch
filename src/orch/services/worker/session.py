"""Long-lived Claude Code stream-json session for one worker attempt."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import orch.services.worker.claude as claude
from orch.config.logger import get_logger
from orch.exceptions.worker import AgentError
from orch.schemas.config.types import Config
from orch.schemas.worker.types import SessionResult, WatchdogLike
from orch.services.docker import stop as stop_container
from orch.services.feedback.service import CommentFeed

log = get_logger(component="worker.session")

_POLL_SECONDS = 0.25
_CLOSE_GRACE_SECONDS = 10.0


class _SessionRuntime:
    """State and threads for one Claude Code stream-json session."""

    def __init__(
        self,
        container: str,
        config: Config,
        run_dir: Path,
        watchdog: WatchdogLike | None,
        mcp_config: Path | None,
    ) -> None:
        self.container = container
        self.process = _spawn(container, config, mcp_config)
        self.input_lock = threading.Lock()
        self.result_seen = threading.Event()
        self.activity_at = [time.monotonic()]
        self.result_at = [0.0]
        self.verdicts: queue.Queue[Any] = queue.Queue()
        self.watchdog_stop = threading.Event()
        self.watchdog_thread: threading.Thread | None = None
        self.timed_out = False
        self.stopped_by_watchdog = False
        self.watchdog_reason: str | None = None
        self.cancelled = False
        self.cancel_reason: str | None = None
        self.handoff_since = time.monotonic()

        self.stdout_thread = threading.Thread(
            target=_pump_stdout,
            args=(
                self.process.stdout,
                run_dir / "events.jsonl",
                self.result_seen,
                self.activity_at,
                self.result_at,
                watchdog,
            ),
            name="orch-claude-stdout",
            daemon=True,
        )
        self.stderr_thread = threading.Thread(
            target=_pump_stderr,
            args=(self.process.stderr, run_dir / "events.stderr.log"),
            name="orch-claude-stderr",
            daemon=True,
        )
        self.stdout_thread.start()
        self.stderr_thread.start()
        if watchdog is not None and config.watchdog.enabled:
            self.watchdog_thread = threading.Thread(
                target=_watchdog_loop,
                args=(
                    watchdog,
                    config.watchdog.check_seconds,
                    self.watchdog_stop,
                    self.verdicts,
                ),
                name="orch-watchdog",
                daemon=True,
            )
            self.watchdog_thread.start()

    def send(self, text: str) -> bool:
        return _send(
            self.process,
            self.input_lock,
            _message_payload(text),
        )

    def mark_activity(self) -> None:
        self.activity_at[0] = time.monotonic()
        self.handoff_since = self.activity_at[0]

    def close(self) -> None:
        self.watchdog_stop.set()
        _close_stdin(self.process)
        try:
            self.process.wait(timeout=_CLOSE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            log.bind(container=self.container).warning(
                "Claude Code did not exit after stdin closed; stopping container"
            )
            _stop(self.container)
            self.process.kill()
            self.process.wait(timeout=5)
        self.stdout_thread.join(timeout=5)
        self.stderr_thread.join(timeout=5)
        if self.watchdog_thread is not None:
            self.watchdog_thread.join(timeout=5)

    def result(self) -> SessionResult:
        returncode = self.process.returncode
        return SessionResult(
            returncode=returncode if returncode is not None else -1,
            timed_out=self.timed_out,
            stopped_by_watchdog=self.stopped_by_watchdog,
            watchdog_reason=self.watchdog_reason,
            cancelled=self.cancelled,
            cancel_reason=self.cancel_reason,
        )


def run_session(
    container: str,
    config: Config,
    run_dir: Path,
    prompt: str,
    deadline: float,
    *,
    feedback: CommentFeed | None = None,
    watchdog: WatchdogLike | None = None,
    completion_path: Path | None = None,
    mcp_config: Path | None = None,
) -> SessionResult:
    """Run Claude Code with an open stdin pipe and inject live messages.

    Args:
        container: Running worker container name.
        config: Orchestrator configuration.
        run_dir: Host directory mounted at ``/out``.
        prompt: Initial user prompt.
        deadline: Monotonic deadline for the whole attempt.
        feedback: Optional comment feed to inject while the agent works.
        watchdog: Optional classifier used for nudges and stops.
        completion_path: Optional handoff file that signals task completion.
        mcp_config: Optional container path to the MCP JSON configuration.

    Returns:
        A session result describing why the process ended.

    Raises:
        AgentError: If the Claude Code process cannot be started.
    """
    runtime = _SessionRuntime(
        container,
        config,
        run_dir,
        watchdog,
        mcp_config,
    )
    if not runtime.send(prompt):
        runtime.close()
        raise AgentError("Could not send the initial prompt to Claude Code")
    try:
        _wait_for_session(runtime, feedback, deadline, completion_path)
    finally:
        runtime.close()
    return runtime.result()


def _spawn(
    container: str,
    config: Config,
    mcp_config: Path | None,
) -> subprocess.Popen[str]:
    mcp_path = "/out/mcp.json" if mcp_config is not None else None
    command = "cd /work && " + claude.build_stream_command(config, mcp_path)
    try:
        return subprocess.Popen(
            [
                "docker",
                "exec",
                "-i",
                "-u",
                "agent",
                "-w",
                "/work",
                container,
                "bash",
                "-lc",
                command,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as ex:
        log.bind(
            container=container,
            error=ex,
            error_type=type(ex).__name__,
        ).error("Could not start the Claude Code session")
        raise AgentError("Could not start the Claude Code session") from ex


def _wait_for_session(
    runtime: _SessionRuntime,
    feedback: CommentFeed | None,
    deadline: float,
    completion_path: Path | None,
) -> None:
    while runtime.process.poll() is None:
        if time.monotonic() >= deadline:
            runtime.timed_out = True
            _stop(runtime.container)
            return
        if feedback is not None and feedback.cancelled:
            runtime.cancelled = True
            runtime.cancel_reason = feedback.cancel_reason or "issue closed"
            _stop(runtime.container)
            return

        injected, stopped, reason = _process_injections(runtime, feedback)
        if stopped:
            runtime.stopped_by_watchdog = True
            runtime.watchdog_reason = reason
            return
        if injected:
            runtime.result_seen.clear()
            runtime.mark_activity()

        if completion_path is not None and completion_path.exists():
            if _handoff_complete(runtime):
                return
        elif runtime.result_seen.is_set():
            if _inject_pending(runtime, feedback):
                runtime.result_seen.clear()
                runtime.mark_activity()
                continue
            if time.monotonic() - runtime.result_at[0] >= 2:
                return
        time.sleep(_POLL_SECONDS)


def _process_injections(
    runtime: _SessionRuntime,
    feedback: CommentFeed | None,
) -> tuple[bool, bool, str | None]:
    injected = False
    for comment in feedback.drain() if feedback is not None else ():
        if runtime.send(_comment_text(comment.author, comment.body)):
            injected = True
            log.bind(
                issue=comment.id,
                author=comment.author,
            ).info("Injected issue comment into the live session")

    while True:
        try:
            verdict = runtime.verdicts.get_nowait()
        except queue.Empty:
            break
        if verdict.action == "nudge":
            if runtime.send(verdict.nudge_text):
                injected = True
                log.warning("Watchdog nudged the live session: {}", verdict.reason)
        elif verdict.action == "stop":
            _stop(runtime.container)
            return injected, True, verdict.reason
    return injected, False, None


def _handoff_complete(runtime: _SessionRuntime) -> bool:
    quiet = time.monotonic() - runtime.activity_at[0]
    finished_turn = runtime.result_at[0] >= runtime.handoff_since
    return (finished_turn and quiet >= 2) or quiet >= 30


def _inject_pending(
    runtime: _SessionRuntime,
    feedback: CommentFeed | None,
) -> bool:
    pending = feedback.drain() if feedback is not None else []
    for comment in pending:
        if runtime.send(_comment_text(comment.author, comment.body)):
            log.bind(
                issue=comment.id,
                author=comment.author,
            ).info("Injected issue comment into the live session")
    return bool(pending)


def _message_payload(text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def _comment_text(author: str, body: str) -> str:
    trimmed = body.strip()
    if len(trimmed) > 4000:
        trimmed = trimmed[:4000] + "\n…"
    return (
        "Новый комментарий к issue от "
        f"@{author}:\n\n{trimmed}\n\n"
        "Учти это в текущей работе. Если комментарий меняет задачу, "
        "скорректируй план и продолжай."
    )


def _send(
    process: subprocess.Popen[str],
    lock: threading.Lock,
    payload: dict[str, Any],
) -> bool:
    if process.stdin is None:
        return False
    try:
        with lock:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
    except (BrokenPipeError, OSError, ValueError) as ex:
        log.bind(error_type=type(ex).__name__).warning(
            "Could not write to the Claude Code stdin pipe"
        )
        return False
    else:
        return True


def _close_stdin(process: subprocess.Popen[str]) -> None:
    if process.stdin is None:
        return
    try:
        process.stdin.close()
    except BrokenPipeError, OSError, ValueError:
        return


def _stop(container: str) -> None:
    try:
        stop_container(container)
    except Exception as ex:
        log.bind(
            container=container,
            error_type=type(ex).__name__,
        ).warning("Could not stop the worker container")


def _pump_stdout(
    stream: Any,
    events_path: Path,
    result_seen: threading.Event,
    activity_at: list[float],
    result_at: list[float],
    watchdog: WatchdogLike | None,
) -> None:
    try:
        with events_path.open("w", encoding="utf-8") as sink:
            for line in stream:
                sink.write(line)
                sink.flush()
                activity_at[0] = time.monotonic()
                try:
                    event = json.loads(line)
                except (TypeError, ValueError) as ex:
                    log.bind(error_type=type(ex).__name__).debug(
                        "Skipping non-JSON Claude Code output"
                    )
                    continue
                if event.get("type") == "result":
                    result_seen.set()
                    result_at[0] = time.monotonic()
                if watchdog is not None:
                    for normalized in _normalize_event(event):
                        watchdog.observe(normalized)
    except (OSError, ValueError) as ex:
        log.bind(error_type=type(ex).__name__).warning("Claude Code stdout reader stopped")


def _pump_stderr(stream: Any, stderr_path: Path) -> None:
    try:
        with stderr_path.open("w", encoding="utf-8") as sink:
            for line in stream:
                sink.write(line)
                sink.flush()
    except (OSError, ValueError) as ex:
        log.bind(error_type=type(ex).__name__).warning("Claude Code stderr reader stopped")


def _watchdog_loop(
    watchdog: WatchdogLike,
    interval: int,
    stop_event: threading.Event,
    verdicts: queue.Queue[Any],
) -> None:
    while not stop_event.wait(interval):
        try:
            verdict = watchdog.check()
        except Exception as ex:
            log.bind(error_type=type(ex).__name__).warning(
                "Watchdog check failed; continuing without intervention"
            )
            continue
        if verdict is None:
            continue
        verdicts.put(verdict)
        if verdict.action == "stop":
            return


def _normalize_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    event_type = event.get("type")
    if event_type == "assistant":
        return _assistant_events(event)
    if event_type == "user":
        return _tool_result_events(event)
    if event_type == "result":
        return [
            {
                "kind": "result",
                "subtype": event.get("subtype"),
                "is_error": bool(event.get("is_error")),
                "usage": event.get("usage") if isinstance(event.get("usage"), dict) else {},
            }
        ]
    if event_type == "system":
        return [{"kind": "system", "subtype": event.get("subtype")}]
    return []


def _assistant_events(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"kind": "assistant", "text": content}]
    if not isinstance(content, list):
        return []
    events: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            events.append({"kind": "assistant", "text": block.get("text")})
        elif block.get("type") == "tool_use":
            events.append(
                {
                    "kind": "tool_use",
                    "name": block.get("name"),
                    "input": block.get("input"),
                }
            )
    return events


def _tool_result_events(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    events: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        events.append(
            {
                "kind": "tool_result",
                "is_error": bool(block.get("is_error")),
                "content": _content_text(block.get("content")),
            }
        )
    return events


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)

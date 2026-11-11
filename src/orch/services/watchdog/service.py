"""Clef-backed watchdog for stalled or looping agent runs."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch.config.logger import get_logger
from orch.schemas.config.types import WatchdogConfig
from orch.schemas.watchdog.types import Classification, Trigger, Verdict
from orch.services.watchdog.clef import classify

log = get_logger(component="watchdog")

_REPEAT_TOOL_THRESHOLD = 5
_REPEAT_ERROR_THRESHOLD = 3
_MAX_EVENTS = 200
_EVENT_WINDOW = 20
_PREVIEW_CHARS = 200


class Watchdog:
    """Supervise one agent run without touching its process or container."""

    def __init__(
        self,
        config: WatchdogConfig,
        *,
        issue_number: int,
        issue_title: str,
        workspace_path: Path,
        started_at: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the watchdog for one issue attempt.

        Args:
            config: Watchdog settings and classifier policy.
            issue_number: GitHub issue number used in the classifier context.
            issue_title: GitHub issue title used in the classifier context.
            workspace_path: Agent workspace, included in the compact state.
            started_at: Clock value when the run started; defaults to now.
            clock: Monotonic clock used for every time comparison.
        """
        self._config = config
        self._issue_number = issue_number
        self._issue_title = issue_title
        self._workspace_path = workspace_path
        self._clock = clock
        now = clock()
        self._started_at = now if started_at is None else started_at
        self._last_event_at = self._started_at
        self._last_gate_at = self._started_at
        self._last_check_at: float | None = None
        self._tool_counts: dict[str, int] = {}
        self._tool_labels: dict[str, str] = {}
        self._error_counts: dict[str, int] = {}
        self._error_labels: dict[str, str] = {}
        self._last_error: str | None = None
        self._events: list[tuple[float, str]] = []
        self._total_tokens = 0
        self._nudges = 0
        self._stop_reason: str | None = None

    @property
    def stop_reason(self) -> str | None:
        """Return why the watchdog stopped the run, if it did."""
        return self._stop_reason

    @property
    def nudges(self) -> int:
        """Return the number of nudges already issued."""
        return self._nudges

    def observe(self, event: dict[str, Any]) -> None:
        """Record one normalized agent event.

        Args:
            event: Event with a ``kind`` of ``tool_use``, ``tool_result``,
                ``assistant``, or ``result``.
        """
        now = self._clock()
        self._last_event_at = now
        kind = event.get("kind")
        summary: str | None = None

        if kind == "tool_use":
            name = str(event.get("name") or "unknown")
            input_value = event.get("input")
            signature = _tool_signature(name, input_value)
            self._tool_counts[signature] = self._tool_counts.get(signature, 0) + 1
            self._tool_labels[signature] = f"{name}: {_preview(input_value)}"
            summary = f"tool_use {name}: {_preview(input_value)}"
        elif kind == "tool_result":
            content = _text(event.get("content"))
            if event.get("is_error"):
                signature = _error_signature(content)
                self._error_counts[signature] = (
                    self._error_counts.get(signature, 0) + 1
                )
                label = _truncate(content, _PREVIEW_CHARS)
                self._error_labels[signature] = label
                self._last_error = label
                summary = f"tool_result error: {label}"
            else:
                summary = f"tool_result ok: {_truncate(content, 120)}"
        elif kind == "assistant":
            text = _text(event.get("text"))
            if text:
                summary = f"assistant: {_truncate(text, _PREVIEW_CHARS)}"
        elif kind == "result":
            usage = event.get("usage")
            if isinstance(usage, dict):
                self._total_tokens += sum(
                    _token_count(usage.get(key))
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                    )
                )
            outcome = event.get("subtype") or event.get("status") or "finished"
            summary = f"result: {_truncate(_text(outcome), 80)}"

        if summary is None:
            return
        self._events.append((now, summary))
        if len(self._events) > _MAX_EVENTS:
            del self._events[:-_MAX_EVENTS]

    def check(self) -> Verdict | None:
        """Classify the run when a trigger fires and return one decision.

        Returns:
            A verdict when the classifier produced an actionable answer,
            otherwise ``None``.
        """
        if not self._config.enabled or self._stop_reason is not None:
            return None

        now = self._clock()
        if now - self._last_gate_at < self._config.check_seconds:
            return None
        self._last_gate_at = now

        if now - self._started_at < self._config.min_run_minutes * 60:
            return None

        cooldown_seconds = self._config.cooldown_minutes * 60
        if (
            self._last_check_at is not None
            and now - self._last_check_at < cooldown_seconds
        ):
            return None

        trigger = self._detect_trigger(now)
        if trigger is None:
            return None

        self._last_check_at = now
        state = self.build_state(trigger)
        classification = classify(self._config, state)
        if classification is None:
            log.bind(
                issue=self._issue_number,
                trigger=trigger.name,
            ).warning("Watchdog classifier unavailable; run left untouched")
            return None
        return self._apply(trigger, classification)

    def build_state(self, trigger: Trigger) -> str:
        """Build the compact text sent to Clef, truncated to the configured limit.

        Args:
            trigger: Deterministic signal that caused this check.

        Returns:
            A compact multiline description of the run.
        """
        now = self._clock()
        elapsed = now - self._started_at
        event_age = now - self._last_event_at
        budget = max(0, self._config.max_context_chars)
        lines = [
            f"Issue #{self._issue_number}: {self._issue_title}",
            f"Workspace: {self._workspace_path}",
            f"Elapsed: {_format_duration(elapsed)}",
            f"Last event: {_format_duration(event_age)} ago",
            f"Trigger: {trigger.name} — {trigger.detail}",
            f"Tool calls: {sum(self._tool_counts.values())} across "
            f"{len(self._tool_counts)} signatures",
            f"Tool errors: {sum(self._error_counts.values())} across "
            f"{len(self._error_counts)} signatures",
            f"Tokens observed: {self._total_tokens}",
        ]
        if self._tool_counts:
            lines.append(
                "Repeated tools: " + _top_counts(self._tool_counts, self._tool_labels)
            )
        if self._error_counts:
            lines.append(
                "Repeated errors: "
                + _top_counts(self._error_counts, self._error_labels)
            )
        if self._last_error:
            lines.append(f"Last error: {_truncate(self._last_error, 400)}")
        lines.append("Recent events:")

        for timestamp, summary in reversed(self._events[-_EVENT_WINDOW:]):
            line = f"- [{_format_duration(timestamp - self._started_at)}] {summary}"
            if len("\n".join([*lines, line])) > budget:
                break
            lines.append(line)

        return "\n".join(lines)[:budget]

    def _detect_trigger(self, now: float) -> Trigger | None:
        silence_seconds = now - self._last_event_at
        if silence_seconds > self._config.silence_minutes * 60:
            return Trigger(
                name="silence",
                detail=f"no events for {_format_duration(silence_seconds)}",
            )

        error_signature, error_count = _top(self._error_counts)
        if error_signature is not None and error_count >= _REPEAT_ERROR_THRESHOLD:
            error = self._error_labels.get(error_signature, error_signature)
            return Trigger(
                name="repeat_error",
                detail=f"same error repeated {error_count} times: "
                f"{_truncate(error, _PREVIEW_CHARS)}",
            )

        tool_signature, tool_count = _top(self._tool_counts)
        if tool_signature is not None and tool_count >= _REPEAT_TOOL_THRESHOLD:
            label = self._tool_labels.get(tool_signature, tool_signature)
            return Trigger(
                name="repeat_tool",
                detail=f"same tool call repeated {tool_count} times: {label}",
            )

        return None

    def _apply(
        self,
        trigger: Trigger,
        classification: Classification,
    ) -> Verdict:
        state = classification.state
        confidence = classification.confidence
        threshold = self._config.confidence_threshold

        if state in {"working", "waiting_external"}:
            return self._wait(state, confidence)
        if state == "needs_human" and confidence >= threshold:
            return self._stop(
                state,
                confidence,
                f"needs_human confidence={confidence:.2f}: {trigger.detail}",
                "human help required",
            )
        if state in {"looping", "stuck"} and confidence >= threshold:
            if self._nudges < self._config.max_nudges:
                return self._nudge(state, confidence, trigger)
            return self._stop(
                state,
                confidence,
                f"clef={state} confidence={confidence:.2f}; "
                f"nudge limit {self._config.max_nudges} reached: "
                f"{trigger.detail}",
                "nudge limit reached",
                retryable=True,
            )
        if confidence < threshold:
            reason = (
                f"clef={state} confidence={confidence:.2f} below threshold "
                f"{threshold:.2f}"
            )
        else:
            reason = f"clef={state} has no watchdog action policy"
        return self._wait(state, confidence, reason)

    def _wait(
        self,
        state: str,
        confidence: float,
        reason: str | None = None,
    ) -> Verdict:
        return Verdict(
            action="wait",
            state=state,
            confidence=confidence,
            reason=reason or f"clef={state} confidence={confidence:.2f}",
        )

    def _stop(
        self,
        state: str,
        confidence: float,
        reason: str,
        log_reason: str,
        *,
        retryable: bool = False,
    ) -> Verdict:
        self._stop_reason = reason
        log.bind(
            issue=self._issue_number,
            state=state,
            confidence=round(confidence, 3),
        ).warning("Watchdog stopping the run: {}", log_reason)
        return Verdict(
            action="stop",
            state=state,
            confidence=confidence,
            reason=reason,
            retryable=retryable,
        )

    def _nudge(
        self,
        state: str,
        confidence: float,
        trigger: Trigger,
    ) -> Verdict:
        self._nudges += 1
        reason = (
            f"clef={state} confidence={confidence:.2f}; "
            f"nudge {self._nudges}/{self._config.max_nudges}"
        )
        log.bind(
            issue=self._issue_number,
            state=state,
            confidence=round(confidence, 3),
            nudge=self._nudges,
        ).info("Watchdog nudging the run")
        return Verdict(
            action="nudge",
            state=state,
            confidence=confidence,
            reason=reason,
            nudge_text=self._nudge_text(trigger),
        )

    def _nudge_text(self, trigger: Trigger) -> str:
        return (
            "Watchdog intervention: the run looks stuck or looping.\n"
            f"Evidence: {trigger.detail}\n"
            "Stop repeating the same action. Summarize the failure in one or two "
            "sentences, state what you already tried and why it did not work, then "
            "choose a different approach. Do not run the same command again without "
            "changing the code, inputs, or plan."
        )


def _tool_signature(name: str, input_value: Any) -> str:
    try:
        rendered = json.dumps(
            input_value,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        rendered = repr(input_value)
    return f"{name}|{_truncate(rendered, 500)}"


def _error_signature(content: str) -> str:
    normalized = " ".join(content.split())
    return _truncate(normalized or "unknown error", 300)


def _preview(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("command", "file_path", "path", "pattern", "url"):
            if key in value:
                return _truncate(f"{key}={value[key]}", _PREVIEW_CHARS)
    try:
        rendered = (
            json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
            if isinstance(value, (dict, list))
            else str(value)
        )
    except (TypeError, ValueError):
        rendered = repr(value)
    return _truncate(rendered, _PREVIEW_CHARS)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _top(counts: dict[str, int]) -> tuple[str | None, int]:
    if not counts:
        return None, 0
    return max(counts.items(), key=lambda item: item[1])


def _top_counts(
    counts: dict[str, int],
    labels: dict[str, str],
    limit: int = 3,
) -> str:
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
    return "; ".join(
        f"{_truncate(labels.get(signature, signature), 80)} x{count}"
        for signature, count in ranked
    )


def _token_count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, remainder = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {remainder}s"
    return f"{remainder}s"

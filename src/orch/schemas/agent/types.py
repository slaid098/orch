"""Shared agent session schemas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SessionResult:
    """Why one long-lived Claude Code session ended."""

    returncode: int
    timed_out: bool = False
    stopped_by_watchdog: bool = False
    watchdog_reason: str | None = None
    cancelled: bool = False
    cancel_reason: str | None = None


class WatchdogLike(Protocol):
    """Minimal watchdog surface used by the session bridge."""

    @property
    def stop_reason(self) -> str | None:
        """Return why the watchdog stopped the run."""

    def observe(self, event: dict[str, Any]) -> None:
        """Record one normalized event."""

    def check(self) -> Any | None:
        """Return a watchdog verdict, if any."""

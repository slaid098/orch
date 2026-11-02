"""Worker result schema."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from orch.schemas.standards.types import Handoff


@dataclass(frozen=True)
class WorkerResult:
    """Artifacts produced by a successful worker attempt."""

    run_dir: Path
    branch: str
    handoff: Handoff
    tests_output: str
    bundle_path: Path


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

"""Pipeline result schema."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orch.schemas.worker.types import WorkerResult


@dataclass(frozen=True)
class AttemptSuccess:
    """A worker result whose branch is published and whose PR is open."""

    worker: WorkerResult
    pr_url: str
    head_sha: str = ""


@dataclass(frozen=True)
class AttemptCycle:
    """State passed from one pipeline retry cycle to the next."""

    outcome: RunOutcome | None
    error: Exception | None
    review_rounds: int
    pr_url: str | None


@dataclass(frozen=True)
class RunOutcome:
    """Result of one issue run."""

    run_id: str
    status: str
    run_dir: Path
    branch: str
    pr_url: str | None = None
    error: str | None = None
    issue_title: str = ""
    duration_seconds: int = 0
    review_rounds: int = 0
    merge_commit: str | None = None

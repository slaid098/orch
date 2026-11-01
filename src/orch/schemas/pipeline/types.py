"""Pipeline result schema."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunOutcome:
    """Result of one issue run."""

    run_id: str
    status: str
    run_dir: Path
    branch: str
    pr_url: str | None = None
    error: str | None = None

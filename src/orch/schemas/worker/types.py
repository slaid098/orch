"""Worker result schema."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orch.schemas.standards.types import Handoff


@dataclass(frozen=True)
class WorkerResult:
    """Artifacts produced by a successful worker attempt."""

    run_dir: Path
    branch: str
    handoff: Handoff
    tests_output: str
    bundle_path: Path

"""Age-based cleanup of local run artifacts."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from orch.config.logger import get_logger

log = get_logger(component="retention")

_SECONDS_PER_DAY = 86_400


def prune_runs(data_dir: Path, retention_days: int) -> int:
    """Delete run directories older than the retention window.

    Args:
        data_dir: Local orch data directory.
        retention_days: Age in days after which a run directory is removed.

    Returns:
        Number of removed run directories.
    """
    runs_dir = data_dir / "runs"
    if not runs_dir.is_dir():
        return 0

    cutoff = time.time() - retention_days * _SECONDS_PER_DAY
    removed = 0
    for repo_dir in runs_dir.iterdir():
        if not repo_dir.is_dir():
            continue
        for issue_dir in repo_dir.iterdir():
            if not issue_dir.is_dir():
                continue
            removed += _prune_issue(issue_dir, cutoff)
            _remove_if_empty(issue_dir)
        _remove_if_empty(repo_dir)
    if removed:
        log.info("Removed {} expired run director(ies)", removed)
    return removed


def _prune_issue(issue_dir: Path, cutoff: float) -> int:
    removed = 0
    for run_dir in issue_dir.iterdir():
        if not run_dir.is_dir() or run_dir.stat().st_mtime >= cutoff:
            continue
        shutil.rmtree(run_dir, ignore_errors=True)
        removed += 1
    return removed


def _remove_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        return

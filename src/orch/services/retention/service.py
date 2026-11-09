"""Age-based cleanup of local run artifacts and issue workspaces."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from orch.config.logger import get_logger
from orch.exceptions.github import GitHubError
from orch.schemas.config.types import Config
from orch.services import git as gitops
from orch.services import github

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


def prune_workspaces(
    config: Config,
    token: str,
    *,
    active_issues: set[int] | None = None,
) -> int:
    """Delete expired issue workspaces that have no open pull request.

    A workspace is kept while its issue has an open pull request or an active
    run, and until its newest file is older than
    ``config.workspace_retention_days``. If the open-pull-request lookup fails,
    nothing is deleted.

    Args:
        config: Orchestrator configuration.
        token: GitHub token used only for the open-pull-request lookup.
        active_issues: Issue numbers with a run in progress.

    Returns:
        Number of removed workspaces.
    """
    workspaces_dir = (
        config.data_dir / "workspaces" / f"{config.repo.owner}__{config.repo.name}"
    )
    if not workspaces_dir.is_dir():
        return 0

    cutoff = time.time() - config.workspace_retention_days * _SECONDS_PER_DAY
    active = active_issues or set()
    candidates: dict[int, Path] = {}
    for path in workspaces_dir.iterdir():
        issue_number = _issue_number(path.name)
        if issue_number is None or issue_number in active or not path.is_dir():
            continue
        if _latest_mtime(path) < cutoff:
            candidates[issue_number] = path

    if not candidates:
        return 0

    try:
        open_issues = github.open_pr_issue_numbers(
            config.repo.full_name,
            candidates,
            token,
        )
    except GitHubError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).warning(
            "Skipping workspace retention because the open-PR check failed"
        )
        return 0

    removed = 0
    for issue_number, path in candidates.items():
        if issue_number in open_issues:
            continue
        gitops.cleanup_workspace(path)
        if path.exists():
            continue
        removed += 1
        log.info("Removed expired workspace for issue #{}", issue_number)
    return removed


def _prune_issue(issue_dir: Path, cutoff: float) -> int:
    removed = 0
    for run_dir in issue_dir.iterdir():
        if not run_dir.is_dir() or run_dir.stat().st_mtime >= cutoff:
            continue
        shutil.rmtree(run_dir, ignore_errors=True)
        removed += 1
    return removed


def _issue_number(name: str) -> int | None:
    """Return the issue number encoded in ``issue-<N>``, if valid."""
    prefix, separator, digits = name.partition("-")
    if prefix != "issue" or not separator or not digits.isdigit():
        return None
    number = int(digits)
    return number if number > 0 else None


def _latest_mtime(path: Path) -> float:
    """Return the newest modification time in a workspace tree.

    Args:
        path: Workspace root.

    Returns:
        The newest mtime, or infinity when the tree cannot be inspected so the
        workspace is kept.
    """
    try:
        latest = path.stat().st_mtime
    except OSError:
        return float("inf")
    for current, directories, files in os.walk(path):
        for name in (*directories, *files):
            try:
                latest = max(latest, os.stat(os.path.join(current, name)).st_mtime)
            except OSError:
                continue
    return latest


def _remove_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        return

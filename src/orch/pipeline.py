"""The issue to pull request pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from orch import github
from orch.agent import WorkerResult, run_worker
from orch.config import Config
from orch.gitops import authenticated_remote, import_bundle, push_branch
from orch.github import Issue
from orch.store import create_run, finish_run

_STATUS_PR_OPEN = "pr_open"
_STATUS_PARKED = "parked"


@dataclass(frozen=True)
class RunOutcome:
    """Result of one issue run."""

    run_id: str
    status: str
    run_dir: Path
    branch: str
    pr_url: str | None = None
    error: str | None = None


def run_issue(config: Config, issue: Issue, token: str) -> RunOutcome:
    """Run the full MVP-0 pipeline for one issue.

    Args:
        config: Orchestrator configuration.
        issue: Issue to implement.
        token: GitHub token.

    Returns:
        Outcome of the run. Failures are parked instead of raised so the
        daemon can continue with the next issue.
    """
    run_id = _new_run_id(issue.number)
    branch = f"agent/{issue.number}"
    run_dir = _create_run_dir(config, issue, run_id)
    repo = config.repo.full_name

    create_run(
        config.data_dir / "orch.db",
        run_id=run_id,
        repo=repo,
        issue=issue.number,
        branch=branch,
    )

    try:
        github.edit_labels(
            repo,
            issue.number,
            token,
            add=(config.labels.running,),
            remove=(config.labels.ready,),
        )
        worker = run_worker(config, issue, run_dir)
        _publish_branch(config, worker, token)
        pr_url = github.create_pr(
            repo,
            token,
            head=worker.branch,
            base=config.repo.base_branch,
            title=_pr_title(issue),
            body=_pr_body(config, issue, worker, run_id),
        )
        github.comment_issue(
            repo,
            issue.number,
            token,
            _success_comment(issue, worker, pr_url, run_id),
        )
        github.edit_labels(
            repo,
            issue.number,
            token,
            add=(config.labels.pr_open,),
            remove=(config.labels.running,),
        )
        _write_result(
            run_dir,
            {
                "run_id": run_id,
                "status": _STATUS_PR_OPEN,
                "branch": worker.branch,
                "pr_url": pr_url,
                "tests_passed": True,
            },
        )
        finish_run(
            config.data_dir / "orch.db",
            run_id=run_id,
            status=_STATUS_PR_OPEN,
            pr_url=pr_url,
        )
        logger.info("Issue #{} is ready for review: {}", issue.number, pr_url)
        return RunOutcome(
            run_id=run_id,
            status=_STATUS_PR_OPEN,
            run_dir=run_dir,
            branch=worker.branch,
            pr_url=pr_url,
        )
    except Exception as ex:
        logger.bind(
            issue=issue.number,
            run_id=run_id,
            error=ex,
            error_type=type(ex).__name__,
        ).error("Issue run failed; parking")
        _park(config, issue, token, run_dir, run_id, branch, ex)
        return RunOutcome(
            run_id=run_id,
            status=_STATUS_PARKED,
            run_dir=run_dir,
            branch=branch,
            error=str(ex),
        )


def _publish_branch(config: Config, worker: WorkerResult, token: str) -> None:
    bare = import_bundle(worker.bundle_path, worker.branch, worker.run_dir)
    remote = authenticated_remote(config.repo.owner, config.repo.name, token)
    push_branch(bare, worker.branch, remote, token)


def _park(
    config: Config,
    issue: Issue,
    token: str,
    run_dir: Path,
    run_id: str,
    branch: str,
    error: Exception,
) -> None:
    message = (
        "orch parked this issue.\n\n"
        f"Error: `{type(error).__name__}: {error}`\n\n"
        f"Run: `{run_id}`\n"
        f"Branch: `{branch}`\n"
    )
    try:
        github.comment_issue(config.repo.full_name, issue.number, token, message)
        github.edit_labels(
            config.repo.full_name,
            issue.number,
            token,
            add=(config.labels.parked,),
            remove=(config.labels.running,),
        )
    except Exception as notify_error:
        logger.bind(
            issue=issue.number,
            error=notify_error,
            error_type=type(notify_error).__name__,
        ).error("Could not park issue on GitHub")

    _write_result(
        run_dir,
        {
            "run_id": run_id,
            "status": _STATUS_PARKED,
            "branch": branch,
            "error": f"{type(error).__name__}: {error}",
        },
    )
    finish_run(
        config.data_dir / "orch.db",
        run_id=run_id,
        status=_STATUS_PARKED,
        error=f"{type(error).__name__}: {error}",
    )


def _pr_title(issue: Issue) -> str:
    title = f"agent: issue #{issue.number} {issue.title}".strip()
    return title[:200]


def _pr_body(
    config: Config,
    issue: Issue,
    worker: WorkerResult,
    run_id: str,
) -> str:
    summary = worker.summary.strip()
    if len(summary) > 6000:
        summary = summary[:6000] + "\n\n_(truncated)_"

    return (
        f"Closes #{issue.number}\n\n"
        "Automated pull request from `orch`.\n\n"
        "## Summary\n\n"
        f"{summary}\n\n"
        "## Tests\n\n"
        "Passed in the worker container.\n\n"
        "## Run\n\n"
        f"- Run ID: `{run_id}`\n"
        f"- Worker model: `{config.worker.model}`\n"
    )


def _success_comment(
    issue: Issue,
    worker: WorkerResult,
    pr_url: str,
    run_id: str,
) -> str:
    return (
        f"orch opened a pull request for #{issue.number}: {pr_url}\n\n"
        f"Tests passed. Run: `{run_id}`\n\n"
        f"{worker.summary.strip()[:3000]}\n"
    )


def _new_run_id(issue: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ").lower()
    return f"{stamp}-{issue}"


def _create_run_dir(config: Config, issue: Issue, run_id: str) -> Path:
    run_dir = (
        config.data_dir
        / "runs"
        / f"{config.repo.owner}__{config.repo.name}"
        / f"issue-{issue.number}"
        / run_id
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _write_result(run_dir: Path, payload: dict[str, object]) -> None:
    (run_dir / "result.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

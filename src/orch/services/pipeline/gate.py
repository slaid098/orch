"""Checks, reviewer, and auto-merge gate."""

from __future__ import annotations

import time
from pathlib import Path

from orch.config.logger import get_logger
from orch.exceptions.github import GitHubError
from orch.exceptions.pipeline import ChecksFailed
from orch.exceptions.worker import RunCancelled
from orch.schemas.config.types import Config
from orch.schemas.github.types import Issue, PullRequestStatus
from orch.schemas.pipeline.types import AttemptSuccess, RunOutcome
from orch.schemas.reviewer.types import ReviewFinding, ReviewResult
from orch.services import git as gitops
from orch.services import github, notify, store
from orch.services.feedback.service import CommentFeed
from orch.services.pipeline.lifecycle import (
    _STATUS_MERGED,
    _STATUS_PR_OPEN,
    _best_effort_comment,
    _best_effort_labels,
    _cancel_run,
    _park_after_retries,
    _write_result,
)
from orch.services.reviewer import run_review

log = get_logger(component="pipeline")


def _review_attempt(
    config: Config,
    issue: Issue,
    attempt_result: AttemptSuccess,
    *,
    workspace: Path,
    run_dir: Path,
    checks_summary: str,
    review_round: int,
) -> ReviewResult:
    return run_review(
        config,
        issue,
        pr_url=attempt_result.pr_url,
        workspace_path=workspace,
        diff_path=attempt_result.worker.run_dir / "diff.patch",
        run_dir=run_dir / f"review-{review_round}",
        checks_summary=checks_summary,
    )


def _wait_for_checks(
    config: Config,
    issue: Issue,
    token: str,
    pr_url: str,
    feed: CommentFeed | None,
    *,
    expected_head: str = "",
) -> str:
    started = time.monotonic()
    deadline = started + config.checks.timeout_minutes * 60
    discovery_deadline = started + config.checks.discovery_seconds
    last_status = None
    last_error = ""
    while True:
        reason = _cancellation_reason(config, issue, token, feed)
        if reason is not None:
            raise RunCancelled(reason)
        try:
            status = github.get_pr_status(config.repo.full_name, pr_url, token)
        except GitHubError as ex:
            last_error = str(ex)
            log.bind(pr_url=pr_url, error=ex, error_type=type(ex).__name__).warning(
                "Could not read pull-request checks; retrying"
            )
        else:
            if expected_head and status.head_sha != expected_head:
                log.bind(
                    pr_url=pr_url,
                    expected_head=expected_head,
                    actual_head=status.head_sha,
                ).info("Waiting for GitHub to report checks for the pushed commit")
                last_status = status
            else:
                last_status = status
                if status.failed:
                    raise ChecksFailed(_format_check_failure(status))
                if status.mergeable == "CONFLICTING":
                    raise ChecksFailed("pull request has merge conflicts")
                if status.checks and not status.pending:
                    return _format_checks(status)
                if not status.checks and time.monotonic() >= discovery_deadline:
                    return "No GitHub checks were reported; local tests passed."
        now = time.monotonic()
        if now >= deadline:
            if last_status is None:
                raise ChecksFailed(f"Could not read GitHub checks before timeout: {last_error}")
            raise ChecksFailed("GitHub checks did not finish before timeout")
        time.sleep(min(config.checks.poll_seconds, max(1, deadline - now)))


def _format_checks(status: PullRequestStatus) -> str:
    lines = [f"{check.name}: {check.state}" for check in status.checks]
    return "\n".join(lines) if lines else "No GitHub checks were reported."


def _format_check_failure(status: PullRequestStatus) -> str:
    failed = [check for check in status.checks if check.state == "failure"]
    lines = [f"{check.name}: {check.state}" for check in failed]
    details = "\n".join(lines) or "unknown check failure"
    return f"GitHub checks failed:\n{details}"


def _finish_pr_open(
    config: Config,
    issue: Issue,
    token: str,
    attempt_result: AttemptSuccess,
    *,
    run_dir: Path,
    run_id: str,
    attempt: int,
    checks_summary: str,
    review_rounds: int = 0,
) -> RunOutcome:
    _best_effort_labels(
        config.repo.full_name,
        issue.number,
        token,
        add=(config.labels.pr_open,),
        remove=(config.labels.running, config.labels.needs_human),
    )
    _write_result(
        run_dir,
        {
            "run_id": run_id,
            "status": _STATUS_PR_OPEN,
            "branch": attempt_result.worker.branch,
            "pr_url": attempt_result.pr_url,
            "tests_passed": True,
            "checks_passed": True,
            "checks": checks_summary,
            "attempts": attempt,
            "review_rounds": review_rounds,
        },
    )
    store.finish_run(
        config.data_dir / "orch.db",
        run_id=run_id,
        status=_STATUS_PR_OPEN,
        pr_url=attempt_result.pr_url,
    )
    log.info(
        "Issue #{} is waiting for manual review and merge: {}",
        issue.number,
        attempt_result.pr_url,
    )
    return RunOutcome(
        run_id=run_id,
        status=_STATUS_PR_OPEN,
        run_dir=run_dir,
        branch=attempt_result.worker.branch,
        pr_url=attempt_result.pr_url,
        review_rounds=review_rounds,
    )


def _merge_and_finish(
    config: Config,
    issue: Issue,
    token: str,
    attempt_result: AttemptSuccess,
    *,
    run_dir: Path,
    run_id: str,
    workspace: Path,
    attempt: int,
    review_rounds: int,
    checks_summary: str,
) -> RunOutcome:
    guard_error = _merge_guard(config, issue, attempt_result)
    if guard_error is not None:
        return _park_merge_error(
            config,
            issue,
            token,
            attempt_result,
            guard_error,
            run_dir=run_dir,
            run_id=run_id,
            attempt=attempt,
            review_rounds=review_rounds,
        )
    try:
        merge_commit = github.merge_pr(
            config.repo.full_name,
            attempt_result.pr_url,
            token,
        )
    except GitHubError as ex:
        _log_merge_error(issue, attempt_result, ex)
        return _park_merge_error(
            config,
            issue,
            token,
            attempt_result,
            ex,
            run_dir=run_dir,
            run_id=run_id,
            attempt=attempt,
            review_rounds=review_rounds,
        )
    return _record_merge(
        config,
        issue,
        token,
        attempt_result,
        run_dir=run_dir,
        run_id=run_id,
        workspace=workspace,
        attempt=attempt,
        review_rounds=review_rounds,
        checks_summary=checks_summary,
        merge_commit=merge_commit,
    )


def _log_merge_error(
    issue: Issue,
    attempt_result: AttemptSuccess,
    error: GitHubError,
) -> None:
    log.bind(
        issue=issue.number,
        pr_url=attempt_result.pr_url,
        error=error,
        error_type=type(error).__name__,
    ).error("Could not merge the approved pull request")


def _park_merge_error(
    config: Config,
    issue: Issue,
    token: str,
    attempt_result: AttemptSuccess,
    error: Exception,
    *,
    run_dir: Path,
    run_id: str,
    attempt: int,
    review_rounds: int,
) -> RunOutcome:
    notify.notify_error(
        config,
        stage="merge",
        error=error,
        issue_number=issue.number,
        issue_title=issue.title,
        run_id=run_id,
        pr_url=attempt_result.pr_url,
        attempt=attempt,
        max_attempts=config.worker.max_attempts,
    )
    return _park_after_retries(
        config,
        issue,
        token,
        run_dir=run_dir,
        run_id=run_id,
        branch=attempt_result.worker.branch,
        error=error,
        attempts=attempt,
        pr_url=attempt_result.pr_url,
        review_rounds=review_rounds,
    )


def _merge_guard(
    config: Config,
    issue: Issue,
    attempt_result: AttemptSuccess,
) -> RuntimeError | None:
    if config.repo.base_branch == "main":
        return None
    log.bind(
        issue=issue.number,
        pr_url=attempt_result.pr_url,
        base=config.repo.base_branch,
    ).error("Refusing to auto-merge outside main")
    return RuntimeError(f"Auto-merge is restricted to main, not {config.repo.base_branch!r}")


def _record_merge(
    config: Config,
    issue: Issue,
    token: str,
    attempt_result: AttemptSuccess,
    *,
    run_dir: Path,
    run_id: str,
    workspace: Path,
    attempt: int,
    review_rounds: int,
    checks_summary: str,
    merge_commit: str,
) -> RunOutcome:
    _best_effort_comment(
        config.repo.full_name,
        issue.number,
        token,
        f"Ревьюер одобрил PR. Merge: {attempt_result.pr_url}\n\n"
        f"Run: `{run_id}`. Раундов ревью: `{review_rounds}`.",
    )
    _write_result(
        run_dir,
        {
            "run_id": run_id,
            "status": _STATUS_MERGED,
            "branch": attempt_result.worker.branch,
            "pr_url": attempt_result.pr_url,
            "merge_commit": merge_commit,
            "tests_passed": True,
            "checks_passed": True,
            "checks": checks_summary,
            "attempts": attempt,
            "review_rounds": review_rounds,
        },
    )
    store.finish_run(
        config.data_dir / "orch.db",
        run_id=run_id,
        status=_STATUS_MERGED,
        pr_url=attempt_result.pr_url,
    )
    gitops.cleanup_workspace(workspace)
    log.info("Issue #{} merged: {}", issue.number, attempt_result.pr_url)
    return RunOutcome(
        run_id=run_id,
        status=_STATUS_MERGED,
        run_dir=run_dir,
        branch=attempt_result.worker.branch,
        pr_url=attempt_result.pr_url,
        review_rounds=review_rounds,
        merge_commit=merge_commit or None,
    )


def _cancel_if_needed(
    config: Config,
    issue: Issue,
    token: str,
    feed: CommentFeed | None,
    *,
    run_dir: Path,
    run_id: str,
    branch: str,
    attempts: int,
) -> RunOutcome | None:
    reason = _cancellation_reason(config, issue, token, feed)
    if reason is None:
        return None
    return _cancel_run(
        config,
        issue,
        run_dir,
        run_id,
        branch,
        reason,
        attempts=attempts,
    )


def _cancellation_reason(
    config: Config,
    issue: Issue,
    token: str,
    feed: CommentFeed | None,
) -> str | None:
    if feed is not None and feed.cancelled:
        return feed.cancel_reason or "issue closed"
    try:
        is_open = github.issue_is_open(config.repo.full_name, issue.number, token)
    except GitHubError as ex:
        log.bind(
            issue=issue.number,
            error=ex,
            error_type=type(ex).__name__,
        ).warning("Could not verify that the issue is still open; continuing")
        return None
    else:
        return None if is_open else "issue closed"


def _comment_review_findings(
    config: Config,
    issue: Issue,
    token: str,
    findings: tuple[ReviewFinding, ...],
) -> None:
    _best_effort_comment(
        config.repo.full_name,
        issue.number,
        token,
        _format_findings(findings),
    )


def _format_findings(findings: tuple[ReviewFinding, ...]) -> str:
    lines = [
        "Ревьюер заблокировал merge. Все замечания обязательны:",
        "",
    ]
    for index, finding in enumerate(findings, start=1):
        lines.append(f"{index}. `{finding.file}:{finding.line}` — {finding.title}")
        lines.append(f"   Ситуация: {finding.situation}")
        if finding.fixes:
            lines.append("   Варианты: " + "; ".join(finding.fixes))
        lines.append("")
    return "\n".join(lines).rstrip()


def _ensure_issue_open(config: Config, issue: Issue, token: str) -> None:
    try:
        is_open = github.issue_is_open(
            config.repo.full_name,
            issue.number,
            token,
        )
    except GitHubError as ex:
        log.bind(
            issue=issue.number,
            error=ex,
            error_type=type(ex).__name__,
        ).warning("Could not verify the issue before opening a PR; continuing")
    else:
        if not is_open:
            raise RunCancelled("issue closed before pull request creation")

"""The issue to pull request pipeline."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from orch.config.logger import get_logger
from orch.exceptions.pipeline import ReviewRejected
from orch.exceptions.reviewer import ReviewerError
from orch.exceptions.standards import StandardsError
from orch.schemas.config.types import Config
from orch.schemas.github.types import Issue
from orch.schemas.pipeline.types import AttemptCycle, AttemptSuccess, RunOutcome
from orch.schemas.reviewer.types import ReviewFinding
from orch.schemas.worker.types import WorkerResult
from orch.services import git as gitops
from orch.services import github, notify, store
from orch.services.feedback.service import CommentFeed
from orch.services.pipeline.failures import (
    _comment_attempt_failure as _comment_attempt_failure,
)
from orch.services.pipeline.failures import (
    _retry_outcome,
)
from orch.services.pipeline.gate import (
    _cancel_if_needed,
    _comment_review_findings,
    _ensure_issue_open,
    _finish_pr_open,
    _format_findings,
    _merge_and_finish,
    _review_attempt,
    _wait_for_checks,
)
from orch.services.pipeline.lifecycle import (
    _STATUS_NEEDS_HUMAN,
    _STATUS_PR_OPEN,
    _best_effort_comment,
    _best_effort_labels,
    _park,
    _park_after_retries,
    _park_invalid_title,
    _workspace_path,
    _write_result,
)
from orch.services.pipeline.publish import (
    _delete_stale_branch as _delete_stale_branch,
)
from orch.services.pipeline.publish import (
    _find_open_pr,
    _publish_success,
)
from orch.services.pipeline.publish import (
    _publish_branch as _publish_branch,
)
from orch.services.standards import (
    branch_name,
    validate_branch,
    validate_title,
)
from orch.services.worker import run_worker

log = get_logger(component="pipeline")


def run_issue(
    config: Config,
    issue: Issue,
    token: str,
    *,
    claim: bool = True,
    attempt_timeout_seconds: int | None = None,
) -> RunOutcome:
    """Run the full pipeline for one issue, including bounded retries.

    Args:
        config: Orchestrator configuration.
        issue: Issue to implement.
        token: GitHub token.
        claim: Whether this call should move the issue from queued to running.
        attempt_timeout_seconds: Optional test/override budget for one attempt.

    Returns:
        Outcome of the run. Failures become ``needs_human`` rather than raising
        so a daemon worker can release its slot and continue.
    """
    started_at = time.monotonic()
    run_id = _new_run_id(issue.number)
    run_dir = _create_run_dir(config, issue, run_id)
    repo = config.repo.full_name
    timeout_seconds = attempt_timeout_seconds or config.worker.timeout_minutes * 60

    identity = _resolve_identity(config, issue, token, run_dir, run_id)
    if isinstance(identity, RunOutcome):
        return _with_run_metrics(identity, issue, started_at)
    title, branch = identity

    store.create_run(
        config.data_dir / "orch.db",
        run_id=run_id,
        repo=repo,
        issue=issue.number,
        branch=branch,
    )
    try:
        outcome = _start_run(
            config,
            issue,
            token,
            run_id=run_id,
            run_dir=run_dir,
            branch=branch,
            title=title,
            timeout_seconds=timeout_seconds,
            claim=claim,
        )
    except Exception as ex:
        outcome = _park_before_attempts(
            config,
            issue,
            token,
            run_dir=run_dir,
            run_id=run_id,
            branch=branch,
            error=ex,
        )
    return _with_run_metrics(outcome, issue, started_at)


def _with_run_metrics(
    outcome: RunOutcome,
    issue: Issue,
    started_at: float,
) -> RunOutcome:
    """Attach issue title and elapsed time for notifications and logs."""
    return replace(
        outcome,
        issue_title=issue.title,
        duration_seconds=max(0, int(time.monotonic() - started_at)),
    )


def _resolve_identity(
    config: Config,
    issue: Issue,
    token: str,
    run_dir: Path,
    run_id: str,
) -> tuple[str, str] | RunOutcome:
    try:
        title = validate_title(issue.title)
        return title, branch_name(title, issue.number)
    except StandardsError as ex:
        store.create_run(
            config.data_dir / "orch.db",
            run_id=run_id,
            repo=config.repo.full_name,
            issue=issue.number,
            branch="",
        )
        _park_invalid_title(config, issue, token, run_dir, run_id, ex)
        return RunOutcome(
            run_id=run_id,
            status=_STATUS_NEEDS_HUMAN,
            run_dir=run_dir,
            branch="",
            error=str(ex),
        )


def _start_run(
    config: Config,
    issue: Issue,
    token: str,
    *,
    run_id: str,
    run_dir: Path,
    branch: str,
    title: str,
    timeout_seconds: int,
    claim: bool,
) -> RunOutcome:
    repo = config.repo.full_name
    existing_pr = _find_open_pr(repo, branch, token)
    if existing_pr:
        return _finish_existing_pr(config, issue, token, run_dir, run_id, branch, existing_pr)
    if claim:
        github.edit_labels(
            repo,
            issue.number,
            token,
            add=(config.labels.running,),
            remove=(config.labels.queued, config.labels.needs_human),
        )
    return _run_attempts(
        config,
        issue,
        token,
        run_id=run_id,
        run_dir=run_dir,
        branch=branch,
        title=title,
        timeout_seconds=timeout_seconds,
    )


def _park_before_attempts(
    config: Config,
    issue: Issue,
    token: str,
    *,
    run_dir: Path,
    run_id: str,
    branch: str,
    error: Exception,
) -> RunOutcome:
    log.bind(
        issue=issue.number,
        run_id=run_id,
        error=error,
        error_type=type(error).__name__,
    ).error("Issue run failed before attempts; parking")
    _park(
        config,
        issue,
        token,
        run_dir,
        run_id,
        branch,
        error,
        attempts=0,
    )
    return RunOutcome(
        run_id=run_id,
        status=_STATUS_NEEDS_HUMAN,
        run_dir=run_dir,
        branch=branch,
        error=str(error),
    )


def _remaining_attempt_seconds(started: float, budget_seconds: int) -> int:
    remaining = budget_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutError("Attempt timed out before the worker started")
    return max(1, int(remaining))


def _finish_existing_pr(
    config: Config,
    issue: Issue,
    token: str,
    run_dir: Path,
    run_id: str,
    branch: str,
    pr_url: str,
) -> RunOutcome:
    log.info("Issue #{} already has an open PR: {}", issue.number, pr_url)
    _best_effort_comment(
        config.repo.full_name,
        issue.number,
        token,
        f"PR уже открыт: {pr_url}\n\nRun: `{run_id}`",
    )
    _best_effort_labels(
        config.repo.full_name,
        issue.number,
        token,
        add=(config.labels.pr_open,),
        remove=(
            config.labels.queued,
            config.labels.running,
            config.labels.needs_human,
        ),
    )
    _write_result(
        run_dir,
        {
            "run_id": run_id,
            "status": _STATUS_PR_OPEN,
            "branch": branch,
            "pr_url": pr_url,
            "existing_pr": True,
        },
    )
    store.finish_run(
        config.data_dir / "orch.db",
        run_id=run_id,
        status=_STATUS_PR_OPEN,
        pr_url=pr_url,
    )
    gitops.cleanup_workspace(_workspace_path(config, issue))
    return RunOutcome(
        run_id=run_id,
        status=_STATUS_PR_OPEN,
        run_dir=run_dir,
        branch=branch,
        pr_url=pr_url,
    )


def _run_attempts(
    config: Config,
    issue: Issue,
    token: str,
    *,
    run_id: str,
    run_dir: Path,
    branch: str,
    title: str,
    timeout_seconds: int,
) -> RunOutcome:
    workspace = _workspace_path(config, issue)
    source: Path | None = None
    feed: CommentFeed | None = None
    initial_context = ""
    if config.feedback.enabled and config.feedback.live_comments:
        feed = CommentFeed(
            config,
            issue,
            token,
            state_path=run_dir / "feedback.json",
        )
        initial_context = feed.initial_context()
        feed.start()
    try:
        mirror = gitops.refresh_mirror(
            config.data_dir,
            config.repo.owner,
            config.repo.name,
            config.repo.base_branch,
            token,
        )
        source = gitops.clone_source(
            mirror,
            config.repo.base_branch,
            run_dir / "source",
        )
        return _retry_until_success(
            config,
            issue,
            token,
            run_id=run_id,
            run_dir=run_dir,
            branch=branch,
            title=title,
            timeout_seconds=timeout_seconds,
            source=source,
            workspace=workspace,
            feed=feed,
            initial_context=initial_context,
        )
    finally:
        if feed is not None:
            feed.stop()
        if source is not None:
            gitops.cleanup_source(source)


def _retry_until_success(
    config: Config,
    issue: Issue,
    token: str,
    *,
    run_id: str,
    run_dir: Path,
    branch: str,
    title: str,
    timeout_seconds: int,
    source: Path,
    workspace: Path,
    feed: CommentFeed | None,
    initial_context: str,
) -> RunOutcome:
    last_error: Exception | None = None
    last_pr_url: str | None = None
    review_rounds = 0
    for attempt in range(1, config.worker.max_attempts + 1):
        cycle = _run_attempt_cycle(
            config,
            issue,
            token,
            run_id=run_id,
            run_dir=run_dir,
            branch=branch,
            title=title,
            timeout_seconds=timeout_seconds,
            source=source,
            workspace=workspace,
            feed=feed,
            initial_context=initial_context,
            attempt=attempt,
            previous_error=last_error,
            review_rounds=review_rounds,
            last_pr_url=last_pr_url,
        )
        if cycle.outcome is not None:
            return cycle.outcome
        last_error = cycle.error
        review_rounds = cycle.review_rounds
        last_pr_url = cycle.pr_url or last_pr_url
    error = last_error or RuntimeError("No attempt produced a pull request")
    return _park_after_retries(
        config,
        issue,
        token,
        run_dir=run_dir,
        run_id=run_id,
        branch=branch,
        error=error,
        attempts=config.worker.max_attempts,
        pr_url=last_pr_url,
        review_rounds=review_rounds,
    )


def _run_attempt_cycle(
    config: Config,
    issue: Issue,
    token: str,
    *,
    run_id: str,
    run_dir: Path,
    branch: str,
    title: str,
    timeout_seconds: int,
    source: Path,
    workspace: Path,
    feed: CommentFeed | None,
    initial_context: str,
    attempt: int,
    previous_error: Exception | None,
    review_rounds: int,
    last_pr_url: str | None,
) -> AttemptCycle:
    try:
        attempt_result = _run_attempt(
            config,
            issue,
            token,
            run_id=run_id,
            run_dir=run_dir,
            branch=branch,
            title=title,
            timeout_seconds=timeout_seconds,
            source=source,
            workspace=workspace,
            feed=feed,
            initial_context=initial_context,
            attempt=attempt,
            previous_error=previous_error,
        )
    except Exception as ex:
        return _worker_failure_cycle(
            config,
            issue,
            token,
            ex,
            run_id=run_id,
            run_dir=run_dir,
            branch=branch,
            attempt=attempt,
            review_rounds=review_rounds,
            last_pr_url=last_pr_url,
        )
    if isinstance(attempt_result, RunOutcome):
        return AttemptCycle(attempt_result, None, review_rounds, last_pr_url)
    return _gate_cycle(
        config,
        issue,
        token,
        attempt_result,
        run_id=run_id,
        run_dir=run_dir,
        branch=branch,
        attempt=attempt,
        review_rounds=review_rounds,
        feed=feed,
        workspace=workspace,
    )


def _worker_failure_cycle(
    config: Config,
    issue: Issue,
    token: str,
    error: Exception,
    *,
    run_id: str,
    run_dir: Path,
    branch: str,
    attempt: int,
    review_rounds: int,
    last_pr_url: str | None,
) -> AttemptCycle:
    outcome = _retry_outcome(
        config,
        issue,
        token,
        error,
        run_id=run_id,
        run_dir=run_dir,
        branch=branch,
        attempt=attempt,
    )
    return AttemptCycle(outcome, error, review_rounds, last_pr_url)


def _gate_cycle(
    config: Config,
    issue: Issue,
    token: str,
    attempt_result: AttemptSuccess,
    *,
    run_id: str,
    run_dir: Path,
    branch: str,
    attempt: int,
    review_rounds: int,
    feed: CommentFeed | None,
    workspace: Path,
) -> AttemptCycle:
    checks_summary, error, outcome = _checks_cycle(
        config,
        issue,
        token,
        attempt_result,
        feed,
        run_id=run_id,
        run_dir=run_dir,
        branch=branch,
        attempt=attempt,
    )
    if outcome is not None or error is not None:
        return AttemptCycle(outcome, error, review_rounds, attempt_result.pr_url)
    if not config.reviewer.enabled:
        return AttemptCycle(
            _finish_pr_open(
                config,
                issue,
                attempt_result,
                run_dir=run_dir,
                run_id=run_id,
                workspace=workspace,
                attempt=attempt,
                checks_summary=checks_summary,
            ),
            None,
            review_rounds,
            attempt_result.pr_url,
        )
    return _review_cycle(
        config,
        issue,
        token,
        attempt_result,
        run_dir=run_dir,
        run_id=run_id,
        branch=branch,
        attempt=attempt,
        review_rounds=review_rounds,
        checks_summary=checks_summary,
        workspace=workspace,
    )


def _checks_cycle(
    config: Config,
    issue: Issue,
    token: str,
    attempt_result: AttemptSuccess,
    feed: CommentFeed | None,
    *,
    run_id: str,
    run_dir: Path,
    branch: str,
    attempt: int,
) -> tuple[str | None, Exception | None, RunOutcome | None]:
    try:
        checks = (
            _wait_for_checks(
                config,
                issue,
                token,
                attempt_result.pr_url,
                feed,
                expected_head=attempt_result.head_sha,
            )
            if attempt_result.head_sha
            else _wait_for_checks(
                config,
                issue,
                token,
                attempt_result.pr_url,
                feed,
            )
        )
        return checks, None, None
    except Exception as ex:
        outcome = _retry_outcome(
            config,
            issue,
            token,
            ex,
            run_id=run_id,
            run_dir=run_dir,
            branch=branch,
            attempt=attempt,
        )
        return None, ex, outcome


def _review_cycle(
    config: Config,
    issue: Issue,
    token: str,
    attempt_result: AttemptSuccess,
    *,
    run_dir: Path,
    run_id: str,
    branch: str,
    attempt: int,
    review_rounds: int,
    checks_summary: str,
    workspace: Path,
) -> AttemptCycle:
    review_round = review_rounds + 1
    try:
        review = _review_attempt(
            config,
            issue,
            attempt_result,
            workspace=workspace,
            run_dir=run_dir,
            checks_summary=checks_summary,
            review_round=review_round,
        )
    except Exception as ex:
        return _review_failure_cycle(
            config,
            issue,
            token,
            attempt_result,
            ex,
            run_dir=run_dir,
            run_id=run_id,
            branch=branch,
            attempt=attempt,
            review_round=review_round,
        )
    if review.approved:
        return _approved_review_cycle(
            config,
            issue,
            token,
            attempt_result,
            run_dir=run_dir,
            run_id=run_id,
            workspace=workspace,
            attempt=attempt,
            review_round=review_round,
            checks_summary=checks_summary,
        )
    return _rejected_review_cycle(
        config,
        issue,
        token,
        attempt_result,
        review.findings,
        run_dir=run_dir,
        run_id=run_id,
        branch=branch,
        attempt=attempt,
        review_round=review_round,
    )


def _review_failure_cycle(
    config: Config,
    issue: Issue,
    token: str,
    attempt_result: AttemptSuccess,
    error: Exception,
    *,
    run_dir: Path,
    run_id: str,
    branch: str,
    attempt: int,
    review_round: int,
) -> AttemptCycle:
    reviewer_error = _reviewer_error(error)
    outcome = _park_reviewer_failure(
        config,
        issue,
        token,
        attempt_result,
        reviewer_error,
        run_dir=run_dir,
        run_id=run_id,
        branch=branch,
        attempt=attempt,
        review_round=review_round,
    )
    return AttemptCycle(
        outcome,
        reviewer_error,
        review_round,
        attempt_result.pr_url,
    )


def _approved_review_cycle(
    config: Config,
    issue: Issue,
    token: str,
    attempt_result: AttemptSuccess,
    *,
    run_dir: Path,
    run_id: str,
    workspace: Path,
    attempt: int,
    review_round: int,
    checks_summary: str,
) -> AttemptCycle:
    outcome = _merge_and_finish(
        config,
        issue,
        token,
        attempt_result,
        run_dir=run_dir,
        run_id=run_id,
        workspace=workspace,
        attempt=attempt,
        review_rounds=review_round,
        checks_summary=checks_summary,
    )
    return AttemptCycle(outcome, None, review_round, attempt_result.pr_url)


def _rejected_review_cycle(
    config: Config,
    issue: Issue,
    token: str,
    attempt_result: AttemptSuccess,
    findings: tuple[ReviewFinding, ...],
    *,
    run_dir: Path,
    run_id: str,
    branch: str,
    attempt: int,
    review_round: int,
) -> AttemptCycle:
    review_error = ReviewRejected(_format_findings(findings))
    _comment_review_findings(config, issue, token, findings)
    notify.notify_review_blocked(
        config,
        issue.number,
        issue.title,
        findings=len(findings),
        review_round=review_round,
        max_rounds=config.reviewer.max_rounds,
        run_id=run_id,
        pr_url=attempt_result.pr_url,
    )
    if review_round >= config.reviewer.max_rounds or attempt >= config.worker.max_attempts:
        outcome = _park_after_retries(
            config,
            issue,
            token,
            run_dir=run_dir,
            run_id=run_id,
            branch=branch,
            error=review_error,
            attempts=attempt,
            pr_url=attempt_result.pr_url,
            review_rounds=review_round,
        )
        return AttemptCycle(
            outcome,
            review_error,
            review_round,
            attempt_result.pr_url,
        )
    return AttemptCycle(None, review_error, review_round, attempt_result.pr_url)


def _reviewer_error(error: Exception) -> ReviewerError:
    if isinstance(error, ReviewerError):
        return error
    return ReviewerError(f"reviewer crashed: {error}")


def _park_reviewer_failure(
    config: Config,
    issue: Issue,
    token: str,
    attempt_result: AttemptSuccess,
    error: ReviewerError,
    *,
    run_dir: Path,
    run_id: str,
    branch: str,
    attempt: int,
    review_round: int,
) -> RunOutcome:
    log.bind(
        issue=issue.number,
        pr_url=attempt_result.pr_url,
        review_round=review_round,
        error=error,
        error_type=type(error).__name__,
    ).error("Reviewer failed technically; parking")
    notify.notify_error(
        config,
        stage="reviewer",
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
        branch=branch,
        error=error,
        attempts=attempt,
        pr_url=attempt_result.pr_url,
        review_rounds=review_round,
    )


def _run_attempt(
    config: Config,
    issue: Issue,
    token: str,
    *,
    run_id: str,
    run_dir: Path,
    branch: str,
    title: str,
    timeout_seconds: int,
    source: Path,
    workspace: Path,
    feed: CommentFeed | None,
    initial_context: str,
    attempt: int,
    previous_error: Exception | None,
) -> AttemptSuccess | RunOutcome:
    cancelled = _attempt_cancelled(
        config,
        issue,
        token,
        feed,
        run_dir=run_dir,
        run_id=run_id,
        branch=branch,
        attempts=attempt - 1,
    )
    if cancelled is not None:
        return cancelled
    worker = _run_worker_attempt(
        config,
        issue,
        run_dir / f"attempt-{attempt}",
        source=source,
        workspace=workspace,
        attempt=attempt,
        previous_error=previous_error,
        timeout_seconds=timeout_seconds,
        feedback=feed,
        initial_context=initial_context if attempt == 1 else "",
    )
    cancelled = _attempt_cancelled(
        config,
        issue,
        token,
        feed,
        run_dir=run_dir,
        run_id=run_id,
        branch=branch,
        attempts=attempt,
    )
    if cancelled is not None:
        return cancelled
    return _publish_attempt(
        config,
        issue,
        token,
        worker,
        run_id=run_id,
        attempt=attempt,
        title=title,
    )


def _publish_attempt(
    config: Config,
    issue: Issue,
    token: str,
    worker: WorkerResult,
    *,
    run_id: str,
    attempt: int,
    title: str,
) -> AttemptSuccess:
    _ensure_issue_open(config, issue, token)
    pr_url, head_sha = _publish_success(
        config,
        issue,
        token,
        worker,
        run_id=run_id,
        attempt=attempt,
        title=title,
    )
    return AttemptSuccess(worker=worker, pr_url=pr_url, head_sha=head_sha)


def _attempt_cancelled(
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
    return _cancel_if_needed(
        config,
        issue,
        token,
        feed,
        run_dir=run_dir,
        run_id=run_id,
        branch=branch,
        attempts=attempts,
    )


def _run_worker_attempt(
    config: Config,
    issue: Issue,
    attempt_dir: Path,
    *,
    source: Path,
    workspace: Path,
    attempt: int,
    previous_error: Exception | None,
    timeout_seconds: int,
    feedback: CommentFeed | None,
    initial_context: str,
) -> WorkerResult:
    attempt_started = time.monotonic()
    log.info(
        "Issue #{} attempt {}/{}",
        issue.number,
        attempt,
        config.worker.max_attempts,
    )
    remaining = _remaining_attempt_seconds(attempt_started, timeout_seconds)
    extra: dict[str, object] = {}
    if feedback is not None:
        extra["feedback"] = feedback
    if initial_context:
        extra["initial_context"] = initial_context
    worker = run_worker(
        config,
        issue,
        attempt_dir,
        source_path=source,
        workspace_path=workspace,
        attempt=attempt,
        previous_error=str(previous_error) if previous_error else None,
        timeout_seconds=remaining,
        **extra,
    )
    validate_branch(worker.branch, issue.number)
    expected_branch = branch_name(issue.title, issue.number)
    if worker.branch != expected_branch:
        raise StandardsError(
            f"Worker returned branch {worker.branch!r}, expected {expected_branch!r}"
        )
    return worker


def _new_run_id(issue: int) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ").lower()
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

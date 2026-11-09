"""Daemon and one-shot orchestration entrypoints."""

from __future__ import annotations

import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from orch.config.logger import get_logger, logger
from orch.exceptions.github import GitHubError
from orch.schemas.config.types import Config
from orch.schemas.github.types import Issue
from orch.schemas.pipeline.types import RunOutcome
from orch.services import github, notify, store
from orch.services.docker import cleanup_managed
from orch.services.github.service import ensure_labels, ensure_user
from orch.services.pipeline import run_issue
from orch.services.retention import prune_runs, prune_workspaces

log = get_logger(component="daemon")

_CLAIM_NOTIFY_SECONDS = 1800
_claim_notified_at: dict[int, float] = {}


def setup_logging(config: Config) -> None:
    """Configure console and rotating file logging.

    Args:
        config: Orchestrator configuration.
    """
    config.data_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    )
    logger.add(
        config.data_dir / "orch.log",
        level="DEBUG",
        rotation="10 MB",
        retention=f"{config.retention_days} days",
        encoding="utf-8",
    )


def initialize(config: Config) -> str:
    """Resolve credentials, ensure labels, and initialize the database.

    Args:
        config: Orchestrator configuration.

    Returns:
        GitHub token used by this process.
    """
    token = github.resolve_token(config.repo.owner, config.gh_token)
    ensure_user(token, config.repo.owner)
    ensure_labels(
        config.repo.full_name,
        token,
        [
            (config.labels.queued, "0e8a16", "Queued for orch"),
            (config.labels.running, "fbca04", "orch is working on this issue"),
            (
                config.labels.needs_human,
                "d73a4a",
                "orch exhausted its attempts; a human must decide",
            ),
            (config.labels.pr_open, "1d76db", "orch opened a pull request"),
            (
                config.labels.hold,
                "5319e7",
                "Human hold: orch must not start this issue",
            ),
        ],
    )
    store.init_db(config.data_dir / "orch.db")
    notify.flush_pending(config)
    return token


def run_once(config: Config, token: str, issue_number: int) -> int:
    """Run one issue synchronously for debugging.

    Args:
        config: Orchestrator configuration.
        token: GitHub token.
        issue_number: Issue to process.

    Returns:
        Process exit code.
    """
    try:
        issue = github.get_issue(config.repo.full_name, issue_number, token)
    except GitHubError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).error("Could not fetch issue")
        notify.notify_error(
            config,
            stage="github",
            error=ex,
            issue_number=issue_number,
        )
        return 1

    if config.labels.hold in issue.labels:
        log.bind(issue=issue.number).warning("Issue is on human hold; refusing to run")
        return 1

    if issue.state != "OPEN":
        log.bind(issue=issue.number, state=issue.state).warning(
            "Issue is not open; refusing to run"
        )
        return 1

    notify.notify_accepted(
        config,
        issue.number,
        issue.title,
        queue=0,
        active=1,
    )
    outcome = run_issue(config, issue, token)
    _notify_outcome(
        config,
        issue.number,
        issue.title,
        outcome,
        queue=0,
        active=0,
    )
    if outcome.status in {"pr_open", "merged"}:
        log.info("{}: {}", outcome.status, outcome.pr_url)
        log.info("Run directory: {}", outcome.run_dir)
        return 0

    log.info("Needs human: {}", outcome.error)
    log.info("Run directory: {}", outcome.run_dir)
    return 1


def run_daemon(config: Config, token: str) -> int:
    """Poll for queued issues until interrupted.

    Args:
        config: Orchestrator configuration.
        token: GitHub token.

    Returns:
        Process exit code.
    """
    log.info(
        "Daemon started for {} with {} worker slot(s)",
        config.repo.full_name,
        config.max_workers,
    )
    _reconcile_startup(config, token)
    prune_runs(config.data_dir, config.retention_days)
    store.prune_old_runs(config.data_dir / "orch.db", config.retention_days)
    prune_workspaces(config, token)
    db_path = config.data_dir / "orch.db"
    active: dict[int, Future[RunOutcome]] = {}
    poll_failed_at: float | None = None

    with ThreadPoolExecutor(
        max_workers=config.max_workers,
        thread_name_prefix="orch",
    ) as executor:
        try:
            while True:
                _reap_finished(active, config, token)

                try:
                    issues = github.list_ready_issues(
                        config.repo.full_name,
                        config.labels.queued,
                        token,
                    )
                except GitHubError as ex:
                    log.bind(error=ex, error_type=type(ex).__name__).error("Poll failed; retrying")
                    now = time.monotonic()
                    if poll_failed_at is None or now - poll_failed_at >= 1800:
                        notify.notify_error(
                            config,
                            stage="daemon-poll",
                            error=ex,
                        )
                        poll_failed_at = now
                    time.sleep(config.poll_interval_seconds)
                    continue

                if poll_failed_at is not None:
                    notify.notify_recovered(
                        config,
                        component="daemon-poll",
                        detail="GitHub polling works again.",
                    )
                    poll_failed_at = None

                _dispatch_issues(
                    issues,
                    active,
                    config,
                    token,
                    db_path,
                    executor,
                )
                time.sleep(config.poll_interval_seconds)
        except KeyboardInterrupt:
            log.info("Daemon stopping; waiting for active workers")
            return 0


def _dispatch_issues(
    issues: list[Issue],
    active: dict[int, Future[RunOutcome]],
    config: Config,
    token: str,
    db_path: Path,
    executor: ThreadPoolExecutor,
) -> None:
    for index, issue in enumerate(issues):
        if len(active) >= config.max_workers:
            break
        if issue.number in active:
            continue
        if config.labels.hold in issue.labels:
            log.bind(issue=issue.number).info("Issue is on human hold; skipping")
            continue
        if store.has_active_run(db_path, config.repo.full_name, issue.number):
            continue
        if not _claim_issue(config, issue, token):
            continue
        log.info("Claiming issue #{}: {}", issue.number, issue.title)
        notify.notify_accepted(
            config,
            issue.number,
            issue.title,
            queue=max(0, len(issues) - index - 1),
            active=len(active) + 1,
        )
        active[issue.number] = executor.submit(
            run_issue,
            config,
            issue,
            token,
            claim=False,
        )


def _reap_finished(
    active: dict[int, Future[RunOutcome]],
    config: Config,
    token: str | None = None,
) -> None:
    finished = False
    for issue_number, future in list(active.items()):
        if not future.done():
            continue
        outcome: RunOutcome | None = None
        try:
            outcome = future.result()
        except Exception as ex:
            log.bind(
                issue=issue_number,
                error=ex,
                error_type=type(ex).__name__,
            ).error("Unhandled issue worker failure")
            notify.notify_error(
                config,
                stage="worker",
                error=ex,
                issue_number=issue_number,
            )
        else:
            log.info(
                "Issue #{} finished with status {}",
                issue_number,
                outcome.status,
            )
        del active[issue_number]
        finished = True
        if outcome is not None:
            _notify_outcome(
                config,
                issue_number,
                outcome.issue_title,
                outcome,
                queue=_queue_size(config, token),
                active=len(active),
            )
    if finished:
        prune_runs(config.data_dir, config.retention_days)
        store.prune_old_runs(config.data_dir / "orch.db", config.retention_days)
        if token:
            prune_workspaces(config, token, active_issues=set(active))


def _notify_outcome(
    config: Config,
    issue_number: int,
    issue_title: str,
    outcome: RunOutcome,
    *,
    queue: int,
    active: int,
) -> None:
    title = issue_title or outcome.issue_title
    if outcome.status in {"pr_open", "merged"} and outcome.pr_url:
        notify.notify_finished(
            config,
            issue_number,
            title,
            pr_url=outcome.pr_url,
            duration_seconds=outcome.duration_seconds,
            queue=queue,
            active=active,
        )
    elif outcome.status == "needs_human":
        notify.notify_needs_human(
            config,
            issue_number,
            title,
            reason=(outcome.error or "unknown failure")[:1000],
            queue=queue,
            active=active,
        )


def _queue_size(config: Config, token: str | None) -> int:
    if token is None:
        return 0
    try:
        return len(
            github.list_ready_issues(
                config.repo.full_name,
                config.labels.queued,
                token,
            )
        )
    except GitHubError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).warning(
            "Could not count queued issues for the notification"
        )
        return 0


def _claim_issue(config: Config, issue: github.Issue, token: str) -> bool:
    try:
        github.edit_labels(
            config.repo.full_name,
            issue.number,
            token,
            add=(config.labels.running,),
            remove=(
                config.labels.queued,
                config.labels.needs_human,
                config.labels.pr_open,
            ),
        )
    except GitHubError as ex:
        log.bind(
            issue=issue.number,
            error=ex,
            error_type=type(ex).__name__,
        ).error("Could not claim issue")
        now = time.monotonic()
        last = _claim_notified_at.get(issue.number)
        if last is None or now - last >= _CLAIM_NOTIFY_SECONDS:
            notify.notify_error(
                config,
                stage="claim",
                error=ex,
                issue_number=issue.number,
                issue_title=issue.title,
            )
            _claim_notified_at[issue.number] = now
        return False
    else:
        _claim_notified_at.pop(issue.number, None)
        return True


def _reconcile_startup(config: Config, token: str) -> None:
    cleanup_managed()
    interrupted = store.interrupt_active_runs(
        config.data_dir / "orch.db",
        config.repo.full_name,
    )
    if interrupted:
        log.warning("Marked {} interrupted run(s) as interrupted", interrupted)

    try:
        issues = github.list_ready_issues(
            config.repo.full_name,
            config.labels.running,
            token,
        )
    except GitHubError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).warning(
            "Could not reconcile running issues"
        )
        notify.notify_error(
            config,
            stage="startup-reconcile",
            error=ex,
        )
        return

    for issue in issues:
        try:
            github.comment_issue(
                config.repo.full_name,
                issue.number,
                token,
                github.mark_bot_body(
                    "orch был перезапущен во время работы. Issue возвращена в очередь."
                ),
            )
            github.edit_labels(
                config.repo.full_name,
                issue.number,
                token,
                add=(config.labels.queued,),
                remove=(config.labels.running, config.labels.needs_human),
            )
        except GitHubError as ex:
            log.bind(
                issue=issue.number,
                error=ex,
                error_type=type(ex).__name__,
            ).error("Could not requeue issue after restart")
            notify.notify_error(
                config,
                stage="startup-requeue",
                error=ex,
                issue_number=issue.number,
                issue_title=issue.title,
            )
        else:
            log.info("Requeued issue #{} after daemon restart", issue.number)

"""Daemon and one-shot orchestration entrypoints."""

from __future__ import annotations

import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor

from orch.config.logger import get_logger, logger
from orch.exceptions.github import GitHubError
from orch.schemas.config.types import Config
from orch.schemas.pipeline.types import RunOutcome
from orch.services import github, store
from orch.services.docker import cleanup_managed
from orch.services.pipeline import run_issue

log = get_logger(component="daemon")


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
        retention=10,
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
    github.ensure_user(token, config.repo.owner)
    github.ensure_labels(
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
        ],
    )
    store.init_db(config.data_dir / "orch.db")
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
        log.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not fetch issue"
        )
        return 1

    outcome = run_issue(config, issue, token)
    if outcome.status == "pr_open":
        log.info("PR opened: {}", outcome.pr_url)
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
    db_path = config.data_dir / "orch.db"
    active: dict[int, Future[RunOutcome]] = {}

    with ThreadPoolExecutor(
        max_workers=config.max_workers,
        thread_name_prefix="orch",
    ) as executor:
        try:
            while True:
                _reap_finished(active)

                try:
                    issues = github.list_ready_issues(
                        config.repo.full_name,
                        config.labels.queued,
                        token,
                    )
                except GitHubError as ex:
                    log.bind(error=ex, error_type=type(ex).__name__).error(
                        "Poll failed; retrying"
                    )
                    time.sleep(config.poll_interval_seconds)
                    continue

                for issue in issues:
                    if len(active) >= config.max_workers:
                        break
                    if issue.number in active:
                        continue
                    if store.has_active_run(
                        db_path,
                        config.repo.full_name,
                        issue.number,
                    ):
                        continue

                    if not _claim_issue(config, issue, token):
                        continue
                    log.info("Claiming issue #{}: {}", issue.number, issue.title)
                    active[issue.number] = executor.submit(
                        run_issue,
                        config,
                        issue,
                        token,
                        claim=False,
                    )

                time.sleep(config.poll_interval_seconds)
        except KeyboardInterrupt:
            log.info("Daemon stopping; waiting for active workers")
            return 0


def _reap_finished(active: dict[int, Future[RunOutcome]]) -> None:
    for issue_number, future in list(active.items()):
        if not future.done():
            continue
        try:
            outcome = future.result()
        except Exception as ex:
            log.bind(
                issue=issue_number,
                error=ex,
                error_type=type(ex).__name__,
            ).error("Unhandled issue worker failure")
        else:
            log.info(
                "Issue #{} finished with status {}",
                issue_number,
                outcome.status,
            )
        del active[issue_number]


def _claim_issue(config: Config, issue: github.Issue, token: str) -> bool:
    try:
        github.edit_labels(
            config.repo.full_name,
            issue.number,
            token,
            add=(config.labels.running,),
            remove=(config.labels.queued, config.labels.needs_human),
        )
    except GitHubError as ex:
        log.bind(
            issue=issue.number,
            error=ex,
            error_type=type(ex).__name__,
        ).error("Could not claim issue")
        return False
    else:
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
        return

    for issue in issues:
        try:
            github.comment_issue(
                config.repo.full_name,
                issue.number,
                token,
                "orch был перезапущен во время работы. Issue возвращена в очередь.",
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
        else:
            log.info("Requeued issue #{} after daemon restart", issue.number)

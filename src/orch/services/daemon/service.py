"""Daemon and one-shot orchestration entrypoints."""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from orch.config.logger import get_logger, logger
from orch.exceptions.forge import ForgeError
from orch.schemas.config.types import Config
from orch.schemas.github.types import Issue
from orch.schemas.pipeline.types import RunOutcome
from orch.services import forge, notify, store
from orch.services.docker import cleanup_managed
from orch.services.pipeline import run_issue
from orch.services.retention import prune_runs, prune_workspaces

log = get_logger(component="daemon")

_CLAIM_NOTIFY_SECONDS = 1800
_ActiveKey = tuple[str, int]
_claim_notified_at: dict[_ActiveKey, float] = {}


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
        Forge token used by this process.
    """
    token = forge.resolve_token(config)
    forge.ensure_user(config, token)
    forge.ensure_labels(
        config,
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
        token: Forge token.
        issue_number: Issue to process.

    Returns:
        Process exit code.
    """
    try:
        issue = forge.get_issue(config, issue_number, token)
    except ForgeError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).error("Could not fetch issue")
        notify.notify_error(
            config,
            stage="forge",
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


def _projects(config: Config | Sequence[Config]) -> tuple[Config, ...]:
    """Normalize one project or a project sequence to a tuple."""
    if isinstance(config, Config):
        return (config,)
    projects = tuple(config)
    if not projects:
        raise ValueError("At least one project is required")
    return projects


def _tokens(
    projects: Sequence[Config],
    token: str | Mapping[str, str] | None,
) -> dict[str, str]:
    """Normalize a single token or a per-repository token mapping."""
    if token is None:
        return {}
    if isinstance(token, str):
        return {project.repo.full_name: token for project in projects}
    missing = [
        project.repo.full_name for project in projects if project.repo.full_name not in token
    ]
    if missing:
        raise ValueError(f"Missing forge token for: {', '.join(missing)}")
    return {project.repo.full_name: token[project.repo.full_name] for project in projects}


def _key_parts(key: int | _ActiveKey, default: Config) -> _ActiveKey:
    """Return the repository and issue number for an active-run key."""
    if isinstance(key, tuple):
        return key
    return default.repo.full_name, key


def _active_owner(
    key: int | _ActiveKey,
    projects: Sequence[Config],
) -> tuple[Config, int]:
    """Resolve the project and issue number that own an active run."""
    repo, issue_number = _key_parts(key, projects[0])
    for project in projects:
        if project.repo.full_name == repo:
            return project, issue_number
    return projects[0], issue_number


def run_daemon(
    config: Config | Sequence[Config],
    token: str | Mapping[str, str],
) -> int:
    """Poll every configured project for queued issues until interrupted.

    Args:
        config: One project configuration or a sequence of them.
        token: Forge token for a single project, or a mapping keyed by
            ``owner/name`` when several projects are configured.

    Returns:
        Process exit code.
    """
    projects = _projects(config)
    tokens = _tokens(projects, token)
    primary = projects[0]
    log.info(
        "Daemon started for {} project(s) with {} worker slot(s): {}",
        len(projects),
        primary.max_workers,
        ", ".join(project.repo.full_name for project in projects),
    )
    _start_projects(projects, tokens)
    db_path = primary.data_dir / "orch.db"
    active: dict[int | _ActiveKey, Future[RunOutcome]] = {}
    poll_failed_at: dict[str, float] = {}

    with ThreadPoolExecutor(
        max_workers=primary.max_workers,
        thread_name_prefix="orch",
    ) as executor:
        try:
            while True:
                _reap_finished(active, projects, tokens)
                for project in projects:
                    _poll_project(
                        project,
                        active,
                        tokens,
                        db_path,
                        executor,
                        poll_failed_at,
                    )
                time.sleep(primary.poll_interval_seconds)
        except KeyboardInterrupt:
            log.info("Daemon stopping; waiting for active workers")
            return 0


def _start_projects(projects: Sequence[Config], tokens: Mapping[str, str]) -> None:
    """Reconcile interrupted runs and prune expired state per project."""
    for project in projects:
        _reconcile_startup(project, tokens[project.repo.full_name])
    primary = projects[0]
    prune_runs(primary.data_dir, primary.retention_days)
    store.prune_old_runs(primary.data_dir / "orch.db", primary.retention_days)
    for project in projects:
        prune_workspaces(project, tokens[project.repo.full_name])


def _poll_project(
    config: Config,
    active: dict[int | _ActiveKey, Future[RunOutcome]],
    tokens: Mapping[str, str],
    db_path: Path,
    executor: ThreadPoolExecutor,
    poll_failed_at: dict[str, float],
) -> None:
    """Poll one project and dispatch its queued issues."""
    repo = config.repo.full_name
    token = tokens[repo]
    try:
        issues = forge.list_ready_issues(config, config.labels.queued, token)
    except ForgeError as ex:
        log.bind(repo=repo, error=ex, error_type=type(ex).__name__).error("Poll failed; retrying")
        now = time.monotonic()
        last = poll_failed_at.get(repo)
        if last is None or now - last >= _CLAIM_NOTIFY_SECONDS:
            notify.notify_error(config, stage="daemon-poll", error=ex)
            poll_failed_at[repo] = now
        return

    if repo in poll_failed_at:
        notify.notify_recovered(
            config,
            component="daemon-poll",
            detail="Forge polling works again.",
        )
        del poll_failed_at[repo]

    _dispatch_issues(issues, active, config, token, db_path, executor)


def _dispatch_issues(
    issues: list[Issue],
    active: dict[int | _ActiveKey, Future[RunOutcome]],
    config: Config,
    token: str,
    db_path: Path,
    executor: ThreadPoolExecutor,
) -> None:
    repo = config.repo.full_name
    for index, issue in enumerate(issues):
        if len(active) >= config.max_workers:
            break
        key = (repo, issue.number)
        if key in active:
            continue
        if config.labels.hold in issue.labels:
            log.bind(repo=repo, issue=issue.number).info("Issue is on human hold; skipping")
            continue
        if store.has_active_run(db_path, repo, issue.number):
            continue
        if not _claim_issue(config, issue, token):
            continue
        log.info("Claiming {}#{}: {}", repo, issue.number, issue.title)
        notify.notify_accepted(
            config,
            issue.number,
            issue.title,
            queue=max(0, len(issues) - index - 1),
            active=len(active) + 1,
        )
        active[key] = executor.submit(
            run_issue,
            config,
            issue,
            token,
            claim=False,
        )


def _reap_finished(
    active: dict[int | _ActiveKey, Future[RunOutcome]],
    config: Config | Sequence[Config],
    token: str | Mapping[str, str] | None = None,
) -> None:
    projects = _projects(config)
    tokens = _tokens(projects, token)
    finished = False
    for key, future in list(active.items()):
        if not future.done():
            continue
        project, issue_number = _active_owner(key, projects)
        outcome = _reap_future(project, issue_number, future)
        del active[key]
        finished = True
        if outcome is not None:
            _notify_outcome(
                project,
                issue_number,
                outcome.issue_title,
                outcome,
                queue=_queue_size(project, tokens.get(project.repo.full_name)),
                active=len(active),
            )
    if finished:
        _prune_after_finish(active, projects, tokens)


def _reap_future(
    project: Config,
    issue_number: int,
    future: Future[RunOutcome],
) -> RunOutcome | None:
    """Read one finished future, reporting unhandled failures."""
    try:
        outcome = future.result()
    except Exception as ex:
        log.bind(
            repo=project.repo.full_name,
            issue=issue_number,
            error=ex,
            error_type=type(ex).__name__,
        ).error("Unhandled issue worker failure")
        notify.notify_error(
            project,
            stage="worker",
            error=ex,
            issue_number=issue_number,
        )
        return None
    log.info(
        "{}#{} finished with status {}",
        project.repo.full_name,
        issue_number,
        outcome.status,
    )
    return outcome


def _prune_after_finish(
    active: dict[int | _ActiveKey, Future[RunOutcome]],
    projects: Sequence[Config],
    tokens: Mapping[str, str],
) -> None:
    """Prune expired artifacts after at least one run finished."""
    primary = projects[0]
    prune_runs(primary.data_dir, primary.retention_days)
    store.prune_old_runs(primary.data_dir / "orch.db", primary.retention_days)
    for project in projects:
        project_token = tokens.get(project.repo.full_name)
        if project_token is None:
            continue
        prune_workspaces(
            project,
            project_token,
            active_issues=_active_issues(active, project, primary),
        )


def _active_issues(
    active: dict[int | _ActiveKey, Future[RunOutcome]],
    project: Config,
    default: Config,
) -> set[int]:
    """Return the project's issue numbers that are still running."""
    issues: set[int] = set()
    for key in active:
        repo, issue_number = _key_parts(key, default)
        if repo == project.repo.full_name:
            issues.add(issue_number)
    return issues


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
            forge.list_ready_issues(
                config,
                config.labels.queued,
                token,
            )
        )
    except ForgeError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).warning(
            "Could not count queued issues for the notification"
        )
        return 0


def _claim_issue(config: Config, issue: Issue, token: str) -> bool:
    try:
        forge.edit_labels(
            config,
            issue.number,
            token,
            add=(config.labels.running,),
            remove=(
                config.labels.queued,
                config.labels.needs_human,
                config.labels.pr_open,
            ),
        )
    except ForgeError as ex:
        log.bind(
            issue=issue.number,
            error=ex,
            error_type=type(ex).__name__,
        ).error("Could not claim issue")
        key = (config.repo.full_name, issue.number)
        now = time.monotonic()
        last = _claim_notified_at.get(key)
        if last is None or now - last >= _CLAIM_NOTIFY_SECONDS:
            notify.notify_error(
                config,
                stage="claim",
                error=ex,
                issue_number=issue.number,
                issue_title=issue.title,
            )
            _claim_notified_at[key] = now
        return False
    else:
        _claim_notified_at.pop((config.repo.full_name, issue.number), None)
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
        issues = forge.list_ready_issues(
            config,
            config.labels.running,
            token,
        )
    except ForgeError as ex:
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
            forge.comment_issue(
                config,
                issue.number,
                token,
                forge.mark_bot_body(
                    "orch был перезапущен во время работы. Issue возвращена в очередь."
                ),
            )
            forge.edit_labels(
                config,
                issue.number,
                token,
                add=(config.labels.queued,),
                remove=(config.labels.running, config.labels.needs_human),
            )
        except ForgeError as ex:
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

"""Attempt failure reporting and terminal retry outcomes."""

from __future__ import annotations

from pathlib import Path

from orch.config.logger import get_logger
from orch.exceptions.worker import RunCancelled, WatchdogStop
from orch.schemas.config.types import Config
from orch.schemas.github.types import Issue
from orch.schemas.pipeline.types import RunOutcome
from orch.services import forge, notify
from orch.services.pipeline.lifecycle import (
    _cancel_run,
    _park_after_retries,
)
from orch.services.pipeline.publish import _delete_stale_branch

log = get_logger(component="pipeline")


def _comment_attempt_failure(
    config: Config,
    issue: Issue,
    token: str,
    attempt: int,
    error: Exception,
) -> None:
    message = (
        f"Попытка {attempt}/{config.worker.max_attempts} не удалась.\n\n"
        f"`{type(error).__name__}: {str(error)[:1000]}`\n\n"
        "Запускаю следующую попытку в том же workspace с новым контекстом."
    )
    try:
        forge.comment_issue(
            config,
            issue.number,
            token,
            forge.mark_bot_body(message),
        )
    except Exception as notify_error:
        log.bind(
            issue=issue.number,
            error=notify_error,
            error_type=type(notify_error).__name__,
        ).warning("Could not post attempt failure comment")


def _handle_attempt_failure(
    config: Config,
    issue: Issue,
    token: str,
    *,
    run_id: str,
    attempt: int,
    branch: str,
    error: Exception,
    preserve_pr: bool = False,
) -> None:
    log.bind(
        issue=issue.number,
        run_id=run_id,
        attempt=attempt,
        error=error,
        error_type=type(error).__name__,
    ).error("Issue attempt failed")
    _comment_attempt_failure(config, issue, token, attempt, error)
    notify.notify_attempt_failed(
        config,
        issue.number,
        issue.title,
        attempt=attempt,
        max_attempts=config.worker.max_attempts,
        error=error,
        run_id=run_id,
    )
    if preserve_pr:
        log.bind(branch=branch).info(
            "Keeping the pull-request branch because this run revises an open PR"
        )
        return
    _delete_stale_branch(config, branch, token)


def _retry_outcome(
    config: Config,
    issue: Issue,
    token: str,
    error: Exception,
    *,
    run_id: str,
    run_dir: Path,
    branch: str,
    attempt: int,
    preserve_pr: bool = False,
) -> RunOutcome | None:
    if isinstance(error, RunCancelled):
        return _cancel_run(
            config,
            issue,
            run_dir,
            run_id,
            branch,
            str(error) or "issue closed",
            attempts=attempt,
        )
    if isinstance(error, WatchdogStop):
        if error.retryable and attempt < config.worker.max_attempts:
            log.bind(
                issue=issue.number,
                attempt=attempt,
                state=error.state,
            ).warning("Watchdog stopped a stalled run; restarting in the same workspace")
            _handle_attempt_failure(
                config,
                issue,
                token,
                run_id=run_id,
                attempt=attempt,
                branch=branch,
                error=error,
                preserve_pr=preserve_pr,
            )
            return None
        return _park_after_retries(
            config,
            issue,
            token,
            run_dir=run_dir,
            run_id=run_id,
            branch=branch,
            error=error,
            attempts=attempt,
        )
    _handle_attempt_failure(
        config,
        issue,
        token,
        run_id=run_id,
        attempt=attempt,
        branch=branch,
        error=error,
        preserve_pr=preserve_pr,
    )
    return None

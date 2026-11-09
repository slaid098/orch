"""Terminal pipeline outcomes: cancellation, parking, and result files."""

from __future__ import annotations

import json
from pathlib import Path

from orch.config.logger import get_logger
from orch.schemas.config.types import Config
from orch.schemas.github.types import Issue
from orch.schemas.pipeline.types import RunOutcome
from orch.services import forge, store

log = get_logger(component="pipeline")

_STATUS_PR_OPEN = "pr_open"
_STATUS_NEEDS_HUMAN = "needs_human"
_STATUS_CANCELLED = "cancelled"
_STATUS_MERGED = "merged"


def _workspace_path(config: Config, issue: Issue) -> Path:
    """Return the persistent host workspace for one issue."""
    repo = f"{config.repo.owner}__{config.repo.name}"
    return config.data_dir / "workspaces" / repo / f"issue-{issue.number}"


def _write_result(run_dir: Path, payload: dict[str, object]) -> None:
    (run_dir / "result.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _park_invalid_title(
    config: Config,
    issue: Issue,
    token: str,
    run_dir: Path,
    run_id: str,
    error: Exception,
) -> None:
    message = (
        "orch не запустил worker: заголовок issue не соответствует стандарту.\n\n"
        f"Текущий заголовок: `{issue.title}`\n\n"
        f"Ошибка: `{type(error).__name__}: {error}`\n\n"
        "Ожидаемый формат: `type(scope): description`. "
        "Тип, scope и английское описание обязательны, длина — до 72 символов.\n"
        f"Run: `{run_id}`\n"
    )
    try:
        forge.comment_issue(
            config,
            issue.number,
            token,
            forge.mark_bot_body(message),
        )
        forge.edit_labels(
            config,
            issue.number,
            token,
            add=(config.labels.needs_human,),
            remove=(config.labels.running, config.labels.queued),
        )
    except Exception as notify_error:
        log.bind(
            issue=issue.number,
            error=notify_error,
            error_type=type(notify_error).__name__,
        ).error("Could not mark invalid issue title as needs-human")

    _write_result(
        run_dir,
        {
            "run_id": run_id,
            "status": _STATUS_NEEDS_HUMAN,
            "branch": "",
            "error": f"{type(error).__name__}: {error}",
            "attempts": 0,
        },
    )
    store.finish_run(
        config.data_dir / "orch.db",
        run_id=run_id,
        status=_STATUS_NEEDS_HUMAN,
        error=f"{type(error).__name__}: {error}",
    )


def _best_effort_comment(
    config: Config,
    issue_number: int,
    token: str,
    body: str,
) -> None:
    """Post a bot comment and log instead of raising on failure."""
    try:
        forge.comment_issue(
            config,
            issue_number,
            token,
            forge.mark_bot_body(body),
        )
    except Exception as ex:
        log.bind(
            issue=issue_number,
            error=ex,
            error_type=type(ex).__name__,
        ).warning("Could not post issue comment")


def _best_effort_labels(
    config: Config,
    issue_number: int,
    token: str,
    *,
    add: tuple[str, ...],
    remove: tuple[str, ...],
) -> None:
    """Update issue labels and log instead of raising on failure."""
    try:
        forge.edit_labels(
            config,
            issue_number,
            token,
            add=add,
            remove=remove,
        )
    except Exception as ex:
        log.bind(
            issue=issue_number,
            error=ex,
            error_type=type(ex).__name__,
        ).warning("Could not update issue labels")


def _park(
    config: Config,
    issue: Issue,
    token: str,
    run_dir: Path,
    run_id: str,
    branch: str,
    error: Exception,
    *,
    attempts: int,
    pr_url: str | None = None,
    review_rounds: int = 0,
) -> None:
    target = "PR до merge" if pr_url else "issue до PR"
    message = (
        f"orch не смог довести {target} после всех попыток.\n\n"
        f"Ошибка: `{type(error).__name__}: {error}`\n\n"
        f"Попыток: `{attempts}` из `{config.worker.max_attempts}`\n"
        f"Раундов ревью: `{review_rounds}` из `{config.reviewer.max_rounds}`\n"
        f"Run: `{run_id}`\n"
        f"Branch: `{branch}`\n"
    )
    if pr_url:
        message += f"PR: {pr_url}\n"
    _notify_needs_human(config, issue, token, message)
    _write_result(
        run_dir,
        {
            "run_id": run_id,
            "status": _STATUS_NEEDS_HUMAN,
            "branch": branch,
            "error": f"{type(error).__name__}: {error}",
            "attempts": attempts,
            "review_rounds": review_rounds,
            "pr_url": pr_url,
        },
    )
    store.finish_run(
        config.data_dir / "orch.db",
        run_id=run_id,
        status=_STATUS_NEEDS_HUMAN,
        error=f"{type(error).__name__}: {error}",
    )


def _notify_needs_human(
    config: Config,
    issue: Issue,
    token: str,
    message: str,
) -> None:
    """Best-effort comment and label update for a parked issue."""
    try:
        forge.comment_issue(
            config,
            issue.number,
            token,
            forge.mark_bot_body(message),
        )
        forge.edit_labels(
            config,
            issue.number,
            token,
            add=(config.labels.needs_human,),
            remove=(config.labels.running, config.labels.queued),
        )
    except Exception as notify_error:
        log.bind(
            issue=issue.number,
            error=notify_error,
            error_type=type(notify_error).__name__,
        ).error("Could not mark issue as needs-human on the forge")


def _cancel_run(
    config: Config,
    issue: Issue,
    run_dir: Path,
    run_id: str,
    branch: str,
    reason: str,
    *,
    attempts: int,
) -> RunOutcome:
    log.bind(issue=issue.number, run_id=run_id, reason=reason).info(
        "Issue run cancelled; leaving the workspace in place"
    )
    _write_result(
        run_dir,
        {
            "run_id": run_id,
            "status": _STATUS_CANCELLED,
            "branch": branch,
            "error": reason,
            "attempts": attempts,
        },
    )
    store.finish_run(
        config.data_dir / "orch.db",
        run_id=run_id,
        status=_STATUS_CANCELLED,
        error=reason,
    )
    return RunOutcome(
        run_id=run_id,
        status=_STATUS_CANCELLED,
        run_dir=run_dir,
        branch=branch,
        error=reason,
    )


def _park_after_retries(
    config: Config,
    issue: Issue,
    token: str,
    *,
    run_dir: Path,
    run_id: str,
    branch: str,
    error: Exception,
    attempts: int | None = None,
    pr_url: str | None = None,
    review_rounds: int = 0,
) -> RunOutcome:
    _park(
        config,
        issue,
        token,
        run_dir,
        run_id,
        branch,
        error,
        attempts=config.worker.max_attempts if attempts is None else attempts,
        pr_url=pr_url,
        review_rounds=review_rounds,
    )
    return RunOutcome(
        run_id=run_id,
        status=_STATUS_NEEDS_HUMAN,
        run_dir=run_dir,
        branch=branch,
        pr_url=pr_url,
        error=str(error),
        review_rounds=review_rounds,
    )

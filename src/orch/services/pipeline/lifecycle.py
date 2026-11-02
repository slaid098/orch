"""Terminal pipeline outcomes: cancellation, parking, and result files."""

from __future__ import annotations

import json
from pathlib import Path

from orch.config.logger import get_logger
from orch.schemas.config.types import Config
from orch.schemas.github.types import Issue
from orch.schemas.pipeline.types import RunOutcome
from orch.services import github, store

log = get_logger(component="pipeline")

_STATUS_PR_OPEN = "pr_open"
_STATUS_NEEDS_HUMAN = "needs_human"
_STATUS_CANCELLED = "cancelled"


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
        github.comment_issue(
            config.repo.full_name,
            issue.number,
            token,
            github.mark_bot_body(message),
        )
        github.edit_labels(
            config.repo.full_name,
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
) -> None:
    message = (
        "orch не смог довести issue до PR после всех попыток.\n\n"
        f"Ошибка: `{type(error).__name__}: {error}`\n\n"
        f"Попыток: `{attempts}` из `{config.worker.max_attempts}`\n"
        f"Run: `{run_id}`\n"
        f"Branch: `{branch}`\n"
    )
    try:
        github.comment_issue(
            config.repo.full_name,
            issue.number,
            token,
            github.mark_bot_body(message),
        )
        github.edit_labels(
            config.repo.full_name,
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
        ).error("Could not mark issue as needs-human on GitHub")

    _write_result(
        run_dir,
        {
            "run_id": run_id,
            "status": _STATUS_NEEDS_HUMAN,
            "branch": branch,
            "error": f"{type(error).__name__}: {error}",
            "attempts": attempts,
        },
    )
    store.finish_run(
        config.data_dir / "orch.db",
        run_id=run_id,
        status=_STATUS_NEEDS_HUMAN,
        error=f"{type(error).__name__}: {error}",
    )


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
    )
    return RunOutcome(
        run_id=run_id,
        status=_STATUS_NEEDS_HUMAN,
        run_dir=run_dir,
        branch=branch,
        error=str(error),
    )

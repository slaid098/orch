"""The issue to pull request pipeline."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from orch import github
from orch.agent import WorkerResult, run_worker
from orch.config import Config
from orch.github import Issue
from orch.gitops import (
    authenticated_remote,
    cleanup_source,
    cleanup_workspace,
    clone_source,
    import_bundle,
    push_branch,
    refresh_mirror,
)
from orch.services.standards import (
    StandardsError,
    branch_name,
    format_pr_body,
    validate_title,
)
from orch.store import create_run, finish_run

_STATUS_PR_OPEN = "pr_open"
_STATUS_NEEDS_HUMAN = "needs_human"


@dataclass(frozen=True)
class RunOutcome:
    """Result of one issue run."""

    run_id: str
    status: str
    run_dir: Path
    branch: str
    pr_url: str | None = None
    error: str | None = None


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
    run_id = _new_run_id(issue.number)
    run_dir = _create_run_dir(config, issue, run_id)
    repo = config.repo.full_name
    db_path = config.data_dir / "orch.db"
    timeout_seconds = attempt_timeout_seconds or config.worker.timeout_minutes * 60

    try:
        title = validate_title(issue.title)
        branch = branch_name(title, issue.number)
    except StandardsError as ex:
        create_run(
            db_path,
            run_id=run_id,
            repo=repo,
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

    create_run(
        db_path,
        run_id=run_id,
        repo=repo,
        issue=issue.number,
        branch=branch,
    )

    try:
        existing_pr = _find_open_pr(repo, branch, token)
        if existing_pr:
            return _finish_existing_pr(
                config, issue, token, run_dir, run_id, branch, existing_pr
            )

        if claim:
            github.edit_labels(
                repo,
                issue.number,
                token,
                add=(config.labels.running,),
                remove=(config.labels.queued, config.labels.needs_human),
            )

        workspace = _workspace_path(config, issue)
        source: Path | None = None
        last_error: Exception | None = None
        try:
            mirror = refresh_mirror(
                config.data_dir,
                config.repo.owner,
                config.repo.name,
                config.repo.base_branch,
                token,
            )
            source = clone_source(
                mirror,
                config.repo.base_branch,
                run_dir / "source",
            )

            for attempt in range(1, config.worker.max_attempts + 1):
                attempt_dir = run_dir / f"attempt-{attempt}"
                attempt_started = time.monotonic()
                try:
                    logger.info(
                        "Issue #{} attempt {}/{}",
                        issue.number,
                        attempt,
                        config.worker.max_attempts,
                    )
                    remaining = _remaining_attempt_seconds(
                        attempt_started,
                        timeout_seconds,
                    )
                    worker = run_worker(
                        config,
                        issue,
                        attempt_dir,
                        source_path=source,
                        workspace_path=workspace,
                        attempt=attempt,
                        previous_error=str(last_error) if last_error else None,
                        timeout_seconds=remaining,
                    )
                    _publish_branch(config, worker, token)
                    pr_url = github.create_pr(
                        repo,
                        token,
                        head=worker.branch,
                        base=config.repo.base_branch,
                        title=title,
                        body=format_pr_body(worker.handoff, issue.number),
                    )
                    _best_effort_comment(
                        repo,
                        issue.number,
                        token,
                        _success_comment(worker, pr_url, run_id, attempt),
                    )
                    _best_effort_labels(
                        repo,
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
                            "branch": worker.branch,
                            "pr_url": pr_url,
                            "tests_passed": True,
                            "attempts": attempt,
                        },
                    )
                    finish_run(
                        db_path,
                        run_id=run_id,
                        status=_STATUS_PR_OPEN,
                        pr_url=pr_url,
                    )
                    cleanup_workspace(workspace)
                    logger.info(
                        "Issue #{} is ready for review: {}",
                        issue.number,
                        pr_url,
                    )
                    return RunOutcome(
                        run_id=run_id,
                        status=_STATUS_PR_OPEN,
                        run_dir=run_dir,
                        branch=worker.branch,
                        pr_url=pr_url,
                    )
                except Exception as ex:
                    last_error = ex
                    logger.bind(
                        issue=issue.number,
                        run_id=run_id,
                        attempt=attempt,
                        error=ex,
                        error_type=type(ex).__name__,
                    ).error("Issue attempt failed")
                    _comment_attempt_failure(config, issue, token, attempt, ex)
                    _delete_stale_branch(config, branch, token)

            _park(
                config,
                issue,
                token,
                run_dir,
                run_id,
                branch,
                last_error or RuntimeError("No attempt produced a pull request"),
                attempts=config.worker.max_attempts,
            )
            return RunOutcome(
                run_id=run_id,
                status=_STATUS_NEEDS_HUMAN,
                run_dir=run_dir,
                branch=branch,
                error=str(last_error or "No attempt produced a pull request"),
            )
        finally:
            if source is not None:
                cleanup_source(source)
    except Exception as ex:
        logger.bind(
            issue=issue.number,
            run_id=run_id,
            error=ex,
            error_type=type(ex).__name__,
        ).error("Issue run failed before attempts; parking")
        _park(
            config,
            issue,
            token,
            run_dir,
            run_id,
            branch,
            ex,
            attempts=0,
        )
        return RunOutcome(
            run_id=run_id,
            status=_STATUS_NEEDS_HUMAN,
            run_dir=run_dir,
            branch=branch,
            error=str(ex),
        )


def _remaining_attempt_seconds(started: float, budget_seconds: int) -> int:
    remaining = budget_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutError("Attempt timed out before the worker started")
    return max(1, int(remaining))


def _workspace_path(config: Config, issue: Issue) -> Path:
    repo = f"{config.repo.owner}__{config.repo.name}"
    return config.data_dir / "workspaces" / repo / f"issue-{issue.number}"


def _find_open_pr(repo: str, branch: str, token: str) -> str | None:
    try:
        return github.find_open_pr(repo, branch, token)
    except github.GitHubError as ex:
        logger.bind(branch=branch, error=str(ex)).warning(
            "Could not check for an existing pull request"
        )
        return None


def _finish_existing_pr(
    config: Config,
    issue: Issue,
    token: str,
    run_dir: Path,
    run_id: str,
    branch: str,
    pr_url: str,
) -> RunOutcome:
    logger.info("Issue #{} already has an open PR: {}", issue.number, pr_url)
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
    finish_run(
        config.data_dir / "orch.db",
        run_id=run_id,
        status=_STATUS_PR_OPEN,
        pr_url=pr_url,
    )
    cleanup_workspace(_workspace_path(config, issue))
    return RunOutcome(
        run_id=run_id,
        status=_STATUS_PR_OPEN,
        run_dir=run_dir,
        branch=branch,
        pr_url=pr_url,
    )


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
        github.comment_issue(config.repo.full_name, issue.number, token, message)
    except Exception as notify_error:
        logger.bind(
            issue=issue.number,
            error=notify_error,
            error_type=type(notify_error).__name__,
        ).warning("Could not post attempt failure comment")


def _best_effort_comment(
    repo: str,
    issue_number: int,
    token: str,
    body: str,
) -> None:
    try:
        github.comment_issue(repo, issue_number, token, body)
    except Exception as ex:
        logger.bind(
            issue=issue_number,
            error=ex,
            error_type=type(ex).__name__,
        ).warning("Could not post issue comment")


def _best_effort_labels(
    repo: str,
    issue_number: int,
    token: str,
    *,
    add: tuple[str, ...],
    remove: tuple[str, ...],
) -> None:
    try:
        github.edit_labels(
            repo,
            issue_number,
            token,
            add=add,
            remove=remove,
        )
    except Exception as ex:
        logger.bind(
            issue=issue_number,
            error=ex,
            error_type=type(ex).__name__,
        ).warning("Could not update issue labels")


def _delete_stale_branch(config: Config, branch: str, token: str) -> None:
    existing = _find_open_pr(config.repo.full_name, branch, token)
    if existing:
        return
    github.delete_branch(config.repo.full_name, branch, token)


def _publish_branch(config: Config, worker: WorkerResult, token: str) -> None:
    bare = import_bundle(worker.bundle_path, worker.branch, worker.run_dir)
    remote = authenticated_remote(config.repo.owner, config.repo.name, token)
    push_branch(bare, worker.branch, remote, token)


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
        github.comment_issue(config.repo.full_name, issue.number, token, message)
        github.edit_labels(
            config.repo.full_name,
            issue.number,
            token,
            add=(config.labels.needs_human,),
            remove=(config.labels.running, config.labels.queued),
        )
    except Exception as notify_error:
        logger.bind(
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
    finish_run(
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
        github.comment_issue(config.repo.full_name, issue.number, token, message)
        github.edit_labels(
            config.repo.full_name,
            issue.number,
            token,
            add=(config.labels.needs_human,),
            remove=(config.labels.running, config.labels.queued),
        )
    except Exception as notify_error:
        logger.bind(
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
    finish_run(
        config.data_dir / "orch.db",
        run_id=run_id,
        status=_STATUS_NEEDS_HUMAN,
        error=f"{type(error).__name__}: {error}",
    )


def _success_comment(
    worker: WorkerResult,
    pr_url: str,
    run_id: str,
    attempt: int,
) -> str:
    return (
        f"orch открыл PR: {pr_url}\n\n"
        f"Попытка: `{attempt}`. Тесты прошли. Run: `{run_id}`\n\n"
        f"{worker.handoff.what[:3000]}\n"
    )


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


def _write_result(run_dir: Path, payload: dict[str, object]) -> None:
    (run_dir / "result.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

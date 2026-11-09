"""Branch publication and pull-request lookup helpers."""

from __future__ import annotations

from orch.config.logger import get_logger
from orch.exceptions.github import GitHubError
from orch.exceptions.pipeline import PublicationError
from orch.schemas.config.types import Config
from orch.schemas.github.types import Issue, PullRequestRef
from orch.schemas.worker.types import WorkerResult
from orch.services import git as gitops
from orch.services import github
from orch.services.pipeline.lifecycle import (
    _best_effort_comment,
    _best_effort_labels,
)
from orch.services.standards import format_pr_body

log = get_logger(component="pipeline")


def _find_open_pr(
    repo: str,
    branch: str,
    token: str,
    *,
    strict: bool = False,
) -> str | None:
    try:
        return github.find_open_pr(repo, branch, token)
    except GitHubError as ex:
        log.bind(branch=branch, error=str(ex)).warning(
            "Could not check for an existing pull request"
        )
        if strict:
            raise
        return None


def _find_open_pr_for_issue(
    repo: str,
    issue_number: int,
    token: str,
    *,
    preferred_head: str | None = None,
    strict: bool = False,
) -> PullRequestRef | None:
    try:
        return github.find_open_pr_for_issue(
            repo,
            issue_number,
            token,
            preferred_head=preferred_head,
        )
    except GitHubError as ex:
        log.bind(issue=issue_number, error=str(ex)).warning(
            "Could not check for an existing pull request for the issue"
        )
        if strict:
            raise
        return None


def _delete_stale_branch(config: Config, branch: str, token: str) -> None:
    try:
        existing = _find_open_pr(config.repo.full_name, branch, token, strict=True)
    except GitHubError:
        log.bind(branch=branch).warning(
            "Skipping stale branch deletion because the open-PR check failed"
        )
        return
    if existing:
        return
    github.delete_branch(config.repo.full_name, branch, token)


def _revision_problem(
    repo: str,
    pr_url: str,
    token: str,
    expected_head: str,
) -> str | None:
    """Return why a revision PR cannot be safely updated, if anything."""
    try:
        status = github.get_pr_status(repo, pr_url, token)
    except GitHubError as ex:
        return f"Could not verify pull request {pr_url}: {ex}"
    if status.state != "OPEN":
        return f"Pull request {pr_url} is no longer open; refusing to publish"
    if status.head_sha != expected_head:
        return (
            f"Pull request {pr_url} head moved from {expected_head[:12]} to "
            f"{status.head_sha[:12]}; refusing to force-push"
        )
    return None


def _publish_branch(
    config: Config,
    worker: WorkerResult,
    token: str,
    *,
    expected: str | None = None,
) -> str:
    bare = gitops.import_bundle(worker.bundle_path, worker.branch, worker.run_dir)
    remote = gitops.authenticated_remote(config.repo.owner, config.repo.name, token)
    if expected is None:
        gitops.push_branch(bare, worker.branch, remote, token)
    else:
        gitops.push_branch(bare, worker.branch, remote, token, expected=expected)
    return gitops.branch_sha(bare, worker.branch)


def _success_comment(
    worker: WorkerResult,
    pr_url: str,
    run_id: str,
    attempt: int,
    *,
    updated: bool = False,
) -> str:
    action = "обновил" if updated else "открыл"
    return (
        f"orch {action} PR: {pr_url}\n\n"
        f"Попытка: `{attempt}`. Локальные тесты прошли. "
        f"Проверяю GitHub checks и ревьюера. Run: `{run_id}`\n\n"
        f"{worker.handoff.what[:3000]}\n"
    )


def _publish_success(
    config: Config,
    issue: Issue,
    token: str,
    worker: WorkerResult,
    *,
    run_id: str,
    attempt: int,
    title: str,
    revision_pr: str | None = None,
    revision_head: str | None = None,
) -> tuple[str, str]:
    repo = config.repo.full_name
    if revision_pr is not None:
        problem = _revision_problem(
            repo,
            revision_pr,
            token,
            revision_head or "",
        )
        if problem is not None:
            raise GitHubError(problem)
    if revision_head is None:
        head_sha = _publish_branch(config, worker, token) or ""
    else:
        head_sha = _publish_branch(config, worker, token, expected=revision_head) or ""
    try:
        pr_url = _find_open_pr(repo, worker.branch, token, strict=True)
    except GitHubError as ex:
        if revision_pr is None:
            raise
        raise PublicationError(str(ex), head_sha) from ex
    if revision_pr is not None:
        if pr_url is None:
            raise PublicationError(
                f"Revision pull request {revision_pr} is no longer open; "
                "refusing to create a replacement",
                head_sha,
            )
        if pr_url != revision_pr:
            raise PublicationError(
                f"Branch {worker.branch} belongs to {pr_url}, expected {revision_pr}",
                head_sha,
            )
    updated = pr_url is not None
    if pr_url is None:
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
        _success_comment(worker, pr_url, run_id, attempt, updated=updated),
    )
    _best_effort_labels(
        repo,
        issue.number,
        token,
        add=(config.labels.pr_open,),
        remove=(config.labels.running, config.labels.needs_human),
    )
    return pr_url, head_sha

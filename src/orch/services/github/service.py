"""Thin GitHub CLI client used by the orchestrator."""

from __future__ import annotations

import json
import os
import subprocess
from urllib.parse import quote

from orch.config.logger import get_logger
from orch.exceptions.github import GitHubError
from orch.schemas.github.types import (
    CheckResult,
    Comment,
    Issue,
    PullRequestStatus,
)

log = get_logger(component="github")

BOT_MARKER = "<!-- orch:bot -->"


def resolve_token(owner: str, configured: str | None) -> str:
    """Resolve a GitHub token, preferring the configured value.

    Args:
        owner: GitHub account that must own the token.
        configured: Token from the environment, if any.

    Returns:
        A non-empty GitHub token.

    Raises:
        GitHubError: If no token can be resolved for the account.
    """
    if configured:
        return configured

    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--user", owner],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not execute gh"
        )
        raise GitHubError("Could not execute gh; is GitHub CLI installed?") from ex
    else:
        token = result.stdout.strip()
        if result.returncode != 0 or not token:
            message = (result.stderr or result.stdout).strip()
            log.bind(owner=owner, stderr=message).error(
                "Could not resolve GitHub token"
            )
            raise GitHubError(
                f"Could not resolve a GitHub token for {owner}. "
                "Run `gh auth login` or set GH_TOKEN."
            )
        return token


def ensure_user(token: str, expected: str) -> None:
    """Verify that the token belongs to the expected GitHub account."""
    login = _run_gh(["api", "user", "--jq", ".login"], token).strip()
    if login != expected:
        raise GitHubError(
            f"GitHub token belongs to {login!r}, expected {expected!r}. "
            "Set GH_TOKEN to the correct account."
        )
    log.info("GitHub account: {}", login)


def ensure_labels(
    repo: str,
    token: str,
    labels: list[tuple[str, str, str]],
) -> None:
    """Create or update the labels used by the pipeline.

    Args:
        repo: Repository in ``owner/name`` form.
        token: GitHub token.
        labels: Tuples of ``(name, color, description)``.
    """
    for name, color, description in labels:
        _run_gh(
            [
                "label",
                "create",
                name,
                "--repo",
                repo,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ],
            token,
        )
        log.debug("Ensured label {}", name)


def list_ready_issues(repo: str, label: str, token: str) -> list[Issue]:
    """Return open issues carrying the ready label, oldest first."""
    output = _run_gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            label,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,title,body,url,author,labels,state",
        ],
        token,
    )
    try:
        data = json.loads(output or "[]")
    except json.JSONDecodeError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not parse gh issue list output"
        )
        raise GitHubError("Could not parse gh issue list output") from ex
    else:
        issues = [Issue.from_json(item) for item in data]
        return sorted(issues, key=lambda issue: issue.number)


def get_issue(repo: str, number: int, token: str) -> Issue:
    """Fetch one issue by number."""
    output = _run_gh(
        [
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,url,author,labels,state",
        ],
        token,
    )
    try:
        data = json.loads(output)
    except json.JSONDecodeError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not parse gh issue view output"
        )
        raise GitHubError("Could not parse gh issue view output") from ex
    else:
        return Issue.from_json(data)


def edit_labels(
    repo: str,
    number: int,
    token: str,
    *,
    add: tuple[str, ...] = (),
    remove: tuple[str, ...] = (),
) -> None:
    """Add and remove labels on an issue."""
    if not add and not remove:
        return

    args = ["issue", "edit", str(number), "--repo", repo]
    for label in add:
        args.extend(["--add-label", label])
    for label in remove:
        args.extend(["--remove-label", label])
    _run_gh(args, token)
    log.info("Issue #{} labels: +{} -{}", number, list(add), list(remove))


def comment_issue(repo: str, number: int, token: str, body: str) -> None:
    """Post a comment on an issue."""
    _run_gh(
        ["issue", "comment", str(number), "--repo", repo, "--body-file", "-"],
        token,
        input_text=body,
    )
    log.info("Commented on issue #{}", number)


def mark_bot_body(body: str) -> str:
    """Append the machine-readable orch marker to a comment body."""
    return f"{body}\n\n{BOT_MARKER}"


def comment_bot_issue(repo: str, number: int, token: str, body: str) -> None:
    """Post a comment marked as generated by orch."""
    comment_issue(repo, number, token, mark_bot_body(body))


def list_issue_comments(
    repo: str,
    number: int,
    token: str,
    *,
    since: str | None = None,
) -> list[Comment]:
    """Return issue comments, optionally updated after an ISO timestamp."""
    path = f"repos/{repo}/issues/{number}/comments?per_page=100"
    if since:
        path += f"&since={quote(since, safe='')}"
    output = _run_gh(["api", path], token)
    try:
        data = json.loads(output or "[]")
    except json.JSONDecodeError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not parse gh api issue comments output"
        )
        raise GitHubError("Could not parse issue comments") from ex
    else:
        return [Comment.from_json(item) for item in data]


def issue_is_open(repo: str, number: int, token: str) -> bool:
    """Return whether an issue is still open."""
    output = _run_gh(
        [
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "state",
        ],
        token,
    )
    try:
        data = json.loads(output)
    except json.JSONDecodeError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not parse gh issue state output"
        )
        raise GitHubError("Could not parse issue state") from ex
    else:
        return str(data.get("state") or "").upper() == "OPEN"


def find_open_pr(repo: str, head: str, token: str) -> str | None:
    """Return the URL of an open pull request for the branch, if any."""
    output = _run_gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            head,
            "--state",
            "open",
            "--json",
            "number,url,headRefName",
        ],
        token,
    )
    try:
        data = json.loads(output or "[]")
    except json.JSONDecodeError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not parse gh pr list output"
        )
        raise GitHubError("Could not parse gh pr list output") from ex
    else:
        for item in data:
            if item.get("headRefName") == head and item.get("url"):
                return str(item["url"])
        return None


def delete_branch(repo: str, branch: str, token: str) -> None:
    """Delete a stale remote branch, ignoring an already-missing ref."""
    ref = quote(branch, safe="")
    try:
        _run_gh(
            ["api", "--method", "DELETE", f"repos/{repo}/git/refs/heads/{ref}"],
            token,
        )
    except GitHubError as ex:
        log.bind(branch=branch, error=str(ex)).warning(
            "Could not delete stale branch (it may not exist)"
        )
    else:
        log.info("Deleted stale branch {}", branch)


def get_pr_status(repo: str, pr_url: str, token: str) -> PullRequestStatus:
    """Return the pull-request state and normalized GitHub checks."""
    number = _pr_number(pr_url)
    output = _run_gh(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,url,state,mergeable,mergeStateStatus,headRefOid,statusCheckRollup",
        ],
        token,
    )
    try:
        data = json.loads(output)
    except json.JSONDecodeError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not parse gh pr view output"
        )
        raise GitHubError("Could not parse gh pr view output") from ex
    else:
        rollup = data.get("statusCheckRollup") or []
        checks = tuple(_parse_check(item) for item in rollup if isinstance(item, dict))
        return PullRequestStatus(
            number=int(data["number"]),
            url=str(data.get("url") or pr_url),
            state=str(data.get("state") or "").upper(),
            mergeable=str(data.get("mergeable") or "UNKNOWN").upper(),
            head_sha=str(data.get("headRefOid") or ""),
            checks=checks,
        )


def merge_pr(
    repo: str,
    pr_url: str,
    token: str,
) -> str:
    """Squash-merge a pull request and return its merge commit, if reported."""
    number = _pr_number(pr_url)
    _run_gh(
        [
            "pr",
            "merge",
            str(number),
            "--repo",
            repo,
            "--squash",
            "--delete-branch",
        ],
        token,
        timeout=300,
    )
    log.info("Squash-merged pull request #{}", number)
    try:
        output = _run_gh(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "mergeCommit",
                "--jq",
                ".mergeCommit.oid // empty",
            ],
            token,
        )
    except GitHubError as ex:
        log.bind(pr=number, error=str(ex)).warning(
            "Could not read the merge commit after merging"
        )
        return ""
    return output.strip()


def _parse_check(item: dict[str, object]) -> CheckResult:
    """Normalize a check run or commit status from ``gh pr view``."""
    context = item.get("context")
    state = str(item.get("state") or "").upper()
    if context is not None:
        name = str(context or "status")
        if state == "SUCCESS":
            normalized = "success"
        elif state in {"PENDING", "EXPECTED"}:
            normalized = "pending"
        else:
            normalized = "failure"
        return CheckResult(
            name=name,
            state=normalized,
            link=str(item.get("targetUrl") or ""),
        )

    name = str(item.get("name") or "check")
    status = str(item.get("status") or "").upper()
    conclusion = str(item.get("conclusion") or "").upper()
    if status and status != "COMPLETED":
        normalized = "pending"
    elif conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        normalized = "success"
    else:
        normalized = "failure"
    return CheckResult(
        name=name,
        state=normalized,
        link=str(item.get("detailsUrl") or ""),
    )


def _pr_number(pr_url: str) -> int:
    tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
    if not tail.isdigit():
        raise GitHubError(f"Could not determine pull-request number from {pr_url!r}")
    return int(tail)


def create_pr(
    repo: str,
    token: str,
    *,
    head: str,
    base: str,
    title: str,
    body: str,
) -> str:
    """Create a pull request or return the existing open one."""
    existing = find_open_pr(repo, head, token)
    if existing:
        log.info("Pull request already exists: {}", existing)
        return existing

    output = _run_gh(
        [
            "pr",
            "create",
            "--repo",
            repo,
            "--head",
            head,
            "--base",
            base,
            "--title",
            title,
            "--body-file",
            "-",
        ],
        token,
        input_text=body,
    )
    url = output.strip().splitlines()[-1] if output.strip() else ""
    if not url:
        raise GitHubError("gh pr create returned no pull request URL")
    log.info("Created pull request: {}", url)
    return url


def _run_gh(
    args: list[str],
    token: str,
    *,
    input_text: str | None = None,
    check: bool = True,
    timeout: int = 120,
) -> str:
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_TOKEN"] = token

    try:
        result = subprocess.run(
            ["gh", *args],
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as ex:
        log.bind(args=args[:3], timeout=timeout).error(
            "GitHub CLI command timed out"
        )
        raise GitHubError(f"GitHub CLI command timed out after {timeout}s") from ex
    except OSError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not execute gh"
        )
        raise GitHubError("Could not execute gh") from ex
    else:
        if check and result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            log.bind(
                args=args[:3],
                returncode=result.returncode,
                stderr=message,
            ).error("GitHub CLI command failed")
            raise GitHubError(message or "GitHub CLI command failed")
        return result.stdout

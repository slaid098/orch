"""Thin GitHub CLI client used by the orchestrator."""

from __future__ import annotations

import json
import os
import subprocess
from urllib.parse import quote

from orch.config.logger import get_logger
from orch.exceptions.github import GitHubError
from orch.schemas.github.types import Issue

log = get_logger(component="github")


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
            "number,title,body,url,author,labels",
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
            "number,title,body,url,author,labels",
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

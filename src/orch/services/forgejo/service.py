"""Forgejo REST API client used by the orchestrator."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from orch.config.logger import get_logger
from orch.constants.standards import BOT_MARKER, PR_ISSUE_MARKER
from orch.exceptions.forgejo import ForgejoError
from orch.schemas.github.types import (
    CheckResult,
    Comment,
    Issue,
    PullRequestRef,
    PullRequestStatus,
)

log = get_logger(component="forgejo")

_PAGE_SIZE = 50
_API_TIMEOUT = 120
_MERGE_TIMEOUT = 300
_SUCCESS_STATES = frozenset({"success", "warning", "skipped"})
_FAILURE_STATES = frozenset({"error", "failure"})


def resolve_token(configured: str | None) -> str:
    """Resolve a Forgejo token from configuration or ``FORGEJO_TOKEN``.

    Args:
        configured: Token already resolved by the configuration loader.

    Returns:
        A non-empty Forgejo token.

    Raises:
        ForgejoError: If no token is available.
    """
    token = (configured or os.environ.get("FORGEJO_TOKEN") or "").strip()
    if not token:
        raise ForgejoError(
            "Could not resolve a Forgejo token. Set FORGEJO_TOKEN or ORCH_FORGEJO_TOKEN."
        )
    return token


def ensure_user(url: str, token: str, expected: str) -> None:
    """Verify that the token belongs to the expected Forgejo account.

    Args:
        url: Forgejo base URL.
        token: Forgejo API token.
        expected: Expected account login, normally the repository owner.

    Raises:
        ForgejoError: If the token belongs to another account.
    """
    data = _request(url, token, "user")
    login = str((data or {}).get("login") or "")
    if login != expected:
        raise ForgejoError(
            f"Forgejo token belongs to {login!r}, expected {expected!r}. "
            "Set FORGEJO_TOKEN to the correct account."
        )
    log.info("Forgejo account: {}", login)


def check_repo(url: str, repo: str, token: str) -> None:
    """Verify that the token can read the repository.

    Args:
        url: Forgejo base URL.
        repo: Repository in ``owner/name`` form.
        token: Forgejo API token.

    Raises:
        ForgejoError: If the repository is missing or not readable.
    """
    _request(url, token, f"repos/{repo}")


def ensure_labels(
    url: str,
    repo: str,
    token: str,
    labels: list[tuple[str, str, str]],
) -> None:
    """Create or update the labels used by the pipeline.

    Args:
        url: Forgejo base URL.
        repo: Repository in ``owner/name`` form.
        token: Forgejo API token.
        labels: Tuples of ``(name, color, description)``.
    """
    existing = _repo_labels(url, repo, token)
    for name, color, description in labels:
        payload = {
            "name": name,
            "color": color.lstrip("#"),
            "description": description,
        }
        label_id = existing.get(name)
        if label_id is None:
            _request(url, token, f"repos/{repo}/labels", method="POST", payload=payload)
        else:
            _request(
                url,
                token,
                f"repos/{repo}/labels/{label_id}",
                method="PATCH",
                payload=payload,
            )
        log.debug("Ensured label {}", name)


def list_ready_issues(
    url: str,
    repo: str,
    label: str,
    token: str,
) -> list[Issue]:
    """Return open issues carrying the ready label, oldest first.

    Args:
        url: Forgejo base URL.
        repo: Repository in ``owner/name`` form.
        label: Required label name.
        token: Forgejo API token.

    Returns:
        Matching issues sorted by number.
    """
    path = (
        f"repos/{repo}/issues?state=open&type=issues"
        f"&labels={quote(label, safe='')}"
    )
    issues = [
        Issue.from_forgejo(item)
        for item in _paged(url, token, path)
        if not item.get("pull_request")
    ]
    return sorted(issues, key=lambda issue: issue.number)


def get_issue(url: str, repo: str, number: int, token: str) -> Issue:
    """Fetch one issue by number."""
    data = _request(url, token, f"repos/{repo}/issues/{number}")
    return Issue.from_forgejo(_mapping(data, "issue"))


def list_blockers(url: str, repo: str, number: int, token: str) -> list[Issue]:
    """Return the issues that block the given issue.

    A ``404`` response means that dependencies are unavailable or unset and
    is treated as an empty list.

    Args:
        url: Forgejo base URL.
        repo: Repository in ``owner/name`` form.
        number: Issue whose blockers are requested.
        token: Forgejo API token.

    Returns:
        Unique blocking issues in forge order.

    Raises:
        ForgejoError: If the request fails, the response is invalid, or an
            issue cannot be mapped.
    """
    path = f"repos/{repo}/issues/{number}/dependencies"
    items = _paged(url, token, path, missing_ok=True)
    blockers: dict[int, Issue] = {}
    for item in items:
        try:
            issue = Issue.from_forgejo(item)
        except (KeyError, TypeError, ValueError) as ex:
            log.bind(error=ex, error_type=type(ex).__name__).error(
                "Could not parse a Forgejo issue dependency"
            )
            raise ForgejoError("Could not parse a Forgejo issue dependency") from ex
        blockers.setdefault(issue.number, issue)
    return list(blockers.values())


def issue_is_open(url: str, repo: str, number: int, token: str) -> bool:
    """Return whether an issue is still open."""
    data = _request(url, token, f"repos/{repo}/issues/{number}")
    return str((data or {}).get("state") or "").lower() == "open"


def edit_labels(
    url: str,
    repo: str,
    number: int,
    token: str,
    *,
    add: tuple[str, ...] = (),
    remove: tuple[str, ...] = (),
) -> None:
    """Add and remove labels on an issue while preserving unmanaged ones.

    Forgejo replaces the complete label set in one request, so the current
    labels are read first and only the requested names are changed.
    """
    if not add and not remove:
        return

    issue = _mapping(
        _request(url, token, f"repos/{repo}/issues/{number}"),
        "issue",
    )
    current = [
        (int(label["id"]), str(label["name"]))
        for label in issue.get("labels") or []
        if isinstance(label, dict) and label.get("id") is not None
    ]
    removed = set(remove)
    final = [(label_id, name) for label_id, name in current if name not in removed]
    present = {name for _, name in final}
    available = _repo_labels(url, repo, token)
    for name in add:
        if name in present:
            continue
        label_id = available.get(name)
        if label_id is None:
            raise ForgejoError(f"Label {name!r} does not exist in {repo}")
        final.append((label_id, name))
        present.add(name)

    _request(
        url,
        token,
        f"repos/{repo}/issues/{number}/labels",
        method="PUT",
        payload={"labels": [label_id for label_id, _ in final]},
    )
    log.info("Issue #{} labels: +{} -{}", number, list(add), list(remove))


def comment_issue(url: str, repo: str, number: int, token: str, body: str) -> None:
    """Post a comment on an issue."""
    _request(
        url,
        token,
        f"repos/{repo}/issues/{number}/comments",
        method="POST",
        payload={"body": body},
    )
    log.info("Commented on issue #{}", number)


def create_issue(
    url: str,
    repo: str,
    token: str,
    *,
    title: str,
    body: str,
    labels: tuple[str, ...] = ("agent:queued",),
) -> str:
    """Create an issue and return its web URL.

    Args:
        url: Forgejo base URL.
        repo: Repository in ``owner/name`` form.
        token: Forgejo API token.
        title: Issue title that already passed the standards validator.
        body: Issue body in Markdown.
        labels: Label names to attach; they must already exist.

    Returns:
        The created issue's web URL.

    Raises:
        ForgejoError: If the request fails, a label is missing, or the
            response contains no issue URL.
    """
    available = _repo_labels(url, repo, token)
    missing = [name for name in labels if name not in available]
    if missing:
        raise ForgejoError(
            f"Labels do not exist in {repo}: {', '.join(missing)}"
        )
    data = _request(
        url,
        token,
        f"repos/{repo}/issues",
        method="POST",
        payload={
            "title": title,
            "body": body,
            "labels": [available[name] for name in labels],
        },
    )
    issue_url = str((data or {}).get("html_url") or "")
    if not issue_url:
        raise ForgejoError("Forgejo returned no issue URL")
    log.info("Created issue: {}", issue_url)
    return issue_url


def mark_bot_body(body: str) -> str:
    """Append the machine-readable orch marker to a comment body."""
    return f"{body}\n\n{BOT_MARKER}"


def comment_bot_issue(url: str, repo: str, number: int, token: str, body: str) -> None:
    """Post a comment marked as generated by orch."""
    comment_issue(url, repo, number, token, mark_bot_body(body))


def list_issue_comments(
    url: str,
    repo: str,
    number: int,
    token: str,
    *,
    since: str | None = None,
) -> list[Comment]:
    """Return issue comments, optionally updated after an ISO timestamp."""
    path = f"repos/{repo}/issues/{number}/comments"
    if since:
        path += f"?since={quote(since, safe='')}"
    data = _request(url, token, path)
    if not isinstance(data, list):
        raise ForgejoError("Could not parse issue comments")
    return [
        Comment.from_forgejo(item)
        for item in data
        if isinstance(item, dict)
    ]


def find_open_pr(url: str, repo: str, head: str, token: str) -> str | None:
    """Return the URL of an open pull request for the branch, if any."""
    for item in _list_open_prs(url, repo, token, head=head):
        if _head_branch(item) == head and item.get("html_url"):
            return str(item["html_url"])
    return None


def find_open_pr_for_issue(
    url: str,
    repo: str,
    issue_number: int,
    token: str,
    *,
    preferred_head: str | None = None,
) -> PullRequestRef | None:
    """Return the open pull request that belongs to an issue.

    Args:
        url: Forgejo base URL.
        repo: Repository in ``owner/name`` form.
        issue_number: Issue number referenced by the pull request.
        token: Forgejo API token.
        preferred_head: Branch that a new run would use, if any.

    Returns:
        The matching pull request, or ``None`` when there is no open one.

    Raises:
        ForgejoError: If the lookup fails or finds more than one match.
    """
    if preferred_head:
        found = find_open_pr(url, repo, preferred_head, token)
        if found:
            return PullRequestRef(
                number=_pr_number(found),
                url=found,
                head_ref=preferred_head,
            )
    matches = [
        item
        for item in _list_open_prs(url, repo, token)
        if _belongs_to_issue(item, issue_number, repo)
    ]
    if not matches:
        return None
    if len(matches) > 1:
        urls = ", ".join(str(item.get("html_url") or "?") for item in matches)
        raise ForgejoError(
            f"Multiple open pull requests reference issue #{issue_number}: {urls}"
        )
    item = matches[0]
    url_value = str(item.get("html_url") or "")
    head = _head_branch(item)
    number = item.get("number")
    if not url_value or not head or not isinstance(number, int):
        raise ForgejoError(
            "Open pull request is missing its number, URL, or head branch"
        )
    return PullRequestRef(number=number, url=url_value, head_ref=head)


def open_pr_issue_numbers(
    url: str,
    repo: str,
    issue_numbers: Iterable[int],
    token: str,
) -> set[int]:
    """Return which of the given issues have an open pull request."""
    wanted = set(issue_numbers)
    if not wanted:
        return set()
    pull_requests = _list_open_prs(url, repo, token)
    return {
        number
        for number in wanted
        if any(_belongs_to_issue(item, number, repo) for item in pull_requests)
    }


def create_pr(
    url: str,
    repo: str,
    token: str,
    *,
    head: str,
    base: str,
    title: str,
    body: str,
) -> str:
    """Create a pull request or return the existing open one."""
    existing = find_open_pr(url, repo, head, token)
    if existing:
        log.info("Pull request already exists: {}", existing)
        return existing

    try:
        data = _request(
            url,
            token,
            f"repos/{repo}/pulls",
            method="POST",
            payload={
                "head": head,
                "base": base,
                "title": title,
                "body": body,
            },
        )
    except ForgejoError:
        existing = find_open_pr(url, repo, head, token)
        if existing:
            log.info("Pull request already exists: {}", existing)
            return existing
        raise

    pr_url = str((data or {}).get("html_url") or "")
    if not pr_url:
        raise ForgejoError("Forgejo returned no pull request URL")
    log.info("Created pull request: {}", pr_url)
    return pr_url


def get_pr_status(url: str, repo: str, pr_url: str, token: str) -> PullRequestStatus:
    """Return the pull-request state and normalized commit statuses."""
    number = _pr_number(pr_url)
    data = _mapping(
        _request(url, token, f"repos/{repo}/pulls/{number}"),
        "pull request",
    )
    head = data.get("head") or {}
    head_sha = str(head.get("sha") or "")
    checks = _commit_checks(url, repo, head_sha, token) if head_sha else ()
    mergeable = data.get("mergeable")
    return PullRequestStatus(
        number=int(data.get("number") or number),
        url=str(data.get("html_url") or pr_url),
        state=_pr_state(data),
        mergeable=_mergeable(mergeable),
        head_sha=head_sha,
        checks=checks,
    )


def merge_pr(url: str, repo: str, pr_url: str, token: str) -> str:
    """Squash-merge a pull request and return its merge commit, if reported."""
    number = _pr_number(pr_url)
    data = _mapping(
        _request(url, token, f"repos/{repo}/pulls/{number}"),
        "pull request",
    )
    head = data.get("head") or {}
    try:
        result = _request(
            url,
            token,
            f"repos/{repo}/pulls/{number}/merge",
            method="POST",
            payload={
                "Do": "squash",
                "delete_branch_after_merge": True,
                "head_commit_id": str(head.get("sha") or ""),
            },
            timeout=_MERGE_TIMEOUT,
        )
    except ForgejoError:
        merged = _mapping(
            _request(url, token, f"repos/{repo}/pulls/{number}"),
            "pull request",
        )
        if not merged.get("merged"):
            raise
        result = {"merged": True}

    if isinstance(result, dict) and result.get("merged") is False:
        raise ForgejoError(
            str(result.get("message") or "Forgejo did not merge the pull request")
        )

    merged = _mapping(
        _request(url, token, f"repos/{repo}/pulls/{number}"),
        "pull request",
    )
    if not merged.get("merged"):
        raise ForgejoError(
            f"Forgejo did not merge pull request #{number}: "
            f"{(result or {}).get('message', 'unknown reason')}"
        )
    log.info("Squash-merged pull request #{}", number)
    return str(merged.get("merge_commit_sha") or "")


def delete_branch(url: str, repo: str, branch: str, token: str) -> None:
    """Delete a stale remote branch, ignoring an already-missing ref."""
    try:
        _request(
            url,
            token,
            f"repos/{repo}/branches/{quote(branch, safe='')}",
            method="DELETE",
            quiet_statuses=frozenset({404}),
        )
    except ForgejoError as ex:
        log.bind(branch=branch, error=str(ex)).warning(
            "Could not delete stale branch (it may not exist)"
        )
    else:
        log.info("Deleted stale branch {}", branch)


def _commit_checks(
    url: str,
    repo: str,
    sha: str,
    token: str,
) -> tuple[CheckResult, ...]:
    data = _request(url, token, f"repos/{repo}/commits/{quote(sha, safe='')}/status")
    statuses = (data or {}).get("statuses") or []
    return tuple(
        _parse_check(item) for item in statuses if isinstance(item, dict)
    )


def _parse_check(item: dict[str, Any]) -> CheckResult:
    state = str(item.get("status") or "").lower()
    if state in _SUCCESS_STATES:
        normalized = "success"
    elif state == "pending":
        normalized = "pending"
    elif state in _FAILURE_STATES:
        normalized = "failure"
    else:
        normalized = "failure"
    return CheckResult(
        name=str(item.get("context") or "status"),
        state=normalized,
        link=str(item.get("target_url") or ""),
    )


def _pr_state(data: dict[str, Any]) -> str:
    if data.get("merged"):
        return "MERGED"
    return str(data.get("state") or "").upper()


def _mergeable(value: object) -> str:
    if value is True:
        return "MERGEABLE"
    if value is False:
        return "CONFLICTING"
    return "UNKNOWN"


def _list_open_prs(
    url: str,
    repo: str,
    token: str,
    *,
    head: str | None = None,
) -> list[dict[str, Any]]:
    path = f"repos/{repo}/pulls?state=open"
    if head:
        path += f"&head={quote(head, safe='')}"
    return _paged(url, token, path)


def _belongs_to_issue(
    item: dict[str, Any],
    issue_number: int,
    repo: str,
) -> bool:
    head = item.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name")
    if head_repo and head_repo != repo:
        return False
    body = str(item.get("body") or "")
    if PR_ISSUE_MARKER.format(issue_number=issue_number) in body:
        return True
    if re.search(rf"(?mi)^(?:closes|fixes|resolves)\s+#{issue_number}\s*$", body):
        return True
    branch = _head_branch(item)
    return re.search(rf"(?:^|/)issue-{issue_number}(?:-|$)", branch) is not None


def _head_branch(item: dict[str, Any]) -> str:
    head = item.get("head") or {}
    label = str(head.get("label") or "")
    if ":" in label:
        label = label.split(":", 1)[1]
    return label or str(head.get("ref") or "")


def _repo_labels(url: str, repo: str, token: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in _paged(url, token, f"repos/{repo}/labels"):
        name = item.get("name")
        label_id = item.get("id")
        if isinstance(name, str) and isinstance(label_id, int):
            result[name] = label_id
    return result


def _paged(
    url: str,
    token: str,
    path: str,
    *,
    missing_ok: bool = False,
) -> list[dict[str, Any]]:
    """Return every page of a Forgejo list endpoint.

    When ``missing_ok`` is true, a 404 on the first page means that the
    endpoint does not exist and yields an empty list. A 404 on a later page
    still fails because data was already returned.
    """
    items: list[dict[str, Any]] = []
    page = 1
    separator = "&" if "?" in path else "?"
    while True:
        try:
            data = _request(
                url,
                token,
                f"{path}{separator}limit={_PAGE_SIZE}&page={page}",
            )
        except ForgejoError as ex:
            if missing_ok and page == 1 and _is_not_found(ex):
                return []
            raise
        if not isinstance(data, list):
            raise ForgejoError(f"Could not parse Forgejo list response for {path}")
        items.extend(item for item in data if isinstance(item, dict))
        if len(data) < _PAGE_SIZE:
            return items
        page += 1


def _is_not_found(ex: ForgejoError) -> bool:
    """Return whether an API error is a confirmed HTTP 404 response.

    Only an ``HTTPError`` chained by ``_request`` counts. A network failure
    whose message happens to mention ``404`` must stay a read failure so that
    the dependency gate fails closed.
    """
    cause = ex.__cause__
    return isinstance(cause, HTTPError) and cause.code == 404


def _request(
    url: str,
    token: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = _API_TIMEOUT,
    quiet_statuses: frozenset[int] = frozenset(),
) -> Any:
    api_url = f"{_api_base(url)}/{path.lstrip('/')}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    request = Request(
        api_url,
        data=data,
        method=method,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "orch",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as ex:
        message = _error_message(ex)
        if ex.code not in quiet_statuses:
            log.bind(
                method=method,
                path=path,
                status=ex.code,
                error=message,
            ).error("Forgejo API request failed")
        raise ForgejoError(
            f"Forgejo API {method} {path} failed with {ex.code}: {message}"
        ) from ex
    except (URLError, TimeoutError, OSError) as ex:
        log.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not reach the Forgejo API"
        )
        raise ForgejoError(f"Could not reach the Forgejo API: {ex}") from ex
    else:
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as ex:
            log.bind(error=ex, error_type=type(ex).__name__).error(
                "Could not parse a Forgejo API response"
            )
            raise ForgejoError("Could not parse a Forgejo API response") from ex


def _api_base(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ForgejoError(f"Invalid Forgejo URL: {url!r}")
    if parsed.username or parsed.password:
        raise ForgejoError("Forgejo URL must not contain credentials")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/api/v1"


def _error_message(ex: HTTPError) -> str:
    try:
        raw = ex.read().decode("utf-8", errors="replace")
    except OSError:
        return str(ex.reason)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()[:500] or str(ex.reason)
    if isinstance(payload, dict) and payload.get("message"):
        return str(payload["message"])[:500]
    return raw.strip()[:500] or str(ex.reason)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ForgejoError(f"Could not parse the Forgejo {name} response")
    return value


def _pr_number(pr_url: str) -> int:
    tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
    if not tail.isdigit():
        raise ForgejoError(f"Could not determine pull-request number from {pr_url!r}")
    return int(tail)

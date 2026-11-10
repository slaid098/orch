"""Tests for the Forgejo REST client."""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

from orch.constants.standards import BOT_MARKER, PR_ISSUE_MARKER
from orch.exceptions.forgejo import ForgejoError
from orch.services.forgejo import service as forgejo

_BASE = "https://git.example.test"
_REPO = "slaid098/orch"


class _Response:
    def __init__(self, body: bytes = b"") -> None:
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class _Api:
    def __init__(self, routes: dict[tuple[str, str], object]) -> None:
        self.routes = routes
        self.requests: list[dict[str, object]] = []

    def __call__(self, request: object, timeout: int | None = None) -> _Response:
        del timeout
        parsed = urlparse(request.full_url)
        path = parsed.path.split("/api/v1/", 1)[1]
        query = parse_qs(parsed.query)
        body = json.loads(request.data) if request.data else None
        method = request.get_method()
        self.requests.append(
            {
                "method": method,
                "path": path,
                "query": query,
                "body": body,
                "headers": dict(request.header_items()),
            }
        )
        value = self.routes.get((method, path))
        if value is None:
            raise AssertionError(f"Unexpected request: {method} {path}")
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(query, body)
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        return _Response(raw)


def _use(monkeypatch: pytest.MonkeyPatch, routes: dict[tuple[str, str], object]) -> _Api:
    api = _Api(routes)
    monkeypatch.setattr(forgejo, "urlopen", api)
    return api


def _http_error(code: int, body: str, *, reason: str = "error") -> HTTPError:
    return HTTPError(
        f"{_BASE}/api/v1/x",
        code,
        reason,
        None,
        io.BytesIO(body.encode("utf-8")),
    )


def _issue_payload(number: int, *, state: str = "open") -> dict[str, object]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "Body",
        "html_url": f"https://git.example.test/slaid098/orch/issues/{number}",
        "user": {"login": "author"},
        "labels": [{"id": 1, "name": "agent:queued"}],
        "state": state,
    }


def _pr_payload(
    number: int = 10,
    *,
    head: dict[str, object] | None = None,
    body: str = "",
    mergeable: bool | None = True,
    merged: bool = False,
    state: str = "open",
) -> dict[str, object]:
    return {
        "number": number,
        "html_url": f"https://git.example.test/slaid098/orch/pulls/{number}",
        "body": body,
        "state": state,
        "merged": merged,
        "mergeable": mergeable,
        "head": head if head is not None else {"label": "fix/x", "sha": "abc123"},
        "merge_commit_sha": "mergesha",
    }


def test_resolve_token_prefers_the_configured_value() -> None:
    assert forgejo.resolve_token("configured") == "configured"


def test_resolve_token_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGEJO_TOKEN", "env-token")

    assert forgejo.resolve_token(None) == "env-token"


def test_resolve_token_requires_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)

    with pytest.raises(ForgejoError, match="Could not resolve"):
        forgejo.resolve_token(None)


def test_ensure_user_accepts_the_expected_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(monkeypatch, {("GET", "user"): {"login": "slaid098"}})

    forgejo.ensure_user(_BASE, "token", "slaid098")

    assert api.requests[0]["headers"]["Authorization"] == "token token"


def test_ensure_user_rejects_another_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, {("GET", "user"): {"login": "someone-else"}})

    with pytest.raises(ForgejoError, match="someone-else"):
        forgejo.ensure_user(_BASE, "token", "slaid098")


def test_check_repo_requests_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(monkeypatch, {("GET", f"repos/{_REPO}"): {}})

    forgejo.check_repo(_BASE, _REPO, "token")

    assert api.requests[0]["path"] == f"repos/{_REPO}"


def test_ensure_labels_creates_missing_and_updates_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/labels"): [
                {"id": 1, "name": "agent:queued"},
            ],
            ("PATCH", f"repos/{_REPO}/labels/1"): {},
            ("POST", f"repos/{_REPO}/labels"): {},
        },
    )

    forgejo.ensure_labels(
        _BASE,
        _REPO,
        "token",
        [
            ("agent:queued", "#00ff00", "Queued"),
            ("agent:hold", "5319e7", "Hold"),
        ],
    )

    patch = next(item for item in api.requests if item["method"] == "PATCH")
    post = next(item for item in api.requests if item["method"] == "POST")
    assert patch["body"] == {
        "name": "agent:queued",
        "color": "00ff00",
        "description": "Queued",
    }
    assert post["body"] == {
        "name": "agent:hold",
        "color": "5319e7",
        "description": "Hold",
    }


def test_list_ready_issues_excludes_pull_requests_and_sorts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/issues"): [
                _issue_payload(2),
                _issue_payload(1),
                {**_issue_payload(3), "pull_request": {"merged": False}},
            ]
        },
    )

    issues = forgejo.list_ready_issues(_BASE, _REPO, "agent:queued", "token")

    assert [issue.number for issue in issues] == [1, 2]
    assert issues[0].url.endswith("/issues/1")
    assert issues[0].state == "OPEN"
    assert issues[0].labels == ("agent:queued",)
    query = api.requests[0]["query"]
    assert query["labels"] == ["agent:queued"]
    assert query["type"] == ["issues"]
    assert query["state"] == ["open"]


def test_get_issue_maps_forgejo_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(
        monkeypatch,
        {("GET", f"repos/{_REPO}/issues/7"): _issue_payload(7, state="closed")},
    )

    issue = forgejo.get_issue(_BASE, _REPO, 7, "token")

    assert issue.number == 7
    assert issue.author == "author"
    assert issue.state == "CLOSED"
    assert issue.url == "https://git.example.test/slaid098/orch/issues/7"


@pytest.mark.parametrize(("state", "expected"), [("open", True), ("closed", False)])
def test_issue_is_open(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    expected: bool,
) -> None:
    _use(
        monkeypatch,
        {("GET", f"repos/{_REPO}/issues/7"): _issue_payload(7, state=state)},
    )

    assert forgejo.issue_is_open(_BASE, _REPO, 7, "token") is expected


def test_edit_labels_replaces_only_managed_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/issues/7"): {
                **_issue_payload(7),
                "labels": [
                    {"id": 1, "name": "agent:queued"},
                    {"id": 2, "name": "bug"},
                ],
            },
            ("GET", f"repos/{_REPO}/labels"): [
                {"id": 1, "name": "agent:queued"},
                {"id": 3, "name": "agent:running"},
            ],
            ("PUT", f"repos/{_REPO}/issues/7/labels"): {},
        },
    )

    forgejo.edit_labels(
        _BASE,
        _REPO,
        7,
        "token",
        add=("agent:running",),
        remove=("agent:queued",),
    )

    update = next(item for item in api.requests if item["method"] == "PUT")
    assert update["body"] == {"labels": [2, 3]}


def test_edit_labels_rejects_an_unknown_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/issues/7"): _issue_payload(7),
            ("GET", f"repos/{_REPO}/labels"): [],
        },
    )

    with pytest.raises(ForgejoError, match="does not exist"):
        forgejo.edit_labels(
            _BASE,
            _REPO,
            7,
            "token",
            add=("missing",),
        )


def test_edit_labels_is_a_noop_without_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(monkeypatch, {})

    forgejo.edit_labels(_BASE, _REPO, 7, "token")

    assert api.requests == []


def test_comment_issue_posts_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _use(monkeypatch, {("POST", f"repos/{_REPO}/issues/7/comments"): {}})

    forgejo.comment_issue(_BASE, _REPO, 7, "token", "hello")

    assert api.requests[0]["body"] == {"body": "hello"}


def test_comment_bot_issue_adds_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(monkeypatch, {("POST", f"repos/{_REPO}/issues/7/comments"): {}})

    forgejo.comment_bot_issue(_BASE, _REPO, 7, "token", "hello")

    assert api.requests[0]["body"] == {"body": f"hello\n\n{BOT_MARKER}"}


def test_create_issue_posts_resolved_label_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/labels"): [
                {"id": 3, "name": "agent:queued"},
                {"id": 4, "name": "bug"},
            ],
            ("POST", f"repos/{_REPO}/issues"): {
                "number": 12,
                "html_url": "https://git.example.test/slaid098/orch/issues/12",
            },
        },
    )

    url = forgejo.create_issue(
        _BASE,
        _REPO,
        "token",
        title="feat(api): add a thing",
        body="Body",
        labels=("agent:queued", "bug"),
    )

    assert url == "https://git.example.test/slaid098/orch/issues/12"
    post = next(item for item in api.requests if item["method"] == "POST")
    assert post["body"] == {
        "title": "feat(api): add a thing",
        "body": "Body",
        "labels": [3, 4],
    }


def test_create_issue_rejects_unknown_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(
        monkeypatch,
        {("GET", f"repos/{_REPO}/labels"): [{"id": 3, "name": "agent:queued"}]},
    )

    with pytest.raises(ForgejoError, match="do not exist"):
        forgejo.create_issue(
            _BASE,
            _REPO,
            "token",
            title="feat(api): add a thing",
            body="Body",
            labels=("missing",),
        )

    assert not any(item["method"] == "POST" for item in api.requests)


def test_create_issue_requires_a_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/labels"): [{"id": 3, "name": "agent:queued"}],
            ("POST", f"repos/{_REPO}/issues"): {"number": 12},
        },
    )

    with pytest.raises(ForgejoError, match="no issue URL"):
        forgejo.create_issue(
            _BASE,
            _REPO,
            "token",
            title="feat(api): add a thing",
            body="Body",
        )


def test_list_issue_comments_maps_and_forwards_since(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/issues/7/comments"): [
                {
                    "id": 5,
                    "user": {"login": "reviewer"},
                    "body": "Fix it",
                    "created_at": "2026-11-10T10:00:00Z",
                    "html_url": "https://git.example.test/comment/5",
                }
            ]
        },
    )

    comments = forgejo.list_issue_comments(
        _BASE,
        _REPO,
        7,
        "token",
        since="2026-11-10T09:00:00Z",
    )

    assert comments[0].id == 5
    assert comments[0].author == "reviewer"
    assert comments[0].body == "Fix it"
    assert api.requests[0]["query"]["since"] == ["2026-11-10T09:00:00Z"]


def test_list_issue_comments_rejects_a_non_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, {("GET", f"repos/{_REPO}/issues/7/comments"): {}})

    with pytest.raises(ForgejoError, match="Could not parse"):
        forgejo.list_issue_comments(_BASE, _REPO, 7, "token")


def test_find_open_pr_uses_the_head_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): [
                _pr_payload(
                    head={"ref": "refs/pull/10/head", "label": "fix/x"},
                )
            ]
        },
    )

    found = forgejo.find_open_pr(_BASE, _REPO, "fix/x", "token")

    assert found == "https://git.example.test/slaid098/orch/pulls/10"
    assert api.requests[0]["query"]["head"] == ["fix/x"]


def test_find_open_pr_falls_back_to_the_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): [
                _pr_payload(head={"ref": "fix/fallback"}),
            ]
        },
    )

    assert forgejo.find_open_pr(_BASE, _REPO, "fix/fallback", "token")


def test_find_open_pr_returns_none_without_a_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {("GET", f"repos/{_REPO}/pulls"): [_pr_payload(head={"label": "other"})]},
    )

    assert forgejo.find_open_pr(_BASE, _REPO, "fix/x", "token") is None


def test_find_open_pr_for_issue_prefers_the_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): [
                _pr_payload(head={"label": "fix/preferred"}),
            ]
        },
    )

    found = forgejo.find_open_pr_for_issue(
        _BASE,
        _REPO,
        7,
        "token",
        preferred_head="fix/preferred",
    )

    assert found is not None
    assert found.head_ref == "fix/preferred"


@pytest.mark.parametrize(
    "body",
    [
        PR_ISSUE_MARKER.format(issue_number=7),
        "Fixes #7",
        "Closes #7",
    ],
)
def test_find_open_pr_for_issue_matches_the_body(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): [
                _pr_payload(body=body),
            ]
        },
    )

    found = forgejo.find_open_pr_for_issue(_BASE, _REPO, 7, "token")

    assert found is not None
    assert found.number == 10


def test_find_open_pr_for_issue_matches_a_legacy_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): [
                _pr_payload(head={"label": "fix/issue-7-repair"}),
            ]
        },
    )

    found = forgejo.find_open_pr_for_issue(_BASE, _REPO, 7, "token")

    assert found is not None


def test_find_open_pr_for_issue_ignores_another_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): [
                _pr_payload(
                    body=PR_ISSUE_MARKER.format(issue_number=7),
                    head={
                        "label": "fix/x",
                        "repo": {"full_name": "other/repo"},
                    },
                )
            ]
        },
    )

    assert forgejo.find_open_pr_for_issue(_BASE, _REPO, 7, "token") is None


def test_find_open_pr_for_issue_rejects_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): [
                _pr_payload(number=10, body="Fixes #7"),
                _pr_payload(number=11, body="Fixes #7"),
            ]
        },
    )

    with pytest.raises(ForgejoError, match="Multiple open pull requests"):
        forgejo.find_open_pr_for_issue(_BASE, _REPO, 7, "token")


def test_find_open_pr_for_issue_rejects_missing_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): [
                {
                    "number": 10,
                    "html_url": "",
                    "body": "Fixes #7",
                    "head": {"label": "fix/x"},
                }
            ]
        },
    )

    with pytest.raises(ForgejoError, match="missing"):
        forgejo.find_open_pr_for_issue(_BASE, _REPO, 7, "token")


def test_open_pr_issue_numbers_matches_each_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): [
                _pr_payload(number=10, body="Fixes #7"),
                _pr_payload(number=11, body="Fixes #9"),
            ]
        },
    )

    assert forgejo.open_pr_issue_numbers(_BASE, _REPO, {7, 8}, "token") == {7}
    assert forgejo.open_pr_issue_numbers(_BASE, _REPO, set(), "token") == set()


def test_create_pr_returns_an_existing_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): [
                _pr_payload(head={"label": "fix/x"}),
            ]
        },
    )

    assert forgejo.create_pr(
        _BASE,
        _REPO,
        "token",
        head="fix/x",
        base="main",
        title="Title",
        body="Body",
    ) == "https://git.example.test/slaid098/orch/pulls/10"
    assert not any(item["method"] == "POST" for item in api.requests)


def test_create_pr_posts_a_new_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): [],
            ("POST", f"repos/{_REPO}/pulls"): _pr_payload(),
        },
    )

    url = forgejo.create_pr(
        _BASE,
        _REPO,
        "token",
        head="fix/x",
        base="main",
        title="Title",
        body="Body",
    )

    assert url.endswith("/pulls/10")
    post = next(item for item in api.requests if item["method"] == "POST")
    assert post["body"] == {
        "head": "fix/x",
        "base": "main",
        "title": "Title",
        "body": "Body",
    }


def test_create_pr_recovers_after_a_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}

    def pulls(_query: object, _body: object) -> list[dict[str, object]]:
        calls["count"] += 1
        return [] if calls["count"] == 1 else [_pr_payload()]

    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): pulls,
            ("POST", f"repos/{_REPO}/pulls"): _http_error(409, "already exists"),
        },
    )

    assert forgejo.create_pr(
        _BASE,
        _REPO,
        "token",
        head="fix/x",
        base="main",
        title="Title",
        body="Body",
    ).endswith("/pulls/10")


def test_create_pr_reraises_a_persistent_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): [],
            ("POST", f"repos/{_REPO}/pulls"): _http_error(500, "boom"),
        },
    )

    with pytest.raises(ForgejoError, match="boom"):
        forgejo.create_pr(
            _BASE,
            _REPO,
            "token",
            head="fix/x",
            base="main",
            title="Title",
            body="Body",
        )


def test_create_pr_requires_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls"): [],
            ("POST", f"repos/{_REPO}/pulls"): {},
        },
    )

    with pytest.raises(ForgejoError, match="no pull request URL"):
        forgejo.create_pr(
            _BASE,
            _REPO,
            "token",
            head="fix/x",
            base="main",
            title="Title",
            body="Body",
        )


def test_get_pr_status_normalizes_commit_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls/10"): _pr_payload(),
            ("GET", f"repos/{_REPO}/commits/abc123/status"): {
                "statuses": [
                    {"status": "success", "context": "lint"},
                    {"status": "warning", "context": "audit"},
                    {"status": "skipped", "context": "deploy"},
                    {"status": "pending", "context": "test"},
                    {"status": "error", "context": "typecheck"},
                    {"status": "failure", "context": "complexity"},
                    {"status": "queued", "context": "unknown"},
                ]
            },
        },
    )

    status = forgejo.get_pr_status(
        _BASE,
        _REPO,
        "https://git.example.test/slaid098/orch/pulls/10",
        "token",
    )

    assert status.state == "OPEN"
    assert status.mergeable == "MERGEABLE"
    assert status.head_sha == "abc123"
    assert [check.state for check in status.checks] == [
        "success",
        "success",
        "success",
        "pending",
        "failure",
        "failure",
        "failure",
    ]


def test_get_pr_status_maps_conflicts_and_merges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls/10"): _pr_payload(
                head={},
                mergeable=False,
                merged=True,
                state="closed",
            ),
        },
    )

    status = forgejo.get_pr_status(
        _BASE,
        _REPO,
        "https://git.example.test/slaid098/orch/pulls/10",
        "token",
    )

    assert status.state == "MERGED"
    assert status.mergeable == "CONFLICTING"
    assert status.checks == ()


def test_get_pr_status_handles_unknown_mergeability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls/10"): _pr_payload(
                mergeable=None,
                head={"sha": "abc123"},
            ),
            ("GET", f"repos/{_REPO}/commits/abc123/status"): {"statuses": []},
        },
    )

    status = forgejo.get_pr_status(
        _BASE,
        _REPO,
        "https://git.example.test/slaid098/orch/pulls/10",
        "token",
    )

    assert status.mergeable == "UNKNOWN"
    assert status.checks == ()


def test_get_pr_status_rejects_a_bad_url() -> None:
    with pytest.raises(ForgejoError, match="pull-request number"):
        forgejo.get_pr_status(
            _BASE,
            _REPO,
            "https://git.example.test/slaid098/orch/pulls/not-a-number",
            "token",
        )


def test_merge_pr_squashes_the_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls/10"): _pr_payload(
                merged=True,
                state="closed",
            ),
            ("POST", f"repos/{_REPO}/pulls/10/merge"): {"merged": True},
        },
    )

    assert forgejo.merge_pr(
        _BASE,
        _REPO,
        "https://git.example.test/slaid098/orch/pulls/10",
        "token",
    ) == "mergesha"
    post = next(item for item in api.requests if item["method"] == "POST")
    assert post["body"] == {
        "Do": "squash",
        "delete_branch_after_merge": True,
        "head_commit_id": "abc123",
    }


def test_merge_pr_reports_a_refused_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls/10"): _pr_payload(merged=False),
            ("POST", f"repos/{_REPO}/pulls/10/merge"): {
                "merged": False,
                "message": "checks failed",
            },
        },
    )

    with pytest.raises(ForgejoError, match="checks failed"):
        forgejo.merge_pr(
            _BASE,
            _REPO,
            "https://git.example.test/slaid098/orch/pulls/10",
            "token",
        )


def test_merge_pr_recovers_when_the_api_reports_success_after_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls/10"): _pr_payload(
                merged=True,
                state="closed",
            ),
            ("POST", f"repos/{_REPO}/pulls/10/merge"): _http_error(500, "boom"),
        },
    )

    assert forgejo.merge_pr(
        _BASE,
        _REPO,
        "https://git.example.test/slaid098/orch/pulls/10",
        "token",
    ) == "mergesha"


def test_merge_pr_reraises_when_the_pull_request_is_still_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls/10"): _pr_payload(merged=False),
            ("POST", f"repos/{_REPO}/pulls/10/merge"): _http_error(500, "boom"),
        },
    )

    with pytest.raises(ForgejoError, match="boom"):
        forgejo.merge_pr(
            _BASE,
            _REPO,
            "https://git.example.test/slaid098/orch/pulls/10",
            "token",
        )


def test_merge_pr_requires_the_final_state_to_be_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/pulls/10"): _pr_payload(merged=False),
            ("POST", f"repos/{_REPO}/pulls/10/merge"): {"merged": True},
        },
    )

    with pytest.raises(ForgejoError, match="did not merge"):
        forgejo.merge_pr(
            _BASE,
            _REPO,
            "https://git.example.test/slaid098/orch/pulls/10",
            "token",
        )


def test_delete_branch_encodes_the_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _use(
        monkeypatch,
        {("DELETE", f"repos/{_REPO}/branches/feature%2Fx"): b""},
    )

    forgejo.delete_branch(_BASE, _REPO, "feature/x", "token")

    assert api.requests[0]["path"].endswith("feature%2Fx")


def test_delete_branch_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(
        monkeypatch,
        {
            ("DELETE", f"repos/{_REPO}/branches/gone"): _http_error(404, "missing"),
        },
    )

    forgejo.delete_branch(_BASE, _REPO, "gone", "token")


def test_request_reports_a_json_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}"): _http_error(
                403,
                '{"message": "forbidden"}',
            )
        },
    )

    with pytest.raises(ForgejoError, match="forbidden"):
        forgejo.check_repo(_BASE, _REPO, "token")


def test_request_reports_a_plain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(
        monkeypatch,
        {("GET", f"repos/{_REPO}"): _http_error(500, "server exploded")},
    )

    with pytest.raises(ForgejoError, match="server exploded"):
        forgejo.check_repo(_BASE, _REPO, "token")


def test_request_reports_a_list_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, {("GET", f"repos/{_REPO}"): _http_error(500, "[]")})

    with pytest.raises(ForgejoError, match=r"\[\]"):
        forgejo.check_repo(_BASE, _REPO, "token")


def test_request_reports_a_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, {("GET", f"repos/{_REPO}"): URLError("offline")})

    with pytest.raises(ForgejoError, match="Could not reach"):
        forgejo.check_repo(_BASE, _REPO, "token")


def test_request_reports_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, {("GET", f"repos/{_REPO}"): b"{broken"})

    with pytest.raises(ForgejoError, match="Could not parse"):
        forgejo.check_repo(_BASE, _REPO, "token")


def test_request_accepts_an_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, {("GET", "empty"): b""})

    assert forgejo._request(_BASE, "token", "empty") is None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://git.example.test",
        "https://user:pass@git.example.test",
        "not-a-url",
    ],
)
def test_request_rejects_an_invalid_base_url(url: str) -> None:
    with pytest.raises(ForgejoError, match="Invalid Forgejo URL|credentials"):
        forgejo._request(url, "token", "user")


def test_get_issue_rejects_a_non_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, {("GET", f"repos/{_REPO}/issues/7"): []})

    with pytest.raises(ForgejoError, match="Could not parse"):
        forgejo.get_issue(_BASE, _REPO, 7, "token")


def test_paged_walks_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    first = [_issue_payload(number) for number in range(1, 51)]
    second = [_issue_payload(51)]

    def page(query: dict[str, list[str]], _body: object) -> list[dict[str, object]]:
        return first if query["page"] == ["1"] else second

    _use(monkeypatch, {("GET", f"repos/{_REPO}/issues"): page})

    issues = forgejo.list_ready_issues(_BASE, _REPO, "agent:queued", "token")

    assert len(issues) == 51
    assert issues[-1].number == 51


def test_list_blockers_maps_fields_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/issues/7/dependencies"): [
                _issue_payload(3, state="closed"),
                _issue_payload(4),
                _issue_payload(3, state="closed"),
            ]
        },
    )

    blockers = forgejo.list_blockers(_BASE, _REPO, 7, "token")

    assert [blocker.number for blocker in blockers] == [3, 4]
    assert blockers[0].state == "CLOSED"
    assert blockers[1].state == "OPEN"
    assert blockers[0].author == "author"
    assert blockers[0].url.endswith("/issues/3")


def test_list_blockers_walks_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    first = [_issue_payload(number) for number in range(1, 51)]
    second = [_issue_payload(51, state="closed")]

    def page(query: dict[str, list[str]], _body: object) -> list[dict[str, object]]:
        return first if query["page"] == ["1"] else second

    api = _use(
        monkeypatch,
        {("GET", f"repos/{_REPO}/issues/7/dependencies"): page},
    )

    blockers = forgejo.list_blockers(_BASE, _REPO, 7, "token")

    assert len(blockers) == 51
    assert blockers[-1].number == 51
    assert api.requests[0]["query"]["limit"] == ["50"]
    assert api.requests[1]["query"]["page"] == ["2"]


def test_list_blockers_treats_a_404_as_no_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/issues/7/dependencies"): _http_error(
                404,
                "not found",
            )
        },
    )

    assert forgejo.list_blockers(_BASE, _REPO, 7, "token") == []


def test_list_blockers_fails_closed_when_a_network_error_mentions_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/issues/7/dependencies"): URLError(
                "Tunnel connection failed: 404 Not Found"
            )
        },
    )

    with pytest.raises(ForgejoError, match="Could not reach"):
        forgejo.list_blockers(_BASE, _REPO, 7, "token")


def test_list_blockers_fails_closed_when_an_untagged_error_mentions_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise ForgejoError("Forgejo API GET x failed with 404: gone")

    monkeypatch.setattr(forgejo, "_request", fail)

    with pytest.raises(ForgejoError, match="404"):
        forgejo.list_blockers(_BASE, _REPO, 7, "token")


def test_list_blockers_reraises_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/issues/7/dependencies"): _http_error(
                500,
                "boom",
            )
        },
    )

    with pytest.raises(ForgejoError, match="boom"):
        forgejo.list_blockers(_BASE, _REPO, 7, "token")


def test_list_blockers_fails_when_a_later_page_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = [_issue_payload(number) for number in range(1, 51)]

    def page(query: dict[str, list[str]], _body: object) -> list[dict[str, object]]:
        if query["page"] == ["1"]:
            return first
        raise _http_error(404, "missing")

    _use(
        monkeypatch,
        {("GET", f"repos/{_REPO}/issues/7/dependencies"): page},
    )

    with pytest.raises(ForgejoError, match="404"):
        forgejo.list_blockers(_BASE, _REPO, 7, "token")


def test_list_blockers_rejects_an_invalid_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("GET", f"repos/{_REPO}/issues/7/dependencies"): [
                {"title": "missing a number"},
            ]
        },
    )

    with pytest.raises(ForgejoError, match="Could not parse"):
        forgejo.list_blockers(_BASE, _REPO, 7, "token")


def test_add_dependency_posts_the_issue_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _use(
        monkeypatch,
        {("POST", f"repos/{_REPO}/issues/7/dependencies"): {}},
    )

    forgejo.add_dependency(_BASE, _REPO, 7, 3, "token")

    assert api.requests[0]["method"] == "POST"
    assert api.requests[0]["body"] == {
        "index": 3,
        "owner": "slaid098",
        "repo": "orch",
    }
    assert api.requests[0]["headers"]["Authorization"] == "token token"


def test_add_dependency_surfaces_api_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(
        monkeypatch,
        {
            ("POST", f"repos/{_REPO}/issues/7/dependencies"): _http_error(
                422,
                "cannot depend on itself",
            )
        },
    )

    with pytest.raises(ForgejoError, match="422"):
        forgejo.add_dependency(_BASE, _REPO, 7, 7, "token")


@pytest.mark.parametrize(
    "repo",
    ["orch", "slaid098/", "/orch", "slaid098/orch/extra"],
)
def test_add_dependency_rejects_an_invalid_repository(
    monkeypatch: pytest.MonkeyPatch,
    repo: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the API must not be called")

    monkeypatch.setattr(forgejo, "_request", fail)

    with pytest.raises(ForgejoError, match="Invalid repository"):
        forgejo.add_dependency(_BASE, repo, 7, 3, "token")

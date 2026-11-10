from __future__ import annotations

import json
import subprocess

import pytest

from orch.services.github import service as github


def _proc(
    stdout: str = "",
    stderr: str = "",
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], code, stdout, stderr)


def test_resolve_token_prefers_configured_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("gh must not run")

    monkeypatch.setattr(github.subprocess, "run", fail)

    assert github.resolve_token("slaid098", "configured") == "configured"


def test_resolve_token_uses_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _proc("token\n")

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    assert github.resolve_token("slaid098", None) == "token"
    assert calls == [["gh", "auth", "token", "--user", "slaid098"]]


@pytest.mark.parametrize(
    "result",
    [
        _proc(stderr="not logged in", code=1),
        _proc(""),
    ],
)
def test_resolve_token_rejects_missing_auth(
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess[str],
) -> None:
    monkeypatch.setattr(github.subprocess, "run", lambda *_args, **_kwargs: result)

    with pytest.raises(github.GitHubError, match="Could not resolve"):
        github.resolve_token("slaid098", None)


def test_resolve_token_cannot_start_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("missing")

    monkeypatch.setattr(github.subprocess, "run", fail)

    with pytest.raises(github.GitHubError, match="Could not execute"):
        github.resolve_token("slaid098", None)


def test_ensure_user_accepts_matching_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "slaid098\n")

    github.ensure_user("token", "slaid098")


def test_ensure_user_rejects_another_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "someone\n")

    with pytest.raises(github.GitHubError, match="belongs to"):
        github.ensure_user("token", "slaid098")


def test_check_repo_reads_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(args: list[str], _token: str, **_kwargs: object) -> str:
        calls.append(args)
        return "{}"

    monkeypatch.setattr(github, "_run_gh", run)

    github.check_repo("slaid098/orch", "token")

    assert calls[0][:3] == ["repo", "view", "slaid098/orch"]


def test_open_pr_issue_numbers_lists_open_prs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "number": 1,
            "url": "https://example.test/pr/1",
            "headRefName": "orch/issue-5",
            "body": "",
            "isCrossRepository": False,
        },
        {
            "number": 2,
            "url": "https://example.test/pr/2",
            "headRefName": "feat/x",
            "body": "Closes #6",
            "isCrossRepository": False,
        },
        {
            "number": 3,
            "url": "https://example.test/pr/3",
            "headRefName": "orch/issue-7",
            "body": "",
            "isCrossRepository": True,
        },
    ]
    calls: list[list[str]] = []

    def run(args: list[str], _token: str, **_kwargs: object) -> str:
        calls.append(args)
        return json.dumps(payload)

    monkeypatch.setattr(github, "_run_gh", run)

    assert github.open_pr_issue_numbers("slaid098/orch", {5, 6, 7, 8}, "token") == {
        5,
        6,
    }
    assert len(calls) == 1


def test_open_pr_issue_numbers_skips_an_empty_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda *_args, **_kwargs: pytest.fail("an empty request must not call gh"),
    )

    assert github.open_pr_issue_numbers("slaid098/orch", set(), "token") == set()


def test_ensure_labels_creates_each_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda args, _token: calls.append(args) or "",
    )

    github.ensure_labels(
        "slaid098/orch",
        "token",
        [("agent:queued", "0e8a16", "Queued"), ("agent:running", "fbca04", "Run")],
    )

    assert [call[2] for call in calls] == ["agent:queued", "agent:running"]
    assert "--force" in calls[0]


def test_list_ready_issues_sorts_by_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "number": 2,
            "title": "fix(worker): retry",
            "body": "Body",
            "url": "https://example.test/2",
            "author": {"login": "slaid098"},
            "labels": [{"name": "agent:queued"}],
        },
        {
            "number": 1,
            "title": "feat(cli): flag",
            "body": "Body",
            "url": "https://example.test/1",
            "author": {},
            "labels": [],
        },
    ]
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: json.dumps(payload))

    issues = github.list_ready_issues("slaid098/orch", "agent:queued", "token")

    assert [issue.number for issue in issues] == [1, 2]
    assert issues[1].author == "slaid098"


def test_list_ready_issues_rejects_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "not-json")

    with pytest.raises(github.GitHubError, match="parse"):
        github.list_ready_issues("slaid098/orch", "agent:queued", "token")


def test_list_ready_issues_accepts_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "")

    assert github.list_ready_issues("slaid098/orch", "agent:queued", "token") == []


def test_get_issue_parses_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda *_args, **_kwargs: json.dumps(
            {
                "number": 7,
                "title": "feat(cli): add flag",
                "body": "Body",
                "url": "https://example.test/7",
                "author": {"login": "slaid098"},
                "labels": [{"name": "agent:queued"}],
            }
        ),
    )

    issue = github.get_issue("slaid098/orch", 7, "token")

    assert issue.number == 7
    assert issue.has_label("agent:queued")


def test_get_issue_rejects_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "{")

    with pytest.raises(github.GitHubError, match="parse"):
        github.get_issue("slaid098/orch", 7, "token")


def test_edit_labels_builds_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda args, _token: calls.append(args) or "",
    )

    github.edit_labels(
        "slaid098/orch",
        7,
        "token",
        add=("agent:running",),
        remove=("agent:queued", "agent:needs-human"),
    )

    assert calls[0][:4] == ["issue", "edit", "7", "--repo"]
    assert calls[0].count("--add-label") == 1
    assert calls[0].count("--remove-label") == 2


def test_edit_labels_does_nothing_for_empty_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("gh must not run")

    monkeypatch.setattr(github, "_run_gh", fail)
    github.edit_labels("slaid098/orch", 7, "token")


def test_comment_issue_uses_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], str | None]] = []
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda args, _token, input_text=None: calls.append((args, input_text)) or "",
    )

    github.comment_issue("slaid098/orch", 7, "token", "body")

    assert calls[0][0][-2:] == ["--body-file", "-"]
    assert calls[0][1] == "body"


def test_find_open_pr_returns_matching_url(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        {"number": 1, "url": "https://example.test/pr/1", "headRefName": "other"},
        {"number": 2, "url": "https://example.test/pr/2", "headRefName": "branch"},
    ]
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: json.dumps(payload))

    assert (
        github.find_open_pr("slaid098/orch", "branch", "token")
        == "https://example.test/pr/2"
    )


def test_find_open_pr_returns_none_without_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "")

    assert github.find_open_pr("slaid098/orch", "branch", "token") is None


def test_find_open_pr_rejects_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "[")

    with pytest.raises(github.GitHubError, match="parse"):
        github.find_open_pr("slaid098/orch", "branch", "token")


def test_find_open_pr_for_issue_prefers_the_expected_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github,
        "find_open_pr",
        lambda *_args: "https://example.test/pr/7",
    )

    ref = github.find_open_pr_for_issue(
        "slaid098/orch",
        7,
        "token",
        preferred_head="orch/issue-7",
    )

    assert ref is not None
    assert ref.number == 7
    assert ref.url == "https://example.test/pr/7"
    assert ref.head_ref == "orch/issue-7"


@pytest.mark.parametrize(
    "item",
    [
        {
            "number": 7,
            "url": "https://example.test/pr/7",
            "headRefName": "orch/issue-7",
            "body": "<!-- orch:issue=7 -->",
            "isCrossRepository": False,
        },
        {
            "number": 7,
            "url": "https://example.test/pr/7",
            "headRefName": "feature/change",
            "body": "Closes #7",
            "isCrossRepository": False,
        },
        {
            "number": 7,
            "url": "https://example.test/pr/7",
            "headRefName": "fix/pipeline/issue-7-reliable-retry-path",
            "body": "",
            "isCrossRepository": False,
        },
    ],
)
def test_find_open_pr_for_issue_matches_known_markers(
    monkeypatch: pytest.MonkeyPatch,
    item: dict[str, object],
) -> None:
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda *_args, **_kwargs: json.dumps([item]),
    )

    ref = github.find_open_pr_for_issue("slaid098/orch", 7, "token")

    assert ref is not None
    assert ref.number == 7
    assert ref.head_ref == item["headRefName"]


def test_find_open_pr_for_issue_ignores_cross_repository_prs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "number": 7,
            "url": "https://example.test/pr/7",
            "headRefName": "orch/issue-7",
            "body": "Closes #7",
            "isCrossRepository": True,
        }
    ]
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    assert github.find_open_pr_for_issue("slaid098/orch", 7, "token") is None


def test_find_open_pr_for_issue_rejects_multiple_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "number": 7,
            "url": "https://example.test/pr/7",
            "headRefName": "orch/issue-7",
            "body": "Closes #7",
            "isCrossRepository": False,
        },
        {
            "number": 8,
            "url": "https://example.test/pr/8",
            "headRefName": "orch/issue-7-old",
            "body": "<!-- orch:issue=7 -->",
            "isCrossRepository": False,
        },
    ]
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    with pytest.raises(github.GitHubError, match="Multiple open pull requests"):
        github.find_open_pr_for_issue("slaid098/orch", 7, "token")


def test_find_open_pr_for_issue_returns_none_without_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "[]")

    assert github.find_open_pr_for_issue("slaid098/orch", 7, "token") is None


def test_delete_branch_ignores_missing_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise github.GitHubError("missing")

    monkeypatch.setattr(github, "_run_gh", fail)

    github.delete_branch("slaid098/orch", "feature/branch", "token")


def test_delete_branch_removes_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda args, _token: calls.append(args) or "",
    )

    github.delete_branch("slaid098/orch", "feature/branch", "token")

    assert "DELETE" in calls[0]
    assert "feature%2Fbranch" in calls[0][-1]


def test_create_pr_returns_existing_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("pr create must not run")

    monkeypatch.setattr(github, "_run_gh", fail)
    monkeypatch.setattr(
        github,
        "find_open_pr",
        lambda *_args: "https://example.test/pr/1",
    )

    result = github.create_pr(
        "slaid098/orch",
        "token",
        head="branch",
        base="main",
        title="feat(cli): flag",
        body="body",
    )

    assert result == "https://example.test/pr/1"


def test_create_pr_uses_last_output_line(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], str | None]] = []
    monkeypatch.setattr(github, "find_open_pr", lambda *_args: None)
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda args, _token, input_text=None: calls.append((args, input_text))
        or "warning\nhttps://example.test/pr/2\n",
    )

    result = github.create_pr(
        "slaid098/orch",
        "token",
        head="branch",
        base="main",
        title="feat(cli): flag",
        body="body",
    )

    assert result == "https://example.test/pr/2"
    assert calls[0][1] == "body"


def test_create_pr_rejects_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "find_open_pr", lambda *_args: None)
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "")

    with pytest.raises(github.GitHubError, match="no pull request URL"):
        github.create_pr(
            "slaid098/orch",
            "token",
            head="branch",
            base="main",
            title="feat(cli): flag",
            body="body",
        )


def test_run_gh_passes_token_and_input(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return _proc("ok")

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    result = github._run_gh(
        ["api", "user"],
        "secret",
        input_text="body",
        timeout=5,
    )

    assert result == "ok"
    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["GH_TOKEN"] == "secret"
    assert calls[0]["input"] == "body"
    assert calls[0]["timeout"] == 5


def test_run_gh_rejects_failed_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        github.subprocess,
        "run",
        lambda *_args, **_kwargs: _proc(stdout="", stderr="boom\n", code=1),
    )

    with pytest.raises(github.GitHubError, match="boom"):
        github._run_gh(["api", "user"], "secret")


def test_run_gh_cannot_start_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("missing")

    monkeypatch.setattr(github.subprocess, "run", fail)

    with pytest.raises(github.GitHubError, match="Could not execute"):
        github._run_gh(["api", "user"], "secret")


def test_run_gh_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["gh"], timeout=1)

    monkeypatch.setattr(github.subprocess, "run", fail)

    with pytest.raises(github.GitHubError, match="timed out"):
        github._run_gh(["api", "user"], "secret", timeout=1)


def test_list_issue_comments_parses_and_encodes_since(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    payload = [
        {
            "id": 10,
            "user": {"login": "slaid098"},
            "body": "Hello",
            "created_at": "2026-11-02T10:00:00Z",
            "html_url": "https://example.test/comment/10",
        }
    ]
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda args, _token: calls.append(args) or json.dumps(payload),
    )

    comments = github.list_issue_comments(
        "slaid098/orch",
        7,
        "token",
        since="2026-11-02T09:00:00Z",
    )

    assert comments[0].id == 10
    assert comments[0].author == "slaid098"
    assert comments[0].body == "Hello"
    assert "since=2026-11-02T09%3A00%3A00Z" in calls[0][1]


def test_list_issue_comments_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    first_page = [
        {
            "id": index,
            "user": {"login": "slaid098"},
            "body": "Hello",
            "created_at": "2026-11-02T10:00:00Z",
            "html_url": f"https://example.test/comment/{index}",
        }
        for index in range(100)
    ]
    second_page = [
        {
            "id": 100,
            "user": {"login": "slaid098"},
            "body": "Newest",
            "created_at": "2026-11-02T11:00:00Z",
            "html_url": "https://example.test/comment/100",
        }
    ]

    def fake_run(args: list[str], _token: str) -> str:
        calls.append(args)
        return json.dumps(first_page if len(calls) == 1 else second_page)

    monkeypatch.setattr(github, "_run_gh", fake_run)

    comments = github.list_issue_comments("slaid098/orch", 7, "token")

    assert len(comments) == 101
    assert comments[-1].body == "Newest"
    assert "page=1" in calls[0][1]
    assert "page=2" in calls[1][1]


def test_list_issue_comments_rejects_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "{")

    with pytest.raises(github.GitHubError, match="comments"):
        github.list_issue_comments("slaid098/orch", 7, "token")


def test_issue_is_open_parses_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: '{"state":"OPEN"}')
    assert github.issue_is_open("slaid098/orch", 7, "token")

    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: '{"state":"CLOSED"}')
    assert not github.issue_is_open("slaid098/orch", 7, "token")


def test_issue_is_open_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "{")

    with pytest.raises(github.GitHubError, match="state"):
        github.issue_is_open("slaid098/orch", 7, "token")


def test_comment_bot_issue_marks_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[str] = []
    monkeypatch.setattr(
        github,
        "comment_issue",
        lambda _repo, _number, _token, body: bodies.append(body),
    )

    github.comment_bot_issue("slaid098/orch", 7, "token", "hello")

    assert bodies == [f"hello\n\n{github.BOT_MARKER}"]
    assert github.mark_bot_body("x").endswith(github.BOT_MARKER)


def test_issue_from_json_reads_state() -> None:
    issue = github.Issue.from_json(
        {
            "number": 7,
            "title": "feat(cli): add flag",
            "body": "Body",
            "url": "https://example.test/7",
            "author": {"login": "slaid098"},
            "labels": [],
            "state": "closed",
        }
    )

    assert issue.state == "CLOSED"


def test_get_pr_status_parses_checks_and_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "number": 12,
        "url": "https://example.test/pr/12",
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "headRefOid": "abc",
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "pytest",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "detailsUrl": "https://example.test/check/1",
            },
            {
                "__typename": "StatusContext",
                "context": "lint",
                "state": "FAILURE",
                "targetUrl": "https://example.test/check/2",
            },
            {
                "__typename": "CheckRun",
                "name": "coverage",
                "status": "IN_PROGRESS",
                "conclusion": None,
            },
        ],
    }
    calls: list[list[str]] = []
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda args, _token: calls.append(args) or json.dumps(payload),
    )

    status = github.get_pr_status(
        "slaid098/orch",
        "https://example.test/pr/12",
        "token",
    )

    assert status.number == 12
    assert status.url == "https://example.test/pr/12"
    assert status.mergeable == "MERGEABLE"
    assert status.head_sha == "abc"
    assert [check.state for check in status.checks] == [
        "success",
        "failure",
        "pending",
    ]
    assert status.failed
    assert status.pending
    assert calls[0][:2] == ["pr", "view"]


def test_get_pr_status_treats_unknown_conclusions_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "number": 12,
        "url": "https://example.test/pr/12",
        "state": "OPEN",
        "mergeable": "UNKNOWN",
        "headRefOid": "abc",
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "strange",
                "status": "COMPLETED",
                "conclusion": "SOMETHING_NEW",
            }
        ],
    }
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    status = github.get_pr_status(
        "slaid098/orch",
        "https://example.test/pr/12",
        "token",
    )

    assert status.failed


def test_get_pr_status_rejects_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "not-json")

    with pytest.raises(github.GitHubError, match="parse"):
        github.get_pr_status(
            "slaid098/orch",
            "https://example.test/pr/12",
            "token",
        )


def test_merge_pr_uses_squash_and_returns_merge_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda args, _token, **_kwargs: calls.append(args) or (
            "abc123\n" if args[:2] == ["pr", "view"] else ""
        ),
    )

    commit = github.merge_pr(
        "slaid098/orch",
        "https://example.test/pr/12",
        "token",
    )

    assert commit == "abc123"
    assert calls[0][:3] == ["pr", "merge", "12"]
    assert "--squash" in calls[0]
    assert "--delete-branch" in calls[0]
    assert calls[1][:3] == ["pr", "view", "12"]


def test_merge_pr_keeps_merge_when_commit_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(args: list[str], _token: str, **_kwargs: object) -> str:
        if args[:2] == ["pr", "view"]:
            raise github.GitHubError("lookup failed")
        return ""

    monkeypatch.setattr(github, "_run_gh", fake)

    commit = github.merge_pr(
        "slaid098/orch",
        "https://example.test/pr/12",
        "token",
    )

    assert commit == ""


def test_merge_pr_rejects_invalid_url(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(github.GitHubError, match="number"):
        github.merge_pr("slaid098/orch", "https://example.test/pull/", "token")


def _blocker_payload(number: int, *, state: str = "open") -> dict[str, object]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "Body",
        "html_url": f"https://example.test/{number}",
        "user": {"login": "slaid098"},
        "labels": [{"name": "agent:queued"}],
        "state": state,
    }


def test_list_blockers_maps_fields_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    payload = [
        _blocker_payload(3, state="closed"),
        _blocker_payload(4),
        _blocker_payload(3, state="closed"),
    ]
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda args, _token: calls.append(args) or json.dumps(payload),
    )

    blockers = github.list_blockers("slaid098/orch", 7, "token")

    assert [blocker.number for blocker in blockers] == [3, 4]
    assert blockers[0].state == "CLOSED"
    assert blockers[1].state == "OPEN"
    assert blockers[0].author == "slaid098"
    assert blockers[0].url == "https://example.test/3"
    assert blockers[0].labels == ("agent:queued",)
    assert calls[0] == [
        "api",
        "repos/slaid098/orch/issues/7/dependencies/blocked_by?per_page=100&page=1",
    ]


def test_list_blockers_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    first_page = [_blocker_payload(number) for number in range(100)]
    second_page = [_blocker_payload(100, state="closed")]

    def run(args: list[str], _token: str) -> str:
        calls.append(args)
        return json.dumps(first_page if len(calls) == 1 else second_page)

    monkeypatch.setattr(github, "_run_gh", run)

    blockers = github.list_blockers("slaid098/orch", 7, "token")

    assert len(blockers) == 101
    assert blockers[-1].state == "CLOSED"
    assert calls[0][1].endswith("page=1")
    assert calls[1][1].endswith("page=2")


def test_list_blockers_treats_a_404_as_no_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> str:
        raise github.GitHubError("gh: Not Found (HTTP 404)")

    monkeypatch.setattr(github, "_run_gh", fail)

    assert github.list_blockers("slaid098/orch", 7, "token") == []


def test_list_blockers_treats_a_plain_not_found_error_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> str:
        raise github.GitHubError("Not Found")

    monkeypatch.setattr(github, "_run_gh", fail)

    with pytest.raises(github.GitHubError, match="Not Found"):
        github.list_blockers("slaid098/orch", 7, "token")


def test_list_blockers_fails_closed_when_a_network_error_mentions_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> str:
        raise github.GitHubError("Tunnel connection failed: 404 Not Found")

    monkeypatch.setattr(github, "_run_gh", fail)

    with pytest.raises(github.GitHubError, match="Tunnel"):
        github.list_blockers("slaid098/orch", 7, "token")


def test_list_blockers_reraises_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> str:
        raise github.GitHubError("boom")

    monkeypatch.setattr(github, "_run_gh", fail)

    with pytest.raises(github.GitHubError, match="boom"):
        github.list_blockers("slaid098/orch", 7, "token")


def test_list_blockers_fails_when_a_later_page_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page = [_blocker_payload(number) for number in range(100)]

    def run(args: list[str], _token: str) -> str:
        if args[1].endswith("page=1"):
            return json.dumps(first_page)
        raise github.GitHubError("gh: Not Found (HTTP 404)")

    monkeypatch.setattr(github, "_run_gh", run)

    with pytest.raises(github.GitHubError, match="404"):
        github.list_blockers("slaid098/orch", 7, "token")


def test_list_blockers_accepts_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "")

    assert github.list_blockers("slaid098/orch", 7, "token") == []


def test_list_blockers_rejects_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "{")

    with pytest.raises(github.GitHubError, match="dependencies"):
        github.list_blockers("slaid098/orch", 7, "token")


def test_list_blockers_rejects_a_non_list_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "_run_gh", lambda *_args, **_kwargs: "{}")

    with pytest.raises(github.GitHubError, match="dependencies"):
        github.list_blockers("slaid098/orch", 7, "token")


def test_list_blockers_rejects_an_invalid_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github,
        "_run_gh",
        lambda *_args, **_kwargs: json.dumps([{"title": "missing a number"}]),
    )

    with pytest.raises(github.GitHubError, match="Could not parse"):
        github.list_blockers("slaid098/orch", 7, "token")

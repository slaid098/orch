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

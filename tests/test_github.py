from __future__ import annotations

import subprocess

import pytest

from orch.schemas.github.types import Issue
from orch.services.github import service as github


def test_issue_from_json() -> None:
    issue = Issue.from_json(
        {
            "number": 12,
            "title": "Test issue",
            "body": "Body",
            "url": "https://example.test/12",
            "author": {"login": "slaid098"},
            "labels": [{"name": "agent:queued"}, {"name": "bug"}],
        }
    )

    assert issue.number == 12
    assert issue.author == "slaid098"
    assert issue.labels == ("agent:queued", "bug")
    assert issue.has_label("agent:queued")
    assert not issue.has_label("agent:running")


def test_run_gh_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["gh"], timeout=1)

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    with pytest.raises(github.GitHubError):
        github._run_gh(["api", "repos/slaid098/orch"], "test-token", timeout=1)

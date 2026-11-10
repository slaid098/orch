"""Tests for the forge provider dispatcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orch.constants.standards import BOT_MARKER
from orch.exceptions.forgejo import ForgejoError
from orch.schemas.config.types import Config, LabelsConfig, RepoConfig, WorkerConfig
from orch.services import forge
from orch.services.forge import service as forge_service


def _config(tmp_path: Path, name: str) -> Config:
    return Config(
        repo=RepoConfig(
            owner="slaid098",
            name="orch",
            path=tmp_path,
            forge=name,
            url="https://git.example.test" if name == "forgejo" else "",
        ),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
        gh_token="gh-token",
        forgejo_token="forgejo-token",
    )


def _recorder(module: str, calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]]):
    def record(*args: Any, **kwargs: Any) -> str:
        calls.append((module, args, kwargs))
        return f"{module}-result"

    return record


_CASES = [
    ("resolve_token", (), {}),
    ("ensure_user", ("token",), {}),
    ("check_repo", ("token",), {}),
    ("ensure_labels", ("token", [("a", "b", "c")]), {}),
    ("list_ready_issues", ("agent:queued", "token"), {}),
    ("get_issue", (7, "token"), {}),
    ("list_blockers", (7, "token"), {}),
    ("issue_is_open", (7, "token"), {}),
    (
        "edit_labels",
        (7, "token"),
        {"add": ("a",), "remove": ("b",)},
    ),
    ("comment_issue", (7, "token", "body"), {}),
    ("list_issue_comments", (7, "token"), {"since": "2026-11-10T00:00:00Z"}),
    ("find_open_pr", ("head", "token"), {}),
    (
        "find_open_pr_for_issue",
        (7, "token"),
        {"preferred_head": "head"},
    ),
    ("open_pr_issue_numbers", ({7}, "token"), {}),
    (
        "create_pr",
        ("token",),
        {"head": "head", "base": "main", "title": "Title", "body": "Body"},
    ),
    ("get_pr_status", ("https://example.test/pull/1", "token"), {}),
    ("merge_pr", ("https://example.test/pull/1", "token"), {}),
    ("delete_branch", ("branch", "token"), {}),
]


@pytest.mark.parametrize(("name", "args", "kwargs"), _CASES)
@pytest.mark.parametrize(("provider", "expected"), [("github", "github"), ("forgejo", "forgejo")])
def test_dispatcher_routes_to_the_configured_forge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    provider: str,
    expected: str,
) -> None:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(forge_service.github, name, _recorder("github", calls))
    monkeypatch.setattr(forge_service.forgejo, name, _recorder("forgejo", calls))

    getattr(forge, name)(_config(tmp_path, provider), *args, **kwargs)

    assert [module for module, _, _ in calls] == [expected]


def test_comment_bot_issue_marks_the_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(forge_service.github, "comment_issue", _recorder("github", calls))

    forge.comment_bot_issue(_config(tmp_path, "github"), 7, "token", "hello")

    assert calls[0][1][3] == f"hello\n\n{BOT_MARKER}"


def test_mark_bot_body_uses_the_shared_marker() -> None:
    assert forge.mark_bot_body("hello") == f"hello\n\n{BOT_MARKER}"


def test_missing_forgejo_token_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    config = Config(
        repo=RepoConfig(
            owner="slaid098",
            name="orch",
            path=tmp_path,
            forge="forgejo",
            url="https://git.example.test",
        ),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
    )

    with pytest.raises(ForgejoError, match="Forgejo token"):
        forge.resolve_token(config)

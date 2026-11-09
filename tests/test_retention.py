from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from orch.exceptions.github import GitHubError
from orch.schemas.config.types import Config, LabelsConfig, RepoConfig, WorkerConfig
from orch.services.retention import prune_runs
from orch.services.retention import service as retention

_SECONDS_PER_DAY = 86_400


def _config(tmp_path: Path, *, workspace_retention_days: int = 14) -> Config:
    return Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
        workspace_retention_days=workspace_retention_days,
    )


def _workspace(tmp_path: Path, issue: int = 7) -> Path:
    path = (
        tmp_path / "data" / "workspaces" / "slaid098__orch" / f"issue-{issue}"
    )
    (path / "src").mkdir(parents=True)
    (path / "src" / "main.py").write_text("x", encoding="utf-8")
    return path


def _age_tree(path: Path, days: int) -> None:
    stamp = time.time() - days * _SECONDS_PER_DAY
    for current, directories, files in os.walk(path, topdown=False):
        for name in (*directories, *files):
            os.utime(os.path.join(current, name), (stamp, stamp))
    os.utime(path, (stamp, stamp))


def _run_dir(root: Path, repo: str, issue: str, run: str) -> Path:
    path = root / "runs" / repo / issue / run
    path.mkdir(parents=True)
    (path / "events.jsonl").write_text("{}", encoding="utf-8")
    return path


def _age(path: Path, days: int) -> None:
    stamp = time.time() - days * 86_400
    os.utime(path, (stamp, stamp))


def test_prune_runs_removes_expired_runs_and_empty_parents(tmp_path: Path) -> None:
    data_dir = tmp_path / "app_data"
    expired = _run_dir(data_dir, "o__r", "issue-1", "run-old")
    fresh = _run_dir(data_dir, "o__r", "issue-2", "run-fresh")
    _age(expired, 10)

    removed = prune_runs(data_dir, 7)

    assert removed == 1
    assert not expired.exists()
    assert not (data_dir / "runs" / "o__r" / "issue-1").exists()
    assert fresh.exists()


def test_prune_runs_keeps_issue_directory_with_fresh_runs(tmp_path: Path) -> None:
    data_dir = tmp_path / "app_data"
    expired = _run_dir(data_dir, "o__r", "issue-1", "run-old")
    fresh = _run_dir(data_dir, "o__r", "issue-1", "run-fresh")
    _age(expired, 30)

    assert prune_runs(data_dir, 7) == 1
    assert (data_dir / "runs" / "o__r" / "issue-1").exists()
    assert fresh.exists()


def test_prune_runs_ignores_missing_directory(tmp_path: Path) -> None:
    assert prune_runs(tmp_path / "missing", 7) == 0


def test_prune_runs_ignores_stray_files(tmp_path: Path) -> None:
    runs = tmp_path / "app_data" / "runs"
    runs.mkdir(parents=True)
    (runs / "notes.txt").write_text("x", encoding="utf-8")

    assert prune_runs(tmp_path / "app_data", 7) == 0
    assert (runs / "notes.txt").exists()


def test_prune_workspaces_removes_an_expired_workspace_without_open_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    _age_tree(workspace, 30)
    calls: list[tuple[str, set[int], str]] = []
    monkeypatch.setattr(
        retention.github,
        "open_pr_issue_numbers",
        lambda repo, numbers, token: calls.append((repo, set(numbers), token))
        or set(),
    )

    removed = retention.prune_workspaces(_config(tmp_path), "token")

    assert removed == 1
    assert not workspace.exists()
    assert calls == [("slaid098/orch", {7}, "token")]


def test_prune_workspaces_keeps_a_workspace_with_an_open_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    _age_tree(workspace, 30)
    monkeypatch.setattr(
        retention.github,
        "open_pr_issue_numbers",
        lambda *_args: {7},
    )

    assert retention.prune_workspaces(_config(tmp_path), "token") == 0
    assert workspace.exists()


def test_prune_workspaces_keeps_a_fresh_workspace_without_a_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(
        retention.github,
        "open_pr_issue_numbers",
        lambda *_args: pytest.fail("fresh workspaces must not trigger a lookup"),
    )

    assert retention.prune_workspaces(_config(tmp_path), "token") == 0
    assert workspace.exists()


def test_prune_workspaces_keeps_a_workspace_with_an_active_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    _age_tree(workspace, 30)
    monkeypatch.setattr(
        retention.github,
        "open_pr_issue_numbers",
        lambda *_args: pytest.fail("active workspaces must not be looked up"),
    )

    removed = retention.prune_workspaces(
        _config(tmp_path),
        "token",
        active_issues={7},
    )

    assert removed == 0
    assert workspace.exists()


def test_prune_workspaces_keeps_everything_when_the_lookup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    _age_tree(workspace, 30)

    def fail(*_args: object) -> set[int]:
        raise GitHubError("offline")

    monkeypatch.setattr(retention.github, "open_pr_issue_numbers", fail)

    assert retention.prune_workspaces(_config(tmp_path), "token") == 0
    assert workspace.exists()


def test_prune_workspaces_uses_the_newest_file_in_the_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    _age_tree(workspace, 30)
    fresh = workspace / "src" / "new.py"
    fresh.write_text("new", encoding="utf-8")
    _age_tree(workspace, 30)
    os.utime(fresh, None)
    monkeypatch.setattr(
        retention.github,
        "open_pr_issue_numbers",
        lambda *_args: pytest.fail("fresh files must keep the workspace"),
    )

    assert retention.prune_workspaces(_config(tmp_path), "token") == 0
    assert workspace.exists()


def test_prune_workspaces_ignores_unrelated_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data" / "workspaces" / "slaid098__orch"
    root.mkdir(parents=True)
    (root / "notes.txt").write_text("x", encoding="utf-8")
    (root / "issue-x").mkdir()
    (root / "issue-0").mkdir()
    monkeypatch.setattr(
        retention.github,
        "open_pr_issue_numbers",
        lambda *_args: pytest.fail("no workspace candidates were expected"),
    )

    assert retention.prune_workspaces(_config(tmp_path), "token") == 0


def test_prune_workspaces_ignores_a_missing_directory(tmp_path: Path) -> None:
    assert retention.prune_workspaces(_config(tmp_path), "token") == 0


def test_prune_workspaces_can_select_multiple_issues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _workspace(tmp_path, 7)
    second = _workspace(tmp_path, 8)
    _age_tree(first, 30)
    _age_tree(second, 30)
    seen: list[set[int]] = []

    def lookup(_repo: str, numbers: set[int], _token: str) -> set[int]:
        seen.append(set(numbers))
        return {7}

    monkeypatch.setattr(retention.github, "open_pr_issue_numbers", lookup)

    assert retention.prune_workspaces(_config(tmp_path), "token") == 1
    assert first.exists()
    assert not second.exists()
    assert seen == [{7, 8}]


def test_prune_workspaces_respects_the_configured_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    _age_tree(workspace, 10)
    monkeypatch.setattr(
        retention.github,
        "open_pr_issue_numbers",
        lambda *_args: pytest.fail("ten-day workspace is fresh at 14 days"),
    )

    assert (
        retention.prune_workspaces(
            _config(tmp_path, workspace_retention_days=14),
            "token",
        )
        == 0
    )
    assert workspace.exists()


def test_latest_mtime_keeps_a_workspace_when_it_cannot_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)

    def fail(_path: Path) -> os.stat_result:
        raise OSError("unreadable")

    monkeypatch.setattr(retention.Path, "stat", fail)

    assert retention._latest_mtime(workspace) == float("inf")

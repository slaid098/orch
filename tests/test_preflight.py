"""Tests for the startup preflight checks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.exceptions.docker import DockerError
from orch.exceptions.github import GitHubError
from orch.schemas.config.types import (
    Config,
    LabelsConfig,
    RepoConfig,
    ReviewerConfig,
    WorkerConfig,
)
from orch.services.preflight import service as preflight


def _config(
    tmp_path: Path,
    *,
    reviewer: bool = True,
    min_free_disk_gb: int = 1,
) -> Config:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=repo),
        labels=LabelsConfig(),
        worker=WorkerConfig(image="worker:latest", test_commands=("pytest",)),
        data_dir=tmp_path / "data",
        min_free_disk_gb=min_free_disk_gb,
        reviewer=ReviewerConfig(enabled=reviewer, image="reviewer:latest"),
    )


class _Connection:
    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def _pass_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    images: set[str] | None = None,
    model_ok: bool = True,
    github_error: Exception | None = None,
    base_branch: bool = True,
    free_bytes: int = 10 * 1024**3,
) -> None:
    available = {"worker:latest", "reviewer:latest"} if images is None else images

    monkeypatch.setattr(preflight.docker, "daemon_available", lambda: True)
    monkeypatch.setattr(
        preflight.docker,
        "image_exists",
        lambda image: image in available,
    )
    monkeypatch.setattr(
        preflight,
        "_base_branch_exists",
        lambda _config: base_branch,
    )
    monkeypatch.setattr(
        preflight.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=free_bytes),
    )

    if github_error is None:
        monkeypatch.setattr(
            preflight.github,
            "resolve_token",
            lambda *_args: "token",
        )
        monkeypatch.setattr(preflight.github, "ensure_user", lambda *_args: None)
        monkeypatch.setattr(preflight.github, "check_repo", lambda *_args: None)
    else:
        def fail(*_args: object) -> str:
            raise github_error

        monkeypatch.setattr(preflight.github, "resolve_token", fail)

    def connect(*_args: object, **_kwargs: object) -> _Connection:
        if not model_ok:
            raise OSError("connection refused")
        return _Connection()

    monkeypatch.setattr(preflight.socket, "create_connection", connect)


def test_check_passes_when_all_dependencies_are_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pass_dependencies(monkeypatch)

    assert preflight.check(_config(tmp_path)) == []


def test_check_reports_every_independent_problem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pass_dependencies(
        monkeypatch,
        model_ok=False,
        github_error=GitHubError("no token"),
        base_branch=False,
        free_bytes=0,
    )
    monkeypatch.setattr(preflight.docker, "daemon_available", lambda: False)

    problems = preflight.check(_config(tmp_path))

    assert len(problems) == 5
    assert any("Docker daemon is not reachable" in problem for problem in problems)
    assert any("Model endpoint 127.0.0.1:11434" in problem for problem in problems)
    assert any("GitHub: no token" in problem for problem in problems)
    assert any("Base branch 'main' is missing" in problem for problem in problems)
    assert any("Only 0.0 GB free" in problem for problem in problems)


def test_check_reports_missing_worker_and_reviewer_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pass_dependencies(monkeypatch, images=set())

    problems = preflight.check(_config(tmp_path))

    assert len(problems) == 2
    assert any("Worker image 'worker:latest'" in problem for problem in problems)
    assert any("Reviewer image 'reviewer:latest'" in problem for problem in problems)


def test_check_skips_the_reviewer_image_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pass_dependencies(monkeypatch, images=set())

    problems = preflight.check(_config(tmp_path, reviewer=False))

    assert len(problems) == 1
    assert "Worker image" in problems[0]


def test_check_reports_a_docker_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> bool:
        raise DockerError("daemon broke")

    _pass_dependencies(monkeypatch)
    monkeypatch.setattr(preflight.docker, "daemon_available", fail)

    problems = preflight.check(_config(tmp_path))

    assert problems == ["Docker check failed: daemon broke"]


def test_base_branch_exists_accepts_a_local_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(preflight.subprocess, "run", run)

    assert preflight._base_branch_exists(_config(tmp_path))
    assert "refs/heads/main" in calls[0]


def test_base_branch_exists_falls_back_to_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter([1, 0])

    def run(_args: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=next(results))

    monkeypatch.setattr(preflight.subprocess, "run", run)

    assert preflight._base_branch_exists(_config(tmp_path))


def test_base_branch_exists_rejects_a_missing_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )

    assert not preflight._base_branch_exists(_config(tmp_path))


def test_base_branch_exists_handles_a_missing_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise OSError("git is missing")

    monkeypatch.setattr(preflight.subprocess, "run", fail)

    assert not preflight._base_branch_exists(_config(tmp_path))

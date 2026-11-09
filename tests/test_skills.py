"""Tests for skill selection, mounting, and preflight checks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orch.config.settings import load_config
from orch.exceptions.config import ConfigError
from orch.exceptions.skills import SkillsError
from orch.schemas.config.types import (
    Config,
    LabelsConfig,
    RepoConfig,
    ReviewerConfig,
    WorkerConfig,
)
from orch.schemas.skills.types import Skill
from orch.services import skills
from orch.services.docker import service as docker


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def _config(tmp_path: Path, *names: str) -> Config:
    skill_root = tmp_path / "skills"
    for name in names:
        manifest = skill_root / name / "SKILL.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("---\nname: test\n---\n", encoding="utf-8")
    return Config(
        repo=RepoConfig(owner="o", name="n", path=_repo(tmp_path)),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
        skills_dir=skill_root,
    )


def test_load_config_reads_skills_and_skills_dir(tmp_path: Path) -> None:
    config_path = tmp_path / "orch.yaml"
    config_path.write_text(
        f"""
repo:
  owner: slaid098
  name: orch
  path: {_repo(tmp_path).as_posix()}
skills_dir: standards
worker:
  test_commands:
    - pytest
  skills:
    - code-standards
reviewer:
  skills:
    - code-standards
    - review-only
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.skills_dir == (tmp_path / "standards").resolve()
    assert config.worker.skills == ("code-standards",)
    assert config.reviewer.skills == ("code-standards", "review-only")


def test_load_config_rejects_unsafe_skill_names(tmp_path: Path) -> None:
    config_path = tmp_path / "orch.yaml"
    config_path.write_text(
        f"""
repo:
  owner: slaid098
  name: orch
  path: {_repo(tmp_path).as_posix()}
worker:
  test_commands:
    - pytest
  skills:
    - ../escape
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="lowercase skill directory name"):
        load_config(config_path)


def test_resolve_returns_named_skill_directories(tmp_path: Path) -> None:
    config = _config(tmp_path, "code-standards")

    assert skills.resolve(config, ("code-standards",)) == (
        Skill(
            name="code-standards",
            path=(tmp_path / "skills" / "code-standards").resolve(),
        ),
    )


def test_resolve_accepts_an_empty_list(tmp_path: Path) -> None:
    assert skills.resolve(_config(tmp_path), ()) == ()


def test_resolve_fails_when_the_manifest_is_missing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (config.skills_dir / "code-standards").mkdir(parents=True)

    with pytest.raises(SkillsError, match="no SKILL.md"):
        skills.resolve(config, ("code-standards",))


def test_check_reports_skills_for_both_roles(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config = Config(
        repo=config.repo,
        labels=config.labels,
        worker=WorkerConfig(test_commands=("pytest",), skills=("worker-only",)),
        data_dir=config.data_dir,
        skills_dir=config.skills_dir,
        reviewer=ReviewerConfig(skills=("reviewer-only",)),
    )

    problems = skills.check(config)

    assert len(problems) == 2
    assert problems[0].startswith("Worker: Skill 'worker-only'")
    assert problems[1].startswith("Reviewer: Skill 'reviewer-only'")


def test_start_worker_mounts_skills_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args: list[str] = []
    skill_dir = tmp_path / "skills" / "code-standards"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: test\n---\n", encoding="utf-8")
    monkeypatch.setattr(docker, "image_exists", lambda _image: True)
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: args.extend(command)
        or subprocess.CompletedProcess(["docker"], 0, "abc123\n", ""),
    )

    docker.start_worker(
        image="image:latest",
        name="worker",
        repo_path=tmp_path,
        workspace_path=tmp_path / "work",
        run_dir=tmp_path / "out",
        model="model",
        skills=(Skill("code-standards", skill_dir.resolve()),),
    )

    assert (
        f"{skill_dir.resolve()}:/home/agent/.claude/skills/code-standards:ro"
        in args
    )


def test_start_reviewer_mounts_skills_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args: list[str] = []
    skill_dir = tmp_path / "skills" / "code-standards"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: test\n---\n", encoding="utf-8")
    monkeypatch.setattr(docker, "image_exists", lambda _image: True)
    monkeypatch.setattr(docker, "_ensure_review_isolation", lambda _image: "10.0.0.2")
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: args.extend(command)
        or subprocess.CompletedProcess(["docker"], 0, "abc123\n", ""),
    )

    docker.start_reviewer(
        image="image:latest",
        name="reviewer",
        repo_path=tmp_path,
        run_dir=tmp_path / "out",
        model="model",
        skills=(Skill("code-standards", skill_dir.resolve()),),
    )

    assert (
        f"{skill_dir.resolve()}:/home/agent/.claude/skills/code-standards:ro"
        in args
    )

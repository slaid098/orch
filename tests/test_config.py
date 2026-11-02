from __future__ import annotations

from pathlib import Path

import pytest

from orch.config.settings import load_config
from orch.exceptions.config import ConfigError


def _write_config(tmp_path: Path, body: str) -> Path:
    config_path = tmp_path / "orch.yaml"
    config_path.write_text(body, encoding="utf-8")
    return config_path


def test_load_config_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    monkeypatch.setenv("GH_TOKEN", "test-token")

    config_path = _write_config(
        tmp_path,
        f"""
repo:
  owner: slaid098
  name: orch
  path: {repo_path.as_posix()}
worker:
  test_commands:
    - uv run pytest -q
data_dir: data
""",
    )

    config = load_config(config_path)

    assert config.repo.full_name == "slaid098/orch"
    assert config.repo.base_branch == "main"
    assert config.labels.queued == "agent:queued"
    assert config.labels.needs_human == "agent:needs-human"
    assert config.worker.image == "orch-agent:latest"
    assert config.worker.timeout_minutes == 90
    assert config.worker.max_attempts == 2
    assert config.worker.autocompact == "200k"
    assert config.worker.test_commands == ("uv run pytest -q",)
    assert config.max_workers == 2
    assert config.retention_days == 7
    assert config.data_dir == (tmp_path / "data").resolve()
    assert config.gh_token == "test-token"


def test_load_config_reads_pipeline_controls(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    config_path = _write_config(
        tmp_path,
        f"""
repo:
  owner: slaid098
  name: orch
  path: {repo_path.as_posix()}
labels:
  queued: custom:queued
  needs_human: custom:needs-human
worker:
  timeout_minutes: 5
  max_attempts: 3
  autocompact: 150k
  test_commands:
    - pytest
max_workers: 3
retention_days: 14
""",
    )

    config = load_config(config_path)

    assert config.labels.queued == "custom:queued"
    assert config.labels.needs_human == "custom:needs-human"
    assert config.worker.timeout_minutes == 5
    assert config.worker.max_attempts == 3
    assert config.worker.autocompact == "150k"
    assert config.max_workers == 3
    assert config.retention_days == 14


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_attempts", 4),
        ("timeout_minutes", 91),
    ],
)
def test_load_config_rejects_worker_limits(
    tmp_path: Path,
    key: str,
    value: int,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    config_path = _write_config(
        tmp_path,
        f"""
repo:
  owner: slaid098
  name: orch
  path: {repo_path.as_posix()}
worker:
  {key}: {value}
  test_commands:
    - pytest
""",
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


@pytest.mark.parametrize("max_workers", [0, 4])
def test_load_config_rejects_invalid_worker_count(
    tmp_path: Path,
    max_workers: int,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    config_path = _write_config(
        tmp_path,
        f"""
repo:
  owner: slaid098
  name: orch
  path: {repo_path.as_posix()}
worker:
  test_commands:
    - pytest
max_workers: {max_workers}
""",
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml")


def test_load_config_requires_test_commands(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    config_path = _write_config(
        tmp_path,
        f"""
repo:
  owner: slaid098
  name: orch
  path: {repo_path.as_posix()}
worker: {{}}
""",
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_rejects_invalid_yaml(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "repo: [\n")

    with pytest.raises(ConfigError, match="Could not read"):
        load_config(config_path)


def test_load_config_rejects_non_mapping_root(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "- item\n")

    with pytest.raises(ConfigError, match="root must be a mapping"):
        load_config(config_path)


def test_load_config_rejects_missing_repo_path(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
repo:
  owner: slaid098
  name: orch
  path: missing
worker:
  test_commands:
    - pytest
""",
    )

    with pytest.raises(ConfigError, match="does not exist"):
        load_config(config_path)


@pytest.mark.parametrize(
    "body",
    [
        """
repo: []
worker:
  test_commands:
    - pytest
""",
        """
repo:
  owner: ""
  name: orch
  path: {repo_path}
worker:
  test_commands:
    - pytest
""",
        """
repo:
  owner: slaid098
  name: orch
  path: {repo_path}
labels:
  queued: ""
worker:
  test_commands:
    - pytest
""",
        """
repo:
  owner: slaid098
  name: orch
  path: {repo_path}
worker:
  test_commands: pytest
""",
        """
repo:
  owner: slaid098
  name: orch
  path: {repo_path}
worker:
  test_commands:
    - ""
""",
    ],
)
def test_load_config_rejects_invalid_shapes(
    tmp_path: Path,
    body: str,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    config_path = _write_config(tmp_path, body.format(repo_path=repo_path.as_posix()))

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_uses_github_token_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", " fallback ")
    config_path = _write_config(
        tmp_path,
        f"""
repo:
  owner: slaid098
  name: orch
  path: {repo_path.as_posix()}
worker:
  test_commands:
    - pytest
""",
    )

    config = load_config(config_path)

    assert config.gh_token == "fallback"


def test_load_config_rejects_non_git_repo(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config_path = _write_config(
        tmp_path,
        f"""
repo:
  owner: slaid098
  name: orch
  path: {repo_path.as_posix()}
worker:
  test_commands:
    - pytest
""",
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


@pytest.mark.parametrize("value", ["99k", "1001k", "abc", "0", "2000"])
def test_load_config_rejects_invalid_autocompact(
    tmp_path: Path,
    value: str,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    config_path = _write_config(
        tmp_path,
        f"""
repo:
  owner: slaid098
  name: orch
  path: {repo_path.as_posix()}
worker:
  autocompact: {value}
  test_commands:
    - pytest
""",
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_accepts_autocompact_auto(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    config_path = _write_config(
        tmp_path,
        f"""
repo:
  owner: slaid098
  name: orch
  path: {repo_path.as_posix()}
worker:
  autocompact: auto
  test_commands:
    - pytest
""",
    )

    assert load_config(config_path).worker.autocompact == "auto"


@pytest.mark.parametrize("value", [0, 366])
def test_load_config_rejects_invalid_retention(
    tmp_path: Path,
    value: int,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    config_path = _write_config(
        tmp_path,
        f"""
repo:
  owner: slaid098
  name: orch
  path: {repo_path.as_posix()}
worker:
  test_commands:
    - pytest
retention_days: {value}
""",
    )

    with pytest.raises(ConfigError):
        load_config(config_path)

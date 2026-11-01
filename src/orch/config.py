"""Configuration loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from loguru import logger


class ConfigError(RuntimeError):
    """Raised when the orchestrator configuration is missing or invalid."""


@dataclass(frozen=True)
class RepoConfig:
    """GitHub repository and local checkout settings."""

    owner: str
    name: str
    path: Path
    base_branch: str = "main"
    remote: str = "origin"

    @property
    def full_name(self) -> str:
        """Return the repository name in ``owner/name`` form."""
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class LabelsConfig:
    """Labels used to move an issue through the pipeline."""

    queued: str = "agent:queued"
    running: str = "agent:running"
    needs_human: str = "agent:needs-human"
    pr_open: str = "agent:pr-open"


@dataclass(frozen=True)
class WorkerConfig:
    """Containerized worker settings."""

    image: str = "orch-agent:latest"
    model: str = "deepseek-v4.1-flash:cloud"
    timeout_minutes: int = 90
    max_attempts: int = 2
    allowed_tools: str = "Read,Write,Edit,Bash,Glob,Grep"
    setup_commands: tuple[str, ...] = ()
    test_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    """Complete orchestrator configuration."""

    repo: RepoConfig
    labels: LabelsConfig
    worker: WorkerConfig
    data_dir: Path
    poll_interval_seconds: int = 30
    max_workers: int = 2
    gh_token: str | None = None


def load_config(path: Path | None = None) -> Config:
    """Load and validate an ``orch.yaml`` file.

    Args:
        path: Optional configuration path. Defaults to ``orch.yaml`` in the
            current working directory.

    Returns:
        Parsed and validated configuration.

    Raises:
        ConfigError: If the file is missing or contains invalid values.
    """
    config_path = (path or Path("orch.yaml")).expanduser().resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as ex:
        logger.bind(
            path=str(config_path), error=ex, error_type=type(ex).__name__
        ).error("Configuration file not found")
        raise ConfigError(f"Configuration file not found: {config_path}") from ex
    except (OSError, yaml.YAMLError) as ex:
        logger.bind(
            path=str(config_path), error=ex, error_type=type(ex).__name__
        ).error("Could not read configuration file")
        raise ConfigError(f"Could not read configuration file: {config_path}") from ex
    else:
        if not isinstance(raw, dict):
            raise ConfigError("Configuration root must be a mapping")
        return _parse_config(raw, config_path.parent)


def _parse_config(raw: dict[str, Any], base_dir: Path) -> Config:
    repo_raw = _mapping(raw.get("repo"), "repo")
    owner = _required_str(repo_raw, "owner", "repo.owner")
    name = _required_str(repo_raw, "name", "repo.name")
    repo_path = _resolve_path(
        _required_str(repo_raw, "path", "repo.path"),
        base_dir,
    )

    if not repo_path.exists():
        raise ConfigError(f"Repository path does not exist: {repo_path}")
    if not (repo_path / ".git").exists():
        raise ConfigError(f"Repository path is not a git checkout: {repo_path}")

    labels_raw = _mapping(raw.get("labels", {}), "labels")
    labels = LabelsConfig(
        queued=_optional_str(labels_raw, "queued", "agent:queued"),
        running=_optional_str(labels_raw, "running", "agent:running"),
        needs_human=_optional_str(labels_raw, "needs_human", "agent:needs-human"),
        pr_open=_optional_str(labels_raw, "pr_open", "agent:pr-open"),
    )

    worker_raw = _mapping(raw.get("worker"), "worker")
    setup_commands = _str_tuple(
        worker_raw.get("setup_commands", []), "worker.setup_commands"
    )
    test_commands = _str_tuple(
        worker_raw.get("test_commands", []), "worker.test_commands"
    )
    if not test_commands:
        raise ConfigError("worker.test_commands must contain at least one command")

    worker = WorkerConfig(
        image=_optional_str(worker_raw, "image", "orch-agent:latest"),
        model=_optional_str(worker_raw, "model", "deepseek-v4.1-flash:cloud"),
        timeout_minutes=_bounded_int(
            worker_raw.get("timeout_minutes", 90),
            "worker.timeout_minutes",
            minimum=1,
            maximum=90,
        ),
        max_attempts=_bounded_int(
            worker_raw.get("max_attempts", 2),
            "worker.max_attempts",
            minimum=1,
            maximum=3,
        ),
        allowed_tools=_optional_str(
            worker_raw, "allowed_tools", "Read,Write,Edit,Bash,Glob,Grep"
        ),
        setup_commands=setup_commands,
        test_commands=test_commands,
    )

    data_dir = _resolve_path(
        str(raw.get("data_dir") or _default_data_dir()),
        base_dir,
    )
    poll_interval_seconds = _positive_int(
        raw.get("poll_interval_seconds", 30), "poll_interval_seconds"
    )
    max_workers = _bounded_int(
        raw.get("max_workers", 2), "max_workers", minimum=1, maximum=3
    )

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    token = token.strip() if token else None

    return Config(
        repo=RepoConfig(
            owner=owner,
            name=name,
            path=repo_path,
            base_branch=_optional_str(repo_raw, "base_branch", "main"),
            remote=_optional_str(repo_raw, "remote", "origin"),
        ),
        labels=labels,
        worker=worker,
        data_dir=data_dir,
        poll_interval_seconds=poll_interval_seconds,
        max_workers=max_workers,
        gh_token=token,
    )


def _default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "orch"
    return Path.home() / ".local" / "share" / "orch"


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _required_str(raw: dict[str, Any], key: str, name: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_str(raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _str_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{name} must contain only non-empty strings")
        result.append(item.strip())
    return tuple(result)


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    number = _positive_int(value, name)
    if not minimum <= number <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return number


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()

"""Configuration data types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
    autocompact: str = "200k"
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
    retention_days: int = 7
    gh_token: str | None = None

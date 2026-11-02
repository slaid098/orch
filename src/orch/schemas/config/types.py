"""Configuration data types."""

from __future__ import annotations

from dataclasses import dataclass, field
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
class TelegramConfig:
    """One-way Telegram notification settings."""

    enabled: bool = False
    chat_id: str = ""
    message_thread_id: int | None = None
    token: str | None = None
    api_base: str = "https://api.telegram.org"


@dataclass(frozen=True)
class FeedbackConfig:
    """Live issue-comment feedback settings."""

    enabled: bool = True
    live_comments: bool = False
    poll_seconds: int = 30
    allowed_users: tuple[str, ...] = ()


@dataclass(frozen=True)
class WatchdogConfig:
    """Clef-based hang and loop supervision settings."""

    enabled: bool = False
    endpoint: str = "https://ollama.com/v1/systemone"
    model: str = "clef"
    api_key: str | None = None
    confidence_threshold: float = 0.7
    silence_minutes: int = 10
    min_run_minutes: int = 15
    max_nudges: int = 2
    cooldown_minutes: int = 10
    check_seconds: int = 60
    max_context_chars: int = 6000


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
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)

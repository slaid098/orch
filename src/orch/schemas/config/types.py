"""Configuration data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RepoConfig:
    """Forge repository and local checkout settings."""

    owner: str
    name: str
    path: Path
    base_branch: str = "main"
    remote: str = "origin"
    forge: str = "github"
    url: str = ""

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
    hold: str = "agent:hold"


@dataclass(frozen=True)
class WorkerConfig:
    """Containerized worker settings."""

    image: str = "orch-agent:latest"
    model: str = "deepseek-v4.1-flash:cloud"
    timeout_minutes: int = 120
    verify_timeout_minutes: int = 45
    max_attempts: int = 3
    allowed_tools: str = "Read,Write,Edit,Bash,Glob,Grep"
    autocompact: str = "200k"
    setup_commands: tuple[str, ...] = ()
    test_commands: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()


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
class ChecksConfig:
    """GitHub pull-request check polling settings."""

    timeout_minutes: int = 30
    poll_seconds: int = 15
    discovery_seconds: int = 60


@dataclass(frozen=True)
class ReviewerConfig:
    """Read-only reviewer container and merge policy settings."""

    enabled: bool = True
    image: str | None = None
    model: str | None = None
    timeout_minutes: int = 30
    max_rounds: int = 3
    autocompact: str = "200k"
    allowed_tools: str = "Read,Grep,Glob"
    auto_merge: bool = False
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpConfig:
    """MCP servers exposed to worker and reviewer containers."""

    enabled: bool = True
    codegraph: bool = True
    context7: bool = True
    playwright: bool = True
    antidetect: bool = False
    context7_url: str = "https://mcp.context7.com/mcp"
    antidetect_url: str = ""
    codegraph_command: str = "codegraph"
    playwright_command: str = "playwright-mcp"


@dataclass(frozen=True)
class Config:
    """Complete orchestrator configuration."""

    repo: RepoConfig
    labels: LabelsConfig
    worker: WorkerConfig
    data_dir: Path
    name: str = ""
    skills_dir: Path = field(default_factory=lambda: Path("skills"))
    poll_interval_seconds: int = 30
    max_workers: int = 2
    retention_days: int = 7
    workspace_retention_days: int = 14
    min_free_disk_gb: int = 5
    gh_token: str | None = None
    forgejo_token: str | None = None
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    checks: ChecksConfig = field(default_factory=ChecksConfig)
    reviewer: ReviewerConfig = field(default_factory=ReviewerConfig)
    mcp: McpConfig = field(default_factory=McpConfig)

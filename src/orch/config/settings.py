"""Configuration loading and validation."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from orch.config.logger import get_logger
from orch.exceptions.config import ConfigError
from orch.schemas.config.types import (
    ChecksConfig,
    Config,
    FeedbackConfig,
    LabelsConfig,
    McpConfig,
    RepoConfig,
    ReviewerConfig,
    TelegramConfig,
    WatchdogConfig,
    WorkerConfig,
)

log = get_logger(component="config")


class _RuntimeSettings(BaseSettings):
    """Environment-level settings that are not stored in ``orch.yaml``."""

    model_config = SettingsConfigDict(env_prefix="ORCH_", extra="ignore")

    gh_token: str | None = None
    forgejo_token: str | None = None


_GLOBAL_KEYS = (
    "data_dir",
    "skills_dir",
    "poll_interval_seconds",
    "max_workers",
    "retention_days",
    "workspace_retention_days",
    "min_free_disk_gb",
    "telegram",
)


def load_config(path: Path | None = None) -> Config:
    """Load the first project from an ``orch.yaml`` file.

    Use :func:`load_projects` when the file may define a ``projects`` list.

    Args:
        path: Optional configuration path. Defaults to ``orch.yaml`` in the
            current working directory.

    Returns:
        Parsed and validated configuration for the first project.

    Raises:
        ConfigError: If the file is missing or contains invalid values.
    """
    return load_projects(path)[0]


def load_projects(path: Path | None = None) -> tuple[Config, ...]:
    """Load and validate every project in an ``orch.yaml`` file.

    A legacy file without a ``projects`` key produces one configuration.
    Otherwise top-level sections are shared defaults and each ``projects``
    entry overrides them; global settings such as ``data_dir`` and Telegram
    stay at the top level.

    Args:
        path: Optional configuration path. Defaults to ``orch.yaml`` in the
            current working directory.

    Returns:
        One configuration per project, in file order.

    Raises:
        ConfigError: If the file is missing, malformed, or defines duplicate
            project names or repositories.
    """
    config_path = (path or Path("orch.yaml")).expanduser().resolve()
    raw = _read_config(config_path)
    return _parse_projects(raw, config_path.parent, _RuntimeSettings())


def _read_config(config_path: Path) -> dict[str, Any]:
    """Read the YAML root mapping or raise a configuration error."""
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as ex:
        log.bind(path=str(config_path), error=ex, error_type=type(ex).__name__).error(
            "Configuration file not found"
        )
        raise ConfigError(f"Configuration file not found: {config_path}") from ex
    except (OSError, yaml.YAMLError) as ex:
        log.bind(path=str(config_path), error=ex, error_type=type(ex).__name__).error(
            "Could not read configuration file"
        )
        raise ConfigError(f"Could not read configuration file: {config_path}") from ex
    else:
        if not isinstance(raw, dict):
            raise ConfigError("Configuration root must be a mapping")
        return raw


def _parse_projects(
    raw: dict[str, Any],
    base_dir: Path,
    settings: _RuntimeSettings,
) -> tuple[Config, ...]:
    """Build one configuration per project, applying shared defaults."""
    projects_raw = raw.get("projects")
    if projects_raw is None:
        return (_parse_config(raw, raw, base_dir, settings),)
    if not isinstance(projects_raw, list) or not projects_raw:
        raise ConfigError("projects must be a non-empty list")
    if "repo" in raw:
        raise ConfigError("repo must be defined inside each projects entry")

    defaults = {key: value for key, value in raw.items() if key != "projects"}
    configs: list[Config] = []
    names: set[str] = set()
    repositories: set[str] = set()
    for index, value in enumerate(projects_raw, start=1):
        project = _mapping(value, f"projects[{index}]")
        _reject_global_overrides(project, index)
        if "repo" not in project:
            raise ConfigError(f"projects[{index}].repo is required")
        name = project.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ConfigError(f"projects[{index}].name must be a non-empty string")
        overrides = {key: item for key, item in project.items() if key != "name"}
        merged = _deep_merge(defaults, overrides)
        config = _parse_config(
            merged,
            defaults,
            base_dir,
            settings,
            name=name.strip() if isinstance(name, str) else None,
        )
        if config.name in names:
            raise ConfigError(f"Duplicate project name: {config.name!r}")
        if config.repo.full_name in repositories:
            raise ConfigError(f"Duplicate project repository: {config.repo.full_name!r}")
        names.add(config.name)
        repositories.add(config.repo.full_name)
        configs.append(config)
    return tuple(configs)


def _reject_global_overrides(project: dict[str, Any], index: int) -> None:
    """Reject per-project overrides for settings shared by the daemon."""
    for key in _GLOBAL_KEYS:
        if key in project:
            raise ConfigError(
                f"projects[{index}].{key} is a global setting; define it at the top level"
            )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge nested mappings, replacing lists and scalar values."""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _parse_config(
    raw: dict[str, Any],
    global_raw: dict[str, Any],
    base_dir: Path,
    settings: _RuntimeSettings,
    *,
    name: str | None = None,
) -> Config:
    repo = _parse_repo(raw, base_dir)
    labels = _parse_labels(raw)
    worker = _parse_worker(raw)
    shared = _parse_globals(global_raw, base_dir)
    return Config(
        repo=repo,
        labels=labels,
        worker=worker,
        data_dir=shared.data_dir,
        name=name or repo.name,
        skills_dir=shared.skills_dir,
        poll_interval_seconds=shared.poll_interval_seconds,
        max_workers=shared.max_workers,
        retention_days=shared.retention_days,
        workspace_retention_days=shared.workspace_retention_days,
        min_free_disk_gb=shared.min_free_disk_gb,
        gh_token=_resolve_token(settings),
        forgejo_token=_resolve_forgejo_token(settings),
        telegram=shared.telegram,
        feedback=_parse_feedback(raw, repo),
        watchdog=_parse_watchdog(raw),
        checks=_parse_checks(raw),
        reviewer=_parse_reviewer(raw),
        mcp=_parse_mcp(raw),
    )


@dataclass(frozen=True)
class _GlobalSettings:
    """Settings shared by every project in one daemon."""

    data_dir: Path
    skills_dir: Path
    poll_interval_seconds: int
    max_workers: int
    retention_days: int
    workspace_retention_days: int
    min_free_disk_gb: int
    telegram: TelegramConfig


def _parse_globals(raw: dict[str, Any], base_dir: Path) -> _GlobalSettings:
    """Parse top-level settings that cannot vary per project."""
    return _GlobalSettings(
        data_dir=_resolve_path(str(raw.get("data_dir") or _default_data_dir()), base_dir),
        skills_dir=_resolve_path(str(raw.get("skills_dir") or "skills"), base_dir),
        poll_interval_seconds=_positive_int(
            raw.get("poll_interval_seconds", 30), "poll_interval_seconds"
        ),
        max_workers=_bounded_int(raw.get("max_workers", 2), "max_workers", minimum=1, maximum=3),
        retention_days=_bounded_int(
            raw.get("retention_days", 7),
            "retention_days",
            minimum=1,
            maximum=365,
        ),
        workspace_retention_days=_bounded_int(
            raw.get("workspace_retention_days", 14),
            "workspace_retention_days",
            minimum=1,
            maximum=365,
        ),
        min_free_disk_gb=_bounded_int(
            raw.get("min_free_disk_gb", 5),
            "min_free_disk_gb",
            minimum=1,
            maximum=1000,
        ),
        telegram=_parse_telegram(raw, base_dir),
    )


def _parse_repo(raw: dict[str, Any], base_dir: Path) -> RepoConfig:
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
    forge = _optional_str(repo_raw, "forge", "github").lower()
    if forge not in {"github", "forgejo"}:
        raise ConfigError("repo.forge must be 'github' or 'forgejo'")
    url = _nullable_str(repo_raw, "url", "repo.url") or ""
    if forge == "forgejo":
        _validate_forgejo_url(url)
    return RepoConfig(
        owner=owner,
        name=name,
        path=repo_path,
        base_branch=_optional_str(repo_raw, "base_branch", "main"),
        remote=_optional_str(repo_raw, "remote", "origin"),
        forge=forge,
        url=url.rstrip("/"),
    )


def _validate_forgejo_url(url: str) -> None:
    """Validate the Forgejo base URL used for API and git access."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("repo.url must be an http(s) URL when repo.forge is 'forgejo'")
    if parsed.username or parsed.password:
        raise ConfigError("repo.url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigError("repo.url must not contain a query or fragment")


def _parse_labels(raw: dict[str, Any]) -> LabelsConfig:
    labels_raw = _mapping(raw.get("labels", {}), "labels")
    return LabelsConfig(
        queued=_optional_str(labels_raw, "queued", "agent:queued"),
        running=_optional_str(labels_raw, "running", "agent:running"),
        needs_human=_optional_str(labels_raw, "needs_human", "agent:needs-human"),
        pr_open=_optional_str(labels_raw, "pr_open", "agent:pr-open"),
        hold=_optional_str(labels_raw, "hold", "agent:hold"),
    )


def _parse_worker(raw: dict[str, Any]) -> WorkerConfig:
    worker_raw = _mapping(raw.get("worker"), "worker")
    setup_commands = _str_tuple(worker_raw.get("setup_commands", []), "worker.setup_commands")
    test_commands = _str_tuple(worker_raw.get("test_commands", []), "worker.test_commands")
    if not test_commands:
        raise ConfigError("worker.test_commands must contain at least one command")
    return WorkerConfig(
        image=_optional_str(worker_raw, "image", "orch-agent:latest"),
        model=_optional_str(worker_raw, "model", "deepseek-v4.1-flash:cloud"),
        timeout_minutes=_bounded_int(
            worker_raw.get("timeout_minutes", 120),
            "worker.timeout_minutes",
            minimum=1,
            maximum=240,
        ),
        verify_timeout_minutes=_bounded_int(
            worker_raw.get("verify_timeout_minutes", 45),
            "worker.verify_timeout_minutes",
            minimum=1,
            maximum=240,
        ),
        max_attempts=_bounded_int(
            worker_raw.get("max_attempts", 3),
            "worker.max_attempts",
            minimum=1,
            maximum=3,
        ),
        allowed_tools=_optional_str(worker_raw, "allowed_tools", "Read,Write,Edit,Bash,Glob,Grep"),
        autocompact=_parse_autocompact(worker_raw, "worker.autocompact"),
        setup_commands=setup_commands,
        test_commands=test_commands,
        skills=_skill_names(worker_raw.get("skills", []), "worker.skills"),
    )


def _parse_autocompact(raw: dict[str, Any], name: str) -> str:
    value = _optional_str(raw, "autocompact", "200k").lower()
    if value == "auto":
        return value
    suffix = value.endswith("k")
    digits = value[:-1] if suffix else value
    if not digits.isdigit():
        raise ConfigError(f"{name} must be 'auto' or tokens like 200k")
    tokens = int(digits) * (1000 if suffix else 1)
    if not 100_000 <= tokens <= 1_000_000:
        raise ConfigError(f"{name} must be between 100k and 1M tokens")
    return f"{tokens // 1000}k" if tokens % 1000 == 0 else str(tokens)


def _parse_telegram(raw: dict[str, Any], base_dir: Path) -> TelegramConfig:
    telegram_raw = _mapping(raw.get("telegram", {}), "telegram")
    enabled = _bool(telegram_raw.get("enabled", False), "telegram.enabled")
    chat_id_value = telegram_raw.get("chat_id", "")
    if isinstance(chat_id_value, int) and not isinstance(chat_id_value, bool):
        chat_id_value = str(chat_id_value)
    chat_id = chat_id_value.strip() if isinstance(chat_id_value, str) else ""
    thread_value = telegram_raw.get("message_thread_id")
    thread_id = (
        None if thread_value is None else _positive_int(thread_value, "telegram.message_thread_id")
    )
    token_file_value = telegram_raw.get("token_file")
    token_file = (
        _resolve_path(str(token_file_value), base_dir)
        if isinstance(token_file_value, str) and token_file_value.strip()
        else None
    )
    token = _resolve_telegram_token(token_file)
    if enabled and not chat_id:
        raise ConfigError("telegram.chat_id is required when telegram is enabled")
    if enabled and not token:
        raise ConfigError(
            "telegram is enabled but no bot token was found; "
            "set TELEGRAM_BOT_TOKEN or telegram.token_file"
        )
    return TelegramConfig(
        enabled=enabled,
        chat_id=chat_id,
        message_thread_id=thread_id,
        token=token,
        api_base=_optional_str(telegram_raw, "api_base", "https://api.telegram.org"),
    )


def _parse_feedback(raw: dict[str, Any], repo: RepoConfig) -> FeedbackConfig:
    feedback_raw = _mapping(raw.get("feedback", {}), "feedback")
    allowed = _str_tuple(feedback_raw.get("allowed_users", []), "feedback.allowed_users")
    return FeedbackConfig(
        enabled=_bool(feedback_raw.get("enabled", True), "feedback.enabled"),
        live_comments=_bool(feedback_raw.get("live_comments", False), "feedback.live_comments"),
        poll_seconds=_bounded_int(
            feedback_raw.get("poll_seconds", 30),
            "feedback.poll_seconds",
            minimum=1,
            maximum=600,
        ),
        allowed_users=allowed or (repo.owner,),
    )


def _parse_watchdog(raw: dict[str, Any]) -> WatchdogConfig:
    watchdog_raw = _mapping(raw.get("watchdog", {}), "watchdog")
    enabled = _bool(watchdog_raw.get("enabled", False), "watchdog.enabled")
    api_key = _resolve_ollama_key()
    if enabled and not api_key:
        raise ConfigError(
            "watchdog is enabled but no Ollama Cloud key was found; set OLLAMA_API_KEY"
        )
    return WatchdogConfig(
        enabled=enabled,
        endpoint=_optional_str(watchdog_raw, "endpoint", "https://ollama.com/v1/systemone"),
        model=_optional_str(watchdog_raw, "model", "clef"),
        api_key=api_key,
        confidence_threshold=_bounded_float(
            watchdog_raw.get("confidence_threshold", 0.7),
            "watchdog.confidence_threshold",
            minimum=0.0,
            maximum=1.0,
        ),
        silence_minutes=_bounded_int(
            watchdog_raw.get("silence_minutes", 10),
            "watchdog.silence_minutes",
            minimum=1,
            maximum=120,
        ),
        min_run_minutes=_bounded_int(
            watchdog_raw.get("min_run_minutes", 15),
            "watchdog.min_run_minutes",
            minimum=0,
            maximum=90,
        ),
        max_nudges=_bounded_int(
            watchdog_raw.get("max_nudges", 2),
            "watchdog.max_nudges",
            minimum=0,
            maximum=5,
        ),
        cooldown_minutes=_bounded_int(
            watchdog_raw.get("cooldown_minutes", 10),
            "watchdog.cooldown_minutes",
            minimum=1,
            maximum=120,
        ),
        check_seconds=_bounded_int(
            watchdog_raw.get("check_seconds", 60),
            "watchdog.check_seconds",
            minimum=10,
            maximum=3600,
        ),
        max_context_chars=_bounded_int(
            watchdog_raw.get("max_context_chars", 6000),
            "watchdog.max_context_chars",
            minimum=1000,
            maximum=20000,
        ),
    )


def _parse_checks(raw: dict[str, Any]) -> ChecksConfig:
    checks_raw = _mapping(raw.get("checks", {}), "checks")
    return ChecksConfig(
        timeout_minutes=_bounded_int(
            checks_raw.get("timeout_minutes", 30),
            "checks.timeout_minutes",
            minimum=5,
            maximum=180,
        ),
        poll_seconds=_bounded_int(
            checks_raw.get("poll_seconds", 15),
            "checks.poll_seconds",
            minimum=5,
            maximum=300,
        ),
        discovery_seconds=_bounded_int(
            checks_raw.get("discovery_seconds", 60),
            "checks.discovery_seconds",
            minimum=10,
            maximum=600,
        ),
    )


def _parse_reviewer(raw: dict[str, Any]) -> ReviewerConfig:
    reviewer_raw = _mapping(raw.get("reviewer", {}), "reviewer")
    return ReviewerConfig(
        enabled=_bool(reviewer_raw.get("enabled", True), "reviewer.enabled"),
        image=_nullable_str(reviewer_raw, "image", "reviewer.image"),
        model=_nullable_str(reviewer_raw, "model", "reviewer.model"),
        timeout_minutes=_bounded_int(
            reviewer_raw.get("timeout_minutes", 30),
            "reviewer.timeout_minutes",
            minimum=1,
            maximum=60,
        ),
        max_rounds=_bounded_int(
            reviewer_raw.get("max_rounds", 3),
            "reviewer.max_rounds",
            minimum=1,
            maximum=3,
        ),
        autocompact=_parse_autocompact(reviewer_raw, "reviewer.autocompact"),
        allowed_tools=_optional_str(reviewer_raw, "allowed_tools", "Read,Grep,Glob"),
        auto_merge=_bool(reviewer_raw.get("auto_merge", False), "reviewer.auto_merge"),
        skills=_skill_names(reviewer_raw.get("skills", []), "reviewer.skills"),
    )


def _parse_mcp(raw: dict[str, Any]) -> McpConfig:
    mcp_raw = _mapping(raw.get("mcp", {}), "mcp")
    enabled = _bool(mcp_raw.get("enabled", True), "mcp.enabled")
    antidetect = _bool(mcp_raw.get("antidetect", False), "mcp.antidetect")
    antidetect_url = mcp_raw.get("antidetect_url", "")
    if not isinstance(antidetect_url, str):
        raise ConfigError("mcp.antidetect_url must be a string")
    antidetect_url = antidetect_url.strip()
    if antidetect:
        if not antidetect_url:
            raise ConfigError("mcp.antidetect_url is required when antidetect is enabled")
        antidetect_url = _validated_antidetect_url(antidetect_url)
    return McpConfig(
        enabled=enabled,
        codegraph=_bool(mcp_raw.get("codegraph", True), "mcp.codegraph"),
        context7=_bool(mcp_raw.get("context7", True), "mcp.context7"),
        playwright=_bool(mcp_raw.get("playwright", True), "mcp.playwright"),
        antidetect=antidetect,
        context7_url=_optional_str(mcp_raw, "context7_url", "https://mcp.context7.com/mcp"),
        antidetect_url=antidetect_url,
        codegraph_command=_optional_str(mcp_raw, "codegraph_command", "codegraph"),
        playwright_command=_optional_str(mcp_raw, "playwright_command", "playwright-mcp"),
    )


_LOCAL_HTTP_HOSTS = frozenset({"localhost", "host.docker.internal"})


def _validated_antidetect_url(url: str) -> str:
    """Validate the antidetect MCP endpoint.

    Args:
        url: Endpoint URL taken from ``mcp.antidetect_url``.

    Returns:
        The unchanged URL when it is safe to use.

    Raises:
        ConfigError: If the URL is malformed, carries embedded credentials, or
            uses plaintext HTTP towards a public host.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ConfigError("mcp.antidetect_url must be an http:// or https:// URL")
    if parsed.username or parsed.password:
        raise ConfigError("mcp.antidetect_url must not contain embedded credentials")
    if not parsed.hostname:
        raise ConfigError("mcp.antidetect_url must include a host")
    try:
        _ = parsed.port
    except ValueError as ex:
        raise ConfigError("mcp.antidetect_url must include a valid port") from ex
    if parsed.scheme == "https" or _is_local_host(parsed.hostname):
        return url
    raise ConfigError(
        "mcp.antidetect_url must use https:// for public hosts; plaintext http "
        f"is allowed only for local addresses, got {parsed.hostname}"
    )


def _is_local_host(host: str) -> bool:
    """Return whether a host is local or private enough for plaintext HTTP.

    Args:
        host: Hostname or IP address to check.

    Returns:
        True for loopback, ``localhost``, ``host.docker.internal``, private
        IPv4 ranges, and IPv6 unique-local or loopback addresses.
    """
    name = host.lower().rstrip(".")
    if name in _LOCAL_HTTP_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def _resolve_telegram_token(token_file: Path | None) -> str | None:
    if token_file is not None:
        token = _read_telegram_token_file(token_file)
        if token:
            return token
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None


def _read_telegram_token_file(token_file: Path) -> str | None:
    try:
        lines = token_file.read_text(encoding="utf-8").splitlines()
    except OSError as ex:
        log.bind(
            path=str(token_file),
            error=ex,
            error_type=type(ex).__name__,
        ).warning("Could not read Telegram token file")
        return None
    for line in lines:
        key, separator, value = line.strip().partition("=")
        if separator and key.strip() == "TELEGRAM_BOT_TOKEN":
            token = value.strip().strip("'\"")
            return token or None
    return None


def _resolve_ollama_key() -> str | None:
    token = os.environ.get("OLLAMA_API_KEY", "").strip()
    if token:
        return token
    path = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    value = (data.get("ollama-cloud") or {}).get("key")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be a boolean")
    return value


def _bounded_float(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{name} must be a number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return number


def _resolve_token(settings: _RuntimeSettings) -> str | None:
    # Ignore ambient GITHUB_TOKEN: on a developer machine it often belongs to
    # another account. The explicit GH_TOKEN or gh keyring wins.
    token = settings.gh_token or os.environ.get("GH_TOKEN")
    return token.strip() if token else None


def _resolve_forgejo_token(settings: _RuntimeSettings) -> str | None:
    token = settings.forgejo_token or os.environ.get("FORGEJO_TOKEN")
    return token.strip() if token else None


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


def _nullable_str(raw: dict[str, Any], key: str, name: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string when set")
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


_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _skill_names(value: Any, name: str) -> tuple[str, ...]:
    """Parse and validate skill directory names."""
    names = _str_tuple(value, name)
    for skill in names:
        if not _SKILL_NAME_RE.fullmatch(skill):
            raise ConfigError(
                f"{name} entry {skill!r} must be a lowercase skill directory "
                "name using letters, digits, and hyphens"
            )
    return names


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

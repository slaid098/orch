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
    assert config.worker.max_attempts == 3
    assert config.worker.autocompact == "200k"
    assert config.mcp.antidetect is False
    assert config.mcp.antidetect_url == ""
    assert config.worker.test_commands == ("uv run pytest -q",)
    assert config.max_workers == 2
    assert config.retention_days == 7
    assert config.data_dir == (tmp_path / "data").resolve()
    assert config.gh_token == "test-token"
    assert config.checks.timeout_minutes == 30
    assert config.checks.poll_seconds == 15
    assert config.checks.discovery_seconds == 60
    assert config.reviewer.enabled
    assert config.reviewer.image is None
    assert config.reviewer.model is None
    assert config.reviewer.timeout_minutes == 15
    assert config.reviewer.max_rounds == 3
    assert config.reviewer.autocompact == "200k"
    assert config.reviewer.allowed_tools == "Read,Grep,Glob"


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


def test_load_config_reads_mcp_and_hold_override(tmp_path: Path) -> None:
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
  hold: custom:hold
worker:
  test_commands:
    - pytest
mcp:
  enabled: false
  codegraph: false
  context7: false
  playwright: false
  antidetect: false
  context7_url: https://docs.example.test/mcp
  antidetect_url: http://127.0.0.1:9000/mcp/
  codegraph_command: cg
  playwright_command: pw-mcp
""",
    )

    config = load_config(config_path)

    assert config.labels.hold == "custom:hold"
    assert not config.mcp.enabled
    assert not config.mcp.codegraph
    assert not config.mcp.context7
    assert not config.mcp.playwright
    assert not config.mcp.antidetect
    assert config.mcp.context7_url == "https://docs.example.test/mcp"
    assert config.mcp.antidetect_url == "http://127.0.0.1:9000/mcp/"
    assert config.mcp.codegraph_command == "cg"
    assert config.mcp.playwright_command == "pw-mcp"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("", "antidetect_url is required"),
        ("http://browser.example/mcp/", "must use https"),
    ],
)
def test_load_config_rejects_unsafe_antidetect_url(
    tmp_path: Path,
    url: str,
    message: str,
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
mcp:
  antidetect: true
  antidetect_url: "{url}"
""",
    )

    with pytest.raises(ConfigError, match=message):
        load_config(config_path)


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


def test_load_config_uses_gh_token_and_ignores_ambient_github_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    monkeypatch.setenv("GH_TOKEN", " explicit ")
    monkeypatch.setenv("GITHUB_TOKEN", " wrong-account ")
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

    assert config.gh_token == "explicit"


def test_load_config_does_not_use_ambient_github_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "wrong-account")
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

    assert config.gh_token is None


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


def test_load_config_reads_notification_feedback_and_watchdog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-key")
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
telegram:
  enabled: true
  chat_id: "123"
  message_thread_id: 42
  api_base: https://telegram.example
feedback:
  enabled: true
  live_comments: true
  poll_seconds: 7
  allowed_users:
    - alice
watchdog:
  enabled: true
  endpoint: https://clef.example/v1
  model: custom-clef
  confidence_threshold: 0.8
  silence_minutes: 12
  min_run_minutes: 20
  max_nudges: 1
  cooldown_minutes: 15
  check_seconds: 90
  max_context_chars: 8000
""",
    )

    config = load_config(config_path)

    assert config.telegram.enabled
    assert config.telegram.chat_id == "123"
    assert config.telegram.message_thread_id == 42
    assert config.telegram.token == "bot-token"
    assert config.telegram.api_base == "https://telegram.example"
    assert config.feedback.live_comments
    assert config.feedback.poll_seconds == 7
    assert config.feedback.allowed_users == ("alice",)
    assert config.watchdog.enabled
    assert config.watchdog.endpoint == "https://clef.example/v1"
    assert config.watchdog.model == "custom-clef"
    assert config.watchdog.api_key == "ollama-key"
    assert config.watchdog.confidence_threshold == 0.8
    assert config.watchdog.max_nudges == 1
    assert config.watchdog.max_context_chars == 8000


def test_telegram_token_can_come_from_a_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    token_file = tmp_path / "telegram.env"
    token_file.write_text("OTHER=x\nTELEGRAM_BOT_TOKEN='from-file'\n", encoding="utf-8")
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
telegram:
  enabled: true
  chat_id: "123"
  token_file: {token_file.as_posix()}
""",
    )

    config = load_config(config_path)

    assert config.telegram.token == "from-file"


def test_telegram_token_file_wins_over_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "ambient-token")
    token_file = tmp_path / "telegram.env"
    token_file.write_text("TELEGRAM_BOT_TOKEN=from-file\n", encoding="utf-8")
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
telegram:
  enabled: true
  chat_id: "123"
  token_file: {token_file.as_posix()}
""",
    )

    config = load_config(config_path)

    assert config.telegram.token == "from-file"


def test_enabled_telegram_requires_a_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
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
telegram:
  enabled: true
  chat_id: "123"
""",
    )

    with pytest.raises(ConfigError, match="bot token"):
        load_config(config_path)


def test_enabled_telegram_requires_a_chat_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
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
telegram:
  enabled: true
""",
    )

    with pytest.raises(ConfigError, match="chat_id"):
        load_config(config_path)


def test_enabled_watchdog_requires_an_ollama_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        "orch.config.settings._resolve_ollama_key",
        lambda: None,
    )
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
watchdog:
  enabled: true
""",
    )

    with pytest.raises(ConfigError, match="Ollama Cloud key"):
        load_config(config_path)


@pytest.mark.parametrize(
    ("section", "body"),
    [
        ("telegram", 'enabled: "yes"'),
        ("feedback", "poll_seconds: 0"),
        ("watchdog", "confidence_threshold: 2"),
    ],
)
def test_load_config_rejects_invalid_feature_settings(
    tmp_path: Path,
    section: str,
    body: str,
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
{section}:
  {body}
""",
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_reads_checks_and_reviewer(tmp_path: Path) -> None:
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
checks:
  timeout_minutes: 45
  poll_seconds: 20
  discovery_seconds: 90
reviewer:
  enabled: false
  image: reviewer:latest
  model: review-model
  timeout_minutes: 12
  max_rounds: 2
  autocompact: 150k
  allowed_tools: "Read,Grep"
""",
    )

    config = load_config(config_path)

    assert config.checks.timeout_minutes == 45
    assert config.checks.poll_seconds == 20
    assert config.checks.discovery_seconds == 90
    assert not config.reviewer.enabled
    assert config.reviewer.image == "reviewer:latest"
    assert config.reviewer.model == "review-model"
    assert config.reviewer.timeout_minutes == 12
    assert config.reviewer.max_rounds == 2
    assert config.reviewer.autocompact == "150k"
    assert config.reviewer.allowed_tools == "Read,Grep"


@pytest.mark.parametrize(
    "section",
    [
        "reviewer:\n  max_rounds: 4",
        "reviewer:\n  max_rounds: 0",
        "reviewer:\n  timeout_minutes: 0",
        "reviewer:\n  autocompact: 99k",
        "reviewer:\n  image: ''",
        "checks:\n  poll_seconds: 1",
        "checks:\n  discovery_seconds: 601",
    ],
)
def test_load_config_rejects_invalid_gate_settings(
    tmp_path: Path,
    section: str,
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
{section}
""",
    )

    with pytest.raises(ConfigError):
        load_config(config_path)

from __future__ import annotations

import json
from pathlib import Path

from orch.schemas.config.types import Config, LabelsConfig, RepoConfig, WorkerConfig
from orch.services.worker import claude


def _config(tmp_path: Path, *, autocompact: str = "200k") -> Config:
    return Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(
            model="deepseek-v4.1-flash:cloud",
            autocompact=autocompact,
            test_commands=("pytest",),
        ),
        data_dir=tmp_path / "app_data",
    )


def test_autocompact_tokens_parses_suffix() -> None:
    assert claude.autocompact_tokens("200k") == 200_000
    assert claude.autocompact_tokens("150000") == 150_000


def test_autocompact_tokens_returns_none_for_auto() -> None:
    assert claude.autocompact_tokens("auto") is None


def test_build_command_pins_window_and_model(tmp_path: Path) -> None:
    command = claude.build_command(_config(tmp_path, autocompact="150k"))

    assert "--autocompact 150k" in command
    assert '--model "deepseek-v4.1-flash:cloud"' in command
    assert "--output-format stream-json" in command
    assert "< /out/prompt.md" in command
    assert '"$(cat /out/prompt.md)"' not in command


def test_build_command_adds_mcp_flags_and_tools(tmp_path: Path) -> None:
    command = claude.build_command(_config(tmp_path), "/out/mcp.json")

    assert '--mcp-config "/out/mcp.json" --strict-mcp-config' in command
    assert "mcp__codegraph__codegraph_explore" in command
    assert "mcp__context7__query-docs" in command
    assert "mcp__playwright__*" in command
    assert "mcp__antidetect-browser__*" in command


def test_build_settings_pins_every_layer(tmp_path: Path) -> None:
    settings = claude.build_settings(_config(tmp_path))

    assert settings["autoCompactEnabled"] is True
    assert settings["autoCompactWindow"] == 200_000
    assert settings["modelSettings"] == {
        "deepseek-v4.1-flash:cloud": {"autoCompactWindow": 200_000}
    }
    assert settings["env"] == {
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000",
    }


def test_build_settings_disables_compaction_for_auto(tmp_path: Path) -> None:
    assert claude.build_settings(_config(tmp_path, autocompact="auto")) == {
        "autoCompactEnabled": False
    }


def test_settings_json_is_valid_json(tmp_path: Path) -> None:
    payload = claude.settings_json(_config(tmp_path, autocompact="500k"))

    assert json.loads(payload)["autoCompactWindow"] == 500_000

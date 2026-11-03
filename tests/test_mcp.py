from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.schemas.config.types import (
    Config,
    LabelsConfig,
    McpConfig,
    RepoConfig,
    WorkerConfig,
)
from orch.services import mcp


def _config(tmp_path: Path, **mcp_values: object) -> Config:
    return Config(
        repo=RepoConfig(owner="owner", name="name", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(),
        data_dir=tmp_path / "data",
        mcp=McpConfig(**mcp_values),
    )


def test_allowed_tools_splits_worker_and_reviewer(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        antidetect=True,
        antidetect_url="https://browser.example/mcp/",
    )

    worker = mcp.allowed_tools(config, "worker")
    reviewer = mcp.allowed_tools(config, "reviewer")

    assert "mcp__codegraph__codegraph_explore" in worker
    assert "mcp__context7__query-docs" in worker
    assert "mcp__playwright__*" in worker
    assert "mcp__antidetect-browser__*" in worker
    assert "mcp__playwright__*" not in reviewer
    assert "mcp__antidetect-browser__*" not in reviewer


def test_antidetect_is_opt_in_and_requires_an_explicit_url(tmp_path: Path) -> None:
    default = _config(tmp_path)
    assert "mcp__antidetect-browser__*" not in mcp.allowed_tools(default, "worker")
    path = mcp.write_worker_config(default, tmp_path / "default-mcp.json")
    assert path is not None
    assert "antidetect-browser" not in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]

    enabled_without_url = _config(tmp_path, antidetect=True)
    assert "mcp__antidetect-browser__*" not in mcp.allowed_tools(enabled_without_url, "worker")
    path = mcp.write_worker_config(enabled_without_url, tmp_path / "no-url-mcp.json")
    assert path is not None
    assert "antidetect-browser" not in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_allowed_tools_respects_disabled_servers(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        codegraph=False,
        context7=False,
        playwright=False,
        antidetect=False,
    )

    assert mcp.allowed_tools(config, "worker") == ()
    assert mcp.allowed_tools(config, "reviewer") == ()


def test_allowed_tools_returns_nothing_when_mcp_is_disabled(tmp_path: Path) -> None:
    assert mcp.allowed_tools(_config(tmp_path, enabled=False), "worker") == ()


def test_write_worker_config_includes_all_worker_servers(tmp_path: Path) -> None:
    path = mcp.write_worker_config(
        _config(
            tmp_path,
            antidetect=True,
            antidetect_url="https://browser.example/mcp/",
        ),
        tmp_path / "mcp.json",
    )

    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    servers = payload["mcpServers"]
    assert set(servers) == {"codegraph", "context7", "playwright", "antidetect-browser"}
    assert servers["codegraph"]["args"] == ["serve", "--mcp"]
    assert servers["context7"]["url"] == "https://mcp.context7.com/mcp"
    assert "--headless" in servers["playwright"]["args"]
    assert "/usr/bin/chromium" in servers["playwright"]["args"]


def test_write_reviewer_config_excludes_worker_only_servers(tmp_path: Path) -> None:
    path = mcp.write_reviewer_config(_config(tmp_path), tmp_path / "mcp.json")

    assert path is not None
    servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"codegraph", "context7"}


def test_write_config_skips_when_disabled_or_empty(tmp_path: Path) -> None:
    assert mcp.write_worker_config(_config(tmp_path, enabled=False), tmp_path / "mcp.json") is None
    assert (
        mcp.write_worker_config(
            _config(
                tmp_path,
                codegraph=False,
                context7=False,
                playwright=False,
                antidetect=False,
            ),
            tmp_path / "mcp.json",
        )
        is None
    )


def test_write_config_does_not_raise_on_filesystem_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", fail)

    assert mcp.write_worker_config(_config(tmp_path), tmp_path / "mcp.json") is None

"""Write per-container MCP server configurations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orch.config.logger import get_logger
from orch.schemas.config.types import Config

log = get_logger(component="mcp")

_CODEGRAPH_TOOL = "mcp__codegraph__codegraph_explore"
_CONTEXT7_TOOLS = (
    "mcp__context7__resolve-library-id",
    "mcp__context7__query-docs",
)
_PLAYWRIGHT_TOOL = "mcp__playwright__*"
_ANTIDETECT_TOOL = "mcp__antidetect-browser__*"


def allowed_tools(config: Config, role: str) -> tuple[str, ...]:
    """Return extra MCP tool names for a worker or reviewer role.

    Args:
        config: Orchestrator configuration.
        role: ``worker`` or ``reviewer``.

    Returns:
        Tool names to append to the role's built-in allowlist.
    """
    if not config.mcp.enabled:
        return ()
    tools: list[str] = []
    if config.mcp.codegraph:
        tools.append(_CODEGRAPH_TOOL)
    if config.mcp.context7:
        tools.extend(_CONTEXT7_TOOLS)
    if role == "worker":
        if config.mcp.playwright:
            tools.append(_PLAYWRIGHT_TOOL)
        if config.mcp.antidetect:
            tools.append(_ANTIDETECT_TOOL)
    return tuple(tools)


def write_worker_config(config: Config, path: Path) -> Path | None:
    """Write the MCP configuration for a worker container.

    Args:
        config: Orchestrator configuration.
        path: Host path mounted as ``/out/mcp.json``.

    Returns:
        The written path, or ``None`` when MCP is disabled or cannot be written.
    """
    return _write_config(config, path, _worker_servers(config))


def write_reviewer_config(config: Config, path: Path) -> Path | None:
    """Write the MCP configuration for a reviewer container.

    Reviewers intentionally receive only local CodeGraph and Context7. They do
    not get Playwright or antidetect-browser access.

    Args:
        config: Orchestrator configuration.
        path: Host path mounted as ``/out/mcp.json``.

    Returns:
        The written path, or ``None`` when MCP is disabled or cannot be written.
    """
    return _write_config(config, path, _reviewer_servers(config))


def _worker_servers(config: Config) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    if config.mcp.codegraph:
        servers["codegraph"] = _codegraph(config)
    if config.mcp.context7:
        servers["context7"] = _context7(config)
    if config.mcp.playwright:
        servers["playwright"] = _playwright(config)
    if config.mcp.antidetect:
        servers["antidetect-browser"] = {
            "type": "http",
            "url": config.mcp.antidetect_url,
        }
    return servers


def _reviewer_servers(config: Config) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    if config.mcp.codegraph:
        servers["codegraph"] = _codegraph(config)
    if config.mcp.context7:
        servers["context7"] = _context7(config)
    return servers


def _codegraph(config: Config) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": config.mcp.codegraph_command,
        "args": ["serve", "--mcp"],
        "env": {"CODEGRAPH_TELEMETRY": "0"},
        "alwaysLoad": True,
    }


def _context7(config: Config) -> dict[str, Any]:
    return {
        "type": "http",
        "url": config.mcp.context7_url,
    }


def _playwright(config: Config) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": config.mcp.playwright_command,
        "args": [
            "--headless",
            "--isolated",
            "--no-sandbox",
            "--executable-path",
            "/usr/bin/chromium",
        ],
    }


def _write_config(
    config: Config,
    path: Path,
    servers: dict[str, Any],
) -> Path | None:
    if not config.mcp.enabled or not servers:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcpServers": servers}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as ex:
        log.bind(
            path=str(path),
            error=ex,
            error_type=type(ex).__name__,
        ).warning("Could not write MCP configuration")
        return None
    log.bind(path=str(path), servers=sorted(servers)).info("Wrote MCP configuration")
    return path

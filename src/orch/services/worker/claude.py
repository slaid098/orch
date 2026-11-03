"""Claude Code invocation shared by every agent role."""

from __future__ import annotations

import json
from typing import Any

from orch.schemas.config.types import Config
from orch.services import mcp


def autocompact_tokens(value: str) -> int | None:
    """Return the autocompact window in tokens, or ``None`` for auto."""
    if value == "auto":
        return None
    suffix = value.endswith("k")
    digits = value[:-1] if suffix else value
    return int(digits) * (1000 if suffix else 1)


def build_command(config: Config, mcp_config: str | None = None) -> str:
    """Build the Claude Code command for one agent attempt.

    Args:
        config: Orchestrator configuration.
        mcp_config: Optional container path to an MCP JSON configuration.

    Returns:
        Shell command executed inside the worker container.
    """
    worker = config.worker
    return (
        'claude -p "$(cat /out/prompt.md)" '
        "--output-format stream-json "
        "--verbose "
        "--permission-mode bypassPermissions "
        "--permission-prompts none "
        f'--allowedTools "{_allowed_tools(config)}" '
        f"{_mcp_flags(mcp_config)}"
        f"--autocompact {worker.autocompact} "
        f'--model "{worker.model}"'
    )


def build_stream_command(
    config: Config,
    mcp_config: str | None = None,
) -> str:
    """Build a long-lived Claude Code command that reads stream-json stdin."""
    worker = config.worker
    return (
        "claude -p "
        "--input-format stream-json "
        "--output-format stream-json "
        "--verbose "
        "--permission-mode bypassPermissions "
        "--permission-prompts none "
        f'--allowedTools "{_allowed_tools(config)}" '
        f"{_mcp_flags(mcp_config)}"
        f"--autocompact {worker.autocompact} "
        f'--model "{worker.model}"'
    )


def _allowed_tools(config: Config) -> str:
    values = [config.worker.allowed_tools]
    values.extend(mcp.allowed_tools(config, "worker"))
    return ",".join(value for value in values if value)


def _mcp_flags(mcp_config: str | None) -> str:
    if not mcp_config:
        return ""
    return f'--mcp-config "{mcp_config}" --strict-mcp-config '


def build_settings(config: Config) -> dict[str, Any]:
    """Build the ``~/.claude/settings.json`` payload for a container.

    Args:
        config: Orchestrator configuration.

    Returns:
        Claude Code settings that pin the context window.
    """
    tokens = autocompact_tokens(config.worker.autocompact)
    if tokens is None:
        return {"autoCompactEnabled": False}
    return {
        "autoCompactEnabled": True,
        "autoCompactWindow": tokens,
        "modelSettings": {config.worker.model: {"autoCompactWindow": tokens}},
        "env": {
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(tokens),
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(tokens),
        },
    }


def settings_json(config: Config) -> str:
    """Serialize container settings deterministically.

    Args:
        config: Orchestrator configuration.

    Returns:
        JSON document written to the container home directory.
    """
    return json.dumps(build_settings(config), ensure_ascii=False, sort_keys=True)

"""Claude Code invocation shared by every agent role."""

from __future__ import annotations

import json
from typing import Any

from orch.schemas.config.types import Config


def autocompact_tokens(value: str) -> int | None:
    """Return the autocompact window in tokens, or ``None`` for auto."""
    if value == "auto":
        return None
    suffix = value.endswith("k")
    digits = value[:-1] if suffix else value
    return int(digits) * (1000 if suffix else 1)


def build_command(config: Config) -> str:
    """Build the Claude Code command for one agent attempt.

    Args:
        config: Orchestrator configuration.

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
        f'--allowedTools "{worker.allowed_tools}" '
        f"--autocompact {worker.autocompact} "
        f'--model "{worker.model}"'
    )


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

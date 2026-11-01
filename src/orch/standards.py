"""Deterministic commit and pull-request standards."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

COMMIT_TYPES = ("feat", "fix", "chore", "docs", "refactor", "test", "style", "perf")
_TITLE_RE = re.compile(
    rf"^({'|'.join(COMMIT_TYPES)})\([a-z0-9][a-z0-9-]*\): .{{1,72}}$"
)
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")

PR_HEADINGS = ("## Что сделано", "## Почему", "## Watch out", "## Pending")


class StandardsError(ValueError):
    """Raised when a worker handoff or generated text violates the standards."""


@dataclass(frozen=True)
class Handoff:
    """Structured result written by the worker."""

    title: str
    what: str
    why: str
    watch_out: str
    pending: str


def load_handoff(run_dir: Path) -> Handoff:
    """Read and validate ``/out/handoff.json`` from a worker run."""
    path = run_dir / "handoff.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as ex:
        raise StandardsError(f"Worker did not write {path}") from ex
    except (OSError, json.JSONDecodeError) as ex:
        logger.bind(
            path=str(path), error=ex, error_type=type(ex).__name__
        ).error("Could not read worker handoff")
        raise StandardsError(f"Could not read worker handoff: {ex}") from ex
    else:
        return _parse_handoff(raw)


def validate_title(title: str) -> str:
    """Return a normalized title or raise when it is not conventional."""
    value = title.strip()
    if "\n" in value or "\r" in value:
        raise StandardsError("Title must be a single line")
    if _CYRILLIC_RE.search(value):
        raise StandardsError("Title must be in English")
    if not _TITLE_RE.fullmatch(value):
        raise StandardsError(
            "Title must match 'type(scope): description' with an allowed type, "
            "a mandatory scope, and a description of 1-72 characters"
        )
    if len(value) > 72:
        raise StandardsError("Title must be at most 72 characters")
    return value


def format_pr_body(handoff: Handoff, issue_number: int) -> str:
    """Build the Russian pull-request body from a validated handoff."""
    what = _required_russian(handoff.what, "what")
    why = _required_russian(handoff.why, "why")
    watch_out = handoff.watch_out.strip() or "—"
    pending = handoff.pending.strip() or "—"
    return (
        f"{PR_HEADINGS[0]}\n{what}\n\n"
        f"{PR_HEADINGS[1]}\n{why}\n\n"
        f"{PR_HEADINGS[2]}\n{watch_out}\n\n"
        f"{PR_HEADINGS[3]}\n{pending}\n\n"
        f"Closes #{issue_number}\n"
    )


def _parse_handoff(raw: Any) -> Handoff:
    if not isinstance(raw, dict):
        raise StandardsError("handoff.json must be a JSON object")
    title = _string_field(raw, "title")
    what = _string_field(raw, "what")
    why = _string_field(raw, "why")
    watch_out = _string_field(raw, "watch_out", allow_empty=True)
    pending = _string_field(raw, "pending", allow_empty=True)
    return Handoff(
        title=validate_title(title),
        what=what,
        why=why,
        watch_out=watch_out,
        pending=pending,
    )


def _string_field(
    raw: dict[str, Any],
    name: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = raw.get(name)
    if not isinstance(value, str):
        raise StandardsError(f"handoff.json field '{name}' must be a string")
    text = value.strip()
    if not text and not allow_empty:
        raise StandardsError(f"handoff.json field '{name}' must not be empty")
    return text


def _required_russian(value: str, name: str) -> str:
    text = value.strip()
    if not _CYRILLIC_RE.search(text):
        raise StandardsError(f"PR field '{name}' must be written in Russian")
    return text

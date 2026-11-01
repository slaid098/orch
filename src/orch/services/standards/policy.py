"""Deterministic issue, branch, commit, and pull-request standards.

This module is the single source of workflow rules. Validators, branch
generation, the host finalizer, and the pull-request builder all use it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from loguru import logger

from orch.schemas.standards.types import Handoff

COMMIT_TYPES = (
    "feat",
    "fix",
    "build",
    "chore",
    "ci",
    "docs",
    "refactor",
    "revert",
    "test",
    "style",
    "perf",
)
TITLE_MAX_LENGTH = 72
PR_HEADINGS = (
    "## Что сделано",
    "## Почему",
    "## На что обратить внимание",
    "## Что осталось",
)

_TITLE_RE = re.compile(
    rf"^({'|'.join(COMMIT_TYPES)})\(([a-z0-9][a-z0-9-]*)\): (.+)$"
)
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")


class StandardsError(ValueError):
    """Raised when a title or worker handoff violates the standards."""


def validate_title(title: str) -> str:
    """Return a normalized title or raise when it is not conventional.

    Args:
        title: Issue title that also becomes the commit and pull-request title.

    Returns:
        The stripped title.

    Raises:
        StandardsError: If the title is not a single-line English conventional
            commit title with a mandatory scope and at most 72 characters.
    """
    value = title.strip()
    if "\n" in value or "\r" in value:
        raise StandardsError("Title must be a single line")
    if _CYRILLIC_RE.search(value):
        raise StandardsError("Title must be in English")
    if len(value) > TITLE_MAX_LENGTH:
        raise StandardsError(
            f"Title must be at most {TITLE_MAX_LENGTH} characters"
        )
    if not _TITLE_RE.fullmatch(value):
        raise StandardsError(
            "Title must match 'type(scope): description' with an allowed type, "
            "a mandatory scope, and a non-empty description"
        )
    return value


def parse_title(title: str) -> tuple[str, str, str]:
    """Split a validated title into ``type``, ``scope``, and ``description``.

    Args:
        title: Issue title.

    Returns:
        A tuple of type, scope, and description.
    """
    value = validate_title(title)
    match = _TITLE_RE.fullmatch(value)
    if match is None:
        raise StandardsError("Title must match 'type(scope): description'")
    return match.group(1), match.group(2), match.group(3).strip()


def slugify(value: str, *, limit: int = 40) -> str:
    """Convert a title description into a branch-safe slug.

    Args:
        value: Human-readable description.
        limit: Maximum slug length.

    Returns:
        A lowercase kebab-case slug.
    """
    slug = _SLUG_INVALID_RE.sub("-", value.lower()).strip("-")
    slug = slug[:limit].rstrip("-")
    if not slug:
        raise StandardsError("Description must contain latin letters or digits")
    return slug


def branch_name(title: str, issue_number: int) -> str:
    """Build the deterministic branch name for an issue.

    Args:
        title: Issue title.
        issue_number: GitHub issue number.

    Returns:
        A branch in ``type/scope/issue-<N>-<slug>`` form.
    """
    commit_type, scope, description = parse_title(title)
    return f"{commit_type}/{scope}/issue-{issue_number}-{slugify(description)}"


def validate_branch(branch: str, issue_number: int) -> str:
    """Validate a branch name against the issue-based format.

    Args:
        branch: Branch name to validate.
        issue_number: Expected issue number.

    Returns:
        The branch name.

    Raises:
        StandardsError: If the branch does not follow the required format.
    """
    pattern = re.compile(
        rf"^({'|'.join(COMMIT_TYPES)})/[a-z0-9][a-z0-9-]*/"
        rf"issue-{issue_number}-[a-z0-9]+(?:-[a-z0-9]+)*$"
    )
    if not pattern.fullmatch(branch):
        raise StandardsError(
            "Branch must match 'type/scope/issue-<N>-<kebab-description>'"
        )
    return branch


def load_handoff(run_dir: Path) -> Handoff:
    """Read and validate ``/out/handoff.json`` from a worker run.

    Args:
        run_dir: Host run directory containing ``handoff.json``.

    Returns:
        Validated worker handoff.

    Raises:
        StandardsError: If the handoff is missing or invalid.
    """
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


def format_pr_body(handoff: Handoff, issue_number: int) -> str:
    """Build the Russian pull-request body from a validated handoff.

    Args:
        handoff: Worker handoff with Russian summary fields.
        issue_number: GitHub issue number.

    Returns:
        Pull-request body with four required sections and a closing link.

    Raises:
        StandardsError: If required fields are empty or not Russian.
    """
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
    return Handoff(
        what=_string_field(raw, "what"),
        why=_string_field(raw, "why"),
        watch_out=_string_field(raw, "watch_out", allow_empty=True),
        pending=_string_field(raw, "pending", allow_empty=True),
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

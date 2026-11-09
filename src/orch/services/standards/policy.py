"""Deterministic issue, branch, commit, and pull-request standards.

This module is the single source of workflow rules. Validators, branch
generation, the host finalizer, and the pull-request builder all use it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from orch.config.logger import get_logger
from orch.constants.standards import (
    COMMIT_TYPES,
    ISSUE_BODY_HEADINGS,
    PR_HEADINGS,
    PR_ISSUE_MARKER,
    TITLE_MAX_LENGTH,
)
from orch.exceptions.standards import StandardsError
from orch.schemas.standards.types import Handoff

log = get_logger(component="standards")

_TITLE_RE = re.compile(
    rf"^({'|'.join(COMMIT_TYPES)})\(([a-z0-9][a-z0-9-]*)\): (.+)$"
)
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")


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
            "a mandatory scope, and a non-empty description. Allowed types: "
            + ", ".join(COMMIT_TYPES)
        )
    return value


def validate_issue_body(body: str) -> str:
    """Validate the fixed eight-section issue contract.

    Args:
        body: Markdown issue body.

    Returns:
        The stripped body.

    Raises:
        StandardsError: If the body is empty, its level-two headings do not
            match the required ordered set, it starts with content before the
            first heading, or any section is empty.
    """
    text = body.strip()
    if not text:
        raise StandardsError("Issue body must not be empty")

    lines = text.splitlines()
    headings = _heading_positions(lines)
    found = [heading for _, heading in headings]
    expected = list(ISSUE_BODY_HEADINGS)
    if found != expected:
        _raise_heading_error(found, expected)
    if "\n".join(lines[: headings[0][0]]).strip():
        raise StandardsError(f"Issue body must start with {expected[0]!r}")

    for index, (position, heading) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(lines)
        if not "\n".join(lines[position + 1 : end]).strip():
            raise StandardsError(f"Issue body section {heading!r} must not be empty")
    return text


def _heading_positions(lines: list[str]) -> list[tuple[int, str]]:
    """Return level-two heading lines outside fenced code blocks."""
    headings: list[tuple[int, str]] = []
    in_fence = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and line.startswith("## "):
            headings.append((index, line.strip()))
    return headings


def _raise_heading_error(found: list[str], expected: list[str]) -> None:
    """Raise a precise error for a heading mismatch."""
    duplicates = sorted({heading for heading in found if found.count(heading) > 1})
    if duplicates:
        raise StandardsError(
            "Issue body contains duplicate headings: "
            + ", ".join(repr(heading) for heading in duplicates)
        )
    missing = [heading for heading in expected if heading not in found]
    if missing:
        raise StandardsError(
            "Issue body is missing headings: "
            + ", ".join(repr(heading) for heading in missing)
        )
    unexpected = [heading for heading in found if heading not in expected]
    if unexpected:
        raise StandardsError(
            "Issue body contains unexpected headings: "
            + ", ".join(repr(heading) for heading in unexpected)
        )
    raise StandardsError(
        "Issue body headings must be exactly: " + ", ".join(expected)
    )


def branch_name(issue_number: int) -> str:
    """Build the stable branch name for an issue.

    Args:
        issue_number: GitHub issue number.

    Returns:
        A branch in ``orch/issue-<N>`` form.
    """
    return f"orch/issue-{issue_number}"


def validate_branch(branch: str, issue_number: int) -> str:
    """Validate a branch name against the new or legacy issue format.

    Args:
        branch: Branch name to validate.
        issue_number: Expected issue number.

    Returns:
        The branch name.

    Raises:
        StandardsError: If the branch does not reference the issue.
    """
    expected = branch_name(issue_number)
    if branch == expected:
        return branch
    legacy = re.compile(
        rf"^({'|'.join(COMMIT_TYPES)})/[a-z0-9][a-z0-9-]*/"
        rf"issue-{issue_number}(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?$"
    )
    if legacy.fullmatch(branch):
        return branch
    raise StandardsError(
        f"Branch must be {expected!r} or a legacy "
        "'type/scope/issue-<N>-<kebab-description>' branch"
    )


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
        log.bind(
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
        f"Closes #{issue_number}\n\n"
        f"{PR_ISSUE_MARKER.format(issue_number=issue_number)}\n"
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

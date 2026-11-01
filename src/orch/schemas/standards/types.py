"""Data types for the worker handoff contract."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Handoff:
    """Structured result written by the worker.

    The issue title is the source of truth for the final commit and pull
    request title, so the worker does not choose it here.
    """

    what: str
    why: str
    watch_out: str
    pending: str

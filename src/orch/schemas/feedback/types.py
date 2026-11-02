"""Live issue-comment feedback schemas."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IncomingComment:
    """One authorized comment ready for injection into the worker."""

    id: int
    author: str
    body: str
    created_at: str

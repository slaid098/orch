"""Reviewer result schema."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewFinding:
    """One mandatory reviewer finding."""

    file: str
    line: int
    title: str
    situation: str
    fixes: tuple[str, ...]


@dataclass(frozen=True)
class ReviewResult:
    """A binary reviewer decision derived from its findings."""

    findings: tuple[ReviewFinding, ...]

    @property
    def approved(self) -> bool:
        """Return whether the reviewer approved the change."""
        return not self.findings

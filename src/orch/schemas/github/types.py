"""GitHub issue schema."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Issue:
    """A GitHub issue reduced to the fields used by the pipeline."""

    number: int
    title: str
    body: str
    url: str
    author: str
    labels: tuple[str, ...]
    state: str = "OPEN"

    def has_label(self, label: str) -> bool:
        """Return whether the issue currently has the given label."""
        return label in self.labels

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Issue:
        """Build an issue from ``gh issue --json`` output."""
        author = data.get("author") or {}
        labels = tuple(
            label["name"]
            for label in data.get("labels", [])
            if isinstance(label, dict) and label.get("name")
        )
        return cls(
            number=int(data["number"]),
            title=str(data["title"]),
            body=str(data.get("body") or ""),
            url=str(data.get("url") or ""),
            author=str(author.get("login") or ""),
            labels=labels,
            state=str(data.get("state") or "OPEN").upper(),
        )


@dataclass(frozen=True)
class Comment:
    """A GitHub issue comment used as live worker feedback."""

    id: int
    author: str
    body: str
    created_at: str
    url: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Comment:
        """Build a comment from ``gh api`` output."""
        user = data.get("user") or {}
        return cls(
            id=int(data["id"]),
            author=str(user.get("login") or ""),
            body=str(data.get("body") or ""),
            created_at=str(data.get("created_at") or ""),
            url=str(data.get("html_url") or ""),
        )

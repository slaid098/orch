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
        )

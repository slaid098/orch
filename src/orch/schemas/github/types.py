"""GitHub issue schema."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Issue:
    """A forge issue reduced to the fields used by the pipeline."""

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

    @classmethod
    def from_forgejo(cls, data: dict[str, Any]) -> Issue:
        """Build an issue from a Forgejo API response."""
        author = data.get("user") or {}
        labels = tuple(
            label["name"]
            for label in data.get("labels", [])
            if isinstance(label, dict) and label.get("name")
        )
        return cls(
            number=int(data["number"]),
            title=str(data["title"]),
            body=str(data.get("body") or ""),
            url=str(data.get("html_url") or data.get("url") or ""),
            author=str(author.get("login") or ""),
            labels=labels,
            state=str(data.get("state") or "open").upper(),
        )


@dataclass(frozen=True)
class CheckResult:
    """One GitHub check or commit-status entry."""

    name: str
    state: str
    link: str = ""


@dataclass(frozen=True)
class PullRequestRef:
    """Minimal identity of an open pull request used for revisions."""

    number: int
    url: str
    head_ref: str


@dataclass(frozen=True)
class PullRequestStatus:
    """The pull-request fields needed by the pipeline gate."""

    number: int
    url: str
    state: str
    mergeable: str
    head_sha: str
    checks: tuple[CheckResult, ...]

    @property
    def failed(self) -> bool:
        """Return whether any check failed."""
        return any(check.state == "failure" for check in self.checks)

    @property
    def pending(self) -> bool:
        """Return whether any check is still running."""
        return any(check.state == "pending" for check in self.checks)


@dataclass(frozen=True)
class Comment:
    """A forge issue comment used as live worker feedback."""

    id: int
    author: str
    body: str
    created_at: str
    url: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Comment:
        """Build a comment from ``gh api`` output."""
        return cls.from_forgejo(data)

    @classmethod
    def from_forgejo(cls, data: dict[str, Any]) -> Comment:
        """Build a comment from a Forgejo API response."""
        user = data.get("user") or {}
        return cls(
            id=int(data["id"]),
            author=str(user.get("login") or ""),
            body=str(data.get("body") or ""),
            created_at=str(data.get("created_at") or ""),
            url=str(data.get("html_url") or ""),
        )

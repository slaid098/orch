"""Pipeline gate failures."""

from __future__ import annotations

from orch.exceptions.github import GitHubError


class ChecksFailed(RuntimeError):
    """A pull request has failing or unavailable GitHub checks."""


class ReviewRejected(RuntimeError):
    """The reviewer returned at least one blocking finding."""


class PublicationError(GitHubError):
    """A publication step failed after the branch was already pushed."""

    def __init__(self, message: str, head_sha: str) -> None:
        super().__init__(message)
        self.head_sha = head_sha

"""Pipeline gate failures."""

from __future__ import annotations


class ChecksFailed(RuntimeError):
    """A pull request has failing or unavailable GitHub checks."""


class ReviewRejected(RuntimeError):
    """The reviewer returned at least one blocking finding."""

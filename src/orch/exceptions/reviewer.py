"""Reviewer failures."""

from __future__ import annotations


class ReviewerError(RuntimeError):
    """The reviewer could not produce a valid, usable result."""

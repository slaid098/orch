"""Watchdog classification and policy schemas."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Classification:
    """One Clef answer about the supervised agent run."""

    state: str
    confidence: float
    probabilities: dict[str, float]
    wait_helpful: float


@dataclass(frozen=True)
class Trigger:
    """Cheap deterministic signal that warrants a classifier check."""

    name: str
    detail: str


@dataclass(frozen=True)
class Verdict:
    """Decision produced by one watchdog check."""

    action: str
    state: str
    confidence: float
    reason: str
    nudge_text: str = ""
    retryable: bool = False

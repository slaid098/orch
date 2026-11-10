"""Skill data types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Skill:
    """One resolved skill directory mounted into an agent container."""

    name: str
    path: Path

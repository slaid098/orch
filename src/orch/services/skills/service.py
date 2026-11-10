"""Skill discovery and validation."""

from __future__ import annotations

from collections.abc import Iterable

from orch.exceptions.skills import SkillsError
from orch.schemas.config.types import Config
from orch.schemas.skills.types import Skill


def resolve(config: Config, names: Iterable[str]) -> tuple[Skill, ...]:
    """Resolve skill names to directories that contain ``SKILL.md``.

    Args:
        config: Orchestrator configuration with the skill root.
        names: Skill directory names selected for one agent role.

    Returns:
        Resolved skills in configuration order; empty when no names were given.

    Raises:
        SkillsError: If a named skill has no ``SKILL.md``.
    """
    selected = _unique(names)
    problems = check_names(config, selected)
    if problems:
        raise SkillsError("; ".join(problems))
    return tuple(
        Skill(name=name, path=(config.skills_dir / name).resolve())
        for name in selected
    )


def check(config: Config) -> list[str]:
    """Return problems for every skill selected by the worker or reviewer."""
    problems: list[str] = []
    problems.extend(_role_problems(config, "Worker", config.worker.skills))
    problems.extend(_role_problems(config, "Reviewer", config.reviewer.skills))
    return problems


def check_names(config: Config, names: Iterable[str]) -> list[str]:
    """Return problems for one role's skill list."""
    problems: list[str] = []
    for name in _unique(names):
        manifest = config.skills_dir / name / "SKILL.md"
        if not manifest.is_file():
            problems.append(
                f"Skill {name!r} is missing: no SKILL.md under {config.skills_dir}"
            )
    return problems


def _role_problems(config: Config, role: str, names: Iterable[str]) -> list[str]:
    return [f"{role}: {problem}" for problem in check_names(config, names)]


def _unique(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(names))

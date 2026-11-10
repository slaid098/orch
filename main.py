"""Production entrypoint for the orch daemon."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from loguru import logger

from orch.config.settings import load_projects
from orch.exceptions.config import ConfigError
from orch.exceptions.instance import InstanceLockError
from orch.schemas.config.types import Config
from orch.services import notify, preflight
from orch.services.daemon import initialize, run_daemon, run_once, setup_logging
from orch.services.instance import instance_lock


def main(argv: list[str] | None = None) -> int:
    """Run the daemon, or one issue in debug mode.

    Args:
        argv: Optional argument list, mainly for tests.

    Returns:
        Process exit code.
    """
    args = _parse_args(argv)
    try:
        projects = load_projects(Path("orch.yaml"))
    except ConfigError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error("Configuration error")
        return 2

    setup_logging(projects[0])
    if args.check:
        return 0 if _preflight(projects, notify_owner=False) else 1
    selected = _selected_project(projects, args.project) if args.once else None
    if args.once and selected is None:
        names = ", ".join(project.name for project in projects)
        logger.error(
            "--project must name exactly one project when several are configured: {}",
            names,
        )
        return 2
    mode = "once" if args.once else "daemon"
    try:
        with instance_lock(projects[0].data_dir, mode=mode):
            if not _preflight(projects, notify_owner=True):
                return 1
            tokens = _initialize_projects(projects)
            if tokens is None:
                return 1
            if selected is not None:
                return run_once(
                    selected,
                    tokens[selected.repo.full_name],
                    args.issue,
                )
            return run_daemon(projects, tokens)
    except InstanceLockError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not start orch: another instance is already running"
        )
        return 1


def _initialize_projects(projects: Sequence[Config]) -> dict[str, str] | None:
    """Resolve the forge token and ensure labels for every project."""
    tokens: dict[str, str] = {}
    for project in projects:
        try:
            tokens[project.repo.full_name] = initialize(project)
        except Exception as ex:
            logger.bind(
                repo=project.repo.full_name,
                error=ex,
                error_type=type(ex).__name__,
            ).error("Could not initialize orch")
            notify.notify_error(project, stage="startup", error=ex)
            return None
    return tokens


def _selected_project(
    projects: Sequence[Config],
    selector: str | None,
) -> Config | None:
    """Resolve the project named by ``--project``."""
    if selector is None:
        return projects[0] if len(projects) == 1 else None
    for project in projects:
        if selector in {project.name, project.repo.name, project.repo.full_name}:
            return project
    return None


def _preflight(projects: Sequence[Config], *, notify_owner: bool) -> bool:
    """Check startup dependencies and report every problem.

    Args:
        projects: Configurations to check.
        notify_owner: Send one Telegram message when checks fail.

    Returns:
        True when no problems were found.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for project in projects:
        for problem in preflight.check(project):
            if problem in seen:
                continue
            seen.add(problem)
            problems.append(
                f"[{project.repo.full_name}] {problem}" if len(projects) > 1 else problem
            )
    if not problems:
        logger.info("Preflight passed")
        return True
    for problem in problems:
        logger.error("Preflight: {}", problem)
    if notify_owner:
        notify.notify_error(
            projects[0],
            stage="preflight",
            error=RuntimeError("; ".join(problems)),
        )
    return False


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="orch",
        description="Issue to pull request orchestrator for GitHub and Forgejo.",
    )
    parser.add_argument("--issue", type=int, help="Issue number for debug mode.")
    parser.add_argument(
        "--project",
        help="Project name for debug mode when several are configured.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one issue and exit instead of starting the daemon.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run startup checks and exit without taking the instance lock.",
    )
    args = parser.parse_args(argv)
    if args.check and (args.once or args.issue is not None):
        parser.error("--check cannot be combined with --issue or --once")
    if args.check and args.project is not None:
        parser.error("--check cannot be combined with --project")
    if args.project is not None and not args.once:
        parser.error("--project requires --issue and --once")
    if args.once != (args.issue is not None):
        parser.error("--issue and --once must be used together")
    return args


if __name__ == "__main__":
    raise SystemExit(main())

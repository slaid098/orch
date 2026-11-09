"""Production entrypoint for the orch daemon."""

from __future__ import annotations

import argparse
from pathlib import Path

from loguru import logger

from orch.config.settings import load_config
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
        config = load_config(Path("orch.yaml"))
    except ConfigError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error("Configuration error")
        return 2

    setup_logging(config)
    if args.check:
        return 0 if _preflight(config, notify_owner=False) else 1
    mode = "once" if args.once else "daemon"
    try:
        with instance_lock(config.data_dir, mode=mode):
            if not _preflight(config, notify_owner=True):
                return 1
            try:
                token = initialize(config)
            except Exception as ex:
                logger.bind(error=ex, error_type=type(ex).__name__).error(
                    "Could not initialize orch"
                )
                notify.notify_error(config, stage="startup", error=ex)
                return 1

            if args.once:
                return run_once(config, token, args.issue)
            return run_daemon(config, token)
    except InstanceLockError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not start orch: another instance is already running"
        )
        return 1


def _preflight(config: Config, *, notify_owner: bool) -> bool:
    """Check startup dependencies and report every problem.

    Args:
        config: Orchestrator configuration.
        notify_owner: Send one Telegram message when checks fail.

    Returns:
        True when no problems were found.
    """
    problems = preflight.check(config)
    if not problems:
        logger.info("Preflight passed")
        return True
    for problem in problems:
        logger.error("Preflight: {}", problem)
    if notify_owner:
        notify.notify_error(
            config,
            stage="preflight",
            error=RuntimeError("; ".join(problems)),
        )
    return False


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="orch",
        description="GitHub issue to pull request orchestrator.",
    )
    parser.add_argument("--issue", type=int, help="Issue number for debug mode.")
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
    if args.once != (args.issue is not None):
        parser.error("--issue and --once must be used together")
    return args


if __name__ == "__main__":
    raise SystemExit(main())

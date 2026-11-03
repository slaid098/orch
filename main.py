"""Production entrypoint for the orch daemon."""

from __future__ import annotations

import argparse
from pathlib import Path

from loguru import logger

from orch.config.settings import load_config
from orch.exceptions.config import ConfigError
from orch.services import notify
from orch.services.daemon import initialize, run_daemon, run_once, setup_logging


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
    try:
        token = initialize(config)
    except Exception as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error("Could not initialize orch")
        notify.notify_error(config, stage="startup", error=ex)
        return 1

    if args.once:
        return run_once(config, token, args.issue)
    return run_daemon(config, token)


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
    args = parser.parse_args(argv)
    if args.once != (args.issue is not None):
        parser.error("--issue and --once must be used together")
    return args


if __name__ == "__main__":
    raise SystemExit(main())

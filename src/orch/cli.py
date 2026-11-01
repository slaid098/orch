"""Command line interface for orch."""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from loguru import logger

from orch import __version__, github, store
from orch.config import Config, ConfigError, load_config
from orch.docker import DockerError, build_image, cleanup_managed
from orch.pipeline import RunOutcome, run_issue


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Optional argument list, mainly for tests.

    Returns:
        Process exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config))
    except ConfigError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Configuration error"
        )
        return 2

    _setup_logging(config)

    if args.command == "build-image":
        return _build_image(config)
    if args.command == "status":
        return _status(config)
    if args.command == "logs":
        return _logs(config, args.issue)

    try:
        token = github.resolve_token(config.repo.owner, config.gh_token)
        github.ensure_user(token, config.repo.owner)
        github.ensure_labels(
            config.repo.full_name,
            token,
            [
                (config.labels.queued, "0e8a16", "Queued for orch"),
                (config.labels.running, "fbca04", "orch is working on this issue"),
                (
                    config.labels.needs_human,
                    "d73a4a",
                    "orch exhausted its attempts; a human must decide",
                ),
                (config.labels.pr_open, "1d76db", "orch opened a pull request"),
            ],
        )
        store.init_db(config.data_dir / "orch.db")
    except (github.GitHubError, DockerError) as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not initialize orch"
        )
        return 1

    if args.command == "run":
        return _run_once(config, token, args.issue)
    if args.command == "daemon":
        return _daemon(config, token)

    parser.error(f"Unknown command: {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orch",
        description="GitHub issue to pull request orchestrator.",
    )
    parser.add_argument(
        "--config",
        default="orch.yaml",
        help="Path to the configuration file (default: orch.yaml).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show the orch version and exit.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one issue synchronously.")
    run_parser.add_argument("--issue", type=int, required=True)

    subparsers.add_parser("daemon", help="Poll for ready issues.")
    subparsers.add_parser("status", help="Show recent runs.")
    subparsers.add_parser("build-image", help="Build the worker Docker image.")

    logs_parser = subparsers.add_parser("logs", help="Show the latest run logs.")
    logs_parser.add_argument("--issue", type=int, required=True)
    return parser


def _setup_logging(config: Config) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    )
    logger.add(
        config.data_dir / "orch.log",
        level="DEBUG",
        rotation="10 MB",
        retention=10,
        encoding="utf-8",
    )


def _build_image(config: Config) -> int:
    dockerfile = config.repo.path / "docker" / "agent.Dockerfile"
    if not dockerfile.exists():
        dockerfile = Path(__file__).resolve().parents[2] / "docker" / "agent.Dockerfile"
    context = dockerfile.parent.parent
    try:
        build_image(config.worker.image, dockerfile, context)
    except DockerError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not build worker image"
        )
        return 1
    return 0


def _run_once(config: Config, token: str, issue_number: int) -> int:
    try:
        issue = github.get_issue(config.repo.full_name, issue_number, token)
    except github.GitHubError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not fetch issue"
        )
        return 1

    outcome = run_issue(config, issue, token)
    if outcome.status == "pr_open":
        print(f"PR opened: {outcome.pr_url}")
        print(f"Run directory: {outcome.run_dir}")
        return 0

    print(f"Needs human: {outcome.error}")
    print(f"Run directory: {outcome.run_dir}")
    return 1


def _daemon(config: Config, token: str) -> int:
    logger.info(
        "Daemon started for {} with {} worker slot(s)",
        config.repo.full_name,
        config.max_workers,
    )
    _reconcile_startup(config, token)
    db_path = config.data_dir / "orch.db"
    active: dict[int, Future[RunOutcome]] = {}

    with ThreadPoolExecutor(
        max_workers=config.max_workers,
        thread_name_prefix="orch",
    ) as executor:
        try:
            while True:
                _reap_finished(active)

                try:
                    issues = github.list_ready_issues(
                        config.repo.full_name,
                        config.labels.queued,
                        token,
                    )
                except github.GitHubError as ex:
                    logger.bind(error=ex, error_type=type(ex).__name__).error(
                        "Poll failed; retrying"
                    )
                    time.sleep(config.poll_interval_seconds)
                    continue

                for issue in issues:
                    if len(active) >= config.max_workers:
                        break
                    if issue.number in active:
                        continue
                    if store.has_active_run(
                        db_path,
                        config.repo.full_name,
                        issue.number,
                    ):
                        continue

                    if not _claim_issue(config, issue, token):
                        continue
                    logger.info("Claiming issue #{}: {}", issue.number, issue.title)
                    active[issue.number] = executor.submit(
                        run_issue,
                        config,
                        issue,
                        token,
                        claim=False,
                    )

                time.sleep(config.poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Daemon stopping; waiting for active workers")
            return 0


def _reap_finished(active: dict[int, Future[RunOutcome]]) -> None:
    for issue_number, future in list(active.items()):
        if not future.done():
            continue
        try:
            outcome = future.result()
        except Exception as ex:
            logger.bind(
                issue=issue_number,
                error=ex,
                error_type=type(ex).__name__,
            ).error("Unhandled issue worker failure")
        else:
            logger.info(
                "Issue #{} finished with status {}",
                issue_number,
                outcome.status,
            )
        del active[issue_number]


def _claim_issue(config: Config, issue: github.Issue, token: str) -> bool:
    try:
        github.edit_labels(
            config.repo.full_name,
            issue.number,
            token,
            add=(config.labels.running,),
            remove=(config.labels.queued, config.labels.needs_human),
        )
    except github.GitHubError as ex:
        logger.bind(
            issue=issue.number,
            error=ex,
            error_type=type(ex).__name__,
        ).error("Could not claim issue")
        return False
    else:
        return True


def _reconcile_startup(config: Config, token: str) -> None:
    cleanup_managed()
    interrupted = store.interrupt_active_runs(
        config.data_dir / "orch.db",
        config.repo.full_name,
    )
    if interrupted:
        logger.warning("Marked {} interrupted run(s) as interrupted", interrupted)

    try:
        issues = github.list_ready_issues(
            config.repo.full_name,
            config.labels.running,
            token,
        )
    except github.GitHubError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).warning(
            "Could not reconcile running issues"
        )
        return

    for issue in issues:
        try:
            github.comment_issue(
                config.repo.full_name,
                issue.number,
                token,
                "orch был перезапущен во время работы. Issue возвращена в очередь.",
            )
            github.edit_labels(
                config.repo.full_name,
                issue.number,
                token,
                add=(config.labels.queued,),
                remove=(config.labels.running, config.labels.needs_human),
            )
        except github.GitHubError as ex:
            logger.bind(
                issue=issue.number,
                error=ex,
                error_type=type(ex).__name__,
            ).error("Could not requeue issue after restart")
        else:
            logger.info("Requeued issue #{} after daemon restart", issue.number)


def _status(config: Config) -> int:
    db_path = config.data_dir / "orch.db"
    if not db_path.exists():
        print("No runs yet.")
        return 0

    runs = store.list_runs(db_path)
    if not runs:
        print("No runs yet.")
        return 0

    for run in runs:
        error_lines = (run.get("error") or "").strip().splitlines()
        error = error_lines[0][:80] if error_lines else ""
        print(
            f"{run['started_at'][:19]}  #{run['issue']:<4}  {run['status']:<12}  "
            f"{run['branch']:<12}  {run.get('pr_url') or '-'}  {error}"
        )
    return 0


def _logs(config: Config, issue_number: int) -> int:
    db_path = config.data_dir / "orch.db"
    if not db_path.exists():
        print("No runs yet.")
        return 0

    run = store.latest_run_for_issue(db_path, config.repo.full_name, issue_number)
    if not run:
        print(f"No runs for issue #{issue_number}.")
        return 0

    run_dir = _find_run_dir(config, issue_number, run["id"])
    attempt_dirs = sorted(run_dir.glob("attempt-*"))
    display_dir = attempt_dirs[-1] if attempt_dirs else run_dir
    print(f"Run: {run['id']}")
    print(f"Status: {run['status']}")
    print(f"Directory: {run_dir}")
    for name in ("tests.txt", "events.stderr.log"):
        path = display_dir / name
        if path.exists():
            print(f"\n--- {name} (tail) ---")
            print(_tail(path.read_text(encoding="utf-8", errors="replace"), 4000))
    return 0


def _find_run_dir(config: Config, issue: int, run_id: str) -> Path:
    return (
        config.data_dir
        / "runs"
        / f"{config.repo.owner}__{config.repo.name}"
        / f"issue-{issue}"
        / run_id
    )


def _tail(text: str, limit: int) -> str:
    return text[-limit:] if len(text) > limit else text

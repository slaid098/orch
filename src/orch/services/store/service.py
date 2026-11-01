"""SQLite-backed run state."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from orch.config.logger import get_logger

log = get_logger(component="store")


def init_db(db_path: Path) -> None:
    """Create the run-state database if it does not exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                issue INTEGER NOT NULL,
                status TEXT NOT NULL,
                branch TEXT NOT NULL,
                pr_url TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_issue "
            "ON runs(repo, issue, status)"
        )
        conn.commit()


def create_run(
    db_path: Path,
    *,
    run_id: str,
    repo: str,
    issue: int,
    branch: str,
) -> None:
    """Insert a new running run."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO runs (id, repo, issue, status, branch, started_at)
            VALUES (?, ?, ?, 'running', ?, ?)
            """,
            (run_id, repo, issue, branch, _now()),
        )
        conn.commit()
    log.info("Created run {} for issue #{}", run_id, issue)


def finish_run(
    db_path: Path,
    *,
    run_id: str,
    status: str,
    pr_url: str | None = None,
    error: str | None = None,
) -> None:
    """Mark a run as finished."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            UPDATE runs
            SET status = ?, pr_url = ?, finished_at = ?, error = ?
            WHERE id = ?
            """,
            (status, pr_url, _now(), error, run_id),
        )
        conn.commit()
    log.info("Finished run {} with status {}", run_id, status)


def has_active_run(db_path: Path, repo: str, issue: int) -> bool:
    """Return whether a running run already exists for the issue."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE repo = ? AND issue = ? AND status = 'running' LIMIT 1",
            (repo, issue),
        ).fetchone()
    return row is not None


def interrupt_active_runs(db_path: Path, repo: str) -> int:
    """Mark all running runs for a repository as interrupted.

    Returns:
        Number of runs changed.
    """
    with closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            """
            UPDATE runs
            SET status = 'interrupted', finished_at = ?, error = ?
            WHERE repo = ? AND status = 'running'
            """,
            (_now(), "orch restarted during the run", repo),
        )
        conn.commit()
        count = cursor.rowcount
    if count:
        log.warning("Interrupted {} active run(s) for {}", count, repo)
    return count


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat()

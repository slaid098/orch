"""SQLite-backed run state."""

from __future__ import annotations

from contextlib import closing
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger


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
    logger.info("Created run {} for issue #{}", run_id, issue)


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
    logger.info("Finished run {} with status {}", run_id, status)


def has_active_run(db_path: Path, repo: str, issue: int) -> bool:
    """Return whether a running run already exists for the issue."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE repo = ? AND issue = ? AND status = 'running' LIMIT 1",
            (repo, issue),
        ).fetchone()
    return row is not None


def list_runs(db_path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent runs, newest first."""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def latest_run_for_issue(
    db_path: Path,
    repo: str,
    issue: int,
) -> dict[str, Any] | None:
    """Return the latest run for an issue, if any."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT * FROM runs
            WHERE repo = ? AND issue = ?
            ORDER BY started_at DESC LIMIT 1
            """,
            (repo, issue),
        ).fetchone()
    return dict(row) if row else None


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

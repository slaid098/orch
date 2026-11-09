from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orch.services import store


def test_run_lifecycle(tmp_path: Path) -> None:
    db_path = tmp_path / "orch.db"
    store.init_db(db_path)

    store.create_run(
        db_path,
        run_id="run-1",
        repo="slaid098/orch",
        issue=1,
        branch="agent/1",
    )

    assert store.has_active_run(db_path, "slaid098/orch", 1)
    assert not store.has_active_run(db_path, "slaid098/orch", 2)

    store.finish_run(
        db_path,
        run_id="run-1",
        status="pr_open",
        pr_url="https://example.test/pr/1",
    )

    assert not store.has_active_run(db_path, "slaid098/orch", 1)


def test_interrupt_active_runs(tmp_path: Path) -> None:
    db_path = tmp_path / "orch.db"
    store.init_db(db_path)
    store.create_run(
        db_path,
        run_id="run-1",
        repo="slaid098/orch",
        issue=1,
        branch="agent/1",
    )

    assert store.interrupt_active_runs(db_path, "slaid098/orch") == 1
    assert not store.has_active_run(db_path, "slaid098/orch", 1)
    assert store.interrupt_active_runs(db_path, "slaid098/orch") == 0


def test_prune_old_runs_removes_only_expired_finished_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "orch.db"
    store.init_db(db_path)
    for run_id, issue in (("old", 1), ("fresh", 2), ("running", 3)):
        store.create_run(
            db_path,
            run_id=run_id,
            repo="slaid098/orch",
            issue=issue,
            branch=f"orch/issue-{issue}",
        )
    store.finish_run(db_path, run_id="old", status="pr_open")
    store.finish_run(db_path, run_id="fresh", status="pr_open")
    old_stamp = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE runs SET finished_at = ? WHERE id = 'old'", (old_stamp,))

    assert store.prune_old_runs(db_path, 7) == 1

    with sqlite3.connect(db_path) as conn:
        remaining = {
            row[0] for row in conn.execute("SELECT id FROM runs").fetchall()
        }
    assert remaining == {"fresh", "running"}


def test_prune_old_runs_ignores_a_missing_database(tmp_path: Path) -> None:
    assert store.prune_old_runs(tmp_path / "missing.db", 7) == 0
    assert not (tmp_path / "missing.db").exists()

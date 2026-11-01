from __future__ import annotations

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

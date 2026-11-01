from __future__ import annotations

from pathlib import Path

from orch import store


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
    runs = store.list_runs(db_path)
    assert len(runs) == 1
    assert runs[0]["status"] == "pr_open"
    assert runs[0]["pr_url"] == "https://example.test/pr/1"

    latest = store.latest_run_for_issue(db_path, "slaid098/orch", 1)
    assert latest is not None
    assert latest["id"] == "run-1"


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

    run = store.latest_run_for_issue(db_path, "slaid098/orch", 1)
    assert run is not None
    assert run["status"] == "interrupted"

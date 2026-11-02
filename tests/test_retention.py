from __future__ import annotations

import os
import time
from pathlib import Path

from orch.services.retention import prune_runs


def _run_dir(root: Path, repo: str, issue: str, run: str) -> Path:
    path = root / "runs" / repo / issue / run
    path.mkdir(parents=True)
    (path / "events.jsonl").write_text("{}", encoding="utf-8")
    return path


def _age(path: Path, days: int) -> None:
    stamp = time.time() - days * 86_400
    os.utime(path, (stamp, stamp))


def test_prune_runs_removes_expired_runs_and_empty_parents(tmp_path: Path) -> None:
    data_dir = tmp_path / "app_data"
    expired = _run_dir(data_dir, "o__r", "issue-1", "run-old")
    fresh = _run_dir(data_dir, "o__r", "issue-2", "run-fresh")
    _age(expired, 10)

    removed = prune_runs(data_dir, 7)

    assert removed == 1
    assert not expired.exists()
    assert not (data_dir / "runs" / "o__r" / "issue-1").exists()
    assert fresh.exists()


def test_prune_runs_keeps_issue_directory_with_fresh_runs(tmp_path: Path) -> None:
    data_dir = tmp_path / "app_data"
    expired = _run_dir(data_dir, "o__r", "issue-1", "run-old")
    fresh = _run_dir(data_dir, "o__r", "issue-1", "run-fresh")
    _age(expired, 30)

    assert prune_runs(data_dir, 7) == 1
    assert (data_dir / "runs" / "o__r" / "issue-1").exists()
    assert fresh.exists()


def test_prune_runs_ignores_missing_directory(tmp_path: Path) -> None:
    assert prune_runs(tmp_path / "missing", 7) == 0


def test_prune_runs_ignores_stray_files(tmp_path: Path) -> None:
    runs = tmp_path / "app_data" / "runs"
    runs.mkdir(parents=True)
    (runs / "notes.txt").write_text("x", encoding="utf-8")

    assert prune_runs(tmp_path / "app_data", 7) == 0
    assert (runs / "notes.txt").exists()

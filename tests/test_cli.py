from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import orch
from orch import store
from orch.cli import _status, main
from orch.config import Config, LabelsConfig, RepoConfig, WorkerConfig


def test_version_flag_prints_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"orch {orch.__version__}"


def test_version_flag_does_not_require_a_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == "orch 0.1.0"


def test_version_flag_via_module() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "orch", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == f"orch {orch.__version__}"


def test_help_lists_existing_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for command in ("run", "daemon", "status", "build-image", "logs"):
        assert command in out
    assert "--version" in out


def test_command_is_still_required() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])

    assert excinfo.value.code == 2


def test_status_handles_a_successful_run_without_an_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
    )
    db_path = config.data_dir / "orch.db"
    store.init_db(db_path)
    store.create_run(
        db_path,
        run_id="run-1",
        repo="slaid098/orch",
        issue=1,
        branch="agent/1",
    )
    store.finish_run(
        db_path,
        run_id="run-1",
        status="pr_open",
        pr_url="https://example.test/pr/1",
    )

    assert _status(config) == 0
    out = capsys.readouterr().out
    assert "pr_open" in out
    assert "https://example.test/pr/1" in out

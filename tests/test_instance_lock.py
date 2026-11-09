"""Tests for the single-instance data-directory lock."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orch.exceptions.instance import InstanceLockError
from orch.services.instance import instance_lock
from orch.services.instance import service as instance_service

_HOLDER = """
import sys
import time
from pathlib import Path

from orch.services.instance import instance_lock

data_dir = Path(sys.argv[1])
ready = Path(sys.argv[2])

with instance_lock(data_dir, mode="holder"):
    ready.write_text("ready", encoding="utf-8")
    time.sleep(60)
"""


def test_lock_writes_metadata_and_can_be_reacquired(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    with instance_lock(data_dir, mode="daemon"):
        pass
    metadata = (data_dir / "orch.lock").read_text(encoding="utf-8")
    assert f"pid={os.getpid()}" in metadata
    assert "mode=daemon" in metadata

    with instance_lock(data_dir, mode="once"):
        pass
    metadata = (data_dir / "orch.lock").read_text(encoding="utf-8")
    assert "mode=once" in metadata


def test_second_process_is_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    ready = tmp_path / "ready"
    process = _start_holder(data_dir, ready)
    try:
        with (
            pytest.raises(InstanceLockError) as excinfo,
            instance_lock(data_dir, mode="test"),
        ):
            pytest.fail("the lock must not be acquired twice")
    finally:
        process.terminate()
        process.wait(timeout=10)

    message = str(excinfo.value)
    assert "pid=" in message
    assert "mode=holder" in message
    assert str(data_dir / "orch.lock") in message


def test_existing_lock_file_can_be_acquired(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "orch.lock").write_bytes(b"\0")

    with instance_lock(data_dir, mode="daemon"):
        pass


def test_metadata_failure_does_not_break_the_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"

    def fail(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(instance_service, "_write_metadata", fail)

    with instance_lock(data_dir, mode="daemon"):
        pass

    with instance_lock(data_dir, mode="once"):
        pass


def _start_holder(data_dir: Path, ready: Path) -> subprocess.Popen[bytes]:
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    python_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repo_root / "src"), python_path) if part
    )
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(data_dir), str(ready)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while not ready.exists():
        if process.poll() is not None:
            _, stderr = process.communicate(timeout=5)
            raise AssertionError(
                "lock holder exited before acquiring the lock: "
                + stderr.decode("utf-8", "replace")
            )
        if time.monotonic() >= deadline:
            process.terminate()
            process.wait(timeout=10)
            raise AssertionError("lock holder did not acquire the lock in time")
        time.sleep(0.05)
    return process

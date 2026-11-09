"""Single-instance lock for the orch data directory."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from orch.config.logger import get_logger
from orch.exceptions.instance import InstanceLockError

log = get_logger(component="instance")

_LOCK_NAME = "orch.lock"
_METADATA_OFFSET = 1


@contextmanager
def instance_lock(data_dir: Path, *, mode: str) -> Iterator[None]:
    """Hold an exclusive process lock for the lifetime of the context.

    The lock is released by the operating system even when the process crashes,
    so a stale lock file never blocks a new start. Only one orch process per
    data directory may hold the lock.

    Args:
        data_dir: Directory containing the lock and all orchestrator state.
        mode: Process mode written to the lock metadata, for diagnostics.

    Raises:
        InstanceLockError: If another process already holds the lock.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / _LOCK_NAME
    path.touch(exist_ok=True)
    with path.open("r+b") as handle:
        _ensure_lock_byte(handle)
        try:
            _acquire(handle)
        except OSError as ex:
            holder = _read_holder(handle)
            raise InstanceLockError(_busy_message(path, holder), holder=holder) from ex
        try:
            try:
                _write_metadata(handle, mode)
            except OSError as ex:
                log.bind(error=ex, error_type=type(ex).__name__).warning(
                    "Could not write instance lock metadata"
                )
            log.info("Acquired instance lock {}", path)
            yield
        finally:
            _release(handle)
            log.info("Released instance lock {}", path)


def _ensure_lock_byte(handle: BinaryIO) -> None:
    """Make sure the file has at least one byte available for locking."""
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()


def _acquire(handle: BinaryIO) -> None:
    """Take a non-blocking exclusive lock on the first byte of the file."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(handle: BinaryIO) -> None:
    """Release the lock, ignoring an already-closed or already-released handle."""
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).warning(
            "Could not release instance lock explicitly; the operating system "
            "will release it when the process exits"
        )


def _write_metadata(handle: BinaryIO, mode: str) -> None:
    """Replace the holder metadata without touching the locked byte."""
    payload = (
        f"pid={os.getpid()}\n"
        f"started_at={datetime.now(UTC).isoformat(timespec='seconds')}\n"
        f"mode={mode}\n"
    ).encode()
    handle.seek(_METADATA_OFFSET)
    handle.truncate()
    handle.write(payload)
    handle.flush()


def _read_holder(handle: BinaryIO) -> str:
    """Read metadata written by the current lock holder, if it is available."""
    try:
        handle.seek(_METADATA_OFFSET)
        return handle.read(4096).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _busy_message(path: Path, holder: str) -> str:
    """Build a diagnostic message for a busy lock."""
    detail = ""
    if holder:
        detail = " (" + ", ".join(holder.splitlines()) + ")"
    return (
        f"Another orch instance already holds {path}{detail}. "
        "Stop the running process before starting a new one."
    )

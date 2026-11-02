from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from orch.exceptions.worker import AgentError
from orch.schemas.config.types import Config, LabelsConfig, RepoConfig, WorkerConfig
from orch.schemas.github.types import Issue
from orch.schemas.standards.types import Handoff
from orch.services.worker import service as worker


def _config(tmp_path: Path, *, attempt: int = 1) -> Config:
    del attempt
    return Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(
            image="image:latest",
            model="model",
            timeout_minutes=1,
            max_attempts=2,
            setup_commands=("uv sync",),
            test_commands=("pytest",),
        ),
        data_dir=tmp_path / "data",
    )


def _issue() -> Issue:
    return Issue(
        number=7,
        title="feat(cli): add version flag",
        body="Body",
        url="https://example.test/7",
        author="slaid098",
        labels=("agent:queued",),
    )


def _handoff() -> Handoff:
    return Handoff(
        what="Добавлен флаг версии.",
        why="Пользователю нужен способ проверить версию.",
        watch_out="—",
        pending="—",
    )


def _proc(stdout: str = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], code, stdout, stderr)


def test_run_worker_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_dir = tmp_path / "runs" / "issue-7"
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    starts: list[dict[str, object]] = []
    stopped: list[str] = []
    commands: list[str] = []
    container = worker._container_name(run_dir)

    monkeypatch.setattr(
        worker,
        "start_worker",
        lambda **kwargs: starts.append(kwargs) or "container",
    )
    monkeypatch.setattr(worker, "stop", lambda name: stopped.append(name))
    monkeypatch.setattr(worker, "load_handoff", lambda _run_dir: _handoff())

    def fake_exec(
        name: str,
        command: str,
        *,
        timeout: int,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert name == container
        assert timeout > 0
        if "printf" in command:
            return _proc("base123\n")
        if "git status --porcelain" in command:
            return _proc(" M file.py\n")
        if "git rev-list" in command:
            return _proc("0\n")
        if "git log -1" in command:
            return _proc("feat(cli): add version flag\n")
        if "git bundle create" in command:
            (run_dir / "result.bundle").write_text("bundle", encoding="utf-8")
        if stdout_path is not None:
            stdout_path.write_text("", encoding="utf-8")
        if stderr_path is not None:
            stderr_path.write_text("", encoding="utf-8")
        return _proc()

    monkeypatch.setattr(worker, "exec_in", fake_exec)

    result = worker.run_worker(
        config,
        _issue(),
        run_dir,
        source_path=source,
        workspace_path=workspace,
        attempt=2,
        previous_error="first failed",
        timeout_seconds=60,
    )

    assert result.branch == "feat/cli/issue-7-add-version-flag"
    assert result.handoff == _handoff()
    assert result.bundle_path == run_dir / "result.bundle"
    assert starts[0]["image"] == "image:latest"
    assert stopped == [container]
    assert (run_dir / "prompt.md").exists()
    assert (run_dir / "commit-message.txt").read_text(encoding="utf-8").strip() == (
        "feat(cli): add version flag"
    )
    assert any("uv sync" in command for command in commands)
    assert any("pytest" in command for command in commands)


def test_run_worker_rejects_no_changes_and_stops_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_dir = tmp_path / "runs" / "issue-7"
    source = tmp_path / "source"
    source.mkdir()
    stopped: list[str] = []
    container = worker._container_name(run_dir)

    monkeypatch.setattr(worker, "start_worker", lambda **_kwargs: "container")
    monkeypatch.setattr(worker, "stop", lambda name: stopped.append(name))

    def fake_exec(
        _name: str,
        command: str,
        *,
        timeout: int,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout, stdout_path, stderr_path
        if "printf" in command:
            return _proc("base123\n")
        return _proc("")

    monkeypatch.setattr(worker, "exec_in", fake_exec)

    with pytest.raises(AgentError, match="no changes"):
        worker.run_worker(
            config,
            _issue(),
            run_dir,
            source_path=source,
            workspace_path=tmp_path / "workspace",
            timeout_seconds=60,
        )

    assert stopped == [container]


def test_run_worker_reports_failed_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_dir = tmp_path / "runs" / "issue-7"
    source = tmp_path / "source"
    source.mkdir()

    monkeypatch.setattr(worker, "start_worker", lambda **_kwargs: "container")
    monkeypatch.setattr(worker, "stop", lambda _name: None)
    monkeypatch.setattr(worker, "load_handoff", lambda _run_dir: _handoff())

    def fake_exec(
        _name: str,
        command: str,
        *,
        timeout: int,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout, stdout_path, stderr_path
        if "printf" in command:
            return _proc("base123\n")
        if "git status --porcelain" in command:
            return _proc(" M file.py\n")
        if "git rev-list" in command:
            return _proc("1\n")
        if "git log -1" in command:
            return _proc("feat(cli): add version flag\n")
        if "/out/tests.txt" in command:
            (run_dir / "tests.txt").write_text("failed\n", encoding="utf-8")
            return _proc(code=1)
        if "git bundle create" in command:
            (run_dir / "result.bundle").write_text("bundle", encoding="utf-8")
        return _proc()

    monkeypatch.setattr(worker, "exec_in", fake_exec)

    with pytest.raises(AgentError, match="Tests failed"):
        worker.run_worker(
            config,
            _issue(),
            run_dir,
            source_path=source,
            workspace_path=tmp_path / "workspace",
            timeout_seconds=60,
        )


def test_prepare_workspace_requires_base_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "exec_in", lambda *_args, **_kwargs: _proc("\n"))

    with pytest.raises(AgentError, match="base commit"):
        worker._prepare_workspace("container", "branch", time.monotonic() + 30)


def test_prepare_workspace_reclones_damaged_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    def fake_exec(
        _name: str,
        command: str,
        *,
        timeout: int,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout, stdout_path, stderr_path
        commands.append(command)
        return _proc("base123\n")

    monkeypatch.setattr(worker, "exec_in", fake_exec)

    worker._prepare_workspace("container", "branch", time.monotonic() + 30)

    assert len(commands) == 1
    assert "! -d /work/.git" in commands[0]
    assert "find /work -mindepth 1 -maxdepth 1 -exec rm -rf {} +" in commands[0]
    assert "git clone /src /work" in commands[0]


def test_run_setup_skips_empty_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("exec must not run")

    monkeypatch.setattr(worker, "exec_in", fail)
    worker._run_setup(
        "container",
        Config(
            repo=RepoConfig(owner="o", name="n", path=Path(".")),
            labels=LabelsConfig(),
            worker=WorkerConfig(test_commands=("pytest",)),
            data_dir=Path("data"),
        ),
        time.monotonic() + 30,
    )


def test_run_claude_logs_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "exec_in",
        lambda *_args, **_kwargs: _proc(stderr="failed", code=1),
    )

    worker._run_claude(
        "container",
        _config(Path(".")),
        Path("run"),
        time.monotonic() + 30,
    )


def test_write_diff_continues_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "exec_in",
        lambda *_args, **_kwargs: _proc(stderr="failed", code=1),
    )

    worker._write_diff("container", "base", Path("run"), time.monotonic() + 30)


def test_finalize_commit_rejects_wrong_subject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "exec_in",
        lambda *_args, **_kwargs: _proc("wrong message\n"),
    )

    with pytest.raises(AgentError, match="does not match"):
        worker._finalize_commit(
            "container",
            "base",
            "feat(cli): add version flag",
            tmp_path,
            time.monotonic() + 30,
        )


def test_bundle_requires_a_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "exec_in", lambda *_args, **_kwargs: _proc())

    with pytest.raises(AgentError, match="missing or empty"):
        worker._bundle("container", "branch", tmp_path, time.monotonic() + 30)


def test_remaining_rejects_expired_deadline() -> None:
    with pytest.raises(AgentError, match="timed out"):
        worker._remaining(time.monotonic() - 1, cap=10)


def test_remaining_returns_at_least_one_second() -> None:
    assert worker._remaining(time.monotonic() + 0.1, cap=10) == 1


def test_exec_or_raise_reports_command_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "exec_in",
        lambda *_args, **_kwargs: _proc(stderr="details", code=1),
    )

    with pytest.raises(AgentError, match="bundle failed"):
        worker._exec_or_raise(
            "container",
            "git bundle",
            timeout=10,
            label="bundle",
        )


def test_checkpoint_saves_patch_status_and_untracked_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    def fake_exec(
        _name: str,
        command: str,
        *,
        timeout: int,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout, stdout_path, stderr_path
        commands.append(command)
        return _proc()

    monkeypatch.setattr(worker, "exec_in", fake_exec)

    worker._checkpoint(
        "container",
        "base123",
        tmp_path,
        time.monotonic() + 30,
    )

    assert len(commands) == 1
    assert "checkpoint.patch" in commands[0]
    assert "checkpoint.status" in commands[0]
    assert "checkpoint-untracked.tar.gz" in commands[0]


def test_checkpoint_failure_does_not_mask_attempt_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("container is gone")

    monkeypatch.setattr(worker, "exec_in", fail)

    worker._checkpoint(
        "container",
        "base123",
        tmp_path,
        time.monotonic() + 30,
    )


def test_checkpoint_logs_non_zero_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "exec_in",
        lambda *_args, **_kwargs: _proc(stderr="tar failed", code=1),
    )

    worker._checkpoint(
        "container",
        "base123",
        tmp_path,
        time.monotonic() + 30,
    )


def test_container_name_is_safe() -> None:
    name = worker._container_name(Path("Issue 7") / "attempt 1")

    assert name == "orch-issue-7-attempt-1"

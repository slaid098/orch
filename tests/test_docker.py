from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orch.services.docker import service as docker


def _proc(
    stdout: str = "",
    stderr: str = "",
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], code, stdout, stderr)


def test_start_worker_requires_an_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker, "_image_exists", lambda _image: False)

    with pytest.raises(docker.DockerError, match="does not exist"):
        docker.start_worker(
            image="missing:latest",
            name="worker",
            repo_path=tmp_path,
            workspace_path=tmp_path / "work",
            run_dir=tmp_path / "out",
            model="model",
        )


def test_start_worker_runs_a_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args: list[str] = []
    monkeypatch.setattr(docker, "_image_exists", lambda _image: True)
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: args.extend(command) or _proc("abc123\n"),
    )

    container = docker.start_worker(
        image="image:latest",
        name="worker",
        repo_path=tmp_path,
        workspace_path=tmp_path / "work",
        run_dir=tmp_path / "out",
        model="model",
    )

    assert container == "abc123"
    assert args[:3] == ["docker", "run", "-d"]
    assert "image:latest" in args
    assert "host.docker.internal:host-gateway" in args
    assert (tmp_path / "work").is_dir()


def test_start_worker_rejects_empty_container_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker, "_image_exists", lambda _image: True)
    monkeypatch.setattr(docker, "_run", lambda *_args, **_kwargs: _proc(""))

    with pytest.raises(docker.DockerError, match="no container id"):
        docker.start_worker(
            image="image:latest",
            name="worker",
            repo_path=tmp_path,
            workspace_path=tmp_path / "work",
            run_dir=tmp_path / "out",
            model="model",
        )


def test_prepare_workspace_ignores_chmod_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_chmod(_path: Path, _mode: int) -> None:
        raise OSError("read only")

    monkeypatch.setattr(Path, "chmod", fail_chmod)
    docker._prepare_workspace_dir(tmp_path / "work")

    assert (tmp_path / "work").is_dir()


def test_exec_in_writes_stdout_and_stderr_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _proc("ok", "warning")

    monkeypatch.setattr(docker.subprocess, "run", fake_run)

    result = docker.exec_in(
        "worker",
        "echo hello",
        timeout=10,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )

    assert result.stdout == "ok"
    assert calls[0][:3] == ["docker", "exec", "-u"]
    assert stdout_path.exists()
    assert stderr_path.exists()


def test_exec_in_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["docker"], timeout=1)

    monkeypatch.setattr(docker.subprocess, "run", fail)

    with pytest.raises(docker.DockerError, match="timed out"):
        docker.exec_in("worker", "sleep 1", timeout=1)


def test_exec_in_cannot_start_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("missing")

    monkeypatch.setattr(docker.subprocess, "run", fail)

    with pytest.raises(docker.DockerError, match="Could not execute"):
        docker.exec_in("worker", "true", timeout=1)


def test_cleanup_managed_removes_each_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["docker", "ps"]:
            return _proc("one\ntwo\n")
        if args[:3] == ["docker", "network", "inspect"]:
            return _proc("true\n")
        return _proc()

    monkeypatch.setattr(docker, "_run", fake_run)

    docker.cleanup_managed()

    assert [call[:3] for call in calls] == [
        ["docker", "ps", "-aq"],
        ["docker", "rm", "-f"],
        ["docker", "rm", "-f"],
        ["docker", "network", "inspect"],
        ["docker", "network", "rm"],
    ]


def test_cleanup_managed_handles_no_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_run",
        lambda args, **_kwargs: calls.append(args) or _proc(),
    )

    docker.cleanup_managed()

    assert len(calls) == 2
    assert calls[1][:3] == ["docker", "network", "inspect"]


def test_stop_ignores_missing_container(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise docker.DockerError("missing")

    monkeypatch.setattr(docker, "_run", fail)
    docker.stop("worker")


def test_stop_removes_container(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_run",
        lambda args, **_kwargs: calls.append(args) or _proc(),
    )

    docker.stop("worker")

    assert calls == [["docker", "rm", "-f", "worker"]]


def test_logs_combines_stdout_and_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        docker,
        "_run",
        lambda *_args, **_kwargs: _proc("stdout\n", "stderr\n"),
    )

    assert docker.logs("worker", tail=10) == "stdout\nstderr"


def test_image_exists_uses_inspect_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        docker,
        "_run",
        lambda *_args, **_kwargs: _proc(code=0),
    )
    assert docker._image_exists("image")

    monkeypatch.setattr(
        docker,
        "_run",
        lambda *_args, **_kwargs: _proc(code=1),
    )
    assert not docker._image_exists("image")


def test_run_rejects_failed_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        docker.subprocess,
        "run",
        lambda *_args, **_kwargs: _proc(stderr="bad\n", code=2),
    )

    with pytest.raises(docker.DockerError, match="bad"):
        docker._run(["docker", "version"], timeout=1)


def test_run_allows_failed_command_when_check_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        docker.subprocess,
        "run",
        lambda *_args, **_kwargs: _proc(stderr="bad", code=2),
    )

    result = docker._run(["docker", "version"], timeout=1, check=False)

    assert result.returncode == 2


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (subprocess.TimeoutExpired(cmd=["docker"], timeout=1), "timed out"),
        (OSError("missing"), "Could not execute"),
    ],
)
def test_run_translates_process_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    message: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(docker.subprocess, "run", fail)

    with pytest.raises(docker.DockerError, match=message):
        docker._run(["docker", "version"], timeout=1)


def test_start_reviewer_mounts_repository_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(docker, "_image_exists", lambda _image: True)
    monkeypatch.setattr(
        docker,
        "_ensure_review_isolation",
        lambda _image: "172.30.50.2",
    )
    monkeypatch.setattr(
        docker,
        "_run",
        lambda args, **_kwargs: calls.append(args) or _proc("container-id\n"),
    )

    result = docker.start_reviewer(
        image="reviewer:latest",
        name="reviewer",
        repo_path=tmp_path / "repo",
        run_dir=tmp_path / "out",
        model="model",
    )

    assert result == "container-id"
    args = calls[0]
    assert "reviewer" in args
    assert "orch.reviewer=true" in args
    assert "--cap-drop" in args
    assert docker._REVIEW_NETWORK in args
    assert "host.docker.internal:172.30.50.2" in args
    assert "mcp.context7.com:172.30.50.2" in args
    assert f"{tmp_path / 'repo'}:/review:ro" in args
    assert f"{tmp_path / 'out'}:/out" in args
    assert all("/work" not in arg for arg in args)
    assert all("GH_TOKEN" not in arg for arg in args)


def test_start_reviewer_requires_an_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker, "_image_exists", lambda _image: False)

    with pytest.raises(docker.DockerError, match="does not exist"):
        docker.start_reviewer(
            image="missing:latest",
            name="reviewer",
            repo_path=tmp_path / "repo",
            run_dir=tmp_path / "out",
            model="model",
        )


def test_exec_in_uses_requested_workdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        docker.subprocess,
        "run",
        lambda args, **_kwargs: calls.append(args) or _proc("ok"),
    )

    docker.exec_in("reviewer", "true", timeout=10, workdir="/review")

    assert calls[0][:7] == [
        "docker",
        "exec",
        "-u",
        "agent",
        "-w",
        "/review",
        "reviewer",
    ]


def test_ensure_review_isolation_reuses_running_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        args: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["docker", "network", "inspect"]:
            return _proc("true\n")
        if args[:2] == ["docker", "inspect"]:
            if "{{.State.Running}}" in args:
                return _proc("true\n")
            return _proc("172.30.50.2\n")
        return _proc()

    monkeypatch.setattr(docker, "_run", fake_run)

    assert docker._ensure_review_isolation("image:latest") == "172.30.50.2"
    assert not any("create" in call for call in calls)
    assert any(call[:3] == ["docker", "network", "connect"] for call in calls)


def test_ensure_review_network_rejects_a_non_internal_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        docker,
        "_run",
        lambda *_args, **_kwargs: _proc("false\n"),
    )

    with pytest.raises(docker.DockerError, match="not internal"):
        docker._ensure_review_network()


def test_ensure_review_isolation_starts_gateway_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        args: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["docker", "network", "inspect"]:
            return _proc(code=1)
        if args[:3] == ["docker", "network", "connect"]:
            return _proc(code=1)
        if args[:2] == ["docker", "inspect"]:
            if "{{.State.Running}}" in args:
                return _proc(code=1)
            return _proc("172.30.50.3\n")
        return _proc()

    monkeypatch.setattr(docker, "_run", fake_run)

    assert docker._ensure_review_isolation("image:latest") == "172.30.50.3"
    assert [
        "docker",
        "network",
        "create",
        "--label",
        "orch.managed=true",
        "--internal",
        docker._REVIEW_NETWORK,
    ] in calls
    gateway_run = next(call for call in calls if call[:3] == ["docker", "run", "-d"])
    assert "NET_BIND_SERVICE" in gateway_run
    assert any("orch-review-gateway" in call for call in calls)


def test_codegraph_mount_only_when_an_index_exists(tmp_path: Path) -> None:
    assert docker._codegraph_mount(tmp_path) == []
    (tmp_path / ".codegraph").mkdir()
    assert docker._codegraph_mount(tmp_path) == [
        "-v",
        f"{tmp_path / '.codegraph'}:/review/.codegraph:ro",
    ]
    assert docker._codegraph_mount(tmp_path, enabled=False) == []

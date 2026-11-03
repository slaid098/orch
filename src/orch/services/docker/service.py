"""Docker lifecycle helpers for worker containers."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from orch.config.logger import get_logger
from orch.exceptions.docker import DockerError

log = get_logger(component="docker")

_REVIEW_NETWORK = "orch-review-net"
_REVIEW_GATEWAY = "orch-review-gateway"
_REVIEW_GATEWAY_SCRIPT = "/usr/local/bin/orch-review-gateway"
_REVIEW_ENSURE_LOCK = threading.Lock()


def _prepare_workspace_dir(workspace_path: Path) -> None:
    workspace_path.mkdir(parents=True, exist_ok=True)
    try:
        workspace_path.chmod(0o777)
    except OSError as ex:
        log.bind(
            path=str(workspace_path),
            error=ex,
            error_type=type(ex).__name__,
        ).warning("Could not widen workspace permissions")


def _codegraph_mount(repo_path: Path) -> list[str]:
    """Keep the reviewer checkout read-only while allowing index reads."""
    index_path = repo_path / ".codegraph"
    if not index_path.is_dir():
        return []
    return ["-v", f"{index_path}:/review/.codegraph:rw"]


def _ensure_review_isolation(image: str) -> str:
    """Create the internal review network and its TCP gateway.

    The reviewer has no route to the internet. A small gateway container is
    attached to both the internal network and the normal bridge; it forwards
    only the model port and the Context7 TLS port. The returned IP is used as
    the reviewer's ``host.docker.internal`` and ``mcp.context7.com``.
    """
    with _REVIEW_ENSURE_LOCK:
        _ensure_review_network()
        _ensure_review_gateway(image)
        return _review_gateway_ip()


def _ensure_review_network() -> None:
    result = _run(
        ["docker", "network", "inspect", _REVIEW_NETWORK],
        timeout=30,
        check=False,
    )
    if result.returncode == 0:
        return
    _run(
        ["docker", "network", "create", "--internal", _REVIEW_NETWORK],
        timeout=60,
    )
    log.info("Created internal reviewer network {}", _REVIEW_NETWORK)


def _ensure_review_gateway(image: str) -> None:
    running = _run(
        ["docker", "inspect", "--format", "{{.State.Running}}", _REVIEW_GATEWAY],
        timeout=30,
        check=False,
    )
    if (running.stdout or "").strip() != "true":
        _run(
            ["docker", "rm", "-f", _REVIEW_GATEWAY],
            timeout=60,
            check=False,
        )
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                _REVIEW_GATEWAY,
                "--label",
                "orch.managed=true",
                "--label",
                "orch.review-gateway=true",
                "--network",
                "bridge",
                "--add-host",
                "host.docker.internal:host-gateway",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "NET_BIND_SERVICE",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "256m",
                "--cpus",
                "1",
                "--pids-limit",
                "128",
                "--user",
                "root",
                image,
                "python",
                _REVIEW_GATEWAY_SCRIPT,
            ],
            timeout=120,
        )
        log.info("Started isolated reviewer gateway")
    connected = _run(
        [
            "docker",
            "network",
            "connect",
            _REVIEW_NETWORK,
            _REVIEW_GATEWAY,
        ],
        timeout=60,
        check=False,
    )
    if connected.returncode != 0:
        log.bind(stderr=(connected.stderr or "").strip()[-500:]).debug(
            "Reviewer gateway network connect was already present"
        )
    _wait_for_gateway()


def _wait_for_gateway() -> None:
    """Wait until the gateway accepts both allowlisted ports."""
    probe = (
        "import socket; "
        "s = socket.create_connection(('127.0.0.1', 11434), 1); s.close(); "
        "s = socket.create_connection(('127.0.0.1', 443), 1); s.close()"
    )
    for _ in range(20):
        result = _run(
            ["docker", "exec", _REVIEW_GATEWAY, "python", "-c", probe],
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(0.25)
    raise DockerError("Reviewer gateway did not become ready")


def _review_gateway_ip() -> str:
    result = _run(
        [
            "docker",
            "inspect",
            "--format",
            (f'{{{{(index .NetworkSettings.Networks "{_REVIEW_NETWORK}").IPAddress}}}}'),
            _REVIEW_GATEWAY,
        ],
        timeout=30,
    )
    ip = (result.stdout or "").strip()
    if not ip:
        raise DockerError("Reviewer gateway has no internal network address")
    return ip


def _worker_run_args(
    *,
    image: str,
    name: str,
    repo_path: Path,
    workspace_path: Path,
    run_dir: Path,
    model: str,
    context_tokens: int | None,
) -> list[str]:
    return [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--label",
        "orch.managed=true",
        "--label",
        f"orch.container={name}",
        "--add-host",
        "host.docker.internal:host-gateway",
        "-v",
        f"{repo_path}:/src:ro",
        "-v",
        f"{workspace_path}:/work",
        "-v",
        f"{run_dir}:/out",
        *_worker_env(model, context_tokens),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "4g",
        "--cpus",
        "2",
        "--pids-limit",
        "512",
        image,
    ]


def _worker_env(model: str, context_tokens: int | None) -> list[str]:
    values = {
        "ANTHROPIC_BASE_URL": "http://host.docker.internal:11434",
        "ANTHROPIC_AUTH_TOKEN": "ollama",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "API_TIMEOUT_MS": "600000",
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
    }
    if context_tokens is not None:
        values["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(context_tokens)
        values["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(context_tokens)
    args: list[str] = []
    for key, value in values.items():
        args.extend(["-e", f"{key}={value}"])
    return args


def start_reviewer(
    *,
    image: str,
    name: str,
    repo_path: Path,
    run_dir: Path,
    model: str,
    context_tokens: int | None = None,
) -> str:
    """Start a read-only reviewer container.

    Args:
        image: Reviewer image tag.
        name: Container name.
        repo_path: Worker checkout mounted read-only at ``/review``.
        run_dir: Host run directory mounted writable at ``/out``.
        model: Ollama model name passed to Claude Code.
        context_tokens: Context window in tokens, or ``None`` for auto.

    Returns:
        The container ID.

    Raises:
        DockerError: If the image is missing or the container cannot start.
    """
    if not _image_exists(image):
        raise DockerError(f"Reviewer image {image!r} does not exist. Build it with Docker.")

    gateway_ip = _ensure_review_isolation(image)
    args = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--label",
        "orch.managed=true",
        "--label",
        "orch.reviewer=true",
        "--network",
        _REVIEW_NETWORK,
        "--add-host",
        f"host.docker.internal:{gateway_ip}",
        "--add-host",
        f"mcp.context7.com:{gateway_ip}",
        "-v",
        f"{repo_path}:/review:ro",
        *_codegraph_mount(repo_path),
        "-v",
        f"{run_dir}:/out",
        *_worker_env(model, context_tokens),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "4g",
        "--cpus",
        "2",
        "--pids-limit",
        "512",
        image,
    ]
    result = _run(args, timeout=120)
    container_id = (result.stdout or "").strip()
    if not container_id:
        raise DockerError("docker run returned no container id")
    log.info("Started reviewer container {} ({})", name, container_id[:12])
    return container_id


def start_worker(
    *,
    image: str,
    name: str,
    repo_path: Path,
    workspace_path: Path,
    run_dir: Path,
    model: str,
    context_tokens: int | None = None,
) -> str:
    """Start a detached worker container.

    Args:
        image: Worker image tag.
        name: Container name.
        repo_path: Host repository mounted read-only at ``/src``.
        workspace_path: Persistent host workspace mounted at ``/work``.
        run_dir: Host run directory mounted at ``/out``.
        model: Ollama model name passed to Claude Code.
        context_tokens: Context window in tokens, or ``None`` for auto.

    Returns:
        The container ID.
    """
    if not _image_exists(image):
        raise DockerError(f"Worker image {image!r} does not exist. Build it with Docker.")

    _prepare_workspace_dir(workspace_path)
    args = _worker_run_args(
        image=image,
        name=name,
        repo_path=repo_path,
        workspace_path=workspace_path,
        run_dir=run_dir,
        model=model,
        context_tokens=context_tokens,
    )
    result = _run(args, timeout=120)
    container_id = (result.stdout or "").strip()
    if not container_id:
        raise DockerError("docker run returned no container id")
    log.info("Started worker container {} ({})", name, container_id[:12])
    return container_id


def exec_in(
    name: str,
    command: str,
    *,
    timeout: int,
    workdir: str = "/work",
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a command inside a managed container.

    Args:
        name: Container name.
        command: Shell command passed to ``bash -lc``.
        timeout: Timeout in seconds.
        workdir: Working directory inside the container.
        stdout_path: Optional file receiving stdout.
        stderr_path: Optional file receiving stderr.

    Returns:
        Completed process. ``stdout`` is ``None`` when redirected to a file.
    """
    args = [
        "docker",
        "exec",
        "-u",
        "agent",
        "-w",
        workdir,
        name,
        "bash",
        "-lc",
        command,
    ]

    stdout_file = None
    stderr_file = None
    try:
        if stdout_path is not None:
            stdout_file = stdout_path.open("w", encoding="utf-8")
        if stderr_path is not None:
            stderr_file = stderr_path.open("w", encoding="utf-8")
        result = subprocess.run(
            args,
            stdout=stdout_file if stdout_file else subprocess.PIPE,
            stderr=stderr_file if stderr_file else subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as ex:
        log.bind(container=name, timeout=timeout).error("Container command timed out")
        raise DockerError(f"Container command timed out after {timeout}s") from ex
    except OSError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).error("Could not execute docker exec")
        raise DockerError("Could not execute docker exec") from ex
    else:
        return result
    finally:
        if stdout_file:
            stdout_file.close()
        if stderr_file:
            stderr_file.close()


def cleanup_managed() -> None:
    """Remove worker containers left by a previous orch process."""
    result = _run(
        ["docker", "ps", "-aq", "--filter", "label=orch.managed=true"],
        timeout=60,
        check=False,
    )
    container_ids = (result.stdout or "").split()
    for container_id in container_ids:
        _run(["docker", "rm", "-f", container_id], timeout=60, check=False)
    if container_ids:
        log.info("Removed {} orphan worker container(s)", len(container_ids))
    _run(
        ["docker", "network", "rm", _REVIEW_NETWORK],
        timeout=60,
        check=False,
    )


def stop(name: str) -> None:
    """Remove a worker container, ignoring missing containers."""
    try:
        _run(["docker", "rm", "-f", name], timeout=60)
    except DockerError:
        log.bind(container=name).warning("Could not remove container")
    else:
        log.info("Removed worker container {}", name)


def logs(name: str, *, tail: int = 100) -> str:
    """Return the tail of a container's logs."""
    result = _run(["docker", "logs", "--tail", str(tail), name], timeout=60)
    return ((result.stdout or "") + (result.stderr or "")).strip()


def _image_exists(image: str) -> bool:
    """Return whether a Docker image is available locally."""
    result = _run(
        ["docker", "image", "inspect", image],
        timeout=30,
        check=False,
    )
    return result.returncode == 0


def _run(
    args: list[str],
    *,
    timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as ex:
        log.bind(args=args[:3], timeout=timeout).error("Docker command timed out")
        raise DockerError(f"Docker command timed out after {timeout}s") from ex
    except OSError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).error("Could not execute docker")
        raise DockerError("Could not execute docker") from ex
    else:
        if check and result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            log.bind(args=args[:3], returncode=result.returncode, stderr=message).error(
                "Docker command failed"
            )
            raise DockerError(message or "Docker command failed")
        return result

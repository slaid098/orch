"""Docker lifecycle helpers for worker containers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from loguru import logger


class DockerError(RuntimeError):
    """Raised when a Docker command fails."""


def build_image(image: str, dockerfile: Path, context: Path) -> None:
    """Build the worker image.

    Args:
        image: Image tag to build.
        dockerfile: Path to the Dockerfile.
        context: Docker build context.
    """
    if not dockerfile.exists():
        raise DockerError(f"Dockerfile not found: {dockerfile}")

    logger.info("Building image {} from {}", image, dockerfile)
    _run(
        [
            "docker",
            "build",
            "-f",
            str(dockerfile),
            "-t",
            image,
            str(context),
        ],
        timeout=1800,
    )
    logger.info("Built image {}", image)


def start_worker(
    *,
    image: str,
    name: str,
    repo_path: Path,
    run_dir: Path,
    model: str,
) -> str:
    """Start a detached worker container.

    Args:
        image: Worker image tag.
        name: Container name.
        repo_path: Host repository mounted read-only at ``/src``.
        run_dir: Host run directory mounted at ``/out``.
        model: Ollama model name passed to Claude Code.

    Returns:
        The container ID.
    """
    if not image_exists(image):
        raise DockerError(
            f"Worker image {image!r} does not exist. Run `orch build-image` first."
        )

    args = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "-v",
        f"{repo_path}:/src:ro",
        "-v",
        f"{run_dir}:/out",
        "-e",
        "ANTHROPIC_BASE_URL=http://host.docker.internal:11434",
        "-e",
        "ANTHROPIC_AUTH_TOKEN=ollama",
        "-e",
        "ANTHROPIC_API_KEY=",
        "-e",
        f"ANTHROPIC_MODEL={model}",
        "-e",
        f"ANTHROPIC_DEFAULT_HAIKU_MODEL={model}",
        "-e",
        f"ANTHROPIC_DEFAULT_SONNET_MODEL={model}",
        "-e",
        f"ANTHROPIC_DEFAULT_OPUS_MODEL={model}",
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
    logger.info("Started worker container {} ({})", name, container_id[:12])
    return container_id


def exec_in(
    name: str,
    command: str,
    *,
    timeout: int,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a command inside a worker container.

    Args:
        name: Container name.
        command: Shell command passed to ``bash -lc``.
        timeout: Timeout in seconds.
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
        "/work",
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
        logger.bind(container=name, timeout=timeout).error(
            "Container command timed out"
        )
        raise DockerError(f"Container command timed out after {timeout}s") from ex
    except OSError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not execute docker exec"
        )
        raise DockerError("Could not execute docker exec") from ex
    else:
        return result
    finally:
        if stdout_file:
            stdout_file.close()
        if stderr_file:
            stderr_file.close()


def stop(name: str) -> None:
    """Remove a worker container, ignoring missing containers."""
    try:
        _run(["docker", "rm", "-f", name], timeout=60)
    except DockerError:
        logger.bind(container=name).warning("Could not remove container")
    else:
        logger.info("Removed worker container {}", name)


def logs(name: str, *, tail: int = 100) -> str:
    """Return the tail of a container's logs."""
    result = _run(["docker", "logs", "--tail", str(tail), name], timeout=60)
    return ((result.stdout or "") + (result.stderr or "")).strip()


def image_exists(image: str) -> bool:
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
        logger.bind(args=args[:3], timeout=timeout).error("Docker command timed out")
        raise DockerError(f"Docker command timed out after {timeout}s") from ex
    except OSError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not execute docker"
        )
        raise DockerError("Could not execute docker") from ex
    else:
        if check and result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            logger.bind(args=args[:3], returncode=result.returncode, stderr=message).error(
                "Docker command failed"
            )
            raise DockerError(message or "Docker command failed")
        return result

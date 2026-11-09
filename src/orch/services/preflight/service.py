"""Read-only startup checks for the daemon."""

from __future__ import annotations

import shutil
import socket
import subprocess

from orch.config.logger import get_logger
from orch.exceptions.docker import DockerError
from orch.exceptions.github import GitHubError
from orch.schemas.config.types import Config
from orch.services import docker, github

log = get_logger(component="preflight")

_MODEL_HOST = "127.0.0.1"
_MODEL_PORT = 11434
_MODEL_TIMEOUT_SECONDS = 3
_GIT_TIMEOUT_SECONDS = 30


def check(config: Config) -> list[str]:
    """Return every startup problem without mutating remote state.

    Checks the Docker daemon and required images, the local model endpoint,
    GitHub authentication and repository access, the base branch, and free
    disk space.

    Args:
        config: Orchestrator configuration.

    Returns:
        Human-readable problems; empty when the daemon can start.
    """
    problems: list[str] = []
    _check_docker(config, problems)
    _check_model_endpoint(problems)
    _check_github(config, problems)
    _check_base_branch(config, problems)
    _check_disk(config, problems)
    return problems


def _check_docker(config: Config, problems: list[str]) -> None:
    """Check the daemon and required images, reporting at most one failure."""
    try:
        if not docker.daemon_available():
            problems.append(
                "Docker daemon is not reachable. Start Docker Desktop and retry."
            )
            return
        _check_image(config.worker.image, "Worker", problems)
        if config.reviewer.enabled:
            image = config.reviewer.image or config.worker.image
            _check_image(image, "Reviewer", problems)
    except DockerError as ex:
        problems.append(f"Docker check failed: {ex}")


def _check_image(image: str, role: str, problems: list[str]) -> None:
    """Report a missing image with its role."""
    if not docker.image_exists(image):
        problems.append(
            f"{role} image {image!r} is missing. Build it before starting orch."
        )


def _check_model_endpoint(problems: list[str]) -> None:
    """Check that the local model endpoint accepts TCP connections."""
    try:
        with socket.create_connection(
            (_MODEL_HOST, _MODEL_PORT),
            timeout=_MODEL_TIMEOUT_SECONDS,
        ):
            pass
    except OSError as ex:
        problems.append(
            f"Model endpoint {_MODEL_HOST}:{_MODEL_PORT} is not reachable: {ex}. "
            "Start the Ollama endpoint that Docker reaches at "
            "host.docker.internal:11434."
        )


def _check_github(config: Config, problems: list[str]) -> None:
    """Check the token owner and repository access."""
    try:
        token = github.resolve_token(config.repo.owner, config.gh_token)
        github.ensure_user(token, config.repo.owner)
        github.check_repo(config.repo.full_name, token)
    except GitHubError as ex:
        problems.append(f"GitHub: {ex}")


def _check_base_branch(config: Config, problems: list[str]) -> None:
    """Check that the base branch exists locally or under origin."""
    if _base_branch_exists(config):
        return
    problems.append(
        f"Base branch {config.repo.base_branch!r} is missing in "
        f"{config.repo.path}. Fetch the repository or fix repo.base_branch."
    )


def _base_branch_exists(config: Config) -> bool:
    """Return whether the base branch exists locally or under origin."""
    base = config.repo.base_branch
    for ref in (f"refs/heads/{base}", f"refs/remotes/origin/{base}"):
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(config.repo.path),
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    ref,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as ex:
            log.bind(
                path=str(config.repo.path),
                error=ex,
                error_type=type(ex).__name__,
            ).warning("Could not check the base branch")
            return False
        if result.returncode == 0:
            return True
    return False


def _check_disk(config: Config, problems: list[str]) -> None:
    """Check free space on the data-directory volume."""
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(config.data_dir).free
    except OSError as ex:
        problems.append(f"Could not check free space in {config.data_dir}: {ex}")
        return
    free_gb = free / (1024**3)
    if free_gb < config.min_free_disk_gb:
        problems.append(
            f"Only {free_gb:.1f} GB free in {config.data_dir}; "
            f"at least {config.min_free_disk_gb} GB is required."
        )

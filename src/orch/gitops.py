"""Git bundle import and push helpers used by the daemon."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

from loguru import logger


class GitError(RuntimeError):
    """Raised when a git operation fails."""


def refresh_mirror(
    data_dir: Path,
    owner: str,
    name: str,
    base_branch: str,
    token: str,
) -> Path:
    """Fetch the latest base branch into a persistent bare mirror.

    Args:
        data_dir: Local orch data directory.
        owner: Repository owner.
        name: Repository name.
        base_branch: Branch the worker should start from.
        token: GitHub token used only for the fetch URL.

    Returns:
        Path to the bare mirror.
    """
    mirror = data_dir / "mirrors" / f"{owner}__{name}.git"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    if not (mirror / "HEAD").exists():
        _run_git(["init", "--bare", str(mirror)], cwd=mirror.parent)

    base_ref = f"refs/heads/{base_branch}"
    _run_git(["--git-dir", str(mirror), "symbolic-ref", "HEAD", base_ref], cwd=mirror)
    remote = authenticated_remote(owner, name, token)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    _run_git(
        [
            "--git-dir",
            str(mirror),
            "fetch",
            "--prune",
            remote,
            f"+{base_ref}:{base_ref}",
        ],
        cwd=mirror.parent,
        env=env,
        secret=token,
    )
    _run_git(["--git-dir", str(mirror), "rev-parse", base_ref], cwd=mirror)
    logger.info("Refreshed source mirror to {}/{}", owner, name)
    return mirror


def clone_source(mirror: Path, base_branch: str, destination: Path) -> Path:
    """Create a disposable source checkout for one issue run.

    The copy is made from the local mirror; the worker never receives a
    GitHub token. It is cloned once per issue, not once per retry.
    """
    if destination.exists():
        _remove_tree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        ["clone", "--no-hardlinks", str(mirror), str(destination)],
        cwd=destination.parent,
    )
    _run_git(["checkout", base_branch], cwd=destination)
    logger.info("Created worker source at {}", destination)
    return destination


def cleanup_source(destination: Path) -> None:
    """Remove a disposable worker source copy, ignoring missing paths."""
    _remove_if_present(destination, label="worker source")


def cleanup_workspace(destination: Path) -> None:
    """Remove a persisted issue workspace after a pull request is created."""
    _remove_if_present(destination, label="issue workspace")


def _remove_if_present(destination: Path, *, label: str) -> None:
    try:
        _remove_tree(destination)
    except FileNotFoundError:
        return
    except OSError as ex:
        logger.bind(
            path=str(destination), error=ex, error_type=type(ex).__name__
        ).warning("Could not remove {}", label)
    else:
        logger.debug("Removed {} {}", label, destination)


def _remove_tree(path: Path) -> None:
    def make_writable(
        function: Callable[[str], None],
        target: str,
        _: object,
    ) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onerror=make_writable)


def import_bundle(bundle: Path, branch: str, run_dir: Path) -> Path:
    """Import a worker bundle into a temporary bare repository.

    Args:
        bundle: Bundle written by the worker.
        branch: Branch name contained in the bundle.
        run_dir: Directory where the temporary bare repository is created.

    Returns:
        Path to the bare repository containing the branch.

    Raises:
        GitError: If the bundle is invalid or the branch is missing.
    """
    if not bundle.exists() or bundle.stat().st_size == 0:
        raise GitError(f"Worker bundle is missing or empty: {bundle}")

    bare = run_dir / "push.git"
    _run_git(["init", "--bare", str(bare)], cwd=run_dir)
    _run_git(["bundle", "verify", str(bundle)], cwd=bare)

    ref = f"refs/heads/{branch}"
    heads = _run_git(["bundle", "list-heads", str(bundle)], cwd=run_dir)
    if ref not in heads:
        raise GitError(f"Bundle does not contain {ref}")

    _run_git(
        [
            "--git-dir",
            str(bare),
            "fetch",
            str(bundle),
            f"{ref}:{ref}",
        ],
        cwd=run_dir,
    )
    logger.info("Imported bundle for branch {}", branch)
    return bare


def push_branch(bare: Path, branch: str, remote_url: str, token: str) -> None:
    """Push a branch from a bare repository to GitHub.

    Args:
        bare: Bare repository created by :func:`import_bundle`.
        branch: Branch name to push.
        remote_url: Authenticated or unauthenticated GitHub remote URL.
        token: GitHub token used for redaction in error messages.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"

    try:
        _run_git(
            [
                "--git-dir",
                str(bare),
                "push",
                remote_url,
                f"refs/heads/{branch}:refs/heads/{branch}",
            ],
            cwd=bare.parent,
            env=env,
            secret=token,
        )
    except GitError as ex:
        raise GitError(f"Could not push branch {branch}") from ex
    else:
        logger.info("Pushed branch {} to GitHub", branch)


def authenticated_remote(owner: str, name: str, token: str) -> str:
    """Build an HTTPS remote URL containing a GitHub token."""
    return f"https://x-access-token:{token}@github.com/{owner}/{name}.git"


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    secret: str | None = None,
    timeout: int = 600,
) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as ex:
        logger.bind(
            args=_redact_args(args, secret),
            timeout=timeout,
        ).error("Git command timed out")
        raise GitError(f"Git command timed out after {timeout}s") from ex
    except OSError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not execute git"
        )
        raise GitError("Could not execute git") from ex
    else:
        if result.returncode != 0:
            message = _redact((result.stderr or result.stdout).strip(), secret)
            logger.bind(
                args=_redact_args(args, secret),
                returncode=result.returncode,
                stderr=message,
            ).error("Git command failed")
            raise GitError(message or "Git command failed")
        return result.stdout


def _redact(text: str, secret: str | None) -> str:
    if not secret:
        return text
    return text.replace(secret, "***")


def _redact_args(args: list[str], secret: str | None) -> list[str]:
    if not secret:
        return args
    return [arg.replace(secret, "***") for arg in args]

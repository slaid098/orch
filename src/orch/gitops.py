"""Git bundle import and push helpers used by the daemon."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from loguru import logger


class GitError(RuntimeError):
    """Raised when a git operation fails."""


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
    _run_git(["bundle", "verify", str(bundle)], cwd=run_dir)

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
            check=False,
        )
    except OSError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not execute git"
        )
        raise GitError("Could not execute git") from ex
    else:
        if result.returncode != 0:
            message = _redact((result.stderr or result.stdout).strip(), secret)
            logger.bind(args=args, returncode=result.returncode, stderr=message).error(
                "Git command failed"
            )
            raise GitError(message or "Git command failed")
        return result.stdout


def _redact(text: str, secret: str | None) -> str:
    if not secret:
        return text
    return text.replace(secret, "***")

"""Worker container orchestration for one issue attempt."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from orch.config.logger import get_logger
from orch.exceptions.worker import AgentError
from orch.schemas.config.types import Config
from orch.schemas.github.types import Issue
from orch.schemas.worker.types import WorkerResult
from orch.services.docker import exec_in, start_worker, stop
from orch.services.standards import branch_name, load_handoff

log = get_logger(component="worker")


def build_prompt(
    config: Config,
    issue: Issue,
    *,
    attempt: int = 1,
    previous_error: str | None = None,
) -> str:
    """Build the complete prompt sent to the worker agent."""
    role_path = Path(__file__).parent / "prompt.md"
    role = role_path.read_text(encoding="utf-8")
    setup = "\n".join(f"- `{command}`" for command in config.worker.setup_commands) or "- none"
    tests = "\n".join(f"- `{command}`" for command in config.worker.test_commands)
    resume = _resume_section(attempt, previous_error)

    return (
        f"{role}\n"
        f"#{issue.number}: {issue.title}\n\n"
        f"{issue.body}\n"
        "</issue>\n\n"
        "## Repository\n"
        f"- `{config.repo.full_name}`\n"
        f"- Base branch: `{config.repo.base_branch}`\n"
        "- Work directory: `/work`\n\n"
        "## Commands\n"
        "Setup:\n"
        f"{setup}\n\n"
        "Tests:\n"
        f"{tests}\n\n"
        f"{resume}"
        "## Output contract\n"
        "Write `/out/handoff.json` exactly as described above. Do not commit.\n"
        "The issue title is the only source for the final commit and pull-request "
        "title; do not choose or rewrite it.\n"
    )


def _resume_section(attempt: int, previous_error: str | None) -> str:
    if attempt <= 1:
        return ""
    error = previous_error or "unknown failure"
    return (
        "## Resume\n"
        f"This is attempt {attempt}. The `/work` directory is persistent and "
        "contains work from the previous attempt. Do not re-clone it, reset it, "
        "or discard changes. Inspect `git status` and `git diff`, continue from "
        "the existing state, and finish the issue.\n"
        f"Previous failure: {error}\n\n"
    )


def run_worker(
    config: Config,
    issue: Issue,
    run_dir: Path,
    *,
    source_path: Path,
    workspace_path: Path,
    attempt: int = 1,
    previous_error: str | None = None,
    timeout_seconds: int,
) -> WorkerResult:
    """Run one worker container and return its artifacts.

    Args:
        config: Orchestrator configuration.
        issue: Issue to implement.
        run_dir: Host directory mounted at ``/out``.
        source_path: Clean source checkout mounted read-only at ``/src``.
        workspace_path: Persistent host workspace mounted at ``/work``.
        attempt: One-based attempt number.
        previous_error: Failure from the previous attempt, used for handoff.
        timeout_seconds: Hard budget for this whole attempt.

    Returns:
        Successful worker result.

    Raises:
        AgentError: If the worker fails, produces no changes, or tests fail.
        DockerError: If the container cannot be managed.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_path.mkdir(parents=True, exist_ok=True)
    branch = branch_name(issue.title, issue.number)
    prompt = build_prompt(
        config,
        issue,
        attempt=attempt,
        previous_error=previous_error,
    )
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    container = _container_name(run_dir)
    deadline = time.monotonic() + timeout_seconds
    start_worker(
        image=config.worker.image,
        name=container,
        repo_path=source_path,
        workspace_path=workspace_path,
        run_dir=run_dir,
        model=config.worker.model,
    )

    try:
        base_sha = _prepare_workspace(container, branch, deadline)
        _run_setup(container, config, deadline)
        _run_claude(container, config, run_dir, deadline)
        _ensure_changes(container, base_sha, deadline)
        handoff = load_handoff(run_dir)
        _finalize_commit(container, base_sha, issue.title, run_dir, deadline)
        _write_diff(container, base_sha, run_dir, deadline)
        tests_output = _run_tests(container, config, run_dir, deadline)
        bundle = _bundle(container, branch, run_dir, deadline)
        return WorkerResult(
            run_dir=run_dir,
            branch=branch,
            handoff=handoff,
            tests_output=tests_output,
            bundle_path=bundle,
        )
    finally:
        stop(container)


def _container_name(run_dir: Path) -> str:
    name = f"orch-{run_dir.parent.name}-{run_dir.name}".lower()
    return "".join(char if char.isalnum() or char in "-_." else "-" for char in name)[:63]


def _prepare_workspace(container: str, branch: str, deadline: float) -> str:
    command = (
        "set -e; "
        "export GIT_CONFIG_GLOBAL=/out/gitconfig; "
        "git config --global --add safe.directory /src/.git; "
        "git config --global --add safe.directory /work; "
        "if [ ! -d /work/.git ]; then git clone /src /work; fi; "
        "cd /work; "
        'git config user.name "orch"; '
        'git config user.email "orch@example.invalid"; '
        f"if git show-ref --verify --quiet refs/heads/{branch}; "
        f"then git switch {branch}; else git switch -c {branch}; fi; "
        "if git rev-parse --verify --quiet refs/tags/orch-base >/dev/null; "
        "then base=$(git rev-parse refs/tags/orch-base); "
        'else base=$(git rev-parse HEAD); git tag -f orch-base "$base"; fi; '
        'printf "%s\\n" "$base"'
    )
    result = _exec_or_raise(
        container,
        command,
        timeout=_remaining(deadline, cap=300),
        label="prepare workspace",
    )
    base_sha = (result.stdout or "").strip().splitlines()[-1]
    if not base_sha:
        raise AgentError("Could not resolve the worker base commit")
    log.info("Prepared workspace on branch {} from {}", branch, base_sha[:12])
    return base_sha


def _run_setup(container: str, config: Config, deadline: float) -> None:
    if not config.worker.setup_commands:
        return
    command = "cd /work && " + " && ".join(config.worker.setup_commands)
    _exec_or_raise(
        container,
        command,
        timeout=_remaining(deadline, cap=900),
        label="setup",
    )


def _run_claude(
    container: str,
    config: Config,
    run_dir: Path,
    deadline: float,
) -> None:
    command = (
        "cd /work && "
        'claude -p "$(cat /out/prompt.md)" '
        "--output-format stream-json "
        "--verbose "
        "--permission-mode bypassPermissions "
        "--permission-prompts none "
        f'--allowedTools "{config.worker.allowed_tools}" '
        "--autocompact 200k "
        f'--model "{config.worker.model}"'
    )
    result = exec_in(
        container,
        command,
        timeout=_remaining(deadline, cap=config.worker.timeout_minutes * 60),
        stdout_path=run_dir / "events.jsonl",
        stderr_path=run_dir / "events.stderr.log",
    )
    if result.returncode != 0:
        log.bind(container=container, returncode=result.returncode).warning(
            "Claude Code exited with a non-zero status; checking for changes"
        )
    else:
        log.info("Claude Code finished")


def _ensure_changes(container: str, base_sha: str, deadline: float) -> None:
    status = exec_in(
        container,
        "cd /work && git status --porcelain",
        timeout=_remaining(deadline, cap=60),
    )
    commits = exec_in(
        container,
        f"cd /work && git rev-list --count {base_sha}..HEAD",
        timeout=_remaining(deadline, cap=60),
    )
    if status.returncode != 0 or commits.returncode != 0:
        raise AgentError("Could not inspect the worker working tree")

    has_uncommitted = bool((status.stdout or "").strip())
    has_commits = (commits.stdout or "").strip() not in {"", "0"}
    if not has_uncommitted and not has_commits:
        raise AgentError("Worker produced no changes")
    log.info(
        "Worker changes detected (uncommitted={}, commits={})",
        has_uncommitted,
        has_commits,
    )


def _finalize_commit(
    container: str,
    base_sha: str,
    title: str,
    run_dir: Path,
    deadline: float,
) -> None:
    """Replace any worker commits with one deterministic commit."""
    (run_dir / "commit-message.txt").write_text(title + "\n", encoding="utf-8")
    command = (
        f"cd /work && git reset --soft {base_sha} && "
        "git add -A && "
        "git commit -F /out/commit-message.txt && "
        "git log -1 --pretty=%s"
    )
    result = _exec_or_raise(
        container,
        command,
        timeout=_remaining(deadline, cap=120),
        label="finalize commit",
    )
    final_subject = (result.stdout or "").strip().splitlines()[-1]
    if final_subject != title:
        raise AgentError("Final commit message does not match the issue title")
    log.info("Finalized standardized commit: {}", title)


def _write_diff(
    container: str,
    base_sha: str,
    run_dir: Path,
    deadline: float,
) -> None:
    command = (
        "cd /work && "
        f"git diff --binary {base_sha}...HEAD "
        "> /out/diff.patch"
    )
    result = exec_in(
        container,
        command,
        timeout=_remaining(deadline, cap=120),
    )
    if result.returncode != 0:
        log.bind(returncode=result.returncode).warning(
            "Could not write diff.patch; continuing"
        )


def _run_tests(
    container: str,
    config: Config,
    run_dir: Path,
    deadline: float,
) -> str:
    commands = [*config.worker.setup_commands, *config.worker.test_commands]
    command = "cd /work && { " + " && ".join(commands) + "; } > /out/tests.txt 2>&1"
    result = exec_in(
        container,
        command,
        timeout=_remaining(deadline, cap=900),
    )
    tests_path = run_dir / "tests.txt"
    output = tests_path.read_text(encoding="utf-8", errors="replace") if tests_path.exists() else ""
    if result.returncode != 0:
        log.bind(returncode=result.returncode, tests=output[-2000:]).error(
            "Worker tests failed"
        )
        raise AgentError(f"Tests failed. See {tests_path}")
    log.info("Worker tests passed")
    return output


def _bundle(
    container: str,
    branch: str,
    run_dir: Path,
    deadline: float,
) -> Path:
    bundle = run_dir / "result.bundle"
    _exec_or_raise(
        container,
        f"cd /work && git bundle create /out/result.bundle refs/heads/{branch}",
        timeout=_remaining(deadline, cap=300),
        label="bundle",
    )
    if not bundle.exists() or bundle.stat().st_size == 0:
        raise AgentError("Worker bundle is missing or empty")
    return bundle


def _remaining(deadline: float, *, cap: int) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AgentError("Attempt timed out")
    return max(1, min(cap, int(remaining)))


def _exec_or_raise(
    container: str,
    command: str,
    *,
    timeout: int,
    label: str,
    stdout_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = exec_in(
        container,
        command,
        timeout=timeout,
        stdout_path=stdout_path,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        log.bind(label=label, returncode=result.returncode).error(
            "Worker step failed: {}", details[-2000:]
        )
        raise AgentError(f"{label} failed")
    return result

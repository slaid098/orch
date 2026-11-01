"""Worker container orchestration for one issue."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from orch.config import Config
from orch.docker import DockerError, exec_in, start_worker, stop
from orch.github import Issue


class AgentError(RuntimeError):
    """Raised when the worker cannot produce a usable result."""


@dataclass(frozen=True)
class WorkerResult:
    """Artifacts produced by a successful worker run."""

    run_dir: Path
    branch: str
    summary: str
    tests_output: str
    bundle_path: Path


def build_prompt(config: Config, issue: Issue) -> str:
    """Build the complete prompt sent to the worker agent."""
    role_path = Path(__file__).parent / "prompts" / "worker.md"
    role = role_path.read_text(encoding="utf-8")
    setup = "\n".join(f"- `{command}`" for command in config.worker.setup_commands) or "- none"
    tests = "\n".join(f"- `{command}`" for command in config.worker.test_commands)

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
        "## Output contract\n"
        "Write `/out/summary.md` and `/out/worker.json` as described above.\n"
    )


def run_worker(config: Config, issue: Issue, run_dir: Path) -> WorkerResult:
    """Run one worker container and return its artifacts.

    Args:
        config: Orchestrator configuration.
        issue: Issue to implement.
        run_dir: Host directory mounted at ``/out``.

    Returns:
        Successful worker result.

    Raises:
        AgentError: If the worker fails, produces no changes, or tests fail.
        DockerError: If the container cannot be managed.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    branch = f"agent/{issue.number}"
    prompt = build_prompt(config, issue)
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    container = f"orch-{run_dir.name.lower()}"
    start_worker(
        image=config.worker.image,
        name=container,
        repo_path=config.repo.path,
        run_dir=run_dir,
        model=config.worker.model,
    )

    try:
        _prepare_workspace(container, branch, config)
        _run_setup(container, config)
        _run_claude(container, config, run_dir)
        _ensure_changes(container, config)
        _commit_if_needed(container, issue)
        _write_diff(container, config, run_dir)
        tests_output = _run_tests(container, config, run_dir)
        bundle = _bundle(container, branch, run_dir)
        summary = _read_summary(run_dir, issue)
        return WorkerResult(
            run_dir=run_dir,
            branch=branch,
            summary=summary,
            tests_output=tests_output,
            bundle_path=bundle,
        )
    finally:
        stop(container)


def _prepare_workspace(container: str, branch: str, config: Config) -> None:
    command = (
        "git clone /src /work && "
        'git config user.name "orch" && '
        'git config user.email "orch@example.invalid" && '
        f"git switch -c {branch}"
    )
    _exec_or_raise(container, command, timeout=300, label="prepare workspace")
    logger.info("Prepared workspace on branch {}", branch)


def _run_setup(container: str, config: Config) -> None:
    if not config.worker.setup_commands:
        return
    command = "cd /work && " + " && ".join(config.worker.setup_commands)
    _exec_or_raise(
        container,
        command,
        timeout=900,
        label="setup",
    )


def _run_claude(container: str, config: Config, run_dir: Path) -> None:
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
        timeout=config.worker.timeout_minutes * 60,
        stdout_path=run_dir / "events.jsonl",
        stderr_path=run_dir / "events.stderr.log",
    )
    if result.returncode != 0:
        logger.bind(container=container, returncode=result.returncode).warning(
            "Claude Code exited with a non-zero status; checking for changes"
        )
    else:
        logger.info("Claude Code finished")


def _ensure_changes(container: str, config: Config) -> None:
    status = exec_in(container, "cd /work && git status --porcelain", timeout=60)
    commits = exec_in(
        container,
        f"cd /work && git rev-list --count origin/{config.repo.base_branch}..HEAD",
        timeout=60,
    )
    if status.returncode != 0 or commits.returncode != 0:
        raise AgentError("Could not inspect the worker working tree")

    has_uncommitted = bool((status.stdout or "").strip())
    has_commits = (commits.stdout or "").strip() not in {"", "0"}
    if not has_uncommitted and not has_commits:
        raise AgentError("Worker produced no changes")
    logger.info(
        "Worker changes detected (uncommitted={}, commits={})",
        has_uncommitted,
        has_commits,
    )


def _commit_if_needed(container: str, issue: Issue) -> None:
    status = exec_in(container, "cd /work && git status --porcelain", timeout=60)
    if status.returncode != 0:
        raise AgentError("Could not inspect the worker working tree")
    if not (status.stdout or "").strip():
        return

    message = f"agent: issue #{issue.number}"
    _exec_or_raise(
        container,
        f'cd /work && git add -A && git commit -m "{message}"',
        timeout=120,
        label="commit",
    )


def _write_diff(container: str, config: Config, run_dir: Path) -> None:
    command = (
        "cd /work && "
        f"git diff --binary origin/{config.repo.base_branch}...HEAD "
        "> /out/diff.patch"
    )
    result = exec_in(container, command, timeout=120)
    if result.returncode != 0:
        logger.bind(returncode=result.returncode).warning(
            "Could not write diff.patch; continuing"
        )


def _run_tests(container: str, config: Config, run_dir: Path) -> str:
    commands = [*config.worker.setup_commands, *config.worker.test_commands]
    command = "cd /work && { " + " && ".join(commands) + "; } > /out/tests.txt 2>&1"
    result = exec_in(container, command, timeout=900)
    tests_path = run_dir / "tests.txt"
    output = tests_path.read_text(encoding="utf-8", errors="replace") if tests_path.exists() else ""
    if result.returncode != 0:
        logger.bind(returncode=result.returncode, tests=output[-2000:]).error(
            "Worker tests failed"
        )
        raise AgentError(f"Tests failed. See {tests_path}")
    logger.info("Worker tests passed")
    return output


def _bundle(container: str, branch: str, run_dir: Path) -> Path:
    bundle = run_dir / "result.bundle"
    _exec_or_raise(
        container,
        f"cd /work && git bundle create /out/result.bundle refs/heads/{branch}",
        timeout=300,
        label="bundle",
    )
    if not bundle.exists() or bundle.stat().st_size == 0:
        raise AgentError("Worker bundle is missing or empty")
    return bundle


def _read_summary(run_dir: Path, issue: Issue) -> str:
    summary_path = run_dir / "summary.md"
    if summary_path.exists():
        text = summary_path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text

    worker_path = run_dir / "worker.json"
    if worker_path.exists():
        try:
            data = json.loads(worker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as ex:
            logger.bind(
                path=str(worker_path), error=ex, error_type=type(ex).__name__
            ).warning("Could not parse worker.json")
        else:
            summary = data.get("summary") if isinstance(data, dict) else None
            if isinstance(summary, str) and summary.strip():
                return summary.strip()

    return f"Automated changes for issue #{issue.number}."


def _exec_or_raise(
    container: str,
    command: str,
    *,
    timeout: int,
    label: str,
    stdout_path: Path | None = None,
) -> None:
    result = exec_in(
        container,
        command,
        timeout=timeout,
        stdout_path=stdout_path,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        logger.bind(label=label, returncode=result.returncode, details=details[-2000:]).error(
            "Worker step failed"
        )
        raise AgentError(f"{label} failed")

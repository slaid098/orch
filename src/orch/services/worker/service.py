"""Worker container orchestration for one issue attempt."""

from __future__ import annotations

import base64
import subprocess
import time
from pathlib import Path

import orch.services.agent.session as session
import orch.services.worker.claude as claude
from orch.config.logger import get_logger
from orch.exceptions.worker import AgentError, RunCancelled, WatchdogStop
from orch.schemas.config.types import Config
from orch.schemas.github.types import Issue
from orch.schemas.worker.types import WorkerResult
from orch.services import mcp
from orch.services.docker import exec_in, start_worker, stop
from orch.services.feedback.service import CommentFeed
from orch.services.standards import branch_name, load_handoff
from orch.services.watchdog.service import Watchdog

log = get_logger(component="worker")

_GIT_ENV = (
    "export GIT_CONFIG_GLOBAL=/out/gitconfig; git config --global --add safe.directory /work; "
)


def build_prompt(
    config: Config,
    issue: Issue,
    *,
    attempt: int = 1,
    previous_error: str | None = None,
    initial_context: str = "",
    revision_pr: str | None = None,
) -> str:
    """Build the complete prompt sent to the worker agent."""
    role_path = Path(__file__).parent / "prompt.md"
    role = role_path.read_text(encoding="utf-8")
    setup = "\n".join(f"- `{command}`" for command in config.worker.setup_commands) or "- none"
    tests = "\n".join(f"- `{command}`" for command in config.worker.test_commands)
    resume = _resume_section(attempt, previous_error)
    revision = _revision_section(revision_pr)

    return (
        f"{role}\n"
        f"#{issue.number}: {issue.title}\n\n"
        f"{issue.body}\n"
        "</issue>\n\n"
        f"{initial_context}"
        "## Repository\n"
        f"- `{config.repo.full_name}`\n"
        f"- Base branch: `{config.repo.base_branch}`\n"
        "- Work directory: `/work`\n\n"
        f"{revision}"
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


def _revision_section(revision_pr: str | None) -> str:
    if revision_pr is None:
        return ""
    return (
        "## Revision\n"
        f"This run revises the existing open pull request: {revision_pr}\n\n"
        "The `/work` directory already contains the pull-request work. Do not "
        "reset it, re-clone it, or discard changes. Inspect `git status`, "
        "`git log`, and the current `/work` diff first. Read the issue comments, "
        "then address the requested review findings or human feedback. Keep "
        "unrelated code unchanged; the orchestrator will force-update the same "
        "branch and pull request.\n\n"
    )


def _resume_section(attempt: int, previous_error: str | None) -> str:
    if attempt <= 1:
        return ""
    error = previous_error or "unknown failure"
    return (
        "## Resume\n"
        f"This is attempt {attempt}. The `/work` directory is persistent and "
        "contains work from the previous attempt. Do not re-clone it, reset it, "
        "or discard changes. Inspect `git status` and `git diff` first. A "
        "previous successful attempt may already be committed in `HEAD`; use "
        "`git log -1`, `git show --stat HEAD`, and the files themselves to see "
        "that work. Continue from the existing state and fix the reported "
        "problem.\n"
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
    feedback: CommentFeed | None = None,
    initial_context: str = "",
    revision_pr: str | None = None,
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
        feedback: Optional live comment feed for stream-json injection.
        initial_context: Comments fetched before the worker started.
        revision_pr: Existing pull request to revise, if this is a revision.

    Returns:
        Successful worker result.

    Raises:
        AgentError: If the worker fails, produces no changes, or tests fail.
        RunCancelled: If the issue is closed while the worker runs.
        WatchdogStop: If Clef decides that the run must stop.
        DockerError: If the container cannot be managed.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_path.mkdir(parents=True, exist_ok=True)
    (run_dir / "handoff.json").unlink(missing_ok=True)
    branch = branch_name(issue.title, issue.number)
    prompt = build_prompt(
        config,
        issue,
        attempt=attempt,
        previous_error=previous_error,
        initial_context=initial_context,
        revision_pr=revision_pr,
    )
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    mcp_path = mcp.write_worker_config(config, run_dir / "mcp.json")

    container = _container_name(run_dir)
    deadline = time.monotonic() + timeout_seconds
    base_sha: str | None = None
    start_worker(
        image=config.worker.image,
        name=container,
        repo_path=source_path,
        workspace_path=workspace_path,
        run_dir=run_dir,
        model=config.worker.model,
        context_tokens=claude.autocompact_tokens(config.worker.autocompact),
    )

    try:
        base_sha = _prepare_attempt(container, config, branch, deadline)
        head_before = _current_head(container, deadline)
        return _complete_attempt(
            container,
            config,
            issue,
            run_dir,
            branch,
            base_sha,
            head_before,
            deadline,
            prompt,
            feedback=feedback,
            workspace_path=workspace_path,
            mcp_config=mcp_path,
        )
    except Exception:
        if base_sha is not None:
            _checkpoint(container, base_sha, run_dir, deadline)
        raise
    finally:
        stop(container)


def _prepare_attempt(
    container: str,
    config: Config,
    branch: str,
    deadline: float,
) -> str:
    _write_settings(container, config, deadline)
    base_sha = _prepare_workspace(container, branch, deadline)
    _run_setup(container, config, deadline)
    _prepare_codegraph(container, config, deadline)
    return base_sha


def _complete_attempt(
    container: str,
    config: Config,
    issue: Issue,
    run_dir: Path,
    branch: str,
    base_sha: str,
    head_before: str,
    deadline: float,
    prompt: str,
    *,
    feedback: CommentFeed | None,
    workspace_path: Path,
    mcp_config: Path | None,
) -> WorkerResult:
    _run_agent(
        container,
        config,
        issue,
        run_dir,
        deadline,
        prompt,
        feedback=feedback,
        workspace_path=workspace_path,
        mcp_config=mcp_config,
    )
    _ensure_changes(container, head_before, deadline)
    handoff = load_handoff(run_dir)
    _finalize_commit(container, base_sha, issue.title, run_dir, deadline)
    _write_diff(container, base_sha, run_dir, deadline)
    _prepare_codegraph(container, config, deadline)
    tests_output = _run_tests(container, config, run_dir, deadline)
    bundle = _bundle(container, branch, run_dir, deadline)
    return WorkerResult(
        run_dir=run_dir,
        branch=branch,
        handoff=handoff,
        tests_output=tests_output,
        bundle_path=bundle,
    )


def _run_agent(
    container: str,
    config: Config,
    issue: Issue,
    run_dir: Path,
    deadline: float,
    prompt: str,
    *,
    feedback: CommentFeed | None,
    workspace_path: Path,
    mcp_config: Path | None,
) -> None:
    if config.feedback.live_comments or config.watchdog.enabled:
        watchdog = (
            Watchdog(
                config.watchdog,
                issue_number=issue.number,
                issue_title=issue.title,
                workspace_path=workspace_path,
            )
            if config.watchdog.enabled
            else None
        )
        _run_claude_session(
            container,
            config,
            run_dir,
            deadline,
            prompt,
            feedback=feedback,
            watchdog=watchdog,
            mcp_config=mcp_config,
        )
    else:
        _run_claude(container, config, run_dir, deadline, mcp_config)


def _write_settings(
    container: str,
    config: Config,
    deadline: float,
) -> None:
    """Write Claude Code settings that pin the context window."""
    payload = claude.settings_json(config).encode("utf-8")
    encoded = base64.b64encode(payload).decode("ascii")
    command = (
        "mkdir -p /home/agent/.claude && "
        f"printf %s {encoded} | base64 -d > /home/agent/.claude/settings.json"
    )
    _exec_or_raise(
        container,
        command,
        timeout=_remaining(deadline, cap=60),
        label="write claude settings",
    )
    log.info("Wrote Claude Code settings (autocompact={})", config.worker.autocompact)


def _checkpoint(
    container: str,
    base_sha: str,
    run_dir: Path,
    deadline: float,
) -> None:
    """Save tracked and untracked work before replacing the container."""
    command = _GIT_ENV + (
        "cd /work && "
        f"git diff --binary {base_sha} > /out/checkpoint.patch; "
        "git status --porcelain=v1 > /out/checkpoint.status; "
        "git ls-files --others --exclude-standard -z "
        "| tar --null -T - -czf /out/checkpoint-untracked.tar.gz"
    )
    try:
        result = exec_in(
            container,
            command,
            timeout=_remaining(deadline, cap=120),
        )
    except Exception as ex:
        log.bind(
            container=container,
            error=ex,
            error_type=type(ex).__name__,
        ).warning("Could not write worker checkpoint")
        return
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        log.bind(container=container, returncode=result.returncode).warning(
            "Worker checkpoint failed: {}", details[-1000:]
        )
        return
    log.info("Wrote worker checkpoint to {}", run_dir)


def _container_name(run_dir: Path) -> str:
    name = f"orch-{run_dir.parent.name}-{run_dir.name}".lower()
    return "".join(char if char.isalnum() or char in "-_." else "-" for char in name)[:63]


def _current_head(container: str, deadline: float) -> str:
    result = _exec_or_raise(
        container,
        _GIT_ENV + "cd /work && git rev-parse HEAD",
        timeout=_remaining(deadline, cap=60),
        label="read workspace HEAD",
    )
    head = (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
    if not head:
        raise AgentError("Could not resolve the workspace HEAD before the agent run")
    return head


def _prepare_workspace(container: str, branch: str, deadline: float) -> str:
    command = (
        "set -e; "
        "export GIT_CONFIG_GLOBAL=/out/gitconfig; "
        "git config --global --add safe.directory /src/.git; "
        "git config --global --add safe.directory /work; "
        "if [ ! -d /work/.git ]; then "
        "rm -rf /work || true; mkdir -p /work; git clone /src /work; "
        "fi; "
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
    lines = (result.stdout or "").strip().splitlines()
    base_sha = lines[-1] if lines else ""
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


def _prepare_codegraph(
    container: str,
    config: Config,
    deadline: float,
) -> None:
    """Build or refresh the CodeGraph index without failing the attempt."""
    if not config.mcp.enabled or not config.mcp.codegraph:
        return
    command = (
        "cd /work; "
        "{ if [ -d .codegraph ]; then codegraph sync .; "
        "else codegraph init --yes .; fi; } "
        ">> /out/codegraph.log 2>&1; status=$?; "
        "mkdir -p .git/info && "
        "grep -qxF '.codegraph/' .git/info/exclude 2>/dev/null || "
        "echo '.codegraph/' >> .git/info/exclude; "
        "exit $status"
    )
    try:
        result = exec_in(
            container,
            command,
            timeout=_remaining(deadline, cap=600),
        )
    except Exception as ex:
        log.bind(
            container=container,
            error=ex,
            error_type=type(ex).__name__,
        ).warning("CodeGraph indexing failed; the worker will fall back to Grep")
        return
    if result.returncode != 0:
        log.bind(container=container, returncode=result.returncode).warning(
            "CodeGraph indexing failed; the worker will fall back to Grep"
        )
    else:
        log.info("CodeGraph index is ready at /work/.codegraph")


def _run_claude_session(
    container: str,
    config: Config,
    run_dir: Path,
    deadline: float,
    prompt: str,
    *,
    feedback: CommentFeed | None,
    watchdog: Watchdog | None,
    mcp_config: Path | None,
) -> None:
    """Run Claude Code with stream-json input and optional live injection."""
    mcp_path = "/out/mcp.json" if mcp_config is not None else None
    command = claude.build_stream_command(config, mcp_path)
    result = session.run_session(
        container,
        config,
        run_dir,
        prompt,
        deadline,
        command=command,
        feedback=feedback,
        watchdog=watchdog,
        completion_path=run_dir / "handoff.json",
    )
    if result.cancelled:
        raise RunCancelled(result.cancel_reason or "issue closed")
    if result.stopped_by_watchdog:
        raise WatchdogStop(result.watchdog_reason or "watchdog stopped the run")
    if result.timed_out:
        raise AgentError("Attempt timed out")
    if result.returncode != 0:
        log.bind(container=container, returncode=result.returncode).warning(
            "Claude Code session exited with a non-zero status; checking for changes"
        )
    else:
        log.info("Claude Code session finished")


def _run_claude(
    container: str,
    config: Config,
    run_dir: Path,
    deadline: float,
    mcp_config: Path | None = None,
) -> None:
    mcp_path = "/out/mcp.json" if mcp_config is not None else None
    command = _GIT_ENV + "cd /work && " + claude.build_command(config, mcp_path)
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


def _ensure_changes(container: str, head_before: str, deadline: float) -> None:
    status = exec_in(
        container,
        _GIT_ENV + "cd /work && git status --porcelain",
        timeout=_remaining(deadline, cap=60),
    )
    commits = exec_in(
        container,
        _GIT_ENV + f"cd /work && git rev-list --count {head_before}..HEAD",
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
    command = _GIT_ENV + (
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
    command = _GIT_ENV + (f"cd /work && git diff --binary {base_sha}...HEAD > /out/diff.patch")
    result = exec_in(
        container,
        command,
        timeout=_remaining(deadline, cap=120),
    )
    if result.returncode != 0:
        log.bind(returncode=result.returncode).warning("Could not write diff.patch; continuing")


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
        log.bind(returncode=result.returncode, tests=output[-2000:]).error("Worker tests failed")
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
        _GIT_ENV + f"cd /work && git bundle create /out/result.bundle refs/heads/{branch}",
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

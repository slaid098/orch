"""Read-only reviewer containers and the binary JSON gate."""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from orch.config.logger import get_logger
from orch.exceptions.reviewer import ReviewerError
from orch.schemas.config.types import Config
from orch.schemas.github.types import Issue
from orch.schemas.reviewer.types import ReviewFinding, ReviewResult
from orch.services.agent import run_session
from orch.services.docker import start_reviewer, stop
from orch.services.mcp import allowed_tools as mcp_allowed_tools
from orch.services.mcp import write_reviewer_config
from orch.services.skills import resolve as resolve_skills
from orch.services.watchdog.service import Watchdog
from orch.services.worker import autocompact_tokens

log = get_logger(component="reviewer")

_REVIEW_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "line", "title", "situation", "fixes"],
                "properties": {
                    "file": {"type": "string", "minLength": 1},
                    "line": {"type": "integer", "minimum": 1},
                    "title": {"type": "string", "minLength": 1},
                    "situation": {"type": "string", "minLength": 1},
                    "fixes": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        }
    },
}

_FINDING_FIELDS = {"file", "line", "title", "situation", "fixes"}
_MAX_LAUNCHES = 2
_ROLE_PROMPTS = {
    "quality": "prompt.md",
    "security": "prompt-security.md",
}
_DEFAULT_ROLES = ("quality", "security")


def build_prompt(
    config: Config,
    issue: Issue,
    diff: str,
    checks_summary: str,
    *,
    role: str = "quality",
) -> str:
    """Build a reviewer prompt from the role, issue, diff, and checks."""
    role_path = Path(__file__).parent / _ROLE_PROMPTS[role]
    role_text = role_path.read_text(encoding="utf-8")
    schema = json.dumps(_REVIEW_SCHEMA, ensure_ascii=False, indent=2)
    checks = checks_summary.strip() or "GitHub checks did not report any entries."
    return (
        f"{role_text}\n"
        "## Issue\n"
        "<issue>\n"
        f"#{issue.number}: {issue.title}\n\n"
        f"{issue.body}\n"
        "</issue>\n\n"
        "## Repository\n"
        f"- `{config.repo.full_name}`\n"
        f"- Base branch: `{config.repo.base_branch}`\n"
        "- Read-only checkout: `/review`\n"
        f"- Reviewer role: `{role}`\n\n"
        "## Checks\n"
        f"{checks}\n\n"
        "## Diff\n"
        "<diff>\n"
        f"{diff}\n"
        "</diff>\n\n"
        "## JSON schema\n"
        "```json\n"
        f"{schema}\n"
        "```\n\n"
        "Return only the JSON document. Do not use markdown fences.\n"
    )


def run_review(
    config: Config,
    issue: Issue,
    *,
    pr_url: str,
    workspace_path: Path,
    diff_path: Path,
    run_dir: Path,
    checks_summary: str,
    timeout_seconds: int | None = None,
    roles: tuple[str, ...] | None = None,
) -> ReviewResult:
    """Run all reviewer roles and return one fail-closed binary result.

    Each role has its own read-only container and may launch twice: the first
    launch is the normal review, the second is a single repair attempt after
    invalid output or a technical recovery. A technical failure in any role
    fails the whole review; it never approves a pull request.

    Args:
        config: Orchestrator configuration.
        issue: GitHub issue used as review context.
        pr_url: Pull-request URL, included only as context.
        workspace_path: Worker checkout mounted read-only at ``/review``.
        diff_path: Diff produced by the worker.
        run_dir: Host directory for reviewer prompts, schemas, and output.
        checks_summary: Human-readable summary of green GitHub checks.
        timeout_seconds: Optional reviewer launch timeout.
        roles: Reviewer roles to run. Defaults to quality and security.

    Returns:
        Merged reviewer result. An empty finding tuple is an approval.

    Raises:
        ReviewerError: If any reviewer fails technically or returns invalid
            output after its repair launch.
    """
    selected = _DEFAULT_ROLES if roles is None else roles
    _validate_roles(selected)
    diff = _read_diff(diff_path)
    run_dir.mkdir(parents=True, exist_ok=True)
    timeout = timeout_seconds or config.reviewer.timeout_minutes * 60
    prepared = {
        role: _prepare_role(
            config,
            issue,
            diff,
            checks_summary,
            run_dir / role,
            role,
            timeout,
        )
        for role in selected
    }
    results = _run_roles(
        config,
        issue,
        pr_url,
        workspace_path,
        prepared,
        selected,
    )
    findings = tuple(finding for role in selected for finding in results[role].findings)
    log.bind(
        issue=issue.number,
        pr_url=pr_url,
        roles=list(selected),
        findings=len(findings),
    ).info(
        "Reviewer decision: {}",
        "approved" if not findings else "blocked",
    )
    return ReviewResult(findings=findings)


def _validate_roles(roles: tuple[str, ...]) -> None:
    unknown = [role for role in roles if role not in _ROLE_PROMPTS]
    if unknown:
        raise ReviewerError(f"unknown reviewer role(s): {', '.join(unknown)}")
    if not roles:
        raise ReviewerError("at least one reviewer role is required")


def _prepare_role(
    config: Config,
    issue: Issue,
    diff: str,
    checks_summary: str,
    role_dir: Path,
    role: str,
    timeout: int,
) -> tuple[Path, str, Path | None, int]:
    role_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_prompt(
        config,
        issue,
        diff,
        checks_summary,
        role=role,
    )
    _write_text(role_dir / "review-prompt.md", prompt)
    _write_text(
        role_dir / "review-schema.json",
        json.dumps(_REVIEW_SCHEMA, ensure_ascii=False, indent=2) + "\n",
    )
    mcp_path = write_reviewer_config(config, role_dir / "mcp.json")
    return role_dir, prompt, mcp_path, timeout


def _run_roles(
    config: Config,
    issue: Issue,
    pr_url: str,
    workspace_path: Path,
    prepared: dict[str, tuple[Path, str, Path | None, int]],
    roles: tuple[str, ...],
) -> dict[str, ReviewResult]:
    if len(roles) == 1:
        role = roles[0]
        return {
            role: _run_role(
                config,
                issue,
                pr_url,
                workspace_path,
                prepared[role],
                role,
            )
        }

    results: dict[str, ReviewResult] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(
        max_workers=len(roles),
        thread_name_prefix="orch-review",
    ) as executor:
        futures = {
            role: executor.submit(
                _run_role,
                config,
                issue,
                pr_url,
                workspace_path,
                prepared[role],
                role,
            )
            for role in roles
        }
    for role in roles:
        try:
            results[role] = futures[role].result()
        except Exception as ex:
            errors.append(f"{role}: {ex}")
    if errors:
        raise ReviewerError("reviewer role(s) failed: " + "; ".join(errors))
    return results


def _run_role(
    config: Config,
    issue: Issue,
    pr_url: str,
    workspace_path: Path,
    prepared: tuple[Path, str, Path | None, int],
    role: str,
) -> ReviewResult:
    role_dir, prompt, mcp_path, timeout = prepared
    last_error = "reviewer produced no output"
    for launch in range(1, _MAX_LAUNCHES + 1):
        try:
            result = _review_launch(
                config,
                issue,
                workspace_path,
                prompt=prompt,
                last_error=last_error,
                launch=launch,
                timeout=timeout,
                role=role,
                role_dir=role_dir,
                mcp_path=mcp_path,
            )
        except ReviewerError as ex:
            last_error = str(ex)
            log.bind(
                issue=issue.number,
                pr_url=pr_url,
                role=role,
                launch=launch,
                error=ex,
                error_type=type(ex).__name__,
            ).warning("Reviewer launch failed or returned invalid output")
            continue
        log.bind(
            issue=issue.number,
            pr_url=pr_url,
            role=role,
            launch=launch,
            findings=len(result.findings),
        ).info(
            "Reviewer role {}: {}",
            role,
            "approved" if result.approved else "blocked",
        )
        return result

    log.bind(
        issue=issue.number,
        pr_url=pr_url,
        role=role,
        error=last_error,
    ).error("Reviewer role failed after two launches")
    raise ReviewerError(f"{role}: {last_error}")


def _review_launch(
    config: Config,
    issue: Issue,
    workspace_path: Path,
    *,
    prompt: str,
    last_error: str,
    launch: int,
    timeout: int,
    role: str,
    role_dir: Path,
    mcp_path: Path | None,
) -> ReviewResult:
    launch_prompt = prompt if launch == 1 else _repair_prompt(prompt, last_error)
    _write_text(role_dir / "review-prompt.md", launch_prompt)
    raw = _run_launch(
        config,
        issue,
        role_dir,
        workspace_path,
        prompt=launch_prompt,
        timeout=timeout,
        launch=launch,
        role=role,
        mcp_path=mcp_path,
    )
    _write_text(role_dir / f"review-{role}-output-{launch}.json", raw)
    return parse_review(raw)


def _read_diff(diff_path: Path) -> str:
    try:
        return diff_path.read_text(encoding="utf-8", errors="replace")
    except OSError as ex:
        raise ReviewerError(f"Could not read reviewer diff: {diff_path}") from ex


def _run_launch(
    config: Config,
    issue: Issue,
    run_dir: Path,
    workspace_path: Path,
    *,
    prompt: str,
    timeout: int,
    launch: int,
    role: str,
    mcp_path: Path | None,
) -> str:
    container = _container_name(run_dir, role, launch)
    image = config.reviewer.image or config.worker.image
    model = config.reviewer.model or config.worker.model
    events_name = f"events-{launch}.jsonl"
    stderr_name = f"events-{launch}.stderr.log"
    try:
        start_reviewer(
            image=image,
            name=container,
            repo_path=workspace_path,
            run_dir=run_dir,
            model=model,
            context_tokens=autocompact_tokens(config.reviewer.autocompact),
            codegraph_enabled=config.mcp.enabled and config.mcp.codegraph,
            skills=resolve_skills(config, config.reviewer.skills),
        )
    except Exception as ex:
        raise ReviewerError(f"Could not start reviewer container: {ex}") from ex

    watchdog = (
        Watchdog(
            config.watchdog,
            issue_number=issue.number,
            issue_title=f"{issue.title} ({role} reviewer)",
            workspace_path=workspace_path,
        )
        if config.watchdog.enabled
        else None
    )
    try:
        result = run_session(
            container,
            config,
            run_dir,
            prompt,
            time.monotonic() + timeout,
            command=_command(config, mcp_path),
            workdir="/review",
            watchdog=watchdog,
            events_name=events_name,
            stderr_name=stderr_name,
        )
    except Exception as ex:
        raise ReviewerError(f"Could not execute reviewer: {ex}") from ex
    finally:
        stop(container)

    if result.timed_out:
        raise ReviewerError(
            f"reviewer timed out after {timeout}s: {_last_event(role_dir=run_dir, name=events_name)}"
        )
    if result.stopped_by_watchdog:
        raise ReviewerError(
            "watchdog stopped reviewer: " + (result.watchdog_reason or "unknown reason")
        )
    if result.returncode != 0:
        details = _read_tail(run_dir / stderr_name)
        raise ReviewerError(f"reviewer exited with {result.returncode}: {details}")
    return _final_review_text(run_dir / events_name)


def _command(config: Config, mcp_path: Path | None) -> str:
    reviewer = config.reviewer
    tools = [reviewer.allowed_tools]
    tools.extend(mcp_allowed_tools(config, "reviewer"))
    allowed = ",".join(tool for tool in tools if tool)
    mcp_flags = ""
    if mcp_path is not None:
        mcp_flags = "--mcp-config /out/mcp.json --strict-mcp-config "
    return (
        "claude -p "
        "--input-format stream-json "
        "--output-format stream-json "
        "--verbose "
        "--permission-mode plan "
        "--permission-prompts none "
        f'--allowedTools "{allowed}" '
        f"{mcp_flags}"
        f"--autocompact {reviewer.autocompact} "
        f'--model "{reviewer.model or config.worker.model}" '
        '--json-schema "$(cat /out/review-schema.json)"'
    )


def _final_review_text(events_path: Path) -> str:
    final_event: dict[str, Any] | None = None
    try:
        lines = events_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as ex:
        raise ReviewerError(f"Could not read reviewer events: {events_path}") from ex
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            final_event = event
    if final_event is None:
        raise ReviewerError("reviewer produced no final result event")
    if final_event.get("is_error"):
        raise ReviewerError(
            "reviewer result is an error: "
            + str(final_event.get("subtype") or final_event.get("result") or "unknown")
        )
    structured = final_event.get("structured_output")
    if isinstance(structured, (dict, list)):
        return json.dumps(structured, ensure_ascii=False)
    if isinstance(structured, str) and structured.strip():
        return structured.strip()
    result = final_event.get("result")
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False)
    if isinstance(result, str) and result.strip():
        return result.strip()
    raise ReviewerError("reviewer final event did not contain JSON output")


def _read_tail(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-2000:]


def _last_event(*, role_dir: Path, name: str) -> str:
    path = role_dir / name
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "no events"
    return (lines[-1] if lines else "no events")[-500:]


def parse_review(raw: str) -> ReviewResult:
    """Parse and validate a reviewer JSON document.

    Args:
        raw: Reviewer stdout.

    Returns:
        Validated review result.

    Raises:
        ReviewerError: If the JSON is missing, malformed, or violates the schema.
    """
    data = _load_json(raw)
    if not isinstance(data, dict) or set(data) != {"findings"}:
        raise ReviewerError("reviewer JSON must contain only the findings field")
    items = data["findings"]
    if not isinstance(items, list):
        raise ReviewerError("reviewer findings must be an array")
    return ReviewResult(findings=tuple(_parse_finding(item) for item in items))


def _parse_finding(item: object) -> ReviewFinding:
    if not isinstance(item, dict) or set(item) != _FINDING_FIELDS:
        raise ReviewerError("each finding must contain exactly the schema fields")
    file = _non_empty(item["file"], "finding.file")
    line = _positive_int(item["line"], "finding.line")
    title = _non_empty(item["title"], "finding.title")
    situation = _non_empty(item["situation"], "finding.situation")
    fixes_raw = item["fixes"]
    if not isinstance(fixes_raw, list):
        raise ReviewerError("finding.fixes must be an array")
    fixes = tuple(_non_empty(fix, "finding.fixes[]") for fix in fixes_raw)
    return ReviewFinding(
        file=file,
        line=line,
        title=title,
        situation=situation,
        fixes=fixes,
    )


def _load_json(raw: str) -> Any:
    text = raw.strip()
    if not text:
        raise ReviewerError("reviewer returned empty output")
    last_error: json.JSONDecodeError | None = None
    for candidate in (text, _fenced_json(text), _braced_json(text)):
        if candidate is None:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as ex:
            last_error = ex
    raise ReviewerError("reviewer output is not valid JSON") from last_error


def _fenced_json(text: str) -> str | None:
    start = text.find("```")
    if start < 0:
        return None
    start = text.find("\n", start)
    if start < 0:
        return None
    end = text.find("```", start + 1)
    if end < 0:
        return None
    return text[start + 1 : end].strip()


def _braced_json(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    return text[start : end + 1]


def _repair_prompt(prompt: str, error: str) -> str:
    return (
        f"{prompt}\n\n"
        "## Repair\n"
        f"Предыдущий ответ не прошёл проверку: {error}\n"
        "Повтори ревью и верни только валидный JSON по схеме. "
        "Без markdown, без пояснений и без текста вокруг JSON.\n"
    )


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _non_empty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewerError(f"{name} must be a non-empty string")
    return value.strip()


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ReviewerError(f"{name} must be a positive integer")
    return value


def _container_name(run_dir: Path, role: str, launch: int) -> str:
    identity = hashlib.sha1(str(run_dir.resolve()).encode("utf-8")).hexdigest()[:12]
    name = f"orch-review-{identity}-{role}-{launch}".lower()
    return "".join(char if char.isalnum() or char in "-_." else "-" for char in name)[:63]

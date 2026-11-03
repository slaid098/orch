from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orch.exceptions.reviewer import ReviewerError
from orch.schemas.config.types import (
    Config,
    LabelsConfig,
    RepoConfig,
    ReviewerConfig,
    WorkerConfig,
)
from orch.schemas.github.types import Issue
from orch.schemas.reviewer.types import ReviewFinding, ReviewResult
from orch.services.reviewer import service as reviewer


def test_validate_roles_rejects_an_empty_selection() -> None:
    with pytest.raises(ReviewerError, match="at least one"):
        reviewer._validate_roles(())


def _config(tmp_path: Path) -> Config:
    return Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(
            image="worker:latest",
            model="worker-model",
            test_commands=("pytest",),
        ),
        data_dir=tmp_path / "data",
        reviewer=ReviewerConfig(
            enabled=True,
            image="reviewer:latest",
            model="reviewer-model",
            timeout_minutes=1,
            max_rounds=3,
        ),
    )


def _issue() -> Issue:
    return Issue(
        number=9,
        title="fix(pipeline): add reliable retry path",
        body="Implement the retry path.",
        url="https://example.test/issues/9",
        author="slaid098",
        labels=("agent:queued",),
    )


def _proc(
    stdout: str = "",
    stderr: str = "",
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["claude"], code, stdout, stderr)


def _prepare_review(tmp_path: Path) -> tuple[Path, Path, Path]:
    run_dir = tmp_path / "run" / "review-1"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    diff_path = tmp_path / "run" / "attempt-1" / "diff.patch"
    diff_path.parent.mkdir(parents=True)
    diff_path.write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
    return run_dir, workspace, diff_path


def _patch_reviewer(
    monkeypatch: pytest.MonkeyPatch,
    outputs: list[subprocess.CompletedProcess[str]],
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    starts: list[dict[str, object]] = []
    commands: list[str] = []
    stops: list[str] = []

    def start(**kwargs: object) -> str:
        starts.append(kwargs)
        return "container"

    def execute(
        name: str,
        command: str,
        *,
        timeout: int,
        workdir: str,
    ) -> subprocess.CompletedProcess[str]:
        del name, timeout, workdir
        commands.append(command)
        if not outputs:
            pytest.fail("unexpected reviewer launch")
        return outputs.pop(0)

    monkeypatch.setattr(reviewer, "start_reviewer", start)
    monkeypatch.setattr(reviewer, "exec_in", execute)
    monkeypatch.setattr(reviewer, "stop", lambda name: stops.append(name))
    return starts, commands, stops


def test_parse_review_approves_empty_findings() -> None:
    result = reviewer.parse_review('{"findings": []}')

    assert result.approved
    assert result.findings == ()


def test_parse_review_accepts_all_finding_fields() -> None:
    payload = {
        "findings": [
            {
                "file": "src/a.py",
                "line": 12,
                "title": "Off-by-one",
                "situation": "The wrong element is returned.",
                "fixes": ["Use index", "Add a regression test"],
            }
        ]
    }

    result = reviewer.parse_review(json.dumps(payload))

    assert not result.approved
    finding = result.findings[0]
    assert finding.file == "src/a.py"
    assert finding.line == 12
    assert finding.fixes == ("Use index", "Add a regression test")


def test_parse_review_accepts_fenced_json() -> None:
    result = reviewer.parse_review('```json\n{"findings": []}\n```')

    assert result.approved


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"findings": [], "verdict": "approve"}',
        '{"findings": [{"file": "a", "line": 1, "title": "t"}]}',
        '{"findings": [{"file": "a", "line": 0, "title": "t", "situation": "s", "fixes": []}]}',
        '{"findings": [{"file": "a", "line": true, "title": "t", "situation": "s", "fixes": []}]}',
        '{"findings": [{"file": "a", "line": 1, "title": "t", "situation": "s", "fixes": [""]}]}',
        '{"findings": [{"file": "a", "line": 1, "title": "t", "situation": "s", "fixes": "x"}]}',
        '{"findings": [{"file": "a", "line": 1, "title": "t", "situation": "s", "fixes": [], "severity": "minor"}]}',
        '{"findings": {}}',
        "[]",
        "not json",
        "",
    ],
)
def test_parse_review_fails_closed(payload: str) -> None:
    with pytest.raises(ReviewerError):
        reviewer.parse_review(payload)


def test_build_prompt_contains_issue_diff_checks_and_schema(tmp_path: Path) -> None:
    prompt = reviewer.build_prompt(
        _config(tmp_path),
        _issue(),
        "diff --git a/a.py b/a.py\n",
        "pytest: success",
    )

    assert "#9: fix(pipeline): add reliable retry path" in prompt
    assert "Implement the retry path." in prompt
    assert "diff --git a/a.py b/a.py" in prompt
    assert "pytest: success" in prompt
    assert "/review" in prompt
    assert '"findings"' in prompt
    assert '"severity"' not in prompt


def test_run_review_approves_and_records_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_dir, workspace, diff_path = _prepare_review(tmp_path)
    starts, commands, stops = _patch_reviewer(
        monkeypatch,
        [_proc('{"findings": []}\n')],
    )

    result = reviewer.run_review(
        config,
        _issue(),
        pr_url="https://example.test/pull/10",
        workspace_path=workspace,
        diff_path=diff_path,
        run_dir=run_dir,
        checks_summary="pytest: success",
        roles=("quality",),
    )

    assert result.approved
    assert starts[0]["repo_path"] == workspace
    assert starts[0]["run_dir"] == run_dir / "quality"
    assert starts[0]["image"] == "reviewer:latest"
    assert starts[0]["model"] == "reviewer-model"
    assert starts[0]["context_tokens"] == 200_000
    assert "--permission-mode plan" in commands[0]
    assert "Read,Grep,Glob" in commands[0]
    assert "mcp__codegraph__codegraph_explore" in commands[0]
    assert "mcp__context7__query-docs" in commands[0]
    assert "--mcp-config /out/mcp.json --strict-mcp-config" in commands[0]
    assert (run_dir / "quality" / "mcp.json").exists()
    assert "--json-schema" in commands[0]
    assert "GH_TOKEN" not in commands[0]
    assert len(stops) == 1
    assert stops[0].startswith("orch-review-")
    assert (run_dir / "quality" / "review-quality-output-1.json").read_text(
        encoding="utf-8"
    ) == '{"findings": []}'


def test_run_review_repairs_invalid_json_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_dir, workspace, diff_path = _prepare_review(tmp_path)
    prompts: list[str] = []

    def start(**kwargs: object) -> str:
        prompt = (run_dir / "quality" / "review-prompt.md").read_text(encoding="utf-8")
        prompts.append(prompt)
        return "container"

    monkeypatch.setattr(reviewer, "start_reviewer", start)
    monkeypatch.setattr(
        reviewer,
        "exec_in",
        lambda *_args, **_kwargs: _proc('{"findings": []}' if len(prompts) == 2 else "not json"),
    )
    monkeypatch.setattr(reviewer, "stop", lambda _name: None)

    result = reviewer.run_review(
        config,
        _issue(),
        pr_url="https://example.test/pull/10",
        workspace_path=workspace,
        diff_path=diff_path,
        run_dir=run_dir,
        checks_summary="pytest: success",
        roles=("quality",),
    )

    assert result.approved
    assert len(prompts) == 2
    assert "## Repair" not in prompts[0]
    assert "## Repair" in prompts[1]
    assert (run_dir / "quality" / "review-quality-output-1.json").read_text(
        encoding="utf-8"
    ) == "not json"
    assert (run_dir / "quality" / "review-quality-output-2.json").read_text(
        encoding="utf-8"
    ) == '{"findings": []}'


def test_run_review_raises_after_two_invalid_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_dir, workspace, diff_path = _prepare_review(tmp_path)
    _patch_reviewer(
        monkeypatch,
        [_proc("not json"), _proc("still not json")],
    )

    with pytest.raises(ReviewerError, match="not valid JSON"):
        reviewer.run_review(
            config,
            _issue(),
            pr_url="https://example.test/pull/10",
            workspace_path=workspace,
            diff_path=diff_path,
            run_dir=run_dir,
            checks_summary="pytest: success",
            roles=("quality",),
        )


def test_run_review_retries_a_technical_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_dir, workspace, diff_path = _prepare_review(tmp_path)
    starts: list[str] = []

    def start(**_kwargs: object) -> str:
        starts.append("start")
        if len(starts) == 1:
            raise RuntimeError("docker unavailable")
        return "container"

    monkeypatch.setattr(reviewer, "start_reviewer", start)
    monkeypatch.setattr(
        reviewer,
        "exec_in",
        lambda *_args, **_kwargs: _proc('{"findings": []}'),
    )
    monkeypatch.setattr(reviewer, "stop", lambda _name: None)

    result = reviewer.run_review(
        config,
        _issue(),
        pr_url="https://example.test/pull/10",
        workspace_path=workspace,
        diff_path=diff_path,
        run_dir=run_dir,
        checks_summary="pytest: success",
        roles=("quality",),
    )

    assert result.approved
    assert len(starts) == 2


def test_run_review_raises_after_two_technical_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_dir, workspace, diff_path = _prepare_review(tmp_path)
    monkeypatch.setattr(
        reviewer,
        "start_reviewer",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("docker unavailable")),
    )

    with pytest.raises(ReviewerError, match="Could not start"):
        reviewer.run_review(
            config,
            _issue(),
            pr_url="https://example.test/pull/10",
            workspace_path=workspace,
            diff_path=diff_path,
            run_dir=run_dir,
            checks_summary="pytest: success",
            roles=("quality",),
        )


def test_run_review_stops_container_when_execution_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_dir, workspace, diff_path = _prepare_review(tmp_path)
    stops: list[str] = []
    monkeypatch.setattr(reviewer, "start_reviewer", lambda **_kwargs: "container")
    monkeypatch.setattr(
        reviewer,
        "exec_in",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("exec failed")),
    )
    monkeypatch.setattr(reviewer, "stop", lambda name: stops.append(name))

    with pytest.raises(ReviewerError, match="Could not execute"):
        reviewer.run_review(
            config,
            _issue(),
            pr_url="https://example.test/pull/10",
            workspace_path=workspace,
            diff_path=diff_path,
            run_dir=run_dir,
            checks_summary="pytest: success",
            roles=("quality",),
        )

    assert len(stops) == 2
    assert all(name.startswith("orch-review-") for name in stops)


def test_run_review_raises_when_diff_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_dir = tmp_path / "run" / "review-1"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        reviewer,
        "start_reviewer",
        lambda **_kwargs: pytest.fail("reviewer must not start"),
    )

    with pytest.raises(ReviewerError, match="Could not read"):
        reviewer.run_review(
            config,
            _issue(),
            pr_url="https://example.test/pull/10",
            workspace_path=workspace,
            diff_path=tmp_path / "missing.patch",
            run_dir=run_dir,
            checks_summary="pytest: success",
            roles=("quality",),
        )


def test_run_review_rejects_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_dir, workspace, diff_path = _prepare_review(tmp_path)
    _patch_reviewer(
        monkeypatch,
        [_proc(stderr="bad", code=2), _proc(stderr="bad", code=2)],
    )

    with pytest.raises(ReviewerError, match="exited with 2"):
        reviewer.run_review(
            config,
            _issue(),
            pr_url="https://example.test/pull/10",
            workspace_path=workspace,
            diff_path=diff_path,
            run_dir=run_dir,
            checks_summary="pytest: success",
            roles=("quality",),
        )


def test_run_review_runs_quality_and_security_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_dir, workspace, diff_path = _prepare_review(tmp_path)
    seen: list[str] = []

    def fake_role(
        _config: Config,
        _issue: Issue,
        _pr_url: str,
        _workspace: Path,
        _prepared: tuple[Path, str, Path | None, int],
        role: str,
    ) -> ReviewResult:
        seen.append(role)
        return ReviewResult(
            findings=(
                ReviewFinding(
                    file="a.py",
                    line=1,
                    title=f"{role} finding",
                    situation="context",
                    fixes=(),
                ),
            )
        )

    monkeypatch.setattr(reviewer, "_run_role", fake_role)

    result = reviewer.run_review(
        config,
        _issue(),
        pr_url="https://example.test/pull/10",
        workspace_path=workspace,
        diff_path=diff_path,
        run_dir=run_dir,
        checks_summary="pytest: success",
    )

    assert set(seen) == {"quality", "security"}
    assert {finding.title for finding in result.findings} == {
        "quality finding",
        "security finding",
    }
    assert (run_dir / "quality" / "review-prompt.md").exists()
    assert (run_dir / "security" / "review-prompt.md").exists()


def test_build_prompt_selects_the_security_role(tmp_path: Path) -> None:
    prompt = reviewer.build_prompt(
        _config(tmp_path),
        _issue(),
        "diff --git a/a.py b/a.py",
        "pytest: success",
        role="security",
    )

    assert "security-ревьюер" in prompt
    assert "Reviewer role: `security`" in prompt


def test_run_review_fails_closed_when_one_role_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_dir, workspace, diff_path = _prepare_review(tmp_path)

    def fake_role(
        _config: Config,
        _issue: Issue,
        _pr_url: str,
        _workspace: Path,
        _prepared: tuple[Path, str, Path | None, int],
        role: str,
    ) -> ReviewResult:
        if role == "security":
            raise ReviewerError("security reviewer exploded")
        return ReviewResult(findings=())

    monkeypatch.setattr(reviewer, "_run_role", fake_role)

    with pytest.raises(ReviewerError, match="security"):
        reviewer.run_review(
            config,
            _issue(),
            pr_url="https://example.test/pull/10",
            workspace_path=workspace,
            diff_path=diff_path,
            run_dir=run_dir,
            checks_summary="pytest: success",
        )

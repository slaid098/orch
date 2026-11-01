from __future__ import annotations

from pathlib import Path

from orch.schemas.config.types import Config, LabelsConfig, RepoConfig, WorkerConfig
from orch.schemas.github.types import Issue
from orch.services.worker.service import build_prompt


def _config(tmp_path: Path) -> Config:
    return Config(
        repo=RepoConfig(
            owner="slaid098",
            name="orch",
            path=tmp_path,
        ),
        labels=LabelsConfig(),
        worker=WorkerConfig(
            setup_commands=("uv sync --all-extras",),
            test_commands=("uv run pytest -q",),
        ),
        data_dir=tmp_path / "data",
    )


def test_build_prompt_contains_issue_and_commands(tmp_path: Path) -> None:
    issue = Issue(
        number=7,
        title="test(worker): add a useful test",
        body="Please add a test.",
        url="https://example.test/issues/7",
        author="slaid098",
        labels=("agent:queued",),
    )

    prompt = build_prompt(_config(tmp_path), issue)

    assert "#7: test(worker): add a useful test" in prompt
    assert "Please add a test." in prompt
    assert "uv sync --all-extras" in prompt
    assert "uv run pytest -q" in prompt
    assert "/out/handoff.json" in prompt
    assert "Do not commit" in prompt
    assert "\"watch_out\"" in prompt


def test_build_prompt_resumes_existing_workspace(tmp_path: Path) -> None:
    issue = Issue(
        number=8,
        title="fix(worker): keep retry work",
        body="Continue.",
        url="https://example.test/issues/8",
        author="slaid098",
        labels=("agent:queued",),
    )

    prompt = build_prompt(
        _config(tmp_path),
        issue,
        attempt=2,
        previous_error="tests failed",
    )

    assert "## Resume" in prompt
    assert "persistent" in prompt
    assert "Do not re-clone" in prompt
    assert "tests failed" in prompt

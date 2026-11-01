from __future__ import annotations

from pathlib import Path

from orch.agent import build_prompt
from orch.config import Config, LabelsConfig, RepoConfig, WorkerConfig
from orch.github import Issue


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
        title="Add a useful test",
        body="Please add a test.",
        url="https://example.test/issues/7",
        author="slaid098",
        labels=("agent:ready",),
    )

    prompt = build_prompt(_config(tmp_path), issue)

    assert "#7: Add a useful test" in prompt
    assert "Please add a test." in prompt
    assert "uv sync --all-extras" in prompt
    assert "uv run pytest -q" in prompt
    assert "/out/summary.md" in prompt

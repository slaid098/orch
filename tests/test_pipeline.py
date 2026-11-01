from __future__ import annotations

import time
from pathlib import Path

import pytest

from orch import pipeline, store
from orch.agent import WorkerResult
from orch.config import Config, LabelsConfig, RepoConfig, WorkerConfig
from orch.github import Issue
from orch.schemas.standards.types import Handoff


def _config(tmp_path: Path) -> Config:
    return Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(
            timeout_minutes=1,
            max_attempts=2,
            test_commands=("pytest",),
        ),
        data_dir=tmp_path / "data",
        max_workers=2,
    )


def _issue() -> Issue:
    return Issue(
        number=9,
        title="fix(pipeline): add reliable retry path",
        body="Do it.",
        url="https://example.test/issues/9",
        author="slaid098",
        labels=("agent:queued",),
    )


def _handoff() -> Handoff:
    return Handoff(
        what="Добавлен повторный запуск с сохранением рабочей папки.",
        why="Сделанная работа не должна теряться при перезапуске.",
        watch_out="—",
        pending="—",
    )


def test_attempt_budget_covers_source_preparation() -> None:
    assert pipeline._remaining_attempt_seconds(time.monotonic(), 10) > 0
    with pytest.raises(TimeoutError):
        pipeline._remaining_attempt_seconds(
            time.monotonic() - 11,
            10,
        )


def test_retry_uses_fresh_source_and_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    issue = _issue()
    sources: list[Path] = []
    comments: list[str] = []
    deleted: list[str] = []
    prs: list[dict[str, object]] = []

    monkeypatch.setattr(pipeline, "refresh_mirror", lambda *args: tmp_path / "mirror.git")

    def clone_source(_mirror: Path, _base: str, destination: Path) -> Path:
        destination.mkdir(parents=True)
        return destination

    monkeypatch.setattr(pipeline, "clone_source", clone_source)
    monkeypatch.setattr(pipeline, "cleanup_source", lambda _source: None)
    monkeypatch.setattr(pipeline, "_publish_branch", lambda *_args: None)
    monkeypatch.setattr(pipeline.github, "find_open_pr", lambda *_args: None)
    monkeypatch.setattr(
        pipeline.github,
        "delete_branch",
        lambda _repo, branch, _token: deleted.append(branch),
    )
    monkeypatch.setattr(
        pipeline.github,
        "comment_issue",
        lambda _repo, _number, _token, body: comments.append(body),
    )
    monkeypatch.setattr(
        pipeline.github,
        "edit_labels",
        lambda *_args, **_kwargs: None,
    )

    def create_pr(
        _repo: str,
        _token: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        prs.append({"head": head, "base": base, "title": title, "body": body})
        return "https://example.test/pull/10"

    monkeypatch.setattr(pipeline.github, "create_pr", create_pr)

    def run_worker(
        _config: Config,
        _issue: Issue,
        run_dir: Path,
        *,
        source_path: Path,
        timeout_seconds: int,
    ) -> WorkerResult:
        assert 0 < timeout_seconds <= 10
        sources.append(source_path)
        if len(sources) == 1:
            raise RuntimeError("first attempt broke")
        return WorkerResult(
            run_dir=run_dir,
            branch="fix/pipeline/issue-9-add-reliable-retry-path",
            handoff=_handoff(),
            tests_output="passed",
            bundle_path=run_dir / "result.bundle",
        )

    monkeypatch.setattr(pipeline, "run_worker", run_worker)

    outcome = pipeline.run_issue(
        config,
        issue,
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "pr_open"
    assert outcome.pr_url == "https://example.test/pull/10"
    assert len(sources) == 2
    assert sources[0] != sources[1]
    assert deleted == ["fix/pipeline/issue-9-add-reliable-retry-path"]
    assert prs[0]["title"] == "fix(pipeline): add reliable retry path"
    assert "## Что сделано" in str(prs[0]["body"])
    assert "Closes #9" in str(prs[0]["body"])
    assert any("Попытка 1/2" in body for body in comments)

    latest = store.latest_run_for_issue(
        config.data_dir / "orch.db",
        "slaid098/orch",
        9,
    )
    assert latest is not None
    assert latest["status"] == "pr_open"


def test_invalid_issue_title_is_parked_without_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    issue = Issue(
        number=10,
        title="Add a reliable retry path",
        body="Do it.",
        url="https://example.test/issues/10",
        author="slaid098",
        labels=("agent:queued",),
    )
    comments: list[str] = []
    labels: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    monkeypatch.setattr(
        pipeline.github,
        "comment_issue",
        lambda _repo, _number, _token, body: comments.append(body),
    )
    monkeypatch.setattr(
        pipeline.github,
        "edit_labels",
        lambda _repo, _number, _token, *, add, remove: labels.append((add, remove)),
    )
    monkeypatch.setattr(
        pipeline,
        "run_worker",
        lambda *_args, **_kwargs: pytest.fail("worker must not start"),
    )

    outcome = pipeline.run_issue(config, issue, "token")

    assert outcome.status == "needs_human"
    assert outcome.branch == ""
    assert any("заголовок issue" in body for body in comments)
    assert labels == [
        (
            ("agent:needs-human",),
            ("agent:running", "agent:queued"),
        )
    ]


def test_exhausted_attempts_mark_needs_human(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    issue = _issue()
    labels: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    monkeypatch.setattr(pipeline, "refresh_mirror", lambda *args: tmp_path / "mirror.git")

    def clone_source(_mirror: Path, _base: str, destination: Path) -> Path:
        destination.mkdir(parents=True)
        return destination

    monkeypatch.setattr(pipeline, "clone_source", clone_source)
    monkeypatch.setattr(pipeline, "cleanup_source", lambda _source: None)
    monkeypatch.setattr(pipeline.github, "find_open_pr", lambda *_args: None)
    monkeypatch.setattr(pipeline.github, "delete_branch", lambda *_args: None)
    monkeypatch.setattr(pipeline.github, "comment_issue", lambda *_args: None)
    monkeypatch.setattr(
        pipeline.github,
        "edit_labels",
        lambda _repo, _number, _token, *, add, remove: labels.append((add, remove)),
    )

    def run_worker(*_args: object, **_kwargs: object) -> WorkerResult:
        raise RuntimeError("always broken")

    monkeypatch.setattr(pipeline, "run_worker", run_worker)

    outcome = pipeline.run_issue(
        config,
        issue,
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "needs_human"
    assert labels[0] == (
        ("agent:running",),
        ("agent:queued", "agent:needs-human"),
    )
    assert labels[-1] == (
        ("agent:needs-human",),
        ("agent:running", "agent:queued"),
    )
    latest = store.latest_run_for_issue(
        config.data_dir / "orch.db",
        "slaid098/orch",
        9,
    )
    assert latest is not None
    assert latest["status"] == "needs_human"
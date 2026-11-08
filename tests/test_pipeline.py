from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from orch.exceptions.github import GitHubError
from orch.exceptions.worker import RunCancelled, WatchdogStop
from orch.schemas.config.types import (
    Config,
    LabelsConfig,
    RepoConfig,
    ReviewerConfig,
    WorkerConfig,
)
from orch.schemas.github.types import Issue
from orch.schemas.standards.types import Handoff
from orch.schemas.worker.types import WorkerResult
from orch.services import store
from orch.services.pipeline import publish
from orch.services.pipeline import service as pipeline


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
        reviewer=ReviewerConfig(enabled=False),
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


def _run_status(db_path: Path) -> str | None:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("SELECT status FROM runs LIMIT 1").fetchone()
    return row[0] if row else None


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


def test_retry_reuses_source_and_workspace_and_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    issue = _issue()
    sources: list[Path] = []
    workspaces: list[Path] = []
    attempts: list[int] = []
    previous_errors: list[str | None] = []
    cleaned_sources: list[Path] = []
    comments: list[str] = []
    deleted: list[str] = []
    prs: list[dict[str, object]] = []

    monkeypatch.setattr(pipeline.gitops, "refresh_mirror", lambda *args: tmp_path / "mirror.git")
    monkeypatch.setattr(
        pipeline,
        "_wait_for_checks",
        lambda *_args: "CI: success",
    )

    def clone_source(_mirror: Path, _base: str, destination: Path) -> Path:
        destination.mkdir(parents=True)
        return destination

    monkeypatch.setattr(pipeline.gitops, "clone_source", clone_source)
    monkeypatch.setattr(
        pipeline.gitops,
        "cleanup_source",
        lambda source: cleaned_sources.append(source),
    )
    monkeypatch.setattr(publish, "_publish_branch", lambda *_args: None)
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
        workspace_path: Path,
        attempt: int,
        previous_error: str | None,
        timeout_seconds: int,
    ) -> WorkerResult:
        assert 0 < timeout_seconds <= 10
        sources.append(source_path)
        workspaces.append(workspace_path)
        attempts.append(attempt)
        previous_errors.append(previous_error)
        if attempt == 1:
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
    assert sources[0] == sources[1]
    assert len(set(workspaces)) == 1
    assert workspaces[0] == (
        config.data_dir / "workspaces" / "slaid098__orch" / "issue-9"
    )
    assert attempts == [1, 2]
    assert previous_errors == [None, "first attempt broke"]
    assert cleaned_sources == [sources[0]]
    assert deleted == ["fix/pipeline/issue-9-add-reliable-retry-path"]
    assert prs[0]["title"] == "fix(pipeline): add reliable retry path"
    assert "## Что сделано" in str(prs[0]["body"])
    assert "Closes #9" in str(prs[0]["body"])
    assert any("Попытка 1/2" in body for body in comments)

    assert _run_status(config.data_dir / "orch.db") == "pr_open"


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

    monkeypatch.setattr(pipeline.gitops, "refresh_mirror", lambda *args: tmp_path / "mirror.git")
    monkeypatch.setattr(
        pipeline,
        "_wait_for_checks",
        lambda *_args: "CI: success",
    )

    def clone_source(_mirror: Path, _base: str, destination: Path) -> Path:
        destination.mkdir(parents=True)
        return destination

    monkeypatch.setattr(pipeline.gitops, "clone_source", clone_source)
    monkeypatch.setattr(pipeline.gitops, "cleanup_source", lambda _source: None)
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
        ("agent:queued", "agent:needs-human", "agent:pr-open"),
    )
    assert labels[-1] == (
        ("agent:needs-human",),
        ("agent:running", "agent:queued"),
    )
    assert _run_status(config.data_dir / "orch.db") == "needs_human"


def _prepare_cancellation_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        pipeline.gitops,
        "refresh_mirror",
        lambda *_args: tmp_path / "mirror.git",
    )
    monkeypatch.setattr(
        pipeline,
        "_wait_for_checks",
        lambda *_args: "CI: success",
    )

    def clone_source(_mirror: Path, _base: str, destination: Path) -> Path:
        destination.mkdir(parents=True)
        return destination

    monkeypatch.setattr(pipeline.gitops, "clone_source", clone_source)
    monkeypatch.setattr(pipeline.gitops, "cleanup_source", lambda _source: None)
    monkeypatch.setattr(pipeline.github, "find_open_pr", lambda *_args: None)
    monkeypatch.setattr(pipeline.github, "edit_labels", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline.github, "comment_issue", lambda *_args, **_kwargs: None)


def test_closed_issue_is_cancelled_before_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    _prepare_cancellation_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: False)
    monkeypatch.setattr(
        pipeline,
        "run_worker",
        lambda *_args, **_kwargs: pytest.fail("worker must not start"),
    )

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "cancelled"
    assert outcome.error == "issue closed"
    assert _run_status(config.data_dir / "orch.db") == "cancelled"


def test_worker_cancellation_does_not_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    calls: list[int] = []
    _prepare_cancellation_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: True)

    def run_worker(*_args: object, **kwargs: object) -> WorkerResult:
        calls.append(int(kwargs.get("attempt", 0)))
        raise RunCancelled("closed during run")

    monkeypatch.setattr(pipeline, "run_worker", run_worker)

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "cancelled"
    assert calls == [1]
    assert _run_status(config.data_dir / "orch.db") == "cancelled"


def test_watchdog_stop_parks_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    calls: list[int] = []
    comments: list[str] = []
    labels: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    deleted: list[str] = []
    _prepare_cancellation_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: True)
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
        pipeline.github,
        "delete_branch",
        lambda _repo, branch, _token: deleted.append(branch),
    )

    def run_worker(*_args: object, **kwargs: object) -> WorkerResult:
        calls.append(int(kwargs.get("attempt", 0)))
        raise WatchdogStop("loop confirmed")

    monkeypatch.setattr(pipeline, "run_worker", run_worker)

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "needs_human"
    assert calls == [1]
    assert deleted == []
    assert labels[-1] == (
        ("agent:needs-human",),
        ("agent:running", "agent:queued"),
    )
    assert any("loop confirmed" in body for body in comments)
    assert any("Попыток: `1` из `2`" in body for body in comments)


def test_closed_issue_cancels_before_pull_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    checks = iter([True, False])
    published: list[str] = []
    prs: list[str] = []
    _prepare_cancellation_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(
        pipeline.github,
        "issue_is_open",
        lambda *_args: next(checks, False),
    )
    monkeypatch.setattr(
        publish,
        "_publish_branch",
        lambda _config, _worker, _token: published.append("published"),
    )
    monkeypatch.setattr(
        pipeline.github,
        "create_pr",
        lambda *_args, **_kwargs: prs.append("created") or "url",
    )

    def run_worker(
        _config: Config,
        _issue: Issue,
        run_dir: Path,
        **_kwargs: object,
    ) -> WorkerResult:
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
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "cancelled"
    assert published == []
    assert prs == []


def test_issue_state_error_does_not_cancel_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    prs: list[str] = []
    _prepare_cancellation_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(
        pipeline.github,
        "issue_is_open",
        lambda *_args: (_ for _ in ()).throw(GitHubError("offline")),
    )
    monkeypatch.setattr(publish, "_publish_branch", lambda *_args: None)
    monkeypatch.setattr(
        pipeline.github,
        "create_pr",
        lambda *_args, **_kwargs: prs.append("created") or "https://example.test/pr/9",
    )

    def run_worker(
        _config: Config,
        _issue: Issue,
        run_dir: Path,
        **_kwargs: object,
    ) -> WorkerResult:
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
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "pr_open"
    assert prs == ["created"]
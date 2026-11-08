from __future__ import annotations

from pathlib import Path

import pytest

from orch.exceptions.github import GitHubError
from orch.exceptions.pipeline import PublicationError
from orch.schemas.config.types import (
    Config,
    LabelsConfig,
    RepoConfig,
    ReviewerConfig,
    WorkerConfig,
)
from orch.schemas.github.types import Issue, PullRequestStatus
from orch.schemas.standards.types import Handoff
from orch.schemas.worker.types import WorkerResult
from orch.services import store
from orch.services.pipeline import publish
from orch.services.pipeline import service as pipeline


def _config(tmp_path: Path, *, max_attempts: int = 2) -> Config:
    return Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(
            timeout_minutes=1,
            max_attempts=max_attempts,
            test_commands=("pytest",),
        ),
        data_dir=tmp_path / "data",
        reviewer=ReviewerConfig(enabled=False),
    )


def _issue(labels: tuple[str, ...] = ("agent:queued",)) -> Issue:
    return Issue(
        number=9,
        title="fix(pipeline): add reliable retry path",
        body="Do it.",
        url="https://example.test/issues/9",
        author="slaid098",
        labels=labels,
    )


def _handoff() -> Handoff:
    return Handoff(
        what="Добавлен повторный запуск.",
        why="Работа не должна теряться.",
        watch_out="—",
        pending="—",
    )


def _worker(run_dir: Path, branch: str) -> WorkerResult:
    return WorkerResult(
        run_dir=run_dir,
        branch=branch,
        handoff=_handoff(),
        tests_output="passed",
        bundle_path=run_dir / "result.bundle",
    )


def test_existing_pr_is_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    comments: list[str] = []
    labels: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    cleaned: list[Path] = []

    monkeypatch.setattr(
        pipeline.github,
        "find_open_pr",
        lambda *_args: "https://example.test/pr/9",
    )
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
        pipeline.gitops,
        "cleanup_workspace",
        lambda path: cleaned.append(path),
    )
    monkeypatch.setattr(
        pipeline,
        "run_worker",
        lambda *_args, **_kwargs: pytest.fail("worker must not start"),
    )

    outcome = pipeline.run_issue(config, _issue(labels=()), "token")

    assert outcome.status == "pr_open"
    assert outcome.pr_url == "https://example.test/pr/9"
    assert comments and "PR уже открыт" in comments[0]
    assert labels == [
        (
            ("agent:pr-open",),
            ("agent:queued", "agent:running", "agent:needs-human"),
        )
    ]
    assert cleaned == []


def test_queued_existing_pr_revises_the_saved_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    pr_url = "https://example.test/pr/9"
    workspace = config.data_dir / "workspaces" / "slaid098__orch" / "issue-9"
    (workspace / ".git").mkdir(parents=True)
    revisions: list[str | None] = []
    comments: list[str] = []
    labels: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    cleaned: list[Path] = []

    monkeypatch.setattr(pipeline.github, "find_open_pr", lambda *_args: pr_url)
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: True)
    monkeypatch.setattr(
        pipeline.github,
        "get_pr_status",
        lambda *_args: PullRequestStatus(
            number=9,
            url=pr_url,
            state="OPEN",
            mergeable="MERGEABLE",
            head_sha="abc123",
            checks=(),
        ),
    )
    monkeypatch.setattr(
        pipeline.gitops,
        "workspace_state",
        lambda _path: ("fix/pipeline/issue-9-add-reliable-retry-path", "abc123"),
    )
    monkeypatch.setattr(
        pipeline.gitops,
        "refresh_mirror",
        lambda *_args: tmp_path / "mirror.git",
    )

    def clone_source(_mirror: Path, _base: str, destination: Path) -> Path:
        destination.mkdir(parents=True)
        return destination

    monkeypatch.setattr(pipeline.gitops, "clone_source", clone_source)
    monkeypatch.setattr(pipeline.gitops, "cleanup_source", lambda _source: None)
    monkeypatch.setattr(pipeline.gitops, "cleanup_workspace", lambda path: cleaned.append(path))
    monkeypatch.setattr(pipeline, "_wait_for_checks", lambda *_args: "CI: success")
    monkeypatch.setattr(publish, "_publish_branch", lambda *_args, **_kwargs: None)
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

    def run_worker(
        _config: Config,
        _issue: Issue,
        run_dir: Path,
        **kwargs: object,
    ) -> WorkerResult:
        revision = kwargs.get("revision_pr")
        revisions.append(revision if isinstance(revision, str) else None)
        return _worker(run_dir, "fix/pipeline/issue-9-add-reliable-retry-path")

    monkeypatch.setattr(pipeline, "run_worker", run_worker)

    outcome = pipeline.run_issue(config, _issue(), "token", attempt_timeout_seconds=10)

    assert outcome.status == "pr_open"
    assert outcome.pr_url == pr_url
    assert revisions == [pr_url]
    assert labels[0] == (
        ("agent:running",),
        ("agent:queued", "agent:needs-human", "agent:pr-open"),
    )
    assert cleaned == []
    assert any("orch обновил PR" in body for body in comments)


def test_queued_existing_pr_without_workspace_is_parked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    comments: list[str] = []
    labels: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    monkeypatch.setattr(
        pipeline.github,
        "find_open_pr",
        lambda *_args: "https://example.test/pr/9",
    )
    monkeypatch.setattr(
        pipeline.github,
        "get_pr_status",
        lambda *_args: PullRequestStatus(
            number=9,
            url="https://example.test/pr/9",
            state="OPEN",
            mergeable="MERGEABLE",
            head_sha="abc123",
            checks=(),
        ),
    )
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

    outcome = pipeline.run_issue(config, _issue(), "token")

    assert outcome.status == "needs_human"
    assert outcome.pr_url == "https://example.test/pr/9"
    assert "workspace is missing" in (outcome.error or "")
    assert any("workspace is missing" in body for body in comments)
    assert any("https://example.test/pr/9" in body for body in comments)
    assert labels[-1] == (
        ("agent:needs-human",),
        ("agent:running", "agent:queued"),
    )


def test_existing_pr_lookup_failure_parks_instead_of_starting_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    comments: list[str] = []

    def fail(*_args: object) -> str | None:
        raise GitHubError("offline")

    monkeypatch.setattr(pipeline.github, "find_open_pr", fail)
    monkeypatch.setattr(
        pipeline.github,
        "comment_issue",
        lambda _repo, _number, _token, body: comments.append(body),
    )
    monkeypatch.setattr(pipeline.github, "edit_labels", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "run_worker",
        lambda *_args, **_kwargs: pytest.fail("worker must not start"),
    )

    outcome = pipeline.run_issue(config, _issue(), "token")

    assert outcome.status == "needs_human"
    assert "Could not verify whether an open PR exists" in (outcome.error or "")
    assert any("offline" in body for body in comments)


def test_revision_refuses_a_workspace_behind_the_pr_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    workspace = config.data_dir / "workspaces" / "slaid098__orch" / "issue-9"
    (workspace / ".git").mkdir(parents=True)

    monkeypatch.setattr(
        pipeline.github,
        "find_open_pr",
        lambda *_args: "https://example.test/pr/9",
    )
    monkeypatch.setattr(
        pipeline.github,
        "get_pr_status",
        lambda *_args: PullRequestStatus(
            number=9,
            url="https://example.test/pr/9",
            state="OPEN",
            mergeable="MERGEABLE",
            head_sha="newsha",
            checks=(),
        ),
    )
    monkeypatch.setattr(
        pipeline.gitops,
        "workspace_state",
        lambda _path: ("fix/pipeline/issue-9-add-reliable-retry-path", "oldsha"),
    )
    monkeypatch.setattr(pipeline.github, "comment_issue", lambda *_args: None)
    monkeypatch.setattr(pipeline.github, "edit_labels", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "run_worker",
        lambda *_args, **_kwargs: pytest.fail("worker must not start"),
    )

    outcome = pipeline.run_issue(config, _issue(), "token")

    assert outcome.status == "needs_human"
    assert "does not match the open PR head" in (outcome.error or "")


def test_source_preparation_failure_is_parked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    comments: list[str] = []

    monkeypatch.setattr(pipeline.github, "find_open_pr", lambda *_args: None)
    monkeypatch.setattr(
        pipeline.gitops,
        "refresh_mirror",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("mirror failed")),
    )
    monkeypatch.setattr(
        pipeline.github,
        "comment_issue",
        lambda _repo, _number, _token, body: comments.append(body),
    )
    monkeypatch.setattr(pipeline.github, "edit_labels", lambda *_args, **_kwargs: None)

    outcome = pipeline.run_issue(config, _issue(), "token")

    assert outcome.status == "needs_human"
    assert "mirror failed" in (outcome.error or "")
    assert any("не смог довести issue" in body for body in comments)


def test_find_open_pr_failure_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object) -> str | None:
        raise GitHubError("offline")

    monkeypatch.setattr(pipeline.github, "find_open_pr", fail)

    assert pipeline._find_open_pr("slaid098/orch", "branch", "token") is None
    with pytest.raises(GitHubError, match="offline"):
        pipeline._find_open_pr("slaid098/orch", "branch", "token", strict=True)


def test_worker_branch_must_match_the_issue_title(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, max_attempts=1)
    store.init_db(config.data_dir / "orch.db")
    deleted: list[str] = []

    _prepare_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(
        pipeline,
        "run_worker",
        lambda _config, _issue, run_dir, **_kwargs: _worker(
            run_dir,
            "fix/pipeline/issue-9-other-slug",
        ),
    )
    monkeypatch.setattr(
        pipeline.github,
        "delete_branch",
        lambda _repo, branch, _token: deleted.append(branch),
    )

    outcome = pipeline.run_issue(config, _issue(), "token", attempt_timeout_seconds=10)

    assert outcome.status == "needs_human"
    assert "expected" in (outcome.error or "")
    assert deleted == ["fix/pipeline/issue-9-add-reliable-retry-path"]


def test_worker_branch_must_be_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, max_attempts=1)
    store.init_db(config.data_dir / "orch.db")

    _prepare_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(
        pipeline,
        "run_worker",
        lambda _config, _issue, run_dir, **_kwargs: _worker(run_dir, "not-a-branch"),
    )

    outcome = pipeline.run_issue(config, _issue(), "token", attempt_timeout_seconds=10)

    assert outcome.status == "needs_human"
    assert "Branch must match" in (outcome.error or "")


def _prepare_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(pipeline.github, "find_open_pr", lambda *_args: None)
    monkeypatch.setattr(pipeline.github, "delete_branch", lambda *_args: None)
    monkeypatch.setattr(pipeline.github, "comment_issue", lambda *_args: None)
    monkeypatch.setattr(pipeline.github, "edit_labels", lambda *_args, **_kwargs: None)
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
    monkeypatch.setattr(
        pipeline.gitops,
        "clone_source",
        lambda _mirror, _base, destination: destination,
    )
    monkeypatch.setattr(pipeline.gitops, "cleanup_source", lambda _source: None)


def test_notification_failures_are_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise GitHubError("offline")

    monkeypatch.setattr(pipeline.github, "comment_issue", fail)
    monkeypatch.setattr(pipeline.github, "edit_labels", fail)

    pipeline._best_effort_comment("repo", 1, "token", "body")
    pipeline._best_effort_labels(
        "repo",
        1,
        "token",
        add=("a",),
        remove=("b",),
    )
    pipeline._comment_attempt_failure(config, _issue(), "token", 1, RuntimeError("bad"))
    pipeline._park_invalid_title(
        config,
        _issue(),
        "token",
        run_dir,
        "run",
        RuntimeError("bad title"),
    )
    pipeline._park(
        config,
        _issue(),
        "token",
        run_dir,
        "run",
        "branch",
        RuntimeError("bad"),
        attempts=1,
    )


def test_delete_stale_branch_keeps_open_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline.github,
        "find_open_pr",
        lambda *_args: "https://example.test/pr/1",
    )
    monkeypatch.setattr(
        pipeline.github,
        "delete_branch",
        lambda *_args: pytest.fail("branch must not be deleted"),
    )

    pipeline._delete_stale_branch(_config(Path(".")), "branch", "token")


def test_publish_branch_imports_and_pushes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []
    worker = _worker(tmp_path, "fix/pipeline/issue-9-add-reliable-retry-path")

    monkeypatch.setattr(
        pipeline.gitops,
        "import_bundle",
        lambda bundle, branch, run_dir: tmp_path / "bare.git",
    )
    monkeypatch.setattr(
        pipeline.gitops,
        "authenticated_remote",
        lambda owner, name, token: "remote",
    )
    monkeypatch.setattr(
        pipeline.gitops,
        "push_branch",
        lambda bare, branch, remote, token: calls.append(
            (str(bare), branch, remote)
        ),
    )
    monkeypatch.setattr(
        pipeline.gitops,
        "branch_sha",
        lambda _bare, _branch: "abc123",
    )

    assert pipeline._publish_branch(_config(tmp_path), worker, "token") == "abc123"

    assert calls == [
        (
            str(tmp_path / "bare.git"),
            "fix/pipeline/issue-9-add-reliable-retry-path",
            "remote",
        )
    ]


def test_publish_branch_forwards_the_revision_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path, "fix/pipeline/issue-9-add-reliable-retry-path")
    leases: list[str | None] = []

    monkeypatch.setattr(
        pipeline.gitops,
        "import_bundle",
        lambda bundle, branch, run_dir: tmp_path / "bare.git",
    )
    monkeypatch.setattr(
        pipeline.gitops,
        "authenticated_remote",
        lambda owner, name, token: "remote",
    )
    monkeypatch.setattr(
        pipeline.gitops,
        "push_branch",
        lambda bare, branch, remote, token, *, expected=None: leases.append(expected),
    )
    monkeypatch.setattr(
        pipeline.gitops,
        "branch_sha",
        lambda _bare, _branch: "newsha",
    )

    assert (
        publish._publish_branch(
            _config(tmp_path),
            worker,
            "token",
            expected="oldsha",
        )
        == "newsha"
    )
    assert leases == ["oldsha"]


def test_publish_success_refuses_a_closed_revision_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path, "fix/pipeline/issue-9-add-reliable-retry-path")

    monkeypatch.setattr(
        publish.github,
        "get_pr_status",
        lambda *_args: PullRequestStatus(
            number=9,
            url="https://example.test/pr/9",
            state="CLOSED",
            mergeable="UNKNOWN",
            head_sha="abc123",
            checks=(),
        ),
    )
    monkeypatch.setattr(
        publish,
        "_publish_branch",
        lambda *_args, **_kwargs: pytest.fail("must not push"),
    )

    with pytest.raises(GitHubError, match="no longer open"):
        publish._publish_success(
            _config(tmp_path),
            _issue(),
            "token",
            worker,
            run_id="run",
            attempt=1,
            title="title",
            revision_pr="https://example.test/pr/9",
            revision_head="abc123",
        )


def test_publish_success_refuses_a_moved_revision_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path, "fix/pipeline/issue-9-add-reliable-retry-path")

    monkeypatch.setattr(
        publish.github,
        "get_pr_status",
        lambda *_args: PullRequestStatus(
            number=9,
            url="https://example.test/pr/9",
            state="OPEN",
            mergeable="MERGEABLE",
            head_sha="newsha",
            checks=(),
        ),
    )
    monkeypatch.setattr(
        publish,
        "_publish_branch",
        lambda *_args, **_kwargs: pytest.fail("must not push"),
    )

    with pytest.raises(GitHubError, match="head moved"):
        publish._publish_success(
            _config(tmp_path),
            _issue(),
            "token",
            worker,
            run_id="run",
            attempt=1,
            title="title",
            revision_pr="https://example.test/pr/9",
            revision_head="abc123",
        )


def test_publish_success_attaches_the_pushed_head_to_lookup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path, "fix/pipeline/issue-9-add-reliable-retry-path")

    monkeypatch.setattr(
        publish.github,
        "get_pr_status",
        lambda *_args: PullRequestStatus(
            number=9,
            url="https://example.test/pr/9",
            state="OPEN",
            mergeable="MERGEABLE",
            head_sha="abc123",
            checks=(),
        ),
    )
    monkeypatch.setattr(
        publish,
        "_publish_branch",
        lambda *_args, **_kwargs: "pushed1",
    )

    def fail(*_args: object) -> str | None:
        raise GitHubError("offline")

    monkeypatch.setattr(publish.github, "find_open_pr", fail)

    with pytest.raises(PublicationError) as info:
        publish._publish_success(
            _config(tmp_path),
            _issue(),
            "token",
            worker,
            run_id="run",
            attempt=1,
            title="title",
            revision_pr="https://example.test/pr/9",
            revision_head="abc123",
        )

    assert info.value.head_sha == "pushed1"


def test_worker_failure_cycle_keeps_the_pushed_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "_retry_outcome",
        lambda *_args, **_kwargs: None,
    )

    cycle = pipeline._worker_failure_cycle(
        _config(tmp_path),
        _issue(),
        "token",
        PublicationError("lookup failed", "pushed1"),
        run_id="run",
        run_dir=tmp_path,
        branch="fix/pipeline/issue-9-add-reliable-retry-path",
        attempt=1,
        review_rounds=0,
        last_pr_url="https://example.test/pr/9",
    )

    assert cycle.pr_head == "pushed1"


def test_revision_retry_tracks_the_head_pushed_by_the_previous_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    pr_url = "https://example.test/pr/9"
    branch = "fix/pipeline/issue-9-add-reliable-retry-path"
    workspace = config.data_dir / "workspaces" / "slaid098__orch" / "issue-9"
    (workspace / ".git").mkdir(parents=True)
    remote_head = "abc123"
    workers: list[str | None] = []
    checks = iter([GitHubError("checks failed"), "CI: success"])

    monkeypatch.setattr(pipeline.github, "find_open_pr", lambda *_args: pr_url)
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: True)
    monkeypatch.setattr(
        pipeline.github,
        "get_pr_status",
        lambda *_args: PullRequestStatus(
            number=9,
            url=pr_url,
            state="OPEN",
            mergeable="MERGEABLE",
            head_sha=remote_head,
            checks=(),
        ),
    )
    monkeypatch.setattr(
        pipeline.gitops,
        "workspace_state",
        lambda _path: (branch, "abc123"),
    )
    monkeypatch.setattr(
        pipeline.gitops,
        "refresh_mirror",
        lambda *_args: tmp_path / "mirror.git",
    )

    def clone_source(_mirror: Path, _base: str, destination: Path) -> Path:
        destination.mkdir(parents=True)
        return destination

    monkeypatch.setattr(pipeline.gitops, "clone_source", clone_source)
    monkeypatch.setattr(pipeline.gitops, "cleanup_source", lambda _source: None)

    def wait_for_checks(*_args: object, **_kwargs: object) -> str:
        result = next(checks)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(pipeline, "_wait_for_checks", wait_for_checks)

    def publish_branch(
        _config: Config,
        _worker: WorkerResult,
        _token: str,
        *,
        expected: str | None = None,
    ) -> str:
        nonlocal remote_head
        assert expected == remote_head
        remote_head = f"pushed{len(workers)}"
        return remote_head

    monkeypatch.setattr(publish, "_publish_branch", publish_branch)
    monkeypatch.setattr(pipeline.github, "comment_issue", lambda *_args: None)
    monkeypatch.setattr(
        pipeline.github,
        "edit_labels",
        lambda *_args, **_kwargs: None,
    )

    def run_worker(
        _config: Config,
        _issue: Issue,
        run_dir: Path,
        **kwargs: object,
    ) -> WorkerResult:
        revision = kwargs.get("revision_pr")
        workers.append(revision if isinstance(revision, str) else None)
        return _worker(run_dir, branch)

    monkeypatch.setattr(pipeline, "run_worker", run_worker)

    outcome = pipeline.run_issue(config, _issue(), "token", attempt_timeout_seconds=10)

    assert outcome.status == "pr_open"
    assert workers == [pr_url, pr_url]
    assert remote_head == "pushed2"

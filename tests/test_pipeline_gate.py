from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from orch.exceptions.github import GitHubError
from orch.exceptions.pipeline import ChecksFailed
from orch.exceptions.reviewer import ReviewerError
from orch.exceptions.worker import RunCancelled
from orch.schemas.config.types import (
    ChecksConfig,
    Config,
    LabelsConfig,
    RepoConfig,
    ReviewerConfig,
    WorkerConfig,
)
from orch.schemas.github.types import CheckResult, Issue, PullRequestStatus
from orch.schemas.reviewer.types import ReviewFinding, ReviewResult
from orch.schemas.standards.types import Handoff
from orch.schemas.worker.types import WorkerResult
from orch.services import store
from orch.services.pipeline import gate, publish
from orch.services.pipeline import service as pipeline


def _config(
    tmp_path: Path,
    *,
    max_attempts: int = 3,
    max_rounds: int = 3,
    reviewer_enabled: bool = True,
    auto_merge: bool = True,
) -> Config:
    return Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(
            timeout_minutes=1,
            max_attempts=max_attempts,
            test_commands=("pytest",),
        ),
        data_dir=tmp_path / "data",
        checks=ChecksConfig(
            timeout_minutes=5,
            poll_seconds=5,
            discovery_seconds=10,
        ),
        reviewer=ReviewerConfig(
            enabled=reviewer_enabled,
            timeout_minutes=1,
            max_rounds=max_rounds,
            auto_merge=auto_merge,
        ),
    )


def _issue(number: int = 9) -> Issue:
    return Issue(
        number=number,
        title="fix(pipeline): add reliable retry path",
        body="Do it.",
        url=f"https://example.test/issues/{number}",
        author="slaid098",
        labels=("agent:queued",),
    )


def _handoff() -> Handoff:
    return Handoff(
        what="Добавлен повторный запуск.",
        why="Работа не должна теряться.",
        watch_out="—",
        pending="—",
    )


def _worker_result(run_dir: Path, branch: str = "orch/issue-9") -> WorkerResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "diff.patch").write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
    return WorkerResult(
        run_dir=run_dir,
        branch=branch,
        handoff=_handoff(),
        tests_output="passed",
        bundle_path=run_dir / "result.bundle",
    )


def _finding() -> ReviewFinding:
    return ReviewFinding(
        file="src/a.py",
        line=3,
        title="Off-by-one in the return value",
        situation="The wrong user is returned.",
        fixes=("Return users[index]",),
    )


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    checks: list[str | Exception] | None = None,
    reviews: list[ReviewResult | Exception] | None = None,
    worker_errors: set[int] | None = None,
) -> dict[str, list[object]]:
    checks = list(checks or ["pytest: success"])
    reviews = list(reviews or [ReviewResult(findings=())])
    worker_errors = worker_errors or set()
    events: dict[str, list[object]] = {
        "workers": [],
        "previous_errors": [],
        "checks": [],
        "reviews": [],
        "merges": [],
        "comments": [],
        "labels": [],
        "cleaned_workspaces": [],
        "cleaned_sources": [],
        "prs": [],
    }

    monkeypatch.setattr(
        pipeline.gitops,
        "refresh_mirror",
        lambda *_args: tmp_path / "mirror.git",
    )

    def clone_source(_mirror: Path, _base: str, destination: Path) -> Path:
        destination.mkdir(parents=True)
        return destination

    monkeypatch.setattr(pipeline.gitops, "clone_source", clone_source)
    monkeypatch.setattr(
        pipeline.gitops,
        "cleanup_source",
        lambda source: events["cleaned_sources"].append(source),
    )
    monkeypatch.setattr(
        pipeline.gitops,
        "cleanup_workspace",
        lambda workspace: events["cleaned_workspaces"].append(workspace),
    )
    monkeypatch.setattr(publish, "_publish_branch", lambda *_args: None)
    monkeypatch.setattr(
        pipeline.github,
        "find_open_pr_for_issue",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(pipeline.github, "find_open_pr", lambda *_args: None)
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: True)
    monkeypatch.setattr(pipeline.github, "delete_branch", lambda *_args: None)
    monkeypatch.setattr(
        pipeline.github,
        "comment_issue",
        lambda _repo, _number, _token, body: events["comments"].append(body),
    )
    monkeypatch.setattr(
        pipeline.github,
        "edit_labels",
        lambda _repo, _number, _token, *, add, remove: events["labels"].append(
            (add, remove)
        ),
    )
    monkeypatch.setattr(
        pipeline.github,
        "create_pr",
        lambda _repo, _token, **kwargs: (
            events["prs"].append(kwargs)
            or "https://example.test/pull/10"
        ),
    )

    def fake_checks(*_args: object) -> str:
        events["checks"].append("check")
        if not checks:
            pytest.fail("unexpected checks poll")
        item = checks.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def fake_review(*_args: object, **_kwargs: object) -> ReviewResult:
        events["reviews"].append("review")
        if not reviews:
            pytest.fail("unexpected reviewer launch")
        item = reviews.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def fake_merge(*_args: object) -> str:
        events["merges"].append("merge")
        return "abc123"

    def fake_worker(
        _config: Config,
        _issue: Issue,
        run_dir: Path,
        **kwargs: object,
    ) -> WorkerResult:
        attempt = int(kwargs["attempt"])
        events["workers"].append(attempt)
        events["previous_errors"].append(kwargs["previous_error"])
        if attempt in worker_errors:
            raise RuntimeError(f"worker failed on attempt {attempt}")
        return _worker_result(run_dir, branch=str(kwargs["branch"]))

    monkeypatch.setattr(pipeline, "_wait_for_checks", fake_checks)
    monkeypatch.setattr(gate, "run_review", fake_review)
    monkeypatch.setattr(pipeline.github, "merge_pr", fake_merge)
    monkeypatch.setattr(pipeline, "run_worker", fake_worker)
    return events


def _run_status(db_path: Path) -> str | None:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("SELECT status FROM runs LIMIT 1").fetchone()
    return row[0] if row else None


def test_approved_review_merges_and_cleans_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    events = _prepare(tmp_path, monkeypatch)
    issue = _issue()

    outcome = pipeline.run_issue(
        config,
        issue,
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "merged"
    assert outcome.pr_url == "https://example.test/pull/10"
    assert outcome.review_rounds == 1
    assert outcome.merge_commit == "abc123"
    assert events["workers"] == [1]
    assert events["checks"] == ["check"]
    assert events["reviews"] == ["review"]
    assert events["merges"] == ["merge"]
    assert events["cleaned_workspaces"] == [
        config.data_dir / "workspaces" / "slaid098__orch" / "issue-9"
    ]
    assert _run_status(config.data_dir / "orch.db") == "merged"
    payload = json.loads((outcome.run_dir / "result.json").read_text(encoding="utf-8"))
    assert payload["status"] == "merged"
    assert payload["checks_passed"] is True
    assert payload["review_rounds"] == 1
    assert payload["merge_commit"] == "abc123"


def test_checks_failure_returns_to_worker_before_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    events = _prepare(
        tmp_path,
        monkeypatch,
        checks=[ChecksFailed("pytest failed"), "pytest: success"],
    )

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "merged"
    assert events["workers"] == [1, 2]
    assert events["reviews"] == ["review"]
    assert events["checks"] == ["check", "check"]
    assert events["merges"] == ["merge"]
    assert events["previous_errors"][0] is None
    assert "pytest failed" in str(events["previous_errors"][1])
    assert any("Попытка 1/3" in str(body) for body in events["comments"])


def test_review_findings_trigger_fix_and_second_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    events = _prepare(
        tmp_path,
        monkeypatch,
        checks=["pytest: success", "pytest: success"],
        reviews=[
            ReviewResult(findings=(_finding(),)),
            ReviewResult(findings=()),
        ],
    )

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "merged"
    assert outcome.review_rounds == 2
    assert events["workers"] == [1, 2]
    assert events["reviews"] == ["review", "review"]
    assert events["merges"] == ["merge"]
    assert "Off-by-one" in str(events["previous_errors"][1])
    assert any("Ревьюер заблокировал merge" in str(body) for body in events["comments"])
    assert any("Все замечания обязательны" in str(body) for body in events["comments"])


def test_review_exhaustion_parks_without_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    events = _prepare(
        tmp_path,
        monkeypatch,
        checks=["pytest: success", "pytest: success", "pytest: success"],
        reviews=[
            ReviewResult(findings=(_finding(),)),
            ReviewResult(findings=(_finding(),)),
            ReviewResult(findings=(_finding(),)),
        ],
    )

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "needs_human"
    assert outcome.review_rounds == 3
    assert events["workers"] == [1, 2, 3]
    assert events["reviews"] == ["review", "review", "review"]
    assert events["merges"] == []
    assert events["cleaned_workspaces"] == []
    assert _run_status(config.data_dir / "orch.db") == "needs_human"
    assert any("agent:needs-human" in str(label) for label in events["labels"])


def test_review_round_limit_parks_early(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, max_rounds=1)
    store.init_db(config.data_dir / "orch.db")
    events = _prepare(
        tmp_path,
        monkeypatch,
        reviews=[ReviewResult(findings=(_finding(),))],
    )

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "needs_human"
    assert outcome.review_rounds == 1
    assert events["workers"] == [1]
    assert events["merges"] == []
    assert _run_status(config.data_dir / "orch.db") == "needs_human"


def test_reviewer_technical_failure_does_not_retry_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    events = _prepare(
        tmp_path,
        monkeypatch,
        reviews=[ReviewerError("invalid JSON twice")],
    )

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "needs_human"
    assert outcome.review_rounds == 1
    assert events["workers"] == [1]
    assert events["reviews"] == ["review"]
    assert events["merges"] == []
    assert events["cleaned_workspaces"] == []
    assert _run_status(config.data_dir / "orch.db") == "needs_human"


def test_unexpected_reviewer_crash_is_treated_as_technical_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    events = _prepare(
        tmp_path,
        monkeypatch,
        reviews=[RuntimeError("reviewer process crashed")],
    )

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "needs_human"
    assert outcome.error is not None
    assert "reviewer crashed" in outcome.error
    assert events["workers"] == [1]
    assert events["merges"] == []
    assert _run_status(config.data_dir / "orch.db") == "needs_human"


def test_all_checks_failed_never_reaches_reviewer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    events = _prepare(
        tmp_path,
        monkeypatch,
        checks=[
            ChecksFailed("failure 1"),
            ChecksFailed("failure 2"),
            ChecksFailed("failure 3"),
        ],
    )

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "needs_human"
    assert outcome.pr_url == "https://example.test/pull/10"
    assert events["workers"] == [1, 2, 3]
    assert events["reviews"] == []
    assert events["merges"] == []
    assert _run_status(config.data_dir / "orch.db") == "needs_human"


def test_approved_review_leaves_pr_open_for_manual_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, auto_merge=False)
    store.init_db(config.data_dir / "orch.db")
    events = _prepare(tmp_path, monkeypatch)

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "pr_open"
    assert outcome.pr_url == "https://example.test/pull/10"
    assert outcome.review_rounds == 1
    assert events["workers"] == [1]
    assert events["reviews"] == ["review"]
    assert events["merges"] == []
    assert events["cleaned_workspaces"] == []
    assert any("готов к ручному merge" in str(body) for body in events["comments"])
    assert _run_status(config.data_dir / "orch.db") == "pr_open"
    payload = json.loads((outcome.run_dir / "result.json").read_text(encoding="utf-8"))
    assert payload["status"] == "pr_open"
    assert payload["review_rounds"] == 1


def test_reviewer_disabled_opens_pr_without_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, reviewer_enabled=False)
    store.init_db(config.data_dir / "orch.db")
    events = _prepare(tmp_path, monkeypatch)

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "pr_open"
    assert events["reviews"] == []
    assert events["merges"] == []
    assert events["cleaned_workspaces"] == []
    assert _run_status(config.data_dir / "orch.db") == "pr_open"


def test_merge_failure_parks_with_pr_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store.init_db(config.data_dir / "orch.db")
    events = _prepare(tmp_path, monkeypatch)

    def fail_merge(*_args: object) -> str:
        raise GitHubError("branch protection")

    monkeypatch.setattr(pipeline.github, "merge_pr", fail_merge)

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "needs_human"
    assert outcome.pr_url == "https://example.test/pull/10"
    assert outcome.review_rounds == 1
    assert events["cleaned_workspaces"] == []
    assert _run_status(config.data_dir / "orch.db") == "needs_human"


def test_auto_merge_refuses_non_main_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config = replace(
        config,
        repo=RepoConfig(
            owner="slaid098",
            name="orch",
            path=tmp_path,
            base_branch="dev",
        ),
    )
    store.init_db(config.data_dir / "orch.db")
    events = _prepare(tmp_path, monkeypatch)

    outcome = pipeline.run_issue(
        config,
        _issue(),
        "token",
        attempt_timeout_seconds=10,
    )

    assert outcome.status == "needs_human"
    assert outcome.pr_url == "https://example.test/pull/10"
    assert events["reviews"] == ["review"]
    assert events["merges"] == []
    assert _run_status(config.data_dir / "orch.db") == "needs_human"


def _status(
    *checks: CheckResult,
    mergeable: str = "MERGEABLE",
    head_sha: str = "abc",
) -> PullRequestStatus:
    return PullRequestStatus(
        number=10,
        url="https://example.test/pull/10",
        state="OPEN",
        mergeable=mergeable,
        head_sha=head_sha,
        checks=checks,
    )


def test_wait_for_checks_returns_green_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: True)
    monkeypatch.setattr(
        pipeline.github,
        "get_pr_status",
        lambda *_args: _status(CheckResult("pytest", "success")),
    )

    summary = pipeline._wait_for_checks(
        config,
        _issue(),
        "token",
        "https://example.test/pull/10",
        None,
    )

    assert summary == "pytest: success"


def test_wait_for_checks_waits_for_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    statuses = iter(
        [
            _status(CheckResult("pytest", "pending")),
            _status(CheckResult("pytest", "success")),
        ]
    )
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: True)
    monkeypatch.setattr(
        pipeline.github,
        "get_pr_status",
        lambda *_args: next(statuses),
    )
    monkeypatch.setattr(pipeline.time, "sleep", lambda _seconds: None)

    summary = pipeline._wait_for_checks(
        config,
        _issue(),
        "token",
        "https://example.test/pull/10",
        None,
    )

    assert summary == "pytest: success"


def test_wait_for_checks_ignores_another_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    statuses = iter(
        [
            _status(CheckResult("pytest", "failure"), head_sha="old"),
            _status(CheckResult("pytest", "success"), head_sha="new"),
        ]
    )
    calls: list[int] = []

    def status(*_args: object) -> PullRequestStatus:
        calls.append(1)
        return next(statuses)

    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: True)
    monkeypatch.setattr(pipeline.github, "get_pr_status", status)
    monkeypatch.setattr(pipeline.time, "sleep", lambda _seconds: None)

    summary = pipeline._wait_for_checks(
        config,
        _issue(),
        "token",
        "https://example.test/pull/10",
        None,
        expected_head="new",
    )

    assert summary == "pytest: success"
    assert len(calls) == 2


def test_wait_for_checks_raises_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: True)
    monkeypatch.setattr(
        pipeline.github,
        "get_pr_status",
        lambda *_args: _status(CheckResult("pytest", "failure")),
    )

    with pytest.raises(ChecksFailed, match="pytest"):
        pipeline._wait_for_checks(
            config,
            _issue(),
            "token",
            "https://example.test/pull/10",
            None,
        )


def test_wait_for_checks_treats_conflicts_as_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: True)
    monkeypatch.setattr(
        pipeline.github,
        "get_pr_status",
        lambda *_args: _status(
            CheckResult("pytest", "success"),
            mergeable="CONFLICTING",
        ),
    )

    with pytest.raises(ChecksFailed, match="conflicts"):
        pipeline._wait_for_checks(
            config,
            _issue(),
            "token",
            "https://example.test/pull/10",
            None,
        )


def test_wait_for_checks_accepts_no_checks_after_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
        checks=ChecksConfig(
            timeout_minutes=5,
            poll_seconds=5,
            discovery_seconds=0,
        ),
    )
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: True)
    monkeypatch.setattr(
        pipeline.github,
        "get_pr_status",
        lambda *_args: _status(),
    )

    summary = pipeline._wait_for_checks(
        config,
        _issue(),
        "token",
        "https://example.test/pull/10",
        None,
    )

    assert "No GitHub checks" in summary


def test_wait_for_checks_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
        checks=ChecksConfig(
            timeout_minutes=0,
            poll_seconds=5,
            discovery_seconds=10,
        ),
    )
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: True)
    monkeypatch.setattr(
        pipeline.github,
        "get_pr_status",
        lambda *_args: _status(CheckResult("pytest", "pending")),
    )

    with pytest.raises(ChecksFailed, match="did not finish"):
        pipeline._wait_for_checks(
            config,
            _issue(),
            "token",
            "https://example.test/pull/10",
            None,
        )


def test_wait_for_checks_times_out_when_github_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
        checks=ChecksConfig(
            timeout_minutes=0,
            poll_seconds=5,
            discovery_seconds=10,
        ),
    )
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: True)

    def fail(*_args: object) -> PullRequestStatus:
        raise GitHubError("api down")

    monkeypatch.setattr(pipeline.github, "get_pr_status", fail)

    with pytest.raises(ChecksFailed, match="Could not read"):
        pipeline._wait_for_checks(
            config,
            _issue(),
            "token",
            "https://example.test/pull/10",
            None,
        )


def test_wait_for_checks_cancels_on_closed_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(pipeline.github, "issue_is_open", lambda *_args: False)

    with pytest.raises(RunCancelled, match="issue closed"):
        pipeline._wait_for_checks(
            config,
            _issue(),
            "token",
            "https://example.test/pull/10",
            None,
        )

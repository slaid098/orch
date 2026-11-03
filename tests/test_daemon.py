from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from orch.exceptions.github import GitHubError
from orch.schemas.config.types import Config, LabelsConfig, RepoConfig, WorkerConfig
from orch.schemas.github.types import Issue
from orch.schemas.pipeline.types import RunOutcome
from orch.services.daemon import service as daemon


def _config(tmp_path: Path) -> Config:
    return Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
        poll_interval_seconds=1,
        max_workers=1,
    )


def _issue(number: int = 7) -> Issue:
    return Issue(
        number=number,
        title="feat(cli): add version flag",
        body="Body",
        url=f"https://example.test/{number}",
        author="slaid098",
        labels=("agent:queued",),
    )


def _outcome(status: str = "pr_open") -> RunOutcome:
    finished = status in {"pr_open", "merged"}
    return RunOutcome(
        run_id="run",
        status=status,
        run_dir=Path("run"),
        branch="feat/cli/issue-7-add-version-flag",
        pr_url="https://example.test/pr/1" if finished else None,
        error=None if finished else "failed",
    )


class FakeLogger:
    def __init__(self) -> None:
        self.removed = 0
        self.added: list[tuple[object, dict[str, object]]] = []

    def remove(self) -> None:
        self.removed += 1

    def add(self, sink: object, **kwargs: object) -> None:
        self.added.append((sink, kwargs))


def test_setup_logging_configures_console_and_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    fake = FakeLogger()
    monkeypatch.setattr(daemon, "logger", fake)

    daemon.setup_logging(config)

    assert fake.removed == 1
    assert len(fake.added) == 2
    assert fake.added[1][0] == config.data_dir / "orch.log"
    assert fake.added[1][1]["retention"] == "7 days"


def test_initialize_resolves_token_and_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(daemon.github, "resolve_token", lambda owner, token: "token")
    monkeypatch.setattr(
        daemon,
        "ensure_user",
        lambda token, owner: calls.append(("user", token, owner)),
    )
    monkeypatch.setattr(
        daemon,
        "ensure_labels",
        lambda repo, token, labels: calls.append(("labels", repo, labels)),
    )
    monkeypatch.setattr(
        daemon.store,
        "init_db",
        lambda path: calls.append(("db", path)),
    )

    token = daemon.initialize(config)

    assert token == "token"
    assert calls[0] == ("user", "token", "slaid098")
    labels = calls[1][2]
    assert isinstance(labels, list)
    assert {label[0] for label in labels} == {
        "agent:queued",
        "agent:running",
        "agent:needs-human",
        "agent:pr-open",
        "agent:hold",
    }
    assert calls[2] == ("db", config.data_dir / "orch.db")


def test_run_once_returns_zero_for_open_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(daemon.github, "get_issue", lambda *_args: _issue())
    monkeypatch.setattr(daemon, "run_issue", lambda *_args: _outcome())

    assert daemon.run_once(config, "token", 7) == 0


def test_run_once_returns_zero_for_merged_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(daemon.github, "get_issue", lambda *_args: _issue())
    monkeypatch.setattr(daemon, "run_issue", lambda *_args: _outcome("merged"))

    assert daemon.run_once(config, "token", 7) == 0


def test_run_once_returns_one_for_needs_human(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(daemon.github, "get_issue", lambda *_args: _issue())
    monkeypatch.setattr(daemon, "run_issue", lambda *_args: _outcome("needs_human"))

    assert daemon.run_once(config, "token", 7) == 1


def test_run_once_returns_one_when_issue_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)

    def fail(*_args: object) -> Issue:
        raise GitHubError("missing")

    monkeypatch.setattr(daemon.github, "get_issue", fail)

    assert daemon.run_once(config, "token", 7) == 1


class FakeFuture:
    def __init__(
        self,
        *,
        done: bool = False,
        result: RunOutcome | None = None,
        error: Exception | None = None,
    ) -> None:
        self._done = done
        self._result = result
        self._error = error

    def done(self) -> bool:
        return self._done

    def result(self) -> RunOutcome:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class FakeExecutor:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.submitted: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def __enter__(self) -> FakeExecutor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def submit(self, function: object, *args: object, **kwargs: object) -> FakeFuture:
        self.submitted.append((function, args, kwargs))
        return FakeFuture()


def test_run_daemon_claims_one_issue_and_stops_on_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    executor_holder: list[FakeExecutor] = []
    monkeypatch.setattr(daemon, "_reconcile_startup", lambda *_args: None)
    monkeypatch.setattr(daemon.github, "list_ready_issues", lambda *_args: [_issue()])
    monkeypatch.setattr(daemon.store, "has_active_run", lambda *_args: False)
    monkeypatch.setattr(daemon, "_claim_issue", lambda *_args: True)

    def make_executor(**kwargs: object) -> FakeExecutor:
        executor = FakeExecutor(**kwargs)
        executor_holder.append(executor)
        return executor

    monkeypatch.setattr(daemon, "ThreadPoolExecutor", make_executor)

    def interrupt(_seconds: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", interrupt)

    assert daemon.run_daemon(config, "token") == 0
    assert len(executor_holder[0].submitted) == 1
    assert executor_holder[0].submitted[0][2]["claim"] is False


def test_run_daemon_survives_poll_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(daemon, "_reconcile_startup", lambda *_args: None)

    def fail(*_args: object) -> list[Issue]:
        raise GitHubError("offline")

    monkeypatch.setattr(daemon.github, "list_ready_issues", fail)
    monkeypatch.setattr(daemon, "ThreadPoolExecutor", FakeExecutor)

    def interrupt(_seconds: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", interrupt)

    assert daemon.run_daemon(config, "token") == 0


def test_run_daemon_skips_active_and_unclaimed_issues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(daemon, "_reconcile_startup", lambda *_args: None)
    monkeypatch.setattr(
        daemon.github,
        "list_ready_issues",
        lambda *_args: [_issue(7), _issue(8)],
    )
    monkeypatch.setattr(
        daemon.store,
        "has_active_run",
        lambda _db, _repo, number: number == 7,
    )
    monkeypatch.setattr(daemon, "_claim_issue", lambda *_args: False)
    monkeypatch.setattr(daemon, "ThreadPoolExecutor", FakeExecutor)

    def interrupt(_seconds: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", interrupt)

    assert daemon.run_daemon(config, "token") == 0


def test_reap_finished_removes_completed_and_failed_futures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = FakeFuture(done=True, result=_outcome())
    failed = FakeFuture(done=True, error=RuntimeError("broken"))
    active = {7: completed, 8: failed, 9: FakeFuture(done=False)}
    pruned: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        daemon,
        "prune_runs",
        lambda data_dir, days: pruned.append((data_dir, days)),
    )

    daemon._reap_finished(active, _config(tmp_path))

    assert list(active) == [9]
    assert pruned == [(tmp_path / "data", 7)]


def test_claim_issue_returns_true_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    monkeypatch.setattr(
        daemon.github,
        "edit_labels",
        lambda _repo, _number, _token, *, add, remove: calls.append((add, remove)),
    )

    assert daemon._claim_issue(config, _issue(), "token")
    assert calls == [
        (
            ("agent:running",),
            ("agent:queued", "agent:needs-human"),
        )
    ]


def test_claim_issue_returns_false_on_github_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise GitHubError("offline")

    monkeypatch.setattr(daemon.github, "edit_labels", fail)

    assert not daemon._claim_issue(config, _issue(), "token")


def test_claim_failure_notification_is_rate_limited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    sent: list[int] = []
    daemon._claim_notified_at.clear()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise GitHubError("offline")

    monkeypatch.setattr(daemon.github, "edit_labels", fail)
    monkeypatch.setattr(
        daemon.notify,
        "notify_error",
        lambda *_args, **kwargs: sent.append(kwargs["issue_number"]),
    )

    assert not daemon._claim_issue(config, _issue(), "token")
    assert not daemon._claim_issue(config, _issue(), "token")
    assert sent == [7]


def test_reconcile_startup_requeues_running_issues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(daemon, "cleanup_managed", lambda: calls.append("cleanup"))
    monkeypatch.setattr(
        daemon.store,
        "interrupt_active_runs",
        lambda *_args: calls.append("interrupt") or 1,
    )
    monkeypatch.setattr(
        daemon.github,
        "list_ready_issues",
        lambda *_args: [_issue()],
    )
    monkeypatch.setattr(
        daemon.github,
        "comment_issue",
        lambda *args: calls.append(("comment", args)),
    )
    monkeypatch.setattr(
        daemon.github,
        "edit_labels",
        lambda *args, **kwargs: calls.append(("labels", args, kwargs)),
    )

    daemon._reconcile_startup(config, "token")

    assert calls[0] == "cleanup"
    assert calls[1] == "interrupt"
    assert calls[2][0] == "comment"
    assert calls[3][0] == "labels"


def test_reconcile_startup_handles_list_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(daemon, "cleanup_managed", lambda: None)
    monkeypatch.setattr(daemon.store, "interrupt_active_runs", lambda *_args: 0)

    def fail(*_args: object) -> list[Issue]:
        raise GitHubError("offline")

    monkeypatch.setattr(daemon.github, "list_ready_issues", fail)

    daemon._reconcile_startup(config, "token")


def test_reconcile_startup_continues_after_one_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(daemon, "cleanup_managed", lambda: None)
    monkeypatch.setattr(daemon.store, "interrupt_active_runs", lambda *_args: 0)
    monkeypatch.setattr(
        daemon.github,
        "list_ready_issues",
        lambda *_args: [_issue(7), _issue(8)],
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise GitHubError("offline")

    monkeypatch.setattr(daemon.github, "comment_issue", fail)

    daemon._reconcile_startup(config, "token")


def test_run_daemon_sends_accepted_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    accepted: list[tuple[int, int, int]] = []
    monkeypatch.setattr(daemon, "_reconcile_startup", lambda *_args: None)
    monkeypatch.setattr(daemon.github, "list_ready_issues", lambda *_args: [_issue(7), _issue(8)])
    monkeypatch.setattr(daemon.store, "has_active_run", lambda *_args: False)
    monkeypatch.setattr(daemon, "_claim_issue", lambda *_args: True)
    monkeypatch.setattr(daemon, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(
        daemon.notify,
        "notify_accepted",
        lambda _config, number, _title, *, queue, active: accepted.append((number, queue, active)),
    )

    def interrupt(_seconds: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", interrupt)

    assert daemon.run_daemon(config, "token") == 0
    assert accepted == [(7, 1, 1)]


def test_reap_finished_sends_pr_and_needs_human_notifications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    pr_outcome = RunOutcome(
        run_id="run-1",
        status="pr_open",
        run_dir=Path("run-1"),
        branch="feat/cli/issue-7-add-version-flag",
        pr_url="https://example.test/pr/7",
        issue_title="feat(cli): add version flag",
        duration_seconds=90,
    )
    human_outcome = RunOutcome(
        run_id="run-2",
        status="needs_human",
        run_dir=Path("run-2"),
        branch="fix/cli/issue-8-broken",
        error="tests failed",
        issue_title="fix(cli): broken",
        duration_seconds=120,
    )
    active = {
        7: FakeFuture(done=True, result=pr_outcome),
        8: FakeFuture(done=True, result=human_outcome),
    }
    finished: list[tuple[int, int, int, int]] = []
    human: list[tuple[int, str, int, int]] = []
    monkeypatch.setattr(daemon, "_queue_size", lambda *_args: 3)
    monkeypatch.setattr(daemon, "prune_runs", lambda *_args: None)
    monkeypatch.setattr(
        daemon.notify,
        "notify_finished",
        lambda _config, number, _title, *, pr_url, duration_seconds, queue, active: finished.append(
            (number, duration_seconds, queue, active)
        ),
    )
    monkeypatch.setattr(
        daemon.notify,
        "notify_needs_human",
        lambda _config, number, _title, *, reason, queue, active: human.append(
            (number, reason, queue, active)
        ),
    )

    daemon._reap_finished(active, config, "token")

    assert finished == [(7, 90, 3, 1)]
    assert human == [(8, "tests failed", 3, 0)]
    assert active == {}


def test_run_once_sends_lifecycle_notifications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    accepted: list[int] = []
    finished: list[str] = []
    monkeypatch.setattr(daemon.github, "get_issue", lambda *_args: _issue())
    monkeypatch.setattr(daemon, "run_issue", lambda *_args: _outcome())
    monkeypatch.setattr(
        daemon.notify,
        "notify_accepted",
        lambda _config, number, _title, *, queue, active: accepted.append(number),
    )
    monkeypatch.setattr(
        daemon.notify,
        "notify_finished",
        lambda _config, number, _title, *, pr_url, **kwargs: finished.append(pr_url),
    )

    assert daemon.run_once(config, "token", 7) == 0
    assert accepted == [7]
    assert finished == ["https://example.test/pr/1"]


def test_run_once_refuses_closed_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    closed = Issue(
        number=7,
        title="feat(cli): add version flag",
        body="Body",
        url="https://example.test/7",
        author="slaid098",
        labels=("agent:queued",),
        state="CLOSED",
    )
    monkeypatch.setattr(daemon.github, "get_issue", lambda *_args: closed)
    monkeypatch.setattr(
        daemon,
        "run_issue",
        lambda *_args: pytest.fail("closed issue must not run"),
    )

    assert daemon.run_once(config, "token", 7) == 1


def test_notify_outcome_treats_merged_as_finished(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    merged = RunOutcome(
        run_id="run",
        status="merged",
        run_dir=Path("run"),
        branch="feat/cli/issue-7-add-version-flag",
        pr_url="https://example.test/pr/7",
        issue_title="feat(cli): add version flag",
        duration_seconds=42,
    )
    finished: list[tuple[str, int]] = []
    monkeypatch.setattr(
        daemon.notify,
        "notify_finished",
        lambda _config, _number, _title, *, pr_url, duration_seconds, queue, active: (
            finished.append((pr_url, duration_seconds))
        ),
    )
    monkeypatch.setattr(
        daemon.notify,
        "notify_needs_human",
        lambda *_args, **_kwargs: pytest.fail("merged must not need a human"),
    )

    daemon._notify_outcome(
        config,
        7,
        "feat(cli): add version flag",
        merged,
        queue=0,
        active=0,
    )

    assert finished == [("https://example.test/pr/7", 42)]


def test_run_once_refuses_human_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    held = replace(_issue(), labels=("agent:queued", "agent:hold"))
    monkeypatch.setattr(daemon.github, "get_issue", lambda *_args: held)
    monkeypatch.setattr(
        daemon,
        "run_issue",
        lambda *_args, **_kwargs: pytest.fail("hold issue must not run"),
    )

    assert daemon.run_once(config, "token", 7) == 1


def test_dispatch_issues_skips_human_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    held = replace(_issue(), labels=("agent:queued", "agent:hold"))
    claimed: list[int] = []
    monkeypatch.setattr(
        daemon,
        "_claim_issue",
        lambda _config, issue, _token: claimed.append(issue.number) or True,
    )

    daemon._dispatch_issues(
        [held],
        {},
        config,
        "token",
        tmp_path / "orch.db",
        object(),  # type: ignore[arg-type]
    )

    assert claimed == []

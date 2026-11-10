from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from orch.exceptions.forge import ForgeError
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


def _blocker(number: int, state: str = "OPEN") -> Issue:
    return replace(_issue(number), state=state)


@pytest.fixture(autouse=True)
def _no_issue_blockers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep dependency lookups offline unless a test overrides them."""
    monkeypatch.setattr(daemon.forge, "list_blockers", lambda *_args: [])


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
    monkeypatch.setattr(daemon.forge, "resolve_token", lambda *_args: "token")
    monkeypatch.setattr(
        daemon.forge,
        "ensure_user",
        lambda config, token: calls.append(("user", token, config.repo.owner)),
    )
    monkeypatch.setattr(
        daemon.forge,
        "ensure_labels",
        lambda config, token, labels: calls.append(("labels", config.repo.full_name, labels)),
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
    monkeypatch.setattr(daemon.forge, "get_issue", lambda *_args: _issue())
    monkeypatch.setattr(daemon, "run_issue", lambda *_args: _outcome())

    assert daemon.run_once(config, "token", 7) == 0


def test_run_once_returns_zero_for_merged_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(daemon.forge, "get_issue", lambda *_args: _issue())
    monkeypatch.setattr(daemon, "run_issue", lambda *_args: _outcome("merged"))

    assert daemon.run_once(config, "token", 7) == 0


def test_run_once_returns_one_for_needs_human(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(daemon.forge, "get_issue", lambda *_args: _issue())
    monkeypatch.setattr(daemon, "run_issue", lambda *_args: _outcome("needs_human"))

    assert daemon.run_once(config, "token", 7) == 1


def test_run_once_returns_one_when_issue_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)

    def fail(*_args: object) -> Issue:
        raise ForgeError("missing")

    monkeypatch.setattr(daemon.forge, "get_issue", fail)

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
    monkeypatch.setattr(daemon.forge, "list_ready_issues", lambda *_args: [_issue()])
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
        raise ForgeError("offline")

    monkeypatch.setattr(daemon.forge, "list_ready_issues", fail)
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
        daemon.forge,
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
    db_pruned: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        daemon,
        "prune_runs",
        lambda data_dir, days: pruned.append((data_dir, days)),
    )
    monkeypatch.setattr(
        daemon.store,
        "prune_old_runs",
        lambda db_path, days: db_pruned.append((db_path, days)),
    )

    daemon._reap_finished(active, _config(tmp_path))

    assert list(active) == [9]
    assert pruned == [(tmp_path / "data", 7)]
    assert db_pruned == [(tmp_path / "data" / "orch.db", 7)]


def test_reap_finished_prunes_workspaces_and_skips_active_issues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    active = {7: FakeFuture(done=True, result=_outcome()), 9: FakeFuture(done=False)}
    calls: list[tuple[Config, str, set[int]]] = []
    monkeypatch.setattr(daemon, "prune_runs", lambda *_args: None)
    monkeypatch.setattr(daemon.store, "prune_old_runs", lambda *_args: None)
    monkeypatch.setattr(
        daemon,
        "prune_workspaces",
        lambda cfg, token, *, active_issues: calls.append((cfg, token, set(active_issues))),
    )

    daemon._reap_finished(active, config, "token")

    assert calls == [(config, "token", {9})]


def test_claim_issue_returns_true_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    monkeypatch.setattr(
        daemon.forge,
        "edit_labels",
        lambda _repo, _number, _token, *, add, remove: calls.append((add, remove)),
    )

    assert daemon._claim_issue(config, _issue(), "token")
    assert calls == [
        (
            ("agent:running",),
            ("agent:queued", "agent:needs-human", "agent:pr-open"),
        )
    ]


def test_claim_issue_returns_false_on_forge_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ForgeError("offline")

    monkeypatch.setattr(daemon.forge, "edit_labels", fail)

    assert not daemon._claim_issue(config, _issue(), "token")


def test_claim_failure_notification_is_rate_limited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    sent: list[int] = []
    daemon._claim_notified_at.clear()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ForgeError("offline")

    monkeypatch.setattr(daemon.forge, "edit_labels", fail)
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
        daemon.forge,
        "list_ready_issues",
        lambda *_args: [_issue()],
    )
    monkeypatch.setattr(
        daemon.forge,
        "comment_issue",
        lambda *args: calls.append(("comment", args)),
    )
    monkeypatch.setattr(
        daemon.forge,
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
        raise ForgeError("offline")

    monkeypatch.setattr(daemon.forge, "list_ready_issues", fail)

    daemon._reconcile_startup(config, "token")


def test_reconcile_startup_continues_after_one_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(daemon, "cleanup_managed", lambda: None)
    monkeypatch.setattr(daemon.store, "interrupt_active_runs", lambda *_args: 0)
    monkeypatch.setattr(
        daemon.forge,
        "list_ready_issues",
        lambda *_args: [_issue(7), _issue(8)],
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ForgeError("offline")

    monkeypatch.setattr(daemon.forge, "comment_issue", fail)

    daemon._reconcile_startup(config, "token")


def test_run_daemon_sends_accepted_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    accepted: list[tuple[int, int, int]] = []
    monkeypatch.setattr(daemon, "_reconcile_startup", lambda *_args: None)
    monkeypatch.setattr(daemon.forge, "list_ready_issues", lambda *_args: [_issue(7), _issue(8)])
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
    monkeypatch.setattr(daemon.forge, "get_issue", lambda *_args: _issue())
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
    monkeypatch.setattr(daemon.forge, "get_issue", lambda *_args: closed)
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
    monkeypatch.setattr(daemon.forge, "get_issue", lambda *_args: held)
    monkeypatch.setattr(
        daemon,
        "run_issue",
        lambda *_args, **_kwargs: pytest.fail("hold issue must not run"),
    )

    assert daemon.run_once(config, "token", 7) == 1


def test_run_once_refuses_a_blocked_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(daemon.forge, "get_issue", lambda *_args: _issue())
    monkeypatch.setattr(
        daemon.forge,
        "list_blockers",
        lambda *_args: [_blocker(3)],
    )
    monkeypatch.setattr(
        daemon,
        "run_issue",
        lambda *_args, **_kwargs: pytest.fail("blocked issue must not run"),
    )

    assert daemon.run_once(config, "token", 7) == 1


def test_run_once_refuses_when_blockers_cannot_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(daemon.forge, "get_issue", lambda *_args: _issue())

    def fail(*_args: object) -> list[Issue]:
        raise ForgeError("offline")

    monkeypatch.setattr(daemon.forge, "list_blockers", fail)
    monkeypatch.setattr(
        daemon,
        "run_issue",
        lambda *_args, **_kwargs: pytest.fail("blocked issue must not run"),
    )

    assert daemon.run_once(config, "token", 7) == 1


def test_run_once_runs_when_all_blockers_are_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(daemon.forge, "get_issue", lambda *_args: _issue())
    monkeypatch.setattr(
        daemon.forge,
        "list_blockers",
        lambda *_args: [_blocker(3, "CLOSED")],
    )
    monkeypatch.setattr(daemon, "run_issue", lambda *_args, **_kwargs: _outcome())

    assert daemon.run_once(config, "token", 7) == 0


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


def test_dispatch_issues_skips_an_issue_with_an_open_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    claimed: list[int] = []
    monkeypatch.setattr(
        daemon.forge,
        "list_blockers",
        lambda *_args: [_blocker(3)],
    )
    monkeypatch.setattr(daemon.store, "has_active_run", lambda *_args: False)
    monkeypatch.setattr(
        daemon,
        "_claim_issue",
        lambda _config, issue, _token: claimed.append(issue.number) or True,
    )

    daemon._dispatch_issues(
        [_issue(7)],
        {},
        config,
        "token",
        tmp_path / "orch.db",
        object(),  # type: ignore[arg-type]
    )

    assert claimed == []


def test_dispatch_issues_treats_an_unknown_blocker_state_as_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    claimed: list[int] = []
    monkeypatch.setattr(
        daemon.forge,
        "list_blockers",
        lambda *_args: [_blocker(3, state="")],
    )
    monkeypatch.setattr(daemon.store, "has_active_run", lambda *_args: False)
    monkeypatch.setattr(
        daemon,
        "_claim_issue",
        lambda _config, issue, _token: claimed.append(issue.number) or True,
    )

    daemon._dispatch_issues(
        [_issue(7)],
        {},
        config,
        "token",
        tmp_path / "orch.db",
        object(),  # type: ignore[arg-type]
    )

    assert claimed == []


def test_dispatch_issues_claims_when_all_blockers_are_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    claimed: list[int] = []
    monkeypatch.setattr(
        daemon.forge,
        "list_blockers",
        lambda *_args: [_blocker(3, "CLOSED"), _blocker(4, "closed")],
    )
    monkeypatch.setattr(daemon.store, "has_active_run", lambda *_args: False)
    monkeypatch.setattr(
        daemon,
        "_claim_issue",
        lambda _config, issue, _token: claimed.append(issue.number) or True,
    )
    executor = FakeExecutor()

    daemon._dispatch_issues(
        [_issue(7)],
        {},
        config,
        "token",
        tmp_path / "orch.db",
        executor,  # type: ignore[arg-type]
    )

    assert claimed == [7]
    assert len(executor.submitted) == 1


def test_dispatch_issues_skips_when_blockers_cannot_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    claimed: list[int] = []

    def fail(*_args: object) -> list[Issue]:
        raise ForgeError("offline")

    monkeypatch.setattr(daemon.forge, "list_blockers", fail)
    monkeypatch.setattr(daemon.store, "has_active_run", lambda *_args: False)
    monkeypatch.setattr(
        daemon,
        "_claim_issue",
        lambda _config, issue, _token: claimed.append(issue.number) or True,
    )

    daemon._dispatch_issues(
        [_issue(7)],
        {},
        config,
        "token",
        tmp_path / "orch.db",
        object(),  # type: ignore[arg-type]
    )

    assert claimed == []


def test_dispatch_issues_caches_blockers_per_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    calls: list[int] = []

    def blockers(_config: Config, number: int, _token: str) -> list[Issue]:
        calls.append(number)
        return []

    monkeypatch.setattr(daemon.forge, "list_blockers", blockers)
    monkeypatch.setattr(daemon.store, "has_active_run", lambda *_args: False)
    monkeypatch.setattr(daemon, "_claim_issue", lambda *_args: False)

    daemon._dispatch_issues(
        [_issue(7), _issue(7)],
        {},
        config,
        "token",
        tmp_path / "orch.db",
        object(),  # type: ignore[arg-type]
    )

    assert calls == [7]


def _second_config(config: Config) -> Config:
    return replace(
        config,
        name="other",
        repo=replace(config.repo, name="other"),
    )


def test_run_daemon_polls_every_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(_config(tmp_path), max_workers=2)
    second = _second_config(config)
    reconciled: list[str] = []
    monkeypatch.setattr(
        daemon,
        "_reconcile_startup",
        lambda project, _token: reconciled.append(project.repo.full_name),
    )
    monkeypatch.setattr(
        daemon.forge,
        "list_ready_issues",
        lambda *_args: [_issue(7)],
    )
    monkeypatch.setattr(daemon.store, "has_active_run", lambda *_args: False)
    monkeypatch.setattr(daemon, "_claim_issue", lambda *_args: True)
    executor = FakeExecutor()
    monkeypatch.setattr(daemon, "ThreadPoolExecutor", lambda **_kwargs: executor)

    def interrupt(_seconds: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", interrupt)

    tokens = {"slaid098/orch": "one", "slaid098/other": "two"}

    assert daemon.run_daemon((config, second), tokens) == 0
    assert reconciled == ["slaid098/orch", "slaid098/other"]
    assert [call[1][0].repo.full_name for call in executor.submitted] == [
        "slaid098/orch",
        "slaid098/other",
    ]
    assert [call[1][2] for call in executor.submitted] == ["one", "two"]


def test_run_daemon_shares_the_worker_pool_across_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(_config(tmp_path), max_workers=1)
    second = _second_config(config)
    monkeypatch.setattr(daemon, "_reconcile_startup", lambda *_args: None)
    monkeypatch.setattr(
        daemon.forge,
        "list_ready_issues",
        lambda *_args: [_issue(7)],
    )
    monkeypatch.setattr(daemon.store, "has_active_run", lambda *_args: False)
    monkeypatch.setattr(daemon, "_claim_issue", lambda *_args: True)
    executor = FakeExecutor()
    monkeypatch.setattr(daemon, "ThreadPoolExecutor", lambda **_kwargs: executor)

    def interrupt(_seconds: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", interrupt)

    tokens = {"slaid098/orch": "one", "slaid098/other": "two"}

    assert daemon.run_daemon((config, second), tokens) == 0
    assert len(executor.submitted) == 1


def test_reap_finished_routes_notifications_per_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    second = _second_config(config)
    monkeypatch.setattr(daemon, "prune_runs", lambda *_args: None)
    monkeypatch.setattr(daemon.store, "prune_old_runs", lambda *_args: None)
    monkeypatch.setattr(daemon, "prune_workspaces", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(daemon, "_queue_size", lambda *_args: 0)
    finished: list[tuple[str, int]] = []
    monkeypatch.setattr(
        daemon.notify,
        "notify_finished",
        lambda project, number, _title, **_kwargs: finished.append(
            (project.repo.full_name, number)
        ),
    )
    active = {
        ("slaid098/orch", 7): FakeFuture(done=True, result=_outcome()),
        ("slaid098/other", 8): FakeFuture(done=True, result=_outcome()),
    }

    daemon._reap_finished(
        active,
        (config, second),
        {"slaid098/orch": "one", "slaid098/other": "two"},
    )

    assert finished == [("slaid098/orch", 7), ("slaid098/other", 8)]
    assert active == {}


def test_reap_finished_requires_a_token_for_every_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    second = _second_config(config)
    monkeypatch.setattr(daemon, "prune_runs", lambda *_args: None)
    monkeypatch.setattr(daemon.store, "prune_old_runs", lambda *_args: None)

    with pytest.raises(ValueError, match="slaid098/other"):
        daemon._reap_finished(
            {},
            (config, second),
            {"slaid098/orch": "one"},
        )

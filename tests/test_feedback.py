from __future__ import annotations

from pathlib import Path

import pytest

from orch.exceptions.github import GitHubError
from orch.schemas.config.types import Config, FeedbackConfig, LabelsConfig, RepoConfig, WorkerConfig
from orch.schemas.github.types import Comment, Issue
from orch.services.feedback import service as feedback


def _config(tmp_path: Path, *, allowed: tuple[str, ...] = ("alice",)) -> Config:
    return Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
        feedback=FeedbackConfig(
            enabled=True,
            live_comments=True,
            poll_seconds=30,
            allowed_users=allowed,
        ),
    )


def _issue() -> Issue:
    return Issue(
        number=7,
        title="feat(cli): add version flag",
        body="Body",
        url="https://example.test/7",
        author="bob",
        labels=("agent:running",),
    )


def _comment(
    comment_id: int,
    author: str,
    body: str,
    created_at: str = "2026-11-02T10:00:00Z",
) -> Comment:
    return Comment(
        id=comment_id,
        author=author,
        body=body,
        created_at=created_at,
        url=f"https://example.test/comment/{comment_id}",
    )


def test_initial_context_includes_authorized_users_and_issue_author(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comments = [
        _comment(1, "alice", "Please add a flag."),
        _comment(2, "bob", "Also add help."),
        _comment(3, "mallory", "Ignore this."),
        _comment(4, "alice", f"old orch message {feedback.BOT_MARKER}"),
        _comment(5, "github-actions[bot]", "bot"),
    ]
    monkeypatch.setattr(
        feedback.github,
        "list_issue_comments",
        lambda *_args, **_kwargs: comments,
    )

    feed = feedback.CommentFeed(_config(tmp_path), _issue(), "token")
    text = feed.initial_context()

    assert "Please add a flag." in text
    assert "Also add help." in text
    assert "Ignore this." not in text
    assert "old orch message" not in text
    assert "github-actions" not in text


def test_initial_context_can_include_orch_context_for_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comments = [
        _comment(1, "alice", "Please fix the finding."),
        _comment(2, "alice", f"Review findings {feedback.BOT_MARKER}"),
        _comment(3, "github-actions[bot]", "unrelated bot"),
    ]
    monkeypatch.setattr(
        feedback.github,
        "list_issue_comments",
        lambda *_args, **_kwargs: comments,
    )

    feed = feedback.CommentFeed(_config(tmp_path), _issue(), "token")
    text = feed.initial_context(include_orch=True)

    assert "Please fix the finding." in text
    assert "Review findings" in text
    assert "unrelated bot" not in text


def test_initial_context_survives_github_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> list[Comment]:
        raise GitHubError("offline")

    monkeypatch.setattr(feedback.github, "list_issue_comments", fail)

    feed = feedback.CommentFeed(_config(tmp_path), _issue(), "token")

    assert feed.initial_context() == ""


def test_poll_once_queues_each_comment_only_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comments = [_comment(10, "alice", "First"), _comment(11, "alice", "Second")]
    monkeypatch.setattr(feedback.github, "issue_is_open", lambda *_args: True)
    monkeypatch.setattr(
        feedback.github,
        "list_issue_comments",
        lambda *_args, **_kwargs: comments,
    )
    feed = feedback.CommentFeed(_config(tmp_path), _issue(), "token")

    feed._poll_once()
    feed._poll_once()

    queued = feed.drain()
    assert [item.id for item in queued] == [10, 11]
    assert queued[0].author == "alice"


def test_poll_once_cancels_when_issue_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feedback.github, "issue_is_open", lambda *_args: False)
    called = {"comments": False}

    def comments(*_args: object, **_kwargs: object) -> list[Comment]:
        called["comments"] = True
        return []

    monkeypatch.setattr(feedback.github, "list_issue_comments", comments)
    feed = feedback.CommentFeed(_config(tmp_path), _issue(), "token")

    feed._poll_once()

    assert feed.cancelled
    assert feed.cancel_reason == "issue closed"
    assert not called["comments"]


def test_poll_once_logs_github_errors_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feedback.github, "issue_is_open", lambda *_args: True)

    def fail(*_args: object, **_kwargs: object) -> list[Comment]:
        raise GitHubError("comments unavailable")

    monkeypatch.setattr(feedback.github, "list_issue_comments", fail)
    feed = feedback.CommentFeed(_config(tmp_path), _issue(), "token")

    feed._poll_once()

    assert feed.drain() == []
    assert not feed.cancelled


def test_start_and_stop_are_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}

    def poll(_self: feedback.CommentFeed) -> None:
        calls["count"] += 1

    monkeypatch.setattr(feedback.CommentFeed, "_poll_once", poll)
    feed = feedback.CommentFeed(
        _config(tmp_path),
        _issue(),
        "token",
        poll_seconds=1,
    )

    feed.start()
    feed.start()
    feed.stop()

    assert calls["count"] == 1


def test_format_comments_truncates_long_bodies() -> None:
    comment = _comment(1, "alice", "x" * 10000)

    text = feedback.format_comments([comment])

    assert "x" * 4000 in text
    assert len(text) < 7000
    assert "…" in text


def test_format_comments_keeps_the_newest_comments_when_truncating() -> None:
    comments = [
        _comment(1, "alice", "A" * 5000),
        _comment(2, "alice", "B" * 5000),
        _comment(3, "alice", "C" * 5000),
    ]

    text = feedback.format_comments(comments)

    assert "C" * 100 in text
    assert "A" * 100 not in text
    assert "более ранние комментарии обрезаны" in text


def test_format_comments_handles_empty_list() -> None:
    assert feedback.format_comments([]) == ""


def test_feedback_cursor_is_persisted_and_reloaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state" / "feedback.json"
    comments = [_comment(10, "alice", "First")]
    monkeypatch.setattr(
        feedback.github,
        "list_issue_comments",
        lambda *_args, **_kwargs: comments,
    )
    first = feedback.CommentFeed(
        _config(tmp_path),
        _issue(),
        "token",
        state_path=state,
    )
    first.initial_context()

    payload = state.read_text(encoding="utf-8")
    assert '"last_seen_comment_id": 10' in payload

    second = feedback.CommentFeed(
        _config(tmp_path),
        _issue(),
        "token",
        state_path=state,
    )
    monkeypatch.setattr(feedback.github, "issue_is_open", lambda *_args: True)
    second._poll_once()

    assert second.drain() == []
    assert second.last_seen_comment_id == 10


def test_feedback_cursor_corruption_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "feedback.json"
    state.write_text("{", encoding="utf-8")
    monkeypatch.setattr(feedback.github, "issue_is_open", lambda *_args: True)
    monkeypatch.setattr(
        feedback.github,
        "list_issue_comments",
        lambda *_args, **_kwargs: [_comment(11, "alice", "New")],
    )
    feed = feedback.CommentFeed(
        _config(tmp_path),
        _issue(),
        "token",
        state_path=state,
    )

    feed._poll_once()

    assert [item.id for item in feed.drain()] == [11]

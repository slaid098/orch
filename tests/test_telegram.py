from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from loguru import logger

from orch.schemas.config.types import (
    Config,
    LabelsConfig,
    RepoConfig,
    TelegramConfig,
    WorkerConfig,
)
from orch.services.notify.service import (
    flush_pending,
    notify_accepted,
    notify_attempt_failed,
    notify_error,
    notify_finished,
    notify_needs_human,
    notify_recovered,
    notify_review_blocked,
    send,
)


class _FakeResponse:
    def __init__(self) -> None:
        self.closed = False

    def read(self) -> bytes:
        return b"{}"

    def close(self) -> None:
        self.closed = True


class _BrokenCloseResponse:
    def close(self) -> None:
        raise OSError("already closed")


def _config(tmp_path: Path, **telegram: object) -> Config:
    return Config(
        repo=RepoConfig(owner="owner", name="name", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(),
        data_dir=tmp_path / "data",
        telegram=TelegramConfig(**telegram),
    )


def _enabled_config(tmp_path: Path, **telegram: object) -> Config:
    settings: dict[str, object] = {
        "enabled": True,
        "chat_id": "-100123",
        "token": "secret-token",
    }
    settings.update(telegram)
    return _config(tmp_path, **settings)


def _install_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[object],
) -> list[tuple[urllib.request.Request, int]]:
    calls: list[tuple[urllib.request.Request, int]] = []

    def fake_urlopen(
        request: urllib.request.Request,
        timeout: int,
    ) -> object:
        calls.append((request, timeout))
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _body(request: urllib.request.Request) -> dict[str, object]:
    assert request.data is not None
    return json.loads(request.data)


def test_send_posts_json_with_thread_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    response = _FakeResponse()
    calls = _install_urlopen(monkeypatch, [response])
    config = _enabled_config(
        tmp_path,
        message_thread_id=42,
        api_base="https://tg.example/",
    )

    assert send(config, "hello") is True

    assert len(calls) == 1
    request, timeout = calls[0]
    assert timeout == 10
    assert request.full_url == "https://tg.example/botsecret-token/sendMessage"
    assert request.full_url.endswith("/sendMessage")
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert _body(request) == {
        "chat_id": "-100123",
        "text": "hello",
        "message_thread_id": 42,
    }
    assert response.closed is True


def test_send_omits_thread_id_when_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _install_urlopen(monkeypatch, [_FakeResponse()])

    assert send(_enabled_config(tmp_path), "hello") is True

    body = _body(calls[0][0])
    assert "message_thread_id" not in body
    assert body == {"chat_id": "-100123", "text": "hello"}


@pytest.mark.parametrize(
    "settings",
    [
        {"enabled": False, "chat_id": "-100123", "token": "secret-token"},
        {"enabled": True, "chat_id": "-100123", "token": None},
        {"enabled": True, "chat_id": "", "token": "secret-token"},
    ],
)
def test_send_skips_incomplete_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    settings: dict[str, object],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Telegram must not be called")

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    assert send(_config(tmp_path, **settings), "hello") is False


def test_send_returns_false_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    error = urllib.error.HTTPError(
        "https://api.telegram.org/botsecret-token/sendMessage",
        400,
        "Bad Request",
        {},
        None,
    )
    calls = _install_urlopen(monkeypatch, [error])

    assert send(_enabled_config(tmp_path), "hello") is False
    assert len(calls) == 1


def test_send_retries_transport_error_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _install_urlopen(
        monkeypatch,
        [urllib.error.URLError("offline"), urllib.error.URLError("offline")],
    )

    assert send(_enabled_config(tmp_path), "hello") is False
    assert len(calls) == 2
    assert [timeout for _, timeout in calls] == [10, 10]


def test_send_recovers_after_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _install_urlopen(
        monkeypatch,
        [urllib.error.URLError("offline"), _FakeResponse()],
    )

    assert send(_enabled_config(tmp_path), "hello") is True
    assert len(calls) == 2


def test_send_never_logs_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    messages: list[str] = []
    sink = logger.add(messages.append, format="{message}")
    error = urllib.error.HTTPError("https://telegram", 401, "Unauthorized", {}, None)
    _install_urlopen(monkeypatch, [error])
    try:
        assert send(_enabled_config(tmp_path), "hello") is False
    finally:
        logger.remove(sink)

    assert messages
    assert all("secret-token" not in message for message in messages)


def test_send_returns_false_on_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _install_urlopen(monkeypatch, [ValueError("bad api_base")])

    assert send(_enabled_config(tmp_path), "hello") is False
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [object(), _BrokenCloseResponse()],
    ids=["no_close", "close_error"],
)
def test_send_tolerates_close_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    response: object,
) -> None:
    _install_urlopen(monkeypatch, [response])

    assert send(_enabled_config(tmp_path), "hello") is True


def test_notify_accepted_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _install_urlopen(monkeypatch, [_FakeResponse()])

    assert (
        notify_accepted(
            _enabled_config(tmp_path),
            7,
            "Fix bug",
            queue=3,
            active=1,
        )
        is True
    )

    assert _body(calls[0][0])["text"] == (
        "🚀 Orch принято\n📦 repo: owner/name\n🎯 issue: #7 Fix bug\n👥 очередь: 3, активных: 1"
    )


def test_notify_finished_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _install_urlopen(monkeypatch, [_FakeResponse()])

    assert (
        notify_finished(
            _enabled_config(tmp_path),
            7,
            "Fix bug",
            pr_url="https://github.com/owner/name/pull/9",
            duration_seconds=720,
            queue=2,
            active=1,
        )
        is True
    )

    assert _body(calls[0][0])["text"] == (
        "✅ Orch завершено\n"
        "📦 repo: owner/name\n"
        "🎯 issue: #7 Fix bug\n"
        "🔗 PR: https://github.com/owner/name/pull/9\n"
        "⏱ длительность: 12 мин\n"
        "👥 очередь: 2, активных: 1"
    )


def test_notify_needs_human_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _install_urlopen(monkeypatch, [_FakeResponse()])

    assert (
        notify_needs_human(
            _enabled_config(tmp_path),
            7,
            "Fix bug",
            reason="CI упал",
            queue=2,
            active=0,
        )
        is True
    )

    assert _body(calls[0][0])["text"] == (
        "🚨 Orch нужен человек\n"
        "📦 repo: owner/name\n"
        "🎯 issue: #7 Fix bug\n"
        "💥 причина: CI упал\n"
        "👥 очередь: 2, активных: 0"
    )


def test_notify_returns_false_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Telegram must not be called")

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    assert notify_accepted(_config(tmp_path), 7, "Fix bug", queue=0, active=0) is False


def test_notify_attempt_failed_contains_actionable_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _install_urlopen(monkeypatch, [_FakeResponse()])

    assert notify_attempt_failed(
        _enabled_config(tmp_path),
        7,
        "Fix bug",
        attempt=2,
        max_attempts=3,
        error=RuntimeError("worker stopped"),
        run_id="run-7",
    )

    text = _body(calls[0][0])["text"]
    assert text.startswith("⚠️ Orch: попытка не удалась")
    assert "🎯 issue: #7 Fix bug" in text
    assert "🔁 попытка: 2/3" in text
    assert "💥 ошибка: RuntimeError: worker stopped" in text
    assert "🗂 run: run-7" in text


def test_notify_review_blocked_contains_findings_and_round(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _install_urlopen(monkeypatch, [_FakeResponse()])

    assert notify_review_blocked(
        _enabled_config(tmp_path),
        7,
        "Fix bug",
        findings=2,
        review_round=1,
        max_rounds=3,
        run_id="run-7",
        pr_url="https://example.test/pull/1",
    )

    text = _body(calls[0][0])["text"]
    assert text.startswith("🛑 Orch: ревьюер заблокировал PR")
    assert "🧐 findings: 2" in text
    assert "🔁 раунд ревью: 1/3" in text
    assert "🔗 PR: https://example.test/pull/1" in text


def test_notify_error_redacts_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _install_urlopen(monkeypatch, [_FakeResponse()])

    assert notify_error(
        _enabled_config(tmp_path),
        stage="reviewer",
        error=(
            "Bearer abc123 ghp_abcdefghijklmnopqrstuvwxyz "
            '{"token": "json-token-value", '
            "\"api_key\": 'json-api-key-value'}"
        ),
        issue_number=7,
        issue_title="Fix bug",
        run_id="run-7",
        attempt=1,
        max_attempts=2,
    )

    text = _body(calls[0][0])["text"]
    assert "🔥 Orch: ошибка" in text
    assert "🧩 stage: reviewer" in text
    assert "🔁 попытка: 1/2" in text
    assert "Bearer abc123" not in text
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in text
    assert "json-token-value" not in text
    assert "json-api-key-value" not in text
    assert "[redacted]" in text


def test_notify_recovered_has_emoji_and_component(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _install_urlopen(monkeypatch, [_FakeResponse()])

    assert notify_recovered(
        _enabled_config(tmp_path),
        component="daemon-poll",
        detail="Forge polling works again.",
    )

    text = _body(calls[0][0])["text"]
    assert text.startswith("🟢 Orch: восстановлено")
    assert "🧩 component: daemon-poll" in text


def test_send_queues_a_failed_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_urlopen(
        monkeypatch,
        [OSError("offline"), OSError("still offline")],
    )
    config = _enabled_config(tmp_path)

    assert not send(config, "⚠️ queued message")

    queue = config.data_dir / "telegram-queue.jsonl"
    assert queue.exists()
    assert "queued message" in queue.read_text(encoding="utf-8")


def test_flush_pending_delivers_and_removes_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _install_urlopen(monkeypatch, [_FakeResponse()])
    config = _enabled_config(tmp_path)
    queue = config.data_dir / "telegram-queue.jsonl"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        json.dumps({"text": "🟢 recovered"}) + "\nbad json\n",
        encoding="utf-8",
    )

    assert flush_pending(config) == 1
    assert _body(calls[0][0])["text"] == "🟢 recovered"
    assert not queue.exists()

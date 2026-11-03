"""One-way Telegram notifications sent through the Bot API."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from orch.config.logger import get_logger
from orch.schemas.config.types import Config

log = get_logger(component="notify")

_TIMEOUT_SECONDS = 10
_MAX_ATTEMPTS = 2
_SECONDS_PER_MINUTE = 60
_MAX_ERROR_CHARS = 1000
_SECRET_PATTERNS = (
    re.compile(r"bot\d+:[A-Za-z0-9_-]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"Bearer\s+[^\s]+", re.IGNORECASE),
    re.compile(
        r"""(?ix)
        ["']?[\w-]*(?:token|password|secret|api[_-]?key)["']?
        \s*[:=]\s*
        (?:"[^"]*"|'[^']*'|[^\s,}]+)
        """
    ),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
)


def send(config: Config, text: str) -> bool:
    """Send a plain-text Telegram message.

    The call never raises: disabled or incomplete configuration, transport
    failures, and API errors all return ``False``. A failed message is queued
    in ``data_dir`` and retried by :func:`flush_pending` on the next startup.
    Transport errors are retried once.

    Args:
        config: Orchestrator configuration with Telegram settings.
        text: Message body to deliver.

    Returns:
        ``True`` when Telegram accepted the message, otherwise ``False``.
    """
    if not _telegram_ready(config):
        return False
    if _send_now(config, text):
        return True
    _queue(config, text)
    return False


def flush_pending(config: Config) -> int:
    """Retry messages left in the Telegram queue by a previous failure.

    Args:
        config: Orchestrator configuration with Telegram settings.

    Returns:
        Number of queued messages accepted by Telegram.
    """
    if not _telegram_ready(config):
        return 0
    path = _queue_path(config)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    except OSError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).warning(
            "Could not read the Telegram queue"
        )
        return 0

    pending: list[str] = []
    sent = 0
    for line in lines:
        text = _queued_text(line)
        if text is None:
            continue
        if _send_now(config, text):
            sent += 1
        else:
            pending.append(line)
    _write_queue(path, pending)
    if sent:
        log.info("Delivered {} queued Telegram notification(s)", sent)
    return sent


def _send_now(config: Config, text: str) -> bool:
    telegram = config.telegram
    payload: dict[str, object] = {
        "chat_id": telegram.chat_id,
        "text": text,
    }
    if telegram.message_thread_id is not None:
        payload["message_thread_id"] = telegram.message_thread_id

    url = f"{telegram.api_base.rstrip('/')}/bot{telegram.token}/sendMessage"
    data = json.dumps(payload).encode("utf-8")

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as ex:
            log.bind(status=ex.code, error_type=type(ex).__name__).warning(
                "Telegram rejected notification (HTTP {})",
                ex.code,
            )
            return False
        except OSError as ex:
            # Bind only the type: exception text may contain the tokenized URL.
            log.bind(error_type=type(ex).__name__).warning(
                "Telegram transport error on attempt {}/{}",
                attempt,
                _MAX_ATTEMPTS,
            )
            continue
        except Exception as ex:
            log.bind(error_type=type(ex).__name__).warning(
                "Telegram notification failed: {}",
                type(ex).__name__,
            )
            return False
        else:
            _close(response)
            return True

    return False


def _telegram_ready(config: Config) -> bool:
    telegram = config.telegram
    if not telegram.enabled:
        log.debug("Telegram notifications are disabled")
        return False
    if not telegram.token:
        log.warning("Telegram notification skipped: token is not configured")
        return False
    if not telegram.chat_id:
        log.warning("Telegram notification skipped: chat_id is not configured")
        return False
    return True


def _queue_path(config: Config) -> Path:
    return config.data_dir / "telegram-queue.jsonl"


def _queue(config: Config, text: str) -> None:
    try:
        path = _queue_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
    except OSError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).warning(
            "Could not queue failed Telegram notification"
        )


def _queued_text(line: str) -> str | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    text = payload.get("text") if isinstance(payload, dict) else None
    return text if isinstance(text, str) and text else None


def _write_queue(path: Path, lines: list[str]) -> None:
    try:
        if lines:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except OSError as ex:
        log.bind(error=ex, error_type=type(ex).__name__).warning(
            "Could not update the Telegram queue"
        )


def notify_accepted(
    config: Config,
    issue_number: int,
    issue_title: str,
    *,
    queue: int,
    active: int,
) -> bool:
    """Notify that an issue was accepted into the pipeline.

    Args:
        config: Orchestrator configuration with Telegram settings.
        issue_number: GitHub issue number.
        issue_title: GitHub issue title.
        queue: Issues waiting in the queue.
        active: Issues currently running.

    Returns:
        ``True`` when Telegram accepted the message, otherwise ``False``.
    """
    text = "\n".join(
        [
            "🚀 Orch принято",
            _repo_line(config),
            _issue_line(issue_number, issue_title),
            _queue_line(queue, active),
        ]
    )
    return send(config, text)


def notify_finished(
    config: Config,
    issue_number: int,
    issue_title: str,
    *,
    pr_url: str,
    duration_seconds: int,
    queue: int,
    active: int,
) -> bool:
    """Notify that an issue finished with a pull request.

    Args:
        config: Orchestrator configuration with Telegram settings.
        issue_number: GitHub issue number.
        issue_title: GitHub issue title.
        pr_url: URL of the opened pull request.
        duration_seconds: Elapsed work duration in seconds.
        queue: Issues waiting in the queue.
        active: Issues currently running.

    Returns:
        ``True`` when Telegram accepted the message, otherwise ``False``.
    """
    text = "\n".join(
        [
            "✅ Orch завершено",
            _repo_line(config),
            _issue_line(issue_number, issue_title),
            f"🔗 PR: {pr_url}",
            f"⏱ длительность: {_format_duration(duration_seconds)}",
            _queue_line(queue, active),
        ]
    )
    return send(config, text)


def notify_needs_human(
    config: Config,
    issue_number: int,
    issue_title: str,
    *,
    reason: str,
    queue: int,
    active: int,
) -> bool:
    """Notify that an issue requires human attention.

    Args:
        config: Orchestrator configuration with Telegram settings.
        issue_number: GitHub issue number.
        issue_title: GitHub issue title.
        reason: Short explanation of why a human is needed.
        queue: Issues waiting in the queue.
        active: Issues currently running.

    Returns:
        ``True`` when Telegram accepted the message, otherwise ``False``.
    """
    text = "\n".join(
        [
            "🚨 Orch нужен человек",
            _repo_line(config),
            _issue_line(issue_number, issue_title),
            f"💥 причина: {_clean(reason)}",
            _queue_line(queue, active),
        ]
    )
    return send(config, text)


def notify_attempt_failed(
    config: Config,
    issue_number: int,
    issue_title: str,
    *,
    attempt: int,
    max_attempts: int,
    error: Exception | str,
    run_id: str = "",
    pr_url: str | None = None,
) -> bool:
    """Notify that one attempt failed and a retry may follow."""
    lines = [
        "⚠️ Orch: попытка не удалась",
        _repo_line(config),
        _issue_line(issue_number, issue_title),
        f"🔁 попытка: {attempt}/{max_attempts}",
        f"💥 ошибка: {_error_text(error)}",
    ]
    if pr_url:
        lines.append(f"🔗 PR: {pr_url}")
    if run_id:
        lines.append(f"🗂 run: {run_id}")
    return send(config, "\n".join(lines))


def notify_review_blocked(
    config: Config,
    issue_number: int,
    issue_title: str,
    *,
    findings: int,
    review_round: int,
    max_rounds: int,
    run_id: str = "",
    pr_url: str = "",
) -> bool:
    """Notify that a reviewer found blocking issues."""
    lines = [
        "🛑 Orch: ревьюер заблокировал PR",
        _repo_line(config),
        _issue_line(issue_number, issue_title),
        f"🧐 findings: {findings}",
        f"🔁 раунд ревью: {review_round}/{max_rounds}",
    ]
    if pr_url:
        lines.append(f"🔗 PR: {pr_url}")
    if run_id:
        lines.append(f"🗂 run: {run_id}")
    return send(config, "\n".join(lines))


def notify_error(
    config: Config,
    *,
    stage: str,
    error: Exception | str,
    issue_number: int | None = None,
    issue_title: str = "",
    run_id: str = "",
    pr_url: str | None = None,
    attempt: int | None = None,
    max_attempts: int | None = None,
) -> bool:
    """Notify about a technical or system failure in any stage."""
    lines = [
        "🔥 Orch: ошибка",
        _repo_line(config),
        _issue_line(issue_number, issue_title),
        f"🧩 stage: {stage}",
    ]
    if attempt is not None and max_attempts is not None:
        lines.append(f"🔁 попытка: {attempt}/{max_attempts}")
    lines.append(f"💥 ошибка: {_error_text(error)}")
    if pr_url:
        lines.append(f"🔗 PR: {pr_url}")
    if run_id:
        lines.append(f"🗂 run: {run_id}")
    return send(config, "\n".join(lines))


def notify_recovered(
    config: Config,
    *,
    component: str,
    detail: str = "",
) -> bool:
    """Notify that a previously failing background component recovered."""
    lines = [
        "🟢 Orch: восстановлено",
        _repo_line(config),
        f"🧩 component: {component}",
    ]
    if detail:
        lines.append(f"ℹ️ {_clean(detail)}")
    return send(config, "\n".join(lines))


def _repo_line(config: Config) -> str:
    return f"📦 repo: {config.repo.full_name}"


def _issue_line(issue_number: int | None, issue_title: str) -> str:
    if issue_number is None:
        return "🎯 issue: —"
    title = f" {issue_title}" if issue_title else ""
    return f"🎯 issue: #{issue_number}{title}"


def _queue_line(queue: int, active: int) -> str:
    return f"👥 очередь: {queue}, активных: {active}"


def _format_duration(duration_seconds: int) -> str:
    """Format a non-negative duration, rounding partial minutes up."""
    seconds = max(duration_seconds, 0)
    minutes = (seconds + _SECONDS_PER_MINUTE - 1) // _SECONDS_PER_MINUTE
    return f"{minutes} мин"


def _error_text(error: Exception | str) -> str:
    text = f"{type(error).__name__}: {error}" if isinstance(error, Exception) else error
    return _clean(text)


def _clean(text: str) -> str:
    value = text.strip()
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[redacted]", value)
    return value[:_MAX_ERROR_CHARS]


def _close(response: object) -> None:
    close = getattr(response, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        return

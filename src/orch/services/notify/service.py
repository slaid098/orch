"""One-way Telegram notifications sent through the Bot API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from orch.config.logger import get_logger
from orch.schemas.config.types import Config

log = get_logger(component="notify")

_TIMEOUT_SECONDS = 10
_MAX_ATTEMPTS = 2
_SECONDS_PER_MINUTE = 60


def send(config: Config, text: str) -> bool:
    """Send a plain-text Telegram message.

    The call never raises: disabled or incomplete configuration, transport
    failures, and API errors all return ``False``. Transport errors are
    retried once.

    Args:
        config: Orchestrator configuration with Telegram settings.
        text: Message body to deliver.

    Returns:
        ``True`` when Telegram accepted the message, otherwise ``False``.
    """
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
            "Orch принято",
            f"repo: {config.repo.full_name}",
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
            "Orch завершено",
            f"repo: {config.repo.full_name}",
            _issue_line(issue_number, issue_title),
            f"PR: {pr_url}",
            f"длительность: {_format_duration(duration_seconds)}",
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
            "Orch нужен человек",
            f"repo: {config.repo.full_name}",
            _issue_line(issue_number, issue_title),
            f"причина: {reason}",
            _queue_line(queue, active),
        ]
    )
    return send(config, text)


def _issue_line(issue_number: int, issue_title: str) -> str:
    return f"issue: #{issue_number} {issue_title}"


def _queue_line(queue: int, active: int) -> str:
    return f"очередь: {queue}, активных: {active}"


def _format_duration(duration_seconds: int) -> str:
    """Format a non-negative duration, rounding partial minutes up."""
    seconds = max(duration_seconds, 0)
    minutes = (seconds + _SECONDS_PER_MINUTE - 1) // _SECONDS_PER_MINUTE
    return f"{minutes} мин"


def _close(response: object) -> None:
    close = getattr(response, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        return

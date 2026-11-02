"""Minimal Clef classifier client used by the watchdog."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from typing import Any

from orch.config.logger import get_logger
from orch.schemas.config.types import WatchdogConfig
from orch.schemas.watchdog.types import Classification

log = get_logger(component="watchdog.clef")

_TIMEOUT_SECONDS = 30
_ATTEMPTS = 2
_BACKOFF_SECONDS = 0.5

_AGENT_STATE_INSTRUCTIONS = (
    "Что сейчас делает агент? Выбери одно состояние, которое лучше всего "
    "описывает текущую ситуацию."
)
_WAIT_HELPFUL_INSTRUCTIONS = (
    "Поможет ли ожидание ещё 10 минут без вмешательства? Вероятность 0..1."
)
_AGENT_STATE_CRITERIA = {
    "working": (
        "Агент активно продвигается к цели: выполняет новые команды, правит файлы, "
        "читает новые участки кода. Долгая сборка, тесты, установка зависимостей или "
        "ожидание ответа сети — это нормальная работа, а не зацикливание."
    ),
    "looping": (
        "Агент повторяет одни и те же действия без нового результата: одинаковые "
        "команды, одинаковые правки или одинаковые ошибки снова и снова, и это не "
        "продвигает задачу."
    ),
    "stuck": (
        "Агент перестал продвигаться: давно нет действий, меняющих результат, или он "
        "завис и не повторяет команды. При этом нет признаков нормальной долгой работы "
        "или ожидания внешнего сервиса."
    ),
    "waiting_external": (
        "Агент ждёт внешний сервис: сеть, API, загрузку образа, ответ Ollama или GitHub. "
        "Долгое ожидание внешнего ответа само по себе не является зацикливанием."
    ),
    "needs_human": (
        "Без вмешательства человека задачу не решить: не хватает доступа, секрета или "
        "решения по требованиям, либо агент явно просит помощи."
    ),
}

_CACHE: dict[str, Classification] = {}


def classify(config: WatchdogConfig, state: str) -> Classification | None:
    """Ask Clef to classify a compact description of the agent run.

    Args:
        config: Watchdog settings with the endpoint, model, and API key.
        state: Compact text state built from recent agent events.

    Returns:
        Parsed classification, or ``None`` when Clef is unavailable or returns
        an unusable response.
    """
    cache_key = hashlib.sha256(state.encode("utf-8")).hexdigest()
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    payload = {
        "model": config.model,
        "state": state,
        "questions": {
            "agent_state": {
                "type": "choice",
                "instructions": _AGENT_STATE_INSTRUCTIONS,
                "criteria": _AGENT_STATE_CRITERIA,
            },
            "wait_helpful": {
                "type": "noul",
                "instructions": _WAIT_HELPFUL_INSTRUCTIONS,
            },
        },
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    for attempt in range(1, _ATTEMPTS + 1):
        try:
            response = _post(config.endpoint, body, headers)
            classification = _parse(response)
        except Exception as exc:
            log.bind(
                attempt=attempt,
                error=exc,
                error_type=type(exc).__name__,
            ).warning("Clef classification request failed")
            if attempt < _ATTEMPTS:
                time.sleep(_BACKOFF_SECONDS)
            continue
        else:
            if classification is None:
                log.bind(attempt=attempt).warning(
                    "Clef returned no usable classification"
                )
                if attempt < _ATTEMPTS:
                    time.sleep(_BACKOFF_SECONDS)
                continue
            _CACHE[cache_key] = classification
            return classification

    return None


def _post(endpoint: str, body: bytes, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def _parse(raw: Any) -> Classification | None:
    if not isinstance(raw, dict):
        return None
    answers = raw.get("answers")
    if not isinstance(answers, dict):
        return None

    agent_state = answers.get("agent_state")
    if not isinstance(agent_state, dict):
        return None
    state = agent_state.get("choice")
    if not isinstance(state, str) or not state:
        return None

    probabilities_raw = agent_state.get("probabilities")
    probabilities = (
        {
            str(key): _as_float(value)
            for key, value in probabilities_raw.items()
        }
        if isinstance(probabilities_raw, dict)
        else {}
    )
    wait_helpful_raw = answers.get("wait_helpful")
    wait_helpful = (
        _as_float(wait_helpful_raw.get("noul"))
        if isinstance(wait_helpful_raw, dict)
        else 0.0
    )

    return Classification(
        state=state,
        confidence=_as_float(agent_state.get("confidence")),
        probabilities=probabilities,
        wait_helpful=wait_helpful,
    )


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

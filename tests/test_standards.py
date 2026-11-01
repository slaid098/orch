from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.standards import Handoff, StandardsError, format_pr_body, load_handoff, validate_title


@pytest.mark.parametrize(
    "title",
    [
        "feat(cli): add version flag",
        "fix(pipeline): retry a failed attempt",
        "build(docker): refresh the worker image",
        "ci(github): run the test suite",
        "revert(worker): roll back the prompt change",
    ],
)
def test_validate_title_accepts_conventional_title(title: str) -> None:
    assert validate_title(title) == title


@pytest.mark.parametrize(
    "title",
    [
        "add version flag",
        "feat: add version flag",
        "feat(cli): добавь версию",
        "chore(cli): " + "x" * 70,
    ],
)
def test_validate_title_rejects_invalid_titles(title: str) -> None:
    with pytest.raises(StandardsError):
        validate_title(title)


def test_format_pr_body_has_required_sections() -> None:
    handoff = Handoff(
        title="feat(cli): add version flag",
        what="Добавлен флаг версии.",
        why="Пользователю нужен способ проверить версию.",
        watch_out="—",
        pending="Добавить тест.",
    )

    body = format_pr_body(handoff, 7)

    assert body.startswith("## Что сделано\nДобавлен флаг версии.")
    assert "## Почему\nПользователю нужен способ проверить версию." in body
    assert "## Watch out\n—" in body
    assert "## Pending\nДобавить тест." in body
    assert body.rstrip().endswith("Closes #7")


def test_load_handoff_reads_and_validates(tmp_path: Path) -> None:
    (tmp_path / "handoff.json").write_text(
        json.dumps(
            {
                "title": "fix(worker): keep retries clean",
                "what": "Попытки теперь начинаются с чистого клона.",
                "why": "Старые изменения не должны влиять на новую попытку.",
                "watch_out": "",
                "pending": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    handoff = load_handoff(tmp_path)

    assert handoff.title == "fix(worker): keep retries clean"
    assert handoff.watch_out == ""


def test_load_handoff_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(StandardsError):
        load_handoff(tmp_path)


def test_format_pr_body_requires_russian_what_and_why() -> None:
    handoff = Handoff(
        title="feat(cli): add version flag",
        what="Added a flag.",
        why="Users need it.",
        watch_out="—",
        pending="—",
    )

    with pytest.raises(StandardsError):
        format_pr_body(handoff, 1)
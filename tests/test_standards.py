from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.schemas.standards.types import Handoff
from orch.services.standards import (
    StandardsError,
    branch_name,
    format_issue_body,
    format_pr_body,
    load_handoff,
    validate_branch,
    validate_issue_body,
    validate_title,
)


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
        "feat(cli): " + "x" * 62,
    ],
)
def test_validate_title_rejects_invalid_titles(title: str) -> None:
    with pytest.raises(StandardsError):
        validate_title(title)


def test_issue_body_requires_what_and_why() -> None:
    body = format_issue_body(
        "Добавить проверку заголовка.",
        "Невалидные issue не должны запускать worker.",
    )

    assert validate_issue_body(body) == body.strip()
    assert body.startswith("## Что сделать\n")
    assert "## Почему\n" in body


@pytest.mark.parametrize(
    "body",
    [
        "Просто текст.",
        "## Что сделать\n\n## Почему\nПричина.",
        "## Почему\nПричина.\n\n## Что сделать\nДействие.",
        "## Что сделать\nДействие.\n\n## Что сделать\nЕщё.\n\n## Почему\nПричина.",
    ],
)
def test_issue_body_rejects_incomplete_sections(body: str) -> None:
    with pytest.raises(StandardsError):
        validate_issue_body(body)


def test_format_pr_body_has_required_sections() -> None:
    handoff = Handoff(
        what="Добавлен флаг версии.",
        why="Пользователю нужен способ проверить версию.",
        watch_out="—",
        pending="Добавить тест.",
    )

    body = format_pr_body(handoff, 7)

    assert body.startswith("## Что сделано\nДобавлен флаг версии.")
    assert "## Почему\nПользователю нужен способ проверить версию." in body
    assert "## На что обратить внимание\n—" in body
    assert "## Что осталось\nДобавить тест." in body
    assert body.rstrip().endswith("Closes #7")


@pytest.mark.parametrize(
    ("title", "issue", "branch"),
    [
        (
            "feat(cli): add version flag",
            7,
            "feat/cli/issue-7-add-version-flag",
        ),
        (
            "fix(worker): keep retries clean",
            42,
            "fix/worker/issue-42-keep-retries-clean",
        ),
    ],
)
def test_branch_name_uses_issue_title(
    title: str,
    issue: int,
    branch: str,
) -> None:
    assert branch_name(title, issue) == branch
    assert validate_branch(branch, issue) == branch


def test_validate_branch_rejects_foreign_issue_number() -> None:
    with pytest.raises(StandardsError):
        validate_branch("feat/cli/issue-8-add-version-flag", 7)


def test_load_handoff_reads_and_validates(tmp_path: Path) -> None:
    (tmp_path / "handoff.json").write_text(
        json.dumps(
            {
                "what": "Попытки продолжаются в той же рабочей папке.",
                "why": "Сделанная работа не должна теряться при перезапуске.",
                "watch_out": "",
                "pending": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    handoff = load_handoff(tmp_path)

    assert handoff.what == "Попытки продолжаются в той же рабочей папке."
    assert handoff.watch_out == ""


def test_load_handoff_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(StandardsError):
        load_handoff(tmp_path)


def test_format_pr_body_requires_russian_what_and_why() -> None:
    handoff = Handoff(
        what="Added a flag.",
        why="Users need it.",
        watch_out="—",
        pending="—",
    )

    with pytest.raises(StandardsError):
        format_pr_body(handoff, 1)
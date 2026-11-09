from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.exceptions.standards import StandardsError
from orch.schemas.standards.types import Handoff
from orch.services.standards import (
    branch_name,
    format_pr_body,
    load_handoff,
    validate_branch,
    validate_title,
)
from orch.services.standards.policy import validate_issue_body


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
    body = (
        "## Что сделать\nДобавить проверку заголовка.\n\n"
        "## Почему\nНевалидные issue не должны запускать worker.\n"
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
    assert "Closes #7" in body
    assert "<!-- orch:issue=7 -->" in body


def test_branch_name_is_stable_across_title_changes() -> None:
    assert branch_name(7) == "orch/issue-7"
    assert validate_branch("orch/issue-7", 7) == "orch/issue-7"


def test_validate_branch_accepts_a_legacy_branch() -> None:
    branch = "feat/cli/issue-7-add-version-flag"

    assert validate_branch(branch, 7) == branch


def test_validate_branch_rejects_foreign_issue_number() -> None:
    with pytest.raises(StandardsError):
        validate_branch("feat/cli/issue-8-add-version-flag", 7)
    with pytest.raises(StandardsError):
        validate_branch("orch/issue-8", 7)


def test_validate_branch_rejects_an_unrelated_branch() -> None:
    with pytest.raises(StandardsError, match="Branch must be"):
        validate_branch("not-a-branch", 7)


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


def test_validate_title_rejects_multiline_input() -> None:
    with pytest.raises(StandardsError, match="single line"):
        validate_title("feat(cli): add\nflag")


def test_issue_body_must_not_be_empty() -> None:
    with pytest.raises(StandardsError, match="must not be empty"):
        validate_issue_body("  ")


def test_load_handoff_rejects_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "handoff.json").write_text("{", encoding="utf-8")

    with pytest.raises(StandardsError, match="Could not read"):
        load_handoff(tmp_path)


def test_load_handoff_rejects_non_object(tmp_path: Path) -> None:
    (tmp_path / "handoff.json").write_text("[]", encoding="utf-8")

    with pytest.raises(StandardsError, match="JSON object"):
        load_handoff(tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        {"what": 1, "why": "Причина.", "watch_out": "", "pending": ""},
        {"what": " ", "why": "Причина.", "watch_out": "", "pending": ""},
    ],
)
def test_load_handoff_rejects_invalid_fields(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    (tmp_path / "handoff.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

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
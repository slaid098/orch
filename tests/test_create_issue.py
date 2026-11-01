from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import create_issue


def _body_file(tmp_path: Path) -> Path:
    path = tmp_path / "body.md"
    path.write_text(
        "## Что сделать\nДобавить проверку.\n\n## Почему\nНужна защита.\n",
        encoding="utf-8",
    )
    return path


def test_validate_only_does_not_call_gh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def fail_run(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("gh must not run")

    monkeypatch.setattr(create_issue.subprocess, "run", fail_run)

    code = create_issue.main(
        [
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            "--validate-only",
        ]
    )

    assert code == 0
    assert not called
    assert "OK: test(standards): validate issue creation" in capsys.readouterr().out


def test_invalid_title_returns_policy_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        create_issue.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("gh must not run"),
    )

    code = create_issue.main(
        [
            "--title",
            "Add issue",
            "--body-file",
            str(_body_file(tmp_path)),
        ]
    )

    assert code == 2


def test_create_issue_calls_gh_with_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands: list[list[str]] = []

    def run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="https://example.test/issues/12\n",
            stderr="",
        )

    monkeypatch.setattr(create_issue.subprocess, "run", run)

    code = create_issue.main(
        [
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
        ]
    )

    assert code == 0
    assert commands[0][:4] == ["gh", "issue", "create", "--title"]
    assert "--label" in commands[0]
    assert "agent:queued" in commands[0]
    assert "https://example.test/issues/12" in capsys.readouterr().out


def test_gh_failure_returns_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        create_issue.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="boom",
        ),
    )

    code = create_issue.main(
        [
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
        ]
    )

    assert code == 1

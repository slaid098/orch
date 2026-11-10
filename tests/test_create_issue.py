from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orch.schemas.github.types import Issue
from scripts import create_issue

_ISSUE_BODY = (
    "## Контекст\nТекущая проверка описана в `policy.py`.\n\n"
    "## Задача\nДобавить проверку заголовка.\n\n"
    "## Контракты\nВалидатор возвращает исходный текст.\n\n"
    "## Инварианты\nПорядок разделов фиксирован.\n\n"
    "## Граничные случаи\nПустое тело отклоняется.\n\n"
    "## Влияние на связанные компоненты\nТесты и README.\n\n"
    "## Вне scope\nИзменение заголовка.\n\n"
    "## Критерии приёмки\n`uv run pytest` проходит.\n"
)


def _body_file(tmp_path: Path) -> Path:
    path = tmp_path / "body.md"
    path.write_text(_ISSUE_BODY, encoding="utf-8")
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


def test_old_two_section_body_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "body.md"
    path.write_text(
        "## Что сделать\nДобавить проверку.\n\n## Почему\nНужна защита.\n",
        encoding="utf-8",
    )

    code = create_issue.main(
        [
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(path),
            "--validate-only",
        ]
    )

    assert code == 2


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


def test_create_issue_uses_forgejo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, str, str, dict[str, object]]] = []
    monkeypatch.setenv("FORGEJO_URL", "https://git.example.test")
    monkeypatch.setenv("FORGEJO_TOKEN", "secret")
    monkeypatch.setattr(
        create_issue,
        "create_forgejo_issue",
        lambda url, repo, token, **kwargs: calls.append(
            (url, repo, token, kwargs)
        )
        or "https://git.example.test/slaid098/orch/issues/5",
    )

    code = create_issue.main(
        [
            "--forge",
            "forgejo",
            "--repo",
            "slaid098/orch",
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
        ]
    )

    assert code == 0
    assert calls[0][0:3] == (
        "https://git.example.test",
        "slaid098/orch",
        "secret",
    )
    assert calls[0][3]["labels"] == ("agent:queued",)
    assert "https://git.example.test/slaid098/orch/issues/5" in capsys.readouterr().out


def test_forgejo_url_flag_wins_over_the_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("FORGEJO_URL", "https://ignored.example.test")
    monkeypatch.setenv("FORGEJO_TOKEN", "secret")
    monkeypatch.setattr(
        create_issue,
        "create_forgejo_issue",
        lambda url, _repo, _token, **_kwargs: calls.append(url) or "url",
    )

    code = create_issue.main(
        [
            "--forge",
            "forgejo",
            "--repo",
            "slaid098/orch",
            "--url",
            "https://git.example.test",
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
        ]
    )

    assert code == 0
    assert calls == ["https://git.example.test"]


@pytest.mark.parametrize(
    ("env", "args"),
    [
        ({"FORGEJO_TOKEN": "secret"}, ["--repo", "slaid098/orch"]),
        ({"FORGEJO_URL": "https://git.example.test"}, ["--repo", "slaid098/orch"]),
        (
            {
                "FORGEJO_URL": "https://git.example.test",
                "FORGEJO_TOKEN": "secret",
            },
            [],
        ),
    ],
)
def test_forgejo_creation_requires_url_token_and_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    args: list[str],
) -> None:
    monkeypatch.delenv("FORGEJO_URL", raising=False)
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        create_issue,
        "create_forgejo_issue",
        lambda *_args, **_kwargs: pytest.fail("creation must not start"),
    )

    code = create_issue.main(
        [
            "--forge",
            "forgejo",
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            *args,
        ]
    )

    assert code == 1


def _fake_gh(url: str, commands: list[list[str]]):
    def run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=f"{url}\n", stderr="")

    return run


def _issue(number: int) -> Issue:
    return Issue(
        number=number,
        title=f"Issue {number}",
        body="Body",
        url=f"https://example.test/issues/{number}",
        author="slaid098",
        labels=(),
    )


def _github_dependency_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    url: str,
    commands: list[list[str]],
    blockers: list[Issue] | None = None,
    added: list[tuple[str, int, int, str]] | None = None,
    edits: list[tuple[str, int, dict[str, object]]] | None = None,
    add_error: Exception | None = None,
    edit_error: Exception | None = None,
) -> None:
    monkeypatch.setattr(create_issue.subprocess, "run", _fake_gh(url, commands))
    monkeypatch.setattr(
        create_issue.github,
        "resolve_token",
        lambda _owner, _configured: "token",
    )
    monkeypatch.setattr(
        create_issue.github,
        "list_blockers",
        lambda _repo, _number, _token: blockers or [],
    )

    def add(repo: str, number: int, blocker: int, token: str) -> None:
        if add_error is not None:
            raise add_error
        assert added is not None
        added.append((repo, number, blocker, token))

    def edit(
        repo: str,
        number: int,
        token: str,
        **kwargs: object,
    ) -> None:
        if edit_error is not None:
            raise edit_error
        assert edits is not None
        edits.append((repo, number, kwargs))

    monkeypatch.setattr(create_issue.github, "add_dependency", add)
    monkeypatch.setattr(create_issue.github, "edit_labels", edit)


def test_blocked_by_creates_on_hold_links_and_queues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands: list[list[str]] = []
    added: list[tuple[str, int, int, str]] = []
    edits: list[tuple[str, int, dict[str, object]]] = []
    _github_dependency_env(
        monkeypatch,
        url="https://example.test/issues/12",
        commands=commands,
        added=added,
        edits=edits,
    )

    code = create_issue.main(
        [
            "--repo",
            "slaid098/orch",
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            "--blocked-by",
            "3",
            "--blocked-by",
            "4",
        ]
    )

    assert code == 0
    assert "agent:hold" in commands[0]
    assert "agent:queued" not in commands[0]
    assert added == [
        ("slaid098/orch", 12, 3, "token"),
        ("slaid098/orch", 12, 4, "token"),
    ]
    assert edits == [
        (
            "slaid098/orch",
            12,
            {"add": ("agent:queued",), "remove": ("agent:hold",)},
        )
    ]
    assert "https://example.test/issues/12" in capsys.readouterr().out


def test_blocked_by_skips_an_existing_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands: list[list[str]] = []
    added: list[tuple[str, int, int, str]] = []
    edits: list[tuple[str, int, dict[str, object]]] = []
    _github_dependency_env(
        monkeypatch,
        url="https://example.test/issues/12",
        commands=commands,
        blockers=[_issue(3)],
        added=added,
        edits=edits,
    )

    code = create_issue.main(
        [
            "--repo",
            "slaid098/orch",
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            "--blocked-by",
            "3",
            "--blocked-by",
            "4",
        ]
    )

    assert code == 0
    assert added == [("slaid098/orch", 12, 4, "token")]
    assert edits[0][0:2] == ("slaid098/orch", 12)
    assert "https://example.test/issues/12" in capsys.readouterr().out


def test_explicit_hold_label_links_without_queueing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    added: list[tuple[str, int, int, str]] = []
    edits: list[tuple[str, int, dict[str, object]]] = []
    _github_dependency_env(
        monkeypatch,
        url="https://example.test/issues/12",
        commands=commands,
        added=added,
        edits=edits,
    )

    code = create_issue.main(
        [
            "--repo",
            "slaid098/orch",
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            "--label",
            "agent:hold",
            "--blocked-by",
            "3",
        ]
    )

    assert code == 0
    assert commands[0].count("--label") == 1
    assert "agent:hold" in commands[0]
    assert added == [("slaid098/orch", 12, 3, "token")]
    assert edits == []


def test_queued_label_with_blocked_by_is_rejected_before_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        create_issue.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("creation must not start"),
    )

    code = create_issue.main(
        [
            "--repo",
            "slaid098/orch",
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            "--label",
            "agent:queued",
            "--blocked-by",
            "3",
        ]
    )

    assert code == 2


def test_dependency_failure_keeps_hold_and_prints_the_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from orch.exceptions.github import GitHubError

    commands: list[list[str]] = []
    added: list[tuple[str, int, int, str]] = []
    edits: list[tuple[str, int, dict[str, object]]] = []
    _github_dependency_env(
        monkeypatch,
        url="https://example.test/issues/12",
        commands=commands,
        added=added,
        edits=edits,
        add_error=GitHubError("link failed"),
    )

    code = create_issue.main(
        [
            "--repo",
            "slaid098/orch",
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            "--blocked-by",
            "3",
        ]
    )

    assert code == 1
    assert edits == []
    assert "https://example.test/issues/12" in capsys.readouterr().out


def test_label_change_failure_keeps_hold_and_prints_the_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from orch.exceptions.github import GitHubError

    commands: list[list[str]] = []
    added: list[tuple[str, int, int, str]] = []
    edits: list[tuple[str, int, dict[str, object]]] = []
    _github_dependency_env(
        monkeypatch,
        url="https://example.test/issues/12",
        commands=commands,
        added=added,
        edits=edits,
        edit_error=GitHubError("labels failed"),
    )

    code = create_issue.main(
        [
            "--repo",
            "slaid098/orch",
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            "--blocked-by",
            "3",
        ]
    )

    assert code == 1
    assert added == [("slaid098/orch", 12, 3, "token")]
    assert "https://example.test/issues/12" in capsys.readouterr().out


def test_unparsed_issue_url_leaves_the_issue_on_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands: list[list[str]] = []
    added: list[tuple[str, int, int, str]] = []
    edits: list[tuple[str, int, dict[str, object]]] = []
    _github_dependency_env(
        monkeypatch,
        url="https://example.test/created",
        commands=commands,
        added=added,
        edits=edits,
    )

    code = create_issue.main(
        [
            "--repo",
            "slaid098/orch",
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            "--blocked-by",
            "3",
        ]
    )

    assert code == 1
    assert added == []
    assert edits == []
    assert "https://example.test/created" in capsys.readouterr().out


def test_validate_only_with_blocked_by_does_not_use_the_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        create_issue.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("gh must not run"),
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the forge must not be called")

    monkeypatch.setattr(create_issue.github, "list_blockers", fail)

    code = create_issue.main(
        [
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            "--blocked-by",
            "3",
            "--validate-only",
        ]
    )

    assert code == 0
    assert "OK:" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_blocked_by_rejects_a_non_positive_value(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(SystemExit) as error:
        create_issue.main(
            [
                "--title",
                "test(standards): validate issue creation",
                "--body-file",
                str(_body_file(tmp_path)),
                "--blocked-by",
                value,
                "--validate-only",
            ]
        )

    assert error.value.code == 2


def test_blocked_by_rejects_duplicate_arguments(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        create_issue.main(
            [
                "--title",
                "test(standards): validate issue creation",
                "--body-file",
                str(_body_file(tmp_path)),
                "--blocked-by",
                "3",
                "--blocked-by",
                "3",
                "--validate-only",
            ]
        )

    assert error.value.code == 2


@pytest.mark.parametrize(
    "blockers",
    [["3"], ["3", "4"]],
    ids=("one", "several"),
)
def test_forgejo_blocked_by_creates_on_hold_links_and_queues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    blockers: list[str],
) -> None:
    monkeypatch.setenv("FORGEJO_URL", "https://git.example.test")
    monkeypatch.setenv("FORGEJO_TOKEN", "secret")
    created: list[dict[str, object]] = []
    added: list[tuple[str, str, int, int, str]] = []
    edits: list[tuple[tuple[str, str, int, str], dict[str, object]]] = []
    monkeypatch.setattr(
        create_issue,
        "create_forgejo_issue",
        lambda url, repo, token, **kwargs: created.append(kwargs)
        or "https://git.example.test/slaid098/orch/issues/5",
    )
    monkeypatch.setattr(
        create_issue.forgejo,
        "list_blockers",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        create_issue.forgejo,
        "add_dependency",
        lambda *args: added.append(args),
    )
    monkeypatch.setattr(
        create_issue.forgejo,
        "edit_labels",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )

    blocker_args = [
        value
        for blocker in blockers
        for value in ("--blocked-by", blocker)
    ]
    code = create_issue.main(
        [
            "--forge",
            "forgejo",
            "--repo",
            "slaid098/orch",
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            *blocker_args,
        ]
    )

    assert code == 0
    assert created[0]["labels"] == ("agent:hold",)
    assert added == [
        ("https://git.example.test", "slaid098/orch", 5, int(blocker), "secret")
        for blocker in blockers
    ]
    assert edits == [
        (
            ("https://git.example.test", "slaid098/orch", 5, "secret"),
            {"add": ("agent:queued",), "remove": ("agent:hold",)},
        )
    ]
    assert "https://git.example.test/slaid098/orch/issues/5" in (
        capsys.readouterr().out
    )


def test_forgejo_blocked_by_skips_an_existing_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGEJO_URL", "https://git.example.test")
    monkeypatch.setenv("FORGEJO_TOKEN", "secret")
    added: list[tuple[str, str, int, int, str]] = []
    edits: list[tuple[tuple[str, str, int, str], dict[str, object]]] = []
    monkeypatch.setattr(
        create_issue,
        "create_forgejo_issue",
        lambda *_args, **_kwargs: (
            "https://git.example.test/slaid098/orch/issues/5"
        ),
    )
    monkeypatch.setattr(
        create_issue.forgejo,
        "list_blockers",
        lambda *_args: [_issue(3)],
    )
    monkeypatch.setattr(
        create_issue.forgejo,
        "add_dependency",
        lambda *args: added.append(args),
    )
    monkeypatch.setattr(
        create_issue.forgejo,
        "edit_labels",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )

    code = create_issue.main(
        [
            "--forge",
            "forgejo",
            "--repo",
            "slaid098/orch",
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            "--blocked-by",
            "3",
            "--blocked-by",
            "4",
        ]
    )

    assert code == 0
    assert added == [
        ("https://git.example.test", "slaid098/orch", 5, 4, "secret"),
    ]
    assert edits[0][0][2] == 5


def test_forgejo_dependency_failure_keeps_hold_and_prints_the_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from orch.exceptions.forgejo import ForgejoError

    monkeypatch.setenv("FORGEJO_URL", "https://git.example.test")
    monkeypatch.setenv("FORGEJO_TOKEN", "secret")
    edits: list[tuple[tuple[str, str, int, str], dict[str, object]]] = []
    monkeypatch.setattr(
        create_issue,
        "create_forgejo_issue",
        lambda *_args, **_kwargs: (
            "https://git.example.test/slaid098/orch/issues/5"
        ),
    )
    monkeypatch.setattr(
        create_issue.forgejo,
        "list_blockers",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        create_issue.forgejo,
        "add_dependency",
        lambda *_args: (_ for _ in ()).throw(ForgejoError("link failed")),
    )
    monkeypatch.setattr(
        create_issue.forgejo,
        "edit_labels",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )

    code = create_issue.main(
        [
            "--forge",
            "forgejo",
            "--repo",
            "slaid098/orch",
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            "--blocked-by",
            "3",
        ]
    )

    assert code == 1
    assert edits == []
    assert (
        "https://git.example.test/slaid098/orch/issues/5"
        in capsys.readouterr().out
    )


def test_blocked_by_without_repo_resolves_it_from_gh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    resolved: list[tuple[str, str | None]] = []
    added: list[tuple[str, int, int, str]] = []

    def run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:3] == ["repo", "view"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="slaid098/orch\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="https://example.test/issues/12\n",
            stderr="",
        )

    monkeypatch.setattr(create_issue.subprocess, "run", run)
    monkeypatch.setattr(
        create_issue.github,
        "resolve_token",
        lambda owner, configured: resolved.append((owner, configured)) or "token",
    )
    monkeypatch.setattr(
        create_issue.github,
        "list_blockers",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        create_issue.github,
        "add_dependency",
        lambda *args: added.append(args),
    )
    monkeypatch.setattr(
        create_issue.github,
        "edit_labels",
        lambda *_args, **_kwargs: None,
    )

    code = create_issue.main(
        [
            "--title",
            "test(standards): validate issue creation",
            "--body-file",
            str(_body_file(tmp_path)),
            "--blocked-by",
            "3",
        ]
    )

    assert code == 0
    assert commands[1][1:3] == ["repo", "view"]
    assert resolved == [("slaid098", None)]
    assert added == [("slaid098/orch", 12, 3, "token")]

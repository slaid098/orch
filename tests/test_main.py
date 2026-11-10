from __future__ import annotations

from pathlib import Path

import pytest

import main as orch_main
from orch.exceptions.config import ConfigError
from orch.exceptions.instance import InstanceLockError
from orch.schemas.config.types import Config, LabelsConfig, RepoConfig, WorkerConfig


def _config(tmp_path: Path, name: str = "orch") -> Config:
    return Config(
        repo=RepoConfig(owner="slaid098", name=name, path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
        name=name,
    )


@pytest.fixture(autouse=True)
def _passing_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch_main, "_preflight", lambda _config, **_kwargs: True)


def test_no_arguments_starts_the_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    calls: list[tuple[tuple[Config, ...], dict[str, str]]] = []

    monkeypatch.setattr(orch_main, "load_projects", lambda _path: (config,))
    monkeypatch.setattr(orch_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(orch_main, "initialize", lambda _config: "token")
    monkeypatch.setattr(
        orch_main,
        "run_daemon",
        lambda cfg, token: calls.append((cfg, token)) or 0,
    )

    assert orch_main.main([]) == 0
    assert calls == [((config,), {"slaid098/orch": "token"})]


def test_debug_mode_runs_one_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    calls: list[tuple[Config, str, int]] = []

    monkeypatch.setattr(orch_main, "load_projects", lambda _path: (config,))
    monkeypatch.setattr(orch_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(orch_main, "initialize", lambda _config: "token")
    monkeypatch.setattr(
        orch_main,
        "run_once",
        lambda cfg, token, issue: calls.append((cfg, token, issue)) or 0,
    )

    assert orch_main.main(["--issue", "7", "--once"]) == 0
    assert calls == [(config, "token", 7)]


def test_debug_mode_selects_a_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _config(tmp_path, "orch")
    second = _config(tmp_path, "antidetect-browser-mcp")
    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(orch_main, "load_projects", lambda _path: (first, second))
    monkeypatch.setattr(orch_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        orch_main,
        "initialize",
        lambda config: f"token-{config.name}",
    )
    monkeypatch.setattr(
        orch_main,
        "run_once",
        lambda config, token, issue: calls.append((token, issue)) or 0,
    )

    assert orch_main.main(["--issue", "7", "--once", "--project", "antidetect-browser-mcp"]) == 0
    assert calls == [("token-antidetect-browser-mcp", 7)]


def test_debug_mode_requires_a_project_when_several_are_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _config(tmp_path, "orch")
    second = _config(tmp_path, "antidetect-browser-mcp")

    monkeypatch.setattr(orch_main, "load_projects", lambda _path: (first, second))
    monkeypatch.setattr(orch_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        orch_main,
        "initialize",
        lambda _config: pytest.fail("initialize must not run without a project"),
    )

    assert orch_main.main(["--issue", "7", "--once"]) == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["--issue", "7"],
        ["--once"],
        ["--check", "--once"],
        ["--check", "--issue", "7", "--once"],
        ["--check", "--project", "orch"],
        ["--project", "orch"],
    ],
)
def test_debug_flags_must_be_used_together(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        orch_main.main(argv)

    assert excinfo.value.code == 2


def test_help_lists_only_debug_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        orch_main.main(["--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--issue" in out
    assert "--once" in out
    assert "--check" in out
    assert "--project" in out
    for command in ("status", "build-image", "logs"):
        assert command not in out


def test_config_error_returns_exit_code_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_path: Path) -> tuple[Config, ...]:
        raise ConfigError("missing")

    monkeypatch.setattr(orch_main, "load_projects", fail)

    assert orch_main.main([]) == 2


def test_initialization_error_returns_exit_code_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(orch_main, "load_projects", lambda _path: (config,))
    monkeypatch.setattr(orch_main, "setup_logging", lambda _config: None)

    def fail(_config: Config) -> str:
        raise RuntimeError("token missing")

    monkeypatch.setattr(orch_main, "initialize", fail)

    assert orch_main.main([]) == 1


def test_busy_instance_returns_exit_code_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(orch_main, "load_projects", lambda _path: (config,))
    monkeypatch.setattr(orch_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        orch_main,
        "initialize",
        lambda _config: pytest.fail("initialize must not run without the lock"),
    )

    def busy(*_args: object, **_kwargs: object) -> object:
        raise InstanceLockError("another instance holds the lock")

    monkeypatch.setattr(orch_main, "instance_lock", busy)

    assert orch_main.main([]) == 1


def test_check_runs_preflight_without_the_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    calls: list[tuple[tuple[Config, ...], bool]] = []

    monkeypatch.setattr(orch_main, "load_projects", lambda _path: (config,))
    monkeypatch.setattr(orch_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        orch_main,
        "_preflight",
        lambda cfg, *, notify_owner: calls.append((cfg, notify_owner)) or True,
    )
    monkeypatch.setattr(
        orch_main,
        "instance_lock",
        lambda *_args, **_kwargs: pytest.fail("--check must not take the lock"),
    )

    assert orch_main.main(["--check"]) == 0
    assert calls == [((config,), False)]


def test_check_failure_returns_exit_code_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)

    monkeypatch.setattr(orch_main, "load_projects", lambda _path: (config,))
    monkeypatch.setattr(orch_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        orch_main,
        "_preflight",
        lambda _config, **_kwargs: False,
    )

    assert orch_main.main(["--check"]) == 1


def test_failed_preflight_blocks_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)

    monkeypatch.setattr(orch_main, "load_projects", lambda _path: (config,))
    monkeypatch.setattr(orch_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(orch_main, "_preflight", lambda _config, **_kwargs: False)
    monkeypatch.setattr(
        orch_main,
        "initialize",
        lambda _config: pytest.fail("initialize must not run after a failed preflight"),
    )

    assert orch_main.main([]) == 1

from __future__ import annotations

from pathlib import Path

import pytest

import main as orch_main
from orch.exceptions.config import ConfigError
from orch.schemas.config.types import Config, LabelsConfig, RepoConfig, WorkerConfig


def _config(tmp_path: Path) -> Config:
    return Config(
        repo=RepoConfig(owner="slaid098", name="orch", path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
    )


def test_no_arguments_starts_the_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    calls: list[tuple[Config, str]] = []

    monkeypatch.setattr(orch_main, "load_config", lambda _path: config)
    monkeypatch.setattr(orch_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(orch_main, "initialize", lambda _config: "token")
    monkeypatch.setattr(
        orch_main,
        "run_daemon",
        lambda cfg, token: calls.append((cfg, token)) or 0,
    )

    assert orch_main.main([]) == 0
    assert calls == [(config, "token")]


def test_debug_mode_runs_one_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    calls: list[tuple[Config, str, int]] = []

    monkeypatch.setattr(orch_main, "load_config", lambda _path: config)
    monkeypatch.setattr(orch_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(orch_main, "initialize", lambda _config: "token")
    monkeypatch.setattr(
        orch_main,
        "run_once",
        lambda cfg, token, issue: calls.append((cfg, token, issue)) or 0,
    )

    assert orch_main.main(["--issue", "7", "--once"]) == 0
    assert calls == [(config, "token", 7)]


@pytest.mark.parametrize(
    "argv",
    [
        ["--issue", "7"],
        ["--once"],
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
    for command in ("status", "build-image", "logs"):
        assert command not in out


def test_config_error_returns_exit_code_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_path: Path) -> Config:
        raise ConfigError("missing")

    monkeypatch.setattr(orch_main, "load_config", fail)

    assert orch_main.main([]) == 2


def test_initialization_error_returns_exit_code_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(orch_main, "load_config", lambda _path: config)
    monkeypatch.setattr(orch_main, "setup_logging", lambda _config: None)

    def fail(_config: Config) -> str:
        raise RuntimeError("token missing")

    monkeypatch.setattr(orch_main, "initialize", fail)

    assert orch_main.main([]) == 1

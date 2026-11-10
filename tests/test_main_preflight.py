from __future__ import annotations

from pathlib import Path

import pytest

import main as orch_main
from orch.schemas.config.types import Config, LabelsConfig, RepoConfig, WorkerConfig


def _config(tmp_path: Path, name: str) -> Config:
    return Config(
        repo=RepoConfig(owner="slaid098", name=name, path=tmp_path),
        labels=LabelsConfig(),
        worker=WorkerConfig(test_commands=("pytest",)),
        data_dir=tmp_path / "data",
        name=name,
    )


def test_preflight_passes_when_every_project_is_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orch_main.preflight, "check", lambda _config: [])

    assert orch_main._preflight((_config(tmp_path, "orch"),), notify_owner=False)


def test_preflight_prefixes_problems_with_the_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _config(tmp_path, "orch")
    second = _config(tmp_path, "antidetect-browser-mcp")
    monkeypatch.setattr(
        orch_main.preflight,
        "check",
        lambda config: [f"{config.name} is broken"],
    )

    assert not orch_main._preflight((first, second), notify_owner=False)


def test_preflight_notifies_the_owner_from_the_first_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _config(tmp_path, "orch")
    second = _config(tmp_path, "antidetect-browser-mcp")
    monkeypatch.setattr(
        orch_main.preflight,
        "check",
        lambda config: [f"{config.name} is broken"],
    )
    notified: list[Config] = []
    monkeypatch.setattr(
        orch_main.notify,
        "notify_error",
        lambda config, **_kwargs: notified.append(config),
    )

    assert not orch_main._preflight((first, second), notify_owner=True)
    assert notified == [first]

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orch.exceptions.git import GitError
from orch.services.git import service as git


def _proc(
    stdout: str = "",
    stderr: str = "",
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], code, stdout, stderr)


def test_refresh_mirror_initializes_and_fetches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _proc("sha\n")

    monkeypatch.setattr(git.subprocess, "run", fake_run)

    mirror = git.refresh_mirror(
        tmp_path,
        "slaid098",
        "orch",
        "main",
        "secret",
    )

    assert mirror == tmp_path / "mirrors" / "slaid098__orch.git"
    assert calls[0][:3] == ["git", "init", "--bare"]
    assert any("fetch" in call for call in calls)
    assert any("refs/heads/main" in " ".join(call) for call in calls)


def test_refresh_mirror_reuses_existing_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror = tmp_path / "mirrors" / "slaid098__orch.git"
    mirror.mkdir(parents=True)
    (mirror / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr(
        git,
        "_run_git",
        lambda args, **_kwargs: calls.append(args) or "",
    )

    git.refresh_mirror(tmp_path, "slaid098", "orch", "main", "secret")

    assert all(call[:2] != ["init", "--bare"] for call in calls)
    assert calls[0][:3] == ["--git-dir", str(mirror), "symbolic-ref"]


def test_clone_source_replaces_existing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "source"
    destination.mkdir()
    removed: list[Path] = []
    calls: list[list[str]] = []

    monkeypatch.setattr(git, "_remove_tree", lambda path: removed.append(path))
    monkeypatch.setattr(
        git,
        "_run_git",
        lambda args, **_kwargs: calls.append(args) or "",
    )

    result = git.clone_source(tmp_path / "mirror.git", "main", destination)

    assert result == destination
    assert removed == [destination]
    assert calls[0][:2] == ["clone", "--no-hardlinks"]
    assert calls[1] == ["checkout", "main"]


def test_cleanup_source_ignores_missing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_path: Path) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(git, "_remove_tree", fail)
    git.cleanup_source(Path("missing"))


def test_cleanup_source_warns_on_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_path: Path) -> None:
        raise OSError("locked")

    monkeypatch.setattr(git, "_remove_tree", fail)
    git.cleanup_workspace(Path("locked"))


@pytest.mark.parametrize("size", [0, 1])
def test_import_bundle_rejects_missing_or_empty(
    tmp_path: Path,
    size: int,
) -> None:
    bundle = tmp_path / "result.bundle"
    if size:
        bundle.write_text("", encoding="utf-8")

    with pytest.raises(GitError, match="missing or empty"):
        git.import_bundle(bundle, "branch", tmp_path)


def test_import_bundle_rejects_missing_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "result.bundle"
    bundle.write_text("bundle", encoding="utf-8")
    monkeypatch.setattr(git, "_run_git", lambda *_args, **_kwargs: "other")

    with pytest.raises(GitError, match="does not contain"):
        git.import_bundle(bundle, "branch", tmp_path)


def test_import_bundle_imports_matching_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "result.bundle"
    bundle.write_text("bundle", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> str:
        calls.append(args)
        if args[:2] == ["bundle", "list-heads"]:
            return "refs/heads/branch other"
        return ""

    monkeypatch.setattr(git, "_run_git", fake_run)

    bare = git.import_bundle(bundle, "branch", tmp_path)

    assert bare == tmp_path / "push.git"
    assert any("fetch" in call for call in calls)


def test_push_branch_reports_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise GitError("rejected")

    monkeypatch.setattr(git, "_run_git", fail)

    with pytest.raises(GitError, match="Could not push branch"):
        git.push_branch(tmp_path / "push.git", "branch", "remote", "token")


def test_authenticated_remote_contains_token() -> None:
    remote = git.authenticated_remote("slaid098", "orch", "secret")

    assert remote == "https://x-access-token:secret@github.com/slaid098/orch.git"


def test_run_git_returns_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return _proc("output\n")

    monkeypatch.setattr(git.subprocess, "run", fake_run)

    result = git._run_git(["status"], cwd=Path("."), timeout=5)

    assert result == "output\n"
    assert calls[0]["timeout"] == 5


def test_run_git_redacts_secret_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        git.subprocess,
        "run",
        lambda *_args, **_kwargs: _proc(stderr="token=secret", code=1),
    )

    with pytest.raises(GitError) as excinfo:
        git._run_git(["fetch", "secret"], cwd=Path("."), secret="secret")

    assert "***" in str(excinfo.value)
    assert "secret" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (subprocess.TimeoutExpired(cmd=["git"], timeout=1), "timed out"),
        (OSError("missing"), "Could not execute"),
    ],
)
def test_run_git_translates_process_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    message: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(git.subprocess, "run", fail)

    with pytest.raises(GitError, match=message):
        git._run_git(["status"], cwd=Path("."))


def test_redaction_helpers() -> None:
    assert git._redact("token", None) == "token"
    assert git._redact("token", "token") == "***"
    assert git._redact_args(["a", "token"], None) == ["a", "token"]
    assert git._redact_args(["a", "token"], "token") == ["a", "***"]

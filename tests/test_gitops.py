from __future__ import annotations

import subprocess
from pathlib import Path

from orch.gitops import (
    cleanup_source,
    clone_source,
    import_bundle,
    push_branch,
)


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_clone_bundle_and_push_round_trip(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    mirror = tmp_path / "mirror.git"
    source = tmp_path / "source"
    attempt = tmp_path / "attempt"

    _git("init", "--bare", str(origin), cwd=tmp_path)
    _git("init", str(work), cwd=tmp_path)
    _git("config", "user.name", "orch", cwd=work)
    _git("config", "user.email", "orch@example.invalid", cwd=work)
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "README.md", cwd=work)
    _git("commit", "-m", "chore: initial", cwd=work)
    _git("branch", "-M", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    _git("push", "origin", "main", cwd=work)

    _git("clone", "--bare", str(origin), str(mirror), cwd=tmp_path)
    _git("--git-dir", str(mirror), "symbolic-ref", "HEAD", "refs/heads/main", cwd=mirror)

    clone_source(mirror, "main", source)
    _git("config", "user.name", "orch", cwd=source)
    _git("config", "user.email", "orch@example.invalid", cwd=source)
    _git("switch", "-c", "agent/1", cwd=source)
    (source / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git("add", "feature.txt", cwd=source)
    _git("commit", "-m", "feat(test): add feature", cwd=source)
    bundle = source / "result.bundle"
    _git("bundle", "create", str(bundle), "refs/heads/agent/1", cwd=source)

    attempt.mkdir()
    bare = import_bundle(bundle, "agent/1", attempt)
    push_branch(bare, "agent/1", str(origin), "not-a-real-token")

    remote_sha = _git("--git-dir", str(origin), "rev-parse", "refs/heads/agent/1", cwd=origin)
    local_sha = _git("rev-parse", "HEAD", cwd=source)
    assert remote_sha == local_sha

    cleanup_source(source)
    assert not source.exists()
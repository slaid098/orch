"""Create a forge issue that satisfies the orch workflow policy."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

from loguru import logger

from orch.exceptions.standards import StandardsError
from orch.services import forgejo, github
from orch.services.forgejo import create_issue as create_forgejo_issue
from orch.services.standards import validate_title
from orch.services.standards.policy import validate_issue_body

_QUEUED_LABEL = "agent:queued"
_HOLD_LABEL = "agent:hold"
_ISSUE_URL_RE = re.compile(r"/issues/(\d+)/?$")


def main(argv: list[str] | None = None) -> int:
    """Validate and create one issue on the selected forge.

    Args:
        argv: Optional argument list, mainly for tests.

    Returns:
        Process exit code: 0 on success, 1 on a forge failure, 2 on invalid
        input.
    """
    args = _parse_args(argv)
    url = ""
    try:
        title = validate_title(args.title)
        body = _read_body(Path(args.body_file))
        validate_issue_body(body)
        if args.validate_only:
            print(f"OK: {title}")
            return 0
        _reject_queued_dependency(args)
        url = _create_issue(args, title, body)
        if args.blocked_by:
            _link_dependencies(args, url)
    except StandardsError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Issue input does not satisfy the workflow policy: {}", ex
        )
        return 2
    except (OSError, RuntimeError) as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not create the issue"
        )
        if url:
            print(url)
        return 1
    else:
        print(url)
        return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--forge", choices=("github", "forgejo"), default="github")
    parser.add_argument("--repo", default=None)
    parser.add_argument("--url", default=None, help="Forgejo base URL")
    parser.add_argument("--label", action="append", default=None)
    parser.add_argument(
        "--blocked-by",
        action="append",
        type=_positive_int,
        default=[],
        metavar="N",
        help="Issue number that must close first; repeat for several blockers",
    )
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    if len(args.blocked_by) != len(set(args.blocked_by)):
        parser.error("--blocked-by must not repeat an issue number")
    return args


def _positive_int(value: str) -> int:
    """Parse a positive issue number for ``--blocked-by``."""
    try:
        number = int(value)
    except ValueError as ex:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an issue number"
        ) from ex
    if number <= 0:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a positive issue number"
        )
    return number


def _read_body(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _reject_queued_dependency(args: argparse.Namespace) -> None:
    """Refuse to create a dependent issue directly in the queue."""
    if args.blocked_by and _QUEUED_LABEL in (args.label or ()):
        raise StandardsError(
            "--label agent:queued cannot be combined with --blocked-by: "
            "the issue must stay on agent:hold until its dependencies are linked"
        )


def _initial_labels(args: argparse.Namespace) -> tuple[str, ...]:
    """Return the labels for the new issue before dependencies are linked."""
    if args.label:
        return tuple(args.label)
    if args.blocked_by:
        return (_HOLD_LABEL,)
    return (_QUEUED_LABEL,)


def _should_queue(args: argparse.Namespace) -> bool:
    """Return whether the default hold label must be replaced by the queue."""
    return bool(args.blocked_by) and not args.label


def _create_issue(
    args: argparse.Namespace,
    title: str,
    body: str,
) -> str:
    if args.forge == "forgejo":
        return _create_forgejo_issue(args, title, body)
    return _create_github_issue(args, title)


def _forgejo_context(args: argparse.Namespace) -> tuple[str, str, str]:
    """Return the Forgejo URL, repository, and token for this invocation."""
    if not args.repo:
        raise RuntimeError("--repo is required when --forge is forgejo")
    url = (args.url or os.environ.get("FORGEJO_URL") or "").strip()
    if not url:
        raise RuntimeError("Set FORGEJO_URL or pass --url when --forge is forgejo")
    token = (os.environ.get("FORGEJO_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Set FORGEJO_TOKEN to create a Forgejo issue")
    return url, args.repo, token


def _create_forgejo_issue(
    args: argparse.Namespace,
    title: str,
    body: str,
) -> str:
    url, repo, token = _forgejo_context(args)
    return create_forgejo_issue(
        url,
        repo,
        token,
        title=title,
        body=body,
        labels=_initial_labels(args),
    )


def _create_github_issue(args: argparse.Namespace, title: str) -> str:
    command = [
        "gh",
        "issue",
        "create",
        "--title",
        title,
        "--body-file",
        args.body_file,
    ]
    if args.repo:
        command.extend(["--repo", args.repo])
    for label in _initial_labels(args):
        command.extend(["--label", label])

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(details or "gh issue create failed")
    url = (result.stdout or "").strip()
    if not url:
        raise RuntimeError("gh issue create returned no issue URL")
    logger.info("Created issue: {}", url)
    return url


def _github_repo(args: argparse.Namespace) -> str:
    """Resolve the target repository when ``--repo`` is omitted."""
    if args.repo:
        return args.repo
    command = [
        "gh",
        "repo",
        "view",
        "--json",
        "nameWithOwner",
        "--jq",
        ".nameWithOwner",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(details or "gh repo view failed")
    repo = (result.stdout or "").strip()
    if not repo:
        raise RuntimeError("gh repo view returned no repository name")
    return repo


def _issue_number(url: str) -> int:
    """Return the issue number encoded in a forge web URL."""
    match = _ISSUE_URL_RE.search(url.strip())
    if match is None:
        raise RuntimeError(f"Could not determine the issue number from {url!r}")
    return int(match.group(1))


def _link_dependencies(args: argparse.Namespace, url: str) -> None:
    """Link every blocked-by edge before the issue may enter the queue."""
    number = _issue_number(url)
    if args.forge == "forgejo":
        _link_forgejo_dependencies(args, number)
    else:
        _link_github_dependencies(args, number)


def _link_forgejo_dependencies(args: argparse.Namespace, number: int) -> None:
    url, repo, token = _forgejo_context(args)
    existing = {
        issue.number for issue in forgejo.list_blockers(url, repo, number, token)
    }
    for blocker in args.blocked_by:
        if blocker in existing:
            logger.info("Issue #{} is already blocked by #{}", number, blocker)
            continue
        forgejo.add_dependency(url, repo, number, blocker, token)
    if _should_queue(args):
        forgejo.edit_labels(
            url,
            repo,
            number,
            token,
            add=(_QUEUED_LABEL,),
            remove=(_HOLD_LABEL,),
        )


def _link_github_dependencies(args: argparse.Namespace, number: int) -> None:
    repo = _github_repo(args)
    token = github.resolve_token(
        repo.split("/", 1)[0],
        os.environ.get("GH_TOKEN"),
    )
    existing = {issue.number for issue in github.list_blockers(repo, number, token)}
    for blocker in args.blocked_by:
        if blocker in existing:
            logger.info("Issue #{} is already blocked by #{}", number, blocker)
            continue
        github.add_dependency(repo, number, blocker, token)
    if _should_queue(args):
        github.edit_labels(
            repo,
            number,
            token,
            add=(_QUEUED_LABEL,),
            remove=(_HOLD_LABEL,),
        )


if __name__ == "__main__":
    raise SystemExit(main())

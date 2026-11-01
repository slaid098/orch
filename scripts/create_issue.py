"""Create a GitHub issue that satisfies the orch workflow policy."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from loguru import logger

from orch.exceptions.standards import StandardsError
from orch.services.standards import validate_title
from orch.services.standards.policy import validate_issue_body


def main(argv: list[str] | None = None) -> int:
    """Validate and create one GitHub issue.

    Args:
        argv: Optional argument list, mainly for tests.

    Returns:
        Process exit code: 0 on success, 1 on GitHub failure, 2 on invalid input.
    """
    args = _parse_args(argv)
    try:
        title = validate_title(args.title)
        validate_issue_body(_read_body(Path(args.body_file)))
        if args.validate_only:
            print(f"OK: {title}")
            return 0
        url = _create_issue(args, title)
    except StandardsError as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Issue input does not satisfy the workflow policy"
        )
        return 2
    except (OSError, RuntimeError) as ex:
        logger.bind(error=ex, error_type=type(ex).__name__).error(
            "Could not create the issue"
        )
        return 1
    else:
        print(url)
        return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--label", action="append", default=None)
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args(argv)


def _read_body(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _create_issue(args: argparse.Namespace, title: str) -> str:
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
    for label in args.label or ["agent:queued"]:
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


if __name__ == "__main__":
    raise SystemExit(main())

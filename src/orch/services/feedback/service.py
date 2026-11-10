"""Live issue-comment feedback for a running worker."""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

from orch.config.logger import get_logger
from orch.constants.standards import BOT_MARKER
from orch.exceptions.forge import ForgeError
from orch.schemas.config.types import Config
from orch.schemas.feedback.types import IncomingComment
from orch.schemas.github.types import Comment, Issue
from orch.services import forge

log = get_logger(component="feedback")

_MAX_INITIAL_CHARS = 6000
_MAX_COMMENT_CHARS = 4000


def _is_authorized(config: Config, issue: Issue, comment: Comment) -> bool:
    allowed = set(config.feedback.allowed_users)
    allowed.add(issue.author)
    return comment.author in allowed and bool(comment.body.strip())


def _is_bot(comment: Comment) -> bool:
    return BOT_MARKER in comment.body or comment.author.endswith("[bot]")


def _select(
    comments: list[Comment],
    config: Config,
    issue: Issue,
    *,
    include_orch: bool = False,
) -> list[Comment]:
    selected: list[Comment] = []
    for comment in comments:
        if _is_bot(comment):
            if (
                include_orch
                and BOT_MARKER in comment.body
                and (
                    comment.author == config.repo.owner
                    or _is_authorized(config, issue, comment)
                )
            ):
                selected.append(comment)
            continue
        if _is_authorized(config, issue, comment):
            selected.append(comment)
    return selected


def format_comments(comments: list[Comment]) -> str:
    """Render pre-start comments as a compact prompt section."""
    if not comments:
        return ""
    lines = ["## Комментарии к issue до старта", ""]
    blocks: list[str] = []
    total = 0
    for comment in reversed(comments[-20:]):
        body = comment.body.strip()
        if len(body) > _MAX_COMMENT_CHARS:
            body = body[:_MAX_COMMENT_CHARS] + "\n…"
        block = f"- @{comment.author} ({comment.created_at}):\n{body}\n"
        if total + len(block) > _MAX_INITIAL_CHARS:
            lines.append("- … более ранние комментарии обрезаны")
            break
        blocks.append(block)
        total += len(block)
    lines.extend(reversed(blocks))
    return "\n".join(lines).strip() + "\n\n"


class CommentFeed:
    """Poll comments for one active issue and buffer them for injection."""

    def __init__(
        self,
        config: Config,
        issue: Issue,
        token: str,
        *,
        poll_seconds: int | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._config = config
        self._issue = issue
        self._token = token
        self._poll_seconds = poll_seconds or config.feedback.poll_seconds
        self._state_path = state_path
        self._queue: queue.Queue[IncomingComment] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_id = 0
        self._since: str | None = None
        self._cancelled = threading.Event()
        self._cancel_reason = ""
        self._seen: set[int] = set()
        self._load_state()

    @property
    def cancelled(self) -> bool:
        """Return whether the issue was closed while the feed was active."""
        return self._cancelled.is_set()

    @property
    def cancel_reason(self) -> str:
        """Return the reason the feed stopped the run."""
        return self._cancel_reason

    @property
    def last_seen_comment_id(self) -> int:
        """Return the highest comment id observed so far."""
        return self._last_id

    def initial_context(self, *, include_orch: bool = False) -> str:
        """Fetch comments made before the worker starts and format them.

        Args:
            include_orch: Include orch's own marked comments, which carry review
                findings and terminal failure context for a revision.
        """
        try:
            comments = forge.list_issue_comments(
                self._config,
                self._issue.number,
                self._token,
            )
        except ForgeError as ex:
            log.bind(
                issue=self._issue.number,
                error=ex,
                error_type=type(ex).__name__,
            ).warning("Could not load pre-start issue comments")
            return ""
        else:
            self._remember(comments)
            return format_comments(
                _select(
                    comments,
                    self._config,
                    self._issue,
                    include_orch=include_orch,
                )
            )

    def start(self) -> None:
        """Start polling in a daemon thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"orch-feedback-{self._issue.number}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop polling and wait briefly for the thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self._poll_seconds + 1.0))
            self._thread = None

    def drain(self) -> list[IncomingComment]:
        """Return and clear all buffered comments."""
        items: list[IncomingComment] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                return items

    def _run(self) -> None:
        self._poll_once()
        while not self._stop.wait(self._poll_seconds):
            self._poll_once()

    def _poll_once(self) -> None:
        if self._cancelled.is_set():
            return
        try:
            is_open = forge.issue_is_open(
                self._config,
                self._issue.number,
                self._token,
            )
        except ForgeError as ex:
            log.bind(
                issue=self._issue.number,
                error=ex,
                error_type=type(ex).__name__,
            ).warning("Could not poll issue state")
        else:
            if not is_open:
                self._cancel("issue closed")
                return

        try:
            comments = forge.list_issue_comments(
                self._config,
                self._issue.number,
                self._token,
                since=self._since,
            )
        except ForgeError as ex:
            log.bind(
                issue=self._issue.number,
                error=ex,
                error_type=type(ex).__name__,
            ).warning("Could not poll issue comments")
            return
        else:
            selected = _select(comments, self._config, self._issue)
            for comment in selected:
                with self._lock:
                    if comment.id in self._seen or comment.id <= self._last_id:
                        continue
                    self._seen.add(comment.id)
                self._queue.put(
                    IncomingComment(
                        id=comment.id,
                        author=comment.author,
                        body=comment.body.strip(),
                        created_at=comment.created_at,
                    )
                )
                log.info(
                    "Queued issue comment {} from @{}",
                    comment.id,
                    comment.author,
                )
            self._remember(comments)

    def _remember(self, comments: list[Comment]) -> None:
        with self._lock:
            for comment in comments:
                self._seen.add(comment.id)
                self._last_id = max(self._last_id, comment.id)
                if comment.created_at:
                    self._since = comment.created_at
        self._persist()

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as ex:
            log.bind(
                issue=self._issue.number,
                error_type=type(ex).__name__,
            ).warning("Could not read the feedback cursor; starting from scratch")
            return
        last_id = payload.get("last_seen_comment_id")
        if isinstance(last_id, int) and last_id >= 0:
            self._last_id = last_id
        since = payload.get("since")
        if isinstance(since, str) and since:
            self._since = since

    def _persist(self) -> None:
        if self._state_path is None:
            return
        payload = json.dumps(
            {
                "last_seen_comment_id": self._last_id,
                "since": self._since,
            },
            ensure_ascii=False,
        )
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_suffix(".tmp")
            temporary.write_text(payload + "\n", encoding="utf-8")
            temporary.replace(self._state_path)
        except OSError as ex:
            log.bind(
                issue=self._issue.number,
                error_type=type(ex).__name__,
            ).warning("Could not persist the feedback cursor")

    def _cancel(self, reason: str) -> None:
        if self._cancelled.is_set():
            return
        self._cancel_reason = reason
        self._cancelled.set()
        log.info("Issue #{} feedback stopped: {}", self._issue.number, reason)

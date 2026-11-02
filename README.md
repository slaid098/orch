# orch

Minimal GitHub issue to pull request orchestrator for containerized coding agents.

`orch` watches GitHub issues labelled `agent:queued`, runs a worker container
with Claude Code, validates a deterministic commit, runs the repository tests,
pushes the branch, and opens a pull request. A human reviews and merges it.

## What it does

```text
GitHub issue with agent:queued
  -> orch claims it (agent:running)
  -> clones origin/main once for the issue
  -> worker implements the issue in a persistent /work volume
  -> authorized issue comments are injected into the running worker
  -> closing the issue cancels the attempt before a pull request
  -> orch squashes all worker changes into one conventional commit
  -> orch runs the configured tests
  -> orch pushes type/scope/issue-N-description
  -> orch opens a standardized pull request
  -> a human merges it
```

The worker has no GitHub token. It cannot push or create pull requests. Only the
orchestrator process touches GitHub.

## Live feedback and cancellation

While an issue is being worked on, orch polls its comments. Only comments from
the issue author and logins listed in `feedback.allowed_users` are accepted.
Orch's own comments and comments from bot accounts are ignored.

Comments made before the worker starts are included in the initial prompt.
Later comments are injected into the same Claude Code `stream-json` session as
new user messages, without restarting the worker. The last seen comment id is
persisted in `<run-dir>/feedback.json`.

Closing the issue is the cancellation signal. The comment poller notices it,
stops the worker container, and abandons the attempt without retrying and
without opening a pull request. The persistent workspace remains under
`app_data/workspaces/<owner>__<repo>/issue-N` and is removed by retention after
`retention_days`. Reopening the issue and adding `agent:queued` again resumes
from the same workspace.

## Telegram status

When `telegram.enabled` is true, orch sends at most three one-way messages per
issue: `Orch принято`, `Orch завершено` with the PR URL and duration, and
`Orch нужен человек` with the reason. Each message includes queue and active
counts. The bot only sends messages; it does not read commands, buttons, or
chat updates. A Telegram failure is logged and never interrupts the pipeline.

The bot token comes from `telegram.token_file` when configured, otherwise from
`TELEGRAM_BOT_TOKEN`. For a forum, set `telegram.chat_id` to the supergroup and
`telegram.message_thread_id` to the single `Orch` topic. Keep the token out of
`orch.yaml`.

## Watchdog

When `watchdog.enabled` is true, cheap deterministic triggers (silence, repeated
tool calls, repeated errors) ask Clef to classify the run as `working`,
`waiting_external`, `looping`, `stuck`, or `needs_human`. Clef only classifies;
the code applies a fixed policy. Before 15 minutes it never intervenes. It may
send at most two nudges into the same session, with a 10-minute cooldown, then it
stops the run and marks the issue `agent:needs-human`. Low-confidence answers
and classifier failures are logged and ignored, and the 90-minute attempt
timeout still applies. The watchdog never restarts an attempt automatically.

Clef uses `OLLAMA_API_KEY` from the environment, falling back to the
`ollama-cloud` key in the local OpenCode `auth.json`.

If an attempt hangs, crashes, or produces no pull request, the worker container
is replaced but the `/work` workspace is kept. Before it stops the container,
orch writes `checkpoint.patch`, `checkpoint.status`, and
`checkpoint-untracked.tar.gz` into the attempt directory, so tracked and
untracked work can be recovered even if the workspace is damaged. The next
attempt continues in the same directory with a fresh context and a short
handoff. The workspace is deleted only after a pull request is created.

## Requirements

- Python 3.14
- `uv`
- Docker
- `gh` authenticated as the repository owner
- Claude Code inside the worker image
- An Ollama endpoint reachable from Docker at `host.docker.internal:11434`

## Setup

```powershell
uv sync
Copy-Item orch.yaml.example orch.yaml
docker build -f docker/agent.Dockerfile -t orch-agent:latest .
```

Create the issue on GitHub and add the `agent:queued` label. Run the daemon:

```powershell
$env:GH_TOKEN = (gh auth token --user slaid098)
uv run python main.py
```

Orch uses `GH_TOKEN` or the `gh` keyring for the repository owner. It
deliberately ignores an ambient `GITHUB_TOKEN`, which often belongs to another
account on a developer machine.

The daemon runs at most `max_workers` containers (default 2, maximum 3) and
waits for active workers on shutdown. On startup it removes orphan containers
from a previous process and re-queues issues left in `agent:running`.

## Live smoke test

Verify the complete issue-to-PR path once, on a real worker container, without
starting the daemon. Create a small issue with a valid conventional title (for
example `docs(smoke): verify the pipeline`), note its number, and run:

```powershell
uv run python main.py --issue N --once
```

`--issue` and `--once` must be used together. The command selects the issue
directly (the `agent:queued` label is not required), claims it
(`agent:running`), runs the worker, validates and squashes the commit, runs
`worker.test_commands`, pushes the branch, opens the pull request, and exits.
It processes exactly one issue and never polls.

- Exit code `0`: the pull request is open. The log prints its URL and the run
  directory; the issue is labelled `agent:pr-open` and commented.
- Exit code `1`: the issue is labelled `agent:needs-human`. Inspect the error,
  `result.json`, and the worker logs in the printed run directory under
  `app_data/runs/<owner>__<repo>/issue-N/<run-id>/`.

A live run needs Docker, an authenticated `gh`, and the worker image built in
[Setup](#setup). It makes up to `worker.max_attempts` attempts, each bounded by
`worker.timeout_minutes`, so it can take a long time and consumes model quota.
Close the smoke-test pull request, branch, and issue when done.

## Labels

- `agent:queued` — trigger for the daemon.
- `agent:running` — an attempt or retry loop is active.
- `agent:pr-open` — orch opened a pull request.
- `agent:needs-human` — the issue needs manual intervention.

## Standards

The issue title is the single source of truth:

- Issue title: `type(scope): English description`, mandatory scope, at most
  72 characters.
- Branch: `type/scope/issue-N-kebab-description`.
- Commit title and pull-request title: exactly the issue title.
- One commit; no commit body, GPG signature, or `Signed-off-by`.
- Pull-request body uses four Russian sections and ends with `Closes #N`.

The executable source of workflow rules is
`src/orch/services/standards/policy.py`. Architecture and code rules live in the
external `audit` skill; do not copy them here.

## Configuration

Copy `orch.yaml.example` to `orch.yaml` and adjust paths, labels, commands, and
the model. `orch.yaml` and `app_data/` are ignored by git.

The base branch is fetched into a local bare mirror under `data_dir`; the worker
never receives a GitHub token and never fetches from GitHub directly.

Feature flags:

- `telegram.enabled` — one-way status messages; requires `chat_id` and a token.
- `feedback.enabled` and `feedback.live_comments` — poll issue comments and
  inject them into the running session. `live_comments: false` keeps the older
  one-shot worker.
- `watchdog.enabled` — Clef-based hang and loop classification; requires an
  Ollama Cloud key.

### Context window

`worker.autocompact` pins the Claude Code context window. The default `200k`
writes the same value to the CLI flag, the container environment, and
`~/.claude/settings.json` inside the worker, so every agent that runs there
inherits it. `auto` leaves the window to Claude Code.

`retention_days` (default 7) removes old run directories and rotated logs, so
local artifacts never grow without bound. Rotation happens at daemon startup
and after every finished issue; old files are deleted, not archived.

## Security model

- The worker runs in a disposable container.
- `/src` is a read-only clone of the issue base revision.
- `/work` is the persistent workspace, not readable by the orchestrator token.
- The worker has no GitHub token and cannot push.
- Only the issue author and explicitly allowed users can inject comments; bot
  comments and orch's own comments are filtered out. Comment text is still
  treated as untrusted task context.
- The Telegram token stays in the orchestrator and is never mounted into the
  worker.
- The worker writes a git bundle and handoff to `/out`.
- The orchestrator validates the bundle, creates the commit, pushes the branch,
  and opens the pull request.
- Logs stay in the local `app_data` directory and are pruned after
  `retention_days`.

## Not yet included

- Reviewer agent and automatic review loop
- Auto-merge by the daemon
- Multi-repository support

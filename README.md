# orch

Minimal GitHub issue to pull request orchestrator for containerized coding agents.

`orch` watches GitHub issues labelled `agent:queued`, runs a worker container
with Claude Code, validates a deterministic commit, runs the repository tests,
pushes the branch, and opens a pull request. It then waits for GitHub checks,
runs a separate read-only reviewer container, and squash-merges the pull request
into `main` only when every check is green and the reviewer returns no findings.

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
  -> orch waits for GitHub checks
  -> failed checks return control to the worker, never to the reviewer
  -> a separate read-only reviewer returns an empty findings array or blocks
  -> any finding consumes one shared worker attempt and goes back to the worker
  -> an empty findings array triggers a squash merge into main
  -> three total worker attempts or three review rounds exhaust to needs-human
```

The worker has no GitHub token. It cannot push or create pull requests. The
reviewer has no GitHub token and only the `Read`, `Grep`, and `Glob` tools. Only
the orchestrator process writes, comments, and merges.

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

## Reviewer and auto-merge

After the worker opens or updates a pull request, orch force-updates the same
deterministic branch and polls the checks for the newly pushed commit until they
finish. A failed check, a merge conflict, or a timeout is reported to the worker
as the next attempt's previous failure. The reviewer is never started for a red
pull request or for a stale commit.

The reviewer runs in a separate container with the worker checkout mounted
read-only at `/review`, no GitHub token, and only `Read`, `Grep`, and `Glob`
available. It receives the issue, the diff, and the green checks summary. Its
result is constrained by a JSON schema:

```json
{"findings": []}
```

An empty array approves. Any finding blocks the merge; there is no severity
field and no optional finding. The orchestrator posts every finding to the
issue and passes the complete list into the next worker attempt.

Worker attempts and reviewer rounds share one budget. `worker.max_attempts`
defaults to 3 and `reviewer.max_rounds` defaults to 3. A failed worker attempt, a
failed GitHub check, and a reviewer rejection all consume the next attempt. A
technical reviewer failure (invalid JSON after one repair launch, timeout, or
container failure) does not consume a worker attempt: the issue is parked
immediately in `agent:needs-human`.

A successful review is followed by a squash merge into `main` only. The
repository base branch must be `main`; orch refuses to auto-merge into any other
branch. A merge failure is also parked for a human. The persistent workspace is
kept until a merge succeeds, a human is needed, or the reviewer is disabled.

If a run starts and finds an already-open PR for the same deterministic branch,
it leaves that PR for a human (`agent:pr-open`) instead of starting another
worker. This avoids guessing at a workspace or diff that may have been cleaned
up after the previous run; orch never auto-merges such a PR.

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
handoff. The workspace is deleted after a successful merge, or when the reviewer
is disabled and the PR is left for manual review. When orch parks an issue in
`agent:needs-human`, the workspace is kept so a human can inspect the failed
checkout; it can be removed manually after the issue is resolved.

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
`worker.test_commands`, pushes the branch, opens the pull request, waits for
GitHub checks, runs the reviewer, and merges when approved. It processes
exactly one issue and never polls.

- Exit code `0`: the pull request was merged, or it is open for manual review
  because `reviewer.enabled` is false. The log prints its URL and the run
  directory; the issue is commented.
- Exit code `1`: the issue is labelled `agent:needs-human`. Inspect the error,
  `result.json`, and the worker and reviewer logs in the printed run directory
  under `app_data/runs/<owner>__<repo>/issue-N/<run-id>/`.

A live run needs Docker, an authenticated `gh`, and the worker image built in
[Setup](#setup). It makes up to `worker.max_attempts` attempts and
`reviewer.max_rounds` review rounds, each worker attempt bounded by
`worker.timeout_minutes`, so it can take a long time and consumes model quota.
If the smoke test is merged, the issue is closed automatically; otherwise close
the pull request, branch, and issue when done.

## First production run

Use this checklist for the first controlled run against a real issue. Complete
[Setup](#setup) first.

1. **Check Docker and the worker image.** Both commands must succeed:

   ```powershell
   docker info
   docker image inspect orch-agent:latest
   ```

   If the image is missing, build it from the repository root:

   ```powershell
   docker build -f docker/agent.Dockerfile -t orch-agent:latest .
   ```

   If `worker.image`, or a separately configured `reviewer.image`, uses another
   tag, inspect that tag instead.

2. **Check the target repository.** The issue must be created in the GitHub
   repository configured as `repo.owner`/`repo.name`. `orch` talks to GitHub
   through `gh`; an issue that exists only in Forgejo, or only in a mirror, is
   not visible to it and the run cannot start.

3. **Create the issue through the project skill.** Follow
   `.opencode/skills/create-issue`; do not call `gh issue create` directly. Use
   an English title in `type(scope): description` form. The skill validates the
   title and body, creates the issue, and adds `agent:queued`. Note the issue
   number `N`.

4. **Run one issue.** From the repository root, with `GH_TOKEN` set as in
   [Setup](#setup):

   ```powershell
   uv run python main.py --issue N --once
   ```

   `--issue` and `--once` must be used together. Replace `N` with the issue
   number. This processes exactly that issue and exits; it does not poll for
   more work.

5. **Confirm each stage in the log.** A successful run prints, in order:

   - `Issue #N attempt 1/3` — the worker started.
   - `Worker tests passed` — `worker.test_commands` succeeded locally.
   - `Created pull request: <url>` — the branch was pushed and the PR opened.
   - `Started reviewer container orch-review-...` — every reported GitHub check
     is complete and none failed. A failed check logs `Issue attempt failed`;
     the full `ChecksFailed: ...` text, including the failing check names, is
     posted as a comment on the issue, and orch starts the next attempt. The
     reviewer is never started for a red PR. If GitHub reports no checks at
     all, orch waits for `checks.discovery_seconds` and continues on the local
     test result.
   - `Reviewer decision: approved` — the reviewer returned an empty `findings`
     array. The raw `{"findings": []}` is in the run directory.
   - `Squash-merged pull request #M` and `Issue #N merged: <url>` — the PR was
     squash-merged into `main`, and `Closes #N` in its body closes the issue.

   The command then exits with code `0` and prints `Run directory: ...`.

6. **Tell success from `agent:needs-human`.**

   - Success: exit code `0`, the status line `merged: <url>`, the issue is
     closed, and `result.json` has `"status": "merged"` plus `merge_commit`
     when GitHub reported it.
   - Parked: exit code `1`, the status line `Needs human: ...`, the issue
     carries `agent:needs-human` and a comment with the error and run ID, and
     `result.json` has `"status": "needs_human"`. The issue stays open; inspect
     the artifacts and re-queue it only after fixing the cause.
   - Exit code `0` alone does not prove a merge: with `reviewer.enabled: false`
     orch stops after green checks, exits `0`, and leaves `result.json` with
     status `pr_open` for a human to merge.

7. **Find the artifacts.** The printed run directory is
   `app_data/runs/<owner>__<repo>/issue-N/<run-id>/`. It contains:

   - `result.json` — terminal status, PR URL, merge commit, checks, attempts,
     and review rounds.
   - `attempt-<K>/events.jsonl` and `attempt-<K>/events.stderr.log` — the worker
     Claude Code stream and stderr. `tests.txt`, `diff.patch`, `handoff.json`,
     and `prompt.md` in the same directory hold the rest of the worker record.
   - `review-<R>/review-output-1.json` — the raw reviewer JSON;
     `review-output-2.json` appears only after a repair launch.
     `review-prompt.md` and `review-schema.json` show what the reviewer was
     asked.
   - `app_data/orch.log` — the orchestrator's own DEBUG log, rotated according
     to `retention_days`.

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
`reviewer.image` and `reviewer.model` fall back to the worker image and model
when omitted. The reviewer timeout should stay in the 10–15 minute range because
a full repository review can be slow.

Feature flags:

- `telegram.enabled` — one-way status messages; requires `chat_id` and a token.
- `feedback.enabled` and `feedback.live_comments` — poll issue comments and
  inject them into the running session. `live_comments: false` keeps the older
  one-shot worker.
- `watchdog.enabled` — Clef-based hang and loop classification; requires an
  Ollama Cloud key.
- `reviewer.enabled` — run the read-only reviewer and auto-merge approved PRs.
  Disable it to stop after green checks and leave the PR for a human.
- `checks.*` — GitHub check polling timeout, interval, and the grace period for
  checks to appear after a PR is opened.

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
- The reviewer is a separate container with the checkout mounted read-only at
  `/review`, no GitHub token, and only `Read`, `Grep`, and `Glob`.
- Reviewer JSON is schema-validated and fail-closed; invalid output never
  approves or merges. A repair launch is allowed once per review round.
- Only the orchestrator merges, and only into `main` after green checks and an
  empty reviewer findings array.
- Logs stay in the local `app_data` directory and are pruned after
  `retention_days`.

## Not yet included

- Multi-repository support
- Per-project worker quotas
- Issue dependency edges (`blocked_by`)
- Hermes triggers or an MCP adapter

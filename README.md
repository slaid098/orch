# orch

Minimal issue-to-pull-request orchestrator for GitHub and Forgejo with
containerized coding agents.

`orch` watches forge issues labelled `agent:queued`, runs a worker container
with Claude Code, validates a deterministic commit, runs the repository tests,
pushes the branch, and opens a pull request. It then waits for forge checks and
runs two separate read-only reviewer containers (quality and security). By
default an approved pull request stays open for a human to merge. Opt-in
`reviewer.auto_merge: true` squash-merges it into `main` only when every check is
green and both reviewers return no findings.

## What it does

```text
Issue with agent:queued
  -> orch claims it (agent:running)
  -> clones origin/main once for the issue
  -> worker implements the issue in a persistent /work volume
  -> authorized issue comments are injected into the running worker
  -> closing the issue cancels the attempt before a pull request
  -> orch squashes all worker changes into one conventional commit
  -> orch runs the configured tests
  -> orch pushes type/scope/issue-N-description
  -> orch opens a standardized pull request
  -> orch waits for forge checks
  -> failed checks return control to the worker, never to the reviewers
  -> quality and security reviewers run in parallel in isolated containers
  -> both must return an empty findings array; any finding blocks
  -> any finding consumes one shared worker attempt and goes back to the worker
  -> approved PR stays open for manual merge by default
  -> reviewer.auto_merge: true squash-merges it into main after green checks
  -> three total worker attempts or three review rounds exhaust to needs-human
```

The worker has no forge token. It cannot push or create pull requests. Both
reviewers have no forge token and only read-only repository tools plus local
CodeGraph and Context7. Reviewers never receive Playwright or antidetect-browser
access. Only the orchestrator process writes and comments; it merges only when
`reviewer.auto_merge` is explicitly enabled.

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
`app_data/workspaces/<owner>__<repo>/issue-N` and is kept while the issue is
revised. It is pruned only after the workspace has had no open pull request for
`workspace_retention_days`. Reopening the issue and adding `agent:queued` again
resumes from the same workspace.

## Reviewer and merge policy

After the worker opens or updates a pull request, orch force-updates the same
deterministic branch and polls the checks for the newly pushed commit until they
finish. A failed check, a merge conflict, or a timeout is reported to the worker
as the next attempt's previous failure. Reviewers are never started for a red
pull request or for a stale commit.

Two reviewers run in parallel, each in its own container with the worker
checkout mounted read-only at `/review` and no forge token:

- **Quality** (`prompt.md`) checks correctness, regressions, tests, and issue
  scope.
- **Security** (`prompt-security.md`) checks secret exposure, auth bypass,
  injection, unsafe shell use, and other concrete security regressions.

Reviewers have `Read`, `Grep`, `Glob`, local CodeGraph, and narrow Context7
access. CodeGraph is optional; if the index is missing or stale, Grep is the
fallback. Context7 is also optional and cannot park a review by itself. The
reviewer containers run on an internal Docker network with no default route. A
small gateway container forwards only the model port and Context7 TLS; all
other egress is unreachable.

Each reviewer result is constrained by a JSON schema:

```json
{"findings": []}
```

Both reviewers must return an empty array. Any finding from either role blocks
the merge; there is no severity field and no optional finding. The orchestrator
posts every finding to the issue and passes the complete list into the next
worker attempt. A technical failure in either role is fail-closed.

Worker attempts and reviewer rounds share one budget. `worker.max_attempts`
defaults to 3 and `reviewer.max_rounds` defaults to 3. A failed worker attempt, a
failed forge check, and a reviewer rejection all consume the next attempt. A
technical reviewer failure (invalid JSON after one repair launch, timeout, or
container failure) does not consume a worker attempt: the issue is parked
immediately in `agent:needs-human`.

By default `reviewer.auto_merge` is `false`. A successful review posts a comment,
labels the issue `agent:pr-open`, and leaves the pull request open for a human to
merge. The persistent workspace is kept so the PR can be revised.

To revise an open pull request, add `agent:queued` to the issue and describe the
requested change in a comment. Orch claims the issue again and reuses the saved
workspace only when its branch and HEAD still match the open pull request. It
then force-updates the same branch with a lease and reruns checks and review. If
the workspace is missing, unreadable, on another branch, or its HEAD no longer
matches the PR, orch parks the issue in `agent:needs-human` instead of rebuilding
it from the base branch. If the PR was closed or its head moved during the run,
orch refuses to push and does not open a replacement PR.

With `reviewer.auto_merge: true`, a successful review is followed by a squash
merge into `main` only. The repository base branch must be `main`; orch refuses
to auto-merge into any other branch. A merge failure is also parked for a human.
The workspace is removed only after a successful merge; it is kept for manual
review, revision, and `agent:needs-human`.

## Telegram status

When `telegram.enabled` is true, orch sends one-way messages to the configured
topic. Status messages use emojis for fast scanning: `🚀 Orch принято`,
`✅ Orch завершено` with the PR URL and duration, and `🚨 Orch нужен человек`
with the reason. Intermediate failures are also reported, including failed
attempts (`⚠️`), reviewer rejections (`🛑`), and technical errors (`🔥`) with the
stage, attempt, run id, and a short redacted reason. Each message includes queue
and active counts where relevant. The bot only sends messages; it does not read
commands, buttons, or chat updates.

Repeated daemon polling failures are reported once, then at most every 30
minutes, with a `🟢` recovery message. Tokens, bearer headers, and obvious
secret assignments are redacted. A failed send is logged, queued under
`data_dir/telegram-queue.jsonl`, and retried on the next daemon startup. It never
interrupts the pipeline.

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
handoff. The workspace is deleted after a successful auto-merge. It is kept for
manual review, revision, and `agent:needs-human` so a human can inspect or
continue the checkout; it can be removed manually after the issue is resolved.

## Requirements

- Python 3.14
- `uv`
- Docker
- `gh` authenticated as the repository owner
- Claude Code, Node, CodeGraph, Playwright, and Chromium inside `orch-agent:latest`
- An Ollama endpoint reachable from Docker at `host.docker.internal:11434`
- Docker permission to create the internal `orch-review-net` network and the
  `orch-review-gateway` container

## Setup

```powershell
uv sync
Copy-Item orch.yaml.example orch.yaml
docker build -f docker/agent.Dockerfile -t orch-agent:latest .
```

If the default Debian mirror is slow from your network, pass a different one with
`--build-arg DEBIAN_MIRROR=mirror.example.org`.

The image is self-contained: Node 26, CodeGraph 1.6.2, Playwright and Chromium,
ripgrep, jq, ffmpeg, and build tools are baked in. Playwright uses the Debian
Chromium at `/usr/bin/chromium`; the build does not download Playwright's
separate browser bundle. Python Playwright calls must pass
`executable_path="/usr/bin/chromium"`; the generated MCP config already does.
Claude Code receives a per-container `mcp.json`;
CodeGraph and Context7 are available to both roles, while Playwright and
antidetect-browser stay worker-only. Antidetect is opt-in: set
`mcp.antidetect: true` and provide its `mcp.antidetect_url`. Public endpoints
must use HTTPS; plain HTTP is accepted only for local or private addresses such
as `host.docker.internal`, `localhost`, loopback, and RFC1918 hosts.

Create the issue on the configured forge and add the `agent:queued` label. Add `agent:hold`
at any time before dispatch to keep orch away from the issue. Before the first
run, or after changing the image, model endpoint, or credentials, verify the
environment:

```powershell
uv run python main.py --check
```

`--check` takes no instance lock and changes nothing remote. It verifies the
Docker daemon and both images, the model endpoint at `127.0.0.1:11434`, the
forge token owner and repository access, the base branch, and free disk space.
It prints every problem and exits with code `1` if any check fails. The daemon
and `--once` run the same checks after taking the lock and before touching
labels, the database, or containers.

Run the daemon:

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

Only one orch process may use a `data_dir` at a time. A second daemon or
`--once` run exits immediately with the PID of the process that holds the lock.
The lock is held by the operating system and is released automatically after a
crash, so there is no stale lock to clean up by hand.

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
forge checks, and runs the reviewer. An approved pull request stays open for
manual merge by default, or is merged when `reviewer.auto_merge: true`. It
processes exactly one issue and never polls.

- Exit code `0`: the pull request was merged, or it is open for manual review
  because `reviewer.auto_merge` is false or `reviewer.enabled` is false. The log
  prints its URL and the run directory; the issue is commented.
- Exit code `1`: the issue is labelled `agent:needs-human`. Inspect the error,
  `result.json`, and the worker and reviewer logs in the printed run directory
  under `app_data/runs/<owner>__<repo>/issue-N/<run-id>/`.

A live run needs Docker, an authenticated `gh`, and the worker image built in
[Setup](#setup). It makes up to `worker.max_attempts` attempts and
`reviewer.max_rounds` review rounds, each worker attempt bounded by
`worker.timeout_minutes`, so it can take a long time and consumes model quota.
If the smoke test is auto-merged, the issue is closed automatically; otherwise
review and merge the pull request yourself, or close it and the issue when done.

## First production run

Use this checklist for the first controlled run against a real issue. Complete
[Setup](#setup) first.

1. **Run the startup checks.** This verifies Docker, both images, the model
   endpoint, forge access, the base branch, and free disk space:

   ```powershell
   uv run python main.py --check
   ```

   If the worker image is missing, build it from the repository root:

   ```powershell
   docker build -f docker/agent.Dockerfile -t orch-agent:latest .
   ```

   If `worker.image`, or a separately configured `reviewer.image`, uses another
   tag, build and inspect that tag instead.

2. **Check the target repository.** The issue must be created in the repository
   configured as `repo.owner`/`repo.name`. `orch` talks to the forge selected by
   `repo.forge`
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
   - `Started reviewer container orch-review-...` — every reported forge check
     is complete and none failed. A failed check logs `Issue attempt failed`;
     the full `ChecksFailed: ...` text, including the failing check names, is
     posted as a comment on the issue, and orch starts the next attempt. No
     reviewer is started for a red PR. If the forge reports no checks at all, orch
     waits for `checks.discovery_seconds` and continues on the local test
     result.
   - `Reviewer role quality: approved` and `Reviewer role security: approved` —
     both reviewers returned empty `findings` arrays. Each raw result is under
     `review-<R>/<role>/`.
   - `Reviewer decision: approved` — merged findings are empty.
   - `Issue #N is waiting for manual review and merge: <url>` — the default
     manual-merge path kept the PR open and the workspace saved.
   - `Squash-merged pull request #M` and `Issue #N merged: <url>` — only with
     `reviewer.auto_merge: true`; `Closes #N` closes the issue.

   The command then exits with code `0` and prints `Run directory: ...`.

6. **Tell success from `agent:needs-human`.**

   - Success: exit code `0`. With auto-merge, the status line is
     `merged: <url>`, the issue is closed, and `result.json` has
     `"status": "merged"` plus `merge_commit` when the forge reported it. Without
     auto-merge, the status is `pr_open` and the PR waits for a human.
   - Parked: exit code `1`, the status line `Needs human: ...`, the issue
     carries `agent:needs-human` and a comment with the error and run ID, and
     `result.json` has `"status": "needs_human"`. The issue stays open; inspect
     the artifacts and re-queue it only after fixing the cause.
   - Exit code `0` alone does not prove a merge: the default manual-merge path
     and `reviewer.enabled: false` both leave `result.json` with status
     `pr_open` for a human to merge.

7. **Find the artifacts.** The printed run directory is
   `app_data/runs/<owner>__<repo>/issue-N/<run-id>/`. It contains:

   - `result.json` — terminal status, PR URL, merge commit, checks, attempts,
     and review rounds.
   - `attempt-<K>/events.jsonl` and `attempt-<K>/events.stderr.log` — the worker
     Claude Code stream and stderr. `tests.txt`, `diff.patch`, `handoff.json`,
     and `prompt.md` in the same directory hold the rest of the worker record.
   - `review-<R>/<role>/review-<role>-output-1.json` — the raw reviewer JSON;
     `review-<role>-output-2.json` appears only after a repair launch.
     `review-prompt.md` and `review-schema.json` show what the reviewer was
     asked.
   - `app_data/orch.log` — the orchestrator's own DEBUG log, rotated according
     to `retention_days`.

## Labels

- `agent:queued` — trigger for the daemon.
- `agent:running` — an attempt or retry loop is active.
- `agent:pr-open` — orch opened a pull request.
- `agent:needs-human` — the issue needs manual intervention.

## Issue dependencies

Both forges are read through their native "blocked by" relations. Before an
issue is claimed, orch asks the forge which issues block it. The rule is
**closed means ready**: an issue starts only when every blocker is `CLOSED`.
The merge status of a blocking pull request is not checked separately.

The dependency check runs immediately before dispatch and again in `run_once`;
it does not change the order or the `max_workers` parallelism of independent
issues. An open or unknown blocker leaves `agent:queued` in place, so the issue
is reconsidered on the next poll. A failed lookup is fail-closed: orch logs the
error and does not start the issue until a later poll succeeds. A `404` from
the dependency endpoint means that dependencies are unsupported or absent and
is treated as an empty list.

Orch stores no dependency state locally; the relations are read from the forge
on every poll, so a daemon restart picks up the current ones.

```text
#42 feat(api): add the endpoint    agent:queued, no blockers
  -> runs immediately

#43 test(api): cover the endpoint    blocked by #42
  -> waits while #42 is OPEN
  -> runs on the next poll after #42 is CLOSED
```

### Creating a dependency chain

`scripts/create_issue.py` creates native "blocked by" edges with the repeatable
`--blocked-by N` option. One call creates one issue, so a chain is a sequence
of calls:

```bash
# A has no dependencies and is queued immediately.
uv run python scripts/create_issue.py \
  --title "feat(api): add the endpoint" --body-file a.md

# B is created on agent:hold, linked to A, and then queued.
uv run python scripts/create_issue.py \
  --title "test(api): cover the endpoint" --body-file b.md --blocked-by 42

# C waits for B.
uv run python scripts/create_issue.py \
  --title "docs(api): document the endpoint" --body-file c.md --blocked-by 43
```

`--blocked-by` accepts several values (`--blocked-by 42 --blocked-by 45`) and
skips an edge that already exists. With `--blocked-by` and no explicit
`--label`, the script creates the issue with `agent:hold`, links every blocker
through the forge adapter, and only then replaces `agent:hold` with
`agent:queued` in one label update. If reading the existing blockers, linking,
or the label update fails, the issue stays on `agent:hold`, the URL is printed,
and the script exits with code `1`; the daemon cannot claim it. The issue
number is parsed from the created issue URL, and an unparsable URL also leaves
the issue on hold with exit code `1`.

An explicit `--label` disables the automatic transition: the script uses
exactly those labels and still links the dependencies, but never queues the
issue. `--label agent:hold` therefore creates a held issue with all edges in
place. `--label agent:queued` together with `--blocked-by` is rejected with
code `2`, because it would let the daemon claim the issue before the edges
exist. `--validate-only` validates only the title and body, touches no network,
and does not require `--blocked-by`.

Limitations:

- The script links dependencies only for the issue it is creating. An edge
  between two existing issues — the first edge in a bootstrap setup, or a link
  from an existing tracking issue — must be added manually through the forge
  UI/API.
- There is no cycle detection, and the CLI cannot remove or reorder edges.
- Dependencies are same-repository only.

## Standards

The issue title is the single source of truth for the work itself; branch
identity depends only on the issue number:

- Issue title: `type(scope): English description`, mandatory scope, at most
  72 characters.
- Branch: `orch/issue-N`. Renaming the issue never changes the branch, so
  revisions continue on the same pull request.
- Commit title and pull-request title: exactly the issue title at creation time.
- One commit; no commit body, GPG signature, or `Signed-off-by`.
- Pull-request body uses four Russian sections, ends with `Closes #N`, and
  carries a hidden `<!-- orch:issue=N -->` marker for stable lookup.

Issue bodies use a fixed eight-section contract. Every section must be present,
in order, and non-empty; extra level-two headings are rejected. This gives an
agent with no prior context enough information to implement the task without
guessing:

- `## Контекст` — current behavior, affected files, and why the task exists.
- `## Задача` — the exact change and the observable result.
- `## Контракты` — signatures, formats, commands, and error behavior.
- `## Инварианты` — rules that must always hold.
- `## Граничные случаи` — empty, stale, duplicate, concurrent, and failure cases.
- `## Влияние на связанные компоненты` — modules, tests, docs, and configuration
  affected.
- `## Вне scope` — work explicitly excluded from this issue.
- `## Критерии приёмки` — verifiable checks that prove the task is done.

The executable source of workflow rules is
`src/orch/services/standards/policy.py`. Architecture and code rules live in the
external `audit` skill; do not copy them here.

## Configuration

Copy `orch.yaml.example` to `orch.yaml` and adjust paths, labels, commands, and
the model. `orch.yaml` and `app_data/` are ignored by git.

The base branch is fetched into a local bare mirror under `data_dir`; the worker
never receives a forge token and never fetches directly.
`reviewer.image` and `reviewer.model` fall back to the worker image and model
when omitted. Reviewers are read-only and usually finish in about 7–8 minutes;
the default 30-minute timeout is a safety cap, while the worker keeps its longer
90-minute budget for implementation and tests.

### Forge selection

`repo.forge` selects the API and the pull-request workflow: `github` (default)
or `forgejo`. For Forgejo, `repo.url` is required (for example
`https://git.example.com`), and the token is read from `ORCH_FORGEJO_TOKEN`
first, then `FORGEJO_TOKEN`. The repository's git remote must point to the same
host. Both forges use the same branch, issue, review, and gate flow. Forgejo
reads `.github/workflows`, so no `.forgejo/workflows` directory is needed.

### Per-role skills

`worker.skills` and `reviewer.skills` list directory names under `skills_dir`
(default `skills/` next to `orch.yaml`). Each selected directory must contain
`SKILL.md`; `--check` fails only when a named skill is missing, and an empty
list is valid. Selected directories are mounted read-only into the
corresponding container under `~/.claude/skills/<name>`. `prompt.md` stays
orchestration-only; role-specific code rules belong in skills.

### Multiple projects

One daemon can serve several repositories. Replace the top-level `repo` block
with a `projects` list and keep shared settings at the top level:

```yaml
data_dir: D:/orch/app_data
max_workers: 2
worker:
  test_commands:
    - uv run pytest -q
projects:
  - name: orch
    repo:
      owner: slaid098
      name: orch
      path: D:/Python projects/orch
      forge: github
  - name: antidetect
    repo:
      owner: slaid098
      name: antidetect-browser-mcp
      path: D:/Python projects/antidetect-browser-mcp
      forge: forgejo
      url: https://git.slaid098.dev
    worker:
      test_commands:
        - uv run pytest -m "not live" -q
```

Top-level `labels`, `worker`, `checks`, `reviewer`, `mcp`, `feedback`, and
`watchdog` are defaults; each project deep-merges its own sections over them,
and lists such as `test_commands` are replaced rather than concatenated.
`data_dir`, `skills_dir`, `poll_interval_seconds`, `max_workers`,
`retention_days`, `workspace_retention_days`, `min_free_disk_gb`, and `telegram`
are global and may only appear at the top level.

One shared pool of `max_workers` slots serves all projects. Each project keeps
its own issue polling, labels, mirror, workspaces, and run directories; run ids
include a repository hash so containers cannot collide. `--check` validates
every project. `--once` needs `--project <name>` when more than one project is
configured. A file without `projects` still loads as one project.

Feature flags:

- `telegram.enabled` — one-way status messages; requires `chat_id` and a token.
- `feedback.enabled` and `feedback.live_comments` — poll issue comments and
  inject them into the running session. `live_comments: false` keeps the older
  one-shot worker.
- `watchdog.enabled` — Clef-based hang and loop classification; requires an
  Ollama Cloud key.
- `reviewer.enabled` — run both read-only reviewers. Disable it to stop after
  green checks and leave the PR for a human.
- `reviewer.auto_merge` — opt-in squash merge after green checks and an
  approved review. Default `false` leaves the PR open for manual merge.
- `checks.*` — forge check polling timeout, interval, and the grace period for
  checks to appear after a PR is opened.
- `mcp.*` — enable or disable CodeGraph, Context7, Playwright, and
  antidetect-browser and override their URLs/commands. Antidetect is disabled
  by default; when enabled, public URLs must use HTTPS, while local/private
  addresses may use HTTP. Reviewers always exclude the worker-only servers.
- `labels.hold` — human hold label (default `agent:hold`). A held issue is
  skipped by dispatch and by the one-shot runner.

### Context window

`worker.autocompact` pins the Claude Code context window. The default `200k`
writes the same value to the CLI flag, the container environment, and
`~/.claude/settings.json` inside the worker, so every agent that runs there
inherits it. `reviewer.autocompact` applies the same setting to reviewer
containers; both roles use `200k` so the shared model and image behave
consistently. `auto` leaves the window to Claude Code.

`retention_days` (default 7) removes old run directories, rotated logs, and
finished rows from the SQLite run database. Rotation happens at daemon startup
and after every finished issue; old data is deleted, not archived.

`workspace_retention_days` (default 14) removes an issue workspace only when no
run is active, the issue has no open pull request, and the newest file in the
workspace is older than the window. The check runs at daemon startup and after
every finished issue. If the open-pull-request lookup fails, every workspace is
kept and the check is retried on the next pass.

`min_free_disk_gb` (default 5) is the minimum free space on the `data_dir`
volume. Both `--check` and normal daemon startup fail before taking work when
the volume has less space.

## Security model

- The worker runs in a disposable container.
- `/src` is a read-only clone of the issue base revision.
- `/work` is the persistent workspace, not readable by the orchestrator token.
- The worker has no forge token and cannot push.
- Only the issue author and explicitly allowed users can inject comments; bot
  comments are filtered out, and orch's own marked comments enter revision
  context only when their author is allowed. Comment text is still treated as
  untrusted task context.
- The Telegram token stays in the orchestrator and is never mounted into the
  worker.
- The worker writes a git bundle and handoff to `/out`.
- The orchestrator validates the bundle, creates the commit, pushes the branch,
  and opens the pull request.
- Each reviewer is a separate container with the checkout mounted read-only at
  `/review`, no forge token, and no direct internet route. A gateway forwards
  only the model port and Context7 TLS.
- Reviewer JSON is schema-validated and fail-closed; invalid output never
  approves or merges. A repair launch is allowed once per review role per
  round. Both roles must independently return empty `findings`.
- Only the orchestrator can merge, and only when `reviewer.auto_merge: true` is
  set: into `main`, after green checks and an empty reviewer findings array.
- Logs stay in the local `app_data` directory and are pruned after
  `retention_days`.

## Not yet included

- Per-project worker quotas
- Hermes triggers

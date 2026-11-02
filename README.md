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
  -> orch squashes all worker changes into one conventional commit
  -> orch runs the configured tests
  -> orch pushes type/scope/issue-N-description
  -> orch opens a standardized pull request
  -> a human merges it
```

The worker has no GitHub token. It cannot push or create pull requests. Only the
orchestrator process touches GitHub.

If an attempt hangs, crashes, or produces no pull request, the worker container
is replaced but the `/work` workspace is kept. Before it stops the container,
orch writes `checkpoint.patch`, `checkpoint.status`, and
`checkpoint-untracked.tar.gz` into the attempt directory, so tracked and
untracked work can be recovered even if the workspace is damaged. The next
attempt continues in the same directory with a fresh context and a short
handoff. The workspace is deleted only after a pull request is created.

## Workspace recovery

The worker container is disposable, the workspace is not: every attempt mounts
the same host directory at `/work`:

```text
<data_dir>/workspaces/<owner>__<repo>/issue-N
```

When an attempt fails, times out, or is interrupted, orch recreates the
container but keeps this directory and reuses it. The branch, the `orch-base`
tag, and tracked and untracked changes remain on disk, so the next attempt
resumes from the existing state in a fresh agent context; its `prompt.md` gets
a `## Resume` section with the previous error. The directory is removed only
after a pull request is created; if attempts are exhausted, it stays for
inspection.

`checkpoint.patch`, `checkpoint.status`, and `checkpoint-untracked.tar.gz` in
`<data_dir>/runs/<owner>__<repo>/issue-N/<run-id>/attempt-N/` are a fallback if
the workspace itself is damaged.

To verify recovery, run
`git -C <data_dir>/workspaces/<owner>__<repo>/issue-N status` and
`git -C <data_dir>/workspaces/<owner>__<repo>/issue-N log --oneline`, and check
that the attempt directory holds the checkpoints and the next attempt's
`prompt.md` contains a `## Resume` section.

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
uv run python main.py
```

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
  `data/runs/<owner>__<repo>/issue-N/<run-id>/`.

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
the model. `orch.yaml` and `data/` are ignored by git.

The base branch is fetched into a local bare mirror under `data_dir`; the worker
never receives a GitHub token and never fetches from GitHub directly.

## Security model

- The worker runs in a disposable container.
- `/src` is a read-only clone of the issue base revision.
- `/work` is the persistent workspace, not readable by the orchestrator token.
- The worker has no GitHub token and cannot push.
- The worker writes a git bundle and handoff to `/out`.
- The orchestrator validates the bundle, creates the commit, pushes the branch,
  and opens the pull request.
- Logs stay in the local data directory and are not committed.

## Not yet included

- Reviewer agent and automatic review loop
- Auto-merge by the daemon
- Multi-repository support

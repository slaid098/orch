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
is replaced but the `/work` workspace is kept. The next attempt continues in the
same directory with a fresh context and a short handoff. The workspace is deleted
only after a pull request is created.

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

Debug one issue synchronously:

```powershell
uv run python main.py --issue 42 --once
```

The daemon runs at most `max_workers` containers (default 2, maximum 3) and
waits for active workers on shutdown. On startup it removes orphan containers
from a previous process and re-queues issues left in `agent:running`.

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

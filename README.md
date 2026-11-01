# orch

Minimal GitHub issue to pull request orchestrator for containerized coding agents.

`orch` watches GitHub issues labelled `agent:queued`, runs a worker container
with Claude Code, validates a deterministic commit, runs the repository tests,
pushes the branch, and opens a pull request. The pull request is reviewed and
merged by a human (or by the assistant operating the repository).

## What it does

```
GitHub issue with agent:queued
  -> orch daemon claims it (agent:running)
  -> a fresh clone of origin/main is prepared for the attempt
  -> worker container implements the issue and writes /out/handoff.json
  -> orch squashes all worker changes into one conventional commit
  -> orch runs the configured tests
  -> orch imports the git bundle and pushes agent/<issue>
  -> orch opens a standardized pull request
  -> a human merges it
```

The worker has no GitHub token. It cannot push or create pull requests. Only the
orchestrator process touches GitHub.

If any attempt hangs, crashes, or produces no pull request, the daemon discards
that attempt and retries from a clean clone of the latest `origin/main`. After
`worker.max_attempts` failures the issue gets `agent:needs-human` and a comment
with the last error.

## Requirements

- Python 3.14
- `uv`
- Docker
- `gh` authenticated as the repository owner
- Claude Code available inside the worker image
- An Ollama endpoint reachable from Docker at `host.docker.internal:11434`

## Setup

```powershell
uv sync
Copy-Item orch.yaml.example orch.yaml
uv run orch build-image
```

Create the issue on GitHub and add the `agent:queued` label. Then run the only
production mode:

```powershell
uv run orch daemon
```

The daemon claims issues by replacing `agent:queued` with `agent:running`. It
runs at most `max_workers` containers (default 2, maximum 3). On shutdown it
waits for active workers. On startup it removes orphan containers from a
previous process and re-queues issues left in `agent:running`.

## Commands

```text
orch run --issue N     Debug one issue synchronously.
orch daemon            Production mode: poll queued issues and run a worker pool.
orch status            Show recent runs.
orch logs --issue N    Print the latest attempt logs for an issue.
orch build-image       Build the worker Docker image.
```

There is no separate watchdog mode. Use a supervisor or the OS task scheduler
if the daemon process itself must be restarted automatically.

## Labels

- `agent:queued` — trigger for the daemon.
- `agent:running` — an attempt or retry loop is active.
- `agent:pr-open` — orch opened a pull request.
- `agent:needs-human` — all attempts failed; inspect the issue comments.

## Commit and PR standard

The worker writes `/out/handoff.json`; it does not choose the final commit or PR.
The finalizer validates the title, resets the worker branch to the attempt base,
stages all changes, and creates exactly one commit.

- Commit and PR title: `type(scope): English description`, at most 72 characters.
- PR body, in Russian: `## Что сделано`, `## Почему`, `## Watch out`, `## Pending`.
- The body ends with `Closes #N`.

## Configuration

Copy `orch.yaml.example` to `orch.yaml` and adjust paths, labels, commands, and
the model. `orch.yaml` and `data/` are intentionally ignored by git.

Each attempt fetches the base branch into a local bare mirror under `data_dir`,
then creates a disposable clone for the worker. The worker never receives a
GitHub token and never fetches from GitHub directly.

## Security model

- The worker runs in a disposable container.
- `/src` is a read-only clone of the attempt's base revision.
- The worker has no GitHub token and cannot push.
- The worker writes a git bundle and handoff to `/out`.
- The orchestrator validates the bundle, creates the commit, pushes the branch,
  and opens the pull request.
- Logs stay in the local data directory and are not committed.

## Not yet included

- Reviewer agent and automatic review loop
- Auto-merge by the daemon
- CI gate / branch protection
- Multi-repository support

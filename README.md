# orch

Minimal GitHub issue to pull request orchestrator for containerized coding agents.

`orch` watches GitHub issues labelled `agent:ready`, runs a worker container with
Claude Code, executes the repository test commands, pushes the resulting branch,
and opens a pull request. In MVP-0 the pull request is merged manually.

## What it does

```
GitHub issue
  -> orch daemon claims it
  -> worker container clones the repository read-only
  -> Claude Code implements the issue and writes artifacts to /out
  -> orch runs the configured tests
  -> orch imports the git bundle and pushes a branch
  -> orch opens a pull request
  -> a human merges the pull request
```

The worker has no GitHub token. It cannot push or create pull requests. Only the
orchestrator process touches GitHub.

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
uv run orch run --issue 1
```

Create the issue on GitHub and add the `agent:ready` label. The orchestrator
will claim it by replacing the label with `agent:running`.

## Commands

```text
orch run --issue N     Run one issue synchronously.
orch daemon            Poll for ready issues and run them one at a time.
orch status            Show recent runs.
orch logs --issue N    Print the latest run directory for an issue.
orch build-image       Build the worker Docker image.
```

## Configuration

Copy `orch.yaml.example` to `orch.yaml` and adjust paths, labels, commands, and
the model. `orch.yaml` and `data/` are intentionally ignored by git.

The worker image is built from `docker/agent.Dockerfile`. The worker clones the
repository from the host path configured in `repo.path`, so the local checkout
must be clean and committed before a run.

## Security model

- The worker runs in a disposable container.
- `/src` is mounted read-only.
- The worker has no GitHub token and cannot push.
- The worker writes a git bundle and artifacts to `/out`.
- The orchestrator validates the branch, pushes it, and opens the pull request.
- Logs stay in the local data directory and are not committed.

## Not in MVP-0

- Specifier agent
- Reviewer agent
- Auto-merge
- CI gate
- Sub-issues and task graphs
- Parallel workers

# Orchestration labels

`orch` uses GitHub issue labels to track where an issue is in the pipeline.
The orchestrator is the only process that changes these labels; humans use them
to opt an issue in or to see what happened to it.

## Labels

| Label | Meaning |
| --- | --- |
| `agent:queued` | The issue is waiting for `orch`. This is the **trigger**: adding it tells the daemon to pick the issue up. |
| `agent:running` | `orch` has claimed the issue and a worker is implementing it. |
| `agent:pr-open` | The worker finished, the tests passed, and `orch` opened a pull request. The pull request is waiting for review and merge. |
| `agent:needs-human` | `orch` could not finish the issue. A **human must inspect the issue** before anything else happens to it. |

The labels are mutually exclusive: an issue in the pipeline has at most one of
them at a time.

## Lifecycle

```text
                 add agent:queued
   (no label) ----------------------> agent:queued   <- trigger
                                          |
                                          | orch claims the issue
                                          v
                                    agent:running
                                       /       \
                     success          /         \        failure
                                     v           v
                             agent:pr-open   agent:needs-human
                                                    |
                                                    | human inspects and retries
                                                    v
                                              agent:queued
```

1. A human adds `agent:queued` to an open issue. This is the trigger for `orch`
   to start working on it.
2. `orch` claims the issue: it removes `agent:queued` and adds `agent:running`.
3. On success, `orch` opens a pull request, removes `agent:running`, adds
   `agent:pr-open`, and comments on the issue with the pull request link.
4. On failure, `orch` removes `agent:running`, adds `agent:needs-human`, and
   comments on the issue with the error, the run ID, and the branch name.
5. A human reviews `agent:pr-open` pull requests and merges them. For
   `agent:needs-human`, a human must inspect the issue first; once the problem
   is resolved, adding `agent:queued` again queues the issue for another run.

## Meaning of `agent:needs-human`

`agent:needs-human` is an explicit stop signal. It does not mean "retry later"
and `orch` will not touch the issue again on its own. A human must read the
issue, inspect the failure (the comment links the run and branch), and decide
whether to fix the problem and re-queue the issue or close it.

## Meaning of `agent:queued`

`agent:queued` is the only trigger. An issue without it is never claimed, even
if it is otherwise ready to work on. Removing it before `orch` claims the issue
cancels the request.

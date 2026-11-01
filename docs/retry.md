# Retry behavior

`orch` treats every run of an issue as an attempt. Attempts are isolated and
automatic: a failure does not leave the issue stuck or require manual cleanup
before the next try.

## A fresh clone for every attempt

Each attempt starts from the latest `origin/main` in a fresh clone. `orch` does
not reuse the working tree, branch, or uncommitted changes left by an earlier
attempt. Everything produced by a failed attempt is discarded, so a retry always
builds on the current base branch instead of on stale or half-finished work.

## Failed and timed-out attempts

An attempt that fails — for example because the worker exits with an error, the
tests fail, or the worker exceeds `worker.timeout_minutes` — is discarded and
retried automatically. The worker container is stopped, its output is kept only
as run artifacts for inspection, and the next attempt begins from a fresh clone
as described above.

## Exhausting the attempts

`orch` retries only up to the configured maximum number of attempts. Once that
limit is reached, the issue is labelled `agent:needs-human` and automatic
retries stop. The last error remains available in the run artifacts, and a human
has to investigate the issue before it is worked on again.

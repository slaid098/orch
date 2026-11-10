---
name: Create Issue
description: Create a validated forge issue for the orch pipeline. Use when the user asks to create, file, or open an issue for this repository.
---

# Create Issue

Create one issue through the project validator. The script uses
`services/standards/policy.py` as the source of truth, so do not reimplement or
bypass its rules. Both GitHub and Forgejo targets are supported; choose the
forge that hosts the target repository.

## Workflow

1. **Research the code first.** Read the modules, tests, configuration, and
   README that the task touches. Identify current behavior, call sites, and
   constraints. Every statement in the issue must be verifiable in the
   repository; never invent file paths, symbols, or behavior.
2. **Resolve open decisions with the user.** Ask concise questions about
   behavior, scope boundaries, and trade-offs that cannot be derived from the
   code. Wait for answers. If the request is already fully specified, say so and
   continue without inventing extra questions.
3. **Draft the title.** Use English `type(scope): description` form. Type, scope,
   and description are mandatory; the validator rejects invalid input.
4. **Write the body** to a temporary file outside the repository. Use exactly
   these eight level-two sections, in this order, with no extra `##` headings:

   ```markdown
   ## Контекст
   Current behavior, affected files and symbols, and why the task exists.

   ## Задача
   The exact change and the observable result. No implementation diary.

   ## Контракты
   Signatures, input/output formats, CLI or configuration shapes, and error
   behavior that must stay stable or change.

   ## Инварианты
   Rules that must always hold: ordering, atomicity, permissions, idempotency,
   fail-closed behavior.

   ## Граничные случаи
   Empty, missing, stale, duplicate, concurrent, and failure cases with the
   required reaction for each.

   ## Влияние на связанные компоненты
   Modules, tests, documentation, configuration, and user-visible behavior
   affected by the change.

   ## Вне scope
   Work explicitly excluded from this issue to prevent scope creep.

   ## Критерии приёмки
   Verifiable checks: commands, tests, expected outputs, manual scenarios.
   ```

   The text above describes what each section must contain; write the actual
   body in Russian. Every section must be non-empty. Use concrete file paths,
   symbols, and commands; keep code-like identifiers in English.
5. **Validate** from the repository root:

   ```bash
   uv run python scripts/create_issue.py \
     --title "type(scope): description" \
     --body-file "<temp-file>" \
     --validate-only
   ```

6. If validation fails, rewrite the title and/or body and run the same command
   again. Do not create the issue until validation passes. Never pad a broken
   title or body with placeholders.
7. **Create** the issue with the same command, without `--validate-only`:

   ```bash
   uv run python scripts/create_issue.py \
     --title "type(scope): description" \
     --body-file "<temp-file>"
   ```

   For a Forgejo-hosted repository, select the forge explicitly. The token is
   read from `FORGEJO_TOKEN`; never pass it on the command line:

   ```bash
   uv run python scripts/create_issue.py \
     --forge forgejo \
     --repo "owner/name" \
     --url "https://git.example.test" \
     --title "type(scope): description" \
     --body-file "<temp-file>"
   ```

   `FORGEJO_URL` may replace `--url`. The script validates again and only then
   calls the forge API. It adds the `agent:queued` label by default and resolves
   label IDs before creating a Forgejo issue.
8. Report the created issue URL and title.

## Цепочки и рёбра зависимостей

A `blocked by` edge is a native forge relation, not a text mention and not a
sub-issue. It tells the orch daemon not to start the issue until the blocking
issue is closed. A sub-issue only groups work under a parent and does not stop
execution by itself; when a child must wait, create an explicit `blocked by`
edge as well. The daemon reads these edges through `list_blockers`, so the
relation must exist before `agent:queued` is added.

### Chain A → B → C

The script creates exactly one issue per invocation. Build a chain with
sequential calls and the repeatable `--blocked-by N` option:

```bash
# A has no dependencies and is queued immediately; it becomes issue #12.
uv run python scripts/create_issue.py \
  --title "feat(api): add the endpoint" \
  --body-file "<a.md>"

# B is created on agent:hold, linked to #12, and only then queued; B is #13.
uv run python scripts/create_issue.py \
  --title "test(api): cover the endpoint" \
  --body-file "<b.md>" \
  --blocked-by 12

# C waits for #13.
uv run python scripts/create_issue.py \
  --title "docs(api): document the endpoint" \
  --body-file "<c.md>" \
  --blocked-by 13
```

Repeat the flag for several blockers: `--blocked-by 12 --blocked-by 15`.

### Why hold → link → queue

`agent:queued` is the daemon trigger. Creating the issue as queued and linking
the edges afterwards would leave a window in which the daemon could claim it.
Therefore, with `--blocked-by` and no explicit `--label`, the script:

1. creates the issue with `agent:hold`;
2. reads the existing blockers, skips duplicates, and links every
   `--blocked-by` through the forge adapter;
3. replaces `agent:hold` with `agent:queued` in one label update.

If reading the existing blockers, linking, or the label update fails, the issue
stays on `agent:hold`, the script prints the issue URL, and exits with code
`1`. The daemon cannot claim it. Fix the cause and queue the issue manually
only after every edge is verified.

An explicit `--label` changes this contract: the script uses exactly those
labels, still links the dependencies, and never queues the issue
automatically. `--label agent:hold` is the way to link edges and keep the
issue held. `--label agent:queued` together with `--blocked-by` is rejected
with code `2` because it would violate the safe-creation invariant.

### Parent tracking issue

A tracking issue is an ordinary issue that lists the chain in its body. Create
it with the hold label so the daemon never runs it:

```bash
uv run python scripts/create_issue.py \
  --title "feat(api): endpoint rollout" \
  --body-file "<tracking.md>" \
  --label agent:hold
```

The tracking issue body still follows the eight-section contract; put the
checklist of child issues in `## Задача`:

```markdown
## Задача
- [ ] #12 feat(api): add the endpoint
- [ ] #13 test(api): cover the endpoint
- [ ] #14 docs(api): document the endpoint
```

Sub-issues express grouping only; add a `blocked by` edge when one child must
wait for another. The first edge between two already existing issues — for
example in a bootstrap setup or from an existing tracking issue — is added
manually through the forge, because the script links dependencies only for the
issue it creates.

## Rules

- Never call `gh issue create`, the Forgejo API, or a dependency endpoint
  directly for a target repository. Use the script for every issue and every
  `blocked by` edge; it goes through `services/github` and `services/forgejo`.
- Never create a dependent issue directly with `agent:queued`. Use
  `--blocked-by` and let the script link the edges before queueing.
- Keep one issue per invocation. A chain is a sequence of calls, not one call.
- Never leave `TBD`, `TODO`, `???`, or similar placeholders in the body.
- Never use `needs-human` to work around an invalid title.
- Keep the title English and the body Russian.
- Do not put credentials, tokens, or environment dumps in the issue.
- If the task is too broad for one issue, split it and ask the user.

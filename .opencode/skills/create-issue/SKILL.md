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

## Rules

- Never call `gh issue create` or the Forgejo API directly for a target
  repository.
- Never leave `TBD`, `TODO`, `???`, or similar placeholders in the body.
- Never use `needs-human` to work around an invalid title.
- Keep the title English and the body Russian.
- Do not put credentials, tokens, or environment dumps in the issue.
- If the task is too broad for one issue, split it and ask the user.

---
name: Create Issue
description: Create a validated GitHub issue for the orch pipeline. Use when the user asks to create, file, or open an issue for this repository.
---

# Create Issue

Create one GitHub issue through the project validator. The script uses
`services/standards/policy.py` as the source of truth, so do not reimplement or
bypass its rules.

## Workflow

1. Determine **what** must change and **why** it matters. Ask only when the
   request is genuinely ambiguous.
2. Draft an English title in `type(scope): description` form. The type, scope,
   and description are mandatory; the validator will reject invalid input.
3. Write the body to a temporary file outside the repository using this
   structure:

   ```markdown
   ## Что сделать
   Конкретное изменение или результат.

   ## Почему
   Проблема или причина, по которой это нужно.
   ```

4. From the repository root, validate before creating:

   ```bash
   uv run python scripts/create_issue.py \
     --title "type(scope): description" \
     --body-file "<temp-file>" \
     --validate-only
   ```

5. If validation fails, rewrite the title and/or body and run the same command
   again. A title must be regenerated, not patched with filler. Do not create the
   issue until validation passes.
6. Create the issue with the same command, without `--validate-only`:

   ```bash
   uv run python scripts/create_issue.py \
     --title "type(scope): description" \
     --body-file "<temp-file>"
   ```

   The script validates again and only then calls `gh issue create`. It adds the
   `agent:queued` label by default.

7. Report the created issue URL and title. Stop if validation cannot be fixed;
   ask the user instead of creating an invalid issue.

## Rules

- Never call `gh issue create` directly for this repository.
- Never use `needs-human` to work around an invalid title.
- Keep the title English and the body focused on what and why.
- Do not put credentials, tokens, or environment dumps in the issue.

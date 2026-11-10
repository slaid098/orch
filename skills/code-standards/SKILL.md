---
name: code-standards
description: Use before writing or changing any code in this repository. Covers search-first discovery, minimal clean architecture, TDD, English Google-style docstrings, error handling, size targets, and the definition of done. Load it for every implementation, refactor, bug fix, or test change.
---

# Code standards

These rules apply to **new and touched code**. Do not rewrite untouched files to
match them: an unrelated refactor inflates the diff and hides the real change.

`audit` is a secondary WARN-only checker, not the rulebook. Its thresholds are a
hard ceiling, not a target. If code is already above the target but below the
ceiling, leave it alone unless the issue touches it.

## 1. Search before writing

1. Use `mcp__codegraph__codegraph_explore` first when the index exists.
2. Fall back to `rg`/`Grep` and `Glob`: search by symbol, error text, config key,
   and test name, not only by file name.
3. Read the call sites, the tests, and the nearest `AGENTS.md`.
4. Reuse the existing function, module, or pattern. Do not create a second
   mechanism for a solved problem.
5. If two solutions are possible and the issue does not decide, prefer the
   smaller one that fits existing conventions; state the trade-off in the
   handoff.

Before adding a dependency, check the manifest and lock file. Prefer the
standard library and already-declared packages.

## 2. Shape of the code

- **KISS and YAGNI.** Write only what the issue needs. No speculative
  abstractions, plugin systems, or "for the future" parameters.
- **Functional first.** Use a class only for state that must be held or for a
  third-party interface. Prefer pure functions and small dataclasses.
- **One responsibility per module.** Business logic does not live in transport,
  CLI, or view code. Files and functions have names that say what they do.
- **Absolute imports** from the package root. No relative imports.
- **Private helpers** use a `_` prefix. Public surface is deliberate: a capsule
  door (`__init__.py`) re-exports behavior only, no logic, no data types.
- **No duplication of infrastructure.** Extract the shared part and keep the
  difference in a parameter or configuration, not in a copied function.
- **Fail closed** on security, permissions, money, and data loss. An unknown
  state is an error, not an implicit approval.

### Size targets and hard ceiling

| Unit | Target | Hard ceiling (audit) | When above target |
| --- | --- | --- | --- |
| Python file | 200–300 lines | 1000 | split by responsibility in touched code |
| Function / method | 30–50 effective lines | 60 | extract named steps |
| `services/<domain>/service.py` | ≤150 lines | 1000 | move parts into capsule modules |
| Capsule door `__init__.py` | ≤30 lines, re-export only | 30 | remove logic and data types |
| Flat `tests/` | ≤40 files | 40 | add `unit/`, `integration/`, `live/` tiers |

Effective lines exclude the docstring, blank lines, and comment-only lines.
Existing files above a ceiling are not a reason to stop; do not make them worse.

## 3. TDD

For every behavior change:

1. Write or update a test that fails for the right reason (missing behavior,
   not an import error).
2. Implement the smallest change that makes it pass.
3. Refactor with the test green.
4. Run the repository's full check command, not only the new test.

Rules:

- Test observable behavior and contracts, not private call order.
- One scenario per test; the name says the scenario.
- Do not weaken, skip, or delete an existing test to make a change pass. If a
  test encodes obsolete behavior, the issue must say so.
- A bug fix needs a regression test that fails on the old code.
- Do not add a test for a scenario the issue explicitly excludes.
- Keep tests deterministic: no real network, clock, or filesystem outside
  `tmp_path` unless the test is marked `live`.

## 4. Docstrings, comments, language

- **Public API has a Google-style docstring in English**: module, public
  function, public class, public method. Describe purpose, `Args`, `Returns`,
  and `Raises` when they exist. Keep it short and factual.
- Private helpers get a docstring only when the name is not enough.
- Comments explain **why**, not what the next line does. Delete commented-out
  code.
- Russian is for issues, pull-request bodies, handoffs, user-facing comments,
  README, and chat. Code identifiers, docstrings, log messages, and commit
  subjects stay English.
- Never leave `TODO`, `TBD`, `???`, or placeholder values in delivered code.

## 5. Errors and logging

- Catch only what you can handle. Use `try/except/else`: the `else` block is the
  success path, `finally` is cleanup.
- Never swallow an exception silently. Re-raise with context or convert it to
  the module's own error type.
- Log through `loguru`, binding the exception and its type:

  ```python
  try:
      ...
  except Exception as ex:
      logger.bind(error=ex, error_type=type(ex).__name__).error(
          "Could not <do the thing>"
      )
  ```

- Do not log secrets, tokens, full issue bodies, or environment dumps.
- A best-effort side effect (notification, stale-branch cleanup) logs a warning
  and continues; a correctness step fails the attempt.

## 6. Definition of done

Before writing the handoff:

- The change is limited to the issue; unrelated files are untouched.
- New and touched behavior is covered by tests; the full suite passes.
- The repository's lint, format, and type commands pass. Use the commands in
  the nearest `AGENTS.md`; if it lists none, use the project's existing
  `pyproject.toml`/CI commands.
- Public contracts (signatures, config keys, error codes, CLI flags) either stay
  unchanged or change exactly as the issue specifies.
- No secrets, debug prints, generated artifacts, or temporary files are added.
- The handoff names the real risk: what could break, what was not verified, and
  what remains.

When the audit report disagrees with this skill, follow this skill for touched
code and fix audit findings only when they are real and in scope.

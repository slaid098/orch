# orch — agent entrypoint

- Architecture and code rules: the external `audit` skill (`agent-skills`).
- Issue, branch, commit, and pull-request rules:
  `src/orch/services/standards/policy.py`.
- Create issues only through `.opencode/skills/create-issue`.
- Production entrypoint: `uv run python main.py`.
- Checks: `uv run ruff check .` and `uv run pytest`.
- Do not restate standards in this file.

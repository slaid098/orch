"""Workflow standards constants."""

COMMIT_TYPES = (
    "feat",
    "fix",
    "build",
    "chore",
    "ci",
    "docs",
    "refactor",
    "revert",
    "test",
    "style",
    "perf",
)
TITLE_MAX_LENGTH = 72
PR_ISSUE_MARKER = "<!-- orch:issue={issue_number} -->"
ISSUE_BODY_HEADINGS = (
    "## Контекст",
    "## Задача",
    "## Контракты",
    "## Инварианты",
    "## Граничные случаи",
    "## Влияние на связанные компоненты",
    "## Вне scope",
    "## Критерии приёмки",
)
PR_HEADINGS = (
    "## Что сделано",
    "## Почему",
    "## На что обратить внимание",
    "## Что осталось",
)

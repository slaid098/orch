from __future__ import annotations

from orch.github import Issue


def test_issue_from_json() -> None:
    issue = Issue.from_json(
        {
            "number": 12,
            "title": "Test issue",
            "body": "Body",
            "url": "https://example.test/12",
            "author": {"login": "slaid098"},
            "labels": [{"name": "agent:ready"}, {"name": "bug"}],
        }
    )

    assert issue.number == 12
    assert issue.author == "slaid098"
    assert issue.labels == ("agent:ready", "bug")
    assert issue.has_label("agent:ready")
    assert not issue.has_label("agent:running")

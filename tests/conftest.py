"""Shared pytest configuration."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Mark unmarked tests as unit tests."""
    del config
    for item in items:
        if not any(
            item.get_closest_marker(marker)
            for marker in ("unit", "integration", "live")
        ):
            item.add_marker(pytest.mark.unit)

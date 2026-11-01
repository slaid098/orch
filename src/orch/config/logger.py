"""Loguru bridge used by all services."""

from __future__ import annotations

from loguru import logger


def get_logger(**context: object):
    """Return a logger bound to stable context fields.

    Args:
        **context: Context bound to the logger.

    Returns:
        A Loguru logger with the given context.
    """
    return logger.bind(**context)

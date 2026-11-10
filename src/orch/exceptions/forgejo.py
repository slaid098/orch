"""Forgejo exceptions."""

from orch.exceptions.forge import ForgeError


class ForgejoError(ForgeError):
    """Raised when a Forgejo API operation fails."""

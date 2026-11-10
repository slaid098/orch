"""GitHub exceptions."""

from orch.exceptions.forge import ForgeError


class GitHubError(ForgeError):
    """Raised when a GitHub CLI operation fails."""

"""Worker exceptions."""


class AgentError(RuntimeError):
    """Raised when the worker cannot produce a usable result."""

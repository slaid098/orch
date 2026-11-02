"""Worker exceptions."""


class AgentError(RuntimeError):
    """Raised when the worker cannot produce a usable result."""


class RunCancelled(AgentError):
    """Raised when an issue is closed while its worker is running."""


class WatchdogStop(AgentError):
    """Raised when the watchdog decides that the run must stop."""

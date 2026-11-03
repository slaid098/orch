"""Worker exceptions."""

from orch.exceptions.agent import AgentError


class RunCancelled(AgentError):
    """Raised when an issue is closed while its worker is running."""


class WatchdogStop(AgentError):
    """Raised when the watchdog decides that the run must stop."""


__all__ = ["AgentError", "RunCancelled", "WatchdogStop"]

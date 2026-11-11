"""Worker exceptions."""

from orch.exceptions.agent import AgentError


class RunCancelled(AgentError):
    """Raised when an issue is closed while its worker is running."""


class WatchdogStop(AgentError):
    """Raised when the watchdog decides that the run must stop.

    Attributes:
        state: Classifier state that triggered the stop, when known.
        retryable: Whether the run may be retried in the same workspace.
    """

    def __init__(
        self,
        message: str = "",
        *,
        state: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.state = state
        self.retryable = retryable


__all__ = ["AgentError", "RunCancelled", "WatchdogStop"]

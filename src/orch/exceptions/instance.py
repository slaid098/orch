"""Single-instance lock exceptions."""


class InstanceLockError(RuntimeError):
    """Raised when another orch process already holds the data-directory lock."""

    def __init__(self, message: str, *, holder: str = "") -> None:
        """Create the error and keep the previous holder metadata.

        Args:
            message: Human-readable explanation.
            holder: Metadata written by the process that owns the lock.
        """
        super().__init__(message)
        self.holder = holder

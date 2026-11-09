"""Local retention entrypoints."""

from orch.services.retention.service import prune_runs, prune_workspaces

__all__ = ["prune_runs", "prune_workspaces"]

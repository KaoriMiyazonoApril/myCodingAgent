"""Immediate, bounded leases for normalized workspace roots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import uuid4

from .errors import WorkspaceBusyError


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    """Opaque ownership token returned for one active Turn."""

    lease_id: str
    workspace: Path


class WorkspaceLeaseManager:
    """Reject overlapping roots or excess capacity without queueing."""

    def __init__(self, max_active_turns: int = 4) -> None:
        if (
            isinstance(max_active_turns, bool)
            or not isinstance(max_active_turns, int)
            or not 1 <= max_active_turns <= 32
        ):
            raise ValueError("max_active_turns must be between 1 and 32")
        self._max_active_turns = max_active_turns
        self._leases: dict[str, Path] = {}
        self._lock = Lock()

    def acquire(self, workspace: Path) -> WorkspaceLease:
        normalized = workspace.resolve(strict=True)
        with self._lock:
            if len(self._leases) >= self._max_active_turns:
                raise WorkspaceBusyError("global active Turn capacity reached")
            if any(
                self._overlaps(normalized, active)
                for active in self._leases.values()
            ):
                raise WorkspaceBusyError(
                    f"workspace overlaps an active Turn: {normalized.as_posix()}"
                )
            lease_id = str(uuid4())
            self._leases[lease_id] = normalized
        return WorkspaceLease(lease_id=lease_id, workspace=normalized)

    def release(self, lease: WorkspaceLease) -> None:
        with self._lock:
            active = self._leases.get(lease.lease_id)
            if active == lease.workspace:
                del self._leases[lease.lease_id]

    @staticmethod
    def _overlaps(first: Path, second: Path) -> bool:
        return first == second or first in second.parents or second in first.parents

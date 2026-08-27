"""Versioned public data returned by the in-memory Agent Runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .settings import ThreadSettings


class ThreadStatus(str, Enum):
    """Whether a Thread can currently accept a new Turn."""

    IDLE = "idle"
    RUNNING = "running"


class TurnStatus(str, Enum):
    """Terminal status exposed for the first Runtime tracer bullet."""

    COMPLETED = "completed"


SCHEMA_VERSION = 1


def _public_dict(value: object) -> dict[str, Any]:
    """Convert a public dataclass to a detached JSON-compatible dictionary."""

    return asdict(value)  # str enums intentionally serialize as JSON strings.


@dataclass(frozen=True, slots=True)
class ThreadSnapshot:
    """Detached point-in-time view from which a frontend can fully recover."""

    schema_version: int
    thread_id: str
    workspace: str
    status: ThreadStatus
    active_turn_id: str | None
    completed_turns: int
    settings: ThreadSettings
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    latest_turn: TurnSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a detached structure accepted by strict JSON encoders."""

        return _public_dict(self)


@dataclass(frozen=True, slots=True)
class TurnSummary:
    """Structured terminal result of one ReAct Turn."""

    schema_version: int
    turn_id: str
    thread_id: str
    status: TurnStatus
    stop_reason: str
    final_text: str
    iterations: int
    tool_calls: int
    usage: dict[str, int | None] = field(default_factory=dict)
    modified_files: list[str] = field(default_factory=list)
    file_diffs: list[dict[str, Any]] = field(default_factory=list)
    diff_complete: bool = False
    started_at: str = ""
    ended_at: str = ""
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a detached structure accepted by strict JSON encoders."""

        return _public_dict(self)

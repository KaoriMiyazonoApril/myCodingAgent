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
    WAITING_APPROVAL = "waiting_approval"
    CLOSED = "closed"


class TurnStatus(str, Enum):
    """Lifecycle states used by current and future public Turn views."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIMIT_REACHED = "limit_reached"


SCHEMA_VERSION = 1


def _public_dict(value: object) -> dict[str, Any]:
    """Convert a public dataclass to a detached JSON-compatible dictionary."""

    return _json_safe(asdict(value))


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


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
    pending_approval: dict[str, Any] | None = None
    turns: list[TurnSummary] = field(default_factory=list)
    skills: dict[str, Any] = field(default_factory=dict)

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

"""Provider-independent public types for the in-memory Agent Runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .settings import ThreadSettings


class ThreadStatus(str, Enum):
    """Whether a Thread can currently accept a new Turn."""

    IDLE = "idle"
    RUNNING = "running"


class TurnStatus(str, Enum):
    """Terminal status exposed for the first Runtime tracer bullet."""

    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ThreadSnapshot:
    """Small immutable view of a Thread's observable lifecycle state."""

    thread_id: str
    workspace: str
    status: ThreadStatus
    active_turn_id: str | None
    completed_turns: int
    settings: ThreadSettings


@dataclass(frozen=True, slots=True)
class TurnSummary:
    """Observable result of one completed ReAct Turn."""

    turn_id: str
    thread_id: str
    status: TurnStatus
    final_text: str
    iterations: int
    tool_calls: int

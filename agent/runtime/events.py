"""Bounded, non-blocking stage events for one in-memory Thread."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
import math
from typing import Any
from uuid import uuid4

from agent.core.messages import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agent.model.types import LLMResponse
from agent.tools.types import ToolResult

from .types import SCHEMA_VERSION


def utc_now() -> str:
    """Return an RFC 3339 UTC timestamp suitable for the public API."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def json_safe(value: object) -> Any:
    """Copy supported values while refusing to expose arbitrary object reprs."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return json_safe(value.value)
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): json_safe(item)
            for key, item in value.items()
            if isinstance(key, (str, int, float, bool))
        }
    return "<unsupported>"


def public_message(message: Message) -> dict[str, Any] | None:
    """Serialize a conversation message without system prompts or reasoning."""

    if message.role == "system":
        return None
    content: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, ReasoningBlock):
            continue
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolCallBlock):
            content.append(
                {
                    "type": "tool_call",
                    "id": block.id,
                    "name": block.name,
                    "arguments": json_safe(block.arguments),
                    "arguments_error": block.arguments_error,
                    "raw_arguments": block.raw_arguments,
                }
            )
        elif isinstance(block, ToolResultBlock):
            content.append(
                {
                    "type": "tool_result",
                    "tool_call_id": block.tool_call_id,
                    "ok": block.ok,
                    "content": block.content,
                    "metadata": json_safe(block.metadata),
                    "error_code": block.error_code,
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "role": message.role,
        "content": content,
    }


def public_usage(response: LLMResponse) -> dict[str, int | None]:
    return {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "total_tokens": response.usage.total_tokens,
    }


def public_tool_call(call: ToolCallBlock) -> dict[str, Any]:
    return {
        "id": call.id,
        "name": call.name,
        "arguments": json_safe(call.arguments),
        "arguments_error": call.arguments_error,
        "raw_arguments": call.raw_arguments,
    }


def public_tool_result(call: ToolCallBlock, result: ToolResult) -> dict[str, Any]:
    return {
        "tool_call_id": call.id,
        "ok": result.ok,
        "content": result.content,
        "metadata": json_safe(result.metadata),
        "error_code": result.error_code,
    }


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One stable JSON-compatible event envelope."""

    schema_version: int
    event_id: str
    thread_id: str
    turn_id: str | None
    sequence: int
    type: str
    timestamp: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EventBatch:
    """A non-blocking event read plus cursor-expiry information."""

    schema_version: int
    events: list[AgentEvent]
    cursor_expired: bool
    latest_event_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBuffer:
    """Drop oldest events at capacity instead of back-pressuring the Agent."""

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("event_buffer_capacity must be a positive integer")
        self._events: deque[AgentEvent] = deque(maxlen=capacity)
        self._thread_sequence = 0

    def append(self, event: AgentEvent) -> None:
        self._events.append(event)

    def emit_thread(
        self,
        *,
        thread_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> AgentEvent:
        """Append an event whose lifecycle scope is the Thread, not one Turn."""

        self._thread_sequence += 1
        event = AgentEvent(
            schema_version=SCHEMA_VERSION,
            event_id=str(uuid4()),
            thread_id=thread_id,
            turn_id=None,
            sequence=self._thread_sequence,
            type=event_type,
            timestamp=utc_now(),
            payload=json_safe(payload),
        )
        self.append(event)
        return event

    def read(self, after_event_id: str | None = None) -> EventBatch:
        retained = list(self._events)
        if after_event_id is None:
            selected = retained
            expired = False
        else:
            index = next(
                (
                    position
                    for position, event in enumerate(retained)
                    if event.event_id == after_event_id
                ),
                None,
            )
            expired = index is None
            selected = [] if expired else retained[index + 1 :]
        return EventBatch(
            schema_version=SCHEMA_VERSION,
            events=[AgentEvent(**event.to_dict()) for event in selected],
            cursor_expired=expired,
            latest_event_id=retained[-1].event_id if retained else None,
        )


class TurnEventEmitter:
    """Translate loop stages into ordered public events without owning the loop."""

    def __init__(
        self,
        *,
        thread_id: str,
        turn_id: str,
        buffer: EventBuffer,
        reasoning_visibility: str,
    ) -> None:
        self._thread_id = thread_id
        self._turn_id = turn_id
        self._buffer = buffer
        self._reasoning_visibility = reasoning_visibility
        self._sequence = 0

    def emit(self, event_type: str, payload: dict[str, Any]) -> AgentEvent:
        self._sequence += 1
        event = AgentEvent(
            schema_version=SCHEMA_VERSION,
            event_id=str(uuid4()),
            thread_id=self._thread_id,
            turn_id=self._turn_id,
            sequence=self._sequence,
            type=event_type,
            timestamp=utc_now(),
            payload=json_safe(payload),
        )
        self._buffer.append(event)
        return event

    def model_response(self, response: LLMResponse, iteration: int) -> None:
        message = public_message(response.message)
        self.emit(
            "model_response",
            {
                "iteration": iteration,
                "message": message,
                "finish_reason": response.finish_reason,
                "usage": public_usage(response),
            },
        )
        if self._reasoning_visibility == "debug":
            reasoning = "".join(
                block.text
                for block in response.message.content
                if isinstance(block, ReasoningBlock)
            )
            if reasoning:
                self.emit(
                    "model_reasoning",
                    {"iteration": iteration, "text": reasoning},
                )

    def tool_requested(self, call: ToolCallBlock) -> None:
        self.emit("tool_requested", {"tool_call": public_tool_call(call)})

    def tool_started(self, call: ToolCallBlock) -> None:
        self.emit(
            "tool_started",
            {"tool_call_id": call.id, "name": call.name},
        )

    def tool_finished(self, call: ToolCallBlock, result: ToolResult) -> None:
        self.emit(
            "tool_finished",
            {
                "name": call.name,
                "result": public_tool_result(call, result),
            },
        )

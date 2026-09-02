"""Bounded, non-blocking stage events for one in-memory Thread."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
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
from agent.model.types import (
    ErrorEvent,
    LLMResponse,
    MessageEndEvent,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolCallDeltaEvent,
    Usage,
)
from agent.tools.types import ToolResult

from .types import SCHEMA_VERSION

# Bounded reasoning text carried on durable model_response events so the UI
# can render the thinking chain without persisting the full stream.
_REASONING_PREVIEW_CHARS = 3000


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


def usage_dict(usage: Usage | None) -> dict[str, int | None]:
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
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


_TRANSIENT_EVENT_TYPES = frozenset(
    {
        "model_text_delta",
        "model_reasoning_delta",
        "model_tool_call_delta",
        "model_message_end",
        "model_error",
        "command_output_delta",
    }
)
DEFAULT_SEQUENCE_RESERVATION_SIZE = 64
_TURN_TERMINAL_EVENT_TYPES = frozenset(
    {
        "turn_completed",
        "turn_failed",
        "turn_cancelled",
        "turn_limit_reached",
    }
)


class EventSubscription:
    """A bounded wake-only subscription over an :class:`EventBuffer`."""

    def __init__(self, buffer: EventBuffer, after_event_id: str | None) -> None:
        self._buffer = buffer
        self._after_event_id = after_event_id
        self._wake = asyncio.Event()
        self._closed = False
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def _notify(self) -> None:
        if self._closed:
            return
        if self._loop is None:
            self._wake.set()
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self._loop:
            self._wake.set()
        else:
            self._loop.call_soon_threadsafe(self._wake.set)

    async def read(self, after_event_id: str | None = None) -> EventBatch:
        """Wait until events follow ``after_event_id`` or the cursor expires."""

        if after_event_id is not None:
            self._after_event_id = after_event_id
        while True:
            if self._closed:
                return self._buffer.read(self._after_event_id)
            self._wake.clear()
            batch = self._buffer.read(self._after_event_id)
            if batch.events or batch.cursor_expired:
                return batch
            await self._wake.wait()

    wait = read
    wait_for_events = read
    next_batch = read

    @property
    def closed(self) -> bool:
        return self._closed

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._buffer._subscriptions.discard(self)
        self._wake.set()

    async def __aenter__(self) -> EventSubscription:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def __aiter__(self) -> EventSubscription:
        return self

    async def __anext__(self) -> AgentEvent:
        batch = await self.read(self._after_event_id)
        if not batch.events:
            await self.aclose()
            raise StopAsyncIteration
        event = batch.events[0]
        self._after_event_id = event.event_id
        return event


class EventBuffer:
    """Drop oldest events at capacity instead of back-pressuring the Agent."""

    def __init__(
        self,
        capacity: int,
        *,
        events: list[AgentEvent] | tuple[AgentEvent, ...] = (),
        initial_sequence: int | None = None,
        sequence_reservation_size: int = DEFAULT_SEQUENCE_RESERVATION_SIZE,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("event_buffer_capacity must be a positive integer")
        if (
            isinstance(sequence_reservation_size, bool)
            or not isinstance(sequence_reservation_size, int)
            or sequence_reservation_size < 1
        ):
            raise ValueError("sequence_reservation_size must be a positive integer")
        self._events: deque[AgentEvent] = deque(maxlen=capacity)
        self._thread_sequence = max(
            0,
            max((event.sequence for event in events), default=0),
            0 if initial_sequence is None else initial_sequence,
        )
        # ``_sequence_watermark`` is a durable high-water mark, not the last
        # event assigned.  Runtime reserves a range before emitting events so
        # a crash after a transient delta cannot cause sequence reuse.
        self._sequence_watermark = self._thread_sequence
        self._sequence_reservation_size = sequence_reservation_size
        self._subscriptions: set[EventSubscription] = set()
        self._durable_sink: Callable[[AgentEvent], object] | None = None
        self._sequence_checkpoint: Callable[[int], object] | None = None
        for event in events:
            self._events.append(event)

    def set_durable_sink(self, sink: Callable[[AgentEvent], object] | None) -> None:
        """Mirror semantic events to storage without persisting stream deltas."""

        self._durable_sink = sink

    def set_sequence_checkpoint(
        self,
        checkpoint: Callable[[int], object] | None,
    ) -> None:
        """Persist reserved sequence high-water marks in bounded batches."""

        self._sequence_checkpoint = checkpoint

    @property
    def thread_sequence(self) -> int:
        """Latest Thread-scoped event sequence, including retained events."""

        return self._thread_sequence

    @property
    def sequence_watermark(self) -> int:
        """Highest sequence reserved durably for this Thread."""

        return self._sequence_watermark

    def next_sequence(self, *, checkpoint: bool = True) -> int:
        """Allocate one monotonic sequence for any Thread event emitter."""

        if self._sequence_checkpoint is not None and (
            self._thread_sequence >= self._sequence_watermark
        ):
            previous_watermark = self._sequence_watermark
            watermark = max(
                self._thread_sequence,
                self._sequence_watermark,
            ) + self._sequence_reservation_size
            self._sequence_watermark = watermark
            if checkpoint:
                try:
                    self._sequence_checkpoint(watermark)
                except BaseException:
                    self._sequence_watermark = previous_watermark
                    raise
        self._thread_sequence += 1
        if self._sequence_checkpoint is None:
            self._sequence_watermark = max(
                self._sequence_watermark,
                self._thread_sequence,
            )
        return self._thread_sequence

    def append(self, event: AgentEvent) -> None:
        self._thread_sequence = max(self._thread_sequence, event.sequence)
        if self._sequence_checkpoint is None:
            self._sequence_watermark = max(
                self._sequence_watermark,
                self._thread_sequence,
            )
        self._events.append(event)
        if self._durable_sink is not None and event.type not in _TRANSIENT_EVENT_TYPES:
            self._durable_sink(event)
        for subscription in tuple(self._subscriptions):
            subscription._notify()

    def subscribe(self, after_event_id: str | None = None) -> EventSubscription:
        """Create a wake-only real-time subscription without adding a queue."""

        subscription = EventSubscription(self, after_event_id)
        self._subscriptions.add(subscription)
        return subscription

    subscribe_realtime = subscribe

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    async def wait_for_events(self, after_event_id: str | None = None) -> EventBatch:
        """Wait once for replayable events, then detach the subscriber."""

        subscription = self.subscribe(after_event_id)
        try:
            return await subscription.read()
        finally:
            await subscription.aclose()

    def emit_thread(
        self,
        *,
        thread_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> AgentEvent:
        """Append an event whose lifecycle scope is the Thread, not one Turn."""

        event = AgentEvent(
            schema_version=SCHEMA_VERSION,
            event_id=str(uuid4()),
            thread_id=thread_id,
            turn_id=None,
            sequence=self.next_sequence(),
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
        self._terminal_event: AgentEvent | None = None

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        checkpoint: bool = True,
    ) -> AgentEvent:
        # A persistent ProcessSession keeps its original emitter after its
        # creator Turn finishes.  Gate that emitter at the lifecycle boundary
        # so delayed process callbacks cannot append ordinary events after a
        # terminal Turn event (or leak into a later Turn through a rebound
        # sink).
        if self._terminal_event is not None:
            return self._terminal_event
        event = AgentEvent(
            schema_version=SCHEMA_VERSION,
            event_id=str(uuid4()),
            thread_id=self._thread_id,
            turn_id=self._turn_id,
            sequence=self._buffer.next_sequence(checkpoint=checkpoint),
            type=event_type,
            timestamp=utc_now(),
            payload=json_safe(payload),
        )
        self._buffer.append(event)
        if event_type in _TURN_TERMINAL_EVENT_TYPES:
            self._terminal_event = event
        return event

    @property
    def terminal(self) -> bool:
        """Whether this Turn's ordinary event channel is closed."""

        return self._terminal_event is not None

    def model_response(self, response: LLMResponse, iteration: int) -> None:
        message = public_message(response.message)
        reasoning = (
            "".join(
                block.text
                for block in response.message.content
                if isinstance(block, ReasoningBlock)
            )
            if self._reasoning_visibility != "hidden"
            else ""
        )
        payload: dict[str, Any] = {
            "iteration": iteration,
            "message": message,
            "finish_reason": response.finish_reason,
            "usage": public_usage(response),
        }
        if reasoning:
            # "visible" keeps a bounded preview on the durable event so the UI
            # can show the thinking chain without persisting the full stream;
            # "debug" keeps the complete chain for diagnostics.
            bounded = (
                self._reasoning_visibility != "debug"
                and len(reasoning) > _REASONING_PREVIEW_CHARS
            )
            payload["reasoning_preview"] = {
                "text": (
                    reasoning[:_REASONING_PREVIEW_CHARS] if bounded else reasoning
                ),
                "truncated": bounded,
                "total_chars": len(reasoning),
            }
        self.emit("model_response", payload)
        if self._reasoning_visibility == "debug" and reasoning:
            self.emit(
                "model_reasoning",
                {"iteration": iteration, "text": reasoning},
            )

    def model_delta(
        self,
        event: TextDeltaEvent
        | ReasoningDeltaEvent
        | ToolCallDeltaEvent
        | MessageEndEvent
        | ErrorEvent,
    ) -> None:
        """Publish provisional model output without constructing history."""

        if isinstance(event, TextDeltaEvent):
            self.emit("model_text_delta", {"text": event.text})
        elif isinstance(event, ReasoningDeltaEvent):
            if self._reasoning_visibility != "hidden":
                self.emit("model_reasoning_delta", {"text": event.text})
        elif isinstance(event, ToolCallDeltaEvent):
            self.emit(
                "model_tool_call_delta",
                {
                    "index": event.index,
                    "id": event.id,
                    "name": event.name,
                    "arguments_delta": event.arguments_delta,
                },
            )
        elif isinstance(event, MessageEndEvent):
            self.emit(
                "model_message_end",
                {
                    "finish_reason": event.finish_reason,
                    "usage": usage_dict(event.usage),
                },
            )
        elif isinstance(event, ErrorEvent):
            self.emit(
                "model_error",
                {
                    "message": event.message,
                    "error_code": event.error_code,
                    "retryable": event.retryable,
                },
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

"""Assemble provider deltas into one canonical assistant response."""

from __future__ import annotations

from dataclasses import dataclass, field
import json

from agent.core.messages import (
    ContentBlock,
    Message,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
)
from agent.model.errors import LLMResponseParseError
from agent.model.types import (
    ErrorEvent,
    LLMEvent,
    LLMResponse,
    MessageEndEvent,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolCallDeltaEvent,
    Usage,
)


@dataclass(slots=True)
class _ToolCallState:
    index: int
    id: str | None = None
    name: str | None = None
    arguments: list[str] = field(default_factory=list)


class MessageAssembler:
    """Keep provisional deltas out of Conversation until message end."""

    def __init__(self) -> None:
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._tool_calls: dict[int, _ToolCallState] = {}
        self._ids: dict[str, int] = {}
        self._finished = False
        self._finish_reason: str | None = None
        self._usage: Usage | None = None
        self._delta_seen = False

    @property
    def delta_seen(self) -> bool:
        """Whether any provisional model content has been observed."""

        return self._delta_seen

    @property
    def finished(self) -> bool:
        return self._finished

    def add(self, event: LLMEvent) -> None:
        """Consume one local provider event without mutating Conversation."""

        if self._finished:
            raise LLMResponseParseError("stream emitted data after message end")
        if isinstance(event, TextDeltaEvent):
            self._text.append(event.text)
            self._delta_seen = self._delta_seen or bool(event.text)
            return
        if isinstance(event, ReasoningDeltaEvent):
            self._reasoning.append(event.text)
            self._delta_seen = self._delta_seen or bool(event.text)
            return
        if isinstance(event, ToolCallDeltaEvent):
            self._add_tool_call(event)
            self._delta_seen = True
            return
        if isinstance(event, MessageEndEvent):
            self._finish_reason = event.finish_reason
            self._usage = event.usage
            self._finished = True
            return
        if isinstance(event, ErrorEvent):
            raise LLMResponseParseError(event.message)
        raise LLMResponseParseError("unknown streaming event")

    consume = add
    feed = add

    def assemble(self) -> LLMResponse:
        """Build the canonical response only after a normal MessageEnd event."""

        if not self._finished:
            raise LLMResponseParseError("stream ended before message end")
        blocks: list[ContentBlock] = []
        text = "".join(self._text)
        reasoning = "".join(self._reasoning)
        if reasoning:
            blocks.append(ReasoningBlock(text=reasoning))
        if text:
            blocks.append(TextBlock(text=text))
        for index in sorted(self._tool_calls):
            state = self._tool_calls[index]
            if not state.id:
                raise LLMResponseParseError(
                    f"Tool call at index {index} has no valid id"
                )
            if not state.name:
                raise LLMResponseParseError(
                    f"Tool call {state.id!r} has no valid function name"
                )
            raw_arguments = "".join(state.arguments)
            arguments, arguments_error = self._parse_arguments(raw_arguments)
            blocks.append(
                ToolCallBlock(
                    id=state.id,
                    name=state.name,
                    arguments=arguments,
                    raw_arguments=raw_arguments,
                    arguments_error=arguments_error,
                )
            )
        return LLMResponse(
            message=Message(role="assistant", content=blocks),
            finish_reason=self._finish_reason,
            usage=self._usage or Usage(),
        )

    finalize = assemble

    @property
    def response(self) -> LLMResponse:
        return self.assemble()

    @property
    def message(self) -> Message:
        return self.assemble().message

    def _add_tool_call(self, event: ToolCallDeltaEvent) -> None:
        if isinstance(event.index, bool) or not isinstance(event.index, int):
            raise LLMResponseParseError("Tool call index must be an integer")
        if event.index < 0:
            raise LLMResponseParseError("Tool call index must not be negative")
        if event.id:
            existing_index = self._ids.get(event.id)
            if existing_index is not None and existing_index != event.index:
                raise LLMResponseParseError(
                    f"Tool call id {event.id!r} changed index"
                )
            self._ids[event.id] = event.index
        state = self._tool_calls.setdefault(
            event.index,
            _ToolCallState(index=event.index),
        )
        if event.id:
            if state.id is not None and state.id != event.id:
                raise LLMResponseParseError(
                    f"Tool call index {event.index} changed id"
                )
            state.id = event.id
        if event.name:
            if state.name is not None and state.name != event.name:
                raise LLMResponseParseError(
                    f"Tool call index {event.index} changed function name"
                )
            state.name = event.name
        if event.arguments_delta is not None:
            state.arguments.append(event.arguments_delta)

    @staticmethod
    def _parse_arguments(raw_arguments: str) -> tuple[dict[str, object] | None, str | None]:
        if raw_arguments == "":
            return {}, None
        try:
            decoded = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return None, "invalid JSON arguments"
        if not isinstance(decoded, dict):
            return None, "arguments must decode to a JSON object"
        return decoded, None


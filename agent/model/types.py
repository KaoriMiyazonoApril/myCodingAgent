"""Provider-independent data types used by the agent's LLM boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias


Role: TypeAlias = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ProviderConfig:
    """Connection settings for one OpenAI-compatible API endpoint."""

    provider: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout: float | None = None

    def __post_init__(self) -> None:
        self.provider = self.provider.strip().lower()
        self.base_url = self.base_url.rstrip("/")
        if not self.provider:
            raise ValueError("provider must not be empty")
        if not self.base_url:
            raise ValueError("base_url must not be empty")
        if not self.api_key:
            raise ValueError("api_key must not be empty")
        if not self.model:
            raise ValueError("model must not be empty")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("timeout must be positive")


@dataclass(slots=True)
class TextBlock:
    type: Literal["text"] = field(default="text", init=False)
    text: str = ""


@dataclass(slots=True)
class ReasoningBlock:
    type: Literal["reasoning"] = field(default="reasoning", init=False)
    text: str = ""


@dataclass(slots=True)
class ToolCallBlock:
    id: str
    name: str
    arguments: dict[str, Any]
    type: Literal["tool_call"] = field(default="tool_call", init=False)


@dataclass(slots=True)
class ToolResultBlock:
    tool_call_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = field(default="tool_result", init=False)


ContentBlock: TypeAlias = TextBlock | ReasoningBlock | ToolCallBlock | ToolResultBlock


@dataclass(slots=True)
class Message:
    role: Role
    content: list[ContentBlock]


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class LLMRequest:
    messages: list[Message]
    tools: list[ToolDefinition] | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    extra_body: dict[str, Any] | None = None


@dataclass(slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(slots=True)
class LLMResponse:
    message: Message
    finish_reason: str | None
    usage: Usage
    raw: Any | None = field(default=None, repr=False)


@dataclass(slots=True)
class TextDeltaEvent:
    type: Literal["text_delta"] = field(default="text_delta", init=False)
    text: str = ""


@dataclass(slots=True)
class ReasoningDeltaEvent:
    type: Literal["reasoning_delta"] = field(default="reasoning_delta", init=False)
    text: str = ""


@dataclass(slots=True)
class ToolCallDeltaEvent:
    index: int
    id: str | None = None
    name: str | None = None
    arguments_delta: str | None = None
    type: Literal["tool_call_delta"] = field(default="tool_call_delta", init=False)


@dataclass(slots=True)
class MessageEndEvent:
    finish_reason: str | None
    usage: Usage | None = None
    type: Literal["message_end"] = field(default="message_end", init=False)


@dataclass(slots=True)
class ErrorEvent:
    message: str
    type: Literal["error"] = field(default="error", init=False)


LLMEvent: TypeAlias = (
    TextDeltaEvent | ReasoningDeltaEvent | ToolCallDeltaEvent | MessageEndEvent | ErrorEvent
)

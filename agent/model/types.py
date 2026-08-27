"""Provider-independent data types used by the agent's LLM boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from agent.core.messages import Message
from agent.tools.types import ToolDefinition

from .errors import LLMConfigurationError


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Protocol behavior that differs between OpenAI-compatible providers."""

    preserve_reasoning_for_tool_calls: bool = False
    reasoning_input_field: str | None = None
    requires_assistant_content_for_tool_calls: bool = False


@dataclass(slots=True)
class ProviderConfig:
    """Connection settings for one OpenAI-compatible API endpoint."""

    provider: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout: float | None = None
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str):
            raise LLMConfigurationError("provider must be a string")
        if not isinstance(self.base_url, str):
            raise LLMConfigurationError("base_url must be a string")
        if not isinstance(self.api_key, str):
            raise LLMConfigurationError("api_key must be a string")
        if not isinstance(self.model, str):
            raise LLMConfigurationError("model must be a string")
        if not isinstance(self.capabilities, ProviderCapabilities):
            raise LLMConfigurationError("capabilities must be ProviderCapabilities")

        self.provider = self.provider.strip().lower()
        self.base_url = self.base_url.strip().rstrip("/")
        if not self.provider:
            raise LLMConfigurationError("provider must not be empty")
        if not self.base_url:
            raise LLMConfigurationError("base_url must not be empty")
        if not self.api_key:
            raise LLMConfigurationError("api_key must not be empty")
        if not self.model:
            raise LLMConfigurationError("model must not be empty")
        if self.timeout is not None and (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or self.timeout <= 0
        ):
            raise LLMConfigurationError("timeout must be a positive number")


@dataclass(slots=True)
class LLMRequest:
    messages: list[Message]
    tools: list[ToolDefinition] | None = None
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

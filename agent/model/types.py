"""Provider-independent data types used by the agent's LLM boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping, TypeAlias

from agent.core.messages import Message
from agent.tools.types import ToolDefinition

from .errors import LLMConfigurationError


class ReasoningRetention(str, Enum):
    """How assistant reasoning is replayed into provider history."""

    NEVER = "never"
    TOOL_CHAIN_ONLY = "tool_chain_only"
    ALWAYS = "always"


class WorkingTailMode(str, Enum):
    """How detached Harness working state is represented at request tail."""

    LATE_SYSTEM = "late_system"
    STRUCTURED_USER_TAIL = "structured_user_tail"


class ThinkingParameterStyle(str, Enum):
    """How a provider adapter encodes the unified thinking controls.

    The Web and Runtime only deal in ``ThinkingSettings``.  This enum keeps
    vendor request details below that boundary while still making the
    mapping inspectable and testable at the provider seam.
    """

    GENERIC = "generic"
    DEEPSEEK_V4 = "deepseek_v4"
    KIMI_REASONING_EFFORT = "kimi_reasoning_effort"
    KIMI_TOGGLE = "kimi_toggle"
    KIMI_ALWAYS_ON = "kimi_always_on"
    GLM = "glm"


class _Unset:
    """Sentinel separating inheritance from an explicit ``None`` override."""


_UNSET = _Unset()


@dataclass(frozen=True, slots=True)
class ThinkingCapabilities:
    """Allowlisted thinking request features supported by one model."""

    supported: bool = False
    default_enabled: bool = False
    toggle_supported: bool | None = None
    supports_budget_tokens: bool = False
    supported_keep_values: tuple[str, ...] = ()
    intensity_supported: bool = False
    intensity_options: tuple[str, ...] = ()
    default_intensity: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.supported, bool):
            raise LLMConfigurationError("thinking supported must be a boolean")
        if not isinstance(self.default_enabled, bool):
            raise LLMConfigurationError("thinking default_enabled must be a boolean")
        toggle_supported = self.toggle_supported
        if toggle_supported is None:
            toggle_supported = self.supported
            object.__setattr__(self, "toggle_supported", toggle_supported)
        if not isinstance(toggle_supported, bool):
            raise LLMConfigurationError("thinking toggle_supported must be a boolean")
        if not isinstance(self.supports_budget_tokens, bool):
            raise LLMConfigurationError(
                "thinking supports_budget_tokens must be a boolean"
            )
        if not isinstance(self.supported_keep_values, tuple) or any(
            not isinstance(value, str) or not value
            for value in self.supported_keep_values
        ):
            raise LLMConfigurationError(
                "thinking supported_keep_values must be non-empty strings"
            )
        if not isinstance(self.intensity_supported, bool):
            raise LLMConfigurationError(
                "thinking intensity_supported must be a boolean"
            )
        if not isinstance(self.intensity_options, tuple) or any(
            not isinstance(value, str) or not value
            for value in self.intensity_options
        ):
            raise LLMConfigurationError(
                "thinking intensity_options must be non-empty strings"
            )
        if len(set(self.intensity_options)) != len(self.intensity_options):
            raise LLMConfigurationError("thinking intensity_options must be unique")
        if self.intensity_supported and not self.intensity_options:
            raise LLMConfigurationError(
                "supported thinking intensity must declare options"
            )
        if not self.intensity_supported and self.intensity_options:
            raise LLMConfigurationError(
                "unsupported thinking intensity cannot declare options"
            )
        if self.default_intensity is not None and (
            not isinstance(self.default_intensity, str)
            or not self.default_intensity
            or self.default_intensity not in self.intensity_options
        ):
            raise LLMConfigurationError(
                "thinking default_intensity must be one of intensity_options"
            )
        if not self.supported and (
            self.default_enabled
            or toggle_supported
            or self.supports_budget_tokens
            or self.supported_keep_values
            or self.intensity_supported
            or self.intensity_options
            or self.default_intensity is not None
        ):
            raise LLMConfigurationError(
                "unsupported thinking cannot declare optional features"
            )

    def to_dict(self) -> dict[str, object]:
        """Return the safe capability projection used by Host/UI adapters."""

        return {
            "supported": self.supported,
            "default_enabled": self.default_enabled,
            "toggle_supported": bool(self.toggle_supported),
            "supports_budget_tokens": self.supports_budget_tokens,
            "supported_keep_values": list(self.supported_keep_values),
            "intensity_supported": self.intensity_supported,
            "intensity_options": list(self.intensity_options),
            "default_intensity": self.default_intensity,
        }


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Protocol behavior that differs between OpenAI-compatible providers."""

    reasoning_retention: ReasoningRetention = ReasoningRetention.NEVER
    reasoning_input_field: str | None = None
    reasoning_output_fields: tuple[str, ...] = ("reasoning_content", "thinking")
    requires_assistant_content_for_tool_calls: bool = False
    thinking: ThinkingCapabilities = field(default_factory=ThinkingCapabilities)
    context_window_tokens: int | None = None
    # Unknown OpenAI-compatible providers use the conservative user-message
    # envelope.  A trailing system message is an explicit opt-in because
    # provider schemas do not guarantee its semantics after tool history.
    working_tail_mode: WorkingTailMode = WorkingTailMode.STRUCTURED_USER_TAIL
    thinking_parameter_style: ThinkingParameterStyle | str = ThinkingParameterStyle.GENERIC

    def __post_init__(self) -> None:
        if not isinstance(self.reasoning_retention, ReasoningRetention):
            raise LLMConfigurationError(
                "reasoning_retention must be a ReasoningRetention value"
            )
        if self.reasoning_input_field is not None and (
            not isinstance(self.reasoning_input_field, str)
            or not self.reasoning_input_field
        ):
            raise LLMConfigurationError(
                "reasoning_input_field must be a non-empty string or None"
            )
        if not isinstance(self.requires_assistant_content_for_tool_calls, bool):
            raise LLMConfigurationError(
                "requires_assistant_content_for_tool_calls must be a boolean"
            )
        if not isinstance(self.thinking, ThinkingCapabilities):
            raise LLMConfigurationError("thinking must be ThinkingCapabilities")
        if self.context_window_tokens is not None and (
            isinstance(self.context_window_tokens, bool)
            or not isinstance(self.context_window_tokens, int)
            or self.context_window_tokens <= 0
        ):
            raise LLMConfigurationError(
                "context_window_tokens must be a positive integer or None"
            )
        if not isinstance(self.working_tail_mode, WorkingTailMode):
            try:
                object.__setattr__(
                    self,
                    "working_tail_mode",
                    WorkingTailMode(self.working_tail_mode),
                )
            except (TypeError, ValueError) as error:
                raise LLMConfigurationError(
                    "working_tail_mode must be late_system or structured_user_tail"
                ) from error
        if not isinstance(self.thinking_parameter_style, ThinkingParameterStyle):
            try:
                object.__setattr__(
                    self,
                    "thinking_parameter_style",
                    ThinkingParameterStyle(self.thinking_parameter_style),
                )
            except (TypeError, ValueError) as error:
                raise LLMConfigurationError(
                    "thinking_parameter_style is not supported"
                ) from error
        if not isinstance(self.reasoning_output_fields, tuple) or any(
            not isinstance(field_name, str) or not field_name
            for field_name in self.reasoning_output_fields
        ):
            raise LLMConfigurationError(
                "reasoning_output_fields must be a tuple of non-empty strings"
            )

@dataclass(frozen=True, slots=True)
class ModelProfile:
    """Partial capability overrides for one exact model."""

    model_id: str | None = None
    display_name: str | None = None
    description: str | None = None
    reasoning_retention: ReasoningRetention | None = None
    reasoning_input_field: str | None | _Unset = _UNSET
    reasoning_output_fields: tuple[str, ...] | _Unset = _UNSET
    requires_assistant_content_for_tool_calls: bool | None = None
    thinking: ThinkingCapabilities | None = None
    context_window_tokens: int | None | _Unset = _UNSET
    working_tail_mode: WorkingTailMode | str | _Unset = _UNSET
    thinking_parameter_style: ThinkingParameterStyle | str | _Unset = _UNSET

    def apply(self, defaults: ProviderCapabilities) -> ProviderCapabilities:
        return ProviderCapabilities(
            reasoning_retention=(
                self.reasoning_retention
                if self.reasoning_retention is not None
                else defaults.reasoning_retention
            ),
            reasoning_input_field=(
                defaults.reasoning_input_field
                if self.reasoning_input_field is _UNSET
                else self.reasoning_input_field
            ),
            reasoning_output_fields=(
                defaults.reasoning_output_fields
                if self.reasoning_output_fields is _UNSET
                else self.reasoning_output_fields
            ),
            requires_assistant_content_for_tool_calls=(
                self.requires_assistant_content_for_tool_calls
                if self.requires_assistant_content_for_tool_calls is not None
                else defaults.requires_assistant_content_for_tool_calls
            ),
            thinking=(
                self.thinking if self.thinking is not None else defaults.thinking
            ),
            context_window_tokens=(
                defaults.context_window_tokens
                if self.context_window_tokens is _UNSET
                else self.context_window_tokens
            ),
            working_tail_mode=(
                defaults.working_tail_mode
                if self.working_tail_mode is _UNSET
                else WorkingTailMode(self.working_tail_mode)
            ),
            thinking_parameter_style=(
                defaults.thinking_parameter_style
                if self.thinking_parameter_style is _UNSET
                else ThinkingParameterStyle(self.thinking_parameter_style)
            ),
        )

    def metadata(self, model_id: str) -> dict[str, object]:
        """Return model facts without exposing credentials or endpoints."""

        resolved_id = self.model_id or model_id
        return {
            "model_id": resolved_id,
            "display_name": self.display_name or resolved_id,
            "description": self.description or "",
            "context_window_tokens": (
                None if self.context_window_tokens is _UNSET else self.context_window_tokens
            ),
            "known": True,
        }


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """Provider defaults plus optional exact-model capability overrides."""

    base_url: str
    default_capabilities: ProviderCapabilities = field(
        default_factory=ProviderCapabilities
    )
    model_profiles: Mapping[str, ModelProfile] = field(default_factory=dict)
    display_name: str | None = None
    description: str | None = None
    conservative_unknown_models: bool = False

    def capabilities_for(self, model: str) -> ProviderCapabilities:
        profile = self.model_profiles.get(model)
        if profile is None:
            if self.conservative_unknown_models:
                return ProviderCapabilities(
                    reasoning_retention=self.default_capabilities.reasoning_retention,
                    reasoning_input_field=self.default_capabilities.reasoning_input_field,
                    reasoning_output_fields=self.default_capabilities.reasoning_output_fields,
                    requires_assistant_content_for_tool_calls=(
                        self.default_capabilities.requires_assistant_content_for_tool_calls
                    ),
                    working_tail_mode=self.default_capabilities.working_tail_mode,
                )
            return self.default_capabilities
        return profile.apply(self.default_capabilities)

    def model_metadata(self, model: str) -> dict[str, object]:
        profile = self.model_profiles.get(model)
        if profile is None:
            metadata: dict[str, object] = {
                "model_id": model,
                "display_name": model,
                "description": "模型能力尚未收录；可继续使用，但不会猜测 Thinking 或 Context Window。",
                "context_window_tokens": None,
                "known": False,
            }
        else:
            metadata = profile.metadata(model)
        capabilities = self.capabilities_for(model)
        metadata["thinking"] = capabilities.thinking.to_dict()
        return metadata


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
    thinking: ThinkingRequest | None = None
    extra_body: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ThinkingRequest:
    """Unified, validated thinking intent passed to a provider adapter."""

    enabled: bool
    budget_tokens: int | None = None
    keep: str | None = None
    intensity: str | None = None


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
    error_code: str | None = None
    retryable: bool = False
    type: Literal["error"] = field(default="error", init=False)


LLMEvent: TypeAlias = (
    TextDeltaEvent | ReasoningDeltaEvent | ToolCallDeltaEvent | MessageEndEvent | ErrorEvent
)

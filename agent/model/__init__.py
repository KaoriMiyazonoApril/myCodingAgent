"""Provider-independent LLM data structures and OpenAI-compatible adapter."""

from .openai_compatible import OpenAICompatibleProvider
from .presets import PROVIDER_PRESETS, create_provider_config
from .types import (
    LLMRequest,
    LLMResponse,
    Message,
    ProviderConfig,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolDefinition,
    ToolResultBlock,
    Usage,
)

__all__ = [
    "LLMRequest",
    "LLMResponse",
    "Message",
    "OpenAICompatibleProvider",
    "PROVIDER_PRESETS",
    "ProviderConfig",
    "ReasoningBlock",
    "TextBlock",
    "ToolCallBlock",
    "ToolDefinition",
    "ToolResultBlock",
    "Usage",
    "create_provider_config",
]

"""Provider-independent LLM data structures and OpenAI-compatible adapter."""

from .openai_compatible import OpenAICompatibleProvider
from .presets import PROVIDER_PRESETS, create_provider_config
from .types import (
    LLMRequest,
    LLMResponse,
    ModelProfile,
    ProviderCapabilities,
    ProviderConfig,
    ProviderProfile,
    ReasoningRetention,
    ThinkingCapabilities,
    Usage,
)

__all__ = [
    "LLMRequest",
    "LLMResponse",
    "ModelProfile",
    "OpenAICompatibleProvider",
    "PROVIDER_PRESETS",
    "ProviderCapabilities",
    "ProviderConfig",
    "ProviderProfile",
    "ReasoningRetention",
    "ThinkingCapabilities",
    "Usage",
    "create_provider_config",
]

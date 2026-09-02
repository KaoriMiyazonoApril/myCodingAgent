"""Provider-independent LLM data structures and OpenAI-compatible adapter."""

from .openai_compatible import OpenAICompatibleClientPool, OpenAICompatibleProvider
from .presets import PROVIDER_PRESETS, create_provider_config
from .types import (
    LLMRequest,
    LLMResponse,
    ModelProfile,
    ProviderCapabilities,
    ProviderConfig,
    ProviderProfile,
    ReasoningRetention,
    ThinkingParameterStyle,
    ThinkingRequest,
    ThinkingCapabilities,
    WorkingTailMode,
    Usage,
)

__all__ = [
    "LLMRequest",
    "LLMResponse",
    "ModelProfile",
    "OpenAICompatibleProvider",
    "OpenAICompatibleClientPool",
    "PROVIDER_PRESETS",
    "ProviderCapabilities",
    "ProviderConfig",
    "ProviderProfile",
    "ReasoningRetention",
    "ThinkingParameterStyle",
    "ThinkingRequest",
    "ThinkingCapabilities",
    "WorkingTailMode",
    "Usage",
    "create_provider_config",
]

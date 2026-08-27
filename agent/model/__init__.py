"""Provider-independent LLM data structures and OpenAI-compatible adapter."""

from .openai_compatible import OpenAICompatibleProvider
from .presets import PROVIDER_PRESETS, create_provider_config
from .types import (
    LLMRequest,
    LLMResponse,
    ProviderConfig,
    Usage,
)

__all__ = [
    "LLMRequest",
    "LLMResponse",
    "OpenAICompatibleProvider",
    "PROVIDER_PRESETS",
    "ProviderConfig",
    "Usage",
    "create_provider_config",
]

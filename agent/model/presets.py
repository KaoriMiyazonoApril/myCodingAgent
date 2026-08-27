"""Convenience presets; every preset still uses OpenAICompatibleProvider."""

from __future__ import annotations

from types import MappingProxyType

from .errors import LLMConfigurationError
from .types import (
    ProviderCapabilities,
    ProviderConfig,
    ProviderProfile,
    ReasoningRetention,
    ThinkingCapabilities,
)


DEEPSEEK_CAPABILITIES = ProviderCapabilities(
    reasoning_retention=ReasoningRetention.TOOL_CHAIN_ONLY,
    reasoning_input_field="reasoning_content",
    requires_assistant_content_for_tool_calls=True,
    thinking=ThinkingCapabilities(supported=True),
)

KIMI_CAPABILITIES = ProviderCapabilities(
    reasoning_retention=ReasoningRetention.ALWAYS,
    reasoning_input_field="reasoning_content",
    thinking=ThinkingCapabilities(
        supported=True,
        supported_keep_values=("none", "all"),
    ),
)

GLM_CAPABILITIES = ProviderCapabilities(
    reasoning_retention=ReasoningRetention.TOOL_CHAIN_ONLY,
    reasoning_input_field="reasoning_content",
    thinking=ThinkingCapabilities(supported=True),
)

PROVIDER_PRESETS = MappingProxyType(
    {
        "deepseek": ProviderProfile(
            base_url="https://api.deepseek.com",
            default_capabilities=DEEPSEEK_CAPABILITIES,
        ),
        "kimi": ProviderProfile(
            base_url="https://api.moonshot.cn/v1",
            default_capabilities=KIMI_CAPABILITIES,
        ),
        "moonshot": ProviderProfile(
            base_url="https://api.moonshot.cn/v1",
            default_capabilities=KIMI_CAPABILITIES,
        ),
        "glm": ProviderProfile(
            base_url="https://open.bigmodel.cn/api/paas/v4",
            default_capabilities=GLM_CAPABILITIES,
        ),
    }
)


def create_provider_config(
    provider: str,
    *,
    api_key: str,
    model: str,
    base_url: str | None = None,
    timeout: float | None = None,
    capabilities: ProviderCapabilities | None = None,
) -> ProviderConfig:
    """Create config from a known preset, while allowing endpoint overrides."""
    normalized_provider = provider.strip().lower()
    try:
        preset = PROVIDER_PRESETS[normalized_provider]
    except KeyError as error:
        choices = ", ".join(PROVIDER_PRESETS)
        raise LLMConfigurationError(
            f"Unsupported provider {provider!r}. Expected one of: {choices}"
        ) from error

    return ProviderConfig(
        provider=normalized_provider,
        base_url=base_url or preset.base_url,
        api_key=api_key,
        model=model,
        timeout=timeout,
        capabilities=capabilities or preset.capabilities_for(model),
    )

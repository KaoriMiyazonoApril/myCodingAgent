"""Convenience presets; every preset still uses OpenAICompatibleProvider."""

from __future__ import annotations

from types import MappingProxyType

from .errors import LLMConfigurationError
from .types import ProviderCapabilities, ProviderConfig


REASONING_TOOL_CALLS = ProviderCapabilities(
    preserve_reasoning_for_tool_calls=True,
    reasoning_input_field="reasoning_content",
)

DEEPSEEK_CAPABILITIES = ProviderCapabilities(
    preserve_reasoning_for_tool_calls=True,
    reasoning_input_field="reasoning_content",
    requires_assistant_content_for_tool_calls=True,
)


PROVIDER_PRESETS = MappingProxyType(
    {
        "deepseek": MappingProxyType(
            {
                "base_url": "https://api.deepseek.com",
                "capabilities": DEEPSEEK_CAPABILITIES,
            }
        ),
        "kimi": MappingProxyType(
            {
                "base_url": "https://api.moonshot.cn/v1",
                "capabilities": REASONING_TOOL_CALLS,
            }
        ),
        "moonshot": MappingProxyType(
            {
                "base_url": "https://api.moonshot.cn/v1",
                "capabilities": REASONING_TOOL_CALLS,
            }
        ),
        "glm": MappingProxyType(
            {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "capabilities": REASONING_TOOL_CALLS,
            }
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
        base_url=base_url or preset["base_url"],
        api_key=api_key,
        model=model,
        timeout=timeout,
        capabilities=preset["capabilities"],
    )

"""Convenience presets; every preset still uses OpenAICompatibleProvider."""

from __future__ import annotations

from types import MappingProxyType

from .types import ProviderConfig


PROVIDER_PRESETS = MappingProxyType(
    {
        "deepseek": MappingProxyType({"base_url": "https://api.deepseek.com"}),
        "kimi": MappingProxyType({"base_url": "https://api.moonshot.cn/v1"}),
        "moonshot": MappingProxyType({"base_url": "https://api.moonshot.cn/v1"}),
        "glm": MappingProxyType({"base_url": "https://open.bigmodel.cn/api/paas/v4"}),
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
        raise ValueError(f"Unsupported provider {provider!r}. Expected one of: {choices}") from error

    return ProviderConfig(
        provider=normalized_provider,
        base_url=base_url or preset["base_url"],
        api_key=api_key,
        model=model,
        timeout=timeout,
    )

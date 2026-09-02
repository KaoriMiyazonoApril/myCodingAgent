"""Convenience presets; every preset still uses OpenAICompatibleProvider."""

from __future__ import annotations

from types import MappingProxyType

from .errors import LLMConfigurationError
from .types import (
    ModelProfile,
    ProviderCapabilities,
    ProviderConfig,
    ProviderProfile,
    ReasoningRetention,
    ThinkingParameterStyle,
    ThinkingCapabilities,
    WorkingTailMode,
)


DEEPSEEK_CAPABILITIES = ProviderCapabilities(
    reasoning_retention=ReasoningRetention.TOOL_CHAIN_ONLY,
    reasoning_input_field="reasoning_content",
    requires_assistant_content_for_tool_calls=True,
    thinking=ThinkingCapabilities(supported=True),
    working_tail_mode=WorkingTailMode.STRUCTURED_USER_TAIL,
)

KIMI_CAPABILITIES = ProviderCapabilities(
    reasoning_retention=ReasoningRetention.ALWAYS,
    reasoning_input_field="reasoning_content",
    thinking=ThinkingCapabilities(
        supported=True,
        supported_keep_values=("none", "all"),
    ),
    working_tail_mode=WorkingTailMode.STRUCTURED_USER_TAIL,
)

GLM_CAPABILITIES = ProviderCapabilities(
    reasoning_retention=ReasoningRetention.TOOL_CHAIN_ONLY,
    reasoning_input_field="reasoning_content",
    thinking=ThinkingCapabilities(supported=True),
    working_tail_mode=WorkingTailMode.STRUCTURED_USER_TAIL,
)


def _deepseek_v4_profile(model_id: str, display_name: str) -> tuple[str, ModelProfile]:
    return model_id, ModelProfile(
        model_id=model_id,
        display_name=display_name,
        description="DeepSeek V4 思考模型，支持低/高/最大 Thinking 强度。",
        context_window_tokens=1_000_000,
        # 能力与请求策略分离：model_max_output_tokens 是官方硬上限（pricing
        # 页 384K），仅用于 clamp；default_request_max_tokens 是 Harness 内部
        # 请求策略（官方未显式给出 max_tokens 时默认上限较低，思考型任务会
        # 在写出正文前耗尽输出预算，故显式提高单次请求上限，避免长任务截断）。
        # 两者不可混用；官方 API 不存在 thinking budget_tokens 参数。
        model_max_output_tokens=384_000,
        default_request_max_tokens=131_072,
        thinking=ThinkingCapabilities(
            supported=True,
            default_enabled=True,
            toggle_supported=True,
            intensity_supported=True,
            intensity_options=("low", "high", "max"),
            default_intensity="high",
        ),
        thinking_parameter_style=ThinkingParameterStyle.DEEPSEEK_V4,
    )


def _kimi_profiles() -> dict[str, ModelProfile]:
    return {
        "kimi-k3": ModelProfile(
            model_id="kimi-k3",
            display_name="Kimi K3",
            description="Kimi K3，1M 上下文，Thinking 始终开启。",
            context_window_tokens=1_000_000,
            thinking=ThinkingCapabilities(
                supported=True,
                default_enabled=True,
                toggle_supported=False,
                intensity_supported=True,
                intensity_options=("low", "high", "max"),
                default_intensity="max",
            ),
            thinking_parameter_style=ThinkingParameterStyle.KIMI_REASONING_EFFORT,
        ),
        "kimi-k2.7-code": ModelProfile(
            model_id="kimi-k2.7-code",
            display_name="Kimi K2.7 Code",
            description="Kimi K2.7 Code，256K 上下文，Thinking 始终开启。",
            context_window_tokens=256_000,
            thinking=ThinkingCapabilities(
                supported=True,
                default_enabled=True,
                toggle_supported=False,
            ),
            thinking_parameter_style=ThinkingParameterStyle.KIMI_ALWAYS_ON,
        ),
        "kimi-k2.7-code-highspeed": ModelProfile(
            model_id="kimi-k2.7-code-highspeed",
            display_name="Kimi K2.7 Code Highspeed",
            description="Kimi K2.7 Code Highspeed，256K 上下文，Thinking 始终开启。",
            context_window_tokens=256_000,
            thinking=ThinkingCapabilities(
                supported=True,
                default_enabled=True,
                toggle_supported=False,
            ),
            thinking_parameter_style=ThinkingParameterStyle.KIMI_ALWAYS_ON,
        ),
        "kimi-k2.6": ModelProfile(
            model_id="kimi-k2.6",
            display_name="Kimi K2.6",
            description="Kimi K2.6，256K 上下文，Thinking 可关闭。",
            context_window_tokens=256_000,
            thinking=ThinkingCapabilities(
                supported=True,
                default_enabled=True,
                toggle_supported=True,
            ),
            thinking_parameter_style=ThinkingParameterStyle.KIMI_TOGGLE,
        ),
    }


def _glm_profiles() -> dict[str, ModelProfile]:
    return {
        "glm-5.3": ModelProfile(
            model_id="glm-5.3",
            display_name="GLM-5.3",
            description="GLM-5.3，1M 上下文，Thinking 始终开启。",
            context_window_tokens=1_000_000,
            thinking=ThinkingCapabilities(
                supported=True,
                default_enabled=True,
                toggle_supported=False,
                intensity_supported=True,
                intensity_options=("low", "high", "max"),
                default_intensity="max",
            ),
            thinking_parameter_style=ThinkingParameterStyle.GLM,
        ),
        "glm-5.2": ModelProfile(
            model_id="glm-5.2",
            display_name="GLM-5.2",
            description="GLM-5.2，1M 上下文，Thinking 可关闭并支持强度。",
            context_window_tokens=1_000_000,
            thinking=ThinkingCapabilities(
                supported=True,
                default_enabled=True,
                toggle_supported=True,
                intensity_supported=True,
                intensity_options=(
                    "none",
                    "minimal",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                ),
                default_intensity="max",
            ),
            thinking_parameter_style=ThinkingParameterStyle.GLM,
        ),
    }


_DEEPSEEK_MODELS = dict(
    (_deepseek_v4_profile("deepseek-v4-flash", "DeepSeek V4 Flash"),
     _deepseek_v4_profile("deepseek-v4-pro", "DeepSeek V4 Pro"))
)

PROVIDER_PRESETS = MappingProxyType(
    {
        "deepseek": ProviderProfile(
            base_url="https://api.deepseek.com",
            default_capabilities=DEEPSEEK_CAPABILITIES,
            model_profiles=_DEEPSEEK_MODELS,
            display_name="DeepSeek",
            description="DeepSeek 官方模型服务。",
            conservative_unknown_models=True,
        ),
        "kimi": ProviderProfile(
            base_url="https://api.moonshot.ai/v1",
            default_capabilities=KIMI_CAPABILITIES,
            model_profiles=_kimi_profiles(),
            display_name="Moonshot / Kimi",
            description="Moonshot / Kimi 官方模型服务。",
            conservative_unknown_models=True,
        ),
        "moonshot": ProviderProfile(
            base_url="https://api.moonshot.ai/v1",
            default_capabilities=KIMI_CAPABILITIES,
            model_profiles=_kimi_profiles(),
            display_name="Moonshot / Kimi",
            description="Moonshot / Kimi 官方模型服务。",
            conservative_unknown_models=True,
        ),
        "glm": ProviderProfile(
            base_url="https://open.bigmodel.cn/api/paas/v4",
            default_capabilities=GLM_CAPABILITIES,
            model_profiles=_glm_profiles(),
            display_name="GLM",
            description="智谱 GLM 官方模型服务。",
            conservative_unknown_models=True,
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

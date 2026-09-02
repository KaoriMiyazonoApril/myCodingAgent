from __future__ import annotations

import asyncio
import inspect
from unittest.mock import patch

import pytest

from agent.core.messages import Message, ReasoningBlock, TextBlock, ToolCallBlock
from agent.model import OpenAICompatibleProvider, create_provider_config
from agent.model.presets import PROVIDER_PRESETS
from agent.model.types import (
    LLMRequest,
    ProviderCapabilities,
    ReasoningRetention,
    ThinkingParameterStyle,
    ThinkingRequest,
)
from agent.runtime.context_budget import ContextBudget
from agent.runtime.events import EventBuffer, TurnEventEmitter, public_message
from agent.runtime.model_invoker import ModelInvoker, resolve_output_limit
from agent.runtime.settings import (
    ModelSettings,
    ThinkingSettings,
    TurnConfig,
    TurnSettingsOverride,
    _UNSET,
)
from agent.runtime.thread_runtime import ThreadRuntime
from agent.tools.registry import ToolRegistry
from agent.model.provider import LLMProvider
from agent.model.types import (
    LLMResponse,
    MessageEndEvent,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolCallDeltaEvent,
    Usage,
)


def _adapter(provider_id: str, model: str) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        create_provider_config(provider_id, api_key="test-key", model=model),
        client=object(),
    )


def _request(*, thinking: ThinkingRequest | None = None, max_tokens: int | None = None):
    return LLMRequest(
        messages=[Message(role="user", content=[TextBlock(text="hello")])],
        max_tokens=max_tokens,
        thinking=thinking,
    )


def test_unknown_exact_model_fails_closed_without_optional_protocol_fields() -> None:
    for provider_id, model in (
        ("deepseek", "deepseek-v4-unknown"),
        ("moonshot", "kimi-k9"),
        ("glm", "glm-5.3"),
    ):
        capabilities = PROVIDER_PRESETS[provider_id].capabilities_for(model)
        assert capabilities.reasoning_retention is ReasoningRetention.NEVER
        assert capabilities.reasoning_input_field is None
        assert capabilities.reasoning_output_fields == ()
        assert capabilities.thinking.supported is False
        assert capabilities.context_window_tokens is None
        assert capabilities.model_max_output_tokens is None
        assert capabilities.default_request_max_tokens is None
        assert capabilities.thinking_parameter_style is ThinkingParameterStyle.GENERIC

        payload = _adapter(provider_id, model)._build_request_payload(
            _request(
                thinking=ThinkingRequest(
                    enabled=True,
                    budget_tokens=100,
                    keep="all",
                    intensity="max",
                )
            ),
            stream=False,
        )
        assert "extra_body" not in payload


def test_exact_profiles_and_provider_wire_serialization_are_separate() -> None:
    deepseek = _adapter("deepseek", "deepseek-v4-pro")
    for requested, mapped in (
        ("low", "low"),
        ("high", "high"),
        ("max", "max"),
        ("medium", "high"),
        ("xhigh", "high"),
    ):
        payload = deepseek._build_request_payload(
            _request(thinking=ThinkingRequest(enabled=True, intensity=requested)),
            stream=False,
        )
        assert payload["extra_body"] == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": mapped,
        }
    disabled = deepseek._build_request_payload(
        _request(thinking=ThinkingRequest(enabled=False, intensity="high")),
        stream=False,
    )
    assert disabled["extra_body"] == {"thinking": {"type": "disabled"}}

    k3 = _adapter("moonshot", "kimi-k3")
    assert k3._build_request_payload(
        _request(thinking=ThinkingRequest(enabled=True, intensity="high"), max_tokens=123),
        stream=False,
    ) == {
        "model": "kimi-k3",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "max_completion_tokens": 123,
        "extra_body": {"reasoning_effort": "high"},
    }
    no_limit = k3._build_request_payload(_request(), stream=False)
    assert "max_tokens" not in no_limit
    assert "max_completion_tokens" not in no_limit

    k26 = _adapter("moonshot", "kimi-k2.6")
    assert k26._build_request_payload(
        _request(thinking=ThinkingRequest(enabled=True, keep="all")),
        stream=False,
    )["extra_body"] == {"thinking": {"type": "enabled", "keep": "all"}}

    glm = _adapter("glm", "glm-5.2")
    assert glm._build_request_payload(
        _request(thinking=ThinkingRequest(enabled=True, intensity="none")),
        stream=False,
    )["extra_body"] == {"thinking": {"type": "disabled"}}
    assert glm._build_request_payload(
        _request(thinking=ThinkingRequest(enabled=True, intensity="low")),
        stream=False,
    )["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }
    assert glm._build_request_payload(
        _request(thinking=ThinkingRequest(enabled=True, intensity="xhigh")),
        stream=False,
    )["extra_body"]["reasoning_effort"] == "max"


@pytest.mark.parametrize(
    "model",
    ("kimi-k2.7-code", "kimi-k2.7-code-highspeed"),
)
def test_kimi_k27_variants_omit_thinking_wire_field_and_replay_fixed_keep_all(
    model: str,
) -> None:
    """K2.7's fixed Preserved Thinking is activated by model selection.

    The official guide says neither K2.7 variant needs a ``thinking`` request
    parameter, and that omission is treated as ``keep=all``.  Keep the
    capability in the exact-model profile and replay historical reasoning,
    but do not manufacture an unconfirmed request field.
    """

    adapter = _adapter("moonshot", model)
    request = ThinkingRequest(enabled=True)
    payload = adapter._build_request_payload(
        _request(thinking=request),
        stream=False,
    )
    assert "extra_body" not in payload

    message = Message(
        role="assistant",
        content=[ReasoningBlock(text="private"), TextBlock(text="answer")],
    )
    encoded = adapter._encode_messages([message], thinking_request=request)
    assert encoded[0]["reasoning_content"] == "private"


def test_kimi_k26_keep_controls_reasoning_replay_without_affecting_canonical() -> None:
    message = Message(
        role="assistant",
        content=[ReasoningBlock(text="private"), TextBlock(text="answer")],
    )
    k26 = _adapter("moonshot", "kimi-k2.6")
    assert "reasoning_content" not in k26._encode_messages([message])[0]
    replay = k26._encode_messages(
        [message],
        thinking_request=ThinkingRequest(enabled=True, keep="all"),
    )[0]
    assert replay["reasoning_content"] == "private"


def test_output_policy_b_is_one_resolver_for_context_and_provider() -> None:
    capabilities = ProviderCapabilities(
        context_window_tokens=1_000_000,
        model_max_output_tokens=384_000,
        default_request_max_tokens=131_072,
    )
    assert resolve_output_limit(_UNSET, capabilities) == 131_072
    assert resolve_output_limit(None, capabilities) is None
    assert resolve_output_limit(500_000, capabilities) == 384_000
    assert resolve_output_limit(100_000, capabilities) == 100_000
    assert resolve_output_limit(_UNSET, ProviderCapabilities()) is None
    assert resolve_output_limit(100_000, ProviderCapabilities()) == 100_000

    budget = ContextBudget(
        context_window_tokens=capabilities.context_window_tokens,
        output_tokens=resolve_output_limit(_UNSET, capabilities),
    )
    assert budget.reserved_output_tokens == 131_072

    class CaptureProvider(LLMProvider):
        def __init__(self) -> None:
            self.capabilities = capabilities
            self.requests: list[LLMRequest] = []

        async def chat(self, request: LLMRequest) -> LLMResponse:
            self.requests.append(request)
            return LLMResponse(
                message=Message(
                    role="assistant", content=[TextBlock(text="done")]
                ),
                finish_reason="stop",
                usage=Usage(),
            )

    provider = CaptureProvider()
    config = TurnConfig.from_model_settings(
        ModelSettings(
            provider_config_id="p",
            model="m",
        ),
        settings_version=0,
        system_prompt="system",
        reasoning_visibility="hidden",
    )
    asyncio.run(
        ModelInvoker(
            provider,
            config,
            resolved_output_limit=resolve_output_limit(_UNSET, capabilities),
        ).chat([], [])
    )
    assert provider.requests[0].max_tokens == budget.reserved_output_tokens


def test_runtime_retry_and_approval_defaults_are_explicit() -> None:
    assert inspect.signature(ModelInvoker).parameters["retry_delays"].default == (
        0.5,
        1,
        2,
        4,
    )
    assert inspect.signature(ThreadRuntime).parameters[
        "model_retry_delays"
    ].default == (0.5, 1, 2, 4)
    assert inspect.signature(ThreadRuntime).parameters[
        "approval_timeout_seconds"
    ].default == 1_800


def test_explicit_none_turn_override_bypasses_default_for_context_and_provider(
    tmp_path,
) -> None:
    capabilities = ProviderCapabilities(
        context_window_tokens=1_000_000,
        model_max_output_tokens=384_000,
        default_request_max_tokens=131_072,
    )

    class CaptureProvider(LLMProvider):
        def __init__(self) -> None:
            self.capabilities = capabilities
            self.requests: list[LLMRequest] = []

        async def chat(self, request: LLMRequest) -> LLMResponse:
            self.requests.append(request)
            return LLMResponse(
                message=Message(
                    role="assistant", content=[TextBlock(text="done")]
                ),
                finish_reason="stop",
                usage=Usage(),
            )

    provider = CaptureProvider()
    runtime = ThreadRuntime(
        provider_resolver=lambda _provider, _model: provider,
        default_settings=ModelSettings(
            provider_config_id="provider",
            model="model",
        ),
        tool_registry_factory=lambda _workspace: ToolRegistry(),
    )
    thread = runtime.create_thread(tmp_path)
    captured_budgets = []
    captured_resolved_limits = []
    original_context_manager_for = runtime._context_manager_for

    def capture_context_manager(*args, **kwargs):
        manager = original_context_manager_for(*args, **kwargs)
        captured_budgets.append(manager.budget)
        captured_resolved_limits.append(kwargs["resolved_output_limit"])
        return manager

    with patch.object(
        runtime,
        "_context_manager_for",
        side_effect=capture_context_manager,
    ):
        asyncio.run(
            runtime.run_turn(
                thread.thread_id,
                "No output limit for this turn.",
                settings_override=TurnSettingsOverride(max_tokens=None),
            )
        )

    assert provider.requests[0].max_tokens is None
    assert len(captured_budgets) == 1
    assert captured_resolved_limits == [None]
    # Both sides received the same resolved None. ContextBudget's documented
    # no-limit fallback reserve remains bounded and deterministic.
    assert captured_budgets[0].reserved_output_tokens == 4_096


def test_public_projection_filters_reasoning_and_intermediate_narration() -> None:
    intermediate = Message(
        role="assistant",
        content=[
            ReasoningBlock(text="secret"),
            TextBlock(text="Inspecting files..."),
            ToolCallBlock(id="call-1", name="read_file", arguments={"path": "x"}),
        ],
    )
    final = Message(
        role="assistant",
        content=[ReasoningBlock(text="secret"), TextBlock(text="Final answer")],
    )
    assert public_message(intermediate) == {
        "schema_version": 1,
        "role": "assistant",
        "content": [
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "read_file",
                "arguments": {"path": "x"},
                "arguments_error": None,
                "raw_arguments": None,
            }
        ],
    }
    assert public_message(final)["content"] == [
        {"type": "text", "text": "Final answer"}
    ]


def test_reasoning_visibility_and_activity_phases_are_strict_and_bounded() -> None:
    def collect(visibility: str):
        buffer = EventBuffer(capacity=32)
        emitter = TurnEventEmitter(
            thread_id="thread",
            turn_id="turn",
            buffer=buffer,
            reasoning_visibility=visibility,
        )
        emitter.model_delta(ReasoningDeltaEvent(text="private"))
        emitter.model_delta(TextDeltaEvent(text="answer"))
        emitter.model_delta(ToolCallDeltaEvent(index=0, id="call"))
        emitter.model_delta(TextDeltaEvent(text="narration"))
        emitter.model_delta(MessageEndEvent(finish_reason="length", usage=Usage()))
        return buffer.read().events

    hidden = collect("hidden")
    assert [event.payload["phase"] for event in hidden if event.type == "model_activity"] == [
        "thinking",
        "generating",
        "idle",
    ]
    assert all(
        set(event.payload) == {"phase"}
        for event in hidden
        if event.type == "model_activity"
    )
    assert not any(event.type == "model_reasoning_delta" for event in hidden)

    debug = collect("debug")
    assert [event.payload["phase"] for event in debug if event.type == "model_activity"] == [
        "thinking",
        "generating",
        "idle",
    ]
    assert [
        event.payload["text"]
        for event in debug
        if event.type == "model_reasoning_delta"
    ] == ["private"]

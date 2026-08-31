from __future__ import annotations

import asyncio
import json
from dataclasses import fields
import sys
from types import SimpleNamespace

import pytest

from agent.core.messages import (
    Message,
    MessageValidationError,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agent.model.errors import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMRequestError,
)
from agent.model.openai_compatible import (
    OpenAICompatibleClientPool,
    OpenAICompatibleProvider,
)
from agent.model.presets import create_provider_config
from agent.model.types import (
    LLMRequest,
    ModelProfile,
    ProviderCapabilities,
    ProviderConfig,
    ProviderProfile,
    ReasoningRetention,
    ThinkingCapabilities,
)
from agent.tools.types import ToolDefinition, ToolResult


def provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderConfig(
            provider="deepseek",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            model="test-model",
        ),
        client=object(),
    )


def test_provider_close_releases_async_sdk_client() -> None:
    class Client:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    client = Client()
    configured = OpenAICompatibleProvider(
        ProviderConfig(
            provider="deepseek",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            model="test-model",
        ),
        client=client,
    )

    asyncio.run(configured.close())

    assert client.closed is True


def test_provider_client_pool_reuses_transport_across_model_adapters(monkeypatch) -> None:
    class Client:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    clients: list[Client] = []

    def create_client(config):
        del config
        client = Client()
        clients.append(client)
        return client

    monkeypatch.setattr(
        OpenAICompatibleProvider,
        "_create_client",
        staticmethod(create_client),
    )
    pool = OpenAICompatibleClientPool()
    first = pool.resolve(
        ProviderConfig(
            provider="deepseek",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            model="model-a",
        )
    )
    second = pool.resolve(
        ProviderConfig(
            provider="deepseek",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            model="model-b",
        )
    )

    assert pool.client_count == 1
    assert len(clients) == 1
    await_first = first.close()
    await_second = second.close()
    asyncio.run(await_first)
    asyncio.run(await_second)
    assert clients[0].close_calls == 0
    asyncio.run(pool.aclose())
    assert clients[0].close_calls == 1


def response(*, content: str | None, tool_calls=None, usage=None):
    message = SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=usage
    )


def test_plain_text_response_becomes_text_block() -> None:
    parsed = provider()._parse_response(response(content="hello"))

    assert parsed.message.role == "assistant"
    assert len(parsed.message.content) == 1
    assert isinstance(parsed.message.content[0], TextBlock)
    assert parsed.message.content[0].text == "hello"


def test_tool_call_arguments_decode_to_dict() -> None:
    tool_call = SimpleNamespace(
        id="call_123",
        function=SimpleNamespace(name="read_file", arguments='{"path":"main.py"}'),
    )

    parsed = provider()._parse_response(response(content=None, tool_calls=[tool_call]))

    assert parsed.message.content == [
        ToolCallBlock(
            id="call_123",
            name="read_file",
            raw_arguments='{"path":"main.py"}',
            arguments={"path": "main.py"},
        )
    ]


def test_invalid_tool_arguments_raise_clear_project_error() -> None:
    tool_call = SimpleNamespace(
        id="call_123",
        function=SimpleNamespace(name="read_file", arguments="{bad"),
    )

    parsed = provider()._parse_response(response(content=None, tool_calls=[tool_call]))

    assert parsed.message.content == [
        ToolCallBlock(
            id="call_123",
            name="read_file",
            raw_arguments="{bad",
            arguments=None,
            arguments_error="invalid JSON arguments",
        )
    ]

    encoded = provider()._encode_messages([parsed.message])
    assert encoded[0]["tool_calls"][0]["function"]["arguments"] == "{bad"


def test_one_invalid_tool_call_does_not_discard_other_calls() -> None:
    invalid = SimpleNamespace(
        id="call_bad",
        function=SimpleNamespace(name="read_file", arguments='{"path":'),
    )
    valid = SimpleNamespace(
        id="call_good",
        function=SimpleNamespace(name="read_file", arguments='{"path":"main.py"}'),
    )

    parsed = provider()._parse_response(
        response(content=None, tool_calls=[invalid, valid])
    )

    assert len(parsed.message.content) == 2
    assert parsed.message.content[0].arguments_error == "invalid JSON arguments"
    assert parsed.message.content[1].arguments == {"path": "main.py"}


def test_usage_is_converted_to_local_usage() -> None:
    parsed = provider()._parse_response(
        response(
            content="hello",
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )
    )

    assert parsed.usage.input_tokens == 11
    assert parsed.usage.output_tokens == 7
    assert parsed.usage.total_tokens == 18


def test_none_content_with_tool_calls_is_valid() -> None:
    tool_call = SimpleNamespace(
        id="call_123",
        function=SimpleNamespace(name="read_file", arguments=""),
    )

    parsed = provider()._parse_response(response(content=None, tool_calls=[tool_call]))

    assert len(parsed.message.content) == 1
    assert parsed.message.content[0].arguments == {}


def test_request_encoding_keeps_sdk_shapes_at_provider_boundary() -> None:
    request = LLMRequest(
        messages=[Message(role="user", content=[TextBlock(text="hello")])],
        tools=[
            ToolDefinition(
                name="read_file",
                description="Read one file",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ],
        temperature=0.2,
        max_tokens=100,
    )

    payload = provider()._build_request_payload(request, stream=False)

    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert payload["tools"][0]["function"]["name"] == "read_file"
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 100


def test_reasoning_is_preserved_for_thinking_tool_call_turns() -> None:
    deepseek = OpenAICompatibleProvider(
        create_provider_config(
            "deepseek", api_key="test-key", model="deepseek-v4-pro"
        ),
        client=object(),
    )
    request = LLMRequest(
        messages=[
            Message(
                role="assistant",
                content=[
                    ReasoningBlock(text="need to inspect the file"),
                    ToolCallBlock(
                        id="call_1", name="read_file", arguments={"path": "main.py"}
                    ),
                ],
            )
        ]
    )

    encoded = deepseek._build_request_payload(request, stream=False)["messages"][0]

    assert encoded["reasoning_content"] == "need to inspect the file"
    assert encoded["content"] == ""


def test_provider_defaults_and_model_overrides_resolve_capabilities() -> None:
    deepseek = create_provider_config(
        "deepseek", api_key="key", model="deepseek-v4-pro"
    )
    kimi = create_provider_config("kimi", api_key="key", model="model")
    glm = create_provider_config("glm", api_key="key", model="model")

    assert deepseek.capabilities.reasoning_retention is ReasoningRetention.TOOL_CHAIN_ONLY
    assert deepseek.capabilities.requires_assistant_content_for_tool_calls is True
    assert kimi.capabilities.reasoning_retention is ReasoningRetention.ALWAYS
    assert glm.capabilities.reasoning_retention is ReasoningRetention.TOOL_CHAIN_ONLY
    assert kimi.capabilities.requires_assistant_content_for_tool_calls is False


def test_model_profile_can_explicitly_disable_a_provider_default_field() -> None:
    profile = ProviderProfile(
        base_url="https://example.invalid",
        default_capabilities=ProviderCapabilities(
            reasoning_retention=ReasoningRetention.TOOL_CHAIN_ONLY,
            reasoning_input_field="reasoning_content",
        ),
        model_profiles={
            "plain": ModelProfile(
                reasoning_retention=ReasoningRetention.NEVER,
                reasoning_input_field=None,
            )
        },
    )

    resolved = profile.capabilities_for("plain")

    assert resolved.reasoning_retention is ReasoningRetention.NEVER
    assert resolved.reasoning_input_field is None


def test_model_profile_can_override_provider_thinking_capabilities() -> None:
    profile = ProviderProfile(
        base_url="https://example.invalid",
        default_capabilities=ProviderCapabilities(
            thinking=ThinkingCapabilities(supported=True),
        ),
        model_profiles={
            "plain": ModelProfile(thinking=ThinkingCapabilities()),
        },
    )

    assert profile.capabilities_for("plain").thinking.supported is False


def test_reasoning_output_fields_are_provider_configurable() -> None:
    configured = OpenAICompatibleProvider(
        ProviderConfig(
            provider="gateway",
            base_url="https://example.invalid/v1",
            api_key="key",
            model="model",
            capabilities=ProviderCapabilities(
                reasoning_output_fields=("reasoning_details",),
            ),
        ),
        client=object(),
    )
    raw_message = SimpleNamespace(
        role="assistant",
        content="answer",
        tool_calls=None,
        reasoning_details="private reasoning",
    )
    raw_response = SimpleNamespace(
        choices=[SimpleNamespace(message=raw_message, finish_reason="stop")],
        usage=None,
    )

    parsed = configured._parse_response(raw_response)

    assert parsed.message.content[0] == ReasoningBlock(text="private reasoning")


def test_deepseek_profiles_do_not_reference_retired_model_aliases() -> None:
    from agent.model.presets import PROVIDER_PRESETS

    assert "deepseek-chat" not in PROVIDER_PRESETS["deepseek"].model_profiles
    assert "deepseek-reasoner" not in PROVIDER_PRESETS["deepseek"].model_profiles


def test_reasoning_retention_supports_never_tool_chain_and_always() -> None:
    def encode(policy: ReasoningRetention, *, with_tool: bool) -> dict:
        configured = OpenAICompatibleProvider(
            ProviderConfig(
                provider="test",
                base_url="https://example.invalid/v1",
                api_key="key",
                model="model",
                capabilities=ProviderCapabilities(
                    reasoning_retention=policy,
                    reasoning_input_field="reasoning_content",
                ),
            ),
            client=object(),
        )
        content = [ReasoningBlock(text="reasoning")]
        if with_tool:
            content.append(ToolCallBlock(id="call", name="read_file", arguments={}))
        return configured._encode_messages([Message(role="assistant", content=content)])[0]

    assert "reasoning_content" not in encode(ReasoningRetention.NEVER, with_tool=True)
    assert (
        encode(ReasoningRetention.TOOL_CHAIN_ONLY, with_tool=True)["reasoning_content"]
        == "reasoning"
    )
    assert "reasoning_content" not in encode(
        ReasoningRetention.TOOL_CHAIN_ONLY, with_tool=False
    )
    assert (
        encode(ReasoningRetention.ALWAYS, with_tool=False)["reasoning_content"]
        == "reasoning"
    )


def test_tool_result_protocol_preserves_metadata_and_error_code_for_model() -> None:
    result = ToolResult(
        content="command failed",
        metadata={"exit_code": 2, "stderr_truncated": True},
        error_code="COMMAND_FAILED",
    )
    block = result.to_message_block("call_1")
    message = Message(role="tool", content=[block])

    encoded = provider()._encode_messages([message])
    envelope = json.loads(encoded[0]["content"])

    assert envelope == {
        "ok": False,
        "content": "command failed",
        "metadata": {"exit_code": 2, "stderr_truncated": True},
        "error_code": "COMMAND_FAILED",
    }
    assert block.is_error is True
    assert block.ok is False
    assert result.ok is False


def test_llm_errors_expose_retry_metadata() -> None:
    cases = [
        (LLMRequestError("bad request", status_code=400), 400, False),
        (LLMAuthenticationError("unauthorized", status_code=401), 401, False),
        (LLMRateLimitError("limited", status_code=429), 429, True),
        (LLMRequestError("server error", status_code=503), 503, True),
        (LLMConnectionError("timeout"), None, True),
    ]

    for error, status_code, retryable in cases:
        assert error.status_code == status_code
        assert error.retryable is retryable


def test_sdk_http_errors_translate_to_stable_retry_taxonomy(monkeypatch) -> None:
    class APIStatusError(Exception):
        def __init__(self, status_code: int) -> None:
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    class BadRequestError(APIStatusError):
        pass

    class AuthenticationError(APIStatusError):
        pass

    class RateLimitError(APIStatusError):
        pass

    class APIConnectionError(Exception):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(
            APIConnectionError=APIConnectionError,
            APIStatusError=APIStatusError,
            APITimeoutError=APITimeoutError,
            AuthenticationError=AuthenticationError,
            BadRequestError=BadRequestError,
            RateLimitError=RateLimitError,
        ),
    )

    translated = [
        provider()._translate_sdk_error(BadRequestError(400)),
        provider()._translate_sdk_error(AuthenticationError(401)),
        provider()._translate_sdk_error(RateLimitError(429)),
        provider()._translate_sdk_error(APIStatusError(503)),
        provider()._translate_sdk_error(APITimeoutError("timeout")),
    ]

    assert [(error.status_code, error.retryable) for error in translated] == [
        (400, False),
        (401, False),
        (429, True),
        (503, True),
        (None, True),
    ]
    assert all(error.provider == "deepseek" for error in translated)


def test_chat_or_stream_method_not_request_field_selects_transport_mode() -> None:
    assert "stream" not in {field.name for field in fields(LLMRequest)}


def test_domain_types_are_imported_from_core_and_tools_modules() -> None:
    assert Message.__module__ == "agent.core.messages"
    assert ToolDefinition.__module__ == "agent.tools.types"


def test_message_rejects_invalid_role_and_content_block_combinations() -> None:
    with pytest.raises(MessageValidationError, match="user.*TextBlock"):
        Message(
            role="user",
            content=[ToolCallBlock(id="call_1", name="read_file", arguments={})],
        )

    with pytest.raises(MessageValidationError, match="tool.*ToolResultBlock"):
        Message(role="tool", content=[TextBlock(text="not a tool result")])

    Message(
        role="tool",
        content=[ToolResultBlock(tool_call_id="call_1", content="file contents")],
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ProviderConfig(
            provider="", base_url="https://example.invalid", api_key="key", model="model"
        ),
        lambda: create_provider_config("unsupported", api_key="key", model="model"),
    ],
)
def test_configuration_errors_use_project_exception(factory) -> None:
    with pytest.raises(LLMConfigurationError):
        factory()


def test_invalid_reasoning_capabilities_are_configuration_errors() -> None:
    with pytest.raises(LLMConfigurationError, match="reasoning_retention"):
        ProviderCapabilities(reasoning_retention=True)  # type: ignore[arg-type]


@pytest.mark.parametrize("context_window", [0, True, -1])
def test_invalid_context_window_capabilities_are_configuration_errors(
    context_window,
) -> None:
    with pytest.raises(LLMConfigurationError, match="context_window_tokens"):
        ProviderCapabilities(context_window_tokens=context_window)


def test_model_profile_can_override_provider_context_window() -> None:
    profile = ProviderProfile(
        base_url="https://example.invalid",
        default_capabilities=ProviderCapabilities(context_window_tokens=32_000),
        model_profiles={
            "large": ModelProfile(context_window_tokens=128_000),
            "unknown": ModelProfile(context_window_tokens=None),
        },
    )

    assert profile.capabilities_for("plain").context_window_tokens == 32_000
    assert profile.capabilities_for("large").context_window_tokens == 128_000
    assert profile.capabilities_for("unknown").context_window_tokens is None

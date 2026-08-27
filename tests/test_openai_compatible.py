from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace

import pytest

from agent.model.errors import LLMConfigurationError, LLMRequestError, LLMToolArgumentsParseError
from agent.model.openai_compatible import OpenAICompatibleProvider
from agent.model.presets import create_provider_config
from agent.model.types import (
    LLMRequest,
    Message,
    ProviderConfig,
    TextBlock,
    ToolCallBlock,
    ToolDefinition,
    ToolResultBlock,
)


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
        ToolCallBlock(id="call_123", name="read_file", arguments={"path": "main.py"})
    ]


def test_invalid_tool_arguments_raise_clear_project_error() -> None:
    tool_call = SimpleNamespace(
        id="call_123",
        function=SimpleNamespace(name="read_file", arguments='{"path":'),
    )

    with pytest.raises(LLMToolArgumentsParseError, match="invalid JSON arguments"):
        provider()._parse_response(response(content=None, tool_calls=[tool_call]))


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


def test_chat_or_stream_method_not_request_field_selects_transport_mode() -> None:
    assert "stream" not in {field.name for field in fields(LLMRequest)}


def test_message_rejects_invalid_role_and_content_block_combinations() -> None:
    with pytest.raises(LLMRequestError, match="user.*TextBlock"):
        Message(
            role="user",
            content=[ToolCallBlock(id="call_1", name="read_file", arguments={})],
        )

    with pytest.raises(LLMRequestError, match="tool.*ToolResultBlock"):
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

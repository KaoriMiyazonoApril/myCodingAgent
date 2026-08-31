"""Adapter between local LLM types and OpenAI Chat Completions JSON."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import json
from collections.abc import AsyncIterator
import inspect
from typing import Any

from agent.core.messages import (
    ContentBlock,
    Message,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agent.tools.types import ToolDefinition

from .errors import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMRequestError,
    LLMResponseParseError,
    LLMStreamingNotImplementedError,
    LLMToolArgumentsParseError,
)
from .provider import LLMProvider
from .types import (
    ErrorEvent,
    LLMEvent,
    LLMRequest,
    LLMResponse,
    MessageEndEvent,
    ProviderConfig,
    ReasoningDeltaEvent,
    ReasoningRetention,
    TextDeltaEvent,
    ToolCallDeltaEvent,
    Usage,
)


class OpenAICompatibleProvider(LLMProvider):
    """One Chat Completions adapter for DeepSeek, Kimi/Moonshot, and GLM.

    ``client`` is an optional injection point for unit tests. Production code
    normally omits it and the provider creates the official ``AsyncOpenAI``
    HTTP client itself.
    """

    def __init__(
        self,
        config: ProviderConfig,
        client: Any | None = None,
        *,
        owns_client: bool | None = None,
    ) -> None:
        self.config = config
        self.capabilities = config.capabilities
        self._client = client if client is not None else self._create_client(config)
        # Injected clients remain owned by a standalone adapter for backwards
        # compatibility; pooled adapters explicitly pass owns_client=False.
        self._owns_client = True if owns_client is None else owns_client

    @staticmethod
    def _create_client(config: ProviderConfig) -> Any:
        try:
            from openai import AsyncOpenAI
        except ImportError as error:
            raise LLMConfigurationError(
                "The OpenAI Python SDK is required. Install dependencies with "
                "`pip install -r requirements.txt`."
            ) from error

        options: dict[str, Any] = {
            "api_key": config.api_key,
            "base_url": config.base_url,
        }
        if config.timeout is not None:
            options["timeout"] = config.timeout
        return AsyncOpenAI(**options)

    async def chat(self, request: LLMRequest) -> LLMResponse:
        """Create one non-streaming completion and translate it to local types."""
        payload = self._build_request_payload(request, stream=False)
        try:
            raw_response = await self._client.chat.completions.create(**payload)
        except Exception as error:
            raise self._translate_sdk_error(error) from error

        return self._parse_response(raw_response)

    async def close(self) -> None:
        """Release the owned SDK client when a Host lifecycle ends."""

        if not self._owns_client:
            return
        close = getattr(self._client, "close", None)
        if not callable(close):
            return
        result = close()
        if inspect.isawaitable(result):
            await result


    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        """Convert SDK Chat Completions chunks into provider-independent events."""

        payload = self._build_request_payload(request, stream=True)
        try:
            created = self._client.chat.completions.create(**payload)
            raw_stream = await created if inspect.isawaitable(created) else created
            if not hasattr(raw_stream, "__aiter__"):
                response = self._parse_response(raw_stream)
                raise LLMStreamingNotImplementedError(
                    "The injected completion client did not return an async stream",
                    fallback_response=response,
                )
            finish_reason: str | None = None
            usage: Usage | None = None
            call_indexes: dict[str, int] = {}
            next_index = 0
            async for chunk in raw_stream:
                raw_usage = self._get_field(chunk, "usage")
                if raw_usage is not None:
                    usage = self._parse_usage(raw_usage)
                choices = self._get_field(chunk, "choices")
                if choices is None:
                    continue
                if not isinstance(choices, (list, tuple)):
                    raise LLMResponseParseError("Streaming choices must be a list")
                if not choices:
                    continue
                choice = choices[0]
                raw_finish = self._get_field(choice, "finish_reason")
                if raw_finish is not None:
                    if not isinstance(raw_finish, str):
                        raise LLMResponseParseError(
                            "Streaming finish_reason must be a string or null"
                        )
                    finish_reason = raw_finish
                delta = self._get_field(choice, "delta")
                if delta is None:
                    continue
                text = self._parse_content(self._get_field(delta, "content"))
                if text:
                    yield TextDeltaEvent(text=text)
                for field_name in self.config.capabilities.reasoning_output_fields:
                    reasoning = self._get_field(delta, field_name)
                    if isinstance(reasoning, str) and reasoning:
                        yield ReasoningDeltaEvent(text=reasoning)
                raw_tool_calls = self._get_field(delta, "tool_calls")
                if raw_tool_calls is None:
                    continue
                if not isinstance(raw_tool_calls, (list, tuple)):
                    raise LLMResponseParseError(
                        "Streaming delta.tool_calls must be a list"
                    )
                for raw_call in raw_tool_calls:
                    raw_id = self._get_field(raw_call, "id")
                    call_id = raw_id if isinstance(raw_id, str) and raw_id else None
                    raw_index = self._get_field(raw_call, "index")
                    if raw_index is None:
                        if call_id is not None and call_id in call_indexes:
                            index = call_indexes[call_id]
                        else:
                            index = next_index
                    elif isinstance(raw_index, bool) or not isinstance(raw_index, int):
                        raise LLMResponseParseError(
                            "Streaming tool call index must be an integer"
                        )
                    else:
                        index = raw_index
                    if index < 0:
                        raise LLMResponseParseError(
                            "Streaming tool call index must not be negative"
                        )
                    next_index = max(next_index, index + 1)
                    if call_id is not None:
                        call_indexes[call_id] = index
                    function = self._get_field(raw_call, "function")
                    raw_name = self._get_field(function, "name")
                    name = raw_name if isinstance(raw_name, str) and raw_name else None
                    raw_arguments = self._get_field(function, "arguments")
                    arguments = (
                        raw_arguments if isinstance(raw_arguments, str) else None
                    )
                    if raw_arguments is not None and arguments is None:
                        arguments = self._stringify_raw_arguments(raw_arguments)
                    if call_id is not None or name is not None or arguments:
                        yield ToolCallDeltaEvent(
                            index=index,
                            id=call_id,
                            name=name,
                            arguments_delta=arguments,
                        )
            yield MessageEndEvent(finish_reason=finish_reason, usage=usage)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            translated = error if isinstance(error, LLMError) else self._translate_sdk_error(error)
            if isinstance(translated, LLMStreamingNotImplementedError):
                raise translated
            yield ErrorEvent(
                message=str(translated),
                error_code=type(translated).__name__,
                retryable=translated.retryable,
            )

    def _build_request_payload(self, request: LLMRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": self._encode_messages(request.messages),
            "stream": stream,
        }
        if request.tools:
            payload["tools"] = self._encode_tools(request.tools)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.extra_body:
            # The SDK passes this through for deliberately provider-specific needs.
            payload["extra_body"] = dict(request.extra_body)
        return payload

    def _encode_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        encoded: list[dict[str, Any]] = []
        for message in messages:
            encoded.extend(self._encode_message(message))
        return encoded

    def _encode_message(self, message: Message) -> list[dict[str, Any]]:
        if message.role == "tool":
            return self._encode_tool_results(message.content)

        text_parts = [block.text for block in message.content if isinstance(block, TextBlock)]
        reasoning_parts = [
            block.text for block in message.content if isinstance(block, ReasoningBlock)
        ]
        tool_calls = [block for block in message.content if isinstance(block, ToolCallBlock)]
        encoded: dict[str, Any] = {"role": message.role, "content": "".join(text_parts)}
        capabilities = self.config.capabilities
        if tool_calls:
            if text_parts or capabilities.requires_assistant_content_for_tool_calls:
                encoded["content"] = "".join(text_parts)
            else:
                encoded["content"] = None
            encoded["tool_calls"] = [self._encode_tool_call(call) for call in tool_calls]
        preserve_reasoning = capabilities.reasoning_retention is ReasoningRetention.ALWAYS or (
            capabilities.reasoning_retention is ReasoningRetention.TOOL_CHAIN_ONLY
            and bool(tool_calls)
        )
        if (
            reasoning_parts
            and preserve_reasoning
            and capabilities.reasoning_input_field is not None
        ):
            encoded[capabilities.reasoning_input_field] = "".join(reasoning_parts)
        return [encoded]

    @staticmethod
    def _encode_tool_call(call: ToolCallBlock) -> dict[str, Any]:
        if not call.id:
            raise LLMRequestError("ToolCallBlock.id must not be empty")
        if not call.name:
            raise LLMRequestError("ToolCallBlock.name must not be empty")
        if call.raw_arguments is not None:
            arguments = call.raw_arguments
        else:
            try:
                arguments = json.dumps(call.arguments, ensure_ascii=False)
            except (TypeError, ValueError) as error:
                raise LLMRequestError(
                    f"Arguments for tool call {call.name!r} are not JSON serializable"
                ) from error
        return {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": arguments},
        }

    @staticmethod
    def _encode_tool_results(content: list[ContentBlock]) -> list[dict[str, Any]]:
        results = [block for block in content if isinstance(block, ToolResultBlock)]
        if len(results) != len(content):
            raise LLMRequestError("A tool message may contain only ToolResultBlock values")
        if not results:
            raise LLMRequestError("A tool message must contain at least one ToolResultBlock")
        encoded: list[dict[str, Any]] = []
        for result in results:
            if not result.tool_call_id:
                raise LLMRequestError("ToolResultBlock.tool_call_id must not be empty")
            try:
                content = json.dumps(
                    {
                        "ok": result.ok,
                        "content": result.content,
                        "metadata": result.metadata,
                        "error_code": result.error_code,
                    },
                    ensure_ascii=False,
                )
            except (TypeError, ValueError) as error:
                raise LLMRequestError(
                    f"Tool result {result.tool_call_id!r} is not JSON serializable"
                ) from error
            encoded.append(
                {
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "content": content,
                }
            )
        return encoded

    @staticmethod
    def _encode_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]

    def _parse_response(self, raw_response: Any) -> LLMResponse:
        choices = self._get_field(raw_response, "choices")
        if not isinstance(choices, (list, tuple)) or not choices:
            raise LLMResponseParseError("Response has no completion choices")
        choice = choices[0]
        raw_message = self._get_field(choice, "message")
        if raw_message is None:
            raise LLMResponseParseError("Response choice has no message")

        role = self._get_field(raw_message, "role", "assistant")
        if role != "assistant":
            raise LLMResponseParseError(f"Expected assistant response role, got {role!r}")

        blocks: list[ContentBlock] = []
        reasoning = self._get_reasoning(raw_message)
        if reasoning:
            blocks.append(ReasoningBlock(text=reasoning))

        content = self._get_field(raw_message, "content")
        text = self._parse_content(content)
        if text is not None:
            blocks.append(TextBlock(text=text))

        raw_tool_calls = self._get_field(raw_message, "tool_calls")
        if raw_tool_calls is not None:
            if not isinstance(raw_tool_calls, (list, tuple)):
                raise LLMResponseParseError("message.tool_calls must be a list when present")
            blocks.extend(self._parse_tool_calls(raw_tool_calls))

        return LLMResponse(
            message=Message(role="assistant", content=blocks),
            finish_reason=self._get_field(choice, "finish_reason"),
            usage=self._parse_usage(self._get_field(raw_response, "usage")),
            raw=raw_response,
        )

    @staticmethod
    def _parse_content(content: Any) -> str | None:
        if content is None:
            return None
        if isinstance(content, str):
            return content
        # Some compatible services return the newer list-of-parts representation.
        if isinstance(content, list):
            text_parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return "".join(text_parts)
        raise LLMResponseParseError("message.content must be a string, list, or null")

    def _parse_tool_calls(self, raw_tool_calls: list[Any] | tuple[Any, ...]) -> list[ToolCallBlock]:
        parsed: list[ToolCallBlock] = []
        for index, raw_call in enumerate(raw_tool_calls):
            call_id = self._get_field(raw_call, "id")
            function = self._get_field(raw_call, "function")
            name = self._get_field(function, "name") if function is not None else None
            if not isinstance(call_id, str) or not call_id:
                raise LLMResponseParseError(f"Tool call at index {index} has no valid id")
            if not isinstance(name, str) or not name:
                raise LLMResponseParseError(f"Tool call {call_id!r} has no valid function name")
            raw_arguments_value = self._get_field(function, "arguments", "")
            raw_arguments = self._stringify_raw_arguments(raw_arguments_value)
            arguments_error: str | None = None
            try:
                arguments = self._parse_tool_arguments(
                    raw_arguments_value, call_id, name
                )
            except LLMToolArgumentsParseError as error:
                arguments = None
                arguments_error = self._tool_arguments_error_reason(error)
            parsed.append(
                ToolCallBlock(
                    id=call_id,
                    name=name,
                    raw_arguments=raw_arguments,
                    arguments=arguments,
                    arguments_error=arguments_error,
                )
            )
        return parsed

    @staticmethod
    def _stringify_raw_arguments(raw_arguments: Any) -> str:
        if isinstance(raw_arguments, str):
            return raw_arguments
        try:
            return json.dumps(raw_arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            return repr(raw_arguments)

    @staticmethod
    def _tool_arguments_error_reason(error: LLMToolArgumentsParseError) -> str:
        message = str(error)
        if "invalid JSON arguments" in message:
            return "invalid JSON arguments"
        if "must decode to a JSON object" in message:
            return "arguments must decode to a JSON object"
        return "arguments must be a JSON string"

    @staticmethod
    def _parse_tool_arguments(raw_arguments: Any, call_id: str, name: str) -> dict[str, Any]:
        if raw_arguments is None or raw_arguments == "":
            return {}
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if not isinstance(raw_arguments, str):
            raise LLMToolArgumentsParseError(
                f"Tool call {call_id!r} ({name}) arguments must be a JSON string"
            )
        try:
            decoded = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            raise LLMToolArgumentsParseError(
                f"Tool call {call_id!r} ({name}) has invalid JSON arguments: {error.msg}"
            ) from error
        if not isinstance(decoded, dict):
            raise LLMToolArgumentsParseError(
                f"Tool call {call_id!r} ({name}) arguments must decode to a JSON object"
            )
        return decoded

    @staticmethod
    def _parse_usage(raw_usage: Any) -> Usage:
        return Usage(
            input_tokens=OpenAICompatibleProvider._get_field(raw_usage, "prompt_tokens"),
            output_tokens=OpenAICompatibleProvider._get_field(raw_usage, "completion_tokens"),
            total_tokens=OpenAICompatibleProvider._get_field(raw_usage, "total_tokens"),
        )

    def _get_reasoning(self, raw_message: Any) -> str | None:
        for field_name in self.config.capabilities.reasoning_output_fields:
            value = self._get_field(raw_message, field_name)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _get_field(value: Any, name: str, default: Any = None) -> Any:
        if value is None:
            return default
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    def _translate_sdk_error(self, error: Exception) -> LLMError:
        """Map optional SDK exceptions to stable project exceptions."""
        try:
            from openai import (
                APIConnectionError,
                APIStatusError,
                APITimeoutError,
                AuthenticationError,
                BadRequestError,
                RateLimitError,
            )
        except ImportError:
            return LLMConnectionError(
                f"Model request failed: {error}", provider=self.config.provider
            )

        status_code = getattr(error, "status_code", None)
        provider = self.config.provider

        if isinstance(error, AuthenticationError):
            return LLMAuthenticationError(
                "Model provider rejected the API key",
                status_code=status_code or 401,
                provider=provider,
            )
        if isinstance(error, RateLimitError):
            return LLMRateLimitError(
                "Model provider rate limit exceeded",
                status_code=status_code or 429,
                provider=provider,
            )
        if isinstance(error, (APITimeoutError, APIConnectionError)):
            return LLMConnectionError(
                f"Could not reach model provider: {error}", provider=provider
            )
        if isinstance(error, BadRequestError):
            return LLMRequestError(
                f"Model provider rejected the request: {error}",
                status_code=status_code or 400,
                retryable=False,
                provider=provider,
            )
        if isinstance(error, APIStatusError):
            return LLMRequestError(
                f"Model provider rejected the request: {error}",
                status_code=status_code,
                provider=provider,
            )
        return LLMConnectionError(
            f"Model request failed: {error}", provider=provider
        )


class OpenAICompatibleClientPool:
    """Bounded transport pool for lightweight per-Turn provider adapters.

    Model and generation settings live on each adapter/configuration, while
    endpoint, credential identity and timeout determine the HTTP transport.
    Pooling at this provider-layer seam keeps SDK details below Runtime and
    avoids one connection pool per Turn.
    """

    def __init__(self, *, max_clients: int = 16) -> None:
        if (
            isinstance(max_clients, bool)
            or not isinstance(max_clients, int)
            or max_clients < 1
        ):
            raise ValueError("max_clients must be a positive integer")
        self._max_clients = max_clients
        self._clients: OrderedDict[tuple[object, ...], Any] = OrderedDict()
        self._retired_clients: list[Any] = []
        self._retired_close_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    @property
    def client_count(self) -> int:
        """Number of live transport clients retained by this pool."""

        return len(self._clients)

    def resolve(self, config: ProviderConfig) -> OpenAICompatibleProvider:
        """Return a model-specific adapter backed by a pooled transport."""

        if self._closed:
            raise LLMConfigurationError("Provider client pool is closed")
        key = self._transport_key(config)
        client = self._clients.get(key)
        if client is None:
            client = OpenAICompatibleProvider._create_client(config)
            self._clients[key] = client
            self._clients.move_to_end(key)
            while len(self._clients) > self._max_clients:
                _, retired = self._clients.popitem(last=False)
                self._schedule_close(retired)
        else:
            self._clients.move_to_end(key)
        return OpenAICompatibleProvider(config, client=client, owns_client=False)

    @staticmethod
    def _transport_key(config: ProviderConfig) -> tuple[object, ...]:
        # Do not retain the raw credential in the pool key; its digest still
        # distinguishes rotated credentials for transport isolation.
        credential_identity = hashlib.sha256(
            config.api_key.encode("utf-8")
        ).hexdigest()
        return (config.provider, config.base_url, config.timeout, credential_identity)

    def _schedule_close(self, client: Any) -> None:
        close = getattr(client, "close", None)
        if not callable(close):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._retired_clients.append(client)
            return
        result = close()
        if inspect.isawaitable(result):
            task = loop.create_task(result)
            self._retired_close_tasks.add(task)
            task.add_done_callback(self._retired_close_tasks.discard)

    async def aclose(self) -> None:
        """Close every retained transport exactly once."""

        if self._closed:
            return
        self._closed = True
        clients = tuple(self._clients.values()) + tuple(self._retired_clients)
        self._clients.clear()
        self._retired_clients.clear()
        for client in clients:
            close = getattr(client, "close", None)
            if not callable(close):
                continue
            result = close()
            if inspect.isawaitable(result):
                await result
        if self._retired_close_tasks:
            await asyncio.gather(*self._retired_close_tasks, return_exceptions=True)

"""Turn-scoped model invocation behind a small Agent Loop interface."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from agent.core.messages import Message
from agent.model.errors import (
    LLMConnectionError,
    LLMError,
    LLMResponseParseError,
    LLMStreamingNotImplementedError,
)
from agent.model.provider import LLMProvider
from agent.model.types import (
    ErrorEvent,
    LLMEvent,
    LLMRequest,
    LLMResponse,
    MessageEndEvent,
    ThinkingRequest,
)
from agent.tools.types import ToolDefinition

from .message_assembler import MessageAssembler
from .settings import TurnConfig


class ModelInvoker:
    """Apply one frozen Turn configuration to every model request."""

    def __init__(
        self,
        provider: LLMProvider,
        config: TurnConfig,
        *,
        retry_delays: tuple[float, ...] = (0.1, 0.2),
        default_context_window_tokens: int = 32_000,
    ) -> None:
        self._provider = provider
        self._config = config
        self._retry_delays = retry_delays
        # ContextManager owns request assembly and capacity checks. Keep the
        # old constructor option for embedders while making this invoker a
        # pure prepared-request executor.
        del default_context_window_tokens
        if config.thinking is not None:
            config.thinking.validate_for(provider.capabilities.thinking)

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
    ) -> LLMResponse:
        request = LLMRequest(
            messages=list(messages),
            tools=tools,
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
            thinking=(
                ThinkingRequest(
                    enabled=self._config.thinking.enabled,
                    budget_tokens=self._config.thinking.budget_tokens,
                    keep=(
                        None
                        if self._config.thinking.keep is None
                        else self._config.thinking.keep.value
                    ),
                    intensity=self._config.thinking.intensity,
                )
                if self._config.thinking is not None
                else None
            ),
        )
        for attempt in range(3):
            try:
                return await self._provider.chat(request)
            except LLMError as error:
                if not error.retryable or attempt == 2:
                    raise
                delay = (
                    self._retry_delays[attempt]
                    if attempt < len(self._retry_delays)
                    else 0
                )
                if delay > 0:
                    await asyncio.sleep(delay)
        raise AssertionError("model retry loop exhausted without returning or raising")

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        on_event: Callable[[LLMEvent], object] | None = None,
    ) -> LLMResponse:
        """Stream one response, retrying only before provisional output appears."""

        request = LLMRequest(
            messages=list(messages),
            tools=tools,
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
            thinking=(
                ThinkingRequest(
                    enabled=self._config.thinking.enabled,
                    budget_tokens=self._config.thinking.budget_tokens,
                    keep=(
                        None
                        if self._config.thinking.keep is None
                        else self._config.thinking.keep.value
                    ),
                    intensity=self._config.thinking.intensity,
                )
                if self._config.thinking is not None
                else None
            ),
        )
        if type(self._provider).stream is LLMProvider.stream:
            # Preserve the established chat seam for providers that have not
            # opted into streaming yet. Their complete response is already
            # canonical and must not manufacture duplicate UI deltas.
            return await self.chat(messages, tools)

        for attempt in range(3):
            delta_seen = False
            error_event_delivered = False
            try:
                assembler = MessageAssembler()
                async for event in self._provider.stream(request):
                    if isinstance(event, ErrorEvent):
                        error = self._error_from_event(event)
                        should_retry = (
                            not delta_seen
                            and error.retryable
                            and attempt < 2
                        )
                        if on_event is not None and not should_retry:
                            on_event(event)
                            error_event_delivered = True
                        raise error
                    assembler.add(event)
                    if not isinstance(event, MessageEndEvent):
                        delta_seen = delta_seen or assembler.delta_seen
                    if on_event is not None:
                        on_event(event)
                return assembler.assemble()
            except asyncio.CancelledError:
                raise
            except LLMError as error:
                terminal_error = delta_seen or not error.retryable or attempt == 2
                if on_event is not None and terminal_error and not error_event_delivered:
                    on_event(
                        ErrorEvent(
                            message=str(error),
                            error_code=type(error).__name__,
                            retryable=error.retryable,
                        )
                    )
                if isinstance(error, LLMStreamingNotImplementedError):
                    fallback_response = getattr(error, "fallback_response", None)
                    if isinstance(fallback_response, LLMResponse):
                        return fallback_response
                    return await self.chat(messages, tools)
                if terminal_error:
                    raise
                delay = (
                    self._retry_delays[attempt]
                    if attempt < len(self._retry_delays)
                    else 0
                )
                if delay > 0:
                    await asyncio.sleep(delay)
        raise AssertionError("model stream retry loop exhausted")

    def _error_from_event(self, event: ErrorEvent) -> LLMError:
        if event.error_code == "LLMConnectionError":
            return LLMConnectionError(event.message, retryable=event.retryable)
        if event.error_code == "LLMResponseParseError":
            return LLMResponseParseError(event.message)
        if event.error_code == "LLMStreamingNotImplementedError":
            return LLMStreamingNotImplementedError(event.message)
        return LLMConnectionError(event.message, retryable=event.retryable)

    def ensure_context(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
    ) -> None:
        """Retained compatibility hook; ContextManager owns this check now."""

        del messages, tools

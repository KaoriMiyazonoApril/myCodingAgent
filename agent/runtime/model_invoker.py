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
    ProviderCapabilities,
    ThinkingRequest,
)
from agent.tools.types import ToolDefinition

from .message_assembler import MessageAssembler
from .settings import TurnConfig, _UNSET


_UNSET_RESOLVED_OUTPUT_LIMIT = object()


def resolve_output_limit(
    explicit_max_tokens: int | None | object,
    capabilities: ProviderCapabilities | None,
) -> int | None:
    """Resolve the single effective per-request output limit.

    Capability and policy stay separate: the model's officially verified
    maximum output only clamps; the Harness-internal default request limit
    only applies when the Thread carries no explicit override.  The settings
    sentinel means "no override"; an explicit ``None`` means "omit
    max_tokens" and therefore must not fall back to the Harness default.
    """
    if explicit_max_tokens is not _UNSET and explicit_max_tokens is not None and (
        isinstance(explicit_max_tokens, bool)
        or not isinstance(explicit_max_tokens, int)
        or explicit_max_tokens <= 0
    ):
        raise ValueError(
            "explicit_max_tokens must be a positive integer, None, or unset"
        )

    model_max = (
        None if capabilities is None else capabilities.model_max_output_tokens
    )
    request_default = (
        None
        if capabilities is None
        else capabilities.default_request_max_tokens
    )
    if explicit_max_tokens is _UNSET:
        base = request_default
    elif explicit_max_tokens is None:
        return None
    else:
        base = explicit_max_tokens
    if base is None:
        return None
    if model_max is not None:
        return min(base, model_max)
    return base


class ModelInvoker:
    """Apply one frozen Turn configuration to every model request."""

    def __init__(
        self,
        provider: LLMProvider,
        config: TurnConfig,
        *,
        resolved_output_limit: int | None | object = _UNSET_RESOLVED_OUTPUT_LIMIT,
        retry_delays: tuple[float, ...] = (0.5, 1, 2, 4),
        default_context_window_tokens: int = 32_000,
    ) -> None:
        self._provider = provider
        self._config = config
        self._retry_delays = retry_delays
        # ContextManager owns request assembly and capacity checks. Keep the
        # old constructor option for embedders while making this invoker a
        # pure prepared-request executor.
        del default_context_window_tokens
        # One resolver feeds both the ContextBudget reserve and the provider
        # request so the two can never diverge.  Embedders that already
        # computed the limit pass it explicitly; standalone construction
        # recomputes it from the frozen config and the provider capabilities.
        if resolved_output_limit is _UNSET_RESOLVED_OUTPUT_LIMIT:
            explicit_max_tokens = (
                _UNSET if config.max_tokens is None else config.max_tokens
            )
            resolved: int | None = resolve_output_limit(
                explicit_max_tokens,
                provider.capabilities,
            )
        else:
            if resolved_output_limit is not None and (
                isinstance(resolved_output_limit, bool)
                or not isinstance(resolved_output_limit, int)
                or resolved_output_limit <= 0
            ):
                raise ValueError(
                    "resolved_output_limit must be a positive integer or None"
                )
            # The explicit None is meaningful: it is the resolver's decision
            # to omit the provider output-limit field, not a request to
            # recompute a model default in this executor.
            resolved = resolved_output_limit
        self._resolved_output_limit: int | None = resolved
        if config.thinking is not None:
            config.thinking.validate_for(provider.capabilities.thinking)

    def _thinking_request(self) -> ThinkingRequest | None:
        """Map frozen ThinkingSettings to a provider request 1:1.

        Nothing is synthesized here: a provider's documented default-on
        behavior is honored by omitting the parameters entirely, and no
        default thinking budget is invented for any provider.
        """

        settings = self._config.thinking
        if settings is None:
            return None
        return ThinkingRequest(
            enabled=settings.enabled,
            budget_tokens=settings.budget_tokens,
            keep=(
                None
                if settings.keep is None
                else settings.keep.value
            ),
            intensity=settings.intensity,
        )

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
    ) -> LLMResponse:
        request = LLMRequest(
            messages=list(messages),
            tools=tools,
            temperature=self._config.temperature,
            max_tokens=self._resolved_output_limit,
            thinking=self._thinking_request(),
        )
        for attempt in range(5):
            try:
                return await self._provider.chat(request)
            except LLMError as error:
                if not error.retryable or attempt == 4:
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
            max_tokens=self._resolved_output_limit,
            thinking=self._thinking_request(),
        )
        if type(self._provider).stream is LLMProvider.stream:
            # Preserve the established chat seam for providers that have not
            # opted into streaming yet. Their complete response is already
            # canonical and must not manufacture duplicate UI deltas.
            return await self.chat(messages, tools)

        for attempt in range(5):
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
                            and attempt < 4
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
                terminal_error = delta_seen or not error.retryable or attempt == 4
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

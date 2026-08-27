"""Turn-scoped model invocation behind a small Agent Loop interface."""

from __future__ import annotations

import asyncio

from agent.core.messages import Message
from agent.model.errors import LLMError
from agent.model.provider import LLMProvider
from agent.model.types import LLMRequest, LLMResponse
from agent.tools.types import ToolDefinition

from .settings import TurnConfig


class ModelInvoker:
    """Apply one frozen Turn configuration to every model request."""

    def __init__(
        self,
        provider: LLMProvider,
        config: TurnConfig,
        *,
        retry_delays: tuple[float, ...] = (0.1, 0.2),
    ) -> None:
        self._provider = provider
        self._config = config
        self._retry_delays = retry_delays
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
            extra_body=(
                self._config.thinking.to_extra_body()
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

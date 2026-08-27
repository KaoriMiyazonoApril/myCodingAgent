"""Turn-scoped model invocation behind a small Agent Loop interface."""

from __future__ import annotations

from agent.core.messages import Message
from agent.model.provider import LLMProvider
from agent.model.types import LLMRequest, LLMResponse
from agent.tools.types import ToolDefinition

from .settings import TurnConfig


class ModelInvoker:
    """Apply one frozen Turn configuration to every model request."""

    def __init__(self, provider: LLMProvider, config: TurnConfig) -> None:
        self._provider = provider
        self._config = config

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
    ) -> LLMResponse:
        return await self._provider.chat(
            LLMRequest(
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
        )

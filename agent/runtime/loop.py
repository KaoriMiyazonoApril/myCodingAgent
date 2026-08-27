"""The deliberately small reasoning-to-tools Agent Loop."""

from __future__ import annotations

from dataclasses import dataclass

from agent.core.messages import Message, TextBlock, ToolCallBlock
from agent.model.provider import LLMProvider
from agent.model.types import LLMRequest
from agent.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    final_text: str
    iterations: int
    tool_calls: int


class AgentLoop:
    """Run complete model responses and ordered tools until the model finishes."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def run(
        self, messages: list[Message], tools: ToolRegistry
    ) -> LoopOutcome:
        iterations = 0
        tool_call_count = 0

        while True:
            response = await self._provider.chat(
                LLMRequest(messages=list(messages), tools=tools.definitions())
            )
            iterations += 1
            messages.append(response.message)

            tool_calls = [
                block
                for block in response.message.content
                if isinstance(block, ToolCallBlock)
            ]
            if not tool_calls:
                return LoopOutcome(
                    final_text="".join(
                        block.text
                        for block in response.message.content
                        if isinstance(block, TextBlock)
                    ),
                    iterations=iterations,
                    tool_calls=tool_call_count,
                )

            for call in tool_calls:
                result = await tools.execute_async(call)
                messages.append(
                    Message(
                        role="tool",
                        content=[result.to_message_block(call.id)],
                    )
                )
                tool_call_count += 1


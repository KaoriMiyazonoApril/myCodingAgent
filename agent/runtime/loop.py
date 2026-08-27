"""The deliberately small reasoning-to-tools Agent Loop."""

from __future__ import annotations

from dataclasses import dataclass

from agent.core.messages import TextBlock, ToolCallBlock
from agent.tools.registry import ToolRegistry

from .conversation import Conversation
from .model_invoker import ModelInvoker


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    final_text: str
    iterations: int
    tool_calls: int


class AgentLoop:
    """Run complete model responses and ordered tools until the model finishes."""

    async def run(
        self,
        conversation: Conversation,
        tools: ToolRegistry,
        model: ModelInvoker,
    ) -> LoopOutcome:
        iterations = 0
        tool_call_count = 0

        while True:
            response = await model.chat(
                conversation.request_messages(),
                tools.definitions(),
            )
            iterations += 1
            conversation.append_assistant(response.message)

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
                conversation.append_tool_result(result.to_message_block(call.id))
                tool_call_count += 1

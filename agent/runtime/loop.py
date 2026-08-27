"""The deliberately small reasoning-to-tools Agent Loop."""

from __future__ import annotations

from dataclasses import dataclass

from agent.core.messages import TextBlock, ToolCallBlock
from agent.tools.registry import ToolRegistry

from .conversation import Conversation
from .events import TurnEventEmitter
from .model_invoker import ModelInvoker


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    final_text: str
    iterations: int
    tool_calls: int
    usage: dict[str, int | None]


class AgentLoop:
    """Run complete model responses and ordered tools until the model finishes."""

    async def run(
        self,
        conversation: Conversation,
        tools: ToolRegistry,
        model: ModelInvoker,
        events: TurnEventEmitter,
    ) -> LoopOutcome:
        iterations = 0
        tool_call_count = 0
        usage_totals: dict[str, int | None] = {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }

        while True:
            response = await model.chat(
                conversation.request_messages(),
                tools.definitions(),
            )
            iterations += 1
            conversation.append_assistant(response.message)
            for name in usage_totals:
                value = getattr(response.usage, name)
                if value is not None:
                    usage_totals[name] = (usage_totals[name] or 0) + value
            events.model_response(response, iterations)

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
                    usage=usage_totals,
                )

            for call in tool_calls:
                events.tool_requested(call)
                events.tool_started(call)
                result = await tools.execute_async(call)
                conversation.append_tool_result(result.to_message_block(call.id))
                tool_call_count += 1
                events.tool_finished(call, result)

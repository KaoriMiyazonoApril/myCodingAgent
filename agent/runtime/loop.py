"""The deliberately small reasoning-to-tools Agent Loop."""

from __future__ import annotations

from dataclasses import dataclass

from agent.core.messages import TextBlock, ToolCallBlock

from .conversation import Conversation
from .events import TurnEventEmitter
from .model_invoker import ModelInvoker
from .run_controller import RunController
from .tool_coordinator import ToolCoordinator


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    final_text: str


class AgentLoop:
    """Run complete model responses and ordered tools until the model finishes."""

    async def run(
        self,
        conversation: Conversation,
        tools: ToolCoordinator,
        model: ModelInvoker,
        events: TurnEventEmitter,
        controller: RunController,
    ) -> LoopOutcome:
        while True:
            controller.begin_iteration()
            response = await controller.wait(
                model.stream(
                    conversation.request_messages(),
                    tools.definitions(),
                    on_event=events.model_delta,
                )
            )
            conversation.append_assistant(response.message)
            assistant_text = "".join(
                block.text
                for block in response.message.content
                if isinstance(block, TextBlock)
            )
            controller.record_model_response(response.usage, assistant_text)
            events.model_response(response, controller.iterations)

            tool_calls = [
                block
                for block in response.message.content
                if isinstance(block, ToolCallBlock)
            ]
            if not tool_calls:
                return LoopOutcome(final_text=assistant_text)

            await tools.execute(tool_calls)

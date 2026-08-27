"""The deliberately small reasoning-to-tools Agent Loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agent.core.messages import TextBlock, ToolCallBlock
from agent.tools.registry import ToolRegistry
from agent.tools.types import ToolResult

from .conversation import Conversation
from .events import TurnEventEmitter
from .errors import TurnLimitReached
from .model_invoker import ModelInvoker
from .run_controller import RunController


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    final_text: str


class AgentLoop:
    """Run complete model responses and ordered tools until the model finishes."""

    async def run(
        self,
        conversation: Conversation,
        tools: ToolRegistry,
        model: ModelInvoker,
        events: TurnEventEmitter,
        controller: RunController,
    ) -> LoopOutcome:
        while True:
            controller.begin_iteration()
            response = await controller.wait(
                model.chat(
                    conversation.request_messages(),
                    tools.definitions(),
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

            for index, call in enumerate(tool_calls):
                events.tool_requested(call)
                try:
                    controller.begin_tool()
                except TurnLimitReached:
                    self._append_skipped_results(
                        conversation,
                        events,
                        tool_calls[index:],
                        reason="tool call budget reached",
                        first_request_emitted=True,
                    )
                    raise
                events.tool_started(call)
                try:
                    result = await controller.wait(tools.execute_async(call))
                except TurnLimitReached:
                    result = ToolResult(
                        content="tool cancelled because the Turn execution deadline was reached",
                        metadata={},
                        error_code="LIMIT_REACHED",
                    )
                    self._record_tool_result(conversation, events, call, result)
                    self._append_skipped_results(
                        conversation,
                        events,
                        tool_calls[index + 1 :],
                        reason="Turn execution deadline reached",
                    )
                    raise
                except asyncio.CancelledError:
                    result = ToolResult(
                        content="tool cancelled with its active Turn",
                        metadata={},
                        error_code="CANCELLED",
                    )
                    self._record_tool_result(conversation, events, call, result)
                    self._append_skipped_results(
                        conversation,
                        events,
                        tool_calls[index + 1 :],
                        reason="Turn cancelled",
                        error_code="CANCELLED",
                    )
                    raise
                self._record_tool_result(conversation, events, call, result)
                try:
                    controller.record_tool_result(call, result)
                except TurnLimitReached:
                    self._append_skipped_results(
                        conversation,
                        events,
                        tool_calls[index + 1 :],
                        reason="repeated tool failure limit reached",
                    )
                    raise

    @staticmethod
    def _append_skipped_results(
        conversation: Conversation,
        events: TurnEventEmitter,
        calls: list[ToolCallBlock],
        *,
        reason: str,
        error_code: str = "LIMIT_REACHED",
        first_request_emitted: bool = False,
    ) -> None:
        for index, call in enumerate(calls):
            if index > 0 or not first_request_emitted:
                events.tool_requested(call)
            result = ToolResult(
                content=reason,
                metadata={"executed": False},
                error_code=error_code,
            )
            AgentLoop._record_tool_result(conversation, events, call, result)

    @staticmethod
    def _record_tool_result(
        conversation: Conversation,
        events: TurnEventEmitter,
        call: ToolCallBlock,
        result: ToolResult,
    ) -> None:
        conversation.append_tool_result(result.to_message_block(call.id))
        events.tool_finished(call, result)

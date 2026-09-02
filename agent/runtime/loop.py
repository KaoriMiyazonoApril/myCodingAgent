"""The deliberately small reasoning-to-tools Agent Loop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agent.core.messages import Message, TextBlock, ToolCallBlock

from .conversation import Conversation
from .context_manager import ContextManager
from .runtime_environment import RuntimeContext
from .task_state import TaskState
from .history import AsyncHistoryCompactor, CompactionCheckpoint
from .events import TurnEventEmitter
from .errors import TurnLimitReached
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
        *,
        context_manager: ContextManager | None = None,
        runtime_context_factory: Callable[[], RuntimeContext] | None = None,
        task_state: TaskState | None = None,
        current_input: str | Message | None = None,
        compaction_checkpoint: CompactionCheckpoint | None = None,
        history_compactor: AsyncHistoryCompactor | None = None,
        checkpoint_sink: Callable[[CompactionCheckpoint], None] | None = None,
        context_diagnostics_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> LoopOutcome:
        pending_input = current_input
        current_checkpoint = compaction_checkpoint
        while True:
            controller.begin_iteration()
            if context_manager is None:
                messages = conversation.request_messages()
            else:
                plan, next_checkpoint = await controller.wait(
                    context_manager.assemble_with_reduction(
                        conversation.canonical_messages(),
                        runtime_context=(
                            None
                            if runtime_context_factory is None
                            else runtime_context_factory()
                        ),
                        task_state=task_state,
                        current_input=pending_input,
                        tools=tools.definitions(),
                        checkpoint=current_checkpoint,
                        semantic_compactor=history_compactor,
                    )
                )
                if next_checkpoint is not None and next_checkpoint != current_checkpoint:
                    current_checkpoint = next_checkpoint
                    if checkpoint_sink is not None:
                        checkpoint_sink(next_checkpoint)
                if context_diagnostics_sink is not None:
                    context_diagnostics_sink(
                        dict(plan.decision_metadata)
                    )
                messages = context_manager.render(plan)
            response = await controller.wait(
                model.stream(
                    messages,
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
            pending_input = None

            tool_calls = [
                block
                for block in response.message.content
                if isinstance(block, ToolCallBlock)
            ]
            if not tool_calls:
                # A response without tool calls ends the Turn, but a silent
                # truncation must not masquerade as successful completion.
                if response.finish_reason == "length":
                    raise TurnLimitReached("output_length")
                if not assistant_text:
                    raise TurnLimitReached("empty_response")
                return LoopOutcome(final_text=assistant_text)

            await tools.execute(tool_calls)

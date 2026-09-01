"""Ordered, provider-independent conversation history."""

from __future__ import annotations

from copy import deepcopy

from agent.core.messages import Message, TextBlock, ToolCallBlock, ToolResultBlock
from agent.tools.result_bounds import reduce_tool_result_block

from .events import public_message


class Conversation:
    """Own legal message construction while exposing request snapshots."""

    def __init__(self, system_prompt: str) -> None:
        self._messages = [
            Message(role="system", content=[TextBlock(text=system_prompt)])
        ]

    @classmethod
    def from_messages(cls, messages: list[Message]) -> Conversation:
        """Restore canonical provider history from a detached store snapshot."""

        if not messages or messages[0].role != "system":
            raise ValueError("conversation must begin with a system message")
        conversation = cls.__new__(cls)
        conversation._messages = deepcopy(messages)
        return conversation

    def append_user(self, text: str) -> None:
        self._messages.append(Message(role="user", content=[TextBlock(text=text)]))

    def append_assistant(self, message: Message) -> None:
        if message.role != "assistant":
            raise ValueError("model response must have the assistant role")
        self._messages.append(message)

    def append_tool_result(self, result: ToolResultBlock) -> None:
        # Layer-1 reduction happens before a result enters canonical history,
        # so every future detached snapshot is already hard-bounded.
        bounded = reduce_tool_result_block(result)
        self._messages.append(Message(role="tool", content=[bounded]))

    def request_messages(self) -> list[Message]:
        return list(self._messages)

    def canonical_messages(self) -> list[Message]:
        """Return detached system/user/assistant/tool history for persistence."""

        return deepcopy(self._messages)

    def append_interrupted_tool_results(self) -> list[str]:
        """Close tool-call history without re-executing calls after a restart."""

        pending: list[str] = []
        completed: set[str] = set()
        for message in self._messages:
            if message.role == "assistant":
                pending.extend(
                    block.id
                    for block in message.content
                    if isinstance(block, ToolCallBlock)
                )
            elif message.role == "tool":
                completed.update(
                    block.tool_call_id
                    for block in message.content
                    if isinstance(block, ToolResultBlock)
                )
        pending = [call_id for call_id in pending if call_id not in completed]
        for call_id in pending:
            self.append_tool_result(
                ToolResultBlock(
                    tool_call_id=call_id,
                    content=(
                        "tool call was interrupted by Runtime restart; execution "
                        "outcome is unknown and side effects may already have "
                        "occurred. Inspect workspace/state before retrying."
                    ),
                    metadata={
                        "execution_status": "unknown",
                        "reason_code": "RUNTIME_RESTARTED",
                        "side_effects_possible": True,
                    },
                    error_code="RUNTIME_RESTARTED",
                )
            )
        return pending

    def prospective_request_messages(self, user_text: str) -> list[Message]:
        """Return the first request for a possible Turn without mutating history."""

        return [
            *self._messages,
            Message(role="user", content=[TextBlock(text=user_text)]),
        ]

    def public_messages(self) -> list[dict[str, object]]:
        """Return a detached transcript without system prompts or reasoning."""

        return [
            serialized
            for message in self._messages
            if (serialized := public_message(message)) is not None
        ]

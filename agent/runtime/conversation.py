"""Ordered, provider-independent conversation history."""

from __future__ import annotations

from agent.core.messages import Message, TextBlock, ToolResultBlock


class Conversation:
    """Own legal message construction while exposing request snapshots."""

    def __init__(self, system_prompt: str) -> None:
        self._messages = [
            Message(role="system", content=[TextBlock(text=system_prompt)])
        ]

    def append_user(self, text: str) -> None:
        self._messages.append(Message(role="user", content=[TextBlock(text=text)]))

    def append_assistant(self, message: Message) -> None:
        if message.role != "assistant":
            raise ValueError("model response must have the assistant role")
        self._messages.append(message)

    def append_tool_result(self, result: ToolResultBlock) -> None:
        self._messages.append(Message(role="tool", content=[result]))

    def request_messages(self) -> list[Message]:
        return list(self._messages)

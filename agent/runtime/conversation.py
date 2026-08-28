"""Ordered, provider-independent conversation history."""

from __future__ import annotations

from agent.core.messages import Message, TextBlock, ToolResultBlock

from .events import public_message


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

"""Conservative provider-independent context capacity checks."""

from __future__ import annotations

from dataclasses import asdict
import json

from agent.core.messages import Message
from agent.tools.types import ToolDefinition

from .errors import ContextLimitError


class ContextBudget:
    """Fail before a request whose conservative size exceeds its input budget."""

    def __init__(
        self,
        *,
        context_window_tokens: int,
        output_tokens: int | None,
    ) -> None:
        if (
            isinstance(context_window_tokens, bool)
            or not isinstance(context_window_tokens, int)
            or context_window_tokens <= 0
        ):
            raise ValueError("context_window_tokens must be a positive integer")
        if output_tokens is not None and (
            isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or output_tokens <= 0
        ):
            raise ValueError("output_tokens must be a positive integer or None")
        self._context_window_tokens = context_window_tokens
        self._output_reserve = output_tokens or max(
            1, min(4096, context_window_tokens // 4)
        )

    @property
    def context_window_tokens(self) -> int:
        """Configured model window used by the assembly owner."""

        return self._context_window_tokens

    @property
    def output_reserve_tokens(self) -> int:
        """Conservative output reservation excluded from input capacity."""

        return self._output_reserve

    @property
    def input_budget_tokens(self) -> int:
        """Maximum estimated input size accepted for this request."""

        return max(0, self._context_window_tokens - self._output_reserve)

    def ensure_fits(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
    ) -> None:
        input_budget = self._context_window_tokens - self._output_reserve
        estimated_tokens = self.estimate_tokens(messages, tools)
        if input_budget <= 0 or estimated_tokens > input_budget:
            raise ContextLimitError(
                "conversation exceeds the configured model context budget"
            )

    @staticmethod
    def estimate_tokens(
        messages: list[Message],
        tools: list[ToolDefinition],
    ) -> int:
        """Use serialized UTF-8 bytes plus framing as a tokenizer-free upper bound."""

        payload = {
            "messages": [asdict(message) for message in messages],
            "tools": [asdict(tool) for tool in tools],
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        block_count = sum(len(message.content) for message in messages)
        framing_allowance = 64 * (len(messages) + len(tools) + block_count)
        return len(serialized) + framing_allowance

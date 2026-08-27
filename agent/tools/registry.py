"""Registration and dispatch for local executable tools."""

from __future__ import annotations

from collections.abc import Callable

from agent.core.messages import ToolCallBlock
from agent.tools.filesystem import ToolOperationError
from agent.tools.types import ToolDefinition, ToolResult


ToolExecutor = Callable[[dict[str, object]], ToolResult]


class ToolRegistry:
    """A workspace-independent registry of model-visible local tools."""

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolDefinition, ToolExecutor]] = {}

    def register(self, definition: ToolDefinition, executor: ToolExecutor) -> None:
        if definition.name in self._tools:
            raise ValueError(f"tool already registered: {definition.name}")
        self._tools[definition.name] = (definition, executor)

    def definitions(self) -> list[ToolDefinition]:
        return [definition for definition, _ in self._tools.values()]

    def lookup(self, name: str) -> ToolDefinition | None:
        registered = self._tools.get(name)
        return None if registered is None else registered[0]

    def execute(self, call: ToolCallBlock) -> ToolResult:
        registered = self._tools.get(call.name)
        if registered is None:
            return ToolResult(
                content=f"unknown tool: {call.name}",
                metadata={"tool": call.name},
                error_code="UNKNOWN_TOOL",
            )
        try:
            return registered[1](call.arguments)
        except ToolOperationError as error:
            return ToolResult(content=str(error), metadata={}, error_code=error.code)
        except Exception:
            return ToolResult(
                content="unexpected internal tool error",
                metadata={},
                error_code="INTERNAL_ERROR",
            )

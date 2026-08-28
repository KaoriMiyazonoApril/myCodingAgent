"""Registration and dispatch for local executable tools."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging

from agent.core.messages import ToolCallBlock
from agent.tools.filesystem import ToolOperationError
from agent.tools.types import (
    ToolArgumentsValidationError,
    ToolDefinition,
    ToolResult,
)


ToolExecutor = Callable[[dict[str, object]], ToolResult]
AsyncToolExecutor = Callable[[dict[str, object]], Awaitable[ToolResult]]

logger = logging.getLogger(__name__)


class ToolRegistry:
    """A workspace-independent registry of model-visible local tools."""

    def __init__(self) -> None:
        self._tools: dict[
            str, tuple[ToolDefinition, ToolExecutor, AsyncToolExecutor | None]
        ] = {}

    def register(
        self,
        definition: ToolDefinition,
        executor: ToolExecutor,
        *,
        async_executor: AsyncToolExecutor | None = None,
    ) -> None:
        if definition.name in self._tools:
            raise ValueError(f"tool already registered: {definition.name}")
        self._tools[definition.name] = (definition, executor, async_executor)

    def definitions(self) -> list[ToolDefinition]:
        return [definition for definition, _, _ in self._tools.values()]

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
        if call.arguments_error is not None:
            return ToolResult(
                content=f"invalid tool arguments: {call.arguments_error}",
                metadata={"tool": call.name, "tool_call_id": call.id},
                error_code="INVALID_ARGUMENTS",
            )
        try:
            if call.arguments is None:
                raise ToolArgumentsValidationError("tool arguments could not be parsed")
            arguments = registered[0].validate_arguments(call.arguments)
            return registered[1](arguments)
        except ToolArgumentsValidationError as error:
            return ToolResult(content=str(error), metadata={}, error_code="INVALID_ARGUMENTS")
        except ToolOperationError as error:
            return ToolResult(content=str(error), metadata={}, error_code=error.code)
        except Exception:
            logger.exception("Unexpected error while executing tool %s", call.name)
            return ToolResult(
                content="unexpected internal tool error",
                metadata={},
                error_code="INTERNAL_ERROR",
            )

    async def execute_async(self, call: ToolCallBlock) -> ToolResult:
        """Dispatch without blocking an async Agent Loop or UI event loop."""
        registered = self._tools.get(call.name)
        if registered is None or registered[2] is None:
            worker = asyncio.create_task(asyncio.to_thread(self.execute, call))
            cancellation_seen = False
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    cancellation_seen = True
            result = worker.result()
            result._settled_after_cancellation = cancellation_seen
            return result
        if call.arguments_error is not None:
            return self.execute(call)
        try:
            if call.arguments is None:
                raise ToolArgumentsValidationError("tool arguments could not be parsed")
            arguments = registered[0].validate_arguments(call.arguments)
            return await registered[2](arguments)
        except ToolArgumentsValidationError as error:
            return ToolResult(content=str(error), metadata={}, error_code="INVALID_ARGUMENTS")
        except ToolOperationError as error:
            return ToolResult(content=str(error), metadata={}, error_code=error.code)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected error while executing tool %s", call.name)
            return ToolResult(
                content="unexpected internal tool error",
                metadata={},
                error_code="INTERNAL_ERROR",
            )

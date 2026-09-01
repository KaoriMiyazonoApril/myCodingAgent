"""Registration and dispatch for local executable tools."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import inspect
import logging
import threading
from typing import TYPE_CHECKING

from agent.core.messages import ToolCallBlock
from agent.tools.filesystem import ToolOperationError
from agent.tools.types import (
    ToolArgumentsValidationError,
    ToolDefinition,
    ToolResult,
)

if TYPE_CHECKING:
    from agent.runtime.policy import ExecutionProfile


ToolExecutor = Callable[[dict[str, object]], ToolResult]
AsyncToolExecutor = Callable[[dict[str, object]], Awaitable[ToolResult]]
CloseCallback = Callable[[], object]
EventSink = Callable[[str, dict[str, object]], object]
ExecutionProfileSetter = Callable[[object | None], None]
SessionContextSetter = Callable[..., None]

logger = logging.getLogger(__name__)


class ToolRegistry:
    """A workspace-independent registry of model-visible local tools."""

    def __init__(
        self,
        *,
        on_close: CloseCallback | None = None,
        async_on_close: CloseCallback | None = None,
    ) -> None:
        self._tools: dict[
            str, tuple[ToolDefinition, ToolExecutor, AsyncToolExecutor | None]
        ] = {}
        self._on_close = on_close
        self._async_on_close = async_on_close
        self._closed = False
        self._event_sink_setters: list[Callable[[EventSink | None], None]] = []
        self._session_cancellers: list[Callable[..., None]] = []
        self._session_context_setters: list[SessionContextSetter] = []
        self._execution_profile_setters: list[ExecutionProfileSetter] = []

    def bind_event_sink(
        self, setter: Callable[[EventSink | None], None]
    ) -> None:
        """Bind a runtime-owned event emitter to a capability that produces events."""

        self._event_sink_setters.append(setter)

    def set_event_sink(self, sink: EventSink | None) -> None:
        """Set or clear the current Turn event sink for bound capabilities."""

        for setter in self._event_sink_setters:
            setter(sink)

    def bind_session_canceller(self, canceller: Callable[..., None]) -> None:
        """Bind Turn-cancellation cleanup for stateful capabilities."""

        self._session_cancellers.append(canceller)

    def bind_session_context_setter(self, setter: SessionContextSetter) -> None:
        """Bind immutable owner context capture for newly-created sessions."""

        self._session_context_setters.append(setter)

    def set_session_context(
        self,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        for setter in self._session_context_setters:
            setter(thread_id=thread_id, turn_id=turn_id)

    def bind_execution_profile_setter(self, setter: ExecutionProfileSetter) -> None:
        """Bind the sandbox profile seam for command-capable tools."""

        self._execution_profile_setters.append(setter)

    def set_execution_profile(self, profile: ExecutionProfile | None) -> None:
        for setter in self._execution_profile_setters:
            setter(profile)

    def cancel_active_sessions(self, owner_turn_id: str | None = None) -> None:
        """Request cleanup of sessions owned by one Turn or whole Thread."""

        for canceller in self._session_cancellers:
            try:
                canceller(owner_turn_id)
            except TypeError:
                # Keep compatibility with simple embedders whose cleanup
                # callback predates owner-filtered cancellation.
                if owner_turn_id is None:
                    canceller()

    def close(self) -> bool:
        """Close owned resources once and report whether this call did so."""

        if self._closed:
            return False
        self._closed = True
        if self._on_close is not None:
            self._on_close()
        return True

    async def aclose(self) -> bool:
        """Await resource cleanup when an owner has an asynchronous lifecycle."""

        if self._closed:
            if self._async_on_close is not None:
                result = self._async_on_close()
                if inspect.isawaitable(result):
                    await result
            return False
        self._closed = True
        close = self._async_on_close or self._on_close
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        return True

    def register(
        self,
        definition: ToolDefinition,
        executor: ToolExecutor,
        *,
        async_executor: AsyncToolExecutor | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("tool registry is closed")
        if definition.name in self._tools:
            raise ValueError(f"tool already registered: {definition.name}")
        self._tools[definition.name] = (definition, executor, async_executor)

    def definitions(self) -> list[ToolDefinition]:
        return [definition for definition, _, _ in self._tools.values()]

    def lookup(self, name: str) -> ToolDefinition | None:
        registered = self._tools.get(name)
        return None if registered is None else registered[0]

    def execute(
        self,
        call: ToolCallBlock,
        *,
        execution_profile: ExecutionProfile | None = None,
    ) -> ToolResult:
        if self._closed:
            return self._closed_result(call)
        self.set_execution_profile(execution_profile)
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
            result = registered[1](arguments)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                return ToolResult(
                    content="tool requires asynchronous dispatch",
                    metadata={"tool": call.name},
                    error_code="ASYNC_ONLY",
                )
            return result
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

    async def execute_async(
        self,
        call: ToolCallBlock,
        *,
        execution_profile: ExecutionProfile | None = None,
    ) -> ToolResult:
        """Dispatch without blocking an async Agent Loop or UI event loop."""
        if self._closed:
            return self._closed_result(call)
        self.set_execution_profile(execution_profile)
        registered = self._tools.get(call.name)
        if registered is None or registered[2] is None:
            # Do not use asyncio's process-global default executor here.  A
            # ThreadRuntime is frequently embedded in ``asyncio.run`` and a
            # long-lived default pool can make loop shutdown wait forever on
            # Python builds with unreliable worker joins.  A daemon worker
            # preserves the non-blocking sync-tool seam without registering a
            # pool that the event loop must join during shutdown.
            result_holder: list[ToolResult] = []
            error_holder: list[BaseException] = []
            settled = threading.Event()

            def run_sync_tool() -> None:
                try:
                    result = self.execute(
                        call,
                        execution_profile=execution_profile,
                    )
                except BaseException as error:
                    error_holder.append(error)
                else:
                    result_holder.append(result)
                finally:
                    settled.set()

            threading.Thread(
                target=run_sync_tool,
                name="my-coding-agent-tool",
                daemon=True,
            ).start()
            cancellation_seen = False
            while not settled.is_set():
                try:
                    # Polling a threading.Event avoids depending on the
                    # interpreter's cross-thread Future wakeup path. The
                    # short sleep still gives the Host event loop a prompt
                    # scheduling point while sync tools run off-thread.
                    await asyncio.sleep(0.005)
                except asyncio.CancelledError:
                    cancellation_seen = True
            if error_holder:
                raise error_holder[0]
            if not result_holder:
                raise RuntimeError("sync tool worker returned no result")
            result = result_holder[0]
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

    @staticmethod
    def _closed_result(call: ToolCallBlock) -> ToolResult:
        return ToolResult(
            content="tool registry is closed",
            metadata={"tool": call.name, "tool_call_id": call.id},
            error_code="TOOL_REGISTRY_CLOSED",
        )

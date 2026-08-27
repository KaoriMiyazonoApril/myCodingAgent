"""Turn budgets, cancellation and deterministic failure-loop detection."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import time
from typing import TypeVar

from agent.core.messages import ToolCallBlock
from agent.model.types import Usage
from agent.tools.types import ToolResult

from .errors import TurnLimitReached
from .events import json_safe
from .settings import AgentLimits


_T = TypeVar("_T")
_REPEATED_FAILURE_LIMIT = 3


class RunController:
    """Single source of truth for one Turn's bounded execution state."""

    def __init__(
        self,
        limits: AgentLimits,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._started_at = clock()
        self._paused_at: float | None = None
        self._paused_seconds = 0.0
        self._deadline_changed = asyncio.Event()
        self._task: asyncio.Task[object] | None = None
        self._cancel_requested = False
        self._last_failure: str | None = None
        self._consecutive_failures = 0
        self.iterations = 0
        self.tool_calls = 0
        self.last_assistant_text = ""
        self.usage: dict[str, int | None] = {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }

    def bind(self, task: asyncio.Task[object]) -> None:
        self._task = task

    def cancel(self) -> bool:
        if self._cancel_requested:
            return False
        self._cancel_requested = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        return True

    def begin_iteration(self) -> None:
        self.checkpoint()
        if self.iterations >= self._limits.max_iterations:
            raise TurnLimitReached("max_iterations")
        self.iterations += 1

    def begin_tool(self) -> None:
        self.checkpoint()
        if self.tool_calls >= self._limits.max_tool_calls:
            raise TurnLimitReached("max_tool_calls")
        self.tool_calls += 1

    def record_model_response(self, usage: Usage, assistant_text: str) -> None:
        self.last_assistant_text = assistant_text
        for name in self.usage:
            value = getattr(usage, name)
            if value is not None:
                self.usage[name] = (self.usage[name] or 0) + value

    def record_tool_result(
        self,
        call: ToolCallBlock,
        result: ToolResult,
    ) -> None:
        if result.error_code is None:
            self._last_failure = None
            self._consecutive_failures = 0
            return
        fingerprint = self._failure_fingerprint(call, result.error_code)
        if fingerprint == self._last_failure:
            self._consecutive_failures += 1
        else:
            self._last_failure = fingerprint
            self._consecutive_failures = 1
        if self._consecutive_failures >= _REPEATED_FAILURE_LIMIT:
            raise TurnLimitReached("repeated_tool_failure")

    def checkpoint(self) -> None:
        if self._cancel_requested:
            raise asyncio.CancelledError
        if self.remaining_seconds() <= 0:
            raise TurnLimitReached("execution_timeout")

    async def wait(self, operation: Awaitable[_T]) -> _T:
        """Bound one active model/tool operation by the remaining execution time."""

        self.checkpoint()
        operation_task = asyncio.ensure_future(operation)
        control_tasks: set[asyncio.Task[object]] = set()
        try:
            while True:
                deadline_changed = asyncio.create_task(
                    self._deadline_changed.wait()
                )
                control_tasks.add(deadline_changed)
                waiters: set[asyncio.Task[object]] = {
                    operation_task,
                    deadline_changed,
                }
                timer_task: asyncio.Task[object] | None = None
                if self._paused_at is None:
                    timer_task = asyncio.create_task(
                        asyncio.sleep(self.remaining_seconds())
                    )
                    control_tasks.add(timer_task)
                    waiters.add(timer_task)

                done, _ = await asyncio.wait(
                    waiters,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if operation_task in done:
                    await self._cancel_tasks(control_tasks)
                    return await operation_task
                if deadline_changed in done:
                    self._deadline_changed.clear()
                    await self._cancel_tasks(control_tasks)
                    control_tasks.clear()
                    continue

                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
                await self._cancel_tasks(control_tasks)
                raise TurnLimitReached("execution_timeout")
        except asyncio.CancelledError:
            operation_task.cancel()
            await self._cancel_tasks(control_tasks)
            await asyncio.gather(operation_task, return_exceptions=True)
            raise

    def pause_deadline(self) -> None:
        """Suspend execution-time accounting while awaiting external approval."""

        if self._paused_at is not None:
            raise RuntimeError("execution deadline is already paused")
        self.checkpoint()
        self._paused_at = self._clock()
        self._deadline_changed.set()

    def resume_deadline(self) -> None:
        if self._paused_at is None:
            raise RuntimeError("execution deadline is not paused")
        self._paused_seconds += self._clock() - self._paused_at
        self._paused_at = None
        self._deadline_changed.set()

    def remaining_seconds(self) -> float:
        now = self._clock()
        current_pause = 0.0 if self._paused_at is None else now - self._paused_at
        elapsed = now - self._started_at - self._paused_seconds - current_pause
        return max(0.0, self._limits.max_execution_seconds - elapsed)

    @staticmethod
    async def _cancel_tasks(tasks: set[asyncio.Task[object]]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _failure_fingerprint(call: ToolCallBlock, error_code: str) -> str:
        arguments: object
        if call.arguments is not None:
            arguments = json_safe(call.arguments)
        else:
            arguments = {"raw_arguments": call.raw_arguments}
        normalized = json.dumps(
            arguments,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"{call.name}\0{normalized}\0{error_code}"

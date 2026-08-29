from __future__ import annotations

import asyncio

import pytest

from agent.host.turn_tasks import (
    DuplicateTurnSubmissionError,
    NoActiveTurnError,
    TurnTaskManager,
)


class _Runtime:
    def __init__(self, *, become_active: bool = False) -> None:
        self.become_active = become_active
        self.active_turn_id: str | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_calls: list[str] = []
        self.cancelled_before_start = False

    async def run_turn(self, thread_id: str, user_text: str, **kwargs):
        if self.become_active:
            self.active_turn_id = "turn-active"
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled_before_start = True
            raise
        finally:
            self.active_turn_id = None

    def cancel_turn(self, thread_id: str) -> bool:
        self.cancel_calls.append(thread_id)
        self.release.set()
        return True


class _Threads:
    def __init__(self, runtime: _Runtime) -> None:
        self.runtime = runtime

    def get_thread(self, thread_id: str):
        if thread_id != "thread-1":
            raise KeyError(thread_id)
        return {
            "snapshot": {
                "thread_id": thread_id,
                "status": "running" if self.runtime.active_turn_id else "idle",
                "active_turn_id": self.runtime.active_turn_id,
            }
        }


async def _wait_until_clean(manager: TurnTaskManager) -> None:
    for _ in range(20):
        if manager.inspect("thread-1") is None:
            return
        await asyncio.sleep(0)
    raise AssertionError("submission did not clean up")


def test_task_manager_exposes_starting_rejects_duplicate_and_cleans_cancel() -> None:
    async def scenario() -> tuple[dict[str, object], bool]:
        runtime = _Runtime()
        manager = TurnTaskManager(_Threads(runtime))  # type: ignore[arg-type]

        accepted = await manager.start("thread-1", "multiline\ntask")
        assert runtime.entered.is_set()
        assert manager.inspect("thread-1")["status"] == "starting"
        with pytest.raises(DuplicateTurnSubmissionError):
            await manager.start("thread-1", "duplicate")

        cancelling = manager.cancel("thread-1")
        await _wait_until_clean(manager)
        assert manager.inspect("thread-1") is None
        return accepted, runtime.cancelled_before_start

    accepted, cancelled = asyncio.run(scenario())

    assert accepted["thread_id"] == "thread-1"
    assert accepted["status"] == "starting"
    assert cancelled is True


def test_task_manager_delegates_active_cancel_and_rejects_idle_cancel() -> None:
    async def scenario() -> list[str]:
        runtime = _Runtime(become_active=True)
        manager = TurnTaskManager(_Threads(runtime))  # type: ignore[arg-type]

        await manager.start("thread-1", "task")
        assert manager.inspect("thread-1")["status"] == "running"
        manager.cancel("thread-1")
        await _wait_until_clean(manager)
        with pytest.raises(NoActiveTurnError):
            manager.cancel("thread-1")
        return runtime.cancel_calls

    assert asyncio.run(scenario()) == ["thread-1"]


def test_task_manager_shutdown_stops_accepting_and_cleans_all_tasks() -> None:
    async def scenario() -> None:
        runtime = _Runtime()
        manager = TurnTaskManager(_Threads(runtime))  # type: ignore[arg-type]
        await manager.start("thread-1", "task")

        await manager.shutdown()

        assert manager.inspect("thread-1") is None
        with pytest.raises(RuntimeError, match="shutting down"):
            await manager.start("thread-1", "too late")

    asyncio.run(scenario())


def test_task_manager_consumes_unexpected_task_failure_and_cleans_mapping() -> None:
    class FailingRuntime(_Runtime):
        async def run_turn(self, thread_id: str, user_text: str, **kwargs):
            raise RuntimeError("private infrastructure details")

    async def scenario() -> tuple[dict[str, object], dict[str, object] | None]:
        runtime = FailingRuntime()
        manager = TurnTaskManager(_Threads(runtime))  # type: ignore[arg-type]

        accepted = await manager.start("thread-1", "task")
        await _wait_until_clean(manager)
        return accepted, manager.inspect_failure("thread-1")

    accepted, failure = asyncio.run(scenario())
    assert accepted["status"] == "starting"
    assert failure == {
        "code": "TURN_TASK_FAILED",
        "message": "Agent Turn task failed",
    }
    assert "private infrastructure details" not in repr(failure)

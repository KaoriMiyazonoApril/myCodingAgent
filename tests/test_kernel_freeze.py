from __future__ import annotations

import asyncio

import pytest

from agent.runtime.events import EventBuffer, TurnEventEmitter
from agent.tools.filesystem import ToolOperationError, WorkspaceFilesystem
from agent.tools.process import ProcessManager
from tests.sandbox_support import DeterministicSandboxBackend


@pytest.mark.parametrize(
    "terminal_type",
    ["turn_completed", "turn_failed", "turn_cancelled", "turn_limit_reached"],
)
def test_turn_emitter_closes_ordinary_channel_after_terminal_event(
    terminal_type: str,
) -> None:
    buffer = EventBuffer(16)
    emitter = TurnEventEmitter(
        thread_id="thread-1",
        turn_id="turn-1",
        buffer=buffer,
        reasoning_visibility="hidden",
    )

    emitter.emit(terminal_type, {"summary": {"status": terminal_type}})
    emitter.emit("command_output_delta", {"session_id": "old"})
    emitter.emit("tool_finished", {"name": "old"})

    assert [event.type for event in buffer.read().events] == [terminal_type]
    assert emitter.terminal is True


def test_process_session_owner_and_turn_cancellation_are_isolated(tmp_path) -> None:
    async def scenario() -> None:
        manager = ProcessManager(
            WorkspaceFilesystem(tmp_path),
            sandbox_backend=DeterministicSandboxBackend(),
            owner_thread_id="thread-1",
        )
        turn_a_events: list[tuple[str, dict[str, object]]] = []
        turn_b_events: list[tuple[str, dict[str, object]]] = []

        manager.set_event_sink(
            lambda event_type, payload: turn_a_events.append((event_type, payload))
        )
        manager.set_session_context(turn_id="turn-a")
        session_a = await manager.exec(
            "read line; printf 'A:%s' \"$line\"", yield_time_ms=0
        )
        session_a_id = str(session_a.metadata["session_id"])
        assert session_a.metadata["owner_thread_id"] == "thread-1"
        assert session_a.metadata["owner_turn_id"] == "turn-a"

        manager.set_event_sink(
            lambda event_type, payload: turn_b_events.append((event_type, payload))
        )
        manager.set_session_context(turn_id="turn-b")
        session_b = await manager.exec("read line", yield_time_ms=0)
        session_b_id = str(session_b.metadata["session_id"])
        assert session_b.metadata["owner_turn_id"] == "turn-b"

        manager.cancel_active("turn-b")
        await asyncio.sleep(0.02)
        with pytest.raises(ToolOperationError, match="exited") as dead:
            await manager.write_stdin(session_b_id, yield_time_ms=0)
        assert dead.value.code == "SESSION_DEAD"

        completed_a = await manager.write_stdin(
            session_a_id, chars="still-alive\n", yield_time_ms=100
        )
        assert completed_a.metadata["status"] == "exited"
        assert completed_a.metadata["owner_turn_id"] == "turn-a"
        assert any(
            kind == "command_output_delta"
            and payload.get("owner_turn_id") == "turn-a"
            for kind, payload in turn_a_events
        )
        assert not any(
            kind == "command_output_delta"
            and payload.get("owner_turn_id") == "turn-a"
            for kind, payload in turn_b_events
        )
        await manager.aclose()

    asyncio.run(scenario())


def test_cancelling_cross_turn_poll_does_not_kill_session_owner(tmp_path) -> None:
    async def scenario() -> None:
        manager = ProcessManager(
            WorkspaceFilesystem(tmp_path),
            sandbox_backend=DeterministicSandboxBackend(),
            owner_thread_id="thread-1",
        )
        manager.set_session_context(turn_id="turn-a")
        started = await manager.exec(
            "read line; printf 'A:%s' \"$line\"", yield_time_ms=0
        )
        session_id = str(started.metadata["session_id"])

        # Turn B is allowed to interact with a Thread-persistent Session, but
        # cancelling that interaction must not transfer ownership or kill the
        # Session created by Turn A.
        manager.set_session_context(turn_id="turn-b")
        polling = asyncio.create_task(
            manager.write_stdin(session_id, yield_time_ms=300_000)
        )
        await asyncio.sleep(0.02)
        polling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await polling
        manager.cancel_active("turn-b")

        completed = await manager.write_stdin(
            session_id, chars="still-alive\n", yield_time_ms=100
        )
        assert completed.metadata["status"] == "exited"
        assert completed.metadata["owner_turn_id"] == "turn-a"
        assert completed.metadata["stdout"] == "A:still-alive"
        await manager.aclose()

    asyncio.run(scenario())


def test_completed_process_resources_and_tombstones_are_bounded(tmp_path) -> None:
    async def scenario() -> None:
        manager = ProcessManager(
            WorkspaceFilesystem(tmp_path),
            sandbox_backend=DeterministicSandboxBackend(),
            max_completed_sessions=2,
            max_dead_sessions=2,
        )
        for _ in range(8):
            result = await manager.exec("sleep .01", yield_time_ms=0)
            assert result.metadata["status"] == "running"
            await asyncio.sleep(0.03)

        assert manager.active_session_count == 0
        assert manager.completed_session_count <= 2
        assert manager.dead_session_count <= 2
        await manager.aclose()

    asyncio.run(scenario())


def test_turn_cancellation_preserves_naturally_completed_final_poll(tmp_path) -> None:
    async def scenario() -> None:
        manager = ProcessManager(
            WorkspaceFilesystem(tmp_path),
            sandbox_backend=DeterministicSandboxBackend(),
            owner_thread_id="thread-1",
        )
        manager.set_session_context(turn_id="turn-a")
        started = await manager.exec("printf completed", yield_time_ms=0)
        session_id = str(started.metadata["session_id"])
        for _ in range(100):
            if manager.completed_session_count:
                break
            await asyncio.sleep(0.01)
        assert manager.completed_session_count == 1

        manager.cancel_active("turn-a")

        final = await manager.write_stdin(session_id, yield_time_ms=0)
        assert final.metadata["status"] == "exited"
        assert final.metadata["owner_turn_id"] == "turn-a"
        assert final.metadata["stdout"] == "completed"
        await manager.aclose()

    asyncio.run(scenario())


def test_thread_close_reaps_sessions_from_multiple_turns(tmp_path) -> None:
    async def scenario() -> None:
        manager = ProcessManager(
            WorkspaceFilesystem(tmp_path),
            sandbox_backend=DeterministicSandboxBackend(),
            owner_thread_id="thread-1",
        )
        manager.set_session_context(turn_id="turn-a")
        await manager.exec("read line", yield_time_ms=0)
        manager.set_session_context(turn_id="turn-b")
        await manager.exec("read line", yield_time_ms=0)

        await manager.aclose()

        assert manager.active_session_count == 0
        assert manager.completed_session_count == 0
        assert manager.dead_session_count == 0
        with pytest.raises(ToolOperationError, match="closed"):
            await manager.write_stdin("unknown", yield_time_ms=0)

    asyncio.run(scenario())

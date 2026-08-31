from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess

import pytest

from agent.core.messages import ToolCallBlock
from agent.runtime.change_tracker import ChangeTracker
from agent.runtime.events import EventBuffer, TurnEventEmitter
from agent.tools.local import create_local_tool_registry
from agent.tools.filesystem import ToolOperationError, WorkspaceFilesystem
from agent.tools.process import ProcessManager
from agent.tools.types import ToolResult
from tests.sandbox_support import DeterministicSandboxBackend


def registry(tmp_path: Path):
    return create_local_tool_registry(
        tmp_path,
        sandbox_backend=DeterministicSandboxBackend(),
    )


def execute(reg, call: ToolCallBlock) -> ToolResult:
    return asyncio.run(reg.execute_async(call))


def test_exec_command_returns_exit_and_separate_incremental_streams(tmp_path) -> None:
    result = execute(
        registry(tmp_path),
        ToolCallBlock(
            id="fast",
            name="exec_command",
            arguments={
                "command": "printf out; printf err >&2",
                "yield_time_ms": 100,
            },
        ),
    )

    assert result.error_code is None
    assert result.metadata["status"] == "exited"
    assert result.metadata["exit_code"] == 0
    assert result.metadata["stdout"] == "out"
    assert result.metadata["stderr"] == "err"
    assert result.metadata["session_id"]


def test_running_session_accepts_stdin_and_empty_poll(tmp_path) -> None:
    async def scenario() -> None:
        reg = registry(tmp_path)
        first = await reg.execute_async(
            ToolCallBlock(
                id="start",
                name="exec_command",
                arguments={
                    "command": "read line; printf 'got:%s' \"$line\"",
                    "yield_time_ms": 20,
                },
            )
        )
        assert first.error_code is None
        assert first.metadata["status"] == "running"
        session_id = first.metadata["session_id"]

        polled = await reg.execute_async(
            ToolCallBlock(
                id="poll",
                name="write_stdin",
                arguments={"session_id": session_id, "yield_time_ms": 0},
            )
        )
        assert polled.error_code is None
        assert polled.metadata["status"] == "running"
        assert polled.metadata["stdout"] == ""

        completed = await reg.execute_async(
            ToolCallBlock(
                id="input",
                name="write_stdin",
                arguments={
                    "session_id": session_id,
                    "chars": "hello\n",
                    "yield_time_ms": 100,
                },
            )
        )
        assert completed.error_code is None
        assert completed.metadata["status"] == "exited"
        assert completed.metadata["stdout"] == "got:hello"

        unknown = await reg.execute_async(
            ToolCallBlock(
                id="unknown",
                name="write_stdin",
                arguments={"session_id": session_id},
            )
        )
        assert unknown.error_code == "SESSION_DEAD"
        reg.close()

    asyncio.run(scenario())


def test_natural_exit_is_available_for_one_final_poll_then_reports_dead(tmp_path) -> None:
    async def scenario() -> None:
        manager = ProcessManager(
            WorkspaceFilesystem(tmp_path),
            sandbox_backend=DeterministicSandboxBackend(),
        )
        started = await manager.exec("printf final; sleep .02", yield_time_ms=0)
        session_id = str(started.metadata["session_id"])
        await asyncio.sleep(0.08)

        final = await manager.write_stdin(session_id, yield_time_ms=0)
        assert final.metadata["status"] == "exited"
        assert final.metadata["stdout"] == "final"
        with pytest.raises(ToolOperationError) as captured:
            await manager.write_stdin(session_id, chars="late\n")
        assert captured.value.code == "SESSION_DEAD"
        manager.close()

    asyncio.run(scenario())


def test_concurrent_session_interactions_are_serialized(tmp_path) -> None:
    async def scenario() -> None:
        manager = ProcessManager(
            WorkspaceFilesystem(tmp_path),
            sandbox_backend=DeterministicSandboxBackend(),
        )
        started = await manager.exec(
            "read first; printf 'one:%s\\n' \"$first\"; read second; printf 'two:%s' \"$second\"",
            yield_time_ms=0,
        )
        session_id = str(started.metadata["session_id"])
        first, second = await asyncio.gather(
            manager.write_stdin(session_id, chars="A\n", yield_time_ms=500),
            manager.write_stdin(session_id, chars="B\n", yield_time_ms=500),
        )
        assert first.metadata["stdout"] == "one:A\n"
        assert second.metadata["stdout"] == "two:B"
        manager.close()

    asyncio.run(scenario())


def test_command_events_are_emitted_as_output_arrives(tmp_path) -> None:
    reg = registry(tmp_path)
    observed: list[tuple[str, dict[str, object]]] = []
    reg.set_event_sink(lambda event_type, payload: observed.append((event_type, payload)))

    result = execute(
        reg,
        ToolCallBlock(
            id="events",
            name="exec_command",
            arguments={
                "command": "printf first; sleep .05; printf second",
                "yield_time_ms": 200,
            },
        ),
    )

    assert result.error_code is None
    event_types = [event_type for event_type, _ in observed]
    assert event_types[0] == "command_started"
    assert "command_output_delta" in event_types
    assert event_types[-1] == "command_completed"
    output = [payload for kind, payload in observed if kind == "command_output_delta"]
    assert "first" in "".join(str(payload["text"]) for payload in output)
    assert "second" in "".join(str(payload["text"]) for payload in output)


def test_tty_metadata_declares_merged_output(tmp_path) -> None:
    result = execute(
        registry(tmp_path),
        ToolCallBlock(
            id="tty",
            name="exec_command",
            arguments={"command": "printf tty", "tty": True, "yield_time_ms": 100},
        ),
    )

    assert result.error_code is None
    assert result.metadata["status"] == "exited"
    assert result.metadata["tty"] is True
    assert result.metadata["stderr_merged"] is True
    assert result.metadata["stdout"] == "tty"
    assert result.metadata["stderr"] == ""


def test_git_workspace_command_tracking_reports_new_path_without_snapshotting_tree(tmp_path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    buffer = EventBuffer(32)
    events = TurnEventEmitter(
        thread_id="thread",
        turn_id="turn",
        buffer=buffer,
        reasoning_visibility="hidden",
    )
    tracker = ChangeTracker(tmp_path, events)
    reg = registry(tmp_path)
    call = ToolCallBlock(
        id="git-command",
        name="exec_command",
        arguments={
            "command": "printf generated > generated.txt",
            "yield_time_ms": 100,
        },
    )

    assert tracker.before_execution(call) is None
    result = execute(reg, call)
    tracker.after_execution(call, result)

    assert result.error_code is None
    assert tracker.diff_complete is True
    assert [change["path"] for change in tracker.changes()] == ["generated.txt"]
    assert tracker.changes()[0]["change_type"] == "added"


def test_git_tracking_detects_dirty_file_changed_from_m_to_m(tmp_path) -> None:
    source = tmp_path / "dirty.txt"
    source.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "dirty.txt"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    source.write_text("two\n", encoding="utf-8")
    buffer = EventBuffer(32)
    events = TurnEventEmitter(
        thread_id="thread",
        turn_id="turn",
        buffer=buffer,
        reasoning_visibility="hidden",
    )
    tracker = ChangeTracker(tmp_path, events)
    reg = registry(tmp_path)
    call = ToolCallBlock(
        id="dirty-command",
        name="exec_command",
        arguments={"command": "printf three > dirty.txt", "yield_time_ms": 100},
    )

    assert tracker.before_execution(call) is None
    result = execute(reg, call)
    tracker.after_execution(call, result)

    assert result.error_code is None
    assert [change["path"] for change in tracker.changes()] == ["dirty.txt"]
    assert "-one" in tracker.changes()[0]["diff"]
    assert "+three" in tracker.changes()[0]["diff"]


def test_ctrl_c_terminates_a_running_session(tmp_path) -> None:
    async def scenario() -> None:
        reg = registry(tmp_path)
        started = await reg.execute_async(
            ToolCallBlock(
                id="sleep",
                name="exec_command",
                arguments={"command": "sleep 10", "yield_time_ms": 10},
            )
        )
        assert started.metadata["status"] == "running"

        stopped = await reg.execute_async(
            ToolCallBlock(
                id="interrupt",
                name="write_stdin",
                arguments={
                    "session_id": started.metadata["session_id"],
                    "chars": "\u0003",
                    "yield_time_ms": 100,
                },
            )
        )
        assert stopped.metadata["status"] == "exited"
        assert stopped.error_code == "COMMAND_FAILED"
        reg.close()

    asyncio.run(scenario())


def test_session_timeout_terminates_process_group(tmp_path) -> None:
    result = execute(
        registry(tmp_path),
        ToolCallBlock(
            id="timeout",
            name="exec_command",
            arguments={
                "command": "printf before; sleep 1",
                "yield_time_ms": 200,
                "timeout_ms": 30,
            },
        ),
    )

    assert result.error_code == "TIMEOUT"
    assert result.metadata["status"] == "exited"
    assert result.metadata["timed_out"] is True
    assert result.metadata["stdout"] == "before"


def test_process_manager_enforces_active_session_limit(tmp_path) -> None:
    async def scenario() -> None:
        backend = DeterministicSandboxBackend()
        manager = ProcessManager(
            WorkspaceFilesystem(tmp_path),
            sandbox_backend=backend,
            max_sessions=1,
            sandbox_checked=False,
        )
        first = await manager.exec("read line", yield_time_ms=0)
        assert first.metadata["status"] == "running"
        with pytest.raises(ToolOperationError) as captured:
            await manager.exec("sleep 1", yield_time_ms=0)
        assert getattr(captured.value, "code", None) == "SESSION_LIMIT"
        manager.close()

    asyncio.run(scenario())


def test_natural_exit_and_idle_timeout_remove_sessions_without_a_follow_up_poll(tmp_path) -> None:
    async def scenario() -> None:
        backend = DeterministicSandboxBackend()
        manager = ProcessManager(
            WorkspaceFilesystem(tmp_path),
            sandbox_backend=backend,
            idle_timeout_seconds=0.05,
        )
        exited = await manager.exec("sleep .01", yield_time_ms=0)
        exited_id = exited.metadata["session_id"]
        await asyncio.sleep(0.08)
        assert exited_id not in manager._sessions

        running = await manager.exec("read line", yield_time_ms=0)
        running_id = running.metadata["session_id"]
        await asyncio.sleep(0.12)
        assert running_id not in manager._sessions
        manager.close()

    asyncio.run(scenario())


def test_async_manager_close_reaps_a_pty_session(tmp_path) -> None:
    async def scenario() -> None:
        backend = DeterministicSandboxBackend()
        manager = ProcessManager(
            WorkspaceFilesystem(tmp_path),
            sandbox_backend=backend,
        )
        result = await manager.exec("read line", tty=True, yield_time_ms=0)
        assert result.metadata["status"] == "running"
        await manager.aclose()
        assert not manager._sessions
        assert not manager._cleanup_tasks

    asyncio.run(scenario())


def test_session_output_is_bounded_per_stream(tmp_path) -> None:
    result = execute(
        registry(tmp_path),
        ToolCallBlock(
            id="bounded",
            name="exec_command",
            arguments={
                "command": "printf '%*s' 120000 '' | tr ' ' x",
                "yield_time_ms": 200,
            },
        ),
    )

    assert result.metadata["status"] == "exited"
    assert len(result.metadata["stdout"]) <= 100 * 1024
    assert result.metadata["stdout_truncated"] is True


def test_sync_registry_dispatch_fails_closed_for_async_session_tools(tmp_path) -> None:
    result = registry(tmp_path).execute(
        ToolCallBlock(
            id="sync",
            name="exec_command",
            arguments={"command": "printf no"},
        )
    )

    assert result.error_code == "ASYNC_ONLY"
    assert result.ok is False


@pytest.mark.parametrize("tool_name", ["exec_command", "write_stdin"])
def test_stateful_command_schemas_are_closed_objects(tmp_path, tool_name) -> None:
    definition = registry(tmp_path).lookup(tool_name)
    assert definition is not None
    assert definition.parameters["additionalProperties"] is False

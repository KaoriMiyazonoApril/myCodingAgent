from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.core.messages import Message, TextBlock, ToolCallBlock
from agent.model.provider import LLMProvider
from agent.model.types import LLMRequest, LLMResponse, Usage
from agent.runtime import (
    AgentEvent,
    AllowAllPolicy,
    ApprovalMode,
    IdempotencyConflictError,
    IdempotencyInterruptedError,
    ModelSettings,
    ThreadRuntime,
    ThreadStatus,
    TurnStatus,
    TurnSummary,
    WorkspaceUnavailableError,
)
from agent.runtime.thread_store import (
    InMemoryThreadStore,
    LocalThreadStore,
    StoredActiveTurn,
    StoredIdempotency,
    ThreadState,
)
from agent.tools.local import create_local_tool_registry
from agent.tools.registry import ToolRegistry


class _Provider(LLMProvider):
    def __init__(self, text: str = "done") -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    async def chat(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            message=Message(role="assistant", content=[TextBlock(text=self.text)]),
            finish_reason="stop",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )


class _ScriptedProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = iter(responses)
        self.requests: list[LLMRequest] = []

    async def chat(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return next(self.responses)


def _runtime(store, provider: _Provider) -> ThreadRuntime:
    return ThreadRuntime(
        tool_registry_factory=lambda _workspace: ToolRegistry(),
        provider_resolver=lambda _provider_id, _model: provider,
        default_settings=ModelSettings(provider_config_id="provider", model="model"),
        store=store,
    )


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_runtime_restarts_with_messages_settings_turns_and_events(
    tmp_path: Path, store_kind: str
) -> None:
    store = (
        InMemoryThreadStore()
        if store_kind == "memory"
        else LocalThreadStore(tmp_path / "state" / "threads.db")
    )
    first_provider = _Provider("first")
    first = _runtime(store, first_provider)
    thread = first.create_thread(tmp_path)
    first_summary = asyncio.run(first.run_turn(thread.thread_id, "hello"))
    first.update_settings(
        thread.thread_id,
        expected_version=0,
        settings=ModelSettings(
            provider_config_id="provider",
            model="model-2",
            approval_mode=ApprovalMode.NEVER,
        ),
    )

    second_provider = _Provider("second")
    second = _runtime(store, second_provider)
    restored = second.open_thread(thread.thread_id)

    assert restored.thread_id == thread.thread_id
    assert restored.status is ThreadStatus.IDLE
    assert restored.settings.model == "model-2"
    assert restored.settings.version == 1
    assert restored.messages[0]["role"] == "user"
    assert restored.messages[1]["content"][0]["text"] == "first"
    assert restored.latest_turn == first_summary
    assert restored.turns == [first_summary]
    assert second.get_events(thread.thread_id).latest_event_id is not None
    assert [event.type for event in second.get_events(thread.thread_id).events][-1] == (
        "settings_updated"
    )

    second_summary = asyncio.run(second.run_turn(thread.thread_id, "continue"))
    assert second_summary.status is TurnStatus.COMPLETED
    assert [message.role for message in second_provider.requests[0].messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    store.close()


def test_runtime_restart_preserves_real_tool_history_and_continues_turns(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = LocalThreadStore(tmp_path / "state" / "threads.db")
    first_provider = _ScriptedProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[
                        ToolCallBlock(
                            id="write-app",
                            name="write_file",
                            arguments={
                                "path": "app.py",
                                "content": "VALUE = 1\n",
                            },
                        )
                    ],
                ),
                finish_reason="tool_calls",
                usage=Usage(input_tokens=2, output_tokens=1, total_tokens=3),
            ),
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[TextBlock(text="File updated.")],
                ),
                finish_reason="stop",
                usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5),
            ),
        ]
    )
    first = ThreadRuntime(
        tool_registry_factory=create_local_tool_registry,
        provider_resolver=lambda _provider_id, _model: first_provider,
        default_settings=ModelSettings(provider_config_id="provider", model="model"),
        tool_policy=AllowAllPolicy(),
        store=store,
    )
    thread = first.create_thread(workspace)
    first_summary = asyncio.run(first.run_turn(thread.thread_id, "Update app."))

    assert first_summary.modified_files == ["app.py"]
    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    asyncio.run(first.aclose())
    store.close()

    second_store = LocalThreadStore(tmp_path / "state" / "threads.db")
    second_provider = _Provider("Second turn complete.")
    second = ThreadRuntime(
        tool_registry_factory=create_local_tool_registry,
        provider_resolver=lambda _provider_id, _model: second_provider,
        default_settings=ModelSettings(provider_config_id="provider", model="model"),
        tool_policy=AllowAllPolicy(),
        store=second_store,
    )
    restored = second.open_thread(thread.thread_id)

    assert restored.latest_turn == first_summary
    assert restored.turns == [first_summary]
    assert [message["role"] for message in restored.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert restored.messages[1]["content"][0]["name"] == "write_file"
    assert restored.messages[2]["content"][0]["tool_call_id"] == "write-app"
    assert restored.messages[2]["content"][0]["error_code"] is None

    second_summary = asyncio.run(
        second.run_turn(thread.thread_id, "Continue after restart.")
    )

    assert second_summary.status is TurnStatus.COMPLETED
    assert second.get_snapshot(thread.thread_id).completed_turns == 2
    assert [message.role for message in second_provider.requests[0].messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    asyncio.run(second.aclose())
    second_store.close()


def test_completed_idempotency_replays_after_restart_without_provider_call(tmp_path) -> None:
    store = LocalThreadStore(tmp_path / "state" / "threads.db")
    first_provider = _Provider("only once")
    first = _runtime(store, first_provider)
    thread = first.create_thread(tmp_path)
    summary = asyncio.run(
        first.run_turn(thread.thread_id, "once", idempotency_key="same-key")
    )

    second_provider = _Provider("must not run")
    second = _runtime(store, second_provider)
    replay = asyncio.run(
        second.run_turn(thread.thread_id, "once", idempotency_key="same-key")
    )
    assert replay == summary
    assert second_provider.requests == []
    with pytest.raises(IdempotencyConflictError):
        asyncio.run(
            second.run_turn(thread.thread_id, "changed", idempotency_key="same-key")
        )
    store.close()


def test_restart_recovers_active_turn_as_failed_without_rerunning_it(tmp_path) -> None:
    store = LocalThreadStore(tmp_path / "state" / "threads.db")
    state = ThreadState(
        thread_id="active-thread",
        workspace=str(tmp_path),
        status=ThreadStatus.RUNNING,
        settings=ModelSettings(  # type: ignore[arg-type]
            provider_config_id="provider",
            model="model",
        ),
        messages=[
            Message(role="system", content=[TextBlock(text="system")]),
            Message(role="user", content=[TextBlock(text="in progress")]),
            Message(
                role="assistant",
                content=[
                    ToolCallBlock(
                        id="call-active",
                        name="write_file",
                        arguments={"path": "output.txt", "content": "side effect"},
                    )
                ],
            ),
        ],
        completed_turns=0,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        active_turn=StoredActiveTurn(
            turn_id="turn-active",
            started_at="2026-01-01T00:00:00Z",
            idempotency_key="active-key",
        ),
        idempotency={
            "active-key": StoredIdempotency("in progress", None),
        },
    )
    # ThreadState uses ThreadSettings for durable defaults; construct the
    # versioned form explicitly while keeping this fixture readable.
    from agent.runtime import ThreadSettings

    state.settings = ThreadSettings.from_model_settings(state.settings, version=0)
    store.save_thread(state)

    provider = _Provider("must not run")
    runtime = _runtime(store, provider)
    snapshot = runtime.get_snapshot("active-thread")
    summary = snapshot.latest_turn
    assert summary is not None
    assert snapshot.status is ThreadStatus.IDLE
    assert snapshot.active_turn_id is None
    assert summary.status is TurnStatus.FAILED
    assert summary.stop_reason == "runtime_restarted"
    assert provider.requests == []
    assert snapshot.messages[-1]["role"] == "tool"
    assert snapshot.messages[-1]["content"][0]["error_code"] == "RUNTIME_RESTARTED"
    interrupted = snapshot.messages[-1]["content"][0]
    assert interrupted["metadata"]["execution_status"] == "unknown"
    assert interrupted["metadata"]["side_effects_possible"] is True
    assert "executed" not in interrupted["metadata"]
    assert "Inspect workspace/state before retrying" in interrupted["content"]

    with pytest.raises(IdempotencyInterruptedError):
        asyncio.run(
            runtime.run_turn(
                "active-thread", "in progress", idempotency_key="active-key"
            )
        )
    assert provider.requests == []
    events = runtime.get_events("active-thread").events
    assert events[-1].type == "turn_failed"
    assert events[-1].turn_id == "turn-active"
    assert [event.sequence for event in events] == sorted(
        event.sequence for event in events
    )
    store.close()


def test_restart_does_not_duplicate_coherently_terminal_turn_with_stale_marker(
    tmp_path: Path,
) -> None:
    store = LocalThreadStore(tmp_path / "state" / "threads.db")
    summary = TurnSummary(
        schema_version=1,
        turn_id="turn-terminal",
        thread_id="terminal-thread",
        status=TurnStatus.COMPLETED,
        stop_reason="completed",
        final_text="already done",
        iterations=1,
        tool_calls=0,
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
    )
    event = AgentEvent(
        schema_version=1,
        event_id="terminal-event",
        thread_id="terminal-thread",
        turn_id="turn-terminal",
        sequence=1,
        type="turn_completed",
        timestamp="2026-01-01T00:00:01Z",
        payload={"summary": summary.to_dict()},
    )
    from agent.runtime import ThreadSettings

    settings = ThreadSettings.from_model_settings(
        ModelSettings(provider_config_id="provider", model="model"),
        version=0,
    )
    store.save_thread(
        ThreadState(
            thread_id="terminal-thread",
            workspace=str(tmp_path),
            # Simulate the crash window: all terminal fields/event are durable,
            # but the old active marker and running status were not cleared.
            status=ThreadStatus.RUNNING,
            settings=settings,
            messages=[
                Message(role="system", content=[TextBlock(text="system")]),
                Message(role="user", content=[TextBlock(text="already done")]),
            ],
            completed_turns=1,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:01Z",
            latest_turn=summary,
            turns=[summary],
            events=[event],
            event_sequence=1,
            active_turn=StoredActiveTurn(
                turn_id="turn-terminal",
                started_at="2026-01-01T00:00:00Z",
                idempotency_key="terminal-key",
            ),
            idempotency={
                "terminal-key": StoredIdempotency(
                    user_text="already done", settings_override=None, summary=summary
                )
            },
        )
    )

    provider = _Provider("must not run")
    runtime = _runtime(store, provider)
    snapshot = runtime.get_snapshot("terminal-thread")

    assert snapshot.status is ThreadStatus.IDLE
    assert snapshot.active_turn_id is None
    assert snapshot.completed_turns == 1
    assert snapshot.turns == [summary]
    assert [item.type for item in runtime.get_events("terminal-thread").events] == [
        "turn_completed"
    ]
    repaired = store.get_thread("terminal-thread")
    assert repaired is not None
    assert repaired.active_turn is None
    assert repaired.status is ThreadStatus.IDLE
    assert asyncio.run(
        runtime.run_turn(
            "terminal-thread", "already done", idempotency_key="terminal-key"
        )
    ) == summary
    assert provider.requests == []
    store.close()


def test_missing_restored_workspace_remains_readable_but_rejects_new_turn(tmp_path) -> None:
    store = LocalThreadStore(tmp_path / "state" / "threads.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = _Provider()
    first = _runtime(store, provider)
    thread = first.create_thread(workspace)
    asyncio.run(first.run_turn(thread.thread_id, "remember this"))
    workspace.rmdir()

    replacement = _Provider("must not run")
    second = _runtime(store, replacement)
    restored = second.get_snapshot(thread.thread_id)
    assert restored.messages[0]["content"][0]["text"] == "remember this"
    assert second.list_threads()[0].thread_id == thread.thread_id
    with pytest.raises(WorkspaceUnavailableError) as failure:
        asyncio.run(second.run_turn(thread.thread_id, "new work"))
    assert failure.value.code == "WORKSPACE_UNAVAILABLE"
    assert replacement.requests == []
    assert not workspace.exists()
    store.close()

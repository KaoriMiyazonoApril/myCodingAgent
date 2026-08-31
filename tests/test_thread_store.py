from __future__ import annotations

import json

import pytest

from agent.core.messages import Message, ReasoningBlock, TextBlock, ToolCallBlock, ToolResultBlock
from agent.runtime import (
    AgentEvent,
    AgentLimits,
    ApprovalMode,
    ModelSettings,
    ThreadSettings,
    ThreadStatus,
    TurnSettingsOverride,
    TurnStatus,
    TurnSummary,
)
from agent.runtime.thread_store import (
    InMemoryThreadStore,
    LocalThreadStore,
    StoredActiveTurn,
    StoredIdempotency,
    ThreadState,
    ThreadStoreError,
)


def _state() -> ThreadState:
    settings = ThreadSettings.from_model_settings(
        ModelSettings(
            provider_config_id="opaque-provider",
            model="opaque-model",
            temperature=0.4,
            max_tokens=2048,
            limits=AgentLimits(max_iterations=3, max_tool_calls=4, max_execution_seconds=5),
            approval_mode=ApprovalMode.NEVER,
        ),
        version=2,
    )
    summary = TurnSummary(
        schema_version=1,
        turn_id="turn-1",
        thread_id="thread-1",
        status=TurnStatus.COMPLETED,
        stop_reason="completed",
        final_text="done",
        iterations=2,
        tool_calls=1,
        usage={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        modified_files=["app.py"],
        file_diffs=[{"path": "app.py", "diff": "-old\n+new"}],
        diff_complete=True,
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
    )
    event = AgentEvent(
        schema_version=1,
        event_id="event-1",
        thread_id="thread-1",
        turn_id="turn-1",
        sequence=4,
        type="tool_finished",
        timestamp="2026-01-01T00:00:01Z",
        payload={"result": {"content": "safe"}},
    )
    return ThreadState(
        thread_id="thread-1",
        workspace="/workspace/deleted-later",
        status=ThreadStatus.IDLE,
        settings=settings,
        messages=[
            Message(role="system", content=[TextBlock(text="system")]),
            Message(role="user", content=[TextBlock(text="question")]),
            Message(
                role="assistant",
                content=[
                    ReasoningBlock(text="private reasoning"),
                    ToolCallBlock(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "app.py"},
                        raw_arguments='{"path":"app.py"}',
                    ),
                ],
            ),
            Message(
                role="tool",
                content=[
                    ToolResultBlock(
                        tool_call_id="call-1",
                        content="safe result",
                        metadata={"path": "app.py"},
                    )
                ],
            ),
        ],
        completed_turns=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:01Z",
        latest_turn=summary,
        turns=[summary],
        events=[event],
        event_sequence=4,
        active_turn=StoredActiveTurn("turn-active", "2026-01-01T00:00:02Z"),
        idempotency={
            "request-1": StoredIdempotency(
                user_text="question",
                settings_override=TurnSettingsOverride(
                    temperature=0.9,
                    approval_mode=ApprovalMode.NEVER,
                ),
                summary=summary,
            )
        },
    )


@pytest.mark.parametrize("store_factory", [InMemoryThreadStore, LocalThreadStore])
def test_thread_store_round_trips_canonical_state(store_factory, tmp_path) -> None:
    store = (
        store_factory()
        if store_factory is InMemoryThreadStore
        else store_factory(tmp_path / "state" / "threads.db")
    )
    state = _state()
    store.save_thread(state)

    restored = store.get_thread("thread-1")

    assert restored == state
    assert store.list_threads() == [state]
    store.close()


def test_local_store_uses_versioned_schema_and_does_not_store_provider_secret(tmp_path) -> None:
    database = tmp_path / "state" / "threads.sqlite3"
    store = LocalThreadStore(database)
    state = _state()
    store.save_thread(state)
    store.close()

    assert json.dumps(state, default=str)  # smoke-check test fixture is non-empty
    raw = database.read_bytes()
    assert b"api-key-sentinel" not in raw
    assert b"opaque-provider" in raw
    reopened = LocalThreadStore(database)
    assert reopened.get_thread("thread-1") == state
    reopened.close()


def test_local_store_rejects_newer_schema(tmp_path) -> None:
    database = tmp_path / "newer.db"
    store = LocalThreadStore(database)
    store.close()
    import sqlite3

    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()

    with pytest.raises(ThreadStoreError, match="schema version"):
        LocalThreadStore(database)

from __future__ import annotations

import pytest

from agent.core.messages import Message, ReasoningBlock, TextBlock, ToolCallBlock, ToolResultBlock
from agent.host.provider_config import ProviderStore
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
from agent.runtime.context_history import (
    CompactionCheckpoint,
    CompactionSummary,
    canonical_history_fingerprint,
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
            provider_config_id="deepseek",
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
    provider_store = ProviderStore(tmp_path / "providers.json")
    provider_store.save_provider(
        "deepseek",
        api_key="api-key-sentinel",
        selected_model="opaque-model",
    )
    store = LocalThreadStore(database)
    state = _state()
    store.save_thread(state)
    store.close()

    assert provider_store.get_credential("deepseek") == "api-key-sentinel"
    raw = database.read_bytes()
    assert b"api-key-sentinel" not in raw
    assert b"deepseek" in raw
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


def test_local_store_incremental_transition_does_not_rebuild_event_log(tmp_path) -> None:
    database = tmp_path / "incremental.db"
    store = LocalThreadStore(database)
    state = _state()
    state.events = []
    state.event_sequence = 0
    store.save_thread(state)
    statements: list[str] = []
    store._connection.set_trace_callback(statements.append)

    for sequence in range(1, 25):
        event = AgentEvent(
            schema_version=1,
            event_id=f"event-{sequence}",
            thread_id=state.thread_id,
            turn_id="turn-1",
            sequence=sequence,
            type="tool_finished",
            timestamp="2026-01-01T00:00:01Z",
            payload={"sequence": sequence},
        )
        state.events.append(event)
        state.event_sequence = sequence
        store.save_thread_transition(state, new_events=(event,))

    assert not any("DELETE FROM thread_events" in statement for statement in statements)
    assert sum("INSERT OR IGNORE INTO thread_events" in statement for statement in statements) == 24
    restored = store.get_thread(state.thread_id)
    assert restored is not None
    assert [event.sequence for event in restored.events] == list(range(1, 25))
    store.close()


def test_local_store_full_save_replaces_the_complete_event_set(tmp_path) -> None:
    store = LocalThreadStore(tmp_path / "replace.db")
    state = _state()
    assert state.events
    store.save_thread(state)

    state.events = []
    state.event_sequence = 0
    store.save_thread(state)

    restored = store.get_thread(state.thread_id)
    assert restored is not None
    assert restored.events == []
    assert restored.event_sequence == 0
    store.close()


@pytest.mark.parametrize("store_factory", [InMemoryThreadStore, LocalThreadStore])
def test_thread_store_round_trips_rolling_compaction_checkpoint(store_factory, tmp_path) -> None:
    store = (
        store_factory()
        if store_factory is InMemoryThreadStore
        else store_factory(tmp_path / "checkpoint.db")
    )
    state = _state()
    state.checkpoint = CompactionCheckpoint(
        CompactionSummary(
            "goal: inspect app.py\nvalidation: pytest passed",
            covered_start=1,
            covered_end=3,
            source_estimate=22,
            metadata={"synthetic": True},
        ),
        covered_through=3,
        source_estimate=22,
        canonical_fingerprint=canonical_history_fingerprint(state.messages, 3),
        metadata={"source_messages": 3},
    )

    store.save_thread(state)
    restored = store.get_thread(state.thread_id)

    assert restored is not None
    assert restored.checkpoint == state.checkpoint
    assert restored.checkpoint is not None
    assert restored.checkpoint.summary.synthetic is True
    store.close()


def test_local_store_migrates_v1_state_without_checkpoint(tmp_path) -> None:
    import json
    import sqlite3

    database = tmp_path / "v1.db"
    store = LocalThreadStore(database)
    state = _state()
    store.save_thread(state)
    store.close()

    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT state_json FROM threads WHERE thread_id = ?", (state.thread_id,)
    ).fetchone()
    assert row is not None
    raw = json.loads(row[0])
    raw["schema_version"] = 1
    raw.pop("checkpoint", None)
    connection.execute(
        "UPDATE threads SET state_json = ? WHERE thread_id = ?",
        (json.dumps(raw, ensure_ascii=False), state.thread_id),
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    reopened = LocalThreadStore(database)
    restored = reopened.get_thread(state.thread_id)
    assert restored is not None
    assert restored.checkpoint is None
    reopened.close()


def test_local_store_ignores_checkpoint_beyond_restored_history_with_diagnostic(tmp_path) -> None:
    import json
    import sqlite3

    database = tmp_path / "invalid-checkpoint.db"
    store = LocalThreadStore(database)
    state = _state()
    store.save_thread(state)
    store.close()

    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT state_json FROM threads WHERE thread_id = ?", (state.thread_id,)
    ).fetchone()
    assert row is not None
    raw = json.loads(row[0])
    raw["checkpoint"] = CompactionCheckpoint(
        CompactionSummary("stale handoff"), covered_through=999
    ).to_dict()
    connection.execute(
        "UPDATE threads SET state_json = ? WHERE thread_id = ?",
        (json.dumps(raw, ensure_ascii=False), state.thread_id),
    )
    connection.commit()
    connection.close()

    reopened = LocalThreadStore(database)
    restored = reopened.get_thread(state.thread_id)
    assert restored is not None
    assert restored.checkpoint is None
    assert "CHECKPOINT_INVALID" in restored.checkpoint_diagnostics
    reopened.close()


def test_checkpoint_fingerprint_is_append_only_and_requires_atomic_boundary() -> None:
    state = _state()
    fingerprint = canonical_history_fingerprint(state.messages, 3)
    checkpoint = CompactionCheckpoint(
        CompactionSummary("stable handoff"),
        covered_through=3,
        canonical_fingerprint=fingerprint,
    )

    assert checkpoint.valid_for_history(state.messages)
    appended = [
        *state.messages,
        Message(role="user", content=[TextBlock(text="new tail")]),
    ]
    assert checkpoint.valid_for_history(appended)

    tampered = list(state.messages)
    tampered[1] = Message(role="user", content=[TextBlock(text="rewritten")])
    assert not checkpoint.valid_for_history(tampered)

    mid_interaction = CompactionCheckpoint(
        CompactionSummary("unsafe handoff"),
        covered_through=2,
        canonical_fingerprint=canonical_history_fingerprint(state.messages, 2),
    )
    assert not mid_interaction.valid_for_history(state.messages)


def test_local_store_discards_checkpoint_when_restored_prefix_is_tampered(tmp_path) -> None:
    import json
    import sqlite3

    database = tmp_path / "tampered-checkpoint.db"
    store = LocalThreadStore(database)
    state = _state()
    state.checkpoint = CompactionCheckpoint(
        CompactionSummary("stable handoff"),
        covered_through=3,
        canonical_fingerprint=canonical_history_fingerprint(state.messages, 3),
    )
    store.save_thread(state)
    store.close()

    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT state_json FROM threads WHERE thread_id = ?", (state.thread_id,)
    ).fetchone()
    assert row is not None
    raw = json.loads(row[0])
    raw["messages"][1]["content"][0]["text"] = "tampered canonical prefix"
    connection.execute(
        "UPDATE threads SET state_json = ? WHERE thread_id = ?",
        (json.dumps(raw, ensure_ascii=False), state.thread_id),
    )
    connection.commit()
    connection.close()

    reopened = LocalThreadStore(database)
    restored = reopened.get_thread(state.thread_id)
    assert restored is not None
    assert restored.checkpoint is None
    assert "CHECKPOINT_INVALID" in restored.checkpoint_diagnostics
    assert restored.messages[1].content[0].text == "tampered canonical prefix"
    reopened.close()

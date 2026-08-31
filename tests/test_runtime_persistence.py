from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.core.messages import Message, TextBlock, ToolCallBlock
from agent.model.provider import LLMProvider
from agent.model.types import LLMRequest, LLMResponse, Usage
from agent.runtime import (
    ApprovalMode,
    IdempotencyConflictError,
    IdempotencyInterruptedError,
    ModelSettings,
    ThreadRuntime,
    ThreadStatus,
    TurnStatus,
    WorkspaceUnavailableError,
)
from agent.runtime.thread_store import (
    InMemoryThreadStore,
    LocalThreadStore,
    StoredActiveTurn,
    StoredIdempotency,
    ThreadState,
)
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

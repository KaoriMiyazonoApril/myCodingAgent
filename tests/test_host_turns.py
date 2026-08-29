from __future__ import annotations

import asyncio
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient

from agent.core.messages import Message, TextBlock, ToolCallBlock
from agent.host.app import create_app
from agent.host.model_catalog import ModelDiscovery
from agent.host.provider_config import ProviderStore
from agent.host.workspace import WorkspaceBrowser
from agent.model.provider import LLMProvider
from agent.model.types import LLMRequest, LLMResponse, Usage
from agent.runtime import ModelSettings, ThreadRuntime
from agent.tools.registry import ToolRegistry
from tests.sandbox_support import create_test_tool_registry


class _Catalog:
    async def discover(self, provider_id: str, api_key: str) -> ModelDiscovery:
        return ModelDiscovery([], cached=False)


class _PausingProvider(LLMProvider):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release: asyncio.Event | None = None

    async def chat(self, request: LLMRequest) -> LLMResponse:
        self.release = asyncio.Event()
        self.started.set()
        await self.release.wait()
        return LLMResponse(
            message=Message(
                role="assistant",
                content=[TextBlock(text="Finished")],
            ),
            finish_reason="stop",
            usage=Usage(),
        )


class _ScriptedProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = iter(responses)

    async def chat(self, request: LLMRequest) -> LLMResponse:
        return next(self._responses)


def _tool_response(call: ToolCallBlock) -> LLMResponse:
    return LLMResponse(
        message=Message(role="assistant", content=[call]),
        finish_reason="tool_calls",
        usage=Usage(input_tokens=2, output_tokens=1, total_tokens=3),
    )


def _app(tmp_path: Path, provider: LLMProvider):
    store = ProviderStore(tmp_path / "providers.json")
    store.save_provider(
        "deepseek",
        api_key="test-key",
        selected_model="deepseek-chat",
    )
    store.set_default("deepseek", model="deepseek-chat")

    def runtime_factory(default_settings: ModelSettings) -> ThreadRuntime:
        return ThreadRuntime(
            tool_registry_factory=lambda workspace: ToolRegistry(),
            provider_resolver=lambda provider_id, model: provider,
            default_settings=default_settings,
        )

    return create_app(
        provider_store=store,
        model_catalog=_Catalog(),
        workspace_browser=WorkspaceBrowser([tmp_path]),
        runtime_factory=runtime_factory,
    )


def _create_thread(client: TestClient, workspace: Path) -> str:
    response = client.post("/api/threads", json={"workspace": str(workspace)})
    assert response.status_code == 201
    return response.json()["thread"]["snapshot"]["thread_id"]


def _wait_for_idle(client: TestClient, thread_id: str) -> dict[str, object]:
    for _ in range(100):
        body = client.get(f"/api/threads/{thread_id}").json()["thread"]
        if body["submission"] is None:
            return body
        time.sleep(0.01)
    raise AssertionError("Turn task did not reach terminal cleanup")


def test_turn_post_returns_accepted_without_waiting_and_active_cancel_is_real(
    tmp_path,
) -> None:
    provider = _PausingProvider()
    with TestClient(_app(tmp_path, provider)) as client:
        thread_id = _create_thread(client, tmp_path)

        started_at = time.monotonic()
        accepted = client.post(
            f"/api/threads/{thread_id}/turns",
            json={"message": "First line\nSecond line"},
        )
        elapsed = time.monotonic() - started_at
        assert accepted.status_code == 202
        assert elapsed < 0.5
        assert accepted.json()["submission"]["status"] == "starting"
        assert provider.started.wait(timeout=2)

        duplicate = client.post(
            f"/api/threads/{thread_id}/turns",
            json={"message": "duplicate"},
        )
        running = client.get(f"/api/threads/{thread_id}")
        cancelled = client.post(f"/api/threads/{thread_id}/cancel")
        terminal = _wait_for_idle(client, thread_id)

        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "TURN_ALREADY_RUNNING"
        assert running.json()["thread"]["submission"]["status"] == "running"
        assert cancelled.status_code == 202
        assert cancelled.json()["submission"]["status"] == "cancelling"
        assert terminal["snapshot"]["status"] == "idle"
        assert terminal["snapshot"]["latest_turn"]["status"] == "cancelled"


def test_starting_turn_can_be_cancelled_without_conversation_mutation(tmp_path) -> None:
    provider = _PausingProvider()
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        thread_id = _create_thread(client, tmp_path)
        runtime = app.state.thread_host.runtime
        assert runtime is not None
        validation_started = threading.Event()
        validation_release = threading.Event()

        def blocking_validation(workspace: Path) -> None:
            validation_started.set()
            validation_release.wait(timeout=2)

        runtime._workspace_validator.validate = blocking_validation
        accepted = client.post(
            f"/api/threads/{thread_id}/turns",
            json={"message": "cancel during preflight"},
        )
        assert accepted.status_code == 202
        assert validation_started.wait(timeout=2)
        starting = client.get(f"/api/threads/{thread_id}").json()["thread"]

        duplicate = client.post(
            f"/api/threads/{thread_id}/turns",
            json={"message": "duplicate preflight"},
        )
        cancelled = client.post(f"/api/threads/{thread_id}/cancel")
        validation_release.set()
        terminal = _wait_for_idle(client, thread_id)

        assert starting["submission"]["status"] == "starting"
        assert duplicate.status_code == 409
        assert cancelled.status_code == 202
        assert terminal["snapshot"]["messages"] == []
        events = runtime.get_events(thread_id).events
        assert [event.type for event in events] == ["turn_rejected"]
        assert events[0].payload["error"]["code"] == "TURN_CANCELLED_BEFORE_START"


def test_idle_cancel_returns_a_stable_conflict(tmp_path) -> None:
    with TestClient(_app(tmp_path, _PausingProvider())) as client:
        thread_id = _create_thread(client, tmp_path)

        response = client.post(f"/api/threads/{thread_id}/cancel")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NO_ACTIVE_TURN"


def test_closing_an_active_host_thread_cancels_and_reaches_closed(tmp_path) -> None:
    provider = _PausingProvider()
    with TestClient(_app(tmp_path, provider)) as client:
        thread_id = _create_thread(client, tmp_path)
        client.post(
            f"/api/threads/{thread_id}/turns",
            json={"message": "close while running"},
        )
        assert provider.started.wait(timeout=2)

        closing = client.post(f"/api/threads/{thread_id}/close")
        terminal = _wait_for_idle(client, thread_id)

        assert closing.status_code == 200
        assert terminal["snapshot"]["status"] == "closed"
        assert terminal["snapshot"]["latest_turn"]["status"] == "cancelled"


def test_host_runs_real_runtime_local_tools_and_returns_recoverable_diff(
    tmp_path,
) -> None:
    source = tmp_path / "app.py"
    source.write_text("def answer():\n    return 1\n", encoding="utf-8")
    provider = _ScriptedProvider(
        [
            _tool_response(
                ToolCallBlock(
                    id="host-read",
                    name="read_file",
                    arguments={"path": "app.py"},
                )
            ),
            _tool_response(
                ToolCallBlock(
                    id="host-edit",
                    name="edit_file",
                    arguments={
                        "path": "app.py",
                        "old_string": "return 1",
                        "new_string": "return 2",
                    },
                )
            ),
            _tool_response(
                ToolCallBlock(
                    id="host-test",
                    name="run_command",
                    arguments={
                        "command": (
                            "python3 -c \"import app; "
                            "assert app.answer() == 2; print('passed')\""
                        )
                    },
                )
            ),
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[TextBlock(text="Updated and tested app.py.")],
                ),
                finish_reason="stop",
                usage=Usage(input_tokens=4, output_tokens=2, total_tokens=6),
            ),
        ]
    )
    store = ProviderStore(tmp_path / "providers.json")
    store.save_provider(
        "deepseek",
        api_key="test-key",
        selected_model="deepseek-chat",
    )
    store.set_default("deepseek", model="deepseek-chat")

    def runtime_factory(default_settings: ModelSettings) -> ThreadRuntime:
        return ThreadRuntime(
            tool_registry_factory=create_test_tool_registry,
            provider_resolver=lambda provider_id, model: provider,
            default_settings=default_settings,
        )

    app = create_app(
        provider_store=store,
        model_catalog=_Catalog(),
        workspace_browser=WorkspaceBrowser([tmp_path]),
        runtime_factory=runtime_factory,
    )
    with TestClient(app) as client:
        thread_id = _create_thread(client, tmp_path)
        accepted = client.post(
            f"/api/threads/{thread_id}/turns",
            json={"message": "Change and test the answer."},
        )
        terminal = _wait_for_idle(client, thread_id)

    summary = terminal["snapshot"]["latest_turn"]
    assert accepted.status_code == 202
    assert source.read_text(encoding="utf-8") == "def answer():\n    return 2\n"
    assert summary["status"] == "completed"
    assert summary["iterations"] == 4
    assert summary["tool_calls"] == 3
    assert summary["modified_files"] == ["app.py"]
    assert "-    return 1" in summary["file_diffs"][0]["diff"]
    assert "+    return 2" in summary["file_diffs"][0]["diff"]
    assert summary["diff_complete"] is False

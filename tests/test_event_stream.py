from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent.host.app import create_app
from agent.host.event_stream import EventStreamAdapter, select_event_cursor
from agent.host.model_catalog import ModelDiscovery
from agent.host.provider_config import ProviderStore
from agent.host.workspace import WorkspaceBrowser
from agent.runtime import ModelSettings, ThreadRuntime
from agent.runtime.events import EventBuffer
from agent.tools.registry import ToolRegistry


class _Threads:
    def __init__(self, *, capacity: int = 8, status: str = "idle") -> None:
        self.buffer = EventBuffer(capacity)
        self.status = status
        self.thread_id = "thread-1"

    def emit(self, event_type: str, payload: dict[str, object] | None = None):
        return self.buffer.emit_thread(
            thread_id=self.thread_id,
            event_type=event_type,
            payload=payload or {},
        )

    def get_events(self, thread_id: str, *, after_event_id=None):
        assert thread_id == self.thread_id
        return self.buffer.read(after_event_id)

    def get_thread(self, thread_id: str):
        assert thread_id == self.thread_id
        latest = self.buffer.read().latest_event_id
        return {
            "schema_version": 1,
            "snapshot": {
                "schema_version": 1,
                "thread_id": thread_id,
                "status": self.status,
                "messages": [],
                "latest_turn": None,
            },
            "event_cursor": latest,
            "submission": None,
        }


class _Submissions:
    def __init__(self, value=None, failure=None) -> None:
        self.value = value
        self.failure = failure
        self.cancel_calls = 0

    def inspect(self, thread_id: str):
        return self.value

    def inspect_failure(self, thread_id: str):
        return self.failure


async def _never_disconnected() -> bool:
    return False


def _data(frame: str) -> dict[str, object]:
    line = next(line for line in frame.splitlines() if line.startswith("data: "))
    return json.loads(line.removeprefix("data: "))


def test_sse_frames_preserve_runtime_order_type_id_and_strict_json() -> None:
    async def scenario() -> tuple[list[str], str]:
        threads = _Threads()
        first = threads.emit("turn_started", {"value": 1})
        second = threads.emit("turn_completed", {"value": 2})
        adapter = EventStreamAdapter(threads, _Submissions())
        stream = adapter.stream(
            threads.thread_id,
            after_event_id=None,
            disconnected=_never_disconnected,
        )
        frames = [await anext(stream), await anext(stream)]
        await stream.aclose()
        return frames, second.event_id

    frames, latest_id = asyncio.run(scenario())

    assert frames[0].startswith("event: turn_started\n")
    assert frames[1].startswith("event: turn_completed\n")
    assert f"id: {latest_id}\n" in frames[1]
    assert _data(frames[0])["type"] == "turn_started"
    assert _data(frames[1])["payload"] == {"value": 2}


def test_reconnect_after_event_id_only_emits_later_runtime_events() -> None:
    async def scenario() -> str:
        threads = _Threads()
        first = threads.emit("turn_started")
        threads.emit("model_response")
        adapter = EventStreamAdapter(threads, _Submissions())
        stream = adapter.stream(
            threads.thread_id,
            after_event_id=first.event_id,
            disconnected=_never_disconnected,
        )
        frame = await anext(stream)
        await stream.aclose()
        return frame

    frame = asyncio.run(scenario())

    assert frame.startswith("event: model_response\n")


def test_expired_cursor_recovers_with_snapshot_and_latest_cursor() -> None:
    async def scenario() -> tuple[str, str]:
        threads = _Threads(capacity=2)
        expired = threads.emit("old")
        threads.emit("newer")
        latest = threads.emit("latest")
        adapter = EventStreamAdapter(threads, _Submissions())
        stream = adapter.stream(
            threads.thread_id,
            after_event_id=expired.event_id,
            disconnected=_never_disconnected,
        )
        frame = await anext(stream)
        await stream.aclose()
        return frame, latest.event_id

    frame, latest_id = asyncio.run(scenario())
    payload = _data(frame)

    assert frame.startswith("event: snapshot\n")
    assert f"id: {latest_id}\n" in frame
    assert payload["cursor"] == latest_id
    assert payload["thread"]["snapshot"]["thread_id"] == "thread-1"


def test_expired_cursor_with_empty_buffer_explicitly_clears_sse_id() -> None:
    async def scenario() -> str:
        threads = _Threads()
        adapter = EventStreamAdapter(threads, _Submissions())
        stream = adapter.stream(
            threads.thread_id,
            after_event_id="obsolete",
            disconnected=_never_disconnected,
        )
        frame = await anext(stream)
        await stream.aclose()
        return frame

    frame = asyncio.run(scenario())

    assert frame.startswith("event: snapshot\nid: \n")
    assert _data(frame)["cursor"] is None


def test_background_host_failure_emits_one_recoverable_snapshot() -> None:
    async def scenario() -> str:
        threads = _Threads()
        submissions = _Submissions(
            failure={
                "code": "TURN_TASK_FAILED",
                "message": "Agent Turn task failed",
            }
        )
        adapter = EventStreamAdapter(threads, submissions)
        stream = adapter.stream(
            threads.thread_id,
            after_event_id=None,
            disconnected=_never_disconnected,
        )
        frame = await anext(stream)
        await stream.aclose()
        return frame

    frame = asyncio.run(scenario())
    payload = _data(frame)

    assert frame.startswith("event: snapshot\nid: \n")
    assert payload["cursor"] is None
    assert payload["thread"]["host_error"] == {
        "code": "TURN_TASK_FAILED",
        "message": "Agent Turn task failed",
    }


@pytest.mark.parametrize(
    ("status", "submission", "expected_interval"),
    [
        ("idle", None, 1.0),
        ("closed", None, 1.0),
        ("running", None, 0.1),
        ("idle", {"status": "starting"}, 0.1),
    ],
)
def test_adapter_uses_active_and_idle_poll_intervals(
    status,
    submission,
    expected_interval,
) -> None:
    async def scenario() -> list[float]:
        threads = _Threads(status=status)
        disconnected = False
        sleeps: list[float] = []

        async def check() -> bool:
            return disconnected

        async def sleep(delay: float) -> None:
            nonlocal disconnected
            sleeps.append(delay)
            disconnected = True

        adapter = EventStreamAdapter(
            threads,
            _Submissions(submission),
            sleep=sleep,
        )
        stream = adapter.stream(
            threads.thread_id,
            after_event_id=None,
            disconnected=check,
        )
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        return sleeps

    assert asyncio.run(scenario()) == [expected_interval]


def test_adapter_sends_heartbeat_without_mutating_or_cancelling_runtime() -> None:
    async def scenario() -> tuple[str, list[float], int]:
        threads = _Threads()
        submissions = _Submissions()
        now = 0.0
        sleeps: list[float] = []

        async def sleep(delay: float) -> None:
            nonlocal now
            sleeps.append(delay)
            now += delay

        adapter = EventStreamAdapter(
            threads,
            submissions,
            heartbeat_interval=3,
            clock=lambda: now,
            sleep=sleep,
        )
        stream = adapter.stream(
            threads.thread_id,
            after_event_id=None,
            disconnected=_never_disconnected,
        )
        heartbeat = await anext(stream)
        await stream.aclose()
        return heartbeat, sleeps, submissions.cancel_calls

    heartbeat, sleeps, cancel_calls = asyncio.run(scenario())

    assert heartbeat == ": heartbeat\n\n"
    assert sleeps == [1.0, 1.0, 1.0]
    assert cancel_calls == 0


def test_query_cursor_precedes_last_event_id_header() -> None:
    assert select_event_cursor("query", "header") == "query"
    assert select_event_cursor("", "header") == ""
    assert select_event_cursor(None, "header") == "header"


def test_sse_route_maps_query_and_header_cursors_with_streaming_headers(
    tmp_path,
) -> None:
    class Catalog:
        async def discover(self, provider_id: str, api_key: str) -> ModelDiscovery:
            return ModelDiscovery([], cached=False)

    class OneFrameStream:
        def __init__(self) -> None:
            self.cursors: list[str | None] = []

        async def stream(self, thread_id, *, after_event_id, disconnected):
            self.cursors.append(after_event_id)
            yield "event: ready\nid: event-1\ndata: {}\n\n"

    store = ProviderStore(tmp_path / "providers.json")
    store.save_provider("deepseek", api_key="key", selected_model="model")
    store.set_default("deepseek", model="model")
    stream = OneFrameStream()

    def runtime_factory(settings: ModelSettings) -> ThreadRuntime:
        return ThreadRuntime(
            tool_registry_factory=lambda workspace: ToolRegistry(),
            provider_resolver=lambda provider_id, model: None,  # type: ignore[return-value]
            default_settings=settings,
        )

    app = create_app(
        provider_store=store,
        model_catalog=Catalog(),
        workspace_browser=WorkspaceBrowser([tmp_path]),
        runtime_factory=runtime_factory,
        event_stream_adapter=stream,  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        created = client.post("/api/threads", json={"workspace": str(tmp_path)})
        thread_id = created.json()["thread"]["snapshot"]["thread_id"]
        query = client.get(
            f"/api/threads/{thread_id}/events",
            params={"after_event_id": "query"},
            headers={"Last-Event-ID": "header"},
        )
        header = client.get(
            f"/api/threads/{thread_id}/events",
            headers={"Last-Event-ID": "header"},
        )

    assert stream.cursors == ["query", "header"]
    assert query.status_code == 200
    assert query.headers["content-type"].startswith("text/event-stream")
    assert query.headers["cache-control"] == "no-cache"
    assert query.text.startswith("event: ready\n")
    assert header.status_code == 200

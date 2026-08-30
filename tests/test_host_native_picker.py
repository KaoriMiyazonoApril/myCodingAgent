from __future__ import annotations

import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient
import pytest
from fastapi.testclient import TestClient

from agent.host.app import create_app
from agent.host.model_catalog import ModelDiscovery
from agent.host.native_picker import (
    NativePickerBusyError,
    NativePickerCapability,
    NativePickerInvalidResultError,
    NativePickerSelection,
    NativeWindowsFolderPicker,
    WindowsInteropUnavailableError,
    WslPathTranslationError,
)
from agent.host.provider_config import ProviderStore
from agent.host.workspace import WorkspaceBrowser
from agent.runtime import ModelSettings, ThreadRuntime
from agent.tools.registry import ToolRegistry


class _Catalog:
    async def discover(self, provider_id: str, api_key: str) -> ModelDiscovery:
        return ModelDiscovery([], cached=False)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakePicker:
    def __init__(
        self,
        *,
        capability: NativePickerCapability | None = None,
        selection: NativePickerSelection | None = None,
        error: Exception | None = None,
        translated: str = "/workspace/project",
    ) -> None:
        self._capability = capability or NativePickerCapability(available=True)
        self._selection = selection or NativePickerSelection.selected(r"C:\project")
        self._error = error
        self._translated = translated
        self.closed = 0
        self.selected = 0
        self.translated: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.wait_for_release = False

    def capability(self) -> NativePickerCapability:
        return self._capability

    async def select(self) -> NativePickerSelection:
        self.selected += 1
        self.started.set()
        if self.wait_for_release:
            await self.release.wait()
        if self._error is not None:
            raise self._error
        return self._selection

    async def translate(self, windows_path: str) -> str:
        self.translated.append(windows_path)
        if self._error is not None:
            raise self._error
        return self._translated

    async def close(self) -> None:
        self.closed += 1


def _app(tmp_path: Path, picker, browser=None):
    return create_app(
        provider_store=ProviderStore(tmp_path / "providers.json"),
        model_catalog=_Catalog(),
        workspace_browser=browser or WorkspaceBrowser([tmp_path]),
        native_picker=picker,
    )


def _configured_store(path: Path) -> ProviderStore:
    store = ProviderStore(path)
    store.save_provider(
        "deepseek",
        api_key="test-key",
        selected_model="deepseek-chat",
    )
    store.set_default("deepseek", model="deepseek-chat")
    return store


def _runtime_factory(default_settings: ModelSettings) -> ThreadRuntime:
    return ThreadRuntime(
        tool_registry_factory=lambda workspace: ToolRegistry(),
        provider_resolver=lambda provider_id, model: None,  # type: ignore[return-value]
        default_settings=default_settings,
    )


class _ImmediateEventStream:
    async def stream(self, thread_id, *, after_event_id, disconnected):
        yield "event: probe\nid: probe\ndata: {}\n\n"


def test_native_picker_capability_endpoint_is_safe_and_versioned(tmp_path: Path) -> None:
    picker = _FakePicker(
        capability=NativePickerCapability(
            available=False,
            reason_code="WINDOWS_INTEROP_UNAVAILABLE",
        )
    )

    with TestClient(_app(tmp_path, picker)) as client:
        response = client.get("/api/native-picker/capability")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "available": False,
        "reason_code": "WINDOWS_INTEROP_UNAVAILABLE",
    }


def test_native_picker_selection_translates_then_reuses_workspace_authorization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    selected = root / "project"
    selected.mkdir()
    picker = _FakePicker(translated=str(selected))
    browser = WorkspaceBrowser([root])

    with TestClient(_app(tmp_path, picker, browser)) as client:
        response = client.post("/api/native-picker/select")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "status": "selected",
        "workspace": str(selected),
    }
    assert picker.translated == [r"C:\project"]


def test_native_picker_cancel_is_not_an_error(tmp_path: Path) -> None:
    picker = _FakePicker(selection=NativePickerSelection.cancelled())

    with TestClient(_app(tmp_path, picker)) as client:
        response = client.post("/api/native-picker/select")

    assert response.status_code == 200
    assert response.json() == {"schema_version": 1, "status": "cancelled"}
    assert picker.translated == []


@pytest.mark.parametrize(
    ("error", "code", "status"),
    [
        (
            WindowsInteropUnavailableError("hidden details"),
            "WINDOWS_INTEROP_UNAVAILABLE",
            409,
        ),
        (NativePickerInvalidResultError("hidden details"), "NATIVE_PICKER_INVALID_RESULT", 502),
        (WslPathTranslationError("C:\\private"), "WSL_PATH_TRANSLATION_FAILED", 502),
    ],
)
def test_native_picker_typed_failures_use_stable_error_envelope(
    tmp_path: Path,
    error: Exception,
    code: str,
    status: int,
) -> None:
    picker = _FakePicker(error=error)

    with TestClient(_app(tmp_path, picker)) as client:
        response = client.post("/api/native-picker/select")

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "hidden details" not in response.text
    assert "C:\\private" not in response.text


def test_native_picker_selected_outside_root_uses_existing_workspace_mapping(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    picker = _FakePicker(translated=str(outside))

    with TestClient(_app(tmp_path, picker, WorkspaceBrowser([root]))) as client:
        response = client.post("/api/native-picker/select")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WORKSPACE_OUTSIDE_ROOT"


@pytest.mark.parametrize(
    ("selected_kind", "expected_code", "expected_status"),
    [
        ("file", "WORKSPACE_NOT_ACCESSIBLE", 403),
        ("symlink", "WORKSPACE_SYMLINK_NOT_ALLOWED", 400),
    ],
)
def test_native_picker_reuses_non_directory_and_symlink_workspace_rejections(
    tmp_path: Path,
    selected_kind: str,
    expected_code: str,
    expected_status: int,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    selected = root / "selected"
    if selected_kind == "file":
        selected.write_text("not a directory", encoding="utf-8")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        selected.symlink_to(outside, target_is_directory=True)
    picker = _FakePicker(translated=str(selected))

    with TestClient(_app(tmp_path, picker, WorkspaceBrowser([root]))) as client:
        response = client.post("/api/native-picker/select")

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code


@pytest.mark.anyio
async def test_picker_wait_does_not_block_health_and_shutdown_closes_adapter(tmp_path: Path):
    picker = _FakePicker(translated=str(tmp_path))
    picker.wait_for_release = True
    app = _app(tmp_path, picker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        selection = asyncio.create_task(client.post("/api/native-picker/select"))
        await picker.started.wait()
        health = await client.get("/api/health")
        assert health.status_code == 200
        threads = await client.get("/api/threads")
        assert threads.status_code == 200
        picker.release.set()
        assert (await selection).status_code == 200

    await app.state.shutdown_resources()
    assert picker.closed == 1


@pytest.mark.anyio
async def test_picker_wait_does_not_block_thread_creation_or_sse(tmp_path: Path):
    picker = _FakePicker(translated=str(tmp_path))
    picker.wait_for_release = True
    app = create_app(
        provider_store=_configured_store(tmp_path / "providers.json"),
        model_catalog=_Catalog(),
        workspace_browser=WorkspaceBrowser([tmp_path]),
        native_picker=picker,
        runtime_factory=_runtime_factory,
        event_stream_adapter=_ImmediateEventStream(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        selection = asyncio.create_task(client.post("/api/native-picker/select"))
        await picker.started.wait()

        created = await client.post("/api/threads", json={"workspace": str(tmp_path)})
        assert created.status_code == 201
        thread_id = created.json()["thread"]["snapshot"]["thread_id"]
        events = await client.get(f"/api/threads/{thread_id}/events")
        assert events.status_code == 200
        assert "event: probe" in events.text

        picker.release.set()
        assert (await selection).status_code == 200

    await app.state.shutdown_resources()


@pytest.mark.anyio
async def test_shutdown_closes_picker_and_threads_when_turn_task_shutdown_fails(
    tmp_path: Path,
) -> None:
    picker = _FakePicker(translated=str(tmp_path))
    app = _app(tmp_path, picker)
    threads_closed = 0

    async def fail_turn_shutdown() -> None:
        raise RuntimeError("turn shutdown failed")

    async def close_threads() -> None:
        nonlocal threads_closed
        threads_closed += 1

    app.state.turn_tasks.shutdown = fail_turn_shutdown
    app.state.thread_host.shutdown = close_threads

    with pytest.raises(RuntimeError, match="turn shutdown failed"):
        await app.state.shutdown_resources()

    assert picker.closed == 1
    assert threads_closed == 1


@pytest.mark.anyio
async def test_native_picker_endpoint_preserves_adapter_single_flight(tmp_path: Path):
    picker = _FakePicker(translated=str(tmp_path))
    picker.wait_for_release = True
    app = _app(tmp_path, picker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(client.post("/api/native-picker/select"))
        await picker.started.wait()
        second = await client.post("/api/native-picker/select")
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "NATIVE_PICKER_BUSY"
        picker.release.set()
        assert (await first).status_code == 200

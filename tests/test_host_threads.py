from __future__ import annotations

from pathlib import Path
import time

from fastapi.testclient import TestClient

from agent.host.app import create_app
from agent.host.thread_service import ThreadHost
from agent.host.model_catalog import ModelDiscovery
from agent.host.provider_config import ProviderStore
from agent.host.workspace import WorkspaceBrowser
from agent.model.types import ProviderCapabilities, ThinkingCapabilities
from agent.runtime import (
    ApprovalMode,
    ModelSettings,
    ThreadRuntime,
    ThinkingKeep,
    ThinkingSettings,
    UnsafeWorkspaceError,
)
from agent.tools.registry import ToolRegistry


class _Catalog:
    async def discover(self, provider_id: str, api_key: str) -> ModelDiscovery:
        return ModelDiscovery([], cached=False)


def _empty_tools(workspace: Path) -> ToolRegistry:
    return ToolRegistry()


def _configured_store(path: Path) -> ProviderStore:
    store = ProviderStore(path)
    store.save_provider(
        "deepseek",
        api_key="test-key",
        selected_model="deepseek-chat",
    )
    store.set_default("deepseek", model="deepseek-chat")
    return store


def _runtime_factory(calls: list[ModelSettings]):
    def create(default_settings: ModelSettings) -> ThreadRuntime:
        calls.append(default_settings)
        return ThreadRuntime(
            tool_registry_factory=_empty_tools,
            provider_resolver=lambda provider_id, model: None,  # type: ignore[return-value]
            default_settings=default_settings,
        )

    return create


def _client(
    tmp_path: Path,
    store: ProviderStore,
    calls: list[ModelSettings],
) -> TestClient:
    return TestClient(
        create_app(
            provider_store=store,
            model_catalog=_Catalog(),
            workspace_browser=WorkspaceBrowser([tmp_path]),
            runtime_factory=_runtime_factory(calls),
        )
    )


def _workspace_id(client: TestClient, path: Path) -> str:
    response = client.post("/api/workspaces/select", json={"path": str(path)})
    assert response.status_code == 201
    return response.json()["workspace"]["workspace_id"]


def test_thread_creation_requires_configuration_without_blocking_setup_api(
    tmp_path,
) -> None:
    calls: list[ModelSettings] = []
    store = ProviderStore(tmp_path / "providers.json")
    client = _client(tmp_path, store, calls)

    workspace_id = _workspace_id(client, tmp_path)
    rejected = client.post("/api/threads", json={"workspace_id": workspace_id})

    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "CONFIGURATION_REQUIRED"
    assert client.get("/api/providers").status_code == 200
    assert client.get("/api/workspaces").status_code == 200
    assert client.get("/api/threads").json()["threads"] == []
    assert calls == []


def test_thread_create_list_and_get_use_runtime_snapshot_and_lazy_singleton(
    tmp_path,
) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    calls: list[ModelSettings] = []
    store = _configured_store(tmp_path / "providers.json")
    client = _client(tmp_path, store, calls)
    first_workspace_id = _workspace_id(client, first_workspace)

    first = client.post(
        "/api/threads", json={"workspace_id": first_workspace_id}
    )
    store.save_provider(
        "moonshot",
        api_key="moonshot-key",
        selected_model="kimi-k2",
    )
    store.set_default("moonshot", model="kimi-k2")
    second_workspace_id = _workspace_id(client, second_workspace)
    second = client.post(
        "/api/threads", json={"workspace_id": second_workspace_id}
    )

    assert first.status_code == 201
    first_view = first.json()["thread"]
    assert first_view["snapshot"]["workspace"] == str(first_workspace)
    assert first_view["snapshot"]["settings"]["provider_config_id"] == "deepseek"
    assert first_view["snapshot"]["settings"]["model"] == "deepseek-chat"
    assert first_view["snapshot"]["settings"]["version"] == 0
    assert first_view["event_cursor"] is None
    assert first_view["submission"] is None
    capabilities = client.get(
        f"/api/threads/{first_view['snapshot']['thread_id']}/capabilities"
    )
    assert capabilities.status_code == 200
    assert capabilities.json()["capabilities"] == {
        "thinking_supported": False,
        "supports_thinking_budget": False,
        "supported_keep_values": [],
        "thinking": {
            "supported": False,
            "supports_budget_tokens": False,
            "supported_keep_values": [],
        },
    }
    assert second.status_code == 201
    assert second.json()["thread"]["snapshot"]["settings"]["model"] == "kimi-k2"
    assert len(calls) == 1
    assert calls[0].provider_config_id == "deepseek"

    listed = client.get("/api/threads")
    thread_id = first_view["snapshot"]["thread_id"]
    fetched = client.get(f"/api/threads/{thread_id}")
    assert listed.status_code == 200
    assert len(listed.json()["threads"]) == 2
    assert fetched.json()["thread"] == first_view


def test_thread_settings_conflict_close_and_closed_mutation_are_stable(
    tmp_path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    calls: list[ModelSettings] = []
    store = _configured_store(tmp_path / "providers.json")
    store.save_provider(
        "glm",
        api_key="glm-key",
        selected_model="glm-4.5",
    )
    client = _client(tmp_path, store, calls)
    workspace_id = _workspace_id(client, workspace)
    created = client.post("/api/threads", json={"workspace_id": workspace_id})
    thread_id = created.json()["thread"]["snapshot"]["thread_id"]

    updated = client.patch(
        f"/api/threads/{thread_id}/settings",
        json={
            "expected_version": 0,
            "provider_config_id": "glm",
            "model": "glm-4.5",
            "temperature": 0.3,
            "max_tokens": 4096,
        },
    )
    stale = client.patch(
        f"/api/threads/{thread_id}/settings",
        json={
            "expected_version": 0,
            "provider_config_id": "glm",
            "model": "glm-4.5",
        },
    )
    closed = client.post(f"/api/threads/{thread_id}/close")
    closed_again = client.post(f"/api/threads/{thread_id}/close")
    closed_update = client.patch(
        f"/api/threads/{thread_id}/settings",
        json={
            "expected_version": 1,
            "provider_config_id": "glm",
            "model": "glm-4.5",
        },
    )

    assert updated.status_code == 200
    settings = updated.json()["thread"]["snapshot"]["settings"]
    assert settings["version"] == 1
    assert settings["provider_config_id"] == "glm"
    assert settings["limits"] == {
        "max_iterations": 20,
        "max_tool_calls": 50,
        "max_execution_seconds": 900,
    }
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "SETTINGS_CONFLICT"
    assert closed.status_code == 200
    assert closed.json()["thread"]["snapshot"]["status"] == "closed"
    assert closed_again.status_code == 200
    assert closed_again.json()["thread"]["snapshot"]["status"] == "closed"
    assert closed_update.status_code == 409
    assert closed_update.json()["error"]["code"] == "THREAD_CLOSED"


def test_candidate_capabilities_are_authoritative_for_settings_switches(tmp_path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = _configured_store(tmp_path / "providers.json")
    store.save_provider("moonshot", api_key="moonshot-key", selected_model="kimi-k2")
    capabilities = {
        ("deepseek", "deepseek-chat"): ProviderCapabilities(
            thinking=ThinkingCapabilities(
                supported=True,
                supports_budget_tokens=True,
                supported_keep_values=("all",),
            )
        ),
        ("moonshot", "kimi-k2"): ProviderCapabilities(),
    }

    def resolve(provider_id: str, model: str):
        del provider_id, model
        return None

    resolve.capabilities_for = lambda provider_id, model: capabilities[(  # type: ignore[attr-defined]
        provider_id,
        model,
    )]

    def runtime_factory(default_settings: ModelSettings) -> ThreadRuntime:
        return ThreadRuntime(
            tool_registry_factory=_empty_tools,
            provider_resolver=resolve,
            default_settings=default_settings,
        )

    browser = WorkspaceBrowser([tmp_path])
    host = ThreadHost(
        provider_store=store,
        workspace_browser=browser,
        runtime_factory=runtime_factory,
    )
    workspace_id = browser.select(str(workspace)).workspace_id
    created = host.create_thread(workspace_id)
    thread_id = created["snapshot"]["thread_id"]

    supported = host.capabilities_for(
        thread_id,
        provider_config_id="deepseek",
        model="deepseek-chat",
    )
    unsupported = host.capabilities_for(
        thread_id,
        provider_config_id="moonshot",
        model="kimi-k2",
    )
    assert supported["supports_thinking_budget"] is True
    assert unsupported["thinking_supported"] is False

    switched = host.update_settings(
        thread_id,
        expected_version=0,
        settings=ModelSettings(
            provider_config_id="moonshot",
            model="kimi-k2",
            thinking=ThinkingSettings(
                enabled=True,
                budget_tokens=512,
                keep=ThinkingKeep.ALL,
            ),
        ),
    )
    assert switched["snapshot"]["settings"]["thinking"] is None

    switched_back = host.update_settings(
        thread_id,
        expected_version=1,
        settings=ModelSettings(
            provider_config_id="deepseek",
            model="deepseek-chat",
            thinking=ThinkingSettings(
                enabled=True,
                budget_tokens=512,
                keep=ThinkingKeep.ALL,
            ),
        ),
    )
    assert switched_back["snapshot"]["settings"]["thinking"] == {
        "enabled": True,
        "budget_tokens": 512,
        "keep": "all",
    }


def test_thread_api_maps_not_found_invalid_workspace_and_provider(tmp_path) -> None:
    calls: list[ModelSettings] = []
    store = _configured_store(tmp_path / "providers.json")
    client = _client(tmp_path, store, calls)

    missing = client.get("/api/threads/not-real")
    escaped = client.post(
        "/api/workspaces/select", json={"path": str(tmp_path.parent)}
    )
    workspace_id = _workspace_id(client, tmp_path)
    invalid_provider = client.post(
        "/api/threads",
        json={
            "workspace_id": workspace_id,
            "provider_config_id": "glm",
            "model": "glm-4.5",
        },
    )

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "THREAD_NOT_FOUND"
    assert escaped.status_code == 400
    assert escaped.json()["error"]["code"] == "OUTSIDE_ALLOWED_ROOT"
    assert invalid_provider.status_code == 409
    assert invalid_provider.json()["error"]["code"] == "CONFIGURATION_REQUIRED"


def test_thread_creation_exposes_frozen_approval_mode(tmp_path) -> None:
    calls: list[ModelSettings] = []
    store = _configured_store(tmp_path / "providers.json")
    client = _client(tmp_path, store, calls)

    response = client.post(
        "/api/threads",
        json={
            "workspace_id": _workspace_id(client, tmp_path),
            "approval_mode": "never",
        },
    )

    assert response.status_code == 201
    settings = response.json()["thread"]["snapshot"]["settings"]
    assert settings["approval_mode"] == ApprovalMode.NEVER.value
    assert calls[0].approval_mode is ApprovalMode.NEVER


def test_production_host_restores_thread_catalog_after_restart(tmp_path) -> None:
    provider_store = _configured_store(tmp_path / "providers.json")
    workspace = tmp_path / "project"
    workspace.mkdir()
    state_dir = tmp_path / "state"

    first_app = create_app(
        provider_store=provider_store,
        model_catalog=_Catalog(),
        workspace_browser=WorkspaceBrowser([tmp_path]),
        state_dir=state_dir,
    )
    with TestClient(first_app) as client:
        workspace_id = _workspace_id(client, workspace)
        created = client.post(
            "/api/threads",
            json={"workspace_id": workspace_id},
        )
        assert created.status_code == 201
        thread = created.json()["thread"]
        thread_id = thread["snapshot"]["thread_id"]
        settings = client.patch(
            f"/api/threads/{thread_id}/settings",
            json={
                "expected_version": 0,
                "provider_config_id": "deepseek",
                "model": "deepseek-chat",
                "approval_mode": "never",
            },
        )
        assert settings.status_code == 200

    second_app = create_app(
        provider_store=provider_store,
        model_catalog=_Catalog(),
        workspace_browser=WorkspaceBrowser([tmp_path]),
        state_dir=state_dir,
    )
    with TestClient(second_app) as client:
        listed = client.get("/api/threads")
        reopened = client.get(f"/api/threads/{thread_id}")

    assert listed.status_code == 200
    assert [item["snapshot"]["thread_id"] for item in listed.json()["threads"]] == [
        thread_id
    ]
    restored = reopened.json()["thread"]
    assert restored["snapshot"]["workspace"] == str(workspace)
    assert restored["snapshot"]["settings"]["version"] == 1
    assert restored["snapshot"]["settings"]["approval_mode"] == "never"
    assert restored["event_cursor"] is not None


def test_restored_deleted_workspace_is_listable_but_turn_is_unavailable(
    tmp_path,
) -> None:
    provider_store = _configured_store(tmp_path / "providers.json")
    workspace = tmp_path / "project"
    workspace.mkdir()
    state_dir = tmp_path / "state"

    first_app = create_app(
        provider_store=provider_store,
        model_catalog=_Catalog(),
        workspace_browser=WorkspaceBrowser([tmp_path]),
        state_dir=state_dir,
    )
    with TestClient(first_app) as client:
        workspace_id = _workspace_id(client, workspace)
        created = client.post(
            "/api/threads",
            json={"workspace_id": workspace_id},
        )
        assert created.status_code == 201
        thread_id = created.json()["thread"]["snapshot"]["thread_id"]
    workspace.rmdir()

    second_app = create_app(
        provider_store=provider_store,
        model_catalog=_Catalog(),
        workspace_browser=WorkspaceBrowser([tmp_path]),
        state_dir=state_dir,
    )
    with TestClient(second_app) as client:
        listed = client.get("/api/threads")
        restored = client.get(f"/api/threads/{thread_id}")
        started = client.post(
            f"/api/threads/{thread_id}/turns",
            json={"message": "must not run"},
        )
        for _ in range(100):
            failure = client.get(f"/api/threads/{thread_id}").json()["thread"][
                "host_error"
            ]
            if failure is not None:
                break
            time.sleep(0.01)

    assert listed.status_code == 200
    assert listed.json()["threads"][0]["workspace"]["path"] == str(workspace)
    assert restored.status_code == 200
    assert restored.json()["thread"]["snapshot"]["workspace"] == str(workspace)
    assert started.status_code == 202
    assert failure is not None
    assert failure["code"] == "WORKSPACE_UNAVAILABLE"


def test_approval_resolution_uses_runtime_and_reports_stale_request(tmp_path) -> None:
    calls: list[ModelSettings] = []
    store = _configured_store(tmp_path / "providers.json")
    client = _client(tmp_path, store, calls)
    created = client.post(
        "/api/threads",
        json={"workspace_id": _workspace_id(client, tmp_path)},
    )
    thread_id = created.json()["thread"]["snapshot"]["thread_id"]

    response = client.post(
        f"/api/threads/{thread_id}/approvals/stale",
        json={"approved": True},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPROVAL_NOT_FOUND"


def test_runtime_workspace_safety_error_is_not_parsed_from_message(tmp_path) -> None:
    class UnsafeRuntime:
        def create_thread(self, workspace, *, settings=None):
            raise UnsafeWorkspaceError("private path details")

    store = _configured_store(tmp_path / "providers.json")
    client = TestClient(
        create_app(
            provider_store=store,
            model_catalog=_Catalog(),
            workspace_browser=WorkspaceBrowser([tmp_path]),
            runtime_factory=lambda settings: UnsafeRuntime(),  # type: ignore[arg-type]
        )
    )

    response = client.post(
        "/api/threads",
        json={"workspace_id": _workspace_id(client, tmp_path)},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNSAFE_WORKSPACE"
    assert "private path details" not in response.text

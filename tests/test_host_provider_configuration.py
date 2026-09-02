from __future__ import annotations

import asyncio
import json
import stat

from fastapi.testclient import TestClient
import pytest

from agent.host.app import create_app
from agent.host.model_catalog import (
    ModelDiscovery,
    ModelDiscoveryError,
    ProviderAuthenticationError,
    ProviderModelCatalog,
    ProviderResponseError,
)
from agent.host.provider_config import ProviderConfigurationError, ProviderStore
from agent.host.thread_service import ProductionRuntimeFactory


class _Catalog:
    async def discover(self, provider_id: str, api_key: str) -> ModelDiscovery:
        return ModelDiscovery(["model-a", "model-b"], cached=False)


class _RejectedCatalog:
    async def discover(self, provider_id: str, api_key: str) -> ModelDiscovery:
        raise ProviderAuthenticationError("rejected upstream")


class _UnexpectedCatalog:
    async def discover(self, provider_id: str, api_key: str) -> ModelDiscovery:
        raise RuntimeError(f"unexpected failure for {api_key} at /private/config")


def test_production_factory_closes_store_even_if_provider_shutdown_fails() -> None:
    class FailingPool:
        async def aclose(self) -> None:
            raise RuntimeError("provider close failed")

    class Store:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    factory = object.__new__(ProductionRuntimeFactory)
    factory._provider_pool = FailingPool()
    thread_store = Store()
    factory._thread_store = thread_store

    with pytest.raises(RuntimeError, match="provider close failed"):
        asyncio.run(factory.close())

    assert thread_store.closed is True


def test_production_factory_defaults_web_threads_to_hidden_reasoning(
    monkeypatch,
) -> None:
    from agent.runtime import ModelSettings

    captured: dict[str, object] = {}

    class RecorderRuntime:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    class Pool:
        async def aclose(self) -> None:
            pass

    class Store:
        def get_credential(self, provider_id: str) -> str | None:
            return "key"

    factory = object.__new__(ProductionRuntimeFactory)
    factory._provider_pool = Pool()
    factory._thread_store = Store()
    monkeypatch.setattr(
        "agent.host.thread_service.ThreadRuntime", RecorderRuntime
    )

    factory(ModelSettings(provider_config_id="deepseek", model="deepseek-v4-flash"))

    # Web Threads no longer force raw reasoning into the normal UI; the
    # runtime's own "hidden" default applies.
    assert "reasoning_visibility" not in captured
    assert captured["default_settings"].model == "deepseek-v4-flash"


def test_provider_configuration_persists_without_exposing_secret(tmp_path) -> None:
    config_path = tmp_path / "config" / "providers.json"
    store = ProviderStore(config_path)

    configured = store.save_provider(
        "deepseek",
        api_key="sk-secret-value",
        selected_model="deepseek-chat",
    )

    assert configured == {
        "provider_id": "deepseek",
        "display_name": "DeepSeek",
        "configured": True,
        "credential_mask": "••••alue",
        "selected_model": "deepseek-chat",
        "is_default": False,
    }
    assert "sk-secret-value" not in repr(configured)
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "default_provider_id": None,
        "providers": {
            "deepseek": {
                "api_key": "sk-secret-value",
                "selected_model": "deepseek-chat",
            }
        },
    }

    reloaded = ProviderStore(config_path)
    assert reloaded.get_credential("deepseek") == "sk-secret-value"
    assert reloaded.list_public()[0] == configured


def test_malformed_provider_configuration_fails_without_echoing_contents(
    tmp_path,
) -> None:
    config_path = tmp_path / "providers.json"
    config_path.write_text('{"api_key":"must-not-leak"', encoding="utf-8")

    with pytest.raises(
        ProviderConfigurationError,
        match="Provider configuration could not be read",
    ) as caught:
        ProviderStore(config_path)

    assert "must-not-leak" not in str(caught.value)


def test_provider_default_and_credential_clear_are_persisted(tmp_path) -> None:
    config_path = tmp_path / "providers.json"
    store = ProviderStore(config_path)
    store.save_provider(
        "moonshot",
        api_key="moonshot-secret",
        selected_model="kimi-k2",
    )

    selected = store.set_default("moonshot", model="kimi-k2-thinking")
    cleared = store.clear_credential("moonshot")

    assert selected["is_default"] is True
    assert selected["selected_model"] == "kimi-k2-thinking"
    assert cleared == {
        "provider_id": "moonshot",
        "display_name": "Moonshot / Kimi",
        "configured": False,
        "credential_mask": None,
        "selected_model": "kimi-k2-thinking",
        "is_default": True,
    }
    reloaded = ProviderStore(config_path)
    assert reloaded.default_selection() == {
        "provider_id": "moonshot",
        "model": "kimi-k2-thinking",
    }


def test_host_starts_in_provider_setup_mode(tmp_path) -> None:
    store = ProviderStore(tmp_path / "providers.json")
    client = TestClient(create_app(provider_store=store, model_catalog=_Catalog()))

    health = client.get("/api/health")
    providers = client.get("/api/providers")

    assert health.status_code == 200
    assert health.json() == {
        "schema_version": 1,
        "status": "ok",
        "configuration_required": True,
    }
    assert providers.status_code == 200
    assert providers.json()["default_provider_id"] is None
    assert [
        provider["provider_id"] for provider in providers.json()["providers"]
    ] == ["deepseek", "moonshot", "glm"]


def test_user_can_configure_provider_discover_models_and_clear_key(tmp_path) -> None:
    store = ProviderStore(tmp_path / "providers.json")
    client = TestClient(create_app(provider_store=store, model_catalog=_Catalog()))

    saved = client.put(
        "/api/providers/deepseek",
        json={"api_key": "sk-api-secret", "selected_model": "model-a"},
    )
    discovered = client.post("/api/providers/deepseek/models/discover")
    selected = client.patch(
        "/api/provider-default",
        json={"provider_id": "deepseek", "model": "model-b"},
    )
    configured_health = client.get("/api/health")
    cleared = client.delete("/api/providers/deepseek/credential")
    setup_health = client.get("/api/health")

    assert saved.status_code == 200
    assert saved.json()["provider"]["credential_mask"] == "••••cret"
    assert "sk-api-secret" not in saved.text
    assert discovered.status_code == 200
    assert discovered.json() == {
        "schema_version": 1,
        "provider_id": "deepseek",
        "models": ["model-a", "model-b"],
        "cached": False,
    }
    assert selected.status_code == 200
    assert selected.json()["provider"]["is_default"] is True
    assert selected.json()["provider"]["selected_model"] == "model-b"
    assert configured_health.json()["configuration_required"] is False
    assert cleared.status_code == 200
    assert cleared.json()["provider"]["configured"] is False
    assert setup_health.json()["configuration_required"] is True


def test_model_discovery_uses_fixed_provider_url_and_five_minute_cache() -> None:
    calls: list[tuple[str, str]] = []
    now = [10.0]

    async def fetch(base_url: str, api_key: str) -> list[str]:
        calls.append((base_url, api_key))
        return [" model-b ", "model-a", "model-a", ""]

    catalog = ProviderModelCatalog(fetcher=fetch, clock=lambda: now[0])

    first = asyncio.run(catalog.discover("deepseek", "secret"))
    second = asyncio.run(catalog.discover("deepseek", "secret"))
    now[0] += 301
    refreshed = asyncio.run(catalog.discover("deepseek", "secret"))

    assert first == ModelDiscovery(["model-a", "model-b"], cached=False)
    assert second == ModelDiscovery(["model-a", "model-b"], cached=True)
    assert refreshed == ModelDiscovery(["model-a", "model-b"], cached=False)
    assert calls == [
        ("https://api.deepseek.com", "secret"),
        ("https://api.deepseek.com", "secret"),
    ]


def test_model_discovery_distinguishes_empty_invalid_auth_and_unavailable() -> None:
    async def empty(base_url: str, api_key: str) -> list[str]:
        return []

    async def invalid(base_url: str, api_key: str) -> list[str]:
        return {"unexpected": True}  # type: ignore[return-value]

    async def malformed_record(base_url: str, api_key: str) -> list[str]:
        return ["model-a", {"missing": "id"}]  # type: ignore[list-item]

    class UpstreamAuthenticationError(RuntimeError):
        status_code = 401

    async def rejected(base_url: str, api_key: str) -> list[str]:
        raise UpstreamAuthenticationError("credential details")

    async def unavailable(base_url: str, api_key: str) -> list[str]:
        raise RuntimeError("network details")

    assert asyncio.run(
        ProviderModelCatalog(fetcher=empty).discover("glm", "secret")
    ) == ModelDiscovery([], cached=False)
    with pytest.raises(ProviderResponseError):
        asyncio.run(
            ProviderModelCatalog(fetcher=invalid).discover("glm", "secret")
        )
    with pytest.raises(ProviderResponseError):
        asyncio.run(
            ProviderModelCatalog(fetcher=malformed_record).discover("glm", "secret")
        )
    with pytest.raises(ProviderAuthenticationError) as auth_error:
        asyncio.run(
            ProviderModelCatalog(fetcher=rejected).discover("glm", "secret")
        )
    with pytest.raises(ModelDiscoveryError) as unavailable_error:
        asyncio.run(
            ProviderModelCatalog(fetcher=unavailable).discover("glm", "secret")
        )
    assert "credential details" not in str(auth_error.value)
    assert "network details" not in str(unavailable_error.value)


def test_provider_api_returns_stable_safe_errors(tmp_path) -> None:
    store = ProviderStore(tmp_path / "providers.json")
    client = TestClient(
        create_app(provider_store=store, model_catalog=_RejectedCatalog())
    )

    unknown = client.put(
        "/api/providers/not-real",
        json={"api_key": "secret"},
    )
    invalid = client.put(
        "/api/providers/deepseek",
        json={"api_key": "secret", "unexpected": True},
    )
    unconfigured = client.post("/api/providers/deepseek/models/discover")
    client.put("/api/providers/deepseek", json={"api_key": "secret"})
    rejected = client.post("/api/providers/deepseek/models/discover")

    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "PROVIDER_NOT_FOUND"
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert unconfigured.status_code == 409
    assert unconfigured.json()["error"]["code"] == "PROVIDER_NOT_CONFIGURED"
    assert rejected.status_code == 400
    assert rejected.json() == {
        "error": {
            "status": 400,
            "code": "PROVIDER_AUTHENTICATION_FAILED",
            "message": "Provider rejected the configured credential",
            "details": {},
        }
    }
    assert "secret" not in rejected.text


def test_unknown_host_error_is_stable_and_redacts_internal_details(tmp_path) -> None:
    store = ProviderStore(tmp_path / "providers.json")
    store.save_provider("deepseek", api_key="never-return-this-secret")
    client = TestClient(
        create_app(provider_store=store, model_catalog=_UnexpectedCatalog()),
        raise_server_exceptions=False,
    )

    response = client.post("/api/providers/deepseek/models/discover")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "status": 500,
            "code": "INTERNAL_ERROR",
            "message": "Agent Host request failed",
            "details": {},
        }
    }
    assert "never-return-this-secret" not in response.text
    assert "/private/config" not in response.text
    assert "traceback" not in response.text.casefold()

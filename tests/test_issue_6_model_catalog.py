from __future__ import annotations

import asyncio
from pathlib import Path
import threading

from fastapi.testclient import TestClient
import httpx

from agent.core.messages import Message, TextBlock
from agent.host.app import create_app
from agent.host.model_catalog import (
    ModelCatalogStatus,
    ModelDiscovery,
    ProviderModelCatalog,
    ProviderTimeoutError,
)
from agent.host.provider_config import ProviderStore
from agent.host.workspace import WorkspaceBrowser
from agent.model.openai_compatible import OpenAICompatibleProvider
from agent.model.presets import PROVIDER_PRESETS
from agent.model.types import (
    LLMRequest,
    ProviderConfig,
    ThinkingParameterStyle,
    ThinkingRequest,
)


def test_official_model_profiles_are_the_single_capability_source() -> None:
    deepseek = PROVIDER_PRESETS["deepseek"]
    kimi = PROVIDER_PRESETS["moonshot"]
    glm = PROVIDER_PRESETS["glm"]

    assert set(deepseek.model_profiles) == {"deepseek-v4-flash", "deepseek-v4-pro"}
    assert set(kimi.model_profiles) == {
        "kimi-k2.6",
        "kimi-k2.7-code",
        "kimi-k2.7-code-highspeed",
        "kimi-k3",
    }
    # Only the officially verified exact GLM model is profiled.  Provider
    # discovery may still return glm-5.3, but it must remain unknown.
    assert set(glm.model_profiles) == {"glm-5.2"}
    assert deepseek.capabilities_for("deepseek-v4-pro").context_window_tokens == 1_000_000
    assert kimi.capabilities_for("kimi-k2.7-code").context_window_tokens == 256_000
    assert glm.capabilities_for("glm-5.3").context_window_tokens is None

    unknown = deepseek.capabilities_for("provider-reported-but-unknown")
    assert unknown.context_window_tokens is None
    assert unknown.thinking.supported is False
    assert deepseek.model_metadata("provider-reported-but-unknown")["known"] is False


def test_host_provider_projection_enriches_remote_ids_without_leaking_status_text(
    tmp_path: Path,
) -> None:
    class Catalog:
        async def discover(self, provider_id: str, api_key: str):
            del provider_id, api_key
            raise AssertionError("not called by this read-only assertion")

        def status(self, provider_id: str):
            del provider_id
            return {
                "status": "error",
                "models": ["account-model", 123],
                "cached": False,
                "error_code": "credential=do-not-leak",
                "exception": "private traceback",
            }

    client = TestClient(
        create_app(
            provider_store=ProviderStore(tmp_path / "providers.json"),
            model_catalog=Catalog(),
        )
    )
    response = client.get("/api/providers")

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert "account-model" in [item["model_id"] for item in provider["model_profiles"]]
    assert provider["catalog"] == {
        "status": "error",
        "models": ["account-model"],
        "cached": False,
        "error_code": "PROVIDER_UNAVAILABLE",
    }
    assert "do-not-leak" not in response.text
    assert "private traceback" not in response.text


def test_provider_adapters_map_unified_thinking_to_documented_payloads() -> None:
    high = ThinkingRequest(enabled=True, intensity="high")
    disabled = ThinkingRequest(enabled=False)

    def body(provider_id: str, model: str, thinking: ThinkingRequest):
        preset = PROVIDER_PRESETS[provider_id]
        adapter = OpenAICompatibleProvider(
            ProviderConfig(
                provider=provider_id,
                base_url=preset.base_url,
                api_key="test",
                model=model,
                capabilities=preset.capabilities_for(model),
            ),
            client=object(),
        )
        request = LLMRequest(
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            thinking=thinking,
        )
        return adapter._build_request_payload(request, stream=False).get("extra_body")

    deepseek = PROVIDER_PRESETS["deepseek"].capabilities_for("deepseek-v4-pro")
    assert deepseek.thinking_parameter_style is ThinkingParameterStyle.DEEPSEEK_V4
    assert body("deepseek", "deepseek-v4-pro", high) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }

    assert body("moonshot", "kimi-k3", high) == {"reasoning_effort": "high"}
    assert body("moonshot", "kimi-k3", disabled) is None

    assert body("moonshot", "kimi-k2.6", disabled) == {
        "thinking": {"type": "disabled"}
    }

    assert body("glm", "glm-5.2", high) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }


def test_output_capability_and_request_policy_are_split() -> None:
    # Official hard maximum (capability, clamp-only) and the Harness-internal
    # request default (policy) are distinct facts for DeepSeek V4 models.
    deepseek = PROVIDER_PRESETS["deepseek"].capabilities_for("deepseek-v4-pro")
    assert deepseek.model_max_output_tokens == 384_000
    assert deepseek.default_request_max_tokens == 131_072
    assert deepseek.thinking.supports_budget_tokens is False

    flash = PROVIDER_PRESETS["deepseek"].capabilities_for("deepseek-v4-flash")
    assert flash.model_max_output_tokens == 384_000
    assert flash.default_request_max_tokens == 131_072

    # Kimi K3's documented completion field has a verified default and hard
    # maximum; other unverified families defer to provider defaults.
    kimi_k3 = PROVIDER_PRESETS["moonshot"].capabilities_for("kimi-k3")
    assert kimi_k3.model_max_output_tokens == 1_048_576
    assert kimi_k3.default_request_max_tokens == 131_072
    for provider_id, model in (
        ("moonshot", "kimi-k2.6"),
        ("glm", "glm-5.3"),
    ):
        capabilities = PROVIDER_PRESETS[provider_id].capabilities_for(model)
        assert capabilities.model_max_output_tokens is None
        assert capabilities.default_request_max_tokens is None
        assert capabilities.thinking.supports_budget_tokens is False
    glm = PROVIDER_PRESETS["glm"].capabilities_for("glm-5.2")
    assert glm.model_max_output_tokens == 128_000
    assert glm.default_request_max_tokens is None

    # Unknown models are never guessed.
    unknown = PROVIDER_PRESETS["deepseek"].capabilities_for("deepseek-unknown")
    assert unknown.model_max_output_tokens is None
    assert unknown.default_request_max_tokens is None


def test_model_catalog_single_flight_timeout_and_safe_status() -> None:
    calls: list[str] = []
    release = asyncio.Event()

    async def fetch(base_url: str, api_key: str) -> list[str]:
        del base_url
        calls.append(api_key)
        await release.wait()
        return ["model-b", "model-a", "model-a"]

    async def exercise() -> tuple[object, object, ModelCatalogStatus]:
        catalog = ProviderModelCatalog(fetcher=fetch, timeout=0.25)
        first_task = asyncio.create_task(catalog.discover("deepseek", "secret"))
        second_task = asyncio.create_task(catalog.discover("deepseek", "secret"))
        await asyncio.sleep(0)
        assert catalog.status("deepseek").status == "loading"
        release.set()
        first, second = await asyncio.gather(first_task, second_task)
        return first, second, catalog.status("deepseek")

    first, second, status = asyncio.run(exercise())
    assert first.models == ["model-a", "model-b"]
    assert first.cached is False
    assert second.models == first.models
    assert second.cached is True
    assert calls == ["secret"]
    assert status == ModelCatalogStatus(
        status="ready", models=("model-a", "model-b"), cached=False
    )

    async def slow(base_url: str, api_key: str) -> list[str]:
        del base_url, api_key
        await asyncio.sleep(0.05)
        return []

    async def timeout_exercise() -> ModelCatalogStatus:
        catalog = ProviderModelCatalog(fetcher=slow, timeout_seconds=0.001)
        try:
            await catalog.discover("glm", "secret")
        except ProviderTimeoutError:
            pass
        return catalog.status("glm")

    assert asyncio.run(timeout_exercise()) == ModelCatalogStatus(
        status="error", error_code="PROVIDER_TIMEOUT"
    )


def test_model_catalog_background_refresh_publishes_loading_then_ready() -> None:
    async def fetch(base_url: str, api_key: str) -> list[str]:
        del base_url, api_key
        await asyncio.sleep(0)
        return ["background-model"]

    async def exercise() -> tuple[ModelDiscovery, ModelCatalogStatus]:
        catalog = ProviderModelCatalog(fetcher=fetch)
        task = catalog.schedule_refresh("glm", "secret")
        assert catalog.status("glm").status == "loading"
        result = await task
        return result, catalog.status("glm")

    result, status = asyncio.run(exercise())
    assert result == ModelDiscovery(["background-model"], cached=False)
    assert status == ModelCatalogStatus(
        status="ready", models=("background-model",), cached=False
    )


def test_model_catalog_shutdown_cancels_background_requests() -> None:
    async def fetch(base_url: str, api_key: str) -> list[str]:
        del base_url, api_key
        await asyncio.Event().wait()
        return []

    async def exercise() -> bool:
        catalog = ProviderModelCatalog(fetcher=fetch)
        task = catalog.schedule_refresh("deepseek", "secret")
        await asyncio.sleep(0)
        await catalog.aclose()
        return task.done()

    assert asyncio.run(exercise()) is True


def test_new_credential_owns_status_and_clearing_it_cancels_refresh() -> None:
    releases = {"old": asyncio.Event(), "new": asyncio.Event()}

    async def fetch(base_url: str, api_key: str) -> list[str]:
        del base_url
        await releases[api_key].wait()
        return [f"{api_key}-model"]

    async def exercise() -> None:
        catalog = ProviderModelCatalog(fetcher=fetch, timeout_seconds=1)
        old = catalog.schedule_refresh("deepseek", "old")
        await asyncio.sleep(0)
        new = catalog.schedule_refresh("deepseek", "new")
        releases["new"].set()
        await new
        releases["old"].set()
        await old
        assert catalog.status("deepseek").models == ("new-model",)

        pending = catalog.schedule_refresh("deepseek", "old")
        await asyncio.sleep(0)
        catalog.invalidate("deepseek")
        await asyncio.gather(pending, return_exceptions=True)
        assert catalog.status("deepseek") == ModelCatalogStatus()

    asyncio.run(exercise())


def test_credential_save_schedules_but_does_not_await_remote_discovery(
    tmp_path: Path,
) -> None:
    class Catalog:
        def __init__(self) -> None:
            self.scheduled: list[tuple[str, str]] = []
            self.discovery_calls = 0

        def schedule_refresh(self, provider_id: str, api_key: str) -> None:
            self.scheduled.append((provider_id, api_key))

        async def discover(self, provider_id: str, api_key: str) -> ModelDiscovery:
            del provider_id, api_key
            self.discovery_calls += 1
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    catalog = Catalog()
    client = TestClient(
        create_app(
            provider_store=ProviderStore(tmp_path / "providers.json"),
            model_catalog=catalog,
        )
    )

    response = client.put(
        "/api/providers/deepseek",
        json={"api_key": "secret", "selected_model": "deepseek-v4-flash"},
    )

    assert response.status_code == 200
    assert response.json()["provider"]["configured"] is True
    assert catalog.scheduled == [("deepseek", "secret")]
    assert catalog.discovery_calls == 0


def test_workspace_routes_use_the_blocking_io_offload_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    calls: list[str] = []

    async def recorded_to_thread(function, *args):
        calls.append(function.__name__)
        return function(*args)

    monkeypatch.setattr("agent.host.app.asyncio.to_thread", recorded_to_thread)
    client = TestClient(
        create_app(
            provider_store=ProviderStore(tmp_path / "providers.json"),
            model_catalog=ProviderModelCatalog(fetcher=lambda *_: asyncio.sleep(0, result=[])),
            workspace_browser=WorkspaceBrowser((root,)),
        )
    )

    assert client.get("/api/workspaces").status_code == 200
    assert client.post(
        "/api/workspaces/select", json={"path": str(root)}
    ).status_code == 201
    assert calls == ["list", "select"]


def test_slow_workspace_listing_does_not_block_other_host_requests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    browser = WorkspaceBrowser((root,))
    real_list = browser.list
    started = threading.Event()
    release = threading.Event()

    def slow_list(path: str | None = None):
        started.set()
        assert release.wait(timeout=1)
        return real_list(path)

    monkeypatch.setattr(browser, "list", slow_list)
    app = create_app(
        provider_store=ProviderStore(tmp_path / "providers.json"),
        model_catalog=ProviderModelCatalog(fetcher=lambda *_: asyncio.sleep(0, result=[])),
        workspace_browser=browser,
    )

    async def exercise() -> tuple[int, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listing = asyncio.create_task(client.get("/api/workspaces"))
            assert await asyncio.to_thread(started.wait, 1)
            providers = await asyncio.wait_for(client.get("/api/providers"), timeout=0.1)
            release.set()
            return providers.status_code, (await listing).status_code

    assert asyncio.run(exercise()) == (200, 200)


def test_workspace_listing_does_not_resolve_each_ordinary_child(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for index in range(2_000):
        (root / f"directory-{index:04}").mkdir()

    resolve_calls = 0
    real_resolve = Path.resolve

    def counted_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        nonlocal resolve_calls
        resolve_calls += 1
        return real_resolve(path, *args, **kwargs)

    browser = WorkspaceBrowser((root,))
    monkeypatch.setattr("agent.host.workspace.Path.resolve", counted_resolve)
    listing = browser.list()

    assert len(listing.entries) == 500
    # One strict resolve validates the requested directory; ordinary entries
    # use DirEntry metadata and are not canonicalized one by one.
    assert resolve_calls == 1

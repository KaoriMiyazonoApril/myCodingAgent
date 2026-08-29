from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from agent.cli import build_parser
from agent.host.app import create_app
from agent.host.model_catalog import ModelDiscovery
from agent.host.provider_config import ProviderStore
from agent.host.server import ProductionAssetsError, production_static_dir
from agent.host import server


class _Catalog:
    async def discover(self, provider_id: str, api_key: str) -> ModelDiscovery:
        return ModelDiscovery([], cached=False)


def test_production_static_assets_and_spa_fallback_do_not_capture_api(
    tmp_path: Path,
) -> None:
    static = tmp_path / "dist"
    assets = static / "assets"
    assets.mkdir(parents=True)
    (static / "index.html").write_text("<main>Agent Web</main>", encoding="utf-8")
    (assets / "app.js").write_text("window.agent = true", encoding="utf-8")
    app = create_app(
        provider_store=ProviderStore(tmp_path / "providers.json"),
        model_catalog=_Catalog(),
        static_dir=static,
    )

    with TestClient(app) as client:
        root = client.get("/")
        asset = client.get("/assets/app.js")
        fallback = client.get("/threads/thread-1")
        missing_api = client.get("/api/not-real")

    assert root.status_code == 200
    assert "Agent Web" in root.text
    assert asset.text == "window.agent = true"
    assert "Agent Web" in fallback.text
    assert missing_api.status_code == 404
    assert missing_api.json()["error"]["code"] == "NOT_FOUND"


def test_production_assets_fail_fast_with_build_instructions(tmp_path: Path) -> None:
    with pytest.raises(ProductionAssetsError, match="npm run build") as caught:
        production_static_dir(tmp_path / "missing")

    assert "npm install" in str(caught.value)
    assert str(tmp_path / "missing" / "index.html") in str(caught.value)


def test_web_cli_is_loopback_only_and_accepts_repeatable_roots() -> None:
    parser = build_parser()

    defaults = parser.parse_args(["web"])
    configured = parser.parse_args(
        [
            "web",
            "--port",
            "4090",
            "--workspace-root",
            "/workspace/one",
            "--workspace-root",
            "/workspace/two",
            "--dev",
        ]
    )

    assert defaults.port == 3080
    assert defaults.workspace_roots == []
    assert not hasattr(defaults, "host")
    assert configured.port == 4090
    assert configured.workspace_roots == ["/workspace/one", "/workspace/two"]
    assert configured.dev is True


def test_web_server_always_passes_loopback_to_uvicorn(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    fake_app = SimpleNamespace(
        state=SimpleNamespace(
            workspace_browser=SimpleNamespace(roots=("/workspace",)),
        )
    )
    monkeypatch.setattr(server, "build_web_app", lambda **kwargs: fake_app)

    class Config:
        def __init__(self, app, **kwargs) -> None:
            calls.append({"app": app, **kwargs})

    class Server:
        lifespan = SimpleNamespace(shutdown_failed=False)

        def __init__(self, config) -> None:
            self.config = config

        def run(self) -> None:
            pass

    monkeypatch.setattr(server.uvicorn, "Config", Config)
    monkeypatch.setattr(server.uvicorn, "Server", Server)

    result = server.run_web(port=4090, workspace_roots=["/workspace"])

    assert result == 0
    assert calls == [{"app": fake_app, "host": "127.0.0.1", "port": 4090}]


def test_web_server_returns_nonzero_after_failed_lifespan_shutdown(
    monkeypatch,
) -> None:
    fake_app = SimpleNamespace(
        state=SimpleNamespace(
            workspace_browser=SimpleNamespace(roots=("/workspace",)),
        )
    )
    monkeypatch.setattr(server, "build_web_app", lambda **kwargs: fake_app)

    class Config:
        def __init__(self, app, **kwargs) -> None:
            self.app = app

    class Server:
        lifespan = SimpleNamespace(shutdown_failed=True)

        def __init__(self, config) -> None:
            self.config = config

        def run(self) -> None:
            pass

    monkeypatch.setattr(server.uvicorn, "Config", Config)
    monkeypatch.setattr(server.uvicorn, "Server", Server)

    assert server.run_web(workspace_roots=["/workspace"]) == 1


def test_lifespan_shuts_down_tasks_before_thread_resources(tmp_path: Path) -> None:
    calls: list[str] = []
    app = create_app(
        provider_store=ProviderStore(tmp_path / "providers.json"),
        model_catalog=_Catalog(),
    )

    async def stop_tasks() -> None:
        calls.append("tasks")

    async def close_threads() -> None:
        calls.append("threads")

    app.state.turn_tasks.shutdown = stop_tasks
    app.state.thread_host.shutdown = close_threads

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200

    assert calls == ["tasks", "threads"]


def test_shutdown_timeout_is_reported_as_failure(tmp_path: Path) -> None:
    app = create_app(
        provider_store=ProviderStore(tmp_path / "providers.json"),
        model_catalog=_Catalog(),
        shutdown_timeout_seconds=0.01,
    )

    async def stuck_shutdown() -> None:
        await asyncio.Event().wait()

    app.state.turn_tasks.shutdown = stuck_shutdown

    with pytest.raises(RuntimeError, match="shutdown timed out"):
        with TestClient(app) as client:
            assert client.get("/api/health").status_code == 200

"""Production composition and uvicorn entrypoint for the local Agent Host."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import uvicorn

from .app import create_app
from .model_catalog import ProviderModelCatalog
from .provider_config import ProviderStore
from .workspace import WorkspaceBrowser


DEFAULT_PORT = 3080
LOOPBACK_HOST = "127.0.0.1"


class ProductionAssetsError(RuntimeError):
    """The Vite production build is absent or incomplete."""


def production_static_dir(candidate: Path | None = None) -> Path:
    """Resolve and validate the Vite output used by the one-process Host."""

    static_dir = candidate or Path(__file__).resolve().parents[2] / "web" / "dist"
    index_path = static_dir / "index.html"
    assets_path = static_dir / "assets"
    if not index_path.is_file() or not assets_path.is_dir():
        raise ProductionAssetsError(
            f"Agent Web UI assets are missing at {index_path}.\n"
            "Build them before starting production mode:\n"
            "  cd web\n"
            "  npm install\n"
            "  npm run build"
        )
    return static_dir


def build_web_app(
    *,
    workspace_roots: Sequence[str] = (),
    dev: bool = False,
):
    """Compose a development or production app without starting a socket."""

    roots = list(workspace_roots) or [str(Path.cwd())]
    static_dir = None if dev else production_static_dir()
    return create_app(
        provider_store=ProviderStore(),
        model_catalog=ProviderModelCatalog(),
        workspace_browser=WorkspaceBrowser(roots),
        dev_mode=dev,
        static_dir=static_dir,
    )


def run_web(
    *,
    port: int = DEFAULT_PORT,
    workspace_roots: Sequence[str] = (),
    dev: bool = False,
) -> int:
    """Start the loopback-only Agent Host and block until shutdown."""

    app = build_web_app(workspace_roots=workspace_roots, dev=dev)
    roots = app.state.workspace_browser.roots
    print(f"Agent Web UI:\nhttp://{LOOPBACK_HOST}:{port}")
    print("Workspace roots:")
    for root in roots:
        print(f"  {root}")
    if dev:
        print("Development mode: open the Vite URL at http://127.0.0.1:5173")
    config = uvicorn.Config(app, host=LOOPBACK_HOST, port=port)
    server = uvicorn.Server(config)
    server.run()
    lifespan = getattr(server, "lifespan", None)
    return 1 if getattr(lifespan, "shutdown_failed", False) else 0

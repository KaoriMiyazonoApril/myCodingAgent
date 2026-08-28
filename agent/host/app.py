"""FastAPI application factory for the local Agent Host."""

from __future__ import annotations

from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agent.runtime import (
    AgentLimits,
    ModelSettings,
    SettingsConflictError,
    ThinkingKeep,
    ThinkingSettings,
    ThreadClosedError,
    UnsafeWorkspaceError,
    WorkspaceValidationLimitError,
)

from .model_catalog import (
    ModelDiscovery,
    ModelDiscoveryError,
    ProviderAuthenticationError,
    ProviderResponseError,
)
from .provider_config import (
    ProviderConfigurationError,
    ProviderNotConfiguredError,
    ProviderStore,
    SCHEMA_VERSION,
    UnknownProviderError,
)
from .thread_service import (
    ConfigurationRequiredError,
    RuntimeFactory,
    ThreadHost,
    ThreadNotFoundError,
    production_runtime_factory,
)
from .workspace import (
    WorkspaceBrowser,
    WorkspaceBrowseError,
    WorkspaceNotAccessibleError,
    WorkspaceNotFoundError,
)


class ModelCatalog(Protocol):
    async def discover(self, provider_id: str, api_key: str) -> ModelDiscovery:
        """Return Provider-reported model IDs for one stored credential."""


class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str
    selected_model: str | None = None


class ProviderDefaultUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model: str


class ThreadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str
    provider_config_id: str | None = None
    model: str | None = None


class ThinkingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    budget_tokens: int | None = None
    keep: ThinkingKeep | None = None


class LimitsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_iterations: int = 20
    max_tool_calls: int = 50
    max_execution_seconds: float = 15 * 60


class ThreadSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    provider_config_id: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    thinking: ThinkingUpdate | None = None
    limits: LimitsUpdate = Field(default_factory=LimitsUpdate)


def create_app(
    *,
    provider_store: ProviderStore,
    model_catalog: ModelCatalog,
    workspace_browser: WorkspaceBrowser | None = None,
    runtime_factory: RuntimeFactory | None = None,
    dev_mode: bool = False,
) -> FastAPI:
    """Compose the local Host at its highest HTTP test seam."""

    app = FastAPI(title="Local Agent Host")
    if dev_mode:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://127.0.0.1:5173"],
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "Last-Event-ID"],
        )
    browser = workspace_browser or WorkspaceBrowser()
    threads = ThreadHost(
        provider_store=provider_store,
        workspace_browser=browser,
        runtime_factory=runtime_factory or production_runtime_factory(provider_store),
    )
    app.state.provider_store = provider_store
    app.state.model_catalog = model_catalog
    app.state.workspace_browser = browser
    app.state.thread_host = threads

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        return _error_response(400, "INVALID_ARGUMENT", "Request is invalid")

    @app.exception_handler(UnknownProviderError)
    async def unknown_provider(
        request: Request,
        error: UnknownProviderError,
    ) -> JSONResponse:
        return _error_response(404, error.code, "Provider does not exist")

    @app.exception_handler(ProviderNotConfiguredError)
    async def provider_not_configured(
        request: Request,
        error: ProviderNotConfiguredError,
    ) -> JSONResponse:
        return _error_response(409, error.code, "Provider is not configured")

    @app.exception_handler(ProviderConfigurationError)
    async def invalid_provider_configuration(
        request: Request,
        error: ProviderConfigurationError,
    ) -> JSONResponse:
        return _error_response(400, "INVALID_ARGUMENT", "Provider settings are invalid")

    @app.exception_handler(ProviderAuthenticationError)
    async def provider_authentication_failed(
        request: Request,
        error: ProviderAuthenticationError,
    ) -> JSONResponse:
        return _error_response(
            400,
            error.code,
            "Provider rejected the configured credential",
        )

    @app.exception_handler(ProviderResponseError)
    async def invalid_provider_response(
        request: Request,
        error: ProviderResponseError,
    ) -> JSONResponse:
        return _error_response(502, error.code, "Provider returned an invalid response")

    @app.exception_handler(ModelDiscoveryError)
    async def provider_unavailable(
        request: Request,
        error: ModelDiscoveryError,
    ) -> JSONResponse:
        return _error_response(502, error.code, "Provider is unavailable")

    @app.exception_handler(WorkspaceNotFoundError)
    async def workspace_not_found(
        request: Request,
        error: WorkspaceNotFoundError,
    ) -> JSONResponse:
        return _error_response(404, error.code, "Workspace path does not exist")

    @app.exception_handler(WorkspaceNotAccessibleError)
    async def workspace_not_accessible(
        request: Request,
        error: WorkspaceNotAccessibleError,
    ) -> JSONResponse:
        return _error_response(403, error.code, "Workspace path is not accessible")

    @app.exception_handler(WorkspaceBrowseError)
    async def invalid_workspace(
        request: Request,
        error: WorkspaceBrowseError,
    ) -> JSONResponse:
        return _error_response(400, error.code, "Workspace path is not allowed")

    @app.exception_handler(ConfigurationRequiredError)
    async def configuration_required(
        request: Request,
        error: ConfigurationRequiredError,
    ) -> JSONResponse:
        return _error_response(409, error.code, "Provider and model configuration required")

    @app.exception_handler(ThreadNotFoundError)
    async def thread_not_found(
        request: Request,
        error: ThreadNotFoundError,
    ) -> JSONResponse:
        return _error_response(404, error.code, "Thread does not exist")

    @app.exception_handler(SettingsConflictError)
    async def settings_conflict(
        request: Request,
        error: SettingsConflictError,
    ) -> JSONResponse:
        return _error_response(409, error.code, "Thread settings changed")

    @app.exception_handler(ThreadClosedError)
    async def thread_closed(
        request: Request,
        error: ThreadClosedError,
    ) -> JSONResponse:
        return _error_response(409, error.code, "Thread is closed")

    @app.exception_handler(UnsafeWorkspaceError)
    async def unsafe_workspace(
        request: Request,
        error: UnsafeWorkspaceError,
    ) -> JSONResponse:
        return _error_response(400, error.code, "Runtime rejected the workspace")

    @app.exception_handler(WorkspaceValidationLimitError)
    async def workspace_validation_limit(
        request: Request,
        error: WorkspaceValidationLimitError,
    ) -> JSONResponse:
        return _error_response(400, error.code, "Workspace validation limit reached")

    @app.exception_handler(ValueError)
    async def invalid_argument(
        request: Request,
        error: ValueError,
    ) -> JSONResponse:
        return _error_response(400, "INVALID_ARGUMENT", "Request is invalid")

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ok",
            "configuration_required": _configuration_required(provider_store),
        }

    @app.get("/api/providers")
    async def providers() -> dict[str, object]:
        default = provider_store.default_selection()
        return {
            "schema_version": SCHEMA_VERSION,
            "default_provider_id": (
                None if default is None else default["provider_id"]
            ),
            "providers": provider_store.list_public(),
        }

    @app.get("/api/workspaces")
    async def workspaces(path: str | None = None) -> dict[str, object]:
        listing = browser.list(path)
        return {
            "schema_version": SCHEMA_VERSION,
            "path": listing.path,
            "parent": listing.parent,
            "roots": list(listing.roots),
            "entries": [
                {
                    "name": entry.name,
                    "path": entry.path,
                    "type": entry.type,
                }
                for entry in listing.entries
            ],
            "truncated": listing.truncated,
        }

    @app.get("/api/threads")
    async def list_threads() -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "threads": threads.list_threads(),
        }

    @app.post("/api/threads", status_code=201)
    async def create_thread(request: ThreadCreate) -> dict[str, object]:
        thread = threads.create_thread(
            request.workspace,
            provider_config_id=request.provider_config_id,
            model=request.model,
        )
        return {"schema_version": SCHEMA_VERSION, "thread": thread}

    @app.get("/api/threads/{thread_id}")
    async def get_thread(thread_id: str) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "thread": threads.get_thread(thread_id),
        }

    @app.patch("/api/threads/{thread_id}/settings")
    async def update_thread_settings(
        thread_id: str,
        request: ThreadSettingsUpdate,
    ) -> dict[str, object]:
        thinking = (
            None
            if request.thinking is None
            else ThinkingSettings(**request.thinking.model_dump())
        )
        settings = ModelSettings(
            provider_config_id=request.provider_config_id,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            thinking=thinking,
            limits=AgentLimits(**request.limits.model_dump()),
        )
        thread = threads.update_settings(
            thread_id,
            expected_version=request.expected_version,
            settings=settings,
        )
        return {"schema_version": SCHEMA_VERSION, "thread": thread}

    @app.post("/api/threads/{thread_id}/close")
    async def close_thread(thread_id: str) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "thread": threads.close_thread(thread_id),
        }

    @app.put("/api/providers/{provider_id}")
    async def save_provider(
        provider_id: str,
        request: ProviderUpdate,
    ) -> dict[str, object]:
        provider = provider_store.save_provider(
            provider_id,
            api_key=request.api_key,
            selected_model=request.selected_model,
        )
        return {"schema_version": SCHEMA_VERSION, "provider": provider}

    @app.delete("/api/providers/{provider_id}/credential")
    async def clear_provider(provider_id: str) -> dict[str, object]:
        provider = provider_store.clear_credential(provider_id)
        return {"schema_version": SCHEMA_VERSION, "provider": provider}

    @app.post("/api/providers/{provider_id}/models/discover")
    async def discover_models(provider_id: str) -> dict[str, object]:
        credential = provider_store.get_credential(provider_id)
        discovered = await model_catalog.discover(provider_id, credential)
        return {
            "schema_version": SCHEMA_VERSION,
            "provider_id": provider_id,
            "models": discovered.models,
            "cached": discovered.cached,
        }

    @app.patch("/api/provider-default")
    async def select_default(
        request: ProviderDefaultUpdate,
    ) -> dict[str, object]:
        provider = provider_store.set_default(
            request.provider_id,
            model=request.model,
        )
        return {"schema_version": SCHEMA_VERSION, "provider": provider}

    return app


def _configuration_required(store: ProviderStore) -> bool:
    default = store.default_selection()
    if default is None:
        return True
    try:
        store.get_credential(default["provider_id"])
    except ProviderConfigurationError:
        return True
    return False


def _error_response(
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": {},
            }
        },
    )

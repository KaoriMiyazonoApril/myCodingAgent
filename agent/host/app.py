"""FastAPI application factory for the local Agent Host."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import inspect
import logging
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from agent.model.presets import PROVIDER_PRESETS
from agent.runtime import (
    AgentLimits,
    ApprovalMode,
    ModelSettings,
    SettingsConflictError,
    ThinkingSettings,
    ThreadClosedError,
    ThreadStore,
    TurnSettingsOverride,
    UnsafeWorkspaceError,
    WorkspaceUnavailableError,
    WorkspaceValidationLimitError,
)

from .model_catalog import (
    ModelDiscovery,
    ModelCatalogStatus,
    ModelDiscoveryError,
    ProviderAuthenticationError,
    ProviderResponseError,
)
from .event_stream import EventStreamAdapter, select_event_cursor
from .provider_config import (
    ProviderConfigurationError,
    ProviderNotConfiguredError,
    ProviderStore,
    PROVIDERS,
    SCHEMA_VERSION,
    UnknownProviderError,
)
from .thread_service import (
    ConfigurationRequiredError,
    ApprovalNotFoundError,
    RuntimeFactory,
    ThreadHost,
    ThreadNotFoundError,
    production_runtime_factory,
)
from .turn_tasks import (
    DuplicateTurnSubmissionError,
    NoActiveTurnError,
    TurnTaskManager,
)
from .workspace import (
    WorkspaceBrowser,
    WorkspaceBrowseError,
    WorkspaceInvalidPathError,
    WorkspaceNotAccessibleError,
    WorkspaceNotFoundError,
)


logger = logging.getLogger(__name__)

_SAFE_CATALOG_ERROR_CODES = frozenset(
    {
        "PROVIDER_TIMEOUT",
        "PROVIDER_AUTHENTICATION_FAILED",
        "INVALID_PROVIDER_RESPONSE",
        "PROVIDER_UNAVAILABLE",
    }
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

    workspace_id: str
    provider_config_id: str | None = None
    model: str | None = None
    approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST


class WorkspaceSelect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class ThinkingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    intensity: str | None = None


class LimitsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_iterations: int = 60
    max_tool_calls: int = 200
    max_execution_seconds: float = 60 * 60


class ThreadSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    provider_config_id: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    thinking: ThinkingUpdate | None = None
    limits: LimitsUpdate = Field(default_factory=LimitsUpdate)
    approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST


class TurnSettingsOverrideUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_config_id: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    thinking: ThinkingUpdate | None = None
    limits: LimitsUpdate | None = None
    approval_mode: ApprovalMode | None = None


class TurnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    idempotency_key: str | None = None
    settings_override: TurnSettingsOverrideUpdate | None = None


class ApprovalResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool


class ApprovalResolutionWithId(ApprovalResolution):
    approval_id: str


def create_app(
    *,
    provider_store: ProviderStore,
    model_catalog: ModelCatalog,
    workspace_browser: WorkspaceBrowser | None = None,
    runtime_factory: RuntimeFactory | None = None,
    event_stream_adapter: EventStreamAdapter | None = None,
    dev_mode: bool = False,
    static_dir: Path | None = None,
    state_dir: Path | None = None,
    database_path: Path | None = None,
    thread_store: ThreadStore | None = None,
    shutdown_timeout_seconds: float = 10.0,
) -> FastAPI:
    """Compose the local Host at its highest HTTP test seam."""

    browser = workspace_browser or WorkspaceBrowser()
    runtime_builder = runtime_factory or production_runtime_factory(
        provider_store,
        state_dir=state_dir,
        database_path=database_path,
        thread_store=thread_store,
    )
    threads = ThreadHost(
        provider_store=provider_store,
        workspace_browser=browser,
        runtime_factory=runtime_builder,
    )
    turn_tasks = TurnTaskManager(threads)
    event_stream = event_stream_adapter or EventStreamAdapter(threads, turn_tasks)
    async def shutdown_resources() -> None:
        try:
            await turn_tasks.shutdown()
        finally:
            try:
                await threads.shutdown()
            finally:
                close_catalog = getattr(model_catalog, "aclose", None)
                if callable(close_catalog):
                    result = close_catalog()
                    if inspect.isawaitable(result):
                        await result

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        try:
            await asyncio.wait_for(
                shutdown_resources(),
                timeout=shutdown_timeout_seconds,
            )
        except TimeoutError as error:
            logger.critical(
                "Agent Host shutdown exceeded %.1f seconds",
                shutdown_timeout_seconds,
            )
            raise RuntimeError("Agent Host shutdown timed out") from error

    app = FastAPI(title="Local Agent Host", lifespan=lifespan)
    if dev_mode:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://127.0.0.1:5173"],
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "Last-Event-ID"],
        )
    app.state.provider_store = provider_store
    app.state.model_catalog = model_catalog
    app.state.workspace_browser = browser
    app.state.thread_host = threads
    app.state.turn_tasks = turn_tasks
    app.state.event_stream = event_stream
    app.state.shutdown_resources = shutdown_resources

    def with_submission(view: dict[str, object]) -> dict[str, object]:
        snapshot = view["snapshot"]
        assert isinstance(snapshot, dict)
        thread_id = snapshot["thread_id"]
        assert isinstance(thread_id, str)
        return {
            **view,
            "submission": turn_tasks.inspect(thread_id),
            "host_error": turn_tasks.inspect_failure(thread_id),
        }

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

    @app.exception_handler(WorkspaceInvalidPathError)
    async def workspace_invalid_path(
        request: Request,
        error: WorkspaceInvalidPathError,
    ) -> JSONResponse:
        return _error_response(400, error.code, "Workspace path is invalid")

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

    @app.exception_handler(ApprovalNotFoundError)
    async def approval_not_found(
        request: Request,
        error: ApprovalNotFoundError,
    ) -> JSONResponse:
        return _error_response(409, error.code, "Approval request is no longer active")

    @app.exception_handler(UnsafeWorkspaceError)
    async def unsafe_workspace(
        request: Request,
        error: UnsafeWorkspaceError,
    ) -> JSONResponse:
        return _error_response(400, error.code, "Runtime rejected the workspace")

    @app.exception_handler(WorkspaceUnavailableError)
    async def workspace_unavailable(
        request: Request,
        error: WorkspaceUnavailableError,
    ) -> JSONResponse:
        return _error_response(409, error.code, "Workspace is unavailable")

    @app.exception_handler(WorkspaceValidationLimitError)
    async def workspace_validation_limit(
        request: Request,
        error: WorkspaceValidationLimitError,
    ) -> JSONResponse:
        return _error_response(400, error.code, "Workspace validation limit reached")

    @app.exception_handler(DuplicateTurnSubmissionError)
    async def duplicate_turn(
        request: Request,
        error: DuplicateTurnSubmissionError,
    ) -> JSONResponse:
        return _error_response(409, error.code, "Thread already has a Turn")

    @app.exception_handler(NoActiveTurnError)
    async def no_active_turn(
        request: Request,
        error: NoActiveTurnError,
    ) -> JSONResponse:
        return _error_response(409, error.code, "Thread has no active Turn")

    @app.exception_handler(ValueError)
    async def invalid_argument(
        request: Request,
        error: ValueError,
    ) -> JSONResponse:
        return _error_response(400, "INVALID_ARGUMENT", "Request is invalid")

    @app.exception_handler(Exception)
    async def internal_error(
        request: Request,
        error: Exception,
    ) -> JSONResponse:
        logger.error(
            "Unhandled Agent Host request failure; error_type=%s",
            type(error).__name__,
        )
        return _error_response(500, "INTERNAL_ERROR", "Agent Host request failed")

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
        public_providers = provider_store.list_public()
        public_providers = [
            _provider_with_catalog_status(provider, model_catalog)
            for provider in public_providers
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "default_provider_id": (
                None if default is None else default["provider_id"]
            ),
            "providers": public_providers,
        }

    @app.get("/api/workspaces")
    async def workspaces(path: str | None = None) -> dict[str, object]:
        # Directory traversal is Host I/O.  Keep it out of the event loop so a
        # large mounted WSL directory cannot delay SSE or model requests.
        listing = await asyncio.to_thread(browser.list, path)
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

    @app.post("/api/workspaces/select", status_code=201)
    async def select_workspace(request: WorkspaceSelect) -> dict[str, object]:
        workspace = await asyncio.to_thread(browser.select, request.path)
        return {
            "schema_version": SCHEMA_VERSION,
            "workspace": workspace.to_dict(),
        }

    @app.get("/api/threads")
    async def list_threads() -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "threads": [with_submission(view) for view in threads.list_threads()],
        }

    @app.post("/api/threads", status_code=201)
    async def create_thread(request: ThreadCreate) -> dict[str, object]:
        thread = threads.create_thread(
            request.workspace_id,
            provider_config_id=request.provider_config_id,
            model=request.model,
            approval_mode=request.approval_mode,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "thread": with_submission(thread),
        }

    @app.get("/api/threads/{thread_id}")
    async def get_thread(thread_id: str) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "thread": with_submission(threads.get_thread(thread_id)),
        }

    @app.get("/api/threads/{thread_id}/capabilities")
    async def thread_capabilities(
        thread_id: str,
        provider_config_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]:
        # Query parameters describe a draft candidate, not persisted Thread
        # settings.  The Host delegates this preview to Runtime so the next
        # request uses the same effective capability decision.
        capabilities = threads.capabilities_for(
            thread_id,
            provider_config_id=provider_config_id,
            model=model,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "thread_id": thread_id,
            "capabilities": capabilities,
        }

    @app.get("/api/threads/{thread_id}/events")
    async def stream_thread_events(
        thread_id: str,
        request: Request,
        after_event_id: str | None = None,
    ) -> StreamingResponse:
        threads.get_thread(thread_id)
        cursor = select_event_cursor(
            after_event_id,
            request.headers.get("last-event-id"),
        )
        return StreamingResponse(
            event_stream.stream(
                thread_id,
                after_event_id=cursor,
                disconnected=request.is_disconnected,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.patch("/api/threads/{thread_id}/settings")
    async def update_thread_settings(
        thread_id: str,
        request: ThreadSettingsUpdate,
    ) -> dict[str, object]:
        current_view = threads.get_thread(thread_id)
        current_snapshot = current_view.get("snapshot", {})
        current_settings = (
            current_snapshot.get("settings", {})
            if isinstance(current_snapshot, dict)
            else {}
        )
        current_limits_payload = (
            current_settings.get("limits", {})
            if isinstance(current_settings, dict)
            else {}
        )
        # ``limits`` are an internal runtime budget, not a Basic Settings
        # control.  If a legacy/client request omits them, preserve the
        # current versioned values rather than resetting them to defaults.
        fields_set = getattr(request, "model_fields_set", set())
        limits_payload = (
            request.limits.model_dump()
            if "limits" in fields_set
            else current_limits_payload
        )
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
            limits=AgentLimits(**limits_payload),
            approval_mode=request.approval_mode,
        )
        thread = threads.update_settings(
            thread_id,
            expected_version=request.expected_version,
            settings=settings,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "thread": with_submission(thread),
        }

    @app.post("/api/threads/{thread_id}/turns", status_code=202)
    async def start_turn(
        thread_id: str,
        request: TurnCreate,
    ) -> dict[str, object]:
        settings_override = _turn_settings_override(request.settings_override)
        submission = await turn_tasks.start(
            thread_id,
            request.message,
            idempotency_key=request.idempotency_key,
            settings_override=settings_override,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "thread_id": thread_id,
            "submission": submission,
        }

    @app.post("/api/threads/{thread_id}/cancel", status_code=202)
    async def cancel_turn(thread_id: str) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "thread_id": thread_id,
            "submission": turn_tasks.cancel(thread_id),
        }

    @app.post("/api/threads/{thread_id}/close")
    async def close_thread(thread_id: str) -> dict[str, object]:
        if turn_tasks.inspect(thread_id) is not None:
            turn_tasks.cancel(thread_id)
        return {
            "schema_version": SCHEMA_VERSION,
            "thread": with_submission(threads.close_thread(thread_id)),
        }

    @app.post("/api/threads/{thread_id}/approvals/{approval_id}")
    async def resolve_approval(
        thread_id: str,
        approval_id: str,
        request: ApprovalResolution,
    ) -> dict[str, object]:
        threads.resolve_approval(
            thread_id,
            approval_id=approval_id,
            approved=request.approved,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "thread_id": thread_id,
            "approval_id": approval_id,
            "approved": request.approved,
        }

    @app.post("/api/threads/{thread_id}/approvals")
    async def resolve_approval_from_body(
        thread_id: str,
        request: ApprovalResolutionWithId,
    ) -> dict[str, object]:
        threads.resolve_approval(
            thread_id,
            approval_id=request.approval_id,
            approved=request.approved,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "thread_id": thread_id,
            "approval_id": request.approval_id,
            "approved": request.approved,
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
        # Credential persistence is the synchronous contract.  Catalog
        # discovery is deliberately best-effort and runs in the background so
        # saving a key never waits on an upstream provider.
        schedule_refresh = getattr(model_catalog, "schedule_refresh", None)
        if callable(schedule_refresh):
            try:
                credential = provider_store.get_credential(provider_id)
                schedule_refresh(provider_id, credential)
            except Exception:
                # The provider remains configured; the status endpoint will
                # report a bounded error if the catalog adapter recorded one.
                logger.debug("Could not schedule model catalog refresh", exc_info=True)
        provider = _provider_with_catalog_status(provider, model_catalog)
        return {"schema_version": SCHEMA_VERSION, "provider": provider}

    @app.delete("/api/providers/{provider_id}/credential")
    async def clear_provider(provider_id: str) -> dict[str, object]:
        provider = provider_store.clear_credential(provider_id)
        invalidate = getattr(model_catalog, "invalidate", None)
        if callable(invalidate):
            invalidate(provider_id)
        provider = _provider_with_catalog_status(provider, model_catalog)
        return {"schema_version": SCHEMA_VERSION, "provider": provider}

    @app.post("/api/providers/{provider_id}/models/discover")
    async def discover_models(provider_id: str) -> dict[str, object]:
        credential = provider_store.get_credential(provider_id)
        discovered = await model_catalog.discover(provider_id, credential)
        response: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "provider_id": provider_id,
            "models": discovered.models,
            "cached": discovered.cached,
        }
        describe_models = getattr(model_catalog, "model_metadata", None)
        if callable(describe_models):
            response["model_profiles"] = describe_models(provider_id, discovered.models)
        status = _catalog_status(model_catalog, provider_id)
        if status is not None:
            response["status"] = status
        return response

    @app.get("/api/providers/{provider_id}/models")
    async def provider_models(provider_id: str) -> dict[str, object]:
        """Return local profile facts plus the latest shared catalog status."""

        if provider_id not in PROVIDERS:
            raise UnknownProviderError(f"Unknown Provider: {provider_id}")
        known = PROVIDER_PRESETS[provider_id]
        status = _catalog_status(model_catalog, provider_id)
        remote_models = [] if status is None else status.get("models", [])
        model_ids = list(known.model_profiles)
        if isinstance(remote_models, list):
            model_ids.extend(
                model for model in remote_models
                if isinstance(model, str) and model not in model_ids
            )
        response: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "provider_id": provider_id,
            "model_profiles": [known.model_metadata(model) for model in sorted(model_ids)],
        }
        if status is not None:
            response["status"] = status
        return response

    @app.patch("/api/provider-default")
    async def select_default(
        request: ProviderDefaultUpdate,
    ) -> dict[str, object]:
        provider = provider_store.set_default(
            request.provider_id,
            model=request.model,
        )
        return {"schema_version": SCHEMA_VERSION, "provider": provider}

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def unknown_api(path: str) -> JSONResponse:
        return _error_response(404, "NOT_FOUND", "API route does not exist")

    if static_dir is not None:
        index_path = static_dir / "index.html"
        assets_path = static_dir / "assets"
        app.mount(
            "/assets",
            StaticFiles(directory=assets_path, check_dir=True),
            name="assets",
        )

        @app.get("/", include_in_schema=False)
        async def web_index() -> FileResponse:
            return FileResponse(index_path)

        @app.get("/{path:path}", include_in_schema=False)
        async def web_fallback(path: str) -> FileResponse:
            return FileResponse(index_path)

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


def _turn_settings_override(
    value: TurnSettingsOverrideUpdate | None,
) -> TurnSettingsOverride | None:
    if value is None:
        return None
    supplied = value.model_dump(exclude_unset=True)
    if "thinking" in supplied and supplied["thinking"] is not None:
        supplied["thinking"] = ThinkingSettings(**supplied["thinking"])
    if "limits" in supplied and supplied["limits"] is not None:
        supplied["limits"] = AgentLimits(**supplied["limits"])
    return TurnSettingsOverride(**supplied)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _catalog_status(catalog: object, provider_id: str) -> dict[str, object] | None:
    """Return a safe catalog status when the injected catalog supports it."""

    getter = getattr(catalog, "get_status", None)
    if not callable(getter):
        getter = getattr(catalog, "status", None)
    if not callable(getter):
        return None
    try:
        value = getter(provider_id)
    except Exception:
        # Status is auxiliary UI state; a custom catalog must not make the
        # provider setup endpoint fail closed.
        return None
    if isinstance(value, ModelCatalogStatus):
        return value.to_dict()
    if isinstance(value, dict):
        # Only retain the documented safe projection.  In particular, do not
        # pass through exception text or credential fingerprints from a custom
        # implementation.
        raw_status = value.get("status")
        status = (
            raw_status
            if isinstance(raw_status, str)
            and raw_status in {"idle", "loading", "ready", "error"}
            else "idle"
        )
        raw_models = value.get("models", [])
        models = (
            [model.strip() for model in raw_models if isinstance(model, str) and model.strip()]
            if isinstance(raw_models, (list, tuple))
            else []
        )
        raw_error_code = value.get("error_code")
        error_code = None
        if isinstance(raw_error_code, str):
            if raw_error_code in _SAFE_CATALOG_ERROR_CODES:
                error_code = raw_error_code
            elif status == "error":
                error_code = "PROVIDER_UNAVAILABLE"
        return {
            "status": status,
            "models": models,
            "cached": bool(value.get("cached", False)),
            "error_code": error_code,
        }
    return None


def _provider_with_catalog_status(
    provider: dict[str, object],
    catalog: object,
) -> dict[str, object]:
    result = dict(provider)
    preset = PROVIDER_PRESETS.get(str(provider.get("provider_id", "")))
    status = _catalog_status(catalog, str(provider.get("provider_id", "")))
    configured = bool(provider.get("configured", False))
    if not configured:
        result["credential_status"] = "unconfigured"
    elif status is not None and status.get("status") == "ready":
        result["credential_status"] = "verified"
    elif status is not None and status.get("status") == "error":
        result["credential_status"] = "verification_failed"
    else:
        result["credential_status"] = "configured"
    if preset is not None:
        if preset.description:
            result["description"] = preset.description
        model_ids = list(preset.model_profiles)
        if status is not None:
            remote_models = status.get("models", [])
            if isinstance(remote_models, list):
                model_ids.extend(
                    model
                    for model in remote_models
                    if isinstance(model, str) and model not in model_ids
                )
        result["model_profiles"] = [
            preset.model_metadata(model_id) for model_id in sorted(model_ids)
        ]
    if status is None:
        return result
    result["catalog"] = status
    return result


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "status": status_code,
                "code": code,
                "message": message,
                "details": details or {},
            }
        },
    )

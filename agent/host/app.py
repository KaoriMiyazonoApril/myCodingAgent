"""FastAPI application factory for the local Agent Host."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

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
from typing import Protocol


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


def create_app(*, provider_store: ProviderStore, model_catalog: ModelCatalog) -> FastAPI:
    """Compose the local Host at its highest HTTP test seam."""

    app = FastAPI(title="Local Agent Host")
    app.state.provider_store = provider_store
    app.state.model_catalog = model_catalog

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

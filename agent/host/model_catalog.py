"""Provider-reported model discovery behind fixed trusted endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import math
import time

from agent.model.presets import PROVIDER_PRESETS

from .provider_config import PROVIDERS


ModelFetcher = Callable[[str, str], Awaitable[list[str]]]


class ModelDiscoveryError(RuntimeError):
    """A safe model discovery failure suitable for Host error mapping."""

    code = "PROVIDER_UNAVAILABLE"


class ProviderAuthenticationError(ModelDiscoveryError):
    code = "PROVIDER_AUTHENTICATION_FAILED"


class ProviderResponseError(ModelDiscoveryError):
    code = "INVALID_PROVIDER_RESPONSE"


class ProviderTimeoutError(ModelDiscoveryError):
    """The bounded catalog request did not settle before its short deadline."""

    code = "PROVIDER_TIMEOUT"


@dataclass(frozen=True, slots=True)
class ModelDiscovery:
    models: list[str]
    cached: bool


@dataclass(frozen=True, slots=True)
class ModelCatalogStatus:
    """Safe, provider-scoped state for the asynchronous model catalog."""

    status: str = "idle"
    models: tuple[str, ...] = ()
    cached: bool = False
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in {
            "idle",
            "loading",
            "ready",
            "error",
        }:
            raise ValueError("catalog status must be idle, loading, ready, or error")
        if not isinstance(self.cached, bool):
            raise ValueError("catalog status cached must be a boolean")
        if any(not isinstance(model, str) or not model for model in self.models):
            raise ValueError("catalog status models must be non-empty strings")
        if self.error_code is not None and not isinstance(self.error_code, str):
            raise ValueError("catalog status error_code must be a string or None")

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "models": list(self.models),
            "cached": self.cached,
            "error_code": self.error_code,
        }


class ProviderModelCatalog:
    """Discover and briefly cache model IDs without accepting caller URLs.

    A catalog belongs to the Host process rather than a React component.  Its
    cache is keyed by a one-way credential fingerprint, and concurrent misses
    for the same provider/key share one bounded request.  The public status
    projection never includes the credential or upstream exception text.
    """

    def __init__(
        self,
        *,
        fetcher: ModelFetcher | None = None,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = 5 * 60,
        timeout_seconds: float = 3.0,
        timeout: float | None = None,
    ) -> None:
        if timeout is not None:
            # ``timeout`` is a small compatibility alias for callers that use
            # the same spelling as the underlying HTTP client.  Keep one
            # canonical validation path and reject contradictory values.
            if timeout_seconds != 3.0:
                raise ValueError("provide only one catalog timeout")
            timeout_seconds = timeout
        if (
            isinstance(cache_seconds, bool)
            or not isinstance(cache_seconds, (int, float))
            or not math.isfinite(cache_seconds)
            or cache_seconds < 0
        ):
            raise ValueError("cache_seconds must be a non-negative number")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive number")
        self._fetcher = fetcher or self._fetch_models
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._timeout_seconds = float(timeout_seconds)
        self._cache: dict[tuple[str, str], tuple[float, list[str]]] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task[ModelDiscovery]] = {}
        self._status: dict[str, ModelCatalogStatus] = {}
        self._status_keys: dict[str, tuple[str, str]] = {}

    def _set_status(
        self,
        provider_id: str,
        cache_key: tuple[str, str],
        status: ModelCatalogStatus,
    ) -> None:
        """Publish status only for the credential currently being refreshed."""

        current_key = self._status_keys.get(provider_id)
        if current_key is None or current_key == cache_key:
            self._status_keys[provider_id] = cache_key
            self._status[provider_id] = status

    def _task_done(
        self,
        cache_key: tuple[str, str],
        task: asyncio.Task[ModelDiscovery],
    ) -> None:
        if self._inflight.get(cache_key) is task:
            self._inflight.pop(cache_key, None)
        # Consume a background exception. Awaiting callers still receive the
        # original exception from the task; this only prevents an unobserved
        # warning when the task came from ``schedule_refresh``.
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    async def discover(self, provider_id: str, api_key: str) -> ModelDiscovery:
        if provider_id not in PROVIDERS:
            raise ProviderResponseError("Unknown Provider")
        fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        cache_key = (provider_id, fingerprint)
        # A new credential immediately becomes the provider's public status
        # owner. Completion of an older in-flight request is then ignored.
        if self._status_keys.get(provider_id) != cache_key:
            self._status_keys[provider_id] = cache_key
        cached = self._cache.get(cache_key)
        now = self._clock()
        if cached is not None and now - cached[0] <= self._cache_seconds:
            self._set_status(provider_id, cache_key, ModelCatalogStatus(
                status="ready",
                models=tuple(cached[1]),
                cached=True,
            ))
            return ModelDiscovery(list(cached[1]), cached=True)

        existing_task = self._inflight.get(cache_key)
        if existing_task is not None:
            try:
                # A caller waiting behind an existing miss did not perform a
                # network request of its own, so expose the result as cached.
                result = await asyncio.shield(existing_task)
                return ModelDiscovery(list(result.models), cached=True)
            except asyncio.CancelledError:
                raise

        self._set_status(provider_id, cache_key, ModelCatalogStatus(status="loading"))
        task = asyncio.create_task(
            self._discover_uncached(provider_id, api_key, cache_key),
            name=f"model-catalog:{provider_id}",
        )
        self._inflight[cache_key] = task
        task.add_done_callback(lambda done: self._task_done(cache_key, done))
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._task_done(cache_key, task)

    async def _discover_uncached(
        self,
        provider_id: str,
        api_key: str,
        cache_key: tuple[str, str],
    ) -> ModelDiscovery:
        preset = PROVIDER_PRESETS[provider_id]
        try:
            raw_models = await asyncio.wait_for(
                self._fetcher(preset.base_url, api_key),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            self._set_status(provider_id, cache_key, ModelCatalogStatus(
                status="error", error_code=ProviderTimeoutError.code
            ))
            raise ProviderTimeoutError("Provider model discovery timed out") from error
        except ModelDiscoveryError as error:
            self._set_status(provider_id, cache_key, ModelCatalogStatus(
                status="error", error_code=error.code
            ))
            raise
        except Exception as error:
            name = type(error).__name__.lower()
            status_code = getattr(error, "status_code", None)
            if status_code in {401, 403} or "authentication" in name:
                mapped: ModelDiscoveryError = ProviderAuthenticationError(
                    "Provider rejected the configured credential"
                )
            else:
                mapped = ModelDiscoveryError("Provider model discovery failed")
            self._set_status(provider_id, cache_key, ModelCatalogStatus(
                status="error", error_code=mapped.code
            ))
            raise mapped from error

        if not isinstance(raw_models, list):
            error = ProviderResponseError("Provider returned an invalid model list")
            self._set_status(provider_id, cache_key, ModelCatalogStatus(
                status="error", error_code=error.code
            ))
            raise error
        if any(not isinstance(model, str) for model in raw_models):
            error = ProviderResponseError("Provider returned an invalid model record")
            self._set_status(provider_id, cache_key, ModelCatalogStatus(
                status="error", error_code=error.code
            ))
            raise error
        models = sorted({model.strip() for model in raw_models if model.strip()})
        self._cache[cache_key] = (self._clock(), models)
        self._set_status(provider_id, cache_key, ModelCatalogStatus(
            status="ready", models=tuple(models), cached=False
        ))
        return ModelDiscovery(list(models), cached=False)

    def status(self, provider_id: str) -> ModelCatalogStatus:
        """Return safe status for one provider, without requiring its key."""

        if provider_id not in PROVIDERS:
            raise ProviderResponseError("Unknown Provider")
        return self._status.get(provider_id, ModelCatalogStatus())

    # A descriptive alias makes the intent clear at Host call sites and keeps
    # compatibility with adapters that already use ``get_status`` terminology.
    get_status = status

    def schedule_refresh(self, provider_id: str, api_key: str) -> asyncio.Task[ModelDiscovery]:
        """Start a non-blocking refresh after a credential mutation."""

        if provider_id not in PROVIDERS:
            raise ProviderResponseError("Unknown Provider")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("api_key must be a non-empty string")
        fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        cache_key = (provider_id, fingerprint)
        self._status_keys[provider_id] = cache_key
        self._set_status(provider_id, cache_key, ModelCatalogStatus(status="loading"))
        existing = self._inflight.get(cache_key)
        if existing is not None:
            return existing
        task = asyncio.create_task(
            self._discover_uncached(provider_id, api_key, cache_key),
            name=f"model-catalog:{provider_id}",
        )
        self._inflight[cache_key] = task
        task.add_done_callback(lambda done: self._task_done(cache_key, done))
        return task

    def invalidate(self, provider_id: str) -> None:
        """Forget provider status after a credential is removed."""

        if provider_id in PROVIDERS:
            for cache_key, task in tuple(self._inflight.items()):
                if cache_key[0] == provider_id:
                    if not task.done():
                        task.cancel()
                    self._inflight.pop(cache_key, None)
            for cache_key in tuple(self._cache):
                if cache_key[0] == provider_id:
                    self._cache.pop(cache_key, None)
            self._status.pop(provider_id, None)
            self._status_keys.pop(provider_id, None)

    async def aclose(self) -> None:
        """Cancel in-flight background refreshes during Host shutdown."""

        tasks = tuple(self._inflight.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()

    close = aclose

    def model_metadata(
        self,
        provider_id: str,
        models: list[str] | tuple[str, ...],
    ) -> list[dict[str, object]]:
        """Enrich remote IDs from the authoritative built-in profiles."""

        if provider_id not in PROVIDER_PRESETS:
            raise ProviderResponseError("Unknown Provider")
        profile = PROVIDER_PRESETS[provider_id]
        return [profile.model_metadata(model) for model in models]

    async def _fetch_models(self, base_url: str, api_key: str) -> list[str]:
        try:
            from openai import AsyncOpenAI
        except ImportError as error:
            raise ModelDiscoveryError("OpenAI client is not installed") from error

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=self._timeout_seconds,
            max_retries=0,
        )
        try:
            response = await client.models.list()
            data = getattr(response, "data", None)
            if not isinstance(data, list):
                raise ProviderResponseError("Provider returned an invalid model list")
            models: list[str] = []
            for item in data:
                model_id = getattr(item, "id", None)
                if not isinstance(model_id, str):
                    raise ProviderResponseError(
                        "Provider returned a model record without an ID"
                    )
                models.append(model_id)
            return models
        finally:
            await client.close()

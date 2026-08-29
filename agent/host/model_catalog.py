"""Provider-reported model discovery behind fixed trusted endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
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


@dataclass(frozen=True, slots=True)
class ModelDiscovery:
    models: list[str]
    cached: bool


class ProviderModelCatalog:
    """Discover and briefly cache model IDs without accepting caller URLs."""

    def __init__(
        self,
        *,
        fetcher: ModelFetcher | None = None,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = 5 * 60,
    ) -> None:
        self._fetcher = fetcher or self._fetch_models
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str], tuple[float, list[str]]] = {}

    async def discover(self, provider_id: str, api_key: str) -> ModelDiscovery:
        if provider_id not in PROVIDERS:
            raise ProviderResponseError("Unknown Provider")
        fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        cache_key = (provider_id, fingerprint)
        cached = self._cache.get(cache_key)
        now = self._clock()
        if cached is not None and now - cached[0] <= self._cache_seconds:
            return ModelDiscovery(list(cached[1]), cached=True)

        preset = PROVIDER_PRESETS[provider_id]
        try:
            raw_models = await self._fetcher(preset.base_url, api_key)
        except ModelDiscoveryError:
            raise
        except Exception as error:
            name = type(error).__name__.lower()
            status_code = getattr(error, "status_code", None)
            if status_code in {401, 403} or "authentication" in name:
                raise ProviderAuthenticationError(
                    "Provider rejected the configured credential"
                ) from error
            raise ModelDiscoveryError("Provider model discovery failed") from error

        if not isinstance(raw_models, list):
            raise ProviderResponseError("Provider returned an invalid model list")
        if any(not isinstance(model, str) for model in raw_models):
            raise ProviderResponseError("Provider returned an invalid model record")
        models = sorted(
            {
                model.strip()
                for model in raw_models
                if model.strip()
            }
        )
        self._cache[cache_key] = (now, models)
        return ModelDiscovery(list(models), cached=False)

    @staticmethod
    async def _fetch_models(base_url: str, api_key: str) -> list[str]:
        try:
            from openai import AsyncOpenAI
        except ImportError as error:
            raise ModelDiscoveryError("OpenAI client is not installed") from error

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
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

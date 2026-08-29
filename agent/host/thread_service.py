"""Thin Host catalog over the public ThreadRuntime interface."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from agent.model import OpenAICompatibleProvider, create_provider_config
from agent.runtime import ModelSettings, ThreadRuntime, ThreadSettings
from agent.tools import create_local_tool_registry

from .provider_config import ProviderConfigurationError, ProviderStore
from .workspace import WorkspaceBrowser


class ConfigurationRequiredError(RuntimeError):
    code = "CONFIGURATION_REQUIRED"


class ThreadNotFoundError(KeyError):
    code = "THREAD_NOT_FOUND"


class RuntimeView(Protocol):
    def create_thread(
        self,
        workspace: Path,
        *,
        settings: ModelSettings | None = None,
    ): ...

    def get_snapshot(self, thread_id: str): ...

    def get_events(self, thread_id: str, *, after_event_id: str | None = None): ...

    def update_settings(
        self,
        thread_id: str,
        *,
        expected_version: int,
        settings: ModelSettings,
    ) -> ThreadSettings: ...

    def close_thread(self, thread_id: str) -> bool: ...

    async def run_turn(
        self,
        thread_id: str,
        user_text: str,
        *,
        idempotency_key: str | None = None,
    ): ...

    def cancel_turn(self, thread_id: str) -> bool: ...


RuntimeFactory = Callable[[ModelSettings], RuntimeView]


class ThreadHost:
    """Own Host-created IDs while delegating all Agent state to Runtime."""

    def __init__(
        self,
        *,
        provider_store: ProviderStore,
        workspace_browser: WorkspaceBrowser,
        runtime_factory: RuntimeFactory,
    ) -> None:
        self._provider_store = provider_store
        self._workspace_browser = workspace_browser
        self._runtime_factory = runtime_factory
        self._runtime: RuntimeView | None = None
        self._thread_ids: list[str] = []

    def list_threads(self) -> list[dict[str, object]]:
        return [self._view(thread_id) for thread_id in self._thread_ids]

    def create_thread(
        self,
        workspace: str,
        *,
        provider_config_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]:
        selected = self._selection(provider_config_id, model)
        normalized_workspace = self._workspace_browser.validate(workspace)
        initial_settings = ModelSettings(
            provider_config_id=selected["provider_id"],
            model=selected["model"],
        )
        if self._runtime is None:
            self._runtime = self._runtime_factory(initial_settings)
        snapshot = self._runtime.create_thread(
            Path(normalized_workspace),
            settings=initial_settings,
        )
        self._thread_ids.append(snapshot.thread_id)
        return self._view(snapshot.thread_id)

    def get_thread(self, thread_id: str) -> dict[str, object]:
        self._require_thread(thread_id)
        return self._view(thread_id)

    def get_events(
        self,
        thread_id: str,
        *,
        after_event_id: str | None = None,
    ):
        runtime = self._require_thread(thread_id)
        return runtime.get_events(thread_id, after_event_id=after_event_id)

    def update_settings(
        self,
        thread_id: str,
        *,
        expected_version: int,
        settings: ModelSettings,
    ) -> dict[str, object]:
        runtime = self._require_thread(thread_id)
        self._configured_provider(settings.provider_config_id)
        runtime.update_settings(
            thread_id,
            expected_version=expected_version,
            settings=settings,
        )
        return self._view(thread_id)

    def close_thread(self, thread_id: str) -> dict[str, object]:
        runtime = self._require_thread(thread_id)
        runtime.close_thread(thread_id)
        return self._view(thread_id)

    @property
    def runtime(self) -> RuntimeView | None:
        return self._runtime

    def _view(self, thread_id: str) -> dict[str, object]:
        runtime = self._require_thread(thread_id)
        snapshot = runtime.get_snapshot(thread_id)
        events = runtime.get_events(thread_id)
        return {
            "schema_version": 1,
            "snapshot": snapshot.to_dict(),
            "event_cursor": events.latest_event_id,
            "submission": None,
        }

    def _require_thread(self, thread_id: str) -> RuntimeView:
        if thread_id not in self._thread_ids or self._runtime is None:
            raise ThreadNotFoundError(thread_id)
        return self._runtime

    def _selection(
        self,
        provider_config_id: str | None,
        model: str | None,
    ) -> dict[str, str]:
        if provider_config_id is None and model is None:
            selected = self._provider_store.default_selection()
            if selected is None:
                raise ConfigurationRequiredError("Provider and model are required")
            provider_config_id = selected["provider_id"]
            model = selected["model"]
        elif provider_config_id is None or model is None:
            raise ValueError("provider_config_id and model must be provided together")
        if not model.strip():
            raise ValueError("model must be a non-empty string")
        self._configured_provider(provider_config_id)
        return {"provider_id": provider_config_id, "model": model.strip()}

    def _configured_provider(self, provider_config_id: str) -> None:
        try:
            self._provider_store.get_credential(provider_config_id)
        except ProviderConfigurationError as error:
            raise ConfigurationRequiredError(
                "The selected Provider is not configured"
            ) from error


def production_runtime_factory(store: ProviderStore) -> RuntimeFactory:
    """Build the Runtime lazily while resolving credentials only for model calls."""

    def create(default_settings: ModelSettings) -> ThreadRuntime:
        def resolve(provider_config_id: str, model: str) -> OpenAICompatibleProvider:
            credential = store.get_credential(provider_config_id)
            config = create_provider_config(
                provider_config_id,
                api_key=credential,
                model=model,
            )
            return OpenAICompatibleProvider(config)

        return ThreadRuntime(
            tool_registry_factory=create_local_tool_registry,
            provider_resolver=resolve,
            default_settings=default_settings,
        )

    return create

"""Thin Host catalog over the public ThreadRuntime interface."""

from __future__ import annotations

from collections.abc import Callable
import inspect
from dataclasses import replace
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from agent.model import (
    OpenAICompatibleClientPool,
    OpenAICompatibleProvider,
    create_provider_config,
)
from agent.runtime import (
    ApprovalMode,
    ModelSettings,
    LocalThreadStore,
    ThreadRuntime,
    ThreadSettings,
    ThreadStore,
    ThinkingSettings,
    default_state_directory,
)
from agent.tools import create_local_tool_registry

from .provider_config import ProviderConfigurationError, ProviderStore
from .workspace import WorkspaceBrowseError, WorkspaceBrowser, WorkspaceRecord


def _disabled_capabilities() -> dict[str, object]:
    """Return a fresh conservative projection for unavailable candidates."""

    return {
        "thinking_supported": False,
        "thinking": {
            "supported": False,
        },
    }


def _safe_capabilities(value: dict[str, object]) -> dict[str, object]:
    """Strip internal thinking controls at the Host/public boundary."""

    safe: dict[str, object] = {}
    if isinstance(value.get("thinking_supported"), bool):
        safe["thinking_supported"] = value["thinking_supported"]
    context_window = value.get("context_window_tokens")
    if isinstance(context_window, int) and not isinstance(context_window, bool):
        safe["context_window_tokens"] = context_window
    nested = value.get("thinking")
    if isinstance(nested, dict):
        safe_nested: dict[str, object] = {}
        for key in (
            "supported",
            "default_enabled",
            "toggle_supported",
            "intensity_supported",
            "intensity_options",
            "default_intensity",
        ):
            candidate = nested.get(key)
            if key == "intensity_options":
                if isinstance(candidate, (list, tuple)):
                    safe_nested[key] = [
                        item for item in candidate if isinstance(item, str)
                    ]
            elif candidate is not None:
                safe_nested[key] = candidate
        safe["thinking"] = safe_nested
    if not safe:
        return _disabled_capabilities()
    return safe


class ConfigurationRequiredError(RuntimeError):
    code = "CONFIGURATION_REQUIRED"


class ThreadNotFoundError(KeyError):
    code = "THREAD_NOT_FOUND"


class ApprovalNotFoundError(RuntimeError):
    code = "APPROVAL_NOT_FOUND"


class RuntimeView(Protocol):
    def list_threads(self) -> list[object]: ...

    def create_thread(
        self,
        workspace: Path,
        *,
        settings: ModelSettings | None = None,
    ): ...

    def get_snapshot(self, thread_id: str): ...

    def capabilities_for(
        self,
        thread_id: str,
        *,
        provider_config_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]: ...

    def get_events(self, thread_id: str, *, after_event_id: str | None = None): ...

    def subscribe_events(
        self,
        thread_id: str,
        *,
        after_event_id: str | None = None,
    ): ...

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
        settings_override=None,
    ): ...

    def cancel_turn(self, thread_id: str) -> bool: ...

    def resolve_approval(
        self,
        thread_id: str,
        *,
        approval_id: str,
        approved: bool,
    ) -> bool: ...


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
        self._thread_workspaces: dict[str, WorkspaceRecord] = {}

    def list_threads(self) -> list[dict[str, object]]:
        self._ensure_persistent_runtime()
        self._sync_thread_ids()
        return [self._view(thread_id) for thread_id in self._thread_ids]

    def create_thread(
        self,
        workspace_id: str,
        *,
        provider_config_id: str | None = None,
        model: str | None = None,
        approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST,
    ) -> dict[str, object]:
        selected = self._selection(provider_config_id, model)
        workspace = self._workspace_browser.get(workspace_id)
        initial_settings = ModelSettings(
            provider_config_id=selected["provider_id"],
            model=selected["model"],
            approval_mode=approval_mode,
        )
        if self._runtime is None:
            self._runtime = self._runtime_factory(initial_settings)
        snapshot = self._runtime.create_thread(
            Path(workspace.path),
            settings=initial_settings,
        )
        if snapshot.thread_id not in self._thread_ids:
            self._thread_ids.append(snapshot.thread_id)
        self._thread_workspaces[snapshot.thread_id] = workspace
        return self._view(snapshot.thread_id)

    def get_thread(self, thread_id: str) -> dict[str, object]:
        self._require_thread(thread_id)
        return self._view(thread_id)

    def capabilities_for(
        self,
        thread_id: str,
        *,
        provider_config_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]:
        """Preview effective capabilities for persisted or draft settings."""

        runtime = self._require_thread(thread_id)
        return self._capabilities(
            runtime,
            thread_id,
            provider_config_id=provider_config_id,
            model=model,
        )

    def get_events(
        self,
        thread_id: str,
        *,
        after_event_id: str | None = None,
    ):
        runtime = self._require_thread(thread_id)
        return runtime.get_events(thread_id, after_event_id=after_event_id)

    def subscribe_events(
        self,
        thread_id: str,
        *,
        after_event_id: str | None = None,
    ):
        runtime = self._require_thread(thread_id)
        subscribe = getattr(runtime, "subscribe_events", None)
        if not callable(subscribe):
            return None
        return subscribe(thread_id, after_event_id=after_event_id)

    def update_settings(
        self,
        thread_id: str,
        *,
        expected_version: int,
        settings: ModelSettings,
    ) -> dict[str, object]:
        runtime = self._require_thread(thread_id)
        self._configured_provider(settings.provider_config_id)
        capabilities = self._capabilities(
            runtime,
            thread_id,
            provider_config_id=settings.provider_config_id,
            model=settings.model,
        )
        settings = self._normalize_settings(settings, capabilities)
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

    def resolve_approval(
        self,
        thread_id: str,
        *,
        approval_id: str,
        approved: bool,
    ) -> bool:
        runtime = self._require_thread(thread_id)
        resolve = getattr(runtime, "resolve_approval", None)
        if not callable(resolve) or not resolve(
            thread_id,
            approval_id=approval_id,
            approved=approved,
        ):
            raise ApprovalNotFoundError("approval request does not exist")
        return True

    @property
    def runtime(self) -> RuntimeView | None:
        return self._runtime

    async def shutdown(self) -> None:
        """Close Runtime resources while keeping Threads resumable."""

        if self._runtime is not None:
            close_runtime = getattr(self._runtime, "aclose", None)
            if callable(close_runtime):
                result = close_runtime()
                if inspect.isawaitable(result):
                    await result
        close = getattr(self._runtime_factory, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    def _view(self, thread_id: str) -> dict[str, object]:
        runtime = self._require_thread(thread_id)
        snapshot = runtime.get_snapshot(thread_id)
        events = runtime.get_events(thread_id)
        workspace = self._thread_workspaces.get(thread_id)
        if workspace is None:
            # Runtime-created records from an older in-memory owner are still
            # recoverable: materialize the matching Host record by canonical
            # path instead of allowing the frontend to become the authority.
            try:
                workspace = self._workspace_browser.select(snapshot.workspace)
            except WorkspaceBrowseError:
                # A deleted or no-longer-allowed directory must not hide
                # durable history. It remains unusable for new Turns, while
                # the Host can still render the canonical path in the view.
                path = Path(snapshot.workspace)
                workspace = WorkspaceRecord(
                    workspace_id=f"restored-{uuid5(NAMESPACE_URL, snapshot.workspace)}",
                    path=snapshot.workspace,
                    display_name=path.name or snapshot.workspace,
                )
            self._thread_workspaces[thread_id] = workspace
        return {
            "schema_version": 1,
            "snapshot": snapshot.to_dict(),
            "workspace": workspace.to_dict(),
            "event_cursor": events.latest_event_id,
            "capabilities": self._capabilities(runtime, thread_id),
            "submission": None,
        }

    @staticmethod
    def _capabilities(
        runtime: RuntimeView,
        thread_id: str,
        *,
        provider_config_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]:
        capabilities = getattr(runtime, "capabilities_for", None)
        if not callable(capabilities):
            capabilities = getattr(runtime, "get_capabilities", None)
        if not callable(capabilities):
            return _disabled_capabilities()
        try:
            value = capabilities(
                thread_id,
                provider_config_id=provider_config_id,
                model=model,
            )
        except TypeError:
            # Source-compatible RuntimeView fakes may still expose the V1
            # one-argument method.  They cannot preview a candidate, but do
            # not make a candidate appear to have current capabilities.
            if provider_config_id is not None or model is not None:
                return _disabled_capabilities()
            try:
                value = capabilities(thread_id)
            except Exception:
                return _disabled_capabilities()
        except Exception:
            return _disabled_capabilities()
        if not isinstance(value, dict) or not value:
            return _disabled_capabilities()
        return _safe_capabilities(value)

    @staticmethod
    def _normalize_settings(
        settings: ModelSettings,
        capabilities: dict[str, object],
    ) -> ModelSettings:
        """Normalize unsupported optional fields at the Host save boundary."""

        thinking = settings.thinking
        if thinking is None:
            return settings
        nested = capabilities.get("thinking")
        nested_capabilities = nested if isinstance(nested, dict) else {}
        supported = bool(
            capabilities.get("thinking_supported", False)
            or nested_capabilities.get("supported", False)
        )
        if not supported:
            return replace(settings, thinking=None)
        # budget_tokens and keep are internal model controls.  Capability
        # projections intentionally omit both, so ordinary Host requests can
        # never persist or expose them even when a model supports one.
        keep = None
        toggle_supported = nested_capabilities.get("toggle_supported", True)
        if not isinstance(toggle_supported, bool):
            toggle_supported = True
        intensity_supported = bool(
            nested_capabilities.get("intensity_supported", False)
        )
        intensity_options = nested_capabilities.get("intensity_options", ())
        if not isinstance(intensity_options, (list, tuple, set, frozenset)):
            intensity_options = ()
        intensity_options = tuple(
            value for value in intensity_options if isinstance(value, str)
        )
        intensity = thinking.intensity
        if not intensity_supported or intensity not in intensity_options:
            intensity = None
        normalized = ThinkingSettings(
            enabled=thinking.enabled if toggle_supported else True,
            budget_tokens=None,
            keep=keep,
            intensity=intensity,
        )
        return replace(settings, thinking=normalized)

    def _require_thread(self, thread_id: str) -> RuntimeView:
        self._ensure_persistent_runtime()
        self._sync_thread_ids()
        if thread_id not in self._thread_ids or self._runtime is None:
            raise ThreadNotFoundError(thread_id)
        return self._runtime

    def _ensure_persistent_runtime(self) -> None:
        """Hydrate a production Runtime on the first catalog/read request."""

        if self._runtime is not None:
            return
        if not getattr(self._runtime_factory, "supports_persistence", False):
            return
        self._runtime = self._runtime_factory(self._provider_defaults_for_restore())

    def _provider_defaults_for_restore(self) -> ModelSettings:
        selected = self._provider_store.default_selection()
        if selected is not None:
            return ModelSettings(
                provider_config_id=selected["provider_id"],
                model=selected["model"],
            )
        # Restored Threads retain their own settings. These placeholders
        # avoid forcing a credential lookup merely to list durable history.
        return ModelSettings(
            provider_config_id="__restored__",
            model="__restored__",
        )

    def _sync_thread_ids(self) -> None:
        if self._runtime is None:
            return
        list_threads = getattr(self._runtime, "list_threads", None)
        if not callable(list_threads):
            return
        for snapshot in list_threads():
            thread_id = getattr(snapshot, "thread_id", None)
            if isinstance(thread_id, str) and thread_id not in self._thread_ids:
                self._thread_ids.append(thread_id)

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


class ProductionRuntimeFactory:
    """Build one Runtime and retain its low-level clients for Host shutdown."""

    supports_persistence = True

    def __init__(
        self,
        store: ProviderStore,
        *,
        thread_store: ThreadStore | None = None,
        state_dir: Path | None = None,
        database_path: Path | None = None,
    ) -> None:
        if state_dir is not None and database_path is not None:
            raise ValueError("provide either state_dir or database_path, not both")
        self._store = store
        self._provider_pool = OpenAICompatibleClientPool()
        self._thread_store = thread_store
        if self._thread_store is None:
            self._thread_store = (
                LocalThreadStore(database_path=database_path)
                if database_path is not None
                else LocalThreadStore(state_dir or default_state_directory())
            )

    def __call__(self, default_settings: ModelSettings) -> ThreadRuntime:
        def resolve(provider_config_id: str, model: str) -> OpenAICompatibleProvider:
            credential = self._store.get_credential(provider_config_id)
            config = create_provider_config(
                provider_config_id,
                api_key=credential,
                model=model,
            )
            return self._provider_pool.resolve(config)

        def capabilities_for(provider_config_id: str, model: str):
            credential = self._store.get_credential(provider_config_id)
            config = create_provider_config(
                provider_config_id,
                api_key=credential,
                model=model,
            )
            return config.capabilities

        # ThreadRuntime uses this optional provider-independent preflight hook
        # to read model capabilities without allocating a transport.
        resolve.capabilities_for = capabilities_for  # type: ignore[attr-defined]

        return ThreadRuntime(
            tool_registry_factory=create_local_tool_registry,
            provider_resolver=resolve,
            default_settings=default_settings,
            store=self._thread_store,
        )

    async def close(self) -> None:
        try:
            await self._provider_pool.aclose()
        finally:
            close_store = getattr(self._thread_store, "close", None)
            if callable(close_store):
                close_store()


def production_runtime_factory(
    store: ProviderStore,
    *,
    thread_store: ThreadStore | None = None,
    state_dir: Path | None = None,
    database_path: Path | None = None,
) -> RuntimeFactory:
    """Build the Runtime lazily while resolving credentials only for model calls."""

    return ProductionRuntimeFactory(
        store,
        thread_store=thread_store,
        state_dir=state_dir,
        database_path=database_path,
    )

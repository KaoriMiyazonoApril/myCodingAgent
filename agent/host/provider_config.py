"""Durable, secret-safe configuration for built-in model Providers."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from typing import Any


SCHEMA_VERSION = 1
PROVIDERS = {
    "deepseek": "DeepSeek",
    "moonshot": "Moonshot / Kimi",
    "glm": "GLM",
}


class ProviderConfigurationError(ValueError):
    """The persisted Provider document or requested mutation is invalid."""

    code = "INVALID_PROVIDER_CONFIGURATION"


class UnknownProviderError(ProviderConfigurationError):
    code = "PROVIDER_NOT_FOUND"


class ProviderNotConfiguredError(ProviderConfigurationError):
    code = "PROVIDER_NOT_CONFIGURED"


def default_provider_config_path() -> Path:
    """Return the Linux/WSL user configuration path without touching disk."""

    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "my-coding-agent" / "providers.json"


class ProviderStore:
    """Own Provider secrets, defaults, validation, masking and atomic writes."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_provider_config_path()
        self._document = self._load()

    def list_public(self) -> list[dict[str, object]]:
        return [self._public(provider_id) for provider_id in PROVIDERS]

    def save_provider(
        self,
        provider_id: str,
        *,
        api_key: str,
        selected_model: str | None = None,
    ) -> dict[str, object]:
        self._validate_provider_id(provider_id)
        key = self._non_empty("api_key", api_key, maximum=4096)
        model = (
            None
            if selected_model is None
            else self._non_empty("selected_model", selected_model, maximum=300)
        )
        providers = self._document["providers"]
        assert isinstance(providers, dict)
        existing = providers.get(provider_id, {})
        if not isinstance(existing, dict):
            existing = {}
        providers[provider_id] = {
            "api_key": key,
            "selected_model": model or existing.get("selected_model"),
        }
        self._write()
        return self._public(provider_id)

    def get_credential(self, provider_id: str) -> str:
        self._validate_provider_id(provider_id)
        record = self._provider_record(provider_id)
        api_key = record.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            raise ProviderNotConfiguredError(
                f"Provider is not configured: {provider_id}"
            )
        return api_key

    def set_default(self, provider_id: str, *, model: str) -> dict[str, object]:
        self._validate_provider_id(provider_id)
        selected_model = self._non_empty("model", model, maximum=300)
        self.get_credential(provider_id)
        providers = self._document["providers"]
        assert isinstance(providers, dict)
        record = self._provider_record(provider_id)
        providers[provider_id] = {
            "api_key": record["api_key"],
            "selected_model": selected_model,
        }
        self._document["default_provider_id"] = provider_id
        self._write()
        return self._public(provider_id)

    def clear_credential(self, provider_id: str) -> dict[str, object]:
        self._validate_provider_id(provider_id)
        providers = self._document["providers"]
        assert isinstance(providers, dict)
        record = self._provider_record(provider_id)
        selected_model = record.get("selected_model")
        providers[provider_id] = {
            "selected_model": (
                selected_model if isinstance(selected_model, str) else None
            )
        }
        self._write()
        return self._public(provider_id)

    def default_selection(self) -> dict[str, str] | None:
        provider_id = self._document["default_provider_id"]
        if not isinstance(provider_id, str):
            return None
        model = self._provider_record(provider_id).get("selected_model")
        if not isinstance(model, str) or not model:
            return None
        return {"provider_id": provider_id, "model": model}

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {
                "schema_version": SCHEMA_VERSION,
                "default_provider_id": None,
                "providers": {},
            }
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProviderConfigurationError(
                "Provider configuration could not be read"
            ) from error
        self._validate_document(raw)
        return deepcopy(raw)

    def _write(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                os.chmod(temporary_path, 0o600)
                json.dump(
                    self._document,
                    temporary,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self._path)
            os.chmod(self._path, 0o600)
        except OSError as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ProviderConfigurationError(
                "Provider configuration could not be saved"
            ) from error

    def _public(self, provider_id: str) -> dict[str, object]:
        record = self._provider_record(provider_id)
        api_key = record.get("api_key")
        selected_model = record.get("selected_model")
        default_provider = self._document["default_provider_id"]
        return {
            "provider_id": provider_id,
            "display_name": PROVIDERS[provider_id],
            "configured": isinstance(api_key, str) and bool(api_key),
            "credential_mask": self._mask(api_key) if isinstance(api_key, str) else None,
            "selected_model": (
                selected_model if isinstance(selected_model, str) else None
            ),
            "is_default": default_provider == provider_id,
        }

    def _provider_record(self, provider_id: str) -> dict[str, object]:
        providers = self._document["providers"]
        assert isinstance(providers, dict)
        record = providers.get(provider_id)
        return record if isinstance(record, dict) else {}

    @staticmethod
    def _mask(api_key: str) -> str:
        return f"••••{api_key[-4:]}" if len(api_key) > 4 else "••••"

    @staticmethod
    def _non_empty(name: str, value: object, *, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ProviderConfigurationError(
                f"{name} must be a non-empty string of at most {maximum} characters"
            )
        return value.strip()

    @staticmethod
    def _validate_provider_id(provider_id: str) -> None:
        if provider_id not in PROVIDERS:
            raise UnknownProviderError(f"Unknown Provider: {provider_id}")

    @classmethod
    def _validate_document(cls, document: object) -> None:
        if not isinstance(document, dict):
            raise ProviderConfigurationError("Provider configuration must be an object")
        if document.get("schema_version") != SCHEMA_VERSION:
            raise ProviderConfigurationError("Unsupported Provider configuration version")
        default_provider = document.get("default_provider_id")
        if default_provider is not None and default_provider not in PROVIDERS:
            raise ProviderConfigurationError("Unknown default Provider")
        providers = document.get("providers")
        if not isinstance(providers, dict):
            raise ProviderConfigurationError("Provider records must be an object")
        for provider_id, record in providers.items():
            cls._validate_provider_id(provider_id)
            if not isinstance(record, dict):
                raise ProviderConfigurationError("Provider record must be an object")
            if set(record) - {"api_key", "selected_model"}:
                raise ProviderConfigurationError("Provider record has unknown fields")
            api_key = record.get("api_key")
            model = record.get("selected_model")
            if api_key is not None:
                cls._non_empty("api_key", api_key, maximum=4096)
            if model is not None:
                cls._non_empty("selected_model", model, maximum=300)

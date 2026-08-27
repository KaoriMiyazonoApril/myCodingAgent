"""Safe model settings owned by the Agent Runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ThinkingKeep(str, Enum):
    """Allowlisted provider history-preservation request values."""

    NONE = "none"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class ThinkingSettings:
    """Public thinking controls accepted from a future transport adapter."""

    enabled: bool
    budget_tokens: int | None = None
    keep: ThinkingKeep | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("thinking.enabled must be a boolean")
        if self.budget_tokens is not None and (
            isinstance(self.budget_tokens, bool)
            or not isinstance(self.budget_tokens, int)
            or self.budget_tokens <= 0
        ):
            raise ValueError("thinking.budget_tokens must be a positive integer")
        if self.keep is not None and not isinstance(self.keep, ThinkingKeep):
            raise ValueError("thinking.keep must be a ThinkingKeep value")
        if not self.enabled and (
            self.budget_tokens is not None or self.keep is not None
        ):
            raise ValueError("disabled thinking cannot set budget_tokens or keep")

    def to_extra_body(self) -> dict[str, object]:
        thinking: dict[str, object] = {
            "type": "enabled" if self.enabled else "disabled"
        }
        if self.budget_tokens is not None:
            thinking["budget_tokens"] = self.budget_tokens
        if self.keep is not None:
            thinking["keep"] = self.keep.value
        return {"thinking": thinking}


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """Allowlisted model defaults without provider credentials or endpoints."""

    provider_config_id: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    thinking: ThinkingSettings | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider_config_id, str)
            or not self.provider_config_id.strip()
        ):
            raise ValueError("provider_config_id must be a non-empty string")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if self.temperature is not None and (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not 0 <= self.temperature <= 2
        ):
            raise ValueError("temperature must be between 0 and 2")
        if self.max_tokens is not None and (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or self.max_tokens <= 0
        ):
            raise ValueError("max_tokens must be a positive integer")
        if self.thinking is not None and not isinstance(
            self.thinking, ThinkingSettings
        ):
            raise ValueError("thinking must be ThinkingSettings or None")


@dataclass(frozen=True, slots=True)
class ThreadSettings(ModelSettings):
    """Versioned model defaults returned from a Thread."""

    version: int = 0

    @classmethod
    def from_model_settings(
        cls, settings: ModelSettings, *, version: int
    ) -> ThreadSettings:
        return cls(
            provider_config_id=settings.provider_config_id,
            model=settings.model,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            thinking=settings.thinking,
            version=version,
        )

@dataclass(frozen=True, slots=True)
class TurnConfig(ModelSettings):
    """Immutable effective model settings captured when a Turn starts."""

    settings_version: int = 0
    system_prompt: str = ""

    @classmethod
    def from_thread_settings(
        cls, settings: ThreadSettings, *, system_prompt: str
    ) -> TurnConfig:
        return cls.from_model_settings(
            settings,
            settings_version=settings.version,
            system_prompt=system_prompt,
        )

    @classmethod
    def from_model_settings(
        cls,
        settings: ModelSettings,
        *,
        settings_version: int,
        system_prompt: str,
    ) -> TurnConfig:
        return cls(
            provider_config_id=settings.provider_config_id,
            model=settings.model,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            thinking=settings.thinking,
            settings_version=settings_version,
            system_prompt=system_prompt,
        )

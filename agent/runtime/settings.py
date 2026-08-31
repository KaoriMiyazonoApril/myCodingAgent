"""Safe model settings owned by the Agent Runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from agent.model.types import ThinkingCapabilities

from .errors import UnsupportedModelSettingError


class _Unset:
    """Sentinel separating inheritance from an explicit ``None`` override."""

    def __deepcopy__(self, memo: dict[int, object]) -> "_Unset":
        """Keep the sentinel identity stable across detached state copies."""

        del memo
        return self


_UNSET = _Unset()


class ThinkingKeep(str, Enum):
    """Allowlisted provider history-preservation request values."""

    NONE = "none"
    ALL = "all"


class ApprovalMode(str, Enum):
    """How a Turn authorizes commands that are not plainly read-only."""

    UNTRUSTED = "untrusted"
    ON_REQUEST = "on_request"
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class AgentLimits:
    """Public, bounded execution budgets frozen into one Turn."""

    max_iterations: int = 20
    max_tool_calls: int = 50
    max_execution_seconds: float = 15 * 60

    def __post_init__(self) -> None:
        self._validate_integer("max_iterations", self.max_iterations, maximum=100)
        self._validate_integer("max_tool_calls", self.max_tool_calls, maximum=500)
        if (
            isinstance(self.max_execution_seconds, bool)
            or not isinstance(self.max_execution_seconds, (int, float))
            or not 0 < self.max_execution_seconds <= 60 * 60
        ):
            raise ValueError(
                "max_execution_seconds must be greater than 0 and at most 3600"
            )
    @staticmethod
    def _validate_integer(name: str, value: int, *, maximum: int) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise ValueError(f"{name} must be between 1 and {maximum}")


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

    def validate_for(self, capabilities: ThinkingCapabilities) -> None:
        if not capabilities.supported:
            raise UnsupportedModelSettingError(
                "selected model does not support thinking"
            )
        if (
            self.budget_tokens is not None
            and not capabilities.supports_budget_tokens
        ):
            raise UnsupportedModelSettingError(
                "selected model does not support thinking.budget_tokens"
            )
        if self.keep is not None and (
            self.keep.value not in capabilities.supported_keep_values
        ):
            raise UnsupportedModelSettingError(
                f"selected model does not support thinking.keep={self.keep.value!r}"
            )


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """Allowlisted model defaults without provider credentials or endpoints."""

    provider_config_id: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    thinking: ThinkingSettings | None = None
    limits: AgentLimits = field(default_factory=AgentLimits)
    approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST

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
        if not isinstance(self.limits, AgentLimits):
            raise ValueError("limits must be AgentLimits")
        if not isinstance(self.approval_mode, ApprovalMode):
            try:
                object.__setattr__(self, "approval_mode", ApprovalMode(self.approval_mode))
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "approval_mode must be an ApprovalMode value"
                ) from error


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
            limits=settings.limits,
            approval_mode=settings.approval_mode,
            version=version,
        )


@dataclass(frozen=True, slots=True)
class TurnSettingsOverride:
    """Field-level settings patch applied to exactly one Turn."""

    provider_config_id: str | _Unset = _UNSET
    model: str | _Unset = _UNSET
    temperature: float | None | _Unset = _UNSET
    max_tokens: int | None | _Unset = _UNSET
    thinking: ThinkingSettings | None | _Unset = _UNSET
    limits: AgentLimits | _Unset = _UNSET
    approval_mode: ApprovalMode | _Unset = _UNSET

    def apply(self, defaults: ModelSettings) -> ModelSettings:
        return ModelSettings(
            provider_config_id=(
                defaults.provider_config_id
                if self.provider_config_id is _UNSET
                else self.provider_config_id
            ),
            model=defaults.model if self.model is _UNSET else self.model,
            temperature=(
                defaults.temperature
                if self.temperature is _UNSET
                else self.temperature
            ),
            max_tokens=(
                defaults.max_tokens if self.max_tokens is _UNSET else self.max_tokens
            ),
            thinking=(
                defaults.thinking if self.thinking is _UNSET else self.thinking
            ),
            limits=defaults.limits if self.limits is _UNSET else self.limits,
            approval_mode=(
                defaults.approval_mode
                if self.approval_mode is _UNSET
                else self.approval_mode
            ),
        )


@dataclass(frozen=True, slots=True)
class TurnConfig(ModelSettings):
    """Immutable effective model settings captured when a Turn starts."""

    settings_version: int = 0
    system_prompt: str = ""
    reasoning_visibility: str = "hidden"

    def __post_init__(self) -> None:
        ModelSettings.__post_init__(self)
        if self.reasoning_visibility not in {"hidden", "debug"}:
            raise ValueError("reasoning_visibility must be 'hidden' or 'debug'")

    @classmethod
    def from_thread_settings(
        cls,
        settings: ThreadSettings,
        *,
        system_prompt: str,
        reasoning_visibility: str,
    ) -> TurnConfig:
        return cls.from_model_settings(
            settings,
            settings_version=settings.version,
            system_prompt=system_prompt,
            reasoning_visibility=reasoning_visibility,
        )

    @classmethod
    def from_model_settings(
        cls,
        settings: ModelSettings,
        *,
        settings_version: int,
        system_prompt: str,
        reasoning_visibility: str,
    ) -> TurnConfig:
        return cls(
            provider_config_id=settings.provider_config_id,
            model=settings.model,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            thinking=settings.thinking,
            limits=settings.limits,
            approval_mode=settings.approval_mode,
            settings_version=settings_version,
            system_prompt=system_prompt,
            reasoning_visibility=reasoning_visibility,
        )

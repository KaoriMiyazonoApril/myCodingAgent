"""Stable failures exposed by the Agent Runtime interface."""


class ThreadBusyError(RuntimeError):
    """A Thread already has an active Turn."""

    code = "THREAD_BUSY"


class ThreadClosedError(RuntimeError):
    """A closed Thread cannot accept commands that mutate its state."""

    code = "THREAD_CLOSED"


class WorkspaceBusyError(RuntimeError):
    """A workspace overlaps an active Turn or global capacity is full."""

    code = "WORKSPACE_BUSY"


class UnsafeWorkspaceError(RuntimeError):
    """A workspace contains a forbidden link, mount or uninspectable entry."""

    code = "UNSAFE_WORKSPACE"


class WorkspaceValidationLimitError(RuntimeError):
    """A complete workspace scan exceeded its configured resource budget."""

    code = "WORKSPACE_VALIDATION_LIMIT"


class ContextLimitError(RuntimeError):
    """A complete model request cannot fit the configured context budget."""

    code = "CONTEXT_LIMIT"


class IdempotencyConflictError(RuntimeError):
    """An idempotency key was reused for a different Turn submission."""

    code = "IDEMPOTENCY_CONFLICT"


class SettingsConflictError(RuntimeError):
    """A settings update used an obsolete Thread version."""

    code = "SETTINGS_CONFLICT"


class UnsupportedModelSettingError(ValueError):
    """Raised before a request when a selected model rejects a public option."""

    code = "UNSUPPORTED_MODEL_SETTING"


class TurnLimitReached(RuntimeError):
    """An execution budget or deterministic failure loop stopped a Turn."""

    code = "LIMIT_REACHED"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ApprovalTimeoutError(RuntimeError):
    """An external tool approval did not arrive before its own timeout."""

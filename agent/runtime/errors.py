"""Stable failures exposed by the Agent Runtime interface."""


class ThreadBusyError(RuntimeError):
    """A Thread already has an active Turn."""


class SettingsConflictError(RuntimeError):
    """A settings update used an obsolete Thread version."""

    code = "SETTINGS_CONFLICT"


class UnsupportedModelSettingError(ValueError):
    """Raised before a request when a selected model rejects a public option."""

    code = "UNSUPPORTED_MODEL_SETTING"

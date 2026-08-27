"""Stable failures exposed by the Agent Runtime interface."""


class ThreadBusyError(RuntimeError):
    """A Thread already has an active Turn."""


class SettingsConflictError(RuntimeError):
    """A settings update used an obsolete Thread version."""

    code = "SETTINGS_CONFLICT"

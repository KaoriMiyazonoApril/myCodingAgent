"""Stable failures exposed by the Agent Runtime interface."""


class ThreadBusyError(RuntimeError):
    """A Thread already has an active Turn."""


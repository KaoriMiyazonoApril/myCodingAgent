"""Stable runtime-environment facts projected into a model Context.

The environment snapshot is intentionally request-scoped.  ``turn_id`` is
kept for telemetry and event correlation, but ``sections`` never exposes it
to a model, so otherwise-identical turns produce the same runtime text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import os
from pathlib import Path

from .context_types import ContextSection
from .settings import ApprovalMode


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Cheap, deterministic environment facts collected per model request."""

    workspace: str | Path
    cwd: str | Path | None = None
    shell: str | None = None
    approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST
    capabilities: tuple[str, ...] = ()
    turn_id: str | None = None

    def __post_init__(self) -> None:
        workspace = _path_text(self.workspace, "workspace")
        cwd = workspace if self.cwd is None else _path_text(self.cwd, "cwd")
        shell = self.shell or os.environ.get("SHELL") or "/bin/sh"
        if not isinstance(shell, str) or not shell.strip():
            raise ValueError("shell must be non-empty text")
        approval_mode = self.approval_mode
        if not isinstance(approval_mode, ApprovalMode):
            try:
                approval_mode = ApprovalMode(approval_mode)
            except (TypeError, ValueError) as error:
                raise ValueError("approval_mode must be an ApprovalMode value") from error
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "shell", shell.strip())
        object.__setattr__(self, "approval_mode", approval_mode)
        object.__setattr__(
            self,
            "capabilities",
            _text_tuple(self.capabilities, "capabilities"),
        )
        if self.turn_id is not None and (
            not isinstance(self.turn_id, str) or not self.turn_id.strip()
        ):
            raise ValueError("turn_id must be non-empty text or None")
        object.__setattr__(
            self,
            "turn_id",
            None if self.turn_id is None else self.turn_id.strip(),
        )

    def sections(self) -> list[ContextSection]:
        capability_text = ", ".join(self.capabilities) or "none"
        return [
            ContextSection(
                "runtime_context",
                "\n".join(
                    (
                        f"workspace: {self.workspace}",
                        f"cwd: {self.cwd}",
                        f"shell: {self.shell}",
                        f"approval_mode: {self.approval_mode.value}",
                        f"capabilities: {capability_text}",
                    )
                ),
                # Runtime facts are frozen for the Turn and do not include
                # telemetry, so they remain part of the cacheable context
                # epoch across tool iterations.
                stable=True,
            )
        ]


RuntimeEnvironment = RuntimeContext


def _path_text(value: str | Path, name: str) -> str:
    if isinstance(value, Path):
        result = value.as_posix()
    elif isinstance(value, str):
        result = value.strip()
    else:
        raise ValueError(f"{name} must be a path or string")
    if not result:
        raise ValueError(f"{name} must be non-empty")
    return result


def _text_tuple(value: Sequence[str] | str, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    try:
        values = tuple(value)
    except TypeError as error:
        raise ValueError(f"{name} must be text or a sequence of text") from error
    if any(not isinstance(item, str) for item in values):
        raise ValueError(f"{name} must contain only text")
    return values


__all__ = ["RuntimeContext", "RuntimeEnvironment"]

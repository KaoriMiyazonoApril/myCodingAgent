"""Small shared Context value types.

This module is deliberately dependency-light.  ``context.py`` remains a
compatibility facade for the original public imports while new embedders can
depend on these focused seams directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agent.core.messages import Message
from .prompt import DEFAULT_SYSTEM_PROMPT


@dataclass(frozen=True, slots=True)
class ContextSection:
    """One named source section in a Context plan."""

    name: str
    content: str
    stable: bool = True
    placement: str = "baseline"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("context section name must be non-empty")
        if not isinstance(self.content, str):
            raise ValueError("context section content must be text")
        if not isinstance(self.stable, bool):
            raise ValueError("context section stable flag must be boolean")
        if self.placement not in {"baseline", "late", "history"}:
            raise ValueError("context section placement must be baseline, late, or history")

    @property
    def partition(self) -> str:
        """Alias used by diagnostics and downstream context tooling."""

        return self.placement

    @property
    def epoch(self) -> str:
        return "late_working_tail" if self.placement == "late" else "context_epoch"


class ContextSource(Protocol):
    """A source that contributes ordered sections to a Context plan."""

    def sections(self) -> Sequence[ContextSection]:
        """Return detached sections in deterministic order."""


@dataclass(frozen=True, slots=True)
class BaseSystemInstructions:
    """Stable agent principles shared by every model request."""

    text: str = DEFAULT_SYSTEM_PROMPT

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("base system instructions must be non-empty text")

    def sections(self) -> list[ContextSection]:
        return [ContextSection("base_system_instructions", self.text)]


__all__ = ["BaseSystemInstructions", "ContextSection", "ContextSource"]

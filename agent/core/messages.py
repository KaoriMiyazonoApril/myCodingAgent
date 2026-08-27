"""The coding agent's provider-independent conversation representation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias


Role: TypeAlias = Literal["system", "user", "assistant", "tool"]


class MessageValidationError(ValueError):
    """A message role was combined with an invalid kind of content block."""


@dataclass(slots=True)
class TextBlock:
    type: Literal["text"] = field(default="text", init=False)
    text: str = ""


@dataclass(slots=True)
class ReasoningBlock:
    type: Literal["reasoning"] = field(default="reasoning", init=False)
    text: str = ""


@dataclass(slots=True)
class ToolCallBlock:
    id: str
    name: str
    arguments: dict[str, Any] | None
    arguments_error: str | None = None
    raw_arguments: str | None = None
    type: Literal["tool_call"] = field(default="tool_call", init=False)


@dataclass(slots=True)
class ToolResultBlock:
    tool_call_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    type: Literal["tool_result"] = field(default="tool_result", init=False)

    @property
    def is_error(self) -> bool:
        return self.error_code is not None

    @property
    def ok(self) -> bool:
        return not self.is_error


ContentBlock: TypeAlias = TextBlock | ReasoningBlock | ToolCallBlock | ToolResultBlock


@dataclass(slots=True)
class Message:
    role: Role
    content: list[ContentBlock]

    def __post_init__(self) -> None:
        allowed_blocks: dict[str, tuple[type[ContentBlock], ...]] = {
            "system": (TextBlock,),
            "user": (TextBlock,),
            "assistant": (TextBlock, ReasoningBlock, ToolCallBlock),
            "tool": (ToolResultBlock,),
        }
        allowed = allowed_blocks.get(self.role)
        if allowed is None:
            raise MessageValidationError(f"Unsupported message role: {self.role!r}")
        invalid = next((block for block in self.content if not isinstance(block, allowed)), None)
        if invalid is not None:
            allowed_names = ", ".join(block_type.__name__ for block_type in allowed)
            raise MessageValidationError(
                f"{self.role!r} messages may contain only {allowed_names}; "
                f"got {type(invalid).__name__}"
            )

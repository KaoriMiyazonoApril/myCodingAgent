"""Tool-domain data types independent of a particular model provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ToolDefinition:
    """A local tool's model-visible name, description, and JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class ToolResult:
    """The provider-independent result of executing one local tool call."""

    content: str
    metadata: dict[str, Any]
    error_code: str | None = None

    @property
    def is_error(self) -> bool:
        """Whether execution failed according to the stable error-code contract."""

        return self.error_code is not None

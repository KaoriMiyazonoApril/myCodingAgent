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

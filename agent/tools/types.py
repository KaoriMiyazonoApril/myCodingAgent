"""Tool-domain data types independent of a particular model provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.core.messages import ToolResultBlock


class ToolArgumentsValidationError(ValueError):
    """Arguments do not satisfy a tool's model-visible JSON Schema subset."""


@dataclass(slots=True)
class ToolDefinition:
    """A local tool's model-visible name, description, and JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any]

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate and default arguments from this definition's object schema."""

        properties = self.parameters.get("properties", {})
        required = self.parameters.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ToolArgumentsValidationError("tool has an invalid parameters schema")

        unknown = set(arguments) - set(properties)
        if self.parameters.get("additionalProperties") is False and unknown:
            names = ", ".join(sorted(unknown))
            raise ToolArgumentsValidationError(f"unknown arguments: {names}")

        normalized = dict(arguments)
        for name, property_schema in properties.items():
            if not isinstance(property_schema, dict):
                raise ToolArgumentsValidationError(
                    f"tool schema for {name!r} must be an object"
                )
            if name not in normalized and "default" in property_schema:
                normalized[name] = property_schema["default"]

        missing = [name for name in required if name not in normalized]
        if missing:
            raise ToolArgumentsValidationError(
                f"missing required arguments: {', '.join(missing)}"
            )

        for name, value in normalized.items():
            property_schema = properties.get(name)
            if isinstance(property_schema, dict):
                self._validate_value(name, value, property_schema)
        return normalized

    @staticmethod
    def _validate_value(name: str, value: Any, schema: dict[str, Any]) -> None:
        expected_type = schema.get("type")
        valid_type = {
            "string": lambda candidate: isinstance(candidate, str),
            "integer": lambda candidate: isinstance(candidate, int)
            and not isinstance(candidate, bool),
            "boolean": lambda candidate: isinstance(candidate, bool),
            "object": lambda candidate: isinstance(candidate, dict),
        }.get(expected_type)
        if valid_type is not None and not valid_type(value):
            raise ToolArgumentsValidationError(
                f"argument {name!r} must have type {expected_type}"
            )
        if isinstance(value, str) and len(value) < schema.get("minLength", 0):
            raise ToolArgumentsValidationError(
                f"argument {name!r} must contain at least {schema['minLength']} character(s)"
            )
        if isinstance(value, int) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                raise ToolArgumentsValidationError(
                    f"argument {name!r} must be at least {schema['minimum']}"
                )
            if "maximum" in schema and value > schema["maximum"]:
                raise ToolArgumentsValidationError(
                    f"argument {name!r} must be at most {schema['maximum']}"
                )


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

    def to_message_block(self, tool_call_id: str) -> ToolResultBlock:
        """Bind this execution result to a conversation tool-call result block."""

        from agent.core.messages import ToolResultBlock

        return ToolResultBlock(
            tool_call_id=tool_call_id,
            content=self.content,
            metadata=dict(self.metadata),
            error_code=self.error_code,
        )

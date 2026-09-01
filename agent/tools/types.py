"""Tool-domain data types independent of a particular model provider."""

from __future__ import annotations

from dataclasses import dataclass, field
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

        if not isinstance(arguments, dict):
            raise ToolArgumentsValidationError("tool arguments must be an object")
        if self.parameters.get("type") != "object":
            raise ToolArgumentsValidationError("tool schema must have type object")
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
            expected_type = property_schema.get("type")
            if expected_type not in {"string", "integer", "boolean", "object", "array"}:
                raise ToolArgumentsValidationError(
                    f"tool schema for {name!r} has unsupported schema type "
                    f"{expected_type!r}"
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
            "array": lambda candidate: isinstance(candidate, list),
        }.get(expected_type)
        if valid_type is not None and not valid_type(value):
            raise ToolArgumentsValidationError(
                f"argument {name!r} must have type {expected_type}"
            )
        if isinstance(value, str) and len(value) < schema.get("minLength", 0):
            raise ToolArgumentsValidationError(
                f"argument {name!r} must contain at least {schema['minLength']} character(s)"
            )
        if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ToolArgumentsValidationError(
                f"argument {name!r} must contain at most {schema['maxLength']} character(s)"
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
        enum = schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            raise ToolArgumentsValidationError(
                f"argument {name!r} must be one of: {', '.join(map(str, enum))}"
            )
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                raise ToolArgumentsValidationError(
                    f"argument {name!r} must contain at least {schema['minItems']} item(s)"
                )
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                raise ToolArgumentsValidationError(
                    f"argument {name!r} must contain at most {schema['maxItems']} item(s)"
                )
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    ToolDefinition._validate_value(
                        f"{name}[{index}]", item, item_schema
                    )
        if isinstance(value, dict):
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            if not isinstance(properties, dict) or not isinstance(required, list):
                raise ToolArgumentsValidationError(
                    f"argument {name!r} has an invalid object schema"
                )
            unknown = set(value) - set(properties)
            if schema.get("additionalProperties") is False and unknown:
                names = ", ".join(sorted(unknown))
                raise ToolArgumentsValidationError(
                    f"unknown arguments in {name!r}: {names}"
                )
            for required_name in required:
                if required_name not in value:
                    raise ToolArgumentsValidationError(
                        f"missing required argument {required_name!r} in {name!r}"
                    )
            for child_name, child_value in value.items():
                child_schema = properties.get(child_name)
                if isinstance(child_schema, dict):
                    ToolDefinition._validate_value(
                        f"{name}.{child_name}", child_value, child_schema
                    )


@dataclass(slots=True)
class ToolResult:
    """The provider-independent result of executing one local tool call."""

    content: str
    metadata: dict[str, Any]
    error_code: str | None = None
    _settled_after_cancellation: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def is_error(self) -> bool:
        """Whether execution failed according to the stable error-code contract."""

        return self.error_code is not None

    @property
    def ok(self) -> bool:
        """Whether execution completed successfully."""

        return not self.is_error

    def to_message_block(self, tool_call_id: str) -> ToolResultBlock:
        """Bind this execution result to a conversation tool-call result block."""

        from agent.core.messages import ToolResultBlock

        return ToolResultBlock(
            tool_call_id=tool_call_id,
            content=self.content,
            metadata=dict(self.metadata),
            error_code=self.error_code,
        )

"""Composition and executable implementations of local MVP tools."""

from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath
import re

from agent.tools.filesystem import ToolOperationError, WorkspaceFilesystem
from agent.tools.process import CommandRunner
from agent.tools.registry import ToolRegistry
from agent.tools.types import ToolDefinition, ToolResult


def _object_schema(
    properties: dict[str, object], required: list[str],
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _read_file(filesystem: WorkspaceFilesystem, arguments: dict[str, object]) -> ToolResult:
    allowed = {"path", "offset", "limit"}
    if set(arguments) - allowed:
        raise ToolOperationError("INVALID_ARGUMENTS", "read_file received unknown arguments")
    offset = arguments.get("offset", 1)
    limit = arguments.get("limit", 200)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 1:
        raise ToolOperationError("INVALID_ARGUMENTS", "offset must be an integer of at least 1")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000:
        raise ToolOperationError("INVALID_ARGUMENTS", "limit must be an integer from 1 through 2000")

    content, relative = filesystem.read_text_file(arguments.get("path"))
    lines = content.splitlines()
    page = lines[offset - 1 : offset - 1 + limit]
    start_line = offset
    end_line = offset + len(page) - 1
    return ToolResult(
        content="\n".join(f"{number}: {line}" for number, line in enumerate(page, offset)),
        metadata={
            "path": relative,
            "requested_limit": limit,
            "returned_lines": len(page),
            "total_lines": len(lines),
            "start_line": start_line,
            "end_line": end_line,
            "truncated": end_line < len(lines),
        },
    )


def _write_file(filesystem: WorkspaceFilesystem, arguments: dict[str, object]) -> ToolResult:
    if set(arguments) - {"path", "content"} or {"path", "content"} - set(arguments):
        raise ToolOperationError("INVALID_ARGUMENTS", "write_file requires path and content only")
    relative, bytes_written = filesystem.write_text_file(
        arguments["path"], arguments["content"]
    )
    return ToolResult(
        content=f"wrote {relative}",
        metadata={"path": relative, "bytes_written": bytes_written},
    )


def _edit_file(filesystem: WorkspaceFilesystem, arguments: dict[str, object]) -> ToolResult:
    required = {"path", "old_string", "new_string"}
    if set(arguments) - (required | {"replace_all"}) or required - set(arguments):
        raise ToolOperationError("INVALID_ARGUMENTS", "edit_file requires path, old_string, and new_string")
    old_string = arguments["old_string"]
    new_string = arguments["new_string"]
    replace_all = arguments.get("replace_all", False)
    if not isinstance(old_string, str) or not old_string:
        raise ToolOperationError("INVALID_ARGUMENTS", "old_string must be a non-empty string")
    if not isinstance(new_string, str) or not isinstance(replace_all, bool):
        raise ToolOperationError("INVALID_ARGUMENTS", "new_string must be a string and replace_all a boolean")

    content, relative = filesystem.read_text_file(arguments["path"])
    occurrences = content.count(old_string)
    if occurrences == 0:
        raise ToolOperationError("EDIT_NOT_FOUND", "old_string does not occur in the file")
    if not replace_all and occurrences > 1:
        raise ToolOperationError("EDIT_AMBIGUOUS", "old_string occurs more than once")
    replacements = occurrences if replace_all else 1
    updated = content.replace(old_string, new_string, replacements)
    filesystem.write_text_file(relative, updated)
    return ToolResult(
        content=f"edited {relative}",
        metadata={"path": relative, "replacements": replacements},
    )


def _glob(filesystem: WorkspaceFilesystem, arguments: dict[str, object]) -> ToolResult:
    if set(arguments) - {"path", "pattern"} or "pattern" not in arguments:
        raise ToolOperationError("INVALID_ARGUMENTS", "glob requires pattern and optional path only")
    pattern = arguments["pattern"]
    if not isinstance(pattern, str) or not pattern:
        raise ToolOperationError("INVALID_ARGUMENTS", "pattern must be a non-empty string")
    selected_path = arguments.get("path", ".")
    selected, selected_relative = filesystem.resolve(selected_path)
    matches = [
        relative
        for _, relative in filesystem.regular_files(selected_path)
        if PurePosixPath(relative).relative_to(
            PurePosixPath(selected.relative_to(filesystem.root).as_posix())
        ).match(pattern)
    ]
    matches.sort()
    returned = matches[:200]
    return ToolResult(
        content="\n".join(returned),
        metadata={
            "path": selected_relative,
            "matches": len(returned),
            "truncated": len(matches) > len(returned),
        },
    )


def _grep(filesystem: WorkspaceFilesystem, arguments: dict[str, object]) -> ToolResult:
    if set(arguments) - {"path", "pattern", "include"} or "pattern" not in arguments:
        raise ToolOperationError("INVALID_ARGUMENTS", "grep requires pattern and optional path and include only")
    pattern = arguments["pattern"]
    include = arguments.get("include")
    if not isinstance(pattern, str) or not pattern:
        raise ToolOperationError("INVALID_ARGUMENTS", "pattern must be a non-empty string")
    if include is not None and (not isinstance(include, str) or not include):
        raise ToolOperationError("INVALID_ARGUMENTS", "include must be a non-empty string")
    try:
        expression = re.compile(pattern)
    except re.error as error:
        raise ToolOperationError("INVALID_REGEX", f"invalid regular expression: {error}") from error

    selected_path = arguments.get("path", ".")
    _, selected_relative = filesystem.resolve(selected_path)
    matches: list[str] = []
    for file_path, relative in filesystem.regular_files(selected_path):
        if include is not None and not PurePosixPath(relative).match(include):
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "\0" in content:
            continue
        for line_number, line in enumerate(content.splitlines(), 1):
            if expression.search(line):
                matches.append(f"{relative}:{line_number}: {line}")
    matches.sort()
    returned = matches[:200]
    return ToolResult(
        content="\n".join(returned),
        metadata={
            "path": selected_relative,
            "matches": len(returned),
            "truncated": len(matches) > len(returned),
        },
    )


def _run_command(runner: CommandRunner, arguments: dict[str, object]) -> ToolResult:
    if set(arguments) - {"command", "cwd", "timeout_ms"} or "command" not in arguments:
        raise ToolOperationError("INVALID_ARGUMENTS", "run_command requires command and optional cwd and timeout_ms only")
    return runner.run(
        arguments["command"], arguments.get("cwd", "."), arguments.get("timeout_ms", 60000)
    )


def create_local_tool_registry(workspace_root: Path) -> ToolRegistry:
    """Compose shared workspace services and register the available local tools."""

    filesystem = WorkspaceFilesystem(workspace_root)
    runner = CommandRunner(filesystem)
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read_file",
            description="Read a UTF-8 text file with one-based line pagination.",
            parameters=_object_schema(
                {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 1, "default": 1},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2000,
                        "default": 200,
                    },
                },
                ["path"],
            ),
        ),
        lambda arguments: _read_file(filesystem, arguments),
    )
    registry.register(
        ToolDefinition(
            name="write_file",
            description="Atomically write UTF-8 text to a workspace-relative file.",
            parameters=_object_schema(
                {"path": {"type": "string"}, "content": {"type": "string"}},
                ["path", "content"],
            ),
        ),
        lambda arguments: _write_file(filesystem, arguments),
    )
    registry.register(
        ToolDefinition(
            name="edit_file",
            description="Replace one or every exact string occurrence in a UTF-8 text file.",
            parameters=_object_schema(
                {
                    "path": {"type": "string"},
                    "old_string": {"type": "string", "minLength": 1},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                ["path", "old_string", "new_string"],
            ),
        ),
        lambda arguments: _edit_file(filesystem, arguments),
    )
    registry.register(
        ToolDefinition(
            name="glob",
            description="Find regular workspace files matching a relative glob pattern.",
            parameters=_object_schema(
                {
                    "path": {"type": "string", "default": "."},
                    "pattern": {"type": "string", "minLength": 1},
                },
                ["pattern"],
            ),
        ),
        lambda arguments: _glob(filesystem, arguments),
    )
    registry.register(
        ToolDefinition(
            name="grep",
            description="Search UTF-8 workspace files with a Python regular expression.",
            parameters=_object_schema(
                {
                    "path": {"type": "string", "default": "."},
                    "pattern": {"type": "string", "minLength": 1},
                    "include": {"type": "string", "minLength": 1},
                },
                ["pattern"],
            ),
        ),
        lambda arguments: _grep(filesystem, arguments),
    )
    registry.register(
        ToolDefinition(
            name="run_command",
            description="Run one non-interactive shell command from a workspace-relative directory.",
            parameters=_object_schema(
                {
                    "command": {"type": "string", "minLength": 1},
                    "cwd": {"type": "string", "default": "."},
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 300000,
                        "default": 60000,
                    },
                },
                ["command"],
            ),
        ),
        lambda arguments: _run_command(runner, arguments),
    )
    return registry

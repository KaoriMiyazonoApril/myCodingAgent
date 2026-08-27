"""Composition and executable implementations of local MVP tools."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import cast

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


def _matches_relative_glob(path: str, pattern: str) -> bool:
    """Match path segments using pathlib-style relative glob semantics."""

    path_parts = PurePosixPath(path).parts
    pattern_parts = PurePosixPath(pattern).parts

    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        segment = pattern_parts[pattern_index]
        if segment == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], segment)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def _read_file(filesystem: WorkspaceFilesystem, arguments: dict[str, object]) -> ToolResult:
    offset = cast(int, arguments["offset"])
    limit = cast(int, arguments["limit"])

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
    relative, bytes_written = filesystem.write_text_file(
        arguments["path"], arguments["content"]
    )
    return ToolResult(
        content=f"wrote {relative}",
        metadata={"path": relative, "bytes_written": bytes_written},
    )


def _edit_file(filesystem: WorkspaceFilesystem, arguments: dict[str, object]) -> ToolResult:
    old_string = cast(str, arguments["old_string"])
    new_string = cast(str, arguments["new_string"])
    replace_all = cast(bool, arguments["replace_all"])

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
    pattern = cast(str, arguments["pattern"])
    pattern_path = PurePosixPath(pattern)
    if pattern_path.is_absolute() or ".." in pattern_path.parts:
        raise ToolOperationError("INVALID_ARGUMENTS", "pattern must be a relative glob")
    selected_path = arguments["path"]
    selected, selected_relative = filesystem.resolve(selected_path)
    matches = [
        relative
        for _, relative in filesystem.regular_files(selected_path)
        if _matches_relative_glob(
            PurePosixPath(relative)
            .relative_to(PurePosixPath(selected.relative_to(filesystem.root).as_posix()))
            .as_posix(),
            pattern,
        )
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
    pattern = cast(str, arguments["pattern"])
    include = arguments.get("include")
    try:
        expression = re.compile(pattern)
    except re.error as error:
        raise ToolOperationError("INVALID_REGEX", f"invalid regular expression: {error}") from error

    selected_path = arguments["path"]
    _, selected_relative = filesystem.resolve(selected_path)
    matches: list[tuple[str, int, str]] = []
    for _, relative in filesystem.regular_files(selected_path):
        if include is not None and not PurePosixPath(relative).match(include):
            continue
        try:
            content, relative = filesystem.read_text_file(relative)
        except ToolOperationError as error:
            if error.code == "NOT_TEXT":
                continue
            raise
        for line_number, line in enumerate(content.splitlines(), 1):
            if expression.search(line):
                matches.append((relative, line_number, line))
    matches.sort(key=lambda match: (match[0], match[1]))
    returned = matches[:200]
    return ToolResult(
        content="\n".join(f"{relative}:{line_number}: {line}" for relative, line_number, line in returned),
        metadata={
            "path": selected_relative,
            "matches": len(returned),
            "truncated": len(matches) > len(returned),
        },
    )


def _run_command(runner: CommandRunner, arguments: dict[str, object]) -> ToolResult:
    return runner.run(
        cast(str, arguments["command"]),
        cast(str, arguments["cwd"]),
        cast(int, arguments["timeout_ms"]),
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
                    "path": {"type": "string", "minLength": 1},
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
                {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
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
                    "path": {"type": "string", "minLength": 1},
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
                    "path": {"type": "string", "minLength": 1, "default": "."},
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
                    "path": {"type": "string", "minLength": 1, "default": "."},
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
                    "cwd": {"type": "string", "minLength": 1, "default": "."},
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

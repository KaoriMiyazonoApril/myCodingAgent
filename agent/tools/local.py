"""Composition and executable implementations of local MVP tools."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from pathlib import PurePosixPath
import time
from typing import cast

import regex

from agent.tools.apply_patch import apply_patch
from agent.tools.filesystem import ToolOperationError, WorkspaceFilesystem
from agent.tools.process import CommandRunner, CommandSandboxBackend, ProcessManager
from agent.tools.registry import ToolRegistry
from agent.tools.types import ToolDefinition, ToolResult


MAX_RETURNED_MATCHES = 200
MAX_SCANNED_FILES = 10_000
MAX_SEARCH_FILE_BYTES = 5 * 1024 * 1024
MAX_SEARCH_TOTAL_BYTES = 50 * 1024 * 1024
MAX_SEARCH_LINE_CHARS = 100_000
MAX_SEARCH_DURATION_SECONDS = 5.0
REGEX_TIMEOUT_SECONDS = 0.05


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

    page, total_lines, relative, fingerprint = filesystem.read_text_page(
        arguments.get("path"), offset=offset, limit=limit
    )
    start_line = offset if page else None
    end_line = offset + len(page) - 1 if page else None
    return ToolResult(
        content="\n".join(f"{number}: {line}" for number, line in enumerate(page, offset)),
        metadata={
            "path": relative,
            "requested_offset": offset,
            "requested_limit": limit,
            "returned_lines": len(page),
            "total_lines": total_lines,
            "start_line": start_line,
            "end_line": end_line,
            "truncated": bool(page) and end_line < total_lines,
            "content_fingerprint": fingerprint,
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


def _apply_patch(filesystem: WorkspaceFilesystem, arguments: dict[str, object]) -> ToolResult:
    result = apply_patch(filesystem, arguments["patch"])
    return ToolResult(
        content=(
            f"applied patch to {len(result.affected_paths)} file"
            f"{'s' if len(result.affected_paths) != 1 else ''}"
        ),
        metadata={
            "affected_paths": result.affected_paths,
            "added_count": result.added_count,
            "updated_count": result.updated_count,
            "deleted_count": result.deleted_count,
        },
    )


def _glob(filesystem: WorkspaceFilesystem, arguments: dict[str, object]) -> ToolResult:
    pattern = cast(str, arguments["pattern"])
    pattern_path = PurePosixPath(pattern)
    if pattern_path.is_absolute() or ".." in pattern_path.parts:
        raise ToolOperationError("INVALID_ARGUMENTS", "pattern must be a relative glob")
    selected_path = arguments["path"]
    selected, selected_relative = filesystem.resolve(selected_path)
    selected_prefix = PurePosixPath(selected.relative_to(filesystem.root).as_posix())
    matches: list[str] = []
    truncated = False
    for scanned_index, (_, relative) in enumerate(
        filesystem.regular_files(selected_path)
    ):
        if scanned_index >= MAX_SCANNED_FILES:
            truncated = True
            break
        relative_to_selected = PurePosixPath(relative).relative_to(selected_prefix)
        if not _matches_relative_glob(relative_to_selected.as_posix(), pattern):
            continue
        if len(matches) >= MAX_RETURNED_MATCHES:
            truncated = True
            break
        matches.append(relative)
    return ToolResult(
        content="\n".join(matches),
        metadata={
            "path": selected_relative,
            "matches": len(matches),
            "truncated": truncated,
        },
    )


def _grep(filesystem: WorkspaceFilesystem, arguments: dict[str, object]) -> ToolResult:
    pattern = cast(str, arguments["pattern"])
    include = arguments.get("include")
    try:
        expression = regex.compile(pattern)
    except regex.error as error:
        raise ToolOperationError("INVALID_REGEX", f"invalid regular expression: {error}") from error

    selected_path = arguments["path"]
    _, selected_relative = filesystem.resolve(selected_path)
    matches: list[tuple[str, int, str]] = []
    truncated = False
    scanned_bytes = 0
    deadline = time.monotonic() + MAX_SEARCH_DURATION_SECONDS
    limit_reached = False
    for scanned_index, (path, relative) in enumerate(
        filesystem.regular_files(selected_path)
    ):
        if scanned_index >= MAX_SCANNED_FILES or time.monotonic() >= deadline:
            truncated = True
            break
        if include is not None and not PurePosixPath(relative).match(include):
            continue
        try:
            file_bytes = path.stat().st_size
        except OSError:
            truncated = True
            continue
        if scanned_bytes + file_bytes > MAX_SEARCH_TOTAL_BYTES:
            truncated = True
            break
        scanned_bytes += file_bytes
        try:
            content, _ = filesystem.read_text_file(
                relative, max_bytes=MAX_SEARCH_FILE_BYTES
            )
        except ToolOperationError as error:
            if error.code == "NOT_TEXT":
                continue
            if error.code == "FILE_TOO_LARGE":
                truncated = True
                continue
            raise
        for line_number, line in enumerate(content.splitlines(), 1):
            if time.monotonic() >= deadline:
                truncated = True
                limit_reached = True
                break
            if len(line) > MAX_SEARCH_LINE_CHARS:
                truncated = True
                continue
            try:
                matched = expression.search(line, timeout=REGEX_TIMEOUT_SECONDS)
            except TimeoutError as error:
                raise ToolOperationError(
                    "REGEX_TIMEOUT", "regular expression exceeded its match time limit"
                ) from error
            if matched:
                if len(matches) >= MAX_RETURNED_MATCHES:
                    truncated = True
                    limit_reached = True
                    break
                matches.append((relative, line_number, line))
        if limit_reached:
            break
    return ToolResult(
        content="\n".join(
            f"{relative}:{line_number}: {line}"
            for relative, line_number, line in matches
        ),
        metadata={
            "path": selected_relative,
            "matches": len(matches),
            "truncated": truncated,
        },
    )


def _run_command(runner: CommandRunner, arguments: dict[str, object]) -> ToolResult:
    return runner.run(
        cast(str, arguments["command"]),
        cast(str, arguments["cwd"]),
        cast(int, arguments["timeout_ms"]),
    )


async def _run_command_async(
    runner: CommandRunner, arguments: dict[str, object]
) -> ToolResult:
    return await runner.run_async(
        cast(str, arguments["command"]),
        cast(str, arguments["cwd"]),
        cast(int, arguments["timeout_ms"]),
    )


async def _exec_command(
    manager: ProcessManager, arguments: dict[str, object]
) -> ToolResult:
    return await manager.exec(
        cast(str, arguments["command"]),
        cast(str, arguments["cwd"]),
        cast(int, arguments["yield_time_ms"]),
        cast(int, arguments["timeout_ms"]),
        cast(bool, arguments["tty"]),
    )


async def _write_stdin(
    manager: ProcessManager, arguments: dict[str, object]
) -> ToolResult:
    return await manager.write_stdin(
        cast(str, arguments["session_id"]),
        cast(str, arguments["chars"]),
        cast(int, arguments["yield_time_ms"]),
    )


def create_local_tool_registry(
    workspace_root: Path,
    *,
    sandbox_backend: CommandSandboxBackend | None = None,
) -> ToolRegistry:
    """Compose shared workspace services and register the available local tools."""

    filesystem = WorkspaceFilesystem(workspace_root)
    runner = CommandRunner(filesystem, sandbox_backend=sandbox_backend)
    process_manager = ProcessManager(
        filesystem,
        sandbox_backend=runner.sandbox,
        sandbox_checked=True,
    )
    registry = ToolRegistry(
        on_close=process_manager.close,
        async_on_close=process_manager.aclose,
    )
    registry.bind_event_sink(process_manager.set_event_sink)
    registry.bind_session_canceller(process_manager.cancel_active)
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
            name="apply_patch",
            description="Atomically apply ordered add, update, and delete file operations.",
            parameters=_object_schema(
                {"patch": {"type": "string", "minLength": 1}},
                ["patch"],
            ),
        ),
        lambda arguments: _apply_patch(filesystem, arguments),
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
        async_executor=lambda arguments: _run_command_async(runner, arguments),
    )
    registry.register(
        ToolDefinition(
            name="exec_command",
            description="Start a sandboxed command and return bounded incremental output.",
            parameters=_object_schema(
                {
                    "command": {"type": "string", "minLength": 1},
                    "cwd": {"type": "string", "minLength": 1, "default": "."},
                    "yield_time_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 300000,
                        "default": 1000,
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 900000,
                        "default": 60000,
                    },
                    "tty": {"type": "boolean", "default": False},
                },
                ["command"],
            ),
        ),
        lambda arguments: _exec_command(process_manager, arguments),
        async_executor=lambda arguments: _exec_command(process_manager, arguments),
    )
    registry.register(
        ToolDefinition(
            name="write_stdin",
            description="Write to an existing command session or poll its output.",
            parameters=_object_schema(
                {
                    "session_id": {"type": "string", "minLength": 1},
                    "chars": {"type": "string", "default": ""},
                    "yield_time_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 300000,
                        "default": 1000,
                    },
                },
                ["session_id"],
            ),
        ),
        lambda arguments: _write_stdin(process_manager, arguments),
        async_executor=lambda arguments: _write_stdin(process_manager, arguments),
    )
    return registry

from __future__ import annotations

import asyncio
import os
import shutil
import time

import pytest

from agent.core.messages import ToolCallBlock
from agent.tools.local import create_local_tool_registry
from agent.tools.registry import ToolRegistry
from agent.tools.types import ToolDefinition, ToolResult


def test_read_file_returns_numbered_page_and_metadata(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("first\nsecond\nthird\n", encoding="utf-8")
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_read",
            name="read_file",
            arguments={"path": "notes.txt", "offset": 2, "limit": 1},
        )
    )

    assert result.error_code is None
    assert result.content == "2: second"
    assert result.metadata == {
        "path": "notes.txt",
        "requested_limit": 1,
        "returned_lines": 1,
        "total_lines": 3,
        "start_line": 2,
        "end_line": 2,
        "truncated": True,
    }


def test_write_file_creates_parent_directories_and_text_file(tmp_path) -> None:
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_write",
            name="write_file",
            arguments={"path": "src/new.py", "content": "print('hello')\n"},
        )
    )

    assert result.error_code is None
    assert (tmp_path / "src" / "new.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert result.metadata == {"path": "src/new.py", "bytes_written": 15}


def test_edit_file_replaces_one_exact_match(tmp_path) -> None:
    source = tmp_path / "app.py"
    source.write_text("value = 'old'\n", encoding="utf-8")
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_edit",
            name="edit_file",
            arguments={
                "path": "app.py",
                "old_string": "'old'",
                "new_string": "'new'",
            },
        )
    )

    assert result.error_code is None
    assert source.read_text(encoding="utf-8") == "value = 'new'\n"
    assert result.metadata == {"path": "app.py", "replacements": 1}


def test_glob_returns_sorted_workspace_relative_regular_files(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "notes.txt").write_text("", encoding="utf-8")
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_glob",
            name="glob",
            arguments={"path": "src", "pattern": "*.py"},
        )
    )

    assert result.error_code is None
    assert result.content == "src/a.py\nsrc/b.py"
    assert result.metadata == {"path": "src", "matches": 2, "truncated": False}


def test_grep_returns_matching_paths_line_numbers_and_lines(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("first\nneedle here\n", encoding="utf-8")
    (tmp_path / "src" / "skip.txt").write_text("needle too\n", encoding="utf-8")
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_grep",
            name="grep",
            arguments={"pattern": "needle", "include": "*.py"},
        )
    )

    assert result.error_code is None
    assert result.content == "src/main.py:2: needle here"
    assert result.metadata == {"path": ".", "matches": 1, "truncated": False}


def test_grep_returns_lines_in_file_order(tmp_path) -> None:
    source = tmp_path / "main.py"
    source.write_text("\n".join(["needle"] * 10) + "\n", encoding="utf-8")
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(id="call_order", name="grep", arguments={"pattern": "needle"})
    )

    assert result.error_code is None
    assert result.content.splitlines() == [f"main.py:{line}: needle" for line in range(1, 11)]


def test_grep_skips_non_text_files_and_keeps_searching(tmp_path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"needle\x00")
    (tmp_path / "main.py").write_text("needle\n", encoding="utf-8")
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(id="call_binary_search", name="grep", arguments={"pattern": "needle"})
    )

    assert result.error_code is None
    assert result.content == "main.py:1: needle"


def test_glob_uses_relative_pathlib_matching_for_recursive_patterns(tmp_path) -> None:
    (tmp_path / "src" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "root.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "nested" / "deep.py").write_text("", encoding="utf-8")
    registry = create_local_tool_registry(tmp_path)

    direct = registry.execute(
        ToolCallBlock(
            id="call_direct_glob",
            name="glob",
            arguments={"path": "src", "pattern": "*.py"},
        )
    )
    recursive = registry.execute(
        ToolCallBlock(
            id="call_recursive_glob",
            name="glob",
            arguments={"path": "src", "pattern": "**/*.py"},
        )
    )

    assert direct.error_code is None
    assert direct.content == "src/root.py"
    assert recursive.error_code is None
    assert recursive.content == "src/nested/deep.py\nsrc/root.py"


def test_glob_rejects_absolute_or_escaping_patterns(tmp_path) -> None:
    registry = create_local_tool_registry(tmp_path)

    for pattern in ("/outside/*.py", "../*.py"):
        result = registry.execute(
            ToolCallBlock(
                id="call_invalid_glob",
                name="glob",
                arguments={"pattern": pattern},
            )
        )
        assert result.error_code == "INVALID_ARGUMENTS"


def test_write_file_reports_a_file_parent_as_an_operational_error(tmp_path) -> None:
    (tmp_path / "parent").write_text("not a directory", encoding="utf-8")
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_file_parent",
            name="write_file",
            arguments={"path": "parent/child.txt", "content": "text"},
        )
    )

    assert result.error_code == "IO_ERROR"


def test_run_command_captures_separate_output_and_nonzero_exit(tmp_path) -> None:
    (tmp_path / "work").mkdir()
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_command",
            name="run_command",
            arguments={
                "command": "printf out; printf err >&2; exit 7",
                "cwd": "work",
            },
        )
    )

    assert result.error_code is None
    assert result.metadata["cwd"] == "work"
    assert result.metadata["exit_code"] == 7
    assert result.metadata["timed_out"] is False
    assert result.metadata["stdout"] == "out"
    assert result.metadata["stderr"] == "err"
    assert result.metadata["command_succeeded"] is False
    assert result.content.startswith("command exited with status 7")


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is unavailable")
def test_run_command_cannot_read_outside_workspace(tmp_path) -> None:
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_isolation",
            name="run_command",
            arguments={
                "command": (
                    "if test -r /etc/passwd; then printf exposed; "
                    "else printf isolated; fi"
                )
            },
        )
    )

    assert result.error_code is None
    assert result.metadata["stdout"] == "isolated"
    assert result.metadata["sandboxed"] is True


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is unavailable")
def test_run_command_can_write_inside_without_modifying_host_sibling(tmp_path) -> None:
    outside = tmp_path.parent / "outside-command.txt"
    outside.write_text("unchanged", encoding="utf-8")
    registry = create_local_tool_registry(tmp_path)

    inside = registry.execute(
        ToolCallBlock(
            id="call_inside_write",
            name="run_command",
            arguments={"command": "printf created > generated.txt"},
        )
    )
    outside_attempt = registry.execute(
        ToolCallBlock(
            id="call_outside_write",
            name="run_command",
            arguments={"command": "printf changed > ../outside-command.txt"},
        )
    )

    assert inside.metadata["command_succeeded"] is True
    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == "created"
    assert outside_attempt.error_code is None
    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_run_command_fails_closed_when_isolation_is_unavailable(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("agent.tools.process.shutil.which", lambda _: None)
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_no_sandbox",
            name="run_command",
            arguments={"command": "printf must-not-run > must-not-run"},
        )
    )

    assert result.error_code == "PROCESS_START_FAILED"
    assert not (tmp_path / "must-not-run").exists()


@pytest.mark.skipif(os.name == "nt", reason="setsid is POSIX-only")
def test_timeout_returns_even_if_detached_descendant_keeps_pipe_open(tmp_path) -> None:
    registry = create_local_tool_registry(tmp_path)
    start = time.monotonic()

    result = registry.execute(
        ToolCallBlock(
            id="call_detached_timeout",
            name="run_command",
            arguments={
                "command": "setsid sh -c 'sleep 1' & printf begun; sleep 1",
                "timeout_ms": 30,
            },
        )
    )

    assert result.error_code == "TIMEOUT"
    assert time.monotonic() - start < 0.5
    assert result.metadata["stdout"] == "begun"


def test_read_out_of_range_and_non_text_files_return_structured_results(tmp_path) -> None:
    (tmp_path / "short.txt").write_text("only\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\xff\x00")
    registry = create_local_tool_registry(tmp_path)

    page = registry.execute(
        ToolCallBlock(
            id="call_page", name="read_file", arguments={"path": "short.txt", "offset": 4}
        )
    )
    non_text = registry.execute(
        ToolCallBlock(id="call_binary", name="read_file", arguments={"path": "binary.bin"})
    )

    assert page.error_code is None
    assert page.content == ""
    assert page.metadata["start_line"] == 4
    assert page.metadata["end_line"] == 3
    assert page.metadata["truncated"] is False
    assert non_text.error_code == "NOT_TEXT"


def test_write_overwrite_preserves_mode_and_edit_failure_is_immutable(tmp_path) -> None:
    script = tmp_path / "script.sh"
    script.write_text("one\none\n", encoding="utf-8")
    script.chmod(0o755)
    registry = create_local_tool_registry(tmp_path)

    write = registry.execute(
        ToolCallBlock(
            id="call_overwrite",
            name="write_file",
            arguments={"path": "script.sh", "content": "one\none\n"},
        )
    )
    ambiguous = registry.execute(
        ToolCallBlock(
            id="call_ambiguous",
            name="edit_file",
            arguments={"path": "script.sh", "old_string": "one", "new_string": "two"},
        )
    )
    assert ambiguous.error_code == "EDIT_AMBIGUOUS"
    assert script.read_text(encoding="utf-8") == "one\none\n"
    global_edit = registry.execute(
        ToolCallBlock(
            id="call_global",
            name="edit_file",
            arguments={
                "path": "script.sh",
                "old_string": "one",
                "new_string": "two",
                "replace_all": True,
            },
        )
    )

    assert write.error_code is None
    assert os.stat(script).st_mode & 0o777 == 0o755
    assert script.read_text(encoding="utf-8") == "two\ntwo\n"
    assert global_edit.metadata["replacements"] == 2


def test_search_skips_default_ignored_directories_but_honors_explicit_path(tmp_path) -> None:
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "package.js").write_text("needle\n", encoding="utf-8")
    (tmp_path / "app.js").write_text("needle\n", encoding="utf-8")
    registry = create_local_tool_registry(tmp_path)

    default_search = registry.execute(
        ToolCallBlock(id="call_default", name="grep", arguments={"pattern": "needle"})
    )
    explicit_search = registry.execute(
        ToolCallBlock(
            id="call_explicit",
            name="grep",
            arguments={"path": "node_modules", "pattern": "needle"},
        )
    )
    invalid_regex = registry.execute(
        ToolCallBlock(id="call_regex", name="grep", arguments={"pattern": "["})
    )

    assert default_search.content == "app.js:1: needle"
    assert explicit_search.content == "node_modules/package.js:1: needle"
    assert invalid_regex.error_code == "INVALID_REGEX"


def test_workspace_escapes_unknown_tools_and_duplicate_registration_are_structured(tmp_path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "external.txt").symlink_to(outside)
    registry = create_local_tool_registry(tmp_path)

    escaped = registry.execute(
        ToolCallBlock(id="call_escape", name="read_file", arguments={"path": "../outside.txt"})
    )
    linked = registry.execute(
        ToolCallBlock(id="call_link", name="read_file", arguments={"path": "external.txt"})
    )
    unknown = registry.execute(
        ToolCallBlock(id="call_unknown", name="missing", arguments={})
    )
    duplicate = ToolRegistry()
    definition = ToolDefinition("tool", "test", {"type": "object"})
    duplicate.register(definition, lambda _: unknown)

    assert escaped.error_code == "WORKSPACE_ESCAPE"
    assert linked.error_code == "WORKSPACE_ESCAPE"
    assert unknown.error_code == "UNKNOWN_TOOL"
    try:
        duplicate.register(definition, lambda _: unknown)
    except ValueError as error:
        assert "already registered" in str(error)
    else:
        raise AssertionError("duplicate registration must fail")


def test_symlink_loops_are_reported_as_workspace_path_errors(tmp_path) -> None:
    (tmp_path / "loop").symlink_to("loop")
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(id="call_loop", name="read_file", arguments={"path": "loop"})
    )

    assert result.error_code == "WORKSPACE_ESCAPE"


def test_all_six_definitions_are_closed_object_schemas(tmp_path) -> None:
    registry = create_local_tool_registry(tmp_path)
    definitions = registry.definitions()

    assert {definition.name for definition in definitions} == {
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "run_command",
    }
    assert all(definition.parameters["additionalProperties"] is False for definition in definitions)
    assert registry.lookup("read_file").name == "read_file"
    assert registry.lookup("missing") is None


def test_registry_uses_tool_schema_for_validation_and_defaults() -> None:
    registry = ToolRegistry()
    received: list[dict[str, object]] = []
    registry.register(
        ToolDefinition(
            name="sample",
            description="sample",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "limit": {"type": "integer", "default": 3},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
        lambda arguments: received.append(arguments) or ToolResult("ok", {}),
    )

    valid = registry.execute(
        ToolCallBlock(id="valid", name="sample", arguments={"name": "value"})
    )
    invalid = registry.execute(
        ToolCallBlock(id="invalid", name="sample", arguments={"name": ""})
    )

    assert valid.error_code is None
    assert received == [{"name": "value", "limit": 3}]
    assert invalid.error_code == "INVALID_ARGUMENTS"


def test_invalid_parsed_arguments_become_recoverable_tool_error(tmp_path) -> None:
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_bad",
            name="read_file",
            arguments={},
            arguments_error="invalid JSON arguments",
        )
    )

    assert result.error_code == "INVALID_ARGUMENTS"
    assert result.metadata == {"tool": "read_file", "tool_call_id": "call_bad"}


def test_async_registry_execution_does_not_block_event_loop() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="slow",
            description="slow",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        ),
        lambda _: (time.sleep(0.1), ToolResult("done", {}))[1],
    )

    async def scenario() -> bool:
        execution = asyncio.create_task(
            registry.execute_async(ToolCallBlock(id="slow", name="slow", arguments={}))
        )
        await asyncio.sleep(0.02)
        responsive = not execution.done()
        await execution
        return responsive

    assert asyncio.run(scenario()) is True


def test_path_schemas_reject_empty_paths_like_local_validation(tmp_path) -> None:
    registry = create_local_tool_registry(tmp_path)

    for name in ("read_file", "write_file", "edit_file"):
        assert registry.lookup(name).parameters["properties"]["path"]["minLength"] == 1
    for name, property_name in (("glob", "path"), ("grep", "path"), ("run_command", "cwd")):
        assert registry.lookup(name).parameters["properties"][property_name]["minLength"] == 1


def test_run_command_timeout_keeps_available_output_and_marks_tool_failure(tmp_path) -> None:
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_timeout",
            name="run_command",
            arguments={"command": "printf begun; sleep 1", "timeout_ms": 30},
        )
    )

    assert result.error_code == "TIMEOUT"
    assert result.metadata["timed_out"] is True
    assert result.metadata["stdout"] == "begun"


def test_run_command_marks_bounded_output_as_truncated(tmp_path) -> None:
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_large_output",
            name="run_command",
            arguments={"command": "yes x | head -c 102401"},
        )
    )

    assert result.error_code is None
    assert result.metadata["stdout_truncated"] is True
    assert result.metadata["stderr_truncated"] is False
    assert len(result.metadata["stdout"].encode("utf-8")) <= 100 * 1024

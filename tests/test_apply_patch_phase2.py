from __future__ import annotations

from pathlib import Path

import pytest

from agent.core.messages import ToolCallBlock
from agent.tools.apply_patch import apply_patch, parse_patch
from agent.tools.filesystem import ToolOperationError, WorkspaceFilesystem
from agent.tools.local import create_local_tool_registry
from agent.tools.types import ToolResult
from agent.runtime.change_tracker import ChangeTracker
from agent.runtime.events import EventBuffer, TurnEventEmitter
from tests.sandbox_support import DeterministicSandboxBackend


def fs(tmp_path: Path) -> WorkspaceFilesystem:
    return WorkspaceFilesystem(tmp_path)


def test_apply_patch_updates_multiple_files_and_reports_ordered_paths(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("before\n", encoding="utf-8")

    result = apply_patch(
        fs(tmp_path),
        """*** Begin Patch
*** Update File: a.txt
@@
-two
+changed
*** Add File: c.txt
+new file
*** Delete File: b.txt
*** End Patch
""",
    )

    assert result.affected_paths == ["a.txt", "c.txt", "b.txt"]
    assert result.added_count == 1
    assert result.updated_count == 1
    assert result.deleted_count == 1
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "one\nchanged\n"
    assert (tmp_path / "c.txt").read_text(encoding="utf-8") == "new file\n"
    assert not (tmp_path / "b.txt").exists()


def test_apply_patch_supports_multiple_update_hunks_deterministically(tmp_path) -> None:
    (tmp_path / "file.txt").write_text("a\nb\nc\nd\n", encoding="utf-8")

    apply_patch(
        fs(tmp_path),
        """*** Begin Patch
*** Update File: file.txt
@@
 a
-b
+B
@@
 c
-d
+D
*** End Patch
""",
    )

    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "a\nB\nc\nD\n"


@pytest.mark.parametrize(
    "patch",
    [
        "",
        "*** Begin Patch\n*** Update File: x\n",
        "*** Begin Patch\n*** Unknown File: x\n*** End Patch\n",
        "*** Begin Patch\n*** Add File: x\nbody\n*** End Patch\n",
        "*** Begin Patch\n*** Delete File: x\n+body\n*** End Patch\n",
        "*** Begin Patch\n*** Update File: x\n@@\ninvalid\n*** End Patch\n",
        "*** Begin Patch\n*** Add File: /absolute\n+x\n*** End Patch\n",
        "*** Begin Patch\n*** Add File: ../escape\n+x\n*** End Patch\n",
    ],
)
def test_apply_patch_rejects_malformed_documents_without_writes(tmp_path, patch) -> None:
    target = tmp_path / "x"
    target.write_text("unchanged\n", encoding="utf-8")

    with pytest.raises(ToolOperationError) as captured:
        apply_patch(fs(tmp_path), patch)

    assert captured.value.code == "PATCH_INVALID"
    assert target.read_text(encoding="utf-8") == "unchanged\n"


def test_apply_patch_hunk_mismatch_is_atomic(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")

    with pytest.raises(ToolOperationError) as captured:
        apply_patch(
            fs(tmp_path),
            """*** Begin Patch
*** Update File: a.txt
@@
-a
+A
*** Update File: b.txt
@@
-missing
+B
*** End Patch
""",
        )

    assert captured.value.code == "PATCH_HUNK_MISMATCH"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "a\n"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "b\n"


def test_apply_patch_rolls_back_committed_paths_after_io_failure(tmp_path, monkeypatch) -> None:
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    filesystem = fs(tmp_path)
    original_write = filesystem.write_text_file
    writes = 0

    def fail_on_second_write(raw_path, content):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise ToolOperationError("IO_ERROR", "injected write failure")
        return original_write(raw_path, content)

    monkeypatch.setattr(filesystem, "write_text_file", fail_on_second_write)
    with pytest.raises(ToolOperationError) as captured:
        apply_patch(
            filesystem,
            """*** Begin Patch
*** Update File: a.txt
@@
-a
+changed
*** Add File: b.txt
+created
*** End Patch
""",
        )

    assert captured.value.code == "IO_ERROR"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "a\n"
    assert not (tmp_path / "b.txt").exists()


@pytest.mark.parametrize("path", ["/absolute.txt", "../escape.txt"])
def test_apply_patch_rejects_unsafe_path(tmp_path, path) -> None:
    with pytest.raises(ToolOperationError) as captured:
        apply_patch(
            fs(tmp_path),
            f"*** Begin Patch\n*** Add File: {path}\n+x\n*** End Patch\n",
        )
    assert captured.value.code == "PATCH_INVALID"


def test_apply_patch_rejects_final_parent_symlinks_and_hardlinks(tmp_path) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    (tmp_path / "linked").symlink_to(real_directory, target_is_directory=True)
    (tmp_path / "real.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "linked-file").symlink_to(tmp_path / "real.txt")
    (tmp_path / "hard-file").hardlink_to(tmp_path / "real.txt")

    for path, operation in (
        (
            "linked/new.txt",
            "*** Add File: linked/new.txt\n+x\n",
        ),
        (
            "linked-file",
            "*** Update File: linked-file\n@@\n-old\n+new\n",
        ),
        (
            "hard-file",
            "*** Update File: hard-file\n@@\n-old\n+new\n",
        ),
    ):
        with pytest.raises(ToolOperationError) as captured:
            apply_patch(fs(tmp_path), f"*** Begin Patch\n{operation}*** End Patch\n")
        assert captured.value.code == "WORKSPACE_LINK"
        assert not (tmp_path / "linked" / "new.txt").exists()
        assert path


def test_registry_exposes_apply_patch_as_a_filesystem_tool(tmp_path) -> None:
    registry = create_local_tool_registry(
        tmp_path,
        sandbox_backend=DeterministicSandboxBackend(),
    )
    definition = registry.lookup("apply_patch")
    assert definition is not None
    assert definition.parameters["additionalProperties"] is False
    result = registry.execute(
        ToolCallBlock(
            id="patch",
            name="apply_patch",
            arguments={
                "patch": "*** Begin Patch\n*** Add File: new.txt\n+hello\n*** End Patch\n"
            },
        )
    )
    assert isinstance(result, ToolResult)
    assert result.error_code is None
    assert result.metadata["affected_paths"] == ["new.txt"]
    assert result.metadata["added_count"] == 1
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello\n"


def test_parser_rejects_duplicate_operations(tmp_path) -> None:
    with pytest.raises(ToolOperationError) as captured:
        parse_patch(
            """*** Begin Patch
*** Add File: same.txt
+one
*** Update File: same.txt
@@
-one
+two
*** End Patch
"""
        )
    assert captured.value.code == "PATCH_INVALID"


def test_change_tracker_tracks_every_apply_patch_path_in_order(tmp_path) -> None:
    (tmp_path / "first.txt").write_text("before\n", encoding="utf-8")
    buffer = EventBuffer(32)
    events = TurnEventEmitter(
        thread_id="thread",
        turn_id="turn",
        buffer=buffer,
        reasoning_visibility="hidden",
    )
    tracker = ChangeTracker(tmp_path, events)
    registry = create_local_tool_registry(
        tmp_path,
        sandbox_backend=DeterministicSandboxBackend(),
    )
    call = ToolCallBlock(
        id="multi",
        name="apply_patch",
        arguments={
            "patch": """*** Begin Patch
*** Update File: first.txt
@@
-before
+after
*** Add File: second.txt
+created
*** End Patch
"""
        },
    )

    assert tracker.before_execution(call) is None
    result = registry.execute(call)
    tracker.after_execution(call, result)

    assert result.error_code is None
    assert [change["path"] for change in tracker.changes()] == [
        "first.txt",
        "second.txt",
    ]
    changed = [event for event in buffer.read().events if event.type == "file_changed"]
    assert [event.payload["path"] for event in changed] == ["first.txt", "second.txt"]


def test_change_tracker_discards_prepared_paths_on_failure_and_cancel(tmp_path) -> None:
    (tmp_path / "file.txt").write_text("old\n", encoding="utf-8")
    buffer = EventBuffer(8)
    events = TurnEventEmitter(
        thread_id="thread",
        turn_id="turn",
        buffer=buffer,
        reasoning_visibility="hidden",
    )
    tracker = ChangeTracker(tmp_path, events)
    call = ToolCallBlock(
        id="failed",
        name="write_file",
        arguments={"path": "file.txt", "content": "new\n"},
    )

    assert tracker.before_execution(call) is None
    tracker.after_execution(call, ToolResult("failed", {}, "IO_ERROR"))
    assert tracker._prepared == {}
    assert tracker.before_execution(call) is None
    tracker.execution_interrupted(call)
    assert tracker._prepared == {}

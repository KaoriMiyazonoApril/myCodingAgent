"""Per-Turn optimistic file versions and original-to-final text diffs."""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from enum import Enum
import hashlib
from pathlib import Path
import subprocess

from agent.core.messages import ToolCallBlock
from agent.tools.types import ToolResult
from agent.tools.apply_patch import parse_patch
from agent.tools.filesystem import ToolOperationError, WorkspaceFilesystem, content_fingerprint

from .events import TurnEventEmitter


_MUTATING_FILE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})


@dataclass(frozen=True, slots=True)
class _FileState:
    kind: _FileKind
    content: bytes | None

    @property
    def fingerprint(self) -> str:
        suffix = "" if self.content is None else content_fingerprint(self.content)
        return f"{self.kind.value}:{suffix}"


class _FileKind(str, Enum):
    MISSING = "missing"
    OTHER = "other"
    FILE = "file"
    UNREADABLE = "unreadable"


class ChangeTracker:
    """Track file-tool changes completely and command changes conservatively."""

    def __init__(
        self,
        workspace: Path,
        events: TurnEventEmitter,
        filesystem: WorkspaceFilesystem | None = None,
    ) -> None:
        self._workspace = workspace
        self._filesystem = filesystem or WorkspaceFilesystem(workspace)
        self._events = events
        self._known: dict[str, str] = {}
        self._originals: dict[str, _FileState] = {}
        self._finals: dict[str, _FileState] = {}
        self._prepared: dict[str, tuple[str, ...]] = {}
        self._git_before_calls: dict[str, dict[str, tuple[str, str]] | None] = {}
        self._git_before_sessions: dict[str, dict[str, tuple[str, str]] | None] = {}
        self._command_changes: dict[str, dict[str, str]] = {}
        self._command_order: list[str] = []
        self._order: list[str] = []
        self.diff_complete = True

    def before_execution(self, call: ToolCallBlock) -> ToolResult | None:
        if call.name == "exec_command":
            self._git_before_calls[call.id] = self._git_status()
            return None
        if call.name == "write_stdin":
            return None
        if call.name not in _MUTATING_FILE_TOOLS:
            return None
        paths = self._resolve_call_paths(call)
        if paths is None:
            return None
        current_states = [(path, relative, self._snapshot(path)) for path, relative in paths]
        for _, relative, current in current_states:
            known = self._known.get(relative)
            if known is not None and known != current.fingerprint:
                return ToolResult(
                    content=(
                        f"file changed since it was last read: {relative}; "
                        "read it again before writing"
                    ),
                    metadata={
                        "path": relative,
                        "paths": [item[1] for item in current_states],
                        "executed": False,
                    },
                    error_code="FILE_CHANGED",
                )
        for _, relative, current in current_states:
            if relative not in self._originals:
                self._originals[relative] = current
                self._order.append(relative)
        self._prepared[call.id] = tuple(relative for _, relative, _ in current_states)
        return None

    def after_execution(self, call: ToolCallBlock, result: ToolResult) -> None:
        if call.name in {"exec_command", "write_stdin"}:
            self._after_command(call, result)
            return
        if call.name == "run_command":
            if "duration_ms" in result.metadata:
                self.diff_complete = False
            return
        if result.error_code is not None:
            self._prepared.pop(call.id, None)
            return
        if call.name == "read_file":
            self._record_read(result)
            return
        if call.name not in _MUTATING_FILE_TOOLS:
            return
        relatives = self._prepared.pop(call.id, None)
        if relatives is None:
            return
        for relative in relatives:
            previous = self._finals.get(relative, self._originals[relative])
            final = self._snapshot(self._workspace / relative)
            self._known[relative] = final.fingerprint
            self._finals[relative] = final
            change = self._diff_record(relative)
            if change is not None:
                self._events.emit("file_changed", change)
            elif previous != final:
                self._events.emit(
                    "file_changed",
                    {"path": relative, "change_type": "reverted", "diff": ""},
                )

    def execution_interrupted(self, call: ToolCallBlock) -> None:
        self._prepared.pop(call.id, None)
        if call.name in {"run_command", "exec_command", "write_stdin"}:
            self.diff_complete = False

    def changes(self) -> list[dict[str, str]]:
        file_changes = [
            change
            for relative in self._order
            if relative in self._finals
            if (change := self._diff_record(relative)) is not None
        ]
        return file_changes + [self._command_changes[path] for path in self._command_order]

    def _after_command(self, call: ToolCallBlock, result: ToolResult) -> None:
        session_id = result.metadata.get("session_id")
        if not isinstance(session_id, str):
            if call.name == "exec_command":
                self.diff_complete = False
                self._git_before_calls.pop(call.id, None)
            return
        if call.name == "exec_command":
            before = self._git_before_calls.pop(call.id, None)
            if result.metadata.get("status") == "running":
                self._git_before_sessions[session_id] = before
                self.diff_complete = False
                return
        else:
            before = self._git_before_sessions.get(session_id)
            if result.metadata.get("status") == "running":
                self.diff_complete = False
                return
            self._git_before_sessions.pop(session_id, None)
        if before is None:
            self.diff_complete = False
            return
        after = self._git_status()
        if after is None:
            self.diff_complete = False
            return
        self._record_git_changes(before, after)
        if (
            result.error_code is not None
            or result.metadata.get("timed_out")
            or result.metadata.get("idle_timed_out")
        ):
            self.diff_complete = False

    def _record_git_changes(
        self,
        before: dict[str, tuple[str, str]],
        after: dict[str, tuple[str, str]],
    ) -> None:
        for relative in sorted(set(before) | set(after)):
            if before.get(relative) == after.get(relative):
                continue
            if relative in self._originals:
                continue
            status_entry = after.get(relative) or before.get(relative)
            status = None if status_entry is None else status_entry[0]
            change = self._git_change(relative, status)
            if change is None:
                self.diff_complete = False
                continue
            if relative not in self._command_changes:
                self._command_order.append(relative)
            self._command_changes[relative] = change

    def _git_status(self) -> dict[str, tuple[str, str]] | None:
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self._workspace),
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                ],
                cwd=self._workspace,
                capture_output=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        statuses: dict[str, tuple[str, str]] = {}
        for record in completed.stdout.decode("utf-8", errors="replace").split("\0"):
            if len(record) < 4:
                continue
            state = record[:2]
            relative = record[3:]
            if " -> " in relative:
                relative = relative.rsplit(" -> ", 1)[-1]
            diff = self._git_diff_text(relative, state)
            statuses[relative] = (
                state,
                "unavailable"
                if diff is None
                else hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            )
        return statuses

    def _git_change(self, relative: str, status: str | None) -> dict[str, str] | None:
        diff = self._git_diff_text(relative, status)
        if not diff:
            return None
        kind = "added" if status and "?" in status else "deleted" if status and "D" in status else "modified"
        return {"path": relative, "change_type": kind, "diff": diff}

    def _git_diff_text(self, relative: str, status: str | None) -> str | None:
        diff = ""
        for command in (
            ["git", "-C", str(self._workspace), "diff", "--no-ext-diff", "--no-color", "--", relative],
            ["git", "-C", str(self._workspace), "diff", "--cached", "--no-ext-diff", "--no-color", "--", relative],
        ):
            try:
                completed = subprocess.run(
                    command,
                    cwd=self._workspace,
                    capture_output=True,
                    timeout=2,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if completed.returncode not in {0, 1}:
                return None
            diff = completed.stdout.decode("utf-8", errors="replace")
            if diff:
                break
        if not diff and status and "?" in status:
            try:
                completed = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self._workspace),
                        "diff",
                        "--no-index",
                        "--no-color",
                        "/dev/null",
                        relative,
                    ],
                    cwd=self._workspace,
                    capture_output=True,
                    timeout=2,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if completed.returncode not in {0, 1}:
                return None
            diff = completed.stdout.decode("utf-8", errors="replace")
        return diff or None

    def _record_read(self, result: ToolResult) -> None:
        relative = result.metadata.get("path")
        fingerprint = result.metadata.get("content_fingerprint")
        if not isinstance(relative, str) or not isinstance(fingerprint, str):
            return
        self._known[relative] = f"{_FileKind.FILE.value}:{fingerprint}"

    def _resolve_call_paths(
        self,
        call: ToolCallBlock,
    ) -> list[tuple[Path, str]] | None:
        if call.arguments is None:
            return None
        if call.name == "apply_patch":
            try:
                paths = parse_patch(call.arguments.get("patch")).affected_paths
            except ToolOperationError:
                # The registry owns the structured parser error. Do not make
                # an invalid patch look like a tracker conflict.
                return None
            resolved: list[tuple[Path, str]] = []
            for raw_path in paths:
                try:
                    resolved.append(self._filesystem.resolve(raw_path))
                except ToolOperationError:
                    return None
            return resolved
        raw_path = call.arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        try:
            return [self._filesystem.resolve(raw_path)]
        except ToolOperationError:
            return None

    def _resolve_call_path(
        self,
        call: ToolCallBlock,
    ) -> tuple[Path, str] | None:
        """Compatibility helper for callers that track one file call."""

        paths = self._resolve_call_paths(call)
        return None if not paths else paths[0]

    @staticmethod
    def _snapshot(path: Path) -> _FileState:
        try:
            if not path.exists():
                return _FileState(_FileKind.MISSING, None)
            if not path.is_file():
                return _FileState(_FileKind.OTHER, None)
            return _FileState(_FileKind.FILE, path.read_bytes())
        except OSError:
            return _FileState(_FileKind.UNREADABLE, None)

    def _diff_record(self, relative: str) -> dict[str, str] | None:
        original = self._originals[relative]
        final = self._finals[relative]
        if original == final:
            return None
        before = self._text_lines(original)
        after = self._text_lines(final)
        fromfile = (
            "/dev/null" if original.kind is _FileKind.MISSING else f"a/{relative}"
        )
        tofile = "/dev/null" if final.kind is _FileKind.MISSING else f"b/{relative}"
        unified = "".join(
            difflib.unified_diff(
                before,
                after,
                fromfile=fromfile,
                tofile=tofile,
            )
        )
        if original.kind is _FileKind.MISSING:
            change_type = "added"
        elif final.kind is _FileKind.MISSING:
            change_type = "deleted"
        else:
            change_type = "modified"
        return {
            "path": relative,
            "change_type": change_type,
            "diff": unified,
        }

    @staticmethod
    def _text_lines(state: _FileState) -> list[str]:
        if state.content is None:
            return []
        return state.content.decode("utf-8", errors="replace").splitlines(
            keepends=True
        )

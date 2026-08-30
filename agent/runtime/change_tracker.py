"""Per-Turn optimistic file versions and original-to-final text diffs."""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from enum import Enum
from pathlib import Path

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
        self._order: list[str] = []
        self.diff_complete = True

    def before_execution(self, call: ToolCallBlock) -> ToolResult | None:
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
        if call.name == "run_command":
            if "duration_ms" in result.metadata:
                self.diff_complete = False
            return
        if result.error_code is not None:
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
        if call.name == "run_command":
            self.diff_complete = False

    def changes(self) -> list[dict[str, str]]:
        return [
            change
            for relative in self._order
            if relative in self._finals
            if (change := self._diff_record(relative)) is not None
        ]

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

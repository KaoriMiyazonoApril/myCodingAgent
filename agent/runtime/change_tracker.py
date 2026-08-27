"""Per-Turn optimistic file versions and original-to-final text diffs."""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from enum import Enum
from pathlib import Path

from agent.core.messages import ToolCallBlock
from agent.tools.types import ToolResult
from agent.tools.filesystem import content_fingerprint

from .events import TurnEventEmitter


_MUTATING_FILE_TOOLS = frozenset({"write_file", "edit_file"})


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

    def __init__(self, workspace: Path, events: TurnEventEmitter) -> None:
        self._workspace = workspace
        self._events = events
        self._known: dict[str, str] = {}
        self._originals: dict[str, _FileState] = {}
        self._finals: dict[str, _FileState] = {}
        self._prepared: dict[str, str] = {}
        self._order: list[str] = []
        self.diff_complete = True

    def before_execution(self, call: ToolCallBlock) -> ToolResult | None:
        if call.name not in _MUTATING_FILE_TOOLS:
            return None
        resolved = self._resolve_call_path(call)
        if resolved is None:
            return None
        path, relative = resolved
        current = self._snapshot(path)
        known = self._known.get(relative)
        if known is not None and known != current.fingerprint:
            return ToolResult(
                content=(
                    f"file changed since it was last read: {relative}; "
                    "read it again before writing"
                ),
                metadata={"path": relative, "executed": False},
                error_code="FILE_CHANGED",
            )
        if relative not in self._originals:
            self._originals[relative] = current
            self._order.append(relative)
        self._prepared[call.id] = relative
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
        relative = self._prepared.pop(call.id, None)
        if relative is None:
            return
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

    def _resolve_call_path(
        self,
        call: ToolCallBlock,
    ) -> tuple[Path, str] | None:
        if call.arguments is None:
            return None
        raw_path = call.arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        candidate = Path(raw_path)
        if candidate.is_absolute():
            return None
        try:
            target = (self._workspace / candidate).resolve()
            relative = target.relative_to(self._workspace).as_posix()
        except (OSError, RuntimeError, ValueError):
            return None
        return target, relative

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

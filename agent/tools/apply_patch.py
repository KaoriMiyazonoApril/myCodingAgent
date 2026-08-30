"""Structured, workspace-confined ``apply_patch`` implementation.

The parser accepts the small patch envelope documented by the project.  It is
intentionally line-oriented rather than a shell/patch command wrapper, so all
operations can be parsed, validated and prepared before the first write.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Literal

from .filesystem import FileSnapshot, ToolOperationError, WorkspaceFilesystem


PatchOperationKind = Literal["add", "update", "delete"]


@dataclass(frozen=True, slots=True)
class PatchHunkLine:
    kind: Literal["context", "remove", "add"]
    text: str


@dataclass(frozen=True, slots=True)
class PatchHunk:
    lines: tuple[PatchHunkLine, ...]
    old_start: int | None = None

    @property
    def pattern(self) -> tuple[str, ...]:
        return tuple(
            line.text for line in self.lines if line.kind in {"context", "remove"}
        )

    @property
    def replacement(self) -> tuple[str, ...]:
        return tuple(
            line.text for line in self.lines if line.kind in {"context", "add"}
        )


@dataclass(frozen=True, slots=True)
class PatchOperation:
    kind: PatchOperationKind
    path: str
    body: tuple[str, ...] = ()
    hunks: tuple[PatchHunk, ...] = ()


@dataclass(frozen=True, slots=True)
class PatchDocument:
    operations: tuple[PatchOperation, ...]

    @property
    def affected_paths(self) -> list[str]:
        return [operation.path for operation in self.operations]


@dataclass(frozen=True, slots=True)
class PatchResult:
    affected_paths: list[str]
    added_count: int
    updated_count: int
    deleted_count: int

    @property
    def added(self) -> int:
        return self.added_count

    @property
    def updated(self) -> int:
        return self.updated_count

    @property
    def deleted(self) -> int:
        return self.deleted_count


_SECTION_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: ?(.*)$")
_HUNK_RE = re.compile(
    r"^@@(?: -(\d+)(?:,\d+)?(?: \+(\d+)(?:,\d+)?)?(?: .*)?@@)?$"
)
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def parse_patch(patch: object) -> PatchDocument:
    """Parse and validate a complete patch before any filesystem access."""

    if not isinstance(patch, str) or not patch:
        raise _invalid("patch must be a non-empty string")
    lines = patch.splitlines()
    if len(lines) < 2 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise _invalid("patch must contain Begin Patch and End Patch markers")

    operations: list[PatchOperation] = []
    index = 1
    seen_paths: set[str] = set()
    end = len(lines) - 1
    while index < end:
        match = _SECTION_RE.match(lines[index])
        if match is None:
            raise _invalid("patch contains an unknown section")
        kind = match.group(1).lower()
        path = _validate_patch_path(match.group(2))
        if path in seen_paths:
            raise _invalid(f"patch contains duplicate operation for {path}")
        seen_paths.add(path)
        index += 1

        if kind == "add":
            body: list[str] = []
            while index < end and _SECTION_RE.match(lines[index]) is None:
                if lines[index].startswith("*** "):
                    raise _invalid("patch contains an unknown section")
                if not lines[index].startswith("+"):
                    raise _invalid("Add File body lines must start with '+'")
                body.append(lines[index][1:])
                index += 1
            operations.append(PatchOperation(kind="add", path=path, body=tuple(body)))
            continue

        if kind == "delete":
            if index < end and _SECTION_RE.match(lines[index]) is None:
                raise _invalid("Delete File must not contain a mutation body")
            operations.append(PatchOperation(kind="delete", path=path))
            continue

        hunks: list[PatchHunk] = []
        while index < end and _SECTION_RE.match(lines[index]) is None:
            hunk_match = _HUNK_RE.match(lines[index])
            if hunk_match is None:
                raise _invalid("Update File body must contain valid @@ hunks")
            old_start = int(hunk_match.group(1)) if hunk_match.group(1) else None
            index += 1
            hunk_lines: list[PatchHunkLine] = []
            while index < end:
                if _SECTION_RE.match(lines[index]) is not None or _HUNK_RE.match(lines[index]):
                    break
                line = lines[index]
                if not line:
                    raise _invalid("hunk lines must use context, '-' or '+' prefixes")
                prefix = line[0]
                if prefix == " ":
                    hunk_lines.append(PatchHunkLine("context", line[1:]))
                elif prefix == "-":
                    hunk_lines.append(PatchHunkLine("remove", line[1:]))
                elif prefix == "+":
                    hunk_lines.append(PatchHunkLine("add", line[1:]))
                else:
                    raise _invalid("hunk lines must use context, '-' or '+' prefixes")
                index += 1
            if not hunk_lines or not any(line.kind != "context" for line in hunk_lines):
                raise _invalid("each hunk must contain a mutation")
            hunks.append(PatchHunk(tuple(hunk_lines), old_start=old_start))
        if not hunks:
            raise _invalid("Update File must contain at least one hunk")
        operations.append(PatchOperation(kind="update", path=path, hunks=tuple(hunks)))

    if not operations:
        raise _invalid("patch must contain at least one operation")
    return PatchDocument(tuple(operations))


def apply_patch(filesystem: WorkspaceFilesystem, patch: object) -> PatchResult:
    """Apply all operations or leave the workspace unchanged.

    Parsing, path validation and content preparation happen before commit.  If
    an I/O failure occurs during commit, every already committed path is restored
    from its pre-commit snapshot.
    """

    document = parse_patch(patch)
    prepared: list[tuple[PatchOperation, Path, str, FileSnapshot, str | None]] = []
    for operation in document.operations:
        target, relative = _resolve_operation_path(filesystem, operation.path)
        before = filesystem.snapshot(operation.path)
        if operation.kind == "add":
            if before.exists:
                raise ToolOperationError("PATCH_TARGET_EXISTS", f"target already exists: {relative}")
            final = "\n".join(operation.body)
            if operation.body:
                final += "\n"
            _validate_text(final, relative)
        elif operation.kind == "update":
            if not before.exists:
                raise ToolOperationError("NOT_FOUND", f"file not found: {relative}")
            content, _ = filesystem.read_text_file(operation.path)
            final = _apply_hunks(content, operation.hunks, relative)
            _validate_text(final, relative)
        else:
            if not before.exists:
                raise ToolOperationError("NOT_FOUND", f"file not found: {relative}")
            filesystem.read_text_file(operation.path)
            final = None
        prepared.append((operation, target, relative, before, final))

    committed: list[tuple[Path, str, FileSnapshot]] = []
    try:
        for operation, target, relative, before, final in prepared:
            if operation.kind == "delete":
                filesystem.remove_file(operation.path)
            else:
                assert final is not None
                filesystem.write_text_file(operation.path, final)
            committed.append((target, relative, before))
    except Exception as error:
        rollback_error: Exception | None = None
        for target, relative, before in reversed(committed):
            try:
                filesystem.restore_snapshot(relative, before)
            except Exception as restore_error:  # pragma: no cover - catastrophic I/O
                rollback_error = restore_error
        if rollback_error is not None:
            raise ToolOperationError(
                "IO_ERROR",
                "patch commit failed and workspace rollback was incomplete",
            ) from rollback_error
        if isinstance(error, ToolOperationError):
            raise
        raise ToolOperationError("IO_ERROR", "patch commit failed; changes were rolled back") from error

    return PatchResult(
        affected_paths=document.affected_paths,
        added_count=sum(operation.kind == "add" for operation in document.operations),
        updated_count=sum(operation.kind == "update" for operation in document.operations),
        deleted_count=sum(operation.kind == "delete" for operation in document.operations),
    )


def _resolve_operation_path(
    filesystem: WorkspaceFilesystem,
    path: str,
) -> tuple[Path, str]:
    try:
        return filesystem.resolve(path)
    except ToolOperationError:
        raise
    except Exception as error:  # pragma: no cover - defensive adapter boundary
        raise ToolOperationError("IO_ERROR", "could not resolve patch path") from error


def _validate_patch_path(raw_path: str) -> str:
    path = raw_path.strip()
    if not path or path in {".", ".."}:
        raise _invalid("patch path must be non-empty")
    if path.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_RE.match(path):
        raise _invalid("patch paths must be relative")
    normalized = PurePosixPath(path.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise _invalid("patch path must not escape the workspace")
    if any(part in {"", "."} for part in normalized.parts):
        # PurePosixPath removes '.' components; reject them only when they are
        # explicit so the model cannot smuggle ambiguous spelling.
        if "/./" in path or path.startswith("./") or path.endswith("/."):
            raise _invalid("patch path contains an ambiguous '.' component")
    return normalized.as_posix()


def _apply_hunks(content: str, hunks: tuple[PatchHunk, ...], relative: str) -> str:
    had_final_newline = content.endswith(("\n", "\r"))
    lines = content.splitlines()
    line_offset = 0
    for hunk in hunks:
        pattern = list(hunk.pattern)
        replacement = list(hunk.replacement)
        candidates: list[int] = []
        if hunk.old_start is not None:
            candidate = max(hunk.old_start - 1 + line_offset, 0)
            if _matches(lines, candidate, pattern):
                candidates = [candidate]
        elif not pattern:
            candidates = [0]
        else:
            for position in range(0, len(lines) - len(pattern) + 1):
                if _matches(lines, position, pattern):
                    candidates.append(position)
        if not candidates or len(candidates) != 1:
            raise ToolOperationError(
                "PATCH_HUNK_MISMATCH",
                f"patch hunk did not match exactly: {relative}",
            )
        position = candidates[0]
        lines[position : position + len(pattern)] = replacement
        if hunk.old_start is not None:
            line_offset += len(replacement) - len(pattern)
    result = "\n".join(lines)
    if had_final_newline and lines:
        result += "\n"
    return result


def _matches(lines: list[str], position: int, pattern: list[str]) -> bool:
    return position >= 0 and lines[position : position + len(pattern)] == pattern


def _validate_text(content: str, relative: str) -> None:
    if "\0" in content:
        raise ToolOperationError("NOT_TEXT", f"patch content contains NUL bytes: {relative}")
    try:
        content.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ToolOperationError("NOT_TEXT", f"patch content is not valid UTF-8: {relative}") from error


def _invalid(message: str) -> ToolOperationError:
    return ToolOperationError("PATCH_INVALID", message)

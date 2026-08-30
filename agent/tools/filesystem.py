"""Workspace-confined filesystem operations shared by local tools."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import Iterator


DEFAULT_IGNORED_DIRECTORIES = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist"}
)
MAX_TEXT_FILE_BYTES = 10 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
_LINE_SEPARATORS = frozenset(
    {
        "\n",
        "\r",
        "\v",
        "\f",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x85",
        "\u2028",
        "\u2029",
    }
)


def content_fingerprint(content: bytes) -> str:
    """Return the stable version identifier used for optimistic file writes."""

    return hashlib.sha256(content).hexdigest()


def existing_path_components(path: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Inspect each existing absolute path component without following links."""

    absolute = Path(path).absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            metadata = os.stat(current, follow_symlinks=False)
        except FileNotFoundError:
            return
        yield current, metadata


class ToolOperationError(Exception):
    """An expected local-tool failure with a stable, model-visible code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """Recoverable state for one regular workspace file."""

    exists: bool
    content: bytes | None
    mode: int | None = None


class WorkspaceFilesystem:
    """Resolve and validate paths below one configured workspace root."""

    def __init__(self, workspace_root: Path) -> None:
        candidate = Path(workspace_root).absolute()
        try:
            for component, metadata in existing_path_components(candidate):
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError(
                        "workspace_root path components must not be symbolic links: "
                        f"{component.as_posix()}"
                    )
            metadata = os.stat(candidate, follow_symlinks=False)
        except OSError as error:
            raise ValueError("workspace_root must be an existing directory") from error
        self.root = candidate.resolve(strict=True)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("workspace_root must be an existing directory")

    def resolve(self, raw_path: object) -> tuple[Path, str]:
        if not isinstance(raw_path, str) or not raw_path:
            raise ToolOperationError("INVALID_ARGUMENTS", "path must be a non-empty string")

        candidate = Path(raw_path)
        if candidate.is_absolute():
            raise ToolOperationError("WORKSPACE_ESCAPE", "absolute paths are not allowed")
        if ".." in candidate.parts:
            raise ToolOperationError("WORKSPACE_ESCAPE", "path escapes the workspace")

        relative = Path(*[part for part in candidate.parts if part not in {"", "."}])
        target = self.root / relative
        try:
            components = existing_path_components(target)
            for component, metadata in components:
                if stat.S_ISLNK(metadata.st_mode):
                    raise ToolOperationError(
                        "WORKSPACE_LINK", "workspace symbolic links are forbidden"
                    )
                if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                    raise ToolOperationError(
                        "WORKSPACE_LINK", "workspace hard links are forbidden"
                    )
        except ToolOperationError:
            raise
        except OSError as error:
            raise ToolOperationError("IO_ERROR", "could not inspect path") from error
        return target, relative.as_posix()

    def _resolve_text_file(
        self, raw_path: object, *, max_bytes: int | None = None
    ) -> tuple[Path, str]:
        target, relative = self.resolve(raw_path)
        if not target.exists():
            raise ToolOperationError("NOT_FOUND", f"file not found: {relative}")
        if not target.is_file():
            raise ToolOperationError("NOT_A_FILE", f"not a regular file: {relative}")
        byte_limit = MAX_TEXT_FILE_BYTES if max_bytes is None else max_bytes
        try:
            size = target.stat().st_size
        except OSError as error:
            raise ToolOperationError("IO_ERROR", f"could not inspect file: {relative}") from error
        if size > byte_limit:
            raise ToolOperationError(
                "FILE_TOO_LARGE",
                f"file exceeds the {byte_limit}-byte resource limit: {relative}",
            )
        return target, relative

    def read_text_file(
        self, raw_path: object, *, max_bytes: int | None = None
    ) -> tuple[str, str]:
        target, relative = self._resolve_text_file(raw_path, max_bytes=max_bytes)
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ToolOperationError("NOT_TEXT", f"file is not valid UTF-8: {relative}") from error
        except OSError as error:
            raise ToolOperationError("IO_ERROR", f"could not read file: {relative}") from error
        if "\0" in content:
            raise ToolOperationError("NOT_TEXT", f"file contains NUL bytes: {relative}")
        return content, relative

    def read_text_page(
        self, raw_path: object, *, offset: int, limit: int
    ) -> tuple[list[str], int, str, str]:
        """Stream one bounded text version and return its exact fingerprint."""

        byte_limit = MAX_TEXT_FILE_BYTES
        target, relative = self._resolve_text_file(raw_path, max_bytes=byte_limit)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        digest = hashlib.sha256()
        page: list[str] = []
        line_parts: list[str] = []
        total_lines = 0
        current_line_has_content = False
        skip_lf_after_cr = False

        def retain_line() -> None:
            nonlocal current_line_has_content, total_lines
            total_lines += 1
            if offset <= total_lines < offset + limit:
                page.append("".join(line_parts))
            line_parts.clear()
            current_line_has_content = False

        def consume(decoded: str) -> None:
            nonlocal current_line_has_content, skip_lf_after_cr
            start = 0
            index = 0
            if skip_lf_after_cr and decoded:
                if decoded[0] == "\n":
                    index = 1
                    start = 1
                skip_lf_after_cr = False
            while index < len(decoded):
                character = decoded[index]
                if character not in _LINE_SEPARATORS:
                    index += 1
                    continue
                if index > start:
                    current_line_has_content = True
                    if offset <= total_lines + 1 < offset + limit:
                        line_parts.append(decoded[start:index])
                retain_line()
                if character == "\r":
                    if index + 1 < len(decoded) and decoded[index + 1] == "\n":
                        index += 1
                    elif index + 1 == len(decoded):
                        skip_lf_after_cr = True
                index += 1
                start = index
            if start < len(decoded):
                current_line_has_content = True
                if offset <= total_lines + 1 < offset + limit:
                    line_parts.append(decoded[start:])

        try:
            bytes_read = 0
            with target.open("rb") as source:
                while chunk := source.read(READ_CHUNK_BYTES):
                    bytes_read += len(chunk)
                    if bytes_read > byte_limit:
                        raise ToolOperationError(
                            "FILE_TOO_LARGE",
                            f"file exceeds the {byte_limit}-byte resource limit: {relative}",
                        )
                    if b"\0" in chunk:
                        raise ToolOperationError(
                            "NOT_TEXT", f"file contains NUL bytes: {relative}"
                        )
                    digest.update(chunk)
                    consume(decoder.decode(chunk))
                consume(decoder.decode(b"", final=True))
        except UnicodeDecodeError as error:
            raise ToolOperationError(
                "NOT_TEXT", f"file is not valid UTF-8: {relative}"
            ) from error
        except ToolOperationError:
            raise
        except OSError as error:
            raise ToolOperationError("IO_ERROR", f"could not read file: {relative}") from error
        if current_line_has_content:
            retain_line()
        return (
            page,
            total_lines,
            relative,
            digest.hexdigest(),
        )

    def write_text_file(self, raw_path: object, content: object) -> tuple[str, int]:
        if not isinstance(content, str):
            raise ToolOperationError("INVALID_ARGUMENTS", "content must be a string")
        if "\0" in content:
            raise ToolOperationError("NOT_TEXT", "new file content must not contain NUL bytes")
        content_bytes = len(content.encode("utf-8"))

        target, relative = self.resolve(raw_path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ToolOperationError("IO_ERROR", f"could not create parent directory: {relative}") from error
        if target.exists() and not target.is_file():
            raise ToolOperationError("NOT_A_FILE", f"not a regular file: {relative}")

        existing_mode: int | None = None
        if target.exists():
            self.read_text_file(raw_path)
            existing_mode = stat.S_IMODE(target.stat().st_mode)

        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target.parent, delete=False
            ) as temporary:
                temporary.write(content)
                temporary_path = Path(temporary.name)
            if existing_mode is not None:
                os.chmod(temporary_path, existing_mode)
            os.replace(temporary_path, target)
        except OSError as error:
            if "temporary_path" in locals():
                temporary_path.unlink(missing_ok=True)
            raise ToolOperationError("IO_ERROR", f"could not write file: {relative}") from error
        return relative, content_bytes

    def snapshot(self, raw_path: object) -> FileSnapshot:
        """Capture one path without following a final symbolic link."""

        target, relative = self.resolve(raw_path)
        try:
            metadata = os.stat(target, follow_symlinks=False)
        except FileNotFoundError:
            return FileSnapshot(exists=False, content=None)
        except OSError as error:
            raise ToolOperationError("IO_ERROR", f"could not inspect file: {relative}") from error
        if not stat.S_ISREG(metadata.st_mode):
            return FileSnapshot(exists=True, content=None, mode=stat.S_IMODE(metadata.st_mode))
        if metadata.st_nlink > 1:
            raise ToolOperationError("WORKSPACE_LINK", "workspace hard links are forbidden")
        if metadata.st_size > MAX_TEXT_FILE_BYTES:
            raise ToolOperationError(
                "FILE_TOO_LARGE",
                f"file exceeds the {MAX_TEXT_FILE_BYTES}-byte resource limit: {relative}",
            )
        try:
            content = target.read_bytes()
        except OSError as error:
            raise ToolOperationError("IO_ERROR", f"could not read file: {relative}") from error
        return FileSnapshot(
            exists=True,
            content=content,
            mode=stat.S_IMODE(metadata.st_mode),
        )

    def remove_file(self, raw_path: object) -> str:
        """Unlink one regular workspace file without following links."""

        target, relative = self.resolve(raw_path)
        try:
            metadata = os.stat(target, follow_symlinks=False)
        except FileNotFoundError as error:
            raise ToolOperationError("NOT_FOUND", f"file not found: {relative}") from error
        except OSError as error:
            raise ToolOperationError("IO_ERROR", f"could not inspect file: {relative}") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise ToolOperationError("NOT_A_FILE", f"not a regular file: {relative}")
        try:
            target.unlink()
        except FileNotFoundError as error:
            raise ToolOperationError("NOT_FOUND", f"file not found: {relative}") from error
        except OSError as error:
            raise ToolOperationError("IO_ERROR", f"could not delete file: {relative}") from error
        return relative

    def restore_snapshot(self, raw_path: object, snapshot: FileSnapshot) -> str:
        """Restore a snapshot using an internal atomic write path.

        This intentionally does not call ``write_text_file``: rollback must
        remain available when a public commit operation has been injected to
        fail.
        """

        target, relative = self.resolve(raw_path)
        if not snapshot.exists:
            try:
                metadata = os.stat(target, follow_symlinks=False)
            except FileNotFoundError:
                return relative
            except OSError as error:
                raise ToolOperationError("IO_ERROR", f"could not inspect file: {relative}") from error
            if not stat.S_ISREG(metadata.st_mode):
                raise ToolOperationError("NOT_A_FILE", f"not a regular file: {relative}")
            try:
                target.unlink()
            except OSError as error:
                raise ToolOperationError("IO_ERROR", f"could not restore file: {relative}") from error
            return relative

        if snapshot.content is None:
            raise ToolOperationError("IO_ERROR", f"snapshot is not a regular file: {relative}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, delete=False) as temporary:
                temporary.write(snapshot.content)
                temporary_path = Path(temporary.name)
            if snapshot.mode is not None:
                os.chmod(temporary_path, snapshot.mode)
            os.replace(temporary_path, target)
        except OSError as error:
            if "temporary_path" in locals():
                temporary_path.unlink(missing_ok=True)
            raise ToolOperationError("IO_ERROR", f"could not restore file: {relative}") from error
        return relative

    def regular_files(self, raw_path: object = ".") -> Iterator[tuple[Path, str]]:
        """Yield regular files beneath a selected workspace directory in stable order."""

        selected, relative = self.resolve(raw_path)
        if not selected.exists():
            raise ToolOperationError("NOT_FOUND", f"path not found: {relative}")
        if not selected.is_dir():
            raise ToolOperationError("NOT_A_FILE", f"not a directory: {relative}")

        def walk(directory: Path) -> Iterator[tuple[Path, str]]:
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError as error:
                raise ToolOperationError(
                    "IO_ERROR", f"could not traverse directory: {relative}"
                ) from error
            for entry in entries:
                try:
                    metadata = entry.stat(follow_symlinks=False)
                    candidate = Path(entry.path)
                    if stat.S_ISLNK(metadata.st_mode):
                        raise ToolOperationError(
                            "WORKSPACE_LINK",
                            f"workspace symbolic link is forbidden: {candidate.name}",
                        )
                    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                        raise ToolOperationError(
                            "WORKSPACE_LINK",
                            f"workspace hard link is forbidden: {candidate.name}",
                        )
                    if stat.S_ISDIR(metadata.st_mode):
                        if entry.name not in DEFAULT_IGNORED_DIRECTORIES:
                            yield from walk(candidate)
                        continue
                except ToolOperationError:
                    raise
                except OSError as error:
                    raise ToolOperationError(
                        "IO_ERROR", f"could not inspect workspace entry: {entry.name}"
                    ) from error
                if stat.S_ISREG(metadata.st_mode):
                    workspace_relative = candidate.relative_to(self.root).as_posix()
                    yield candidate, workspace_relative

        yield from walk(selected)

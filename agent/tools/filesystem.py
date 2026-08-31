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


def truncate_utf8(text: str, max_bytes: int, *, marker: str = "\n... output truncated ...") -> tuple[str, bool]:
    """Return text bounded by UTF-8 bytes without splitting a code point."""

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= max_bytes:
        return marker_bytes[:max_bytes].decode("utf-8", errors="ignore"), True
    prefix = encoded[: max_bytes - len(marker_bytes)]
    prefix = prefix.decode("utf-8", errors="ignore").encode("utf-8")
    return (prefix + marker_bytes).decode("utf-8"), True


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


@dataclass(frozen=True, slots=True)
class BoundedTextPage:
    """One line page whose serialized text is bounded in UTF-8 bytes."""

    content: str
    total_lines: int
    relative: str
    fingerprint: str
    returned_lines: int
    start_line: int | None
    end_line: int | None
    truncated: bool
    line_truncated: bool
    returned_bytes: int
    original_selected_bytes: int


class WorkspaceFilesystem:
    """Resolve and validate paths below one configured workspace root."""

    def __init__(self, workspace_root: Path) -> None:
        candidate = Path(workspace_root).expanduser()
        try:
            canonical = candidate.resolve(strict=True)
            metadata = os.stat(canonical, follow_symlinks=True)
        except (OSError, RuntimeError) as error:
            raise ValueError("workspace_root must be an existing directory") from error
        self.root = canonical
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

        parts = [part for part in candidate.parts if part not in {"", "."}]
        relative = Path(*parts) if parts else Path(".")
        target = self.root / relative
        try:
            # ``strict=False`` resolves all existing symlink components and
            # the nearest existing parent for a new target. The effective
            # target, rather than its lexical spelling, is the security seam.
            effective = target.resolve(strict=False)
            effective.relative_to(self.root)
        except ValueError as error:
            if "embedded null" in str(error).lower():
                raise ToolOperationError("INVALID_ARGUMENTS", "path is invalid") from error
            raise ToolOperationError(
                "WORKSPACE_ESCAPE", "path resolves outside the workspace"
            ) from error
        except (OSError, RuntimeError) as error:
            raise ToolOperationError("IO_ERROR", "could not inspect path") from error
        effective_relative = effective.relative_to(self.root).as_posix()
        return effective, effective_relative or "."

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

    def read_text_page_bounded(
        self,
        raw_path: object,
        *,
        offset: int,
        limit: int,
        max_output_bytes: int,
    ) -> BoundedTextPage:
        """Read a page while bounding the returned serialized UTF-8 text.

        The scanner continues through the selected file after output fills so
        that line counts, selected source bytes, and the content fingerprint
        remain truthful.  Only a bounded prefix of a selected line is kept in
        memory, which prevents one-line/minified files from becoming a hidden
        context-sized allocation.
        """

        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        target, relative = self._resolve_text_file(raw_path, max_bytes=MAX_TEXT_FILE_BYTES)
        marker = b"\n... output truncated ..."
        if max_output_bytes <= len(marker):
            marker = b"...truncated..."[:max_output_bytes]
        content_capacity = max(0, max_output_bytes - len(marker))

        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        digest = hashlib.sha256()
        output = bytearray()
        total_lines = 0
        selected_line_count = 0
        returned_lines = 0
        start_line: int | None = None
        end_line: int | None = None
        output_truncated = False
        line_truncated = False
        original_selected_bytes = 0
        line_bytes = 0
        line_capture = bytearray()
        line_capture_truncated = False
        skip_lf_after_cr = False
        pending_cr_selected = False

        def current_line_number() -> int:
            return total_lines + 1

        def append_selected_line(line_number: int, separator_bytes: int) -> None:
            nonlocal selected_line_count, original_selected_bytes
            nonlocal returned_lines, start_line, end_line
            nonlocal output_truncated, line_truncated
            if not (offset <= line_number < offset + limit):
                return
            selected_line_count += 1
            original_selected_bytes += line_bytes + separator_bytes
            if output_truncated:
                return

            prefix = f"{line_number}: ".encode("utf-8")
            line = prefix + bytes(line_capture)
            separator = b"\n" if returned_lines else b""
            # A source line that exceeded the capture budget is necessarily
            # incomplete even if its formatted prefix would otherwise fit.
            needs_marker = line_capture_truncated
            available = content_capacity - len(output) - len(separator)
            if not needs_marker and len(line) <= available:
                output.extend(separator)
                output.extend(line)
                returned_lines += 1
                if start_line is None:
                    start_line = line_number
                end_line = line_number
                return

            output_truncated = True
            line_truncated = True
            if available <= 0:
                return
            # Prefixes are tiny compared with the configured boundary.  If a
            # caller intentionally supplies a very small boundary, keep the
            # prefix whole and report only the marker rather than splitting it.
            if len(prefix) > available:
                return
            content_budget = available - len(prefix)
            clipped = bytes(line_capture[:content_budget])
            output.extend(separator)
            output.extend(prefix)
            output.extend(clipped)
            returned_lines += 1
            if start_line is None:
                start_line = line_number
            end_line = line_number

        def finish_line(separator_bytes: int) -> None:
            nonlocal total_lines, line_bytes, line_capture
            nonlocal line_capture_truncated, skip_lf_after_cr
            nonlocal pending_cr_selected
            line_number = current_line_number()
            append_selected_line(line_number, separator_bytes)
            pending_cr_selected = offset <= line_number < offset + limit
            total_lines += 1
            line_bytes = 0
            line_capture.clear()
            line_capture_truncated = False
            skip_lf_after_cr = False

        def consume(decoded: str) -> None:
            nonlocal line_bytes, line_capture_truncated, skip_lf_after_cr
            nonlocal pending_cr_selected, original_selected_bytes
            for character in decoded:
                encoded = character.encode("utf-8")
                if skip_lf_after_cr and character == "\n":
                    # ``finish_line`` counted the CR separator immediately;
                    # account for the LF here, including when the two bytes
                    # arrive in different read chunks.
                    if pending_cr_selected:
                        original_selected_bytes += len(encoded)
                    skip_lf_after_cr = False
                    pending_cr_selected = False
                    continue
                skip_lf_after_cr = False
                pending_cr_selected = False
                if character in _LINE_SEPARATORS:
                    finish_line(len(encoded))
                    if character == "\r":
                        skip_lf_after_cr = True
                    continue
                line_bytes += len(encoded)
                line_number = current_line_number()
                if offset <= line_number < offset + limit:
                    if len(line_capture) + len(encoded) <= max_output_bytes:
                        line_capture.extend(encoded)
                    else:
                        line_capture_truncated = True

        try:
            with target.open("rb") as source:
                while chunk := source.read(READ_CHUNK_BYTES):
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

        if line_bytes:
            finish_line(0)
        if output_truncated:
            output.extend(marker[: max(0, max_output_bytes - len(output))])
        content = bytes(output).decode("utf-8", errors="strict")
        returned_bytes = len(output)
        has_more_lines = end_line is not None and end_line < total_lines
        return BoundedTextPage(
            content=content,
            total_lines=total_lines,
            relative=relative,
            fingerprint=digest.hexdigest(),
            returned_lines=returned_lines,
            start_line=start_line,
            end_line=end_line,
            truncated=output_truncated or has_more_lines,
            line_truncated=line_truncated,
            returned_bytes=returned_bytes,
            original_selected_bytes=original_selected_bytes,
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
        preserve_links = False
        if target.exists():
            self.read_text_file(raw_path)
            target_metadata = target.stat()
            existing_mode = stat.S_IMODE(target_metadata.st_mode)
            # Replacing a hard-linked inode would silently detach the other
            # names from the write.  Hard links are valid workspace aliases,
            # so update the existing inode in place when more than one name
            # refers to it.  The old bytes are retained for the unlikely
            # partial-write path so callers still get an all-or-nothing
            # operation.
            preserve_links = target_metadata.st_nlink > 1

        try:
            if preserve_links:
                previous = target.read_bytes()
                try:
                    with target.open("wb") as destination:
                        destination.write(content.encode("utf-8"))
                except OSError:
                    try:
                        target.write_bytes(previous)
                    except OSError:
                        pass
                    raise
                return relative, content_bytes
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
            preserve_links = False
            if target.exists():
                preserve_links = target.stat().st_nlink > 1
            if preserve_links:
                with target.open("wb") as destination:
                    destination.write(snapshot.content)
                if snapshot.mode is not None:
                    os.chmod(target, snapshot.mode)
                return relative
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

        # Preserve the caller's workspace-relative spelling in search results
        # while using the resolved target for every access check. This keeps an
        # internal directory alias useful to a model (``alias/file.py``) and
        # still denies an alias whose effective target leaves the workspace.
        if isinstance(raw_path, str):
            lexical_parts = [part for part in Path(raw_path).parts if part not in {"", "."}]
            logical_selected = Path(*lexical_parts).as_posix() if lexical_parts else "."
        else:
            logical_selected = relative
        visited_directories: set[tuple[int, int]] = set()

        def walk(directory: Path, logical_directory: str) -> Iterator[tuple[Path, str]]:
            try:
                directory_metadata = os.stat(directory, follow_symlinks=True)
                directory_key = (directory_metadata.st_dev, directory_metadata.st_ino)
            except OSError as error:
                raise ToolOperationError(
                    "IO_ERROR", f"could not inspect directory: {directory}"
                ) from error
            if directory_key in visited_directories:
                return
            visited_directories.add(directory_key)
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError as error:
                raise ToolOperationError(
                    "IO_ERROR", f"could not traverse directory: {relative}"
                ) from error
            for entry in entries:
                try:
                    candidate = Path(entry.path)
                    # Resolve every entry before inspecting its effective
                    # type. External aliases are ignored, not allowed to
                    # invalidate the whole search.
                    effective = candidate.resolve(strict=True)
                    try:
                        effective.relative_to(self.root)
                    except ValueError:
                        continue
                    metadata = os.stat(effective, follow_symlinks=True)
                    if stat.S_ISDIR(metadata.st_mode):
                        if entry.name not in DEFAULT_IGNORED_DIRECTORIES:
                            child_logical = (
                                f"{logical_directory}/{entry.name}"
                                if logical_directory != "."
                                else entry.name
                            )
                            yield from walk(effective, child_logical)
                        continue
                except (FileNotFoundError, RuntimeError):
                    continue
                except OSError as error:
                    raise ToolOperationError(
                        "IO_ERROR", f"could not inspect workspace entry: {entry.name}"
                    ) from error
                if stat.S_ISREG(metadata.st_mode):
                    workspace_relative = (
                        f"{logical_directory}/{entry.name}"
                        if logical_directory != "."
                        else entry.name
                    )
                    yield effective, workspace_relative

        yield from walk(selected, logical_selected)

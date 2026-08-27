"""Workspace-confined filesystem operations shared by local tools."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from typing import Iterator


DEFAULT_IGNORED_DIRECTORIES = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist"}
)
MAX_TEXT_FILE_BYTES = 10 * 1024 * 1024


class ToolOperationError(Exception):
    """An expected local-tool failure with a stable, model-visible code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class WorkspaceFilesystem:
    """Resolve and validate paths below one configured workspace root."""

    def __init__(self, workspace_root: Path) -> None:
        self.root = workspace_root.resolve()
        if not self.root.is_dir():
            raise ValueError("workspace_root must be an existing directory")

    def resolve(self, raw_path: object) -> tuple[Path, str]:
        if not isinstance(raw_path, str) or not raw_path:
            raise ToolOperationError("INVALID_ARGUMENTS", "path must be a non-empty string")

        candidate = Path(raw_path)
        if candidate.is_absolute():
            raise ToolOperationError("WORKSPACE_ESCAPE", "absolute paths are not allowed")

        try:
            target = (self.root / candidate).resolve()
        except RuntimeError as error:
            raise ToolOperationError("WORKSPACE_ESCAPE", "path cannot be resolved safely") from error
        except OSError as error:
            raise ToolOperationError("IO_ERROR", "could not resolve path") from error
        try:
            relative = target.relative_to(self.root)
        except ValueError as error:
            raise ToolOperationError("WORKSPACE_ESCAPE", "path escapes the workspace") from error
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
    ) -> tuple[list[str], int, str]:
        """Stream one page while counting lines within the file resource limit."""

        target, relative = self._resolve_text_file(raw_path)
        page: list[str] = []
        total_lines = 0
        try:
            with target.open(mode="r", encoding="utf-8", newline=None) as source:
                for total_lines, raw_line in enumerate(source, 1):
                    if "\0" in raw_line:
                        raise ToolOperationError(
                            "NOT_TEXT", f"file contains NUL bytes: {relative}"
                        )
                    if offset <= total_lines < offset + limit:
                        page.append(raw_line.rstrip("\r\n"))
        except UnicodeDecodeError as error:
            raise ToolOperationError(
                "NOT_TEXT", f"file is not valid UTF-8: {relative}"
            ) from error
        except OSError as error:
            raise ToolOperationError("IO_ERROR", f"could not read file: {relative}") from error
        return page, total_lines, relative

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
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in DEFAULT_IGNORED_DIRECTORIES:
                            yield from walk(Path(entry.path))
                        continue
                    candidate = Path(entry.path)
                    resolved = candidate.resolve()
                    resolved.relative_to(selected)
                    resolved.relative_to(self.root)
                except (OSError, RuntimeError, ValueError):
                    continue
                if resolved.is_file():
                    workspace_relative = candidate.relative_to(self.root).as_posix()
                    yield resolved, workspace_relative

        yield from walk(selected)

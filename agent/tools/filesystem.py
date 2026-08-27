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

        target = (self.root / candidate).resolve()
        try:
            relative = target.relative_to(self.root)
        except ValueError as error:
            raise ToolOperationError("WORKSPACE_ESCAPE", "path escapes the workspace") from error
        return target, relative.as_posix()

    def read_text_file(self, raw_path: object) -> tuple[str, str]:
        target, relative = self.resolve(raw_path)
        if not target.exists():
            raise ToolOperationError("NOT_FOUND", f"file not found: {relative}")
        if not target.is_file():
            raise ToolOperationError("NOT_A_FILE", f"not a regular file: {relative}")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ToolOperationError("NOT_TEXT", f"file is not valid UTF-8: {relative}") from error
        except OSError as error:
            raise ToolOperationError("IO_ERROR", f"could not read file: {relative}") from error
        if "\0" in content:
            raise ToolOperationError("NOT_TEXT", f"file contains NUL bytes: {relative}")
        return content, relative

    def write_text_file(self, raw_path: object, content: object) -> tuple[str, int]:
        if not isinstance(content, str):
            raise ToolOperationError("INVALID_ARGUMENTS", "content must be a string")
        if "\0" in content:
            raise ToolOperationError("NOT_TEXT", "new file content must not contain NUL bytes")

        target, relative = self.resolve(raw_path)
        target.parent.mkdir(parents=True, exist_ok=True)
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
        return relative, len(content.encode("utf-8"))

    def regular_files(self, raw_path: object = ".") -> Iterator[tuple[Path, str]]:
        """Yield regular files beneath a selected workspace directory in stable order."""

        selected, relative = self.resolve(raw_path)
        if not selected.exists():
            raise ToolOperationError("NOT_FOUND", f"path not found: {relative}")
        if not selected.is_dir():
            raise ToolOperationError("NOT_A_FILE", f"not a directory: {relative}")

        for directory, directories, filenames in os.walk(selected, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in DEFAULT_IGNORED_DIRECTORIES)
            for filename in sorted(filenames):
                candidate = Path(directory) / filename
                try:
                    resolved = candidate.resolve()
                    workspace_relative = resolved.relative_to(self.root).as_posix()
                except ValueError:
                    continue
                if resolved.is_file():
                    yield resolved, workspace_relative

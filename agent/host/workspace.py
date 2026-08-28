"""Server-side directory browsing constrained to configured workspace roots."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable


class WorkspaceBrowseError(ValueError):
    """A safe workspace browsing failure with a stable transport code."""

    code = "INVALID_WORKSPACE"


class WorkspaceOutsideRootError(WorkspaceBrowseError):
    code = "WORKSPACE_OUTSIDE_ROOT"


class WorkspaceNotFoundError(WorkspaceBrowseError):
    code = "WORKSPACE_NOT_FOUND"


class WorkspaceNotAccessibleError(WorkspaceBrowseError):
    code = "WORKSPACE_NOT_ACCESSIBLE"


class WorkspaceSymlinkError(WorkspaceBrowseError):
    code = "WORKSPACE_SYMLINK_NOT_ALLOWED"


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    name: str
    path: str
    type: str = "directory"


@dataclass(frozen=True, slots=True)
class WorkspaceListing:
    path: str
    parent: str | None
    roots: tuple[str, ...]
    entries: tuple[WorkspaceEntry, ...]
    truncated: bool


class WorkspaceBrowser:
    """Normalize roots and expose one-level, directory-only navigation."""

    def __init__(self, roots: Iterable[Path | str] = (), *, limit: int = 500) -> None:
        requested = tuple(roots) or (Path.cwd(),)
        normalized: list[Path] = []
        for candidate in requested:
            root = Path(candidate).expanduser()
            if root.is_symlink():
                raise WorkspaceSymlinkError("Workspace root cannot be a symlink")
            try:
                root = root.resolve(strict=True)
            except FileNotFoundError as error:
                raise WorkspaceNotFoundError("Workspace root does not exist") from error
            except (OSError, RuntimeError) as error:
                raise WorkspaceNotAccessibleError(
                    "Workspace root is not accessible"
                ) from error
            if not root.is_dir():
                raise WorkspaceNotAccessibleError(
                    "Workspace root is not a directory"
                )
            if root not in normalized:
                normalized.append(root)
        if limit < 1:
            raise ValueError("Workspace listing limit must be positive")
        self._roots = tuple(normalized)
        self._limit = limit

    @property
    def roots(self) -> tuple[str, ...]:
        return tuple(str(root) for root in self._roots)

    def list(self, requested_path: str | None = None) -> WorkspaceListing:
        raw_path = requested_path if requested_path is not None else str(self._roots[0])
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise WorkspaceOutsideRootError("Workspace path is outside configured roots")

        candidate = Path(os.path.abspath(candidate))
        root = self._containing_root(candidate)
        self._reject_symlink_components(root, candidate)

        try:
            if not candidate.exists():
                raise WorkspaceNotFoundError("Workspace path does not exist")
            if not candidate.is_dir():
                raise WorkspaceNotAccessibleError(
                    "Workspace path is not a directory"
                )
            entries = self._directory_entries(candidate)
        except WorkspaceBrowseError:
            raise
        except FileNotFoundError as error:
            raise WorkspaceNotFoundError("Workspace path does not exist") from error
        except PermissionError as error:
            raise WorkspaceNotAccessibleError(
                "Workspace path is not accessible"
            ) from error
        except OSError as error:
            raise WorkspaceNotAccessibleError(
                "Workspace path could not be listed"
            ) from error

        parent = None if candidate == root else str(candidate.parent)
        return WorkspaceListing(
            path=str(candidate),
            parent=parent,
            roots=self.roots,
            entries=tuple(entries[: self._limit]),
            truncated=len(entries) > self._limit,
        )

    def _containing_root(self, candidate: Path) -> Path:
        for root in self._roots:
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            return root
        raise WorkspaceOutsideRootError("Workspace path is outside configured roots")

    @staticmethod
    def _reject_symlink_components(root: Path, candidate: Path) -> None:
        relative = candidate.relative_to(root)
        current = root
        for part in relative.parts:
            current /= part
            try:
                if current.is_symlink():
                    raise WorkspaceSymlinkError(
                        "Workspace symlink navigation is not allowed"
                    )
            except OSError as error:
                raise WorkspaceNotAccessibleError(
                    "Workspace path is not accessible"
                ) from error

    @staticmethod
    def _directory_entries(path: Path) -> list[WorkspaceEntry]:
        directories: list[WorkspaceEntry] = []
        with os.scandir(path) as iterator:
            for entry in iterator:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                directories.append(
                    WorkspaceEntry(name=entry.name, path=entry.path)
                )
        directories.sort(key=lambda entry: entry.name)
        return directories

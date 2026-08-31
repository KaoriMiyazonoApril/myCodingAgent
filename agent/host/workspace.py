"""Host-side directory browsing and explicit in-memory Workspace records.

Browsing is deliberately separate from Agent filesystem authorization. This
module only decides which Host directories can be selected and gives each
selected canonical directory an opaque, process-local id.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable
from uuid import uuid4


class WorkspaceBrowseError(ValueError):
    """A safe workspace browsing failure with a stable transport code."""

    code = "HOST_ERROR"


class WorkspaceOutsideRootError(WorkspaceBrowseError):
    code = "OUTSIDE_ALLOWED_ROOT"


class WorkspaceNotFoundError(WorkspaceBrowseError):
    code = "PATH_NOT_FOUND"


class WorkspaceNotAccessibleError(WorkspaceBrowseError):
    code = "PERMISSION_DENIED"


class WorkspaceInvalidPathError(WorkspaceBrowseError):
    code = "INVALID_PATH"


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


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    """Host-owned identity for one canonical selected directory."""

    workspace_id: str
    path: str
    display_name: str

    @property
    def canonical_path(self) -> str:
        return self.path

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace_id": self.workspace_id,
            "path": self.path,
            "canonical_path": self.path,
            "display_name": self.display_name,
        }


class WorkspaceBrowser:
    """Normalize roots and expose one-level, directory-only Host navigation.

    With no administrator roots, ``/`` is the browse root. Configured roots
    are an allowlist: canonical targets of navigation and selection must stay
    within at least one of them. Directory contents are never recursively
    scanned here.
    """

    def __init__(self, roots: Iterable[Path | str] = (), *, limit: int = 500) -> None:
        requested = tuple(roots) or (Path("/"),)
        normalized: list[Path] = []
        for candidate in requested:
            root = Path(candidate).expanduser()
            try:
                root = root.resolve(strict=True)
            except FileNotFoundError as error:
                raise WorkspaceNotFoundError("Workspace root does not exist") from error
            except ValueError as error:
                raise WorkspaceInvalidPathError("Workspace root is invalid") from error
            except (OSError, RuntimeError) as error:
                raise WorkspaceNotAccessibleError(
                    "Workspace root is not accessible"
                ) from error
            if not root.is_dir():
                raise WorkspaceInvalidPathError("Workspace root is not a directory")
            if root not in normalized:
                normalized.append(root)
        if limit < 1:
            raise ValueError("Workspace listing limit must be positive")
        self._roots = tuple(normalized)
        self._limit = limit
        self._records: dict[str, WorkspaceRecord] = {}
        self._records_by_path: dict[Path, WorkspaceRecord] = {}

    @property
    def roots(self) -> tuple[str, ...]:
        return tuple(str(root) for root in self._roots)

    def list(self, requested_path: str | None = None) -> WorkspaceListing:
        candidate = self._resolve_directory(requested_path)
        root = self._containing_root(candidate)

        try:
            entries = self._directory_entries(candidate)
        except PermissionError as error:
            raise WorkspaceNotAccessibleError(
                "Workspace path is not accessible"
            ) from error
        except FileNotFoundError as error:
            raise WorkspaceNotFoundError("Workspace path does not exist") from error
        except OSError as error:
            raise WorkspaceNotAccessibleError(
                "Workspace path could not be listed"
            ) from error

        parent = None if candidate == root else self._parent_if_allowed(candidate, root)
        return WorkspaceListing(
            path=str(candidate),
            parent=parent,
            roots=self.roots,
            entries=tuple(entries[: self._limit]),
            truncated=len(entries) > self._limit,
        )

    def validate(self, requested_path: str | None = None) -> str:
        """Validate and canonicalize one selectable directory."""

        return str(self._resolve_directory(requested_path))

    def select(self, requested_path: str | None = None) -> WorkspaceRecord:
        """Create or reuse the Host Workspace record for a canonical path."""

        canonical = self._resolve_directory(requested_path)
        existing = self._records_by_path.get(canonical)
        if existing is not None:
            return existing
        display_name = "/" if canonical == Path("/") else canonical.name
        record = WorkspaceRecord(
            workspace_id=str(uuid4()),
            path=str(canonical),
            display_name=display_name or str(canonical),
        )
        self._records[record.workspace_id] = record
        self._records_by_path[canonical] = record
        return record

    def get(self, workspace_id: str) -> WorkspaceRecord:
        record = self._records.get(workspace_id)
        if record is None:
            raise WorkspaceNotFoundError("Workspace does not exist")
        return record

    def has(self, workspace_id: str) -> bool:
        return workspace_id in self._records

    def _resolve_directory(self, requested_path: str | None) -> Path:
        raw_path = requested_path if requested_path is not None else str(self._roots[0])
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise WorkspaceInvalidPathError("Workspace path is invalid")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            raise WorkspaceInvalidPathError("Workspace path must be absolute")
        try:
            canonical = candidate.resolve(strict=True)
        except FileNotFoundError as error:
            raise WorkspaceNotFoundError("Workspace path does not exist") from error
        except ValueError as error:
            raise WorkspaceInvalidPathError("Workspace path is invalid") from error
        except PermissionError as error:
            raise WorkspaceNotAccessibleError(
                "Workspace path is not accessible"
            ) from error
        except (OSError, RuntimeError) as error:
            raise WorkspaceNotAccessibleError(
                "Workspace path is not accessible"
            ) from error
        if not canonical.is_dir():
            raise WorkspaceInvalidPathError("Workspace path is not a directory")
        self._containing_root(canonical)
        # A directory listing is the minimum Host accessibility check and is
        # intentionally limited to this selected directory.
        try:
            with os.scandir(canonical):
                pass
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
        return canonical

    def _containing_root(self, candidate: Path) -> Path:
        for root in self._roots:
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            return root
        raise WorkspaceOutsideRootError("Workspace path is outside configured roots")

    def _parent_if_allowed(self, candidate: Path, root: Path) -> str | None:
        parent = candidate.parent.resolve(strict=True)
        try:
            parent.relative_to(root)
        except ValueError:
            return None
        return str(parent)

    def _directory_entries(self, path: Path) -> list[WorkspaceEntry]:
        directories: list[WorkspaceEntry] = []
        with os.scandir(path) as iterator:
            for entry in iterator:
                try:
                    # Follow directory aliases so internal links are useful in
                    # the browser. External aliases are omitted from listings;
                    # direct navigation still returns OUTSIDE_ALLOWED_ROOT.
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                    canonical = Path(entry.path).resolve(strict=True)
                    self._containing_root(canonical)
                except WorkspaceOutsideRootError:
                    continue
                except (FileNotFoundError, RuntimeError, OSError):
                    continue
                directories.append(WorkspaceEntry(name=entry.name, path=str(canonical)))
        directories.sort(key=lambda entry: entry.name)
        return directories

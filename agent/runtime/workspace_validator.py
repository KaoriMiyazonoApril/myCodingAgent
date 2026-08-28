"""Fail-closed validation for one persistent workspace tree."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import stat
import time

from agent.tools.filesystem import existing_path_components

from .errors import UnsafeWorkspaceError, WorkspaceValidationLimitError


def mounted_paths() -> frozenset[Path]:
    """Read Linux mount points without treating escaped spaces as separators."""

    paths: set[Path] = set()
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise UnsafeWorkspaceError("could not inspect system mount table") from error
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        mount_point = fields[4]
        for escaped, decoded in (
            ("\\040", " "),
            ("\\011", "\t"),
            ("\\012", "\n"),
            ("\\134", "\\"),
        ):
            mount_point = mount_point.replace(escaped, decoded)
        paths.add(Path(mount_point))
    return frozenset(paths)


class WorkspaceValidator:
    """Reject links, nested mounts and incomplete validation scans."""

    def __init__(
        self,
        *,
        max_entries: int = 100_000,
        max_seconds: float = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or not 1 <= max_entries <= 1_000_000
        ):
            raise ValueError(
                "workspace_validation_max_entries must be between 1 and 1000000"
            )
        if (
            isinstance(max_seconds, bool)
            or not isinstance(max_seconds, (int, float))
            or not 0 < max_seconds <= 60
        ):
            raise ValueError(
                "workspace_validation_max_seconds must be greater than 0 and at most 60"
            )
        self._max_entries = max_entries
        self._max_seconds = max_seconds
        self._clock = clock

    @staticmethod
    def normalize_root(workspace: Path) -> Path:
        candidate = Path(workspace).absolute()
        try:
            for component, metadata in existing_path_components(candidate):
                if stat.S_ISLNK(metadata.st_mode):
                    raise UnsafeWorkspaceError(
                        "workspace path component must not be a symbolic link: "
                        f"{component.as_posix()}"
                    )
            metadata = os.stat(candidate, follow_symlinks=False)
        except OSError as error:
            raise ValueError("workspace must be an existing directory") from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("workspace must be an existing directory")
        return candidate.resolve(strict=True)

    def validate(self, workspace: Path) -> None:
        started = self._clock()
        root_metadata = self._lstat(workspace)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise UnsafeWorkspaceError("workspace root is not a regular directory")
        nested_mounts = {
            path
            for path in mounted_paths()
            if path != workspace and workspace in path.parents
        }
        entries_seen = 0
        pending = [workspace]
        while pending:
            self._check_time(started)
            directory = pending.pop()
            try:
                entries = os.scandir(directory)
            except OSError as error:
                raise UnsafeWorkspaceError(
                    f"could not completely scan workspace: {directory.as_posix()}"
                ) from error
            with entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > self._max_entries:
                        raise WorkspaceValidationLimitError(
                            "workspace entry validation budget exceeded"
                        )
                    self._check_time(started)
                    path = Path(entry.path)
                    metadata = self._lstat(path)
                    if stat.S_ISLNK(metadata.st_mode):
                        raise UnsafeWorkspaceError(
                            f"workspace symbolic link is forbidden: {path.as_posix()}"
                        )
                    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                        raise UnsafeWorkspaceError(
                            f"workspace hard link is forbidden: {path.as_posix()}"
                        )
                    if path in nested_mounts or os.path.ismount(path):
                        raise UnsafeWorkspaceError(
                            f"nested workspace mount is forbidden: {path.as_posix()}"
                        )
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(path)

    def _check_time(self, started: float) -> None:
        if self._clock() - started > self._max_seconds:
            raise WorkspaceValidationLimitError(
                "workspace time validation budget exceeded"
            )

    @staticmethod
    def _lstat(path: Path) -> os.stat_result:
        try:
            return os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise UnsafeWorkspaceError(
                f"could not inspect workspace entry: {path.as_posix()}"
            ) from error

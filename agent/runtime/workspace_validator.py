"""Selection-time validation for one canonical workspace root.

The validator intentionally does not walk the workspace.  File tools perform
effective-target containment checks for each access, while this module only
ensures that a selected root remains a readable directory.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import stat
import time

from .errors import (
    UnsafeWorkspaceError,
    WorkspaceUnavailableError,
    WorkspaceValidationLimitError,
)


def mounted_paths() -> frozenset[Path]:
    """Read Linux mount points for compatibility with older callers.

    Mount inspection is no longer part of Turn preflight; this helper remains
    available for integrations that explicitly need to inspect the host.
    """

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
    """Validate only the selected root; authorize descendants at access time."""

    def __init__(
        self,
        *,
        max_entries: int = 100_000,
        max_seconds: float = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Keep constructor compatibility for callers that still provide the
        # old scan budgets. They are deliberately unused by validation.
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
        candidate = Path(workspace).expanduser()
        try:
            canonical = candidate.resolve(strict=True)
            metadata = os.stat(canonical, follow_symlinks=True)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("workspace must be an existing directory")
            # Validate readability once at selection/thread creation time. No
            # descendant is inspected and no recursive scan is performed.
            with os.scandir(canonical):
                pass
            return canonical
        except ValueError:
            raise
        except (FileNotFoundError, NotADirectoryError, OSError, RuntimeError) as error:
            raise ValueError("workspace must be an existing directory") from error

    def validate(self, workspace: Path) -> None:
        """Check that the root still exists and is a directory.

        This method is retained as a lightweight compatibility seam. It does
        not inspect children, symlinks, hard links, mounts, or repository size.
        """

        try:
            metadata = os.stat(workspace, follow_symlinks=True)
        except (FileNotFoundError, NotADirectoryError, PermissionError) as error:
            raise WorkspaceUnavailableError("workspace root is not accessible") from error
        except OSError as error:
            raise UnsafeWorkspaceError("workspace root is not accessible") from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceUnavailableError("workspace root is not a directory")

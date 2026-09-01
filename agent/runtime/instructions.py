"""Stable and project-provided instruction sources for Context V2.

Instruction loading is deliberately independent from Context orchestration:
the manager freezes one :class:`ProjectInstructions` value at Turn start and
only this module knows how the bounded root ``AGENTS.md`` read works.  The
historical ``agent.runtime.context`` module re-exports these names for source
compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .context_types import BaseSystemInstructions, ContextSection


MAX_PROJECT_INSTRUCTIONS_BYTES = 64 * 1024
PROJECT_INSTRUCTIONS_MAX_BYTES = MAX_PROJECT_INSTRUCTIONS_BYTES


@dataclass(frozen=True, slots=True)
class ProjectInstructions:
    """Immutable project guidance returned by a project provider."""

    text: str = ""
    source: str = ""
    original_bytes: int = 0
    retained_bytes: int = 0
    omitted_bytes: int = 0
    truncated: bool = False
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("project instructions must be text")
        if not isinstance(self.source, str):
            raise ValueError("project instructions source must be text")
        for name, value in (
            ("original_bytes", self.original_bytes),
            ("retained_bytes", self.retained_bytes),
            ("omitted_bytes", self.omitted_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.truncated, bool):
            raise ValueError("project instructions truncated must be boolean")
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, str) for item in self.diagnostics
        ):
            raise ValueError("project instructions diagnostics must be text tuple")
        retained = len(self.text.encode("utf-8"))
        if self.retained_bytes == 0 and self.text:
            object.__setattr__(self, "retained_bytes", retained)
        if self.original_bytes == 0 and self.text:
            object.__setattr__(self, "original_bytes", retained)

    def sections(self) -> list[ContextSection]:
        if not self.text:
            return []
        return [ContextSection("project_instructions", self.text)]

    @property
    def loaded(self) -> bool:
        return bool(self.source) and not {
            "AGENTS_INVALID_UTF8",
            "AGENTS_READ_ERROR",
        }.intersection(self.diagnostics)

    @property
    def metadata(self) -> dict[str, object]:
        return {
            "source": self.source,
            "loaded": self.loaded,
            "original_bytes": self.original_bytes,
            "retained_bytes": self.retained_bytes,
            "omitted_bytes": self.omitted_bytes,
            "truncated": self.truncated,
            "diagnostics": list(self.diagnostics),
        }


class ProjectInstructionsProvider(Protocol):
    """Provider seam for resolving project guidance."""

    def load(self, workspace: Path) -> ProjectInstructions:
        raise NotImplementedError


class StaticProjectInstructionsProvider:
    """Provider for instructions supplied by the Runtime or a test."""

    def __init__(self, instructions: ProjectInstructions | str | None = None) -> None:
        self._instructions = (
            instructions
            if isinstance(instructions, ProjectInstructions)
            else ProjectInstructions(instructions or "")
        )

    def load(self, workspace: Path) -> ProjectInstructions:
        del workspace
        return self._instructions


class RootProjectInstructionsProvider:
    """Load exactly ``<workspace>/AGENTS.md`` with a bounded UTF-8 read."""

    filename = "AGENTS.md"

    def __init__(
        self,
        *,
        max_bytes: int = MAX_PROJECT_INSTRUCTIONS_BYTES,
        additional_instructions: str | None = None,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if additional_instructions is not None and not isinstance(
            additional_instructions, str
        ):
            raise ValueError("additional_instructions must be text or None")
        self.max_bytes = max_bytes
        self.additional_instructions = additional_instructions or ""

    def load(self, workspace: Path) -> ProjectInstructions:
        path = Path(workspace) / self.filename
        try:
            metadata = path.stat()
            original_bytes = max(0, int(metadata.st_size))
            with path.open("rb") as source:
                raw = source.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                original_bytes = max(original_bytes, len(raw))
            if original_bytes > self.max_bytes or len(raw) > self.max_bytes:
                text, retained, omitted = _bounded_instruction_text(
                    raw[: self.max_bytes], original_bytes, self.max_bytes
                )
                instructions = ProjectInstructions(
                    text=text,
                    source=self.filename,
                    original_bytes=original_bytes,
                    retained_bytes=retained,
                    omitted_bytes=omitted,
                    truncated=True,
                    diagnostics=("AGENTS_TRUNCATED",),
                )
            else:
                text = raw.decode("utf-8")
                instructions = ProjectInstructions(
                    text=text,
                    source=self.filename,
                    original_bytes=len(raw),
                    retained_bytes=len(raw),
                )
        except FileNotFoundError:
            instructions = ProjectInstructions()
        except UnicodeDecodeError:
            instructions = ProjectInstructions(
                source=self.filename,
                diagnostics=("AGENTS_INVALID_UTF8",),
            )
        except (OSError, RuntimeError):
            instructions = ProjectInstructions(
                source=self.filename,
                diagnostics=("AGENTS_READ_ERROR",),
            )

        if not self.additional_instructions:
            return instructions
        combined = (
            f"{instructions.text}\n\n{self.additional_instructions}"
            if instructions.text
            else self.additional_instructions
        )
        return ProjectInstructions(
            text=combined,
            source=instructions.source,
            original_bytes=instructions.original_bytes,
            retained_bytes=len(combined.encode("utf-8")),
            omitted_bytes=instructions.omitted_bytes,
            truncated=instructions.truncated,
            diagnostics=instructions.diagnostics,
        )


FileProjectInstructionsProvider = RootProjectInstructionsProvider
WorkspaceProjectInstructionsProvider = RootProjectInstructionsProvider


def _bounded_instruction_text(
    raw_prefix: bytes,
    original_bytes: int,
    max_bytes: int,
) -> tuple[str, int, int]:
    marker_template = (
        "\n...[AGENTS.md truncated: original_bytes={original}; "
        "omitted_bytes={omitted}]...\n"
    )
    marker = marker_template.format(
        original=original_bytes,
        omitted=max(0, original_bytes - len(raw_prefix)),
    ).encode("ascii")
    for _ in range(8):
        prefix = raw_prefix[: max(0, max_bytes - len(marker))]
        while prefix:
            try:
                prefix.decode("utf-8")
                break
            except UnicodeDecodeError:
                prefix = prefix[:-1]
        next_marker = marker_template.format(
            original=original_bytes,
            omitted=max(0, original_bytes - len(prefix)),
        ).encode("ascii")
        if next_marker == marker:
            break
        marker = next_marker
    if len(marker) >= max_bytes:
        marker = marker[:max_bytes]
        prefix = b""
    else:
        prefix = raw_prefix[: max_bytes - len(marker)]
        while prefix:
            try:
                prefix.decode("utf-8")
                break
            except UnicodeDecodeError:
                prefix = prefix[:-1]
    omitted = max(0, original_bytes - len(prefix))
    marker = marker_template.format(
        original=original_bytes,
        omitted=omitted,
    ).encode("ascii")
    if len(marker) > max_bytes:
        marker = marker[:max_bytes]
        prefix = b""
    result = (prefix + marker)[:max_bytes]
    return result.decode("utf-8", errors="strict"), len(result), omitted


__all__ = [
    "BaseSystemInstructions",
    "FileProjectInstructionsProvider",
    "MAX_PROJECT_INSTRUCTIONS_BYTES",
    "PROJECT_INSTRUCTIONS_MAX_BYTES",
    "ProjectInstructions",
    "ProjectInstructionsProvider",
    "RootProjectInstructionsProvider",
    "StaticProjectInstructionsProvider",
    "WorkspaceProjectInstructionsProvider",
]

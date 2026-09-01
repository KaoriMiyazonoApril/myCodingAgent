"""Provider-independent Skill metadata and turn-local loading state."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import re

from agent.tools.types import ToolResult


SKILL_SCHEMA_VERSION = 1
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_NAME_CHARS = 64
MAX_SKILL_DESCRIPTION_CHARS = 1_024
MAX_SKILL_FILE_BYTES = 64 * 1024
MAX_SKILL_BODY_CHARS = MAX_SKILL_FILE_BYTES
MAX_SKILL_CATALOG_CHARS = 8_000
# Explicitly mentioned skills are projected as one bounded late instruction
# block.  Tool-loaded bodies are intentionally excluded from this projection:
# their canonical source is the chronological ToolResult pair.
MAX_EXPLICIT_SKILL_PROJECTION_CHARS = 16_000


@dataclass(frozen=True, slots=True)
class Skill:
    """A validated ``SKILL.md`` with frontmatter removed from ``body``."""

    name: str
    description: str
    body: str
    source: str
    source_path: str
    directory: str

    def __post_init__(self) -> None:
        if not SKILL_NAME_PATTERN.fullmatch(self.name) or len(self.name) > MAX_SKILL_NAME_CHARS:
            raise ValueError("skill name must be lowercase kebab-case")
        if not isinstance(self.description, str) or not 1 <= len(self.description) <= MAX_SKILL_DESCRIPTION_CHARS:
            raise ValueError("skill description must be between 1 and 1024 characters")
        if not isinstance(self.body, str):
            raise ValueError("skill body must be text")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("skill source must be non-empty")
        if not isinstance(self.source_path, str) or not self.source_path:
            raise ValueError("skill source_path must be non-empty")
        if not isinstance(self.directory, str) or not self.directory:
            raise ValueError("skill directory must be non-empty")

    @property
    def path(self) -> str:
        return self.source_path

    @property
    def metadata(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "source_path": self.source_path,
            "directory": self.directory,
        }

    def to_dict(self) -> dict[str, str]:
        return self.metadata


@dataclass(frozen=True, slots=True)
class SkillDiagnostic:
    source: str
    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "path": self.path,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class SkillCatalogProjection:
    text: str
    omitted: int = 0
    included: int = 0
    max_chars: int = MAX_SKILL_CATALOG_CHARS

    @property
    def content(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """Complete valid winners plus fail-soft discovery diagnostics."""

    skills: tuple[Skill, ...] = ()
    diagnostics: tuple[SkillDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.skills, key=lambda skill: skill.name))
        if len({skill.name for skill in ordered}) != len(ordered):
            raise ValueError("skill catalog names must be unique")
        object.__setattr__(self, "skills", ordered)
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    @property
    def available(self) -> tuple[Skill, ...]:
        return self.skills

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in self.skills)

    def get(self, name: str) -> Skill | None:
        if not isinstance(name, str):
            return None
        return next((skill for skill in self.skills if skill.name == name), None)

    lookup = get

    def model_projection(self, *, max_chars: int = MAX_SKILL_CATALOG_CHARS) -> SkillCatalogProjection:
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 64:
            raise ValueError("max_chars must be at least 64")
        lines = [
            "available_skills:",
        ]
        omitted = 0
        included = 0
        for skill in self.skills:
            prefix = f"- {skill.name}: "
            # Descriptions are shortened before dropping a whole entry, which
            # keeps large installations useful while preserving deterministic
            # name ordering.
            description = skill.description
            available = max_chars - len("\n".join(lines)) - len(prefix) - 1
            if available <= 0:
                omitted += 1
                continue
            if len(description) > available:
                description = description[: max(0, available - 1)].rstrip() + "…"
            line = prefix + description
            candidate = "\n".join((*lines, line))
            if len(candidate) > max_chars:
                omitted += 1
                continue
            lines.append(line)
            included += 1
        if omitted:
            marker = f"- omitted_skills: {omitted} (see complete Host catalog)"
            if len("\n".join((*lines, marker))) <= max_chars:
                lines.append(marker)
        text = "\n".join(lines)
        if len(text) > max_chars:
            # The entry loop normally proves the bound, but keep the public
            # projection contract defensive if a future marker changes.
            text = text[: max_chars - 1].rstrip() + "…"
        return SkillCatalogProjection(
            text=text,
            omitted=omitted,
            included=included,
            max_chars=max_chars,
        )

    @property
    def model_catalog(self) -> str:
        return self.model_projection().text

    @property
    def catalog(self) -> str:
        return self.model_catalog

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SKILL_SCHEMA_VERSION,
            "available": [skill.metadata for skill in self.skills],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class SkillLoad:
    skill: Skill
    newly_loaded: bool

    @property
    def name(self) -> str:
        return self.skill.name


class SkillTurnState:
    """Turn-local loaded set shared by explicit and model tool activation."""

    def __init__(
        self,
        catalog: SkillCatalog,
        *,
        on_loaded: Callable[[Skill], object] | None = None,
        body_max_chars: int = MAX_SKILL_BODY_CHARS,
    ) -> None:
        if not isinstance(catalog, SkillCatalog):
            raise ValueError("catalog must be SkillCatalog")
        if isinstance(body_max_chars, bool) or not isinstance(body_max_chars, int) or body_max_chars < 1:
            raise ValueError("body_max_chars must be positive")
        self.catalog = catalog
        self._loaded: dict[str, Skill] = {}
        self._explicit_loaded: dict[str, Skill] = {}
        self._on_loaded = on_loaded
        self.body_max_chars = body_max_chars
        # Body formatting belongs to the loader seam; this object only owns
        # turn-local state and activation lifecycle.
        from .loader import SkillLoader

        self.loader = SkillLoader(catalog, body_max_chars=body_max_chars)

    @property
    def loaded(self) -> tuple[Skill, ...]:
        return tuple(self._loaded[name] for name in self._loaded)

    @property
    def loaded_names(self) -> tuple[str, ...]:
        return tuple(self._loaded)

    def get(self, name: str) -> Skill | None:
        return self._loaded.get(name)

    def activate(self, name: str) -> Skill | None:
        skill = self.catalog.get(name)
        if skill is None:
            return None
        if name not in self._loaded:
            self._mark_loaded(skill, explicit=True)
        elif name not in self._explicit_loaded:
            # A caller can explicitly mention a skill after the model has
            # loaded it.  Metadata remains unified while the late projection
            # is promoted exactly once.
            self._explicit_loaded[name] = skill
        return skill

    def _mark_loaded(self, skill: Skill, *, explicit: bool) -> None:
        if skill.name in self._loaded:
            if explicit:
                self._explicit_loaded.setdefault(skill.name, skill)
            return
        self._loaded[skill.name] = skill
        if explicit:
            self._explicit_loaded[skill.name] = skill
        callback = self._on_loaded
        if callback is not None:
            callback(skill)

    def load(self, name: str) -> ToolResult:
        if not isinstance(name, str) or not name.strip():
            return ToolResult(
                content="skill name must be a non-empty string",
                metadata={"status": "invalid"},
                error_code="INVALID_ARGUMENTS",
            )
        normalized = name.strip()
        skill = self.catalog.get(normalized)
        if skill is None:
            return self.loader.load(normalized)
        if normalized in self._loaded:
            return ToolResult(
                content=f"skill already loaded: {normalized}",
                metadata={
                    "status": "already_loaded",
                    "name": normalized,
                    "source": skill.source,
                    "source_path": skill.source_path,
                },
            )
        # Model tool loading is represented by the real ToolCall/ToolResult
        # chronological pair.  It must not be copied into the next request's
        # late instruction tail.
        self._mark_loaded(skill, explicit=False)
        return self.loader.load(skill)

    def explicit_activation(self, names: Sequence[str]) -> tuple[str, ...]:
        loaded: list[str] = []
        for name in names:
            if self.activate(name) is not None and name not in loaded:
                loaded.append(name)
        return tuple(loaded)

    @property
    def explicit_loaded_names(self) -> tuple[str, ...]:
        return tuple(self._explicit_loaded)

    def projection(self, *, max_chars: int = MAX_EXPLICIT_SKILL_PROJECTION_CHARS) -> str:
        """Return only explicit skill bodies as a bounded aggregate.

        ``skill(name)`` tool bodies remain in chronological history.  The
        distinction is deliberate: repeating a tool result in a late system
        message makes each subsequent request grow without bound and erases
        the protocol provenance of the load.
        """

        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 64:
            raise ValueError("max_chars must be at least 64")
        if not self._explicit_loaded:
            # An empty turn-local projection should not add a synthetic
            # message to otherwise unchanged V1 requests.
            return ""
        lines = ["loaded_skills:"]
        omitted = 0
        for skill in self._explicit_loaded.values():
            body = skill.body[: self.body_max_chars]
            entry = (
                f"## {skill.name} ({skill.source_path})\n{skill.description}\n{body}"
            )
            candidate = "\n\n".join((*lines, entry))
            if len(candidate) <= max_chars:
                lines.append(entry)
                continue
            # Keep the metadata useful even when one explicit body is huge;
            # never emit an unbounded partial body merely to fit it.
            metadata_entry = (
                f"## {skill.name} ({skill.source_path})\n{skill.description}\n"
                "[body omitted: explicit skill projection budget]"
            )
            candidate = "\n\n".join((*lines, metadata_entry))
            if len(candidate) <= max_chars:
                lines.append(metadata_entry)
            omitted += 1
        if omitted:
            marker = f"- omitted_explicit_skill_bodies: {omitted}"
            candidate = "\n\n".join((*lines, marker))
            if len(candidate) <= max_chars:
                lines.append(marker)
        text = "\n\n".join(lines)
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        return text

    def snapshot(self) -> dict[str, object]:
        return {
            "loaded": [skill.metadata for skill in self.loaded],
            "available_count": len(self.catalog.skills),
        }

    def reset(self) -> None:
        self._loaded.clear()
        self._explicit_loaded.clear()


__all__ = [
    "MAX_SKILL_BODY_CHARS",
    "MAX_SKILL_CATALOG_CHARS",
    "MAX_EXPLICIT_SKILL_PROJECTION_CHARS",
    "MAX_SKILL_DESCRIPTION_CHARS",
    "MAX_SKILL_FILE_BYTES",
    "MAX_SKILL_NAME_CHARS",
    "SKILL_NAME_PATTERN",
    "SKILL_SCHEMA_VERSION",
    "Skill",
    "SkillCatalog",
    "SkillCatalogProjection",
    "SkillDiagnostic",
    "SkillLoad",
    "SkillTurnState",
]

"""Bounded Skill body loading independent from turn-local state."""

from __future__ import annotations

from agent.tools.types import ToolResult

from .types import MAX_SKILL_BODY_CHARS, Skill, SkillCatalog


class SkillLoader:
    """Render one validated Skill as a bounded ordinary tool result.

    Loading is deliberately stateless here.  :class:`SkillTurnState` owns
    idempotence, callbacks, and the turn-local loaded set; this seam only
    knows how to resolve a catalog entry and remove frontmatter from the
    body returned to the model.
    """

    def __init__(
        self,
        catalog: SkillCatalog,
        *,
        body_max_chars: int = MAX_SKILL_BODY_CHARS,
    ) -> None:
        if not isinstance(catalog, SkillCatalog):
            raise ValueError("catalog must be SkillCatalog")
        if (
            isinstance(body_max_chars, bool)
            or not isinstance(body_max_chars, int)
            or body_max_chars < 1
        ):
            raise ValueError("body_max_chars must be positive")
        self.catalog = catalog
        self.body_max_chars = body_max_chars

    def resolve(self, name: str) -> Skill | None:
        if not isinstance(name, str):
            return None
        return self.catalog.get(name.strip())

    def load(self, value: Skill | str) -> ToolResult:
        """Return a bounded body result for a Skill or catalog name."""

        if isinstance(value, Skill):
            skill = value
        elif isinstance(value, str) and value.strip():
            name = value.strip()
            skill = self.catalog.get(name)
            if skill is None:
                return ToolResult(
                    content=f"unknown skill: {name}",
                    metadata={"status": "unknown", "name": name},
                    error_code="SKILL_NOT_FOUND",
                )
        else:
            return ToolResult(
                content="skill name must be a non-empty string",
                metadata={"status": "invalid"},
                error_code="INVALID_ARGUMENTS",
            )
        body = skill.body[: self.body_max_chars]
        return ToolResult(
            content=(
                f"# Skill: {skill.name}\n"
                f"source: {skill.source_path}\n\n"
                f"{body}"
            ),
            metadata={
                "status": "loaded",
                "name": skill.name,
                "source": skill.source,
                "source_path": skill.source_path,
            },
        )

    load_skill = load


__all__ = ["SkillLoader", "Skill", "SkillCatalog"]

"""Skills V1: bounded discovery, explicit activation and turn-local loading."""

from .activation import explicit_skill_names, parse_explicit_skills, parse_skill_mentions
from .discovery import (
    SkillDiscovery,
    SkillRegistry,
    USER_SKILL_ROOTS,
    WORKSPACE_SKILL_ROOTS,
    discover_skills,
    parse_skill_file,
)
from .loader import SkillLoader
from .types import (
    MAX_SKILL_BODY_CHARS,
    MAX_SKILL_CATALOG_CHARS,
    MAX_SKILL_DESCRIPTION_CHARS,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_NAME_CHARS,
    SKILL_NAME_PATTERN,
    SKILL_SCHEMA_VERSION,
    Skill,
    SkillCatalog,
    SkillCatalogProjection,
    SkillDiagnostic,
    SkillLoad,
    SkillTurnState,
)

__all__ = [
    "MAX_SKILL_BODY_CHARS",
    "MAX_SKILL_CATALOG_CHARS",
    "MAX_SKILL_DESCRIPTION_CHARS",
    "MAX_SKILL_FILE_BYTES",
    "MAX_SKILL_NAME_CHARS",
    "SKILL_NAME_PATTERN",
    "SKILL_SCHEMA_VERSION",
    "Skill",
    "SkillCatalog",
    "SkillCatalogProjection",
    "SkillDiagnostic",
    "SkillDiscovery",
    "SkillLoad",
    "SkillLoader",
    "SkillRegistry",
    "SkillTurnState",
    "USER_SKILL_ROOTS",
    "WORKSPACE_SKILL_ROOTS",
    "discover_skills",
    "explicit_skill_names",
    "parse_explicit_skills",
    "parse_skill_file",
    "parse_skill_mentions",
]

"""Explicit ``$skill-name`` activation parsing."""

from __future__ import annotations

import re
from collections.abc import Sequence


_EXPLICIT_SKILL_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])\$([a-z0-9]+(?:-[a-z0-9]+)*)")


def explicit_skill_names(text: str) -> tuple[str, ...]:
    """Return known-shape mentions in first-mention order, deduplicated."""

    if not isinstance(text, str):
        raise ValueError("user text must be a string")
    return tuple(dict.fromkeys(match.group(1) for match in _EXPLICIT_SKILL_PATTERN.finditer(text)))


parse_explicit_skills = explicit_skill_names
parse_skill_mentions = explicit_skill_names


__all__ = ["explicit_skill_names", "parse_explicit_skills", "parse_skill_mentions"]

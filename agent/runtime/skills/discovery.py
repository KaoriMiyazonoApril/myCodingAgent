"""Known-root, fail-soft ``SKILL.md`` discovery."""

from __future__ import annotations

from pathlib import Path

from .types import (
    MAX_SKILL_DESCRIPTION_CHARS,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_NAME_CHARS,
    SKILL_NAME_PATTERN,
    Skill,
    SkillCatalog,
    SkillDiagnostic,
)


WORKSPACE_SKILL_ROOTS: tuple[tuple[str, str], ...] = (
    (".agents", ".agents/skills"),
    (".claude", ".claude/skills"),
    (".opencode", ".opencode/skills"),
)
USER_SKILL_ROOTS: tuple[tuple[str, str], ...] = (
    ("~/.agents", ".agents/skills"),
    ("~/.claude", ".claude/skills"),
    ("~/.codex", ".codex/skills"),
    ("~/.config/opencode", ".config/opencode/skills"),
)


def _parse_frontmatter(raw: str) -> tuple[str, str, str] | None:
    if not raw.startswith("---"):
        return None
    first_end = raw.find("\n")
    if first_end < 0:
        return None
    lines = raw[first_end + 1 :].splitlines(keepends=True)
    closing = None
    for index, line in enumerate(lines):
        if line.rstrip("\r\n") == "---":
            closing = index
            break
    if closing is None:
        return None
    values: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []
    for line in lines[:closing]:
        stripped = line.rstrip("\r\n")
        if not stripped.strip():
            if current_key == "description":
                current_lines.append("")
            continue
        if stripped[0].isspace():
            if current_key is None:
                return None
            if current_key == "description":
                current_lines.append(stripped.strip())
            continue
        if ":" not in stripped:
            return None
        key, value = stripped.split(":", 1)
        key = key.strip()
        if key not in {"name", "description"}:
            # Unknown keys are intentionally ignored for portability.
            # Keep accepting their indented continuation lines; only known
            # fields participate in the strict value parser below.
            current_key = "__ignored__"
            current_lines = []
            continue
        if key in values:
            return None
        value = value.strip()
        if value in {">", "|"}:
            current_key = key
            current_lines = []
            values[key] = ""
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
        current_key = key
        current_lines = [value]
    if current_key == "description" and current_lines:
        values["description"] = "\n".join(current_lines).strip()
    body_start = first_end + 1
    offsets = raw.splitlines(keepends=True)
    end_offset = sum(len(item) for item in offsets[: closing + 2])
    body = raw[end_offset:]
    return values.get("name", ""), values.get("description", ""), body.lstrip("\r\n")


def parse_skill_file(
    path: Path,
    *,
    source: str,
    directory_name: str | None = None,
    max_bytes: int = MAX_SKILL_FILE_BYTES,
) -> tuple[Skill | None, SkillDiagnostic | None]:
    """Parse one bounded, direct-child ``SKILL.md``."""

    path_text = str(path)
    try:
        info = path.stat()
        if not path.is_file():
            return None, SkillDiagnostic(source, path_text, "SKILL_NOT_FILE", "SKILL.md is not a regular file")
        if info.st_size > max_bytes:
            return None, SkillDiagnostic(source, path_text, "SKILL_TOO_LARGE", f"SKILL.md exceeds {max_bytes} bytes")
        raw_bytes = path.read_bytes()
        if len(raw_bytes) > max_bytes:
            return None, SkillDiagnostic(source, path_text, "SKILL_TOO_LARGE", f"SKILL.md exceeds {max_bytes} bytes")
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None, SkillDiagnostic(source, path_text, "SKILL_INVALID_UTF8", "SKILL.md is not valid UTF-8")
    except (OSError, RuntimeError) as error:
        return None, SkillDiagnostic(source, path_text, "SKILL_READ_ERROR", type(error).__name__)
    parsed = _parse_frontmatter(raw)
    if parsed is None:
        return None, SkillDiagnostic(source, path_text, "SKILL_INVALID_FRONTMATTER", "SKILL.md frontmatter is malformed or missing")
    name, description, body = parsed
    if not SKILL_NAME_PATTERN.fullmatch(name) or len(name) > MAX_SKILL_NAME_CHARS:
        return None, SkillDiagnostic(source, path_text, "SKILL_INVALID_NAME", "name must be lowercase kebab-case")
    if directory_name is not None and name != directory_name:
        return None, SkillDiagnostic(source, path_text, "SKILL_NAME_DIRECTORY_MISMATCH", "name must match the Skill directory")
    if not 1 <= len(description) <= MAX_SKILL_DESCRIPTION_CHARS:
        return None, SkillDiagnostic(source, path_text, "SKILL_INVALID_DESCRIPTION", "description must be 1..1024 characters")
    try:
        canonical = path.resolve(strict=True)
        directory = canonical.parent
    except (OSError, RuntimeError):
        return None, SkillDiagnostic(source, path_text, "SKILL_INVALID_PATH", "Skill path could not be canonicalized")
    return Skill(
        name=name,
        description=description,
        body=body,
        source=source,
        source_path=str(canonical),
        directory=str(directory),
    ), None


class SkillDiscovery:
    """Scan only the explicit workspace and user-global locations."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        max_file_bytes: int = MAX_SKILL_FILE_BYTES,
    ) -> None:
        self.home = Path.home() if home is None else Path(home)
        if (
            isinstance(max_file_bytes, bool)
            or not isinstance(max_file_bytes, int)
            or max_file_bytes < 1
        ):
            raise ValueError("max_file_bytes must be a positive integer")
        self.max_file_bytes = max_file_bytes

    def roots(self, workspace: Path) -> tuple[tuple[str, Path], ...]:
        root = Path(workspace)
        workspace_roots = tuple((label, root / relative) for label, relative in WORKSPACE_SKILL_ROOTS)
        user_roots = tuple((label, self.home / relative) for label, relative in USER_SKILL_ROOTS)
        return workspace_roots + user_roots

    def discover(self, workspace: Path) -> SkillCatalog:
        winners: dict[str, Skill] = {}
        diagnostics: list[SkillDiagnostic] = []
        for source, root in self.roots(workspace):
            try:
                entries = sorted(root.iterdir(), key=lambda item: item.name)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as error:
                diagnostics.append(SkillDiagnostic(source, str(root), "SKILL_ROOT_READ_ERROR", type(error).__name__))
                continue
            for entry in entries:
                try:
                    is_directory = entry.is_dir()
                except (OSError, RuntimeError) as error:
                    diagnostics.append(
                        SkillDiagnostic(
                            source,
                            str(entry),
                            "SKILL_INVALID_PATH",
                            type(error).__name__,
                        )
                    )
                    continue
                if not is_directory:
                    diagnostics.append(SkillDiagnostic(source, str(entry), "SKILL_INVALID_PATH", "Skill entry is not a directory"))
                    continue
                if not SKILL_NAME_PATTERN.fullmatch(entry.name) or len(entry.name) > MAX_SKILL_NAME_CHARS:
                    diagnostics.append(SkillDiagnostic(source, str(entry), "SKILL_INVALID_NAME", "directory name must be lowercase kebab-case"))
                    continue
                skill_path = entry / "SKILL.md"
                skill, diagnostic = parse_skill_file(
                    skill_path,
                    source=source,
                    directory_name=entry.name,
                    max_bytes=self.max_file_bytes,
                )
                if diagnostic is not None:
                    diagnostics.append(diagnostic)
                    continue
                assert skill is not None
                if skill.name in winners:
                    diagnostics.append(
                        SkillDiagnostic(
                            source,
                            str(skill_path),
                            "SKILL_SHADOWED",
                            f"shadowed by {winners[skill.name].source_path}",
                        )
                    )
                    continue
                winners[skill.name] = skill
        return SkillCatalog(tuple(winners.values()), tuple(diagnostics))

    scan = discover


def discover_skills(workspace: Path, *, home: Path | None = None) -> SkillCatalog:
    return SkillDiscovery(home=home).discover(workspace)


# Names used by a few integrations that model the scanner as a registry.
SkillRegistry = SkillDiscovery


__all__ = [
    "SkillDiscovery",
    "SkillRegistry",
    "WORKSPACE_SKILL_ROOTS",
    "USER_SKILL_ROOTS",
    "discover_skills",
    "parse_skill_file",
]

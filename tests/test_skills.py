from __future__ import annotations

from pathlib import Path

from agent.runtime.skills import (
    SkillDiscovery,
    SkillLoader,
    SkillTurnState,
    discover_skills,
    explicit_skill_names,
    parse_skill_file,
)


def _write_skill(root: Path, name: str, description: str, body: str) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
        encoding="utf-8",
    )


def test_discovery_uses_known_roots_precedence_and_deterministic_order(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    _write_skill(workspace / ".agents" / "skills", "shared", "workspace winner", "A")
    _write_skill(workspace / ".claude" / "skills", "shared", "shadowed", "B")
    _write_skill(home / ".agents" / "skills", "shared", "user shadowed", "C")
    _write_skill(workspace / ".opencode" / "skills", "zeta", "last", "Z")
    _write_skill(home / ".codex" / "skills", "alpha", "first", "A")

    catalog = discover_skills(workspace, home=home)
    assert catalog.names == ("alpha", "shared", "zeta")
    assert catalog.get("shared") is not None
    assert catalog.get("shared").description == "workspace winner"  # type: ignore[union-attr]
    assert sum(item.code == "SKILL_SHADOWED" for item in catalog.diagnostics) == 2
    projection = catalog.model_projection(max_chars=120)
    assert len(projection.text) <= 120
    assert "A" not in projection.text  # bodies never enter the catalog


def test_malformed_skill_is_fail_soft_and_explicit_mentions_are_deduplicated(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broken = workspace / ".agents" / "skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("not frontmatter", encoding="utf-8")

    catalog = SkillDiscovery(home=tmp_path / "empty-home").discover(workspace)
    assert catalog.skills == ()
    assert any(item.code == "SKILL_INVALID_FRONTMATTER" for item in catalog.diagnostics)
    assert explicit_skill_names("$alpha then $alpha and $beta") == ("alpha", "beta")


def test_skill_loading_is_idempotent_and_turn_local(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(workspace / ".agents" / "skills", "alpha", "Do alpha", "body text")
    catalog = SkillDiscovery(home=tmp_path / "empty-home").discover(workspace)
    loaded_events: list[str] = []
    state = SkillTurnState(catalog, on_loaded=lambda skill: loaded_events.append(skill.name))

    first = state.load("alpha")
    second = state.load("alpha")
    assert first.metadata["status"] == "loaded"
    assert "body text" in first.content
    assert second.metadata["status"] == "already_loaded"
    assert "body text" not in second.content
    assert loaded_events == ["alpha"]
    # Model ``skill(name)`` loads are represented by a real chronological
    # ToolResult; their full body is not duplicated in the late tail.
    assert state.projection() == ""
    state.reset()
    state.explicit_activation(("alpha",))
    assert "body text" in state.projection()
    state.reset()
    assert state.loaded_names == ()
    assert state.projection() == ""


def test_skill_snapshot_reports_activation_source_and_available_bucket(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(workspace / ".agents" / "skills", "alpha", "Do alpha", "body text")
    _write_skill(workspace / ".agents" / "skills", "beta", "Do beta", "other body")
    catalog = SkillDiscovery(home=tmp_path / "empty-home").discover(workspace)
    state = SkillTurnState(catalog)

    state.activate("alpha")
    state.load("beta")
    snapshot = state.snapshot()

    assert snapshot["available_count"] == 0
    loaded = snapshot["loaded"]
    assert isinstance(loaded, list)
    assert loaded[0]["name"] == "alpha"
    assert loaded[0]["activation_source"] == "explicit"
    assert loaded[0]["placement"] == "working_tail"
    assert loaded[1]["name"] == "beta"
    assert loaded[1]["activation_source"] == "tool"
    assert loaded[1]["placement"] == "tool_history"


def test_skill_loader_is_a_stateless_bounded_body_seam(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(workspace / ".agents" / "skills", "alpha", "Do alpha", "body text")
    catalog = SkillDiscovery(home=tmp_path / "empty-home").discover(workspace)
    loader = SkillLoader(catalog)

    loaded = loader.load("alpha")
    unknown = loader.load("missing")

    assert loaded.metadata["status"] == "loaded"
    assert "body text" in loaded.content
    assert unknown.error_code == "SKILL_NOT_FOUND"


def test_unknown_frontmatter_fields_with_continuations_are_ignored(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill = workspace / ".agents" / "skills" / "portable"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: portable\n"
        "description: Portable workflow\n"
        "metadata: >\n"
        "  vendor detail\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )

    catalog = SkillDiscovery(home=tmp_path / "empty-home").discover(workspace)
    assert catalog.names == ("portable",)
    assert catalog.get("portable").body == "body\n"  # type: ignore[union-attr]


def test_discovery_covers_all_declared_workspace_and_user_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    workspace_roots = (
        workspace / ".agents" / "skills",
        workspace / ".claude" / "skills",
        workspace / ".opencode" / "skills",
    )
    user_roots = (
        home / ".agents" / "skills",
        home / ".claude" / "skills",
        home / ".codex" / "skills",
        home / ".config" / "opencode" / "skills",
    )
    for index, root in enumerate((*workspace_roots, *user_roots)):
        _write_skill(root, f"root-{index}", f"root {index}", f"body {index}")

    catalog = SkillDiscovery(home=home).discover(workspace)

    assert catalog.names == tuple(f"root-{index}" for index in range(7))
    assert [skill.source for skill in catalog.skills] == [
        ".agents",
        ".claude",
        ".opencode",
        "~/.agents",
        "~/.claude",
        "~/.codex",
        "~/.config/opencode",
    ]


def test_skill_parser_matrix_rejects_malformed_missing_oversized_and_invalid_paths(
    tmp_path: Path,
) -> None:
    def write(name: str, raw: str | bytes) -> Path:
        path = tmp_path / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(raw, bytes):
            path.write_bytes(raw)
        else:
            path.write_text(raw, encoding="utf-8")
        return path

    cases = (
        ("leading-space", " ---\nname: leading-space\ndescription: bad\n---\nbody", "SKILL_INVALID_FRONTMATTER"),
        ("suffix-delimiter", "---extra\nname: suffix-delimiter\ndescription: bad\n---\nbody", "SKILL_INVALID_FRONTMATTER"),
        ("missing-closing", "---\nname: missing-closing\ndescription: bad\nbody", "SKILL_INVALID_FRONTMATTER"),
        ("missing-name", "---\ndescription: missing name\n---\nbody", "SKILL_INVALID_NAME"),
        ("missing-description", "---\nname: missing-description\n---\nbody", "SKILL_INVALID_DESCRIPTION"),
        ("duplicate-name", "---\nname: duplicate-name\nname: duplicate-name\ndescription: bad\n---\nbody", "SKILL_INVALID_FRONTMATTER"),
        ("bad-field", "---\nname bad\ndescription: bad\n---\nbody", "SKILL_INVALID_FRONTMATTER"),
        ("bad-name", "---\nname: Bad_Name\ndescription: bad\n---\nbody", "SKILL_INVALID_NAME"),
        ("bad-directory", "---\nname: another-name\ndescription: bad\n---\nbody", "SKILL_NAME_DIRECTORY_MISMATCH"),
    )
    for directory, raw, code in cases:
        skill, diagnostic = parse_skill_file(
            write(directory, raw),
            source="test",
            directory_name=directory,
        )
        assert skill is None
        assert diagnostic is not None
        assert diagnostic.code == code

    oversized = write(
        "oversized",
        "---\nname: oversized\ndescription: too large\n---\n" + "x" * 128,
    )
    _, diagnostic = parse_skill_file(
        oversized,
        source="test",
        directory_name="oversized",
        max_bytes=64,
    )
    assert diagnostic is not None and diagnostic.code == "SKILL_TOO_LARGE"

    invalid_utf8 = tmp_path / "invalid-utf8" / "SKILL.md"
    invalid_utf8.parent.mkdir()
    invalid_utf8.write_bytes(b"---\nname: invalid-utf8\ndescription: \xff\n---\n")
    _, diagnostic = parse_skill_file(
        invalid_utf8,
        source="test",
        directory_name="invalid-utf8",
    )
    assert diagnostic is not None and diagnostic.code == "SKILL_INVALID_UTF8"

    non_file = tmp_path / "not-a-file"
    non_file.mkdir()
    _, diagnostic = parse_skill_file(non_file, source="test")
    assert diagnostic is not None and diagnostic.code == "SKILL_NOT_FILE"


def test_readable_canonical_skill_symlink_is_allowed_and_broken_path_is_fail_soft(
    tmp_path: Path,
) -> None:
    target = tmp_path / "canonical" / "SKILL.md"
    target.parent.mkdir()
    target.write_text(
        "---\nname: linked\ndescription: canonical target\n---\nbody",
        encoding="utf-8",
    )
    link = tmp_path / "linked" / "SKILL.md"
    link.parent.mkdir()
    link.symlink_to(target)
    skill, diagnostic = parse_skill_file(
        link,
        source="test",
        directory_name="linked",
    )
    assert diagnostic is None
    assert skill is not None
    assert Path(skill.source_path) == target.resolve()

    # A symlinked directory whose canonical SKILL.md keeps the matching name
    # is readable and accepted; only broken/unreadable paths fail soft.
    canonical_dir = tmp_path / "canonical-name"
    canonical_dir.mkdir()
    (canonical_dir / "SKILL.md").write_text(
        "---\nname: linked-dir\ndescription: canonical target\n---\nbody",
        encoding="utf-8",
    )
    linked_dir = tmp_path / "linked-dir"
    linked_dir.symlink_to(canonical_dir, target_is_directory=True)
    skill, diagnostic = parse_skill_file(
        linked_dir / "SKILL.md",
        source="test",
        directory_name="linked-dir",
    )
    assert diagnostic is None
    assert skill is not None
    assert Path(skill.source_path) == (canonical_dir / "SKILL.md").resolve()

    broken = tmp_path / "broken" / "SKILL.md"
    broken.parent.mkdir()
    broken.symlink_to(tmp_path / "does-not-exist")
    _, diagnostic = parse_skill_file(broken, source="test")
    assert diagnostic is not None
    assert diagnostic.code in {"SKILL_NOT_FILE", "SKILL_READ_ERROR", "SKILL_INVALID_PATH"}


def test_explicit_skill_projection_is_deterministic_and_bounded() -> None:
    from agent.runtime.skills import Skill, SkillCatalog

    catalog = SkillCatalog(
        tuple(
            Skill(
                name=name,
                description=f"description for {name}",
                body=f"body for {name} " + "x" * 200,
                source="test",
                source_path=f"/skills/{name}/SKILL.md",
                directory=f"/skills/{name}",
            )
            for name in ("alpha", "beta", "gamma")
        )
    )
    first = SkillTurnState(catalog)
    second = SkillTurnState(catalog)
    assert first.explicit_activation(("gamma", "alpha", "gamma", "beta")) == (
        "gamma",
        "alpha",
        "beta",
    )
    second.explicit_activation(("gamma", "alpha", "gamma", "beta"))

    projection = first.projection(max_chars=180)
    assert projection == second.projection(max_chars=180)
    assert len(projection) <= 180
    assert "loaded_skills:" in projection
    assert "gamma" in projection
    assert "omitted_explicit_skill_bodies" in projection or "body omitted" in projection
    assert first.loaded_names == ("gamma", "alpha", "beta")
    first.reset()
    assert first.loaded_names == ()
    assert first.explicit_loaded_names == ()
    assert first.projection() == ""

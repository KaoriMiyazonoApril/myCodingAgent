from __future__ import annotations

from pathlib import Path

from agent.runtime.skills import (
    SkillDiscovery,
    SkillLoader,
    SkillTurnState,
    discover_skills,
    explicit_skill_names,
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
    assert "body text" in state.projection()
    state.reset()
    assert state.loaded_names == ()
    assert state.projection() == ""


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

"""Regression coverage for bundled lookup and retired Profile copies."""

import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import marlow_constants
from agent import bundled_skills, skill_commands, skill_utils
from agent.prompt_builder import (
    build_skills_system_prompt,
    clear_skills_system_prompt_cache,
)
from marlow_cli.skills_config import _list_all_skills
from tools import skill_manager_tool, skills_tool


SOURCE_SKILLS = Path(__file__).resolve().parents[2] / "skills"
PACKAGED_SKILLS = {
    "native-mcp": "mcp",
    "webhook-subscriptions": "devops",
}


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("MARLOW_HOME", str(home))
    monkeypatch.setenv("MARLOW_BUNDLED_SKILLS", str(SOURCE_SKILLS))
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", home / "skills")
    monkeypatch.setattr(skill_manager_tool, "SKILLS_DIR", home / "skills")
    monkeypatch.setattr(skill_commands, "_skill_commands", {})
    monkeypatch.setattr(skill_commands, "_skill_commands_platform", None)
    return home


def _write_skill(root, path, body, *, name=None, frontmatter=""):
    directory = root / path
    directory.mkdir(parents=True)
    skill_file = directory / "SKILL.md"
    skill_file.write_text(
        f"---\nname: {name or directory.name}\ndescription: Test skill.\n{frontmatter}---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_file


def _view(name):
    return json.loads(skills_tool.skill_view(name, preprocess=False))


def _available():
    result = json.loads(skills_tool.skills_list())
    assert result["success"], result
    return {skill["name"]: skill for skill in result["skills"]}


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_explicit_catalog_loads_without_installing(profile, tmp_path, monkeypatch, platform):
    monkeypatch.setattr(skill_utils, "sys", SimpleNamespace(platform=platform))
    package_root = tmp_path / "package" / "skills"
    for name, category in PACKAGED_SKILLS.items():
        shutil.copytree(SOURCE_SKILLS / category / name, package_root / category / name)
    _write_skill(package_root, "email/himalaya", "This stale package resource stays hidden.")
    monkeypatch.setenv("MARLOW_BUNDLED_SKILLS", str(package_root))

    assert build_skills_system_prompt() == ""
    assert skill_commands.scan_skill_commands() == {}
    assert {name: row["category"] for name, row in _available().items()} == PACKAGED_SKILLS
    assert not _view("himalaya")["success"]

    for name, category in PACKAGED_SKILLS.items():
        skill_file = package_root / category / name / "SKILL.md"
        for lookup in (name, f"{category}/{name}", f"{category}:{name}"):
            result = _view(lookup)
            assert result["success"], result
            assert result["name"] == name
            assert Path(result["skill_dir"]) == skill_file.parent
        key = skill_commands.resolve_skill_command_key(name.replace("-", "_"))
        assert key == f"/{name}"
        assert f'The user has invoked the "{name}" skill' in (
            skill_commands.build_skill_invocation_message(key, "User instruction")
        )

    assert skill_commands.get_skill_commands() == {}
    assert not list((profile / "skills").rglob("SKILL.md"))


def test_bundled_lookup_resolves_checkout_and_wheel(profile, tmp_path, monkeypatch):
    monkeypatch.delenv("MARLOW_BUNDLED_SKILLS")
    installed = tmp_path / "wheel"
    monkeypatch.setattr(
        marlow_constants, "sysconfig",
        SimpleNamespace(
            get_preferred_scheme=lambda scope: "user",
            get_path=lambda key, scheme=None: str(installed),
        ),
    )
    expected = SOURCE_SKILLS / "mcp/native-mcp/SKILL.md"
    assert list(bundled_skills.iter_on_demand_skills("native-mcp")) == [expected]

    wheel_copy = installed / "skills/mcp/native-mcp/SKILL.md"
    wheel_copy.parent.mkdir(parents=True)
    shutil.copyfile(expected, wheel_copy)
    assert list(bundled_skills.iter_on_demand_skills("native-mcp")) == [wheel_copy]
    assert Path(_view("native-mcp")["skill_dir"]) == wheel_copy.parent
    assert not list((profile / "skills").rglob("SKILL.md"))


def test_bundled_lookup_resolves_user_wheel(profile, tmp_path, monkeypatch):
    monkeypatch.delenv("MARLOW_BUNDLED_SKILLS")
    user_base = tmp_path / "user"
    user_lib = user_base / "lib"
    monkeypatch.setattr(marlow_constants, "__file__", str(user_lib / "marlow_constants.py"))
    monkeypatch.setattr(
        marlow_constants, "sysconfig",
        SimpleNamespace(
            get_preferred_scheme=lambda scope: "user",
            get_path=lambda key, scheme=None: str(
                user_lib if key in {"purelib", "platlib"} else user_base
            ),
        ),
    )
    wheel_copy = user_base / "skills/mcp/native-mcp/SKILL.md"
    wheel_copy.parent.mkdir(parents=True)
    shutil.copyfile(SOURCE_SKILLS / "mcp/native-mcp/SKILL.md", wheel_copy)
    assert list(bundled_skills.iter_on_demand_skills("native-mcp")) == [wheel_copy]


def test_opt_out_hides_bundled_catalog_but_keeps_custom_skills(profile):
    (profile / bundled_skills.NO_BUNDLED_SKILLS_MARKER).touch()
    _write_skill(profile / "skills", "my-skill", "A custom skill.")

    assert list(bundled_skills.iter_on_demand_skills()) == []
    assert set(_available()) == {"my-skill"}
    assert _view("my-skill")["success"]
    assert not _view("native-mcp")["success"]
    assert skill_commands.resolve_skill_command_key("native_mcp") is None


def test_platform_configuration_disables_bundled_skill(profile, monkeypatch):
    (profile / "config.yaml").write_text(
        "skills:\n  platform_disabled:\n    telegram: [native-mcp]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MARLOW_PLATFORM", "telegram")
    assert "native-mcp" not in _available()
    assert not _view("native-mcp")["success"]
    assert skill_commands.build_skill_invocation_message("/native-mcp") is None
    assert {skill["name"] for skill in _list_all_skills()} == set(PACKAGED_SKILLS)

    monkeypatch.setenv("MARLOW_PLATFORM", "cli")
    assert "native-mcp" in _available()
    assert _view("native-mcp")["success"]


def test_explicit_lookup_preserves_user_precedence_and_ambiguity(profile, tmp_path):
    external = tmp_path / "external"
    (profile / "config.yaml").write_text(
        f"skills:\n  external_dirs: [{json.dumps(str(external))}]\n",
        encoding="utf-8",
    )
    ext_file = _write_skill(external, "mcp/native-mcp", "External custom version.")
    assert "External custom version." in _view("native-mcp")["content"]

    local_file = _write_skill(
        profile / "skills", "personal/native-mcp", "Profile custom version."
    )
    assert _available()["native-mcp"]["category"] == "personal"
    assert "Profile custom version." in _view("personal/native-mcp")["content"]
    result = _view("native-mcp")
    assert not result["success"], result
    assert set(result["matches"]) == {str(ext_file), str(local_file)}


def test_bare_lookup_prefers_the_declared_name_of_a_custom_skill(profile):
    _write_skill(
        profile / "skills", "personal/custom-mcp", "Profile custom version.",
        name="native-mcp",
    )
    assert "Profile custom version." in _view("native-mcp")["content"]
    assert "Profile custom version." in skill_commands.build_skill_invocation_message(
        "/native-mcp"
    )
    assert Path(_view("mcp/native-mcp")["skill_dir"]).is_relative_to(SOURCE_SKILLS)


@pytest.mark.parametrize("path", ["native-mcp", "personal/native-mcp"])
def test_custom_directory_does_not_shadow_a_different_declared_name(profile, path):
    _write_skill(profile / "skills", path, "Other custom version.", name="different-skill")

    assert {"different-skill", "native-mcp"} <= set(_available())
    assert Path(_view("native-mcp")["skill_dir"]).is_relative_to(SOURCE_SKILLS)
    assert "Other custom version." in skill_commands.build_skill_invocation_message(
        "/different-skill"
    )
    assert 'user has invoked the "native-mcp" skill' in (
        skill_commands.build_skill_invocation_message("/native-mcp")
    )
    assert "Other custom version." in _view(f"./{path}")["content"]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("folder", ["native-mcp", "custom-mcp"])
def test_bare_lookup_uses_catalog_when_custom_skill_is_incompatible(
    profile, monkeypatch, platform, folder
):
    monkeypatch.setattr(skill_utils, "sys", SimpleNamespace(platform=platform))
    incompatible = "linux" if platform == "darwin" else "macos"
    _write_skill(
        profile / "skills", f"personal/{folder}", "Platform-specific custom version.",
        name="native-mcp", frontmatter=f"platforms: [{incompatible}]\n",
    )

    assert _available()["native-mcp"]["category"] == "mcp"
    assert Path(_view("native-mcp")["skill_dir"]).is_relative_to(SOURCE_SKILLS)
    assert skill_commands.build_skill_invocation_message("/native-mcp")
    assert "not supported" in _view(f"personal/{folder}")["error"]


def test_custom_archived_skill_can_still_be_opened_by_explicit_path(profile):
    _write_skill(profile / "skills", ".archive/custom", "Preserved custom skill.")
    assert "custom" not in _available()
    assert not _view("custom")["success"]
    assert "Preserved custom skill." in _view(".archive/custom")["content"]


def test_incomplete_legacy_scan_does_not_hide_user_skill(profile, monkeypatch):
    root = profile / "skills"
    relative = Path("autonomous-ai-agents/codex")
    skill_file = _write_skill(root, relative, "Previously shipped test content.")
    digest = hashlib.md5(b"SKILL.md" + skill_file.read_bytes(), usedforsecurity=False).hexdigest()
    monkeypatch.setitem(bundled_skills._LEGACY_DIGESTS, relative, (digest,))
    (root / ".bundled_manifest").write_text(f"codex:{digest}\n", encoding="utf-8")
    assert bundled_skills.is_unchanged_legacy_skill(skill_file, root)

    walk = bundled_skills.os.walk

    def incomplete_walk(path, *args, onerror=None, **kwargs):
        if path == skill_file.parent:
            onerror(PermissionError("Cannot read a subdirectory"))
        return walk(path, *args, onerror=onerror, **kwargs)

    monkeypatch.setattr(bundled_skills.os, "walk", incomplete_walk)
    assert not bundled_skills.is_unchanged_legacy_skill(skill_file, root)


@pytest.mark.parametrize(
    "state",
    ["original", "edited", "name-only", "no-manifest", "unrecognized", "symlink"],
)
def test_legacy_copy_is_only_hidden_with_proven_provenance(
    profile, tmp_path, monkeypatch, state
):
    root = profile / "skills"
    relative = Path("autonomous-ai-agents/codex")
    skill_file = _write_skill(root, relative, "Previously shipped test content.")
    original = skill_file.read_bytes()
    digest = hashlib.md5(b"SKILL.md" + original, usedforsecurity=False).hexdigest()
    monkeypatch.setitem(bundled_skills._LEGACY_DIGESTS, relative, (digest,))
    manifest = root / ".bundled_manifest"
    if state == "edited":
        skill_file.write_bytes(original + b"User edit.\n")
    if state == "symlink":
        external_file = tmp_path / "external.md"
        skill_file.rename(external_file)
        skill_file.symlink_to(external_file)
    if state == "unrecognized":
        skill_file.write_bytes(original + b"Legacy sync recorded this user edit.\n")
        digest = hashlib.md5(
            b"SKILL.md" + skill_file.read_bytes(), usedforsecurity=False
        ).hexdigest()
    if state != "no-manifest":
        manifest.write_text("codex\n" if state == "name-only" else f"codex:{digest}\n")
    expected_bytes = skill_file.read_bytes()
    expected_manifest = manifest.read_bytes() if manifest.exists() else None

    visible = state != "original"
    assert ("codex" in _available()) == visible
    assert ("/codex" in skill_commands.scan_skill_commands()) == visible
    assert ("codex" in build_skills_system_prompt()) == visible
    for name in ("codex", relative.as_posix(), "autonomous-ai-agents:codex"):
        assert _view(name)["success"] is visible

    if state == "original":
        result = json.loads(skill_manager_tool.skill_manage(
            action="create",
            name="codex",
            category="autonomous-ai-agents",
            content=original.decode() + "An attempted replacement.\n",
        ))
        assert not result["success"], result

    assert skill_file.read_bytes() == expected_bytes
    assert skill_file.is_symlink() == (state == "symlink")
    assert (manifest.read_bytes() if manifest.exists() else None) == expected_manifest


def test_existing_snapshot_and_legacy_retained_copy(profile):
    relative = Path("mcp/native-mcp/SKILL.md")
    package_file = SOURCE_SKILLS / relative
    profile_file = profile / "skills" / relative
    profile_file.parent.mkdir(parents=True)
    shutil.copyfile(package_file, profile_file)

    # Start with a snapshot that indexed the copy before provenance was known.
    assert "native-mcp" in build_skills_system_prompt()
    assert (profile / ".skills_prompt_snapshot.json").is_file()
    digest = hashlib.md5(
        b"SKILL.md" + profile_file.read_bytes(), usedforsecurity=False
    ).hexdigest()
    (profile / "skills/.bundled_manifest").write_text(f"native-mcp:{digest}\n")
    clear_skills_system_prompt_cache()

    assert "native-mcp" not in build_skills_system_prompt()
    assert "/native-mcp" not in skill_commands.scan_skill_commands()
    assert Path(_view("native-mcp")["skill_dir"]) == package_file.parent
    assert profile_file.read_bytes() == package_file.read_bytes()

"""Tests for tools/skill_usage.py — sidecar telemetry + provenance filtering."""

import json
import multiprocessing as mp
import os
from pathlib import Path

import pytest


def _bump_view_many(marlow_home: str, skill_name: str, iterations: int) -> None:
    os.environ["MARLOW_HOME"] = marlow_home
    from tools.skill_usage import bump_view

    for _ in range(iterations):
        bump_view(skill_name)


@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    home = tmp_path / ".marlow"
    home.mkdir()
    (home / "skills").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("MARLOW_HOME", str(home))
    # Force skill_usage module to re-resolve paths per test
    import importlib
    import tools.skill_usage as mod
    importlib.reload(mod)
    return home


def _write_skill(skills_dir: Path, name: str, category: str = ""):
    """Create a minimal SKILL.md with a name: frontmatter field."""
    if category:
        d = skills_dir / category / name
    else:
        d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"""---
name: {name}
description: test skill
---

# body
""",
        encoding="utf-8",
    )
    return d


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def test_empty_usage_returns_empty_dict(skills_home):
    from tools.skill_usage import load_usage
    assert load_usage() == {}


def test_save_and_load_roundtrip(skills_home):
    from tools.skill_usage import load_usage, save_usage
    data = {"skill-a": {"use_count": 3, "state": "active"}}
    save_usage(data)
    loaded = load_usage()
    assert loaded["skill-a"]["use_count"] == 3
    assert loaded["skill-a"]["state"] == "active"


def test_save_is_atomic_no_partial_tmp_files(skills_home):
    from tools.skill_usage import save_usage, _usage_file
    save_usage({"x": {"use_count": 1}})
    skills_dir = _usage_file().parent
    # No leftover tempfile
    for p in skills_dir.iterdir():
        assert not p.name.startswith(".usage_"), f"leftover tmp: {p.name}"


def test_get_record_missing_returns_empty_record(skills_home):
    from tools.skill_usage import get_record
    rec = get_record("nonexistent")
    assert rec["use_count"] == 0
    assert rec["view_count"] == 0


def test_get_record_backfills_missing_keys(skills_home):
    from tools.skill_usage import get_record, save_usage
    save_usage({"legacy": {"use_count": 5}})  # old-format record
    rec = get_record("legacy")
    assert rec["use_count"] == 5
    assert "view_count" in rec  # backfilled


def test_load_usage_handles_corrupt_file(skills_home):
    from tools.skill_usage import load_usage, _usage_file
    _usage_file().write_text("{ not json }", encoding="utf-8")
    assert load_usage() == {}


# ---------------------------------------------------------------------------
# Counter bumps
# ---------------------------------------------------------------------------

def test_bump_view_increments_and_timestamps(skills_home):
    from tools.skill_usage import bump_view, get_record
    bump_view("my-skill")
    bump_view("my-skill")
    rec = get_record("my-skill")
    assert rec["view_count"] == 2
    assert rec["last_viewed_at"] is not None


def test_bump_use_increments_and_timestamps(skills_home):
    from tools.skill_usage import bump_use, get_record
    bump_use("my-skill")
    rec = get_record("my-skill")
    assert rec["use_count"] == 1
    assert rec["last_used_at"] is not None


def test_bump_patch_increments_and_timestamps(skills_home):
    from tools.skill_usage import bump_patch, get_record
    bump_patch("my-skill")
    rec = get_record("my-skill")
    assert rec["patch_count"] == 1
    assert rec["last_patched_at"] is not None


def test_bump_on_empty_name_is_noop(skills_home):
    from tools.skill_usage import bump_view, load_usage
    bump_view("")
    assert load_usage() == {}


def test_bumps_do_not_corrupt_other_skills(skills_home):
    from tools.skill_usage import bump_view, bump_use, get_record
    bump_view("skill-a")
    bump_use("skill-b")
    bump_view("skill-a")
    assert get_record("skill-a")["view_count"] == 2
    assert get_record("skill-a")["use_count"] == 0
    assert get_record("skill-b")["use_count"] == 1


def test_concurrent_bump_view_preserves_all_updates(skills_home):
    from tools.skill_usage import get_record

    process_count = 6
    iterations = 25
    ctx = mp.get_context("spawn")
    processes = [
        ctx.Process(
            target=_bump_view_many,
            args=(str(skills_home), "shared-skill", iterations),
        )
        for _ in range(process_count)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    for process in processes:
        assert process.exitcode == 0
    assert get_record("shared-skill")["view_count"] == process_count * iterations


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------

def test_forget_removes_record(skills_home):
    from tools.skill_usage import bump_view, forget, load_usage
    bump_view("x")
    assert "x" in load_usage()
    forget("x")
    assert "x" not in load_usage()


# ---------------------------------------------------------------------------
# Bundled skill telemetry
# ---------------------------------------------------------------------------

def test_bump_view_tracks_bundled_skill(skills_home):
    """Bundled skills also record usage."""
    from tools.skill_usage import bump_view, load_usage
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "ship-bundled")
    (skills_dir / ".bundled_manifest").write_text(
        "ship-bundled:abc\n", encoding="utf-8",
    )

    bump_view("ship-bundled")
    rec = load_usage().get("ship-bundled")
    assert isinstance(rec, dict), "bundled skill telemetry should be recorded"
    assert rec["view_count"] == 1


def test_usage_report_covers_retained_provenance(skills_home):
    """usage_report() covers bundled and locally created skills."""
    from tools.skill_usage import (
        bump_use, usage_report,
    )
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "bundled-one")
    _write_skill(skills_dir, "mine")
    (skills_dir / ".bundled_manifest").write_text("bundled-one:abc\n", encoding="utf-8")
    for n in ("bundled-one", "mine"):
        bump_use(n)

    rows = {r["name"]: r for r in usage_report()}
    assert set(rows) == {"bundled-one", "mine"}
    assert rows["bundled-one"]["provenance"] == "bundled"
    assert rows["mine"]["provenance"] == "agent"
    # All carry real usage now.
    for n in rows:
        assert rows[n]["use_count"] == 1
        assert rows[n]["_persisted"] is True



def test_legacy_pin_and_suppression_survive_tracking(skills_home):
    from tools.skill_usage import bump_view, get_record, save_usage

    save_usage({"custom": {"pinned": True, "state": "stale"}})
    suppression_file = skills_home / "skills" / ".curator_suppressed"
    suppression_file.write_text("retired-bundled-skill\n", encoding="utf-8")

    bump_view("custom")

    assert get_record("custom")["pinned"] is True
    assert get_record("custom")["state"] == "stale"
    assert suppression_file.read_text(encoding="utf-8") == "retired-bundled-skill\n"

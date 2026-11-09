"""Verify discovery and data compatibility after the Phase 1 removals."""

import logging

from agent.skill_utils import iter_skill_index_files
from marlow_cli.config import ensure_marlow_home, load_config, save_config_value
from plugins.memory import discover_memory_providers, load_memory_provider


def test_fresh_profile_does_not_advertise_retired_features(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    ensure_marlow_home()
    config = load_config()
    assert {"tts", "stt", "voice", "curator", "personalities"}.isdisjoint(config)
    assert {"skin", "personality", "tui_status_indicator"}.isdisjoint(config["display"])
    assert config["memory"]["provider"] == ""

    import model_tools

    names = model_tools.TOOL_TO_TOOLSET_MAP
    assert {"image_generate", "text_to_speech", "computer_use"}.isdisjoint(names)
    assert {"vision_analyze", "browser_navigate", "cronjob", "delegate_task"} <= names.keys()
    providers = {provider[0] for provider in discover_memory_providers()}
    assert "honcho" in providers
    assert "holographic" not in providers


def test_legacy_profile_preserves_data_and_reports_retired_provider(
    tmp_path, monkeypatch, caplog,
):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    legacy_config = "# profile preferences\nmemory:\n  provider: holographic\nvoice:\n  auto_tts: true\n"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(legacy_config)
    database = tmp_path / "memory_store.db"
    database.write_bytes(b"legacy provider data")
    retired = tmp_path / "skills" / "apple" / "macos-computer-use" / "SKILL.md"
    current = tmp_path / "skills" / "custom" / "SKILL.md"
    for path in (retired, current):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Skill")

    with caplog.at_level(logging.WARNING):
        assert load_memory_provider(load_config()["memory"]["provider"]) is None

    assert "no longer supported" in caplog.text
    assert config_path.read_text() == legacy_config
    assert database.read_bytes() == b"legacy provider data"
    assert list(iter_skill_index_files(tmp_path / "skills", "SKILL.md")) == [current]
    assert retired.read_text() == "# Skill"

    assert save_config_value("approvals.destructive_slash_confirm", False)
    assert "# profile preferences" in config_path.read_text()
    assert load_config()["approvals"]["destructive_slash_confirm"] is False
    assert load_config()["memory"]["provider"] == "holographic"

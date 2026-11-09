"""Tests for marlow_cli configuration management."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from marlow_cli.config import (
    DEFAULT_CONFIG,
    get_marlow_home,
    ensure_marlow_home,
    load_custom_provider_entries,
    load_config,
    load_env,
    migrate_config,
    remove_env_value,
    save_config,
    save_env_value,
    save_env_value_secure,
)


class TestGetMarlowHome:
    def test_default_path(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MARLOW_HOME", None)
            home = get_marlow_home()
            assert home == Path.home() / ".marlow"

    def test_env_override(self):
        with patch.dict(os.environ, {"MARLOW_HOME": "/custom/path"}):
            home = get_marlow_home()
            assert home == Path("/custom/path")


class TestEnsureMarlowHome:
    def test_creates_subdirs(self, tmp_path):
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            ensure_marlow_home()
            assert (tmp_path / "cron").is_dir()
            assert (tmp_path / "sessions").is_dir()
            assert (tmp_path / "logs").is_dir()
            assert (tmp_path / "memories").is_dir()

    def test_creates_default_soul_md_if_missing(self, tmp_path):
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            ensure_marlow_home()
            soul_path = tmp_path / "SOUL.md"
            assert soul_path.exists()
            assert soul_path.read_text(encoding="utf-8").strip() != ""

    def test_does_not_overwrite_existing_soul_md(self, tmp_path):
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            soul_path = tmp_path / "SOUL.md"
            soul_path.write_text("custom soul", encoding="utf-8")
            ensure_marlow_home()
            assert soul_path.read_text(encoding="utf-8") == "custom soul"


class TestLoadConfigDefaults:
    def test_returns_defaults_when_no_file(self, tmp_path):
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            config = load_config()
            assert config["model"] == DEFAULT_CONFIG["model"]
            assert config["agent"]["max_turns"] == DEFAULT_CONFIG["agent"]["max_turns"]
            assert "max_turns" not in config
            assert "terminal" in config
            assert config["terminal"]["backend"] == "local"
            assert config["display"]["interim_assistant_messages"] is True


class TestLoadConfigParseFailure:
    """A YAML parse failure must NOT silently fall back to defaults.

    Before issue #23570 this was a single ``print(...)`` that scrolled past
    on the first invocation — users saw aux-fallback misbehavior with no clue
    their config.yaml was being ignored. The helper must:
      * log at WARNING (so ``marlow logs`` surfaces it)
      * also write to stderr (so it's visible at startup even before
        ``setup_logging()`` has wired up file handlers)
      * dedup on (path, mtime_ns, size) so concurrent loads don't spam
      * re-warn after the user edits the file (different mtime)
    """

    def test_logs_and_warns_on_parse_failure(self, tmp_path, caplog, capsys):
        # Reset the dedup cache so this test isn't affected by other tests
        # that may have warned about a different broken config.
        from marlow_cli import config as cfg_mod
        cfg_mod._CONFIG_PARSE_WARNED.clear()

        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            (tmp_path / "config.yaml").write_text("\tbroken tab indent:\n")

            import logging
            with caplog.at_level(logging.WARNING, logger="marlow_cli.config"):
                config = load_config()

            # Falls back to defaults — confirms the silent-fallback we're warning about
            assert config["model"] == DEFAULT_CONFIG["model"]

            # WARNING-level log was emitted with file path + reason
            assert any(
                str(tmp_path / "config.yaml") in rec.message
                and "Falling back to default config" in rec.message
                for rec in caplog.records
            ), f"expected WARNING log, got: {[r.message for r in caplog.records]}"

            # stderr also got a user-visible message (with the ⚠️ marker so it
            # stands out at marlow startup before logging is configured)
            captured = capsys.readouterr()
            assert "marlow config:" in captured.err
            assert str(tmp_path / "config.yaml") in captured.err

    def test_dedup_on_repeated_load_same_file(self, tmp_path, capsys):
        from marlow_cli import config as cfg_mod
        cfg_mod._CONFIG_PARSE_WARNED.clear()

        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            (tmp_path / "config.yaml").write_text("\tbroken:\n")

            load_config()
            first = capsys.readouterr().err
            assert "marlow config:" in first

            load_config()
            second = capsys.readouterr().err
            assert second == "", "second load should NOT re-warn (same file, same mtime)"

    def test_rewarns_after_file_edit(self, tmp_path, capsys):
        import time
        from marlow_cli import config as cfg_mod
        cfg_mod._CONFIG_PARSE_WARNED.clear()

        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            (tmp_path / "config.yaml").write_text("\tbroken:\n")
            load_config()
            capsys.readouterr()  # discard first warning

            # Edit the file (still broken, but different content) — mtime changes
            time.sleep(0.05)
            (tmp_path / "config.yaml").write_text("\tstill broken differently:\n")
            load_config()
            after_edit = capsys.readouterr().err
            assert "marlow config:" in after_edit, "edited file should re-warn"


class TestSaveAndLoadRoundtrip:
    def test_roundtrip(self, tmp_path):
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            config = load_config()
            config["model"] = "test/custom-model"
            config["agent"]["max_turns"] = 42
            save_config(config)

            reloaded = load_config()
            assert reloaded["model"] == "test/custom-model"
            assert reloaded["agent"]["max_turns"] == 42

            saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
            assert saved["agent"]["max_turns"] == 42
            assert "max_turns" not in saved

    def test_nested_values_preserved(self, tmp_path):
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            config = load_config()
            config["terminal"]["timeout"] = 999
            save_config(config)

            reloaded = load_config()
            assert reloaded["terminal"]["timeout"] == 999


class TestSaveEnvValueSecure:
    def test_save_env_value_writes_without_stdout(self, tmp_path, capsys):
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            save_env_value("TENOR_API_KEY", "sk-test-secret")
            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err == ""

            env_values = load_env()
            assert env_values["TENOR_API_KEY"] == "sk-test-secret"

    def test_secure_save_returns_metadata_only(self, tmp_path):
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            result = save_env_value_secure("GITHUB_TOKEN", "ghp_test_secret")
            assert result == {
                "success": True,
                "stored_as": "GITHUB_TOKEN",
                "validated": False,
            }
            assert "secret" not in str(result).lower()

    def test_save_env_value_updates_process_environment(self, tmp_path):
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}, clear=False):
            os.environ.pop("TENOR_API_KEY", None)
            save_env_value("TENOR_API_KEY", "sk-test-secret")
            assert os.environ["TENOR_API_KEY"] == "sk-test-secret"

    def test_save_env_value_hardens_file_permissions_on_posix(self, tmp_path):
        if os.name == "nt":
            return

        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            save_env_value("TENOR_API_KEY", "sk-test-secret")
            env_mode = (tmp_path / ".env").stat().st_mode & 0o777
            assert env_mode == 0o600


class TestRemoveEnvValue:
    def test_removes_key_from_env_file(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("KEY_A=value_a\nKEY_B=value_b\nKEY_C=value_c\n")
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path), "KEY_B": "value_b"}):
            result = remove_env_value("KEY_B")
            assert result is True
            content = env_path.read_text()
            assert "KEY_B" not in content
            assert "KEY_A=value_a" in content
            assert "KEY_C=value_c" in content

    def test_clears_os_environ(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("MY_KEY=my_value\n")
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path), "MY_KEY": "my_value"}):
            remove_env_value("MY_KEY")
            assert "MY_KEY" not in os.environ

    def test_returns_false_when_key_not_found(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("OTHER_KEY=value\n")
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            result = remove_env_value("MISSING_KEY")
            assert result is False
            # File should be untouched
            assert env_path.read_text() == "OTHER_KEY=value\n"

    def test_handles_missing_env_file(self, tmp_path):
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path), "GHOST_KEY": "ghost"}):
            result = remove_env_value("GHOST_KEY")
            assert result is False
            # os.environ should still be cleared
            assert "GHOST_KEY" not in os.environ

    def test_clears_os_environ_even_when_not_in_file(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("OTHER=stuff\n")
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path), "ORPHAN_KEY": "orphan"}):
            remove_env_value("ORPHAN_KEY")
            assert "ORPHAN_KEY" not in os.environ


class TestSaveConfigAtomicity:
    """Verify save_config uses atomic writes (tempfile + os.replace)."""

    def test_no_partial_write_on_crash(self, tmp_path):
        """If save_config crashes mid-write, the previous file stays intact."""
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            # Write an initial config
            config = load_config()
            config["model"] = "original-model"
            save_config(config)

            config_path = tmp_path / "config.yaml"
            assert config_path.exists()

            # Simulate a crash during yaml.dump by making atomic_yaml_write's
            # yaml.dump raise after the temp file is created but before replace.
            with patch("utils.yaml.dump", side_effect=OSError("disk full")):
                try:
                    config["model"] = "should-not-persist"
                    save_config(config)
                except OSError:
                    pass

            # Original file must still be intact
            reloaded = load_config()
            assert reloaded["model"] == "original-model"

    def test_no_leftover_temp_files(self, tmp_path):
        """Failed writes must clean up their temp files."""
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            config = load_config()
            save_config(config)

            with patch("utils.yaml.dump", side_effect=OSError("disk full")):
                try:
                    save_config(config)
                except OSError:
                    pass

            # No .tmp files should remain
            tmp_files = list(tmp_path.glob(".*config*.tmp"))
            assert tmp_files == []

    def test_atomic_write_creates_valid_yaml(self, tmp_path):
        """The written file must be valid YAML matching the input."""
        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            config = load_config()
            config["model"] = "test/atomic-model"
            config["agent"]["max_turns"] = 77
            save_config(config)

            # Read raw YAML to verify it's valid and correct
            config_path = tmp_path / "config.yaml"
            with open(config_path) as f:
                raw = yaml.safe_load(f)
            assert raw["model"] == "test/atomic-model"
            assert raw["agent"]["max_turns"] == 77


class TestCanonicalCustomProviders:
    def test_loads_canonical_provider_entry(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump({
                "providers": {
                    "local-vllm": {
                        "name": "Local vLLM",
                        "base_url": "http://127.0.0.1:8000/v1",
                        "api_key": "test-key",
                        "api_mode": "chat_completions",
                        "model": "local-model",
                    }
                }
            }),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            entries = load_custom_provider_entries()

        assert entries == [{
            "name": "Local vLLM",
            "base_url": "http://127.0.0.1:8000/v1",
            "provider_key": "local-vllm",
            "api_key": "test-key",
            "api_mode": "chat_completions",
            "model": "local-model",
        }]

    def test_ignores_removed_legacy_provider_fields(self, tmp_path, caplog):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump({
                "providers": {
                    "legacy": {
                        "api": "https://example.com/v1",
                        "default_model": "old-model",
                        "transport": "openai_chat",
                    }
                }
            }),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            assert load_custom_provider_entries() == []
        assert "unknown config keys ignored" in caplog.text


class TestCurrentConfigMigration:
    def test_only_advances_schema_version(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        original = {"_config_version": 1, "model": {"default": "local-model"}}
        config_path.write_text(yaml.safe_dump(original), encoding="utf-8")

        with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
            result = migrate_config(interactive=False, quiet=True)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        assert raw["_config_version"] == DEFAULT_CONFIG["_config_version"]
        assert raw["model"] == original["model"]
        assert result["config_added"] == ["_config_version"]


class TestInterimAssistantMessageConfig:
    """Test the explicit gateway interim-message config gate."""

    def test_default_config_enables_interim_assistant_messages(self):
        assert DEFAULT_CONFIG["display"]["interim_assistant_messages"] is True

class TestDiscordChannelPromptsConfig:
    def test_default_config_includes_discord_channel_prompts(self):
        assert DEFAULT_CONFIG["discord"]["channel_prompts"] == {}



class TestEnvWriteDenylist:
    """``save_env_value`` refuses to persist env-var names that
    influence how subprocesses execute — ``LD_PRELOAD``, ``PYTHONPATH``,
    ``PATH``, ``EDITOR``, etc. — or any ``MARLOW_*`` runtime flag.

    Without this gate, a config-writing caller could plant
    ``LD_PRELOAD=/tmp/evil.so`` in ``.env`` and own the next Marlow
    process on next startup via the dotenv → ``os.environ`` chain in
    ``marlow_cli/env_loader.py``.

    The write gate remains part of the retained config security boundary.
    """

    @pytest.fixture(autouse=True)
    def _marlow_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
        ensure_marlow_home()

    @pytest.mark.parametrize(
        "denied_key",
        [
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "LD_AUDIT",
            "DYLD_INSERT_LIBRARIES",
            "DYLD_LIBRARY_PATH",
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONSTARTUP",
            "NODE_OPTIONS",
            "NODE_PATH",
            "PATH",
            "SHELL",
            "EDITOR",
            "VISUAL",
            "PAGER",
            "BROWSER",
            "GIT_SSH_COMMAND",
            "GIT_EXEC_PATH",
            "MARLOW_HOME",
            "MARLOW_PROFILE",
            "MARLOW_CONFIG",
            "MARLOW_ENV",
        ],
    )
    def test_denylisted_keys_rejected(self, denied_key):
        """Each denylisted name raises ``ValueError`` and never reaches
        the on-disk ``.env`` file."""
        with pytest.raises(ValueError, match="denylist"):
            save_env_value(denied_key, "anything")

        # And nothing landed on disk either.
        env = load_env()
        assert denied_key not in env

    @pytest.mark.parametrize(
        "allowed_key",
        [
            "MARLOW_LANGFUSE_PUBLIC_KEY",
            "MARLOW_MAX_ITERATIONS",
        ],
    )
    def test_marlow_integration_keys_still_writable(self, allowed_key):
        """``MARLOW_*`` overall is NOT blocked — only the four runtime
        location names (HOME/PROFILE/CONFIG/ENV) are. Integration
        credentials following the ``MARLOW_*`` convention must keep
        working or we'd regress every provider setup wizard that
        currently writes one of these (auth.py, Langfuse, …)."""
        save_env_value(allowed_key, "test-value-123")
        env = load_env()
        assert env[allowed_key] == "test-value-123"

    def test_custom_provider_key_still_works(self):
        """The denylist must not regress custom endpoint key writes."""
        save_env_value("LOCAL_INFERENCE_API_KEY", "test-key-1234")
        env = load_env()
        assert env["LOCAL_INFERENCE_API_KEY"] == "test-key-1234"

    def test_arbitrary_user_key_still_works(self):
        """Plugin / user-defined env vars (anything outside the
        denylist and outside ``MARLOW_*``) keep working. The denylist
        is narrow on purpose."""
        save_env_value("MY_PLUGIN_TOKEN", "plugin-secret-123")
        env = load_env()
        assert env["MY_PLUGIN_TOKEN"] == "plugin-secret-123"

    def test_save_env_value_secure_inherits_denylist(self):
        """The ``_secure`` variant goes through ``save_env_value`` so
        it inherits the gate — verify, don't assume."""
        with pytest.raises(ValueError, match="denylist"):
            save_env_value_secure("LD_PRELOAD", "/tmp/evil.so")

    def test_pre_existing_value_in_env_file_is_left_alone(self, tmp_path):
        """The gate is on *write*. If ``.env`` already contains
        ``LD_PRELOAD`` (set out-of-band by the operator before this
        change shipped, or hand-edited), we don't blow up — we just
        refuse to add or update it via the API."""
        env_path = tmp_path / ".env"
        env_path.write_text("LD_PRELOAD=/something/legit.so\n")

        # load_env returns it (the read path is intentionally permissive)
        env = load_env()
        assert env["LD_PRELOAD"] == "/something/legit.so"

        # But the write path still refuses to update it
        with pytest.raises(ValueError, match="denylist"):
            save_env_value("LD_PRELOAD", "/tmp/evil.so")

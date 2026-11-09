"""Tests for non-interactive setup and first-run headless behavior."""

from argparse import Namespace
from unittest.mock import patch

import pytest
from marlow_cli.config import DEFAULT_CONFIG, load_config, save_config


def _make_setup_args(**overrides):
    return Namespace(
        non_interactive=overrides.get("non_interactive", False),
        section=overrides.get("section", None),
        reset=overrides.get("reset", False),
    )


class TestNonInteractiveSetup:
    """Verify setup paths exit cleanly in headless/non-interactive environments."""

    def test_cmd_setup_allows_noninteractive_flag_without_tty(self):
        """The CLI entrypoint should not block --non-interactive before setup.py handles it."""
        from marlow_cli.main import cmd_setup

        args = _make_setup_args(non_interactive=True)

        with (
            patch("marlow_cli.setup.run_setup_wizard") as mock_run_setup,
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = False
            cmd_setup(args)

        mock_run_setup.assert_called_once_with(args)

    def test_cmd_setup_defers_no_tty_handling_to_setup_wizard(self):
        """Bare `marlow setup` should reach setup.py, which prints headless guidance."""
        from marlow_cli.main import cmd_setup

        args = _make_setup_args(non_interactive=False)

        with (
            patch("marlow_cli.setup.run_setup_wizard") as mock_run_setup,
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = False
            cmd_setup(args)

        mock_run_setup.assert_called_once_with(args)

    def test_non_interactive_flag_skips_wizard(self, capsys):
        """--non-interactive should print guidance and not enter the wizard."""
        from marlow_cli.setup import run_setup_wizard

        args = _make_setup_args(non_interactive=True)

        with (
            patch("marlow_cli.setup.ensure_marlow_home"),
            patch("marlow_cli.setup.load_config", return_value={}),
            patch("marlow_cli.setup.get_marlow_home", return_value="/tmp/.marlow"),
            patch("marlow_cli.auth.get_active_provider", side_effect=AssertionError("wizard continued")),
            patch("builtins.input", side_effect=AssertionError("input should not be called")),
        ):
            run_setup_wizard(args)

        out = capsys.readouterr().out
        assert "marlow config set model.provider custom" in out

    def test_no_tty_skips_wizard(self, capsys):
        """When stdin has no TTY, the setup wizard should print guidance and return."""
        from marlow_cli.setup import run_setup_wizard

        args = _make_setup_args(non_interactive=False)

        with (
            patch("marlow_cli.setup.ensure_marlow_home"),
            patch("marlow_cli.setup.load_config", return_value={}),
            patch("marlow_cli.setup.get_marlow_home", return_value="/tmp/.marlow"),
            patch("marlow_cli.auth.get_active_provider", side_effect=AssertionError("wizard continued")),
            patch("sys.stdin") as mock_stdin,
            patch("builtins.input", side_effect=AssertionError("input should not be called")),
        ):
            mock_stdin.isatty.return_value = False
            run_setup_wizard(args)

        out = capsys.readouterr().out
        assert "marlow config set model.provider custom" in out

    def test_reset_flag_rewrites_config_before_noninteractive_exit(self, tmp_path, monkeypatch, capsys):
        """--reset should rewrite config.yaml even when the wizard cannot run interactively."""
        from marlow_cli.setup import run_setup_wizard

        monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
        cfg = load_config()
        cfg["model"] = {"provider": "custom", "base_url": "http://localhost:8080/v1", "default": "llama3"}
        cfg["agent"]["max_turns"] = 12
        save_config(cfg)

        args = _make_setup_args(non_interactive=True, reset=True)

        run_setup_wizard(args)

        reloaded = load_config()
        assert reloaded["model"] == DEFAULT_CONFIG["model"]
        assert reloaded["agent"]["max_turns"] == DEFAULT_CONFIG["agent"]["max_turns"]
        out = capsys.readouterr().out
        assert "Configuration reset to defaults." in out

    def test_chat_first_run_headless_skips_setup_prompt(self, capsys):
        from marlow_cli._parser import build_top_level_parser
        from marlow_cli.main import cmd_chat

        parser, _, _ = build_top_level_parser()
        args = parser.parse_args(["chat", "-q", "hello"])

        with (
            patch("marlow_cli.oneshot._resolve_query_runtime", side_effect=RuntimeError("Run marlow setup")),
            patch("marlow_cli.main.cmd_setup") as mock_setup,
            patch("sys.stdin") as mock_stdin,
            patch("builtins.input", side_effect=AssertionError("input should not be called")),
        ):
            mock_stdin.isatty.return_value = False
            with pytest.raises(SystemExit) as exc:
                cmd_chat(args)

        assert exc.value.code == 1
        mock_setup.assert_not_called()
        assert "Run marlow setup" in capsys.readouterr().err

    def test_main_rejects_retired_tts_setup_section(self, monkeypatch, capsys):
        from marlow_cli import main as main_mod

        with patch.object(main_mod, "cmd_setup") as setup:
            monkeypatch.setattr("sys.argv", ["marlow", "setup", "tts"])
            with pytest.raises(SystemExit) as exc:
                main_mod.main()
        assert exc.value.code == 2
        assert "invalid choice" in capsys.readouterr().err
        setup.assert_not_called()

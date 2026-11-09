"""Tests for lightweight per-platform tool configuration."""
from unittest.mock import patch

from marlow_cli.tools_config import (
    CONFIGURABLE_TOOLSETS, PLATFORMS, _DEFAULT_OFF_TOOLSETS,
    _get_platform_tools, _parse_enabled_flag, _platform_toolset_summary,
    _save_platform_tools, tools_command,
)


def test_retained_platforms_only():
    assert set(PLATFORMS) == {"cli", "telegram", "discord", "slack", "email", "feishu", "webhook", "cron"}


def test_configurable_toolsets_match_retained_surface():
    keys = {key for key, _label, _description in CONFIGURABLE_TOOLSETS}
    assert keys == {
        "admin_approval", "browser", "clarify", "code_execution",
        "context_engine", "cronjob", "delegation", "file",
        "memory", "messaging", "moa", "session_search",
        "skills", "terminal", "todo", "vision", "web",
    }


def test_default_platform_tools_are_nonempty_and_exclude_opt_in():
    enabled = _get_platform_tools({}, "cli", include_default_mcp_servers=False)
    assert enabled
    assert enabled.isdisjoint(_DEFAULT_OFF_TOOLSETS)


def test_explicit_platform_selection_is_authoritative():
    cfg = {"platform_toolsets": {"cli": ["web", "terminal"]}}
    assert _get_platform_tools(cfg, "cli", include_default_mcp_servers=False) == {"web", "terminal"}


def test_global_disabled_toolsets_win():
    cfg = {"agent": {"disabled_toolsets": ["memory"]}, "platform_toolsets": {"cli": ["memory", "web"]}}
    assert _get_platform_tools(cfg, "cli", include_default_mcp_servers=False) == {"web"}


def test_active_context_engine_added_except_explicit_empty():
    cfg = {"context": {"engine": "lcm"}, "platform_toolsets": {"cli": ["web"]}}
    assert "context_engine" in _get_platform_tools(cfg, "cli", include_default_mcp_servers=False)
    cfg["platform_toolsets"]["cli"] = []
    assert "context_engine" not in _get_platform_tools(cfg, "cli", include_default_mcp_servers=False)


def test_enabled_mcp_servers_are_default_and_can_be_suppressed():
    cfg = {"mcp_servers": {"local": {"command": "server"}, "off": {"enabled": False}}}
    assert "local" in _get_platform_tools(cfg, "cli")
    assert "off" not in _get_platform_tools(cfg, "cli")
    cfg["platform_toolsets"] = {"cli": ["web", "no_mcp"]}
    assert "local" not in _get_platform_tools(cfg, "cli")


def test_save_preserves_explicit_mcp_names():
    cfg = {"platform_toolsets": {"cli": ["web", "local"]}, "mcp_servers": {"local": {"command": "server"}}}
    with patch("marlow_cli.tools_config.save_config"):
        _save_platform_tools(cfg, "cli", {"terminal"})
    assert cfg["platform_toolsets"]["cli"] == ["local", "terminal"]


def test_summary_uses_requested_platforms():
    cfg = {"platform_toolsets": {"cli": ["web"], "cron": ["terminal"]}}
    assert _platform_toolset_summary(cfg, ["cli", "cron"]) == {"cli": {"web"}, "cron": {"terminal"}}


def test_enabled_flag_parser():
    assert _parse_enabled_flag("yes") is True
    assert _parse_enabled_flag("off") is False
    assert _parse_enabled_flag(None, default=False) is False


def test_tools_command_uses_single_select_default_index():
    captured = {}

    def fake_single_select(title, items, default_index=0):
        captured.update(title=title, items=items, default_index=default_index)
        return len(items) - 1

    with patch("marlow_cli.tools_config._get_enabled_platforms", return_value=["cli"]), \
         patch("marlow_cli.curses_ui.curses_single_select", side_effect=fake_single_select):
        tools_command(config={})

    assert captured == {
        "title": "Configure tools",
        "items": [PLATFORMS["cli"]["label"], "Done"],
        "default_index": 0,
    }

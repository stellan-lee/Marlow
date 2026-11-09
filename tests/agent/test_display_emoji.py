"""Tool emoji defaults for CLI and Gateway progress."""

from unittest.mock import patch

from agent.display import get_tool_emoji


def test_registered_emoji():
    with patch("tools.registry.registry.get_emoji", return_value="💻"):
        assert get_tool_emoji("terminal") == "💻"


def test_fallback_emoji():
    with patch("tools.registry.registry.get_emoji", return_value=""):
        assert get_tool_emoji("unknown") == "⚡"
        assert get_tool_emoji("unknown", default="⚙️") == "⚙️"

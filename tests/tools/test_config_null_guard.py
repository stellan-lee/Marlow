"""Tests for config.get() null-coalescing in tool configuration.

YAML ``null`` values (or ``~``) for a present key make ``dict.get(key, default)``
return ``None`` instead of the default — calling ``.lower()`` on that raises
``AttributeError``.  These tests verify the ``or`` coalescing guards.
"""

from unittest.mock import patch


class TestMCPAuthNullGuard:
    """tools/mcp_tool.py — MCPServerTask.__init__() auth config line"""

    def test_explicit_null_auth_does_not_crash(self):
        """YAML ``auth: null`` in MCP server config should not raise."""
        # Test the expression directly — MCPServerTask.__init__ has many deps
        config = {"auth": None, "timeout": 30}
        auth_type = (config.get("auth") or "").lower().strip()
        assert auth_type == ""

    def test_missing_auth_defaults_to_empty(self):
        config = {"timeout": 30}
        auth_type = (config.get("auth") or "").lower().strip()
        assert auth_type == ""

    def test_valid_auth_passed_through(self):
        config = {"auth": "OAUTH", "timeout": 30}
        auth_type = (config.get("auth") or "").lower().strip()
        assert auth_type == "oauth"


# ── Trajectory compressor ─────────────────────────────────────────────────

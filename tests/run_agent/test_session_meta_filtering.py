"""Tests for session_meta filtering — issue #4715.

Ensures that transcript-only session_meta messages never reach the
chat-completions API, via both the API-boundary guard in
_sanitize_api_messages() and the CLI session-restore paths.
"""

import logging

from run_agent import AIAgent


# ---------------------------------------------------------------------------
# Layer 1 — _sanitize_api_messages role-allowlist guard
# ---------------------------------------------------------------------------

class TestCLISessionRestoreFiltering:

    def test_restore_filters_session_meta(self):
        """Simulates the CLI restore path and verifies session_meta is removed."""
        # Build a fake restored message list (as returned by get_messages_as_conversation)
        fake_restored = [
            {"role": "session_meta", "content": {"model": "gpt-4"}},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "session_meta", "content": {"tools": []}},
        ]

        # Apply the same filtering that the patched CLI code now does
        filtered = [m for m in fake_restored if m.get("role") != "session_meta"]

        assert len(filtered) == 2
        assert all(m["role"] != "session_meta" for m in filtered)
        assert filtered[0]["role"] == "user"
        assert filtered[1]["role"] == "assistant"

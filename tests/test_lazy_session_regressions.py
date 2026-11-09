"""Reproduction tests for #18370 fallout: lazy session creation regressions.

Tests cover Gateway fallback output and pruning orphaned compression continuations.
"""

import threading
import time
import types
from unittest.mock import MagicMock, patch



# ===========================================================================
# Helpers
# ===========================================================================

def _make_session_db(tmp_path):
    """Create a real SessionDB for integration-style tests."""
    from marlow_state import SessionDB
    db_path = tmp_path / "test_state.db"
    return SessionDB(db_path=db_path)


class TestGatewaySurfacesNullResponse:
    """When the agent does work (api_calls > 0) but returns no final_response,
    the gateway must surface an error to the user instead of silently sending
    nothing. Tests exercise the production _normalize_empty_agent_response helper."""

    def test_partial_response_surfaces_error(self):
        """Agent returns partial=True with no response → user sees error."""
        from gateway.run import _normalize_empty_agent_response

        agent_result = {
            "final_response": None,
            "api_calls": 5,
            "partial": True,
            "interrupted": False,
            "error": "Model generated invalid tool call: nonexistent_tool",
        }

        response = agent_result.get("final_response") or ""
        response = _normalize_empty_agent_response(
            agent_result, response, history_len=10,
        )

        assert response != "", "Null response with api_calls>0 must be surfaced"
        assert "nonexistent_tool" in response

    def test_interrupted_response_stays_empty(self):
        """Interrupted agent → response stays empty (platform handles UX)."""
        from gateway.run import _normalize_empty_agent_response

        agent_result = {
            "final_response": None,
            "api_calls": 3,
            "partial": False,
            "interrupted": True,
        }

        response = agent_result.get("final_response") or ""
        response = _normalize_empty_agent_response(
            agent_result, response, history_len=10,
        )

        assert response == "", "Interrupted turns should not get synthetic responses"

    def test_failed_context_overflow(self):
        """Agent failed with context overflow → specific guidance message."""
        from gateway.run import _normalize_empty_agent_response

        agent_result = {
            "final_response": None,
            "api_calls": 0,
            "failed": True,
            "error": "400 Bad Request: context length exceeded",
        }

        response = agent_result.get("final_response") or ""
        response = _normalize_empty_agent_response(
            agent_result, response, history_len=60,
        )

        assert "context window" in response
        assert "/compact" in response

    def test_failed_generic_error(self):
        """Agent failed with non-context error → generic error message."""
        from gateway.run import _normalize_empty_agent_response

        agent_result = {
            "final_response": None,
            "api_calls": 0,
            "failed": True,
            "error": "500 Internal Server Error",
        }

        response = agent_result.get("final_response") or ""
        response = _normalize_empty_agent_response(
            agent_result, response, history_len=5,
        )

        assert "500 Internal Server Error" in response
        assert "/reset" in response

    def test_nonempty_response_passes_through(self):
        """Non-empty response is returned unchanged."""
        from gateway.run import _normalize_empty_agent_response

        agent_result = {"final_response": "Hello!", "api_calls": 1}
        response = "Hello!"
        result = _normalize_empty_agent_response(
            agent_result, response, history_len=5,
        )

        assert result == "Hello!"


# ===========================================================================
# Prune: finalize_orphaned_compression_sessions
# ===========================================================================

class TestFinalizeOrphanedCompressionSessions:
    """The prune migration marks ghost compression continuations as ended."""

    def test_marks_ghost_continuation_with_compression_parent(self, tmp_path):
        """Ghost session with compression-ended parent + messages → finalized."""
        db = _make_session_db(tmp_path)

        # Parent session (ended by compression — this is the key condition)
        db.create_session(session_id="parent", source="tui", model="test")
        db.end_session("parent", "compression")

        # Ghost continuation (has messages, never finalized)
        db.create_session(
            session_id="ghost-cont",
            source="tui",
            model="test",
            parent_session_id="parent",
        )
        db.append_message("ghost-cont", role="user", content="hello")
        db.append_message("ghost-cont", role="assistant", content="hi")

        # Make it old enough (fake started_at)
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (time.time() - 800000, "ghost-cont"),  # ~9 days old
            )
        )

        count = db.finalize_orphaned_compression_sessions()
        assert count == 1

        session = db.get_session("ghost-cont")
        assert session["ended_at"] is not None
        assert session["end_reason"] == "orphaned_compression"

    def test_skips_session_without_parent(self, tmp_path):
        """Ghost session without parent_session_id is NOT a compression
        continuation — should not be touched by this prune."""
        db = _make_session_db(tmp_path)

        db.create_session(session_id="ghost-notitle", source="tui", model="test")
        db.append_message("ghost-notitle", role="user", content="test")

        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (time.time() - 800000, "ghost-notitle"),
            )
        )

        count = db.finalize_orphaned_compression_sessions()
        assert count == 0

    def test_skips_recent_sessions(self, tmp_path):
        """Sessions younger than 7 days are not touched."""
        db = _make_session_db(tmp_path)

        # Create parent first to satisfy FK constraint
        db.create_session(session_id="some-parent", source="tui", model="test")
        db.create_session(
            session_id="recent",
            source="tui",
            model="test",
            parent_session_id="some-parent",
        )
        db.append_message("recent", role="user", content="hello")
        # started_at is now() — within 7 days

        count = db.finalize_orphaned_compression_sessions()
        assert count == 0

    def test_skips_sessions_with_end_reason(self, tmp_path):
        """Properly finalized sessions (even without api_call_count) are skipped."""
        db = _make_session_db(tmp_path)

        # Create parent first to satisfy FK constraint
        db.create_session(session_id="parent", source="tui", model="test")
        db.end_session("parent", "compression")

        db.create_session(
            session_id="already-ended",
            source="tui",
            model="test",
            parent_session_id="parent",
        )
        db.append_message("already-ended", role="user", content="hello")
        db.end_session("already-ended", "user_exit")

        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (time.time() - 800000, "already-ended"),
            )
        )

        count = db.finalize_orphaned_compression_sessions()
        assert count == 0

    def test_skips_session_with_non_compression_parent(self, tmp_path):
        """Child session whose parent was NOT ended by compression should
        not be touched — it's not from the compression continuation path."""
        db = _make_session_db(tmp_path)

        # Parent ended by user_exit, not compression
        db.create_session(session_id="parent", source="tui", model="test")
        db.end_session("parent", "user_exit")

        db.create_session(
            session_id="child",
            source="tui",
            model="test",
            parent_session_id="parent",
        )
        db.append_message("child", role="user", content="hello")

        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (time.time() - 800000, "child"),
            )
        )

        count = db.finalize_orphaned_compression_sessions()
        assert count == 0

    def test_skips_sessions_without_messages(self, tmp_path):
        """Empty sessions (no messages) are NOT targeted by this prune —
        those are handled by prune_empty_ghost_sessions()."""
        db = _make_session_db(tmp_path)

        # Create parent first to satisfy FK constraint
        db.create_session(session_id="parent", source="tui", model="test")
        db.end_session("parent", "compression")

        db.create_session(
            session_id="empty-ghost",
            source="tui",
            model="test",
            parent_session_id="parent",
        )
        # No messages appended

        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (time.time() - 800000, "empty-ghost"),
            )
        )

        count = db.finalize_orphaned_compression_sessions()
        assert count == 0

    def test_titled_ghost_with_parent_is_caught(self, tmp_path):
        """Ghost continuation that HAS a title (propagated from parent by
        _compress_context) is still caught via parent with end_reason='compression'."""
        db = _make_session_db(tmp_path)

        # Create parent first — ended by compression
        db.create_session(session_id="parent", source="tui", model="test")
        db.set_session_title("parent", "Chat")
        db.end_session("parent", "compression")

        db.create_session(
            session_id="titled-ghost",
            source="tui",
            model="test",
            parent_session_id="parent",
        )
        db.set_session_title("titled-ghost", "Chat (2)")
        db.append_message("titled-ghost", role="user", content="continued...")

        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (time.time() - 800000, "titled-ghost"),
            )
        )

        count = db.finalize_orphaned_compression_sessions()
        assert count == 1

        session = db.get_session("titled-ghost")
        assert session["end_reason"] == "orphaned_compression"

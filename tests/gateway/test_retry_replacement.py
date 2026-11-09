"""Regression tests for /retry preserving history and starting an explicit turn."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionStore


@pytest.mark.asyncio
async def test_gateway_retry_preserves_transcript_and_requests_new_turn(tmp_path, monkeypatch):
    # Pin DEFAULT_DB_PATH so SessionDB() doesn't write to the real ~/.marlow/state.db.
    # (Module-level constant snapshot, see test_load_transcript_db_only.)
    import marlow_state
    monkeypatch.setattr(marlow_state, "DEFAULT_DB_PATH", tmp_path / "state.db")

    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path, config=config)

    session_id = "retry_session"
    store._db.create_session(session_id=session_id, source="test")
    for msg in [
        {"role": "session_meta", "tools": []},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "retry me"},
        {"role": "assistant", "content": "old answer"},
    ]:
        store.append_to_transcript(session_id, msg)

    gw = GatewayRunner.__new__(GatewayRunner)
    gw.config = config
    gw.session_store = store

    session_entry = MagicMock(session_id=session_id)
    session_entry.last_prompt_tokens = 111
    gw.session_store.get_or_create_session = MagicMock(return_value=session_entry)

    async def fake_handle_message(event):
        assert event.text.endswith("retry me")
        assert "Verify previous side effects" in event.text
        transcript_before = store.load_transcript(session_id)
        assert [m.get("content") for m in transcript_before if m.get("role") == "user"] == [
            "first question", "retry me"
        ]
        store.append_to_transcript(session_id, {"role": "user", "content": event.text})
        store.append_to_transcript(session_id, {"role": "assistant", "content": "new answer"})
        return "new answer"

    gw._handle_message = AsyncMock(side_effect=fake_handle_message)

    result = await gw._handle_retry_command(
        MessageEvent(text="/retry", message_type=MessageType.TEXT, source=MagicMock())
    )

    assert result == "new answer"
    transcript_after = store.load_transcript(session_id)
    assert [m.get("content") for m in transcript_after if m.get("role") == "user"] == [
        "first question",
        "retry me",
        "The user explicitly requests another attempt. Verify previous side effects before repeating writes.\nretry me",
    ]
    assert [m.get("content") for m in transcript_after if m.get("role") == "assistant"] == [
        "first answer",
        "old answer",
        "new answer",
    ]


@pytest.mark.asyncio
async def test_gateway_retry_replays_original_text_not_retry_command(tmp_path):
    config = MagicMock()
    config.sessions_dir = tmp_path
    config.max_context_messages = 20
    gw = GatewayRunner.__new__(GatewayRunner)
    gw.config = config
    gw.session_store = MagicMock()

    session_entry = MagicMock(session_id="test-session")
    session_entry.last_prompt_tokens = 55
    gw.session_store.get_or_create_session.return_value = session_entry
    gw.session_store.load_transcript.return_value = [
        {"role": "user", "content": "real message"},
        {"role": "assistant", "content": "answer"},
    ]
    gw.session_store.rewrite_transcript = MagicMock()

    captured = {}

    async def fake_handle_message(event):
        captured["text"] = event.text
        return "ok"

    gw._handle_message = AsyncMock(side_effect=fake_handle_message)

    await gw._handle_retry_command(
        MessageEvent(text="/retry", message_type=MessageType.TEXT, source=MagicMock())
    )

    assert captured["text"].endswith("real message")
    assert "Verify previous side effects" in captured["text"]
    gw.session_store.rewrite_transcript.assert_not_called()

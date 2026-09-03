"""Tests for stateless Microsoft Teams thread gateway behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import threading

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import (
    ExternalConversationMessage,
    ExternalConversationSnapshot,
    ExternalHistoryMode,
    MessageEvent,
    SendResult,
)
from gateway.run import _canonical_teams_thread_lane, _work_experience_turn_kwargs
from gateway.session import SessionSource, build_session_key


class _RecordingAdapter:
    platform = Platform("teams")

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, reply_to, metadata))
        return SendResult(success=True, message_id="sent-1")

    async def send_typing(self, chat_id, metadata=None):
        self.typing.append((chat_id, metadata))
        return SendResult(success=True, message_id=None)

    async def stop_typing(self, chat_id):
        self.stop_typing_calls.append(chat_id)


def _teams_source(metadata=None) -> SessionSource:
    return SessionSource(
        platform=Platform("teams"),
        user_id="user-1",
        user_name="Alice",
        chat_id="team-1;channel-1",
        chat_type="channel",
        thread_id="root-1",
        metadata=metadata,
    )


def _snapshot() -> ExternalConversationSnapshot:
    return ExternalConversationSnapshot(
        source_kind="teams_channel_thread",
        platform=Platform("teams"),
        chat_id="team-1;channel-1",
        thread_id="root-1",
        captured_at=datetime.now(timezone.utc),
        trigger_message_id="current-1",
        complete_through_trigger=True,
        history_mode=ExternalHistoryMode.EXTERNAL_AUTHORITATIVE_STATELESS,
        metadata={
            "tenant_id": "tenant-1",
            "team_id": "team-1",
            "channel_id": "channel-1",
            "root_message_id": "root-1",
            "current_message_id": "current-1",
            "root_source": "conversation_messageid",
        },
        messages=(
            ExternalConversationMessage(
                message_id="root-1",
                parent_message_id=None,
                actor=SimpleNamespace(display_name="Alice"),
                created_at=datetime.now(timezone.utc),
                edited_at=None,
                deleted_at=None,
                subject="Design",
                text="Please answer from Graph.",
                attachments=(),
            ),
            ExternalConversationMessage(
                message_id="current-1",
                parent_message_id="root-1",
                actor=SimpleNamespace(display_name="Alice"),
                created_at=datetime.now(timezone.utc),
                edited_at=None,
                deleted_at=None,
                subject=None,
                text="@Marlow what is the plan?",
                attachments=(),
                is_trigger=True,
            ),
        ),
    )


def _event(snapshot: ExternalConversationSnapshot | None = None) -> MessageEvent:
    return MessageEvent(
        text="Graph context",
        message_type="text",
        source=_teams_source(metadata={"teams_reference": snapshot} if snapshot else None),
        raw_user_message="@Marlow what is the plan?",
        message_id="current-1",
        external_conversation_snapshot=snapshot,
    )


def _runner() -> tuple[object, object]:
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform("teams"): PlatformConfig(enabled=True, token="***")}
    )
    adapter = _RecordingAdapter()
    adapter.sent = []
    adapter.typing = []
    adapter.stop_typing_calls = []
    adapter.enrich_authorized_event = AsyncMock(side_effect=lambda event: event)
    runner.adapters = {Platform("teams"): adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session = MagicMock()
    runner.session_store.load_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner.session_store.reset_session = MagicMock()
    runner.session_store.clear_resume_pending = MagicMock()
    runner.session_store.has_any_sessions = MagicMock(return_value=True)
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._thread_execution_lane_locks = {}
    runner._thread_execution_lane_locks_lock = threading.Lock()
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_providers = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._is_super_admin_source = lambda _source: False
    runner._recover_telegram_topic_thread_id = AsyncMock(return_value=None)
    runner._is_telegram_topic_lane = lambda _source: False
    runner._format_session_info = lambda: None
    return runner, adapter


def _agent_result(final_response="ok") -> dict:
    return {
        "final_response": final_response,
        "messages": [
            {"role": "user", "content": "Graph context"},
            {"role": "assistant", "content": final_response},
        ],
        "tools": [],
        "history_offset": 0,
        "last_prompt_tokens": 0,
        "input_tokens": 1,
        "output_tokens": 1,
        "model": "test-model",
        "failed": False,
        "completed": True,
    }


@pytest.mark.asyncio
async def test_stateless_teams_skips_session_store_and_uses_fresh_agent() -> None:
    runner, adapter = _runner()
    snapshot = _snapshot()
    event = _event(snapshot)
    session_key = build_session_key(event.source)
    run_agent_calls = []

    async def fake_run_agent(**kwargs):
        run_agent_calls.append(kwargs)
        assert kwargs["history"] == []
        assert kwargs["session_id"] is None
        assert "session_db" not in kwargs
        assert "conversation_persistence_policy" not in kwargs
        assert kwargs["event"] is event
        return _agent_result()

    runner._session_run_generation[session_key] = 1
    runner._run_agent = AsyncMock(side_effect=fake_run_agent)

    result = await runner._handle_message(event)

    assert result == "ok"
    assert run_agent_calls == [dict(run_agent_calls[0])]
    assert run_agent_calls[0]["skip_memory"] is True
    runner.session_store.get_or_create_session.assert_not_called()
    runner.session_store.load_transcript.assert_not_called()
    runner.session_store.append_to_transcript.assert_not_called()
    runner.session_store.update_session.assert_not_called()



@pytest.mark.asyncio
async def test_stateless_teams_failure_does_not_persist_transcript() -> None:
    runner, adapter = _runner()
    event = _event(_snapshot())
    session_key = build_session_key(event.source)
    runner._session_run_generation[session_key] = 1
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "provider failed",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model": "test-model",
            "failed": True,
            "error": "provider authentication failed",
        }
    )

    result = await runner._handle_message(event)

    assert result == "provider failed"
    runner.session_store.load_transcript.assert_not_called()
    runner.session_store.append_to_transcript.assert_not_called()
    runner.session_store.update_session.assert_not_called()



@pytest.mark.asyncio
async def test_stateless_teams_replaces_only_visible_history_snapshot() -> None:
    runner, adapter = _runner()
    event = _event(_snapshot())
    session_key = build_session_key(event.source)
    runner._session_run_generation[session_key] = 1
    captured = {}

    async def fake_run_agent(**kwargs):
        captured.update(kwargs)
        assert "conversation_persistence_policy" not in kwargs
        return _agent_result()

    runner._run_agent = AsyncMock(side_effect=fake_run_agent)

    await runner._handle_message(event)

    assert captured["history"] == []
    assert captured["message"].startswith("[Alice] [External conversation context")
    assert "External conversation context" in captured["message"]
    assert "Current authenticated request" in captured["message"]
    assert captured["message"].endswith("Graph context")


def test_canonical_teams_thread_lane_key_uses_graph_locator() -> None:
    snapshot = _snapshot()

    assert (
        _canonical_teams_thread_lane(snapshot)
        == "teams:tenant-1:team-1:channel-1:root-1"
    )

    bad_snapshot = _snapshot()
    bad_snapshot.metadata["team_id"] = ""
    assert _canonical_teams_thread_lane(bad_snapshot) is None


def test_work_experience_teams_boundary_uses_raw_authenticated_request() -> None:
    assert _work_experience_turn_kwargs(
        _teams_source(),
        raw_user_message="@Marlow plan?",
    ) == {
        "raw_user_message": "@Marlow plan?",
        "turn_origin": "teams",
    }

"""Route-isolation primitives for interactive messaging."""

import asyncio
import os
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gateway.config import Platform
from gateway.outbound import (
    DeliveryRecorder,
    OutboundDeliveryService,
    OutboundEnvelope,
    OutboundKind,
    OutboundPolicy,
    StagedAttachment,
    SuccessfulDelivery,
)
from gateway.pending_delivery import (
    PendingDelivery,
    PendingDeliveryState,
    approve,
    cancel,
    clear_session,
    claim,
    format_confirmation_text,
    get,
    make_request_id,
    payload_digest,
    register,
    register_gateway_notify,
    request_confirmation,
    resolve_from_event,
    stage_attachment,
    unregister_gateway_notify,
)
from gateway.session import SessionSource
from gateway.turn_context import (
    ActorIdentity,
    ConversationRoute,
    ExecutionMode,
    TurnContext,
)


def _turn(
    *,
    platform=Platform.TELEGRAM,
    chat_id="100",
    thread_id=None,
    user_id="u1",
    mode=ExecutionMode.INTERACTIVE,
    session_key="sess",
) -> TurnContext:
    return TurnContext(
        turn_id="turn-1",
        mode=mode,
        origin=ConversationRoute.from_parts(platform, chat_id, thread_id),
        actor=ActorIdentity(platform=platform, user_ids=frozenset({user_id})),
        session_key=session_key,
        session_id="session-1",
    )


def test_conversation_route_identity_normalizes_optional_ids():
    assert ConversationRoute.from_parts(Platform.TELEGRAM, "100", None) == ConversationRoute.from_parts(
        Platform.TELEGRAM,
        "100",
        "",
    )
    assert ConversationRoute.from_parts(Platform.TELEGRAM, "100", "1") != ConversationRoute.from_parts(
        Platform.TELEGRAM,
        "100",
        "2",
    )
    assert ConversationRoute.from_parts(Platform.TELEGRAM, "100", "2").to_target() == "telegram:100:2"


def test_origin_only_policy_allows_exact_origin_for_all_origin_kinds():
    turn = _turn()
    for kind in OutboundPolicy.origin_only_kinds():
        decision = OutboundPolicy.evaluate(turn, kind, turn.origin)
        assert decision.allowed is True
        assert decision.reason == "origin"


def test_origin_only_policy_denies_non_origin_destination():
    turn = _turn()
    destination = ConversationRoute.from_parts(Platform.TELEGRAM, "200")
    decision = OutboundPolicy.evaluate(turn, OutboundKind.REPLY, destination)
    assert decision.allowed is False
    assert decision.reason == "origin_only_destination_mismatch"


def test_interactive_cross_conversation_requires_concrete_non_origin_destination():
    turn = _turn()
    ambiguous = ConversationRoute.from_parts(Platform.TELEGRAM, "")
    assert OutboundPolicy.evaluate(turn, OutboundKind.CROSS_CONVERSATION, ambiguous).reason == "ambiguous_destination"
    assert OutboundPolicy.evaluate(turn, OutboundKind.CROSS_CONVERSATION, turn.origin).reason == "same_origin_reply"
    decision = OutboundPolicy.evaluate(turn, OutboundKind.CROSS_CONVERSATION, ConversationRoute.from_parts(Platform.TELEGRAM, "200"))
    assert decision.allowed is True
    assert decision.requires_confirmation is True
    assert decision.reason == "cross_conversation_requires_grant"


def test_scheduled_system_and_local_policy_modes():
    scheduled = TurnContext("turn", ExecutionMode.SCHEDULED)
    assert OutboundPolicy.evaluate(scheduled, OutboundKind.SCHEDULED, ConversationRoute.from_parts(Platform.TELEGRAM, "home")).allowed
    system = TurnContext("turn", ExecutionMode.SYSTEM)
    assert OutboundPolicy.evaluate(system, OutboundKind.SYSTEM_NOTICE, ConversationRoute.from_parts(Platform.TELEGRAM, "home")).allowed
    local = TurnContext("turn", ExecutionMode.LOCAL)
    assert not OutboundPolicy.evaluate(local, OutboundKind.CROSS_CONVERSATION, ConversationRoute.from_parts(Platform.TELEGRAM, "200")).allowed


def test_pending_delivery_register_approve_claim_once(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.pending_delivery.get_marlow_home", lambda: tmp_path)
    turn = _turn()
    destination = ConversationRoute.from_parts(Platform.TELEGRAM, "200")
    envelope = OutboundEnvelope("delivery-1", OutboundKind.CROSS_CONVERSATION, destination, "hello")
    entry = PendingDelivery(
        request_id="delivery-1",
        turn_id="turn-1",
        session_key="sess",
        origin=turn.origin,
        actor=turn.actor,
        destination=destination,
        payload_digest=payload_digest("hello", ()),
        envelope=envelope,
        created_at=1,
        expires_at=9999999999,
    )
    register_gateway_notify("sess", lambda payload: None)
    register(entry)
    assert approve("delivery-1", actor=turn.actor, route=turn.origin, session_key="sess")["success"]
    assert approve("delivery-1", actor=turn.actor, route=turn.origin, session_key="sess")["already_resolved"]
    assert claim("delivery-1") == envelope
    assert claim("delivery-1") is None
    unregister_gateway_notify("sess")


def test_pending_delivery_rejects_wrong_actor_route_and_session():
    turn = _turn()
    destination = ConversationRoute.from_parts(Platform.TELEGRAM, "200")
    entry = PendingDelivery(
        request_id="delivery-2",
        turn_id="turn-1",
        session_key="sess",
        origin=turn.origin,
        actor=turn.actor,
        destination=destination,
        payload_digest=payload_digest("hello", ()),
        envelope=OutboundEnvelope("delivery-2", OutboundKind.CROSS_CONVERSATION, destination, "hello"),
        created_at=1,
        expires_at=9999999999,
    )
    register(entry)
    assert approve("delivery-2", actor=ActorIdentity(Platform.TELEGRAM, frozenset({"u2"})), route=turn.origin, session_key="sess")["actor_mismatch"]
    assert approve("delivery-2", actor=turn.actor, route=destination, session_key="sess")["route_mismatch"]
    assert approve("delivery-2", actor=turn.actor, route=turn.origin, session_key="other")["route_mismatch"]
    unregister_gateway_notify("sess")


def test_resolve_from_event_rejects_missing_request_id_and_stale_callbacks(monkeypatch):
    event = SimpleNamespace(
        get_command=lambda: "send-approve",
        get_command_args=lambda: "",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="100", user_id="u1"),
    )
    assert "without a request ID" in resolve_from_event("sess", event)

    entry = PendingDelivery(
        request_id="delivery-stale",
        turn_id="turn-1",
        session_key="sess",
        origin=_turn().origin,
        actor=_turn().actor,
        destination=ConversationRoute.from_parts(Platform.TELEGRAM, "200"),
        payload_digest=payload_digest("hello", ()),
        envelope=OutboundEnvelope("delivery-stale", OutboundKind.CROSS_CONVERSATION, ConversationRoute.from_parts(Platform.TELEGRAM, "200"), "hello"),
        created_at=1,
        expires_at=9999999999,
    )
    register(entry)
    wrong_route_event = SimpleNamespace(
        get_command=lambda: "send-approve",
        get_command_args=lambda: "delivery-stale",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="999", user_id="u1"),
    )
    assert resolve_from_event("sess", wrong_route_event) == ""
    assert get("delivery-stale").state == PendingDeliveryState.PENDING
    assert clear_session("sess") == 1
    assert get("delivery-stale") is None


def test_cancel_and_clear_session_clean_staged_files(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.pending_delivery.get_marlow_home", lambda: tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("hello")
    entry = PendingDelivery(
        request_id="delivery-3",
        turn_id="turn-1",
        session_key="sess",
        origin=_turn().origin,
        actor=_turn().actor,
        destination=ConversationRoute.from_parts(Platform.TELEGRAM, "200"),
        payload_digest="",
        envelope=OutboundEnvelope("delivery-3", OutboundKind.CROSS_CONVERSATION, ConversationRoute.from_parts(Platform.TELEGRAM, "200"), "hello"),
        created_at=1,
        expires_at=9999999999,
    )
    entry.envelope = OutboundEnvelope(
        "delivery-3",
        OutboundKind.CROSS_CONVERSATION,
        entry.destination,
        "hello",
        attachments=(stage_attachment(str(source), entry.request_id),),
    )
    register(entry)
    assert (tmp_path / "pending_deliveries" / "delivery-3").exists()
    assert cancel("delivery-3")
    assert not (tmp_path / "pending_deliveries" / "delivery-3").exists()
    assert get("delivery-3") is None


@pytest.mark.asyncio
async def test_live_delivery_service_uses_runner_adapter_only():
    turn = _turn()
    destination = ConversationRoute.from_parts(Platform.TELEGRAM, "200")
    envelope = OutboundEnvelope("delivery-4", OutboundKind.CROSS_CONVERSATION, destination, "hello", grant_id="delivery-4")
    adapter = AsyncMock()
    adapter.send.return_value = MagicMock(success=True, message_id="m1", continuation_message_ids=())
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})
    result = await OutboundDeliveryService(runner=runner).send(turn, envelope)
    assert result["success"] is True
    assert result["message_id"] == "m1"
    adapter.send.assert_awaited_once_with(chat_id="200", content="hello", metadata=None)


@pytest.mark.asyncio
async def test_live_delivery_service_rejects_missing_adapter():
    turn = _turn()
    destination = ConversationRoute.from_parts(Platform.TELEGRAM, "200")
    envelope = OutboundEnvelope("delivery-5", OutboundKind.CROSS_CONVERSATION, destination, "hello", grant_id="delivery-5")
    result = await OutboundDeliveryService(runner=SimpleNamespace(adapters={})).send(turn, envelope)
    assert result["reason"] == "live_adapter_unavailable"


def test_delivery_recorder_records_after_success(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.mirror._SESSIONS_INDEX", tmp_path / "sessions.json")
    monkeypatch.setattr("gateway.mirror._SESSIONS_DIR", tmp_path)
    sessions = {
        "agent:main:telegram:dm": {
            "session_id": "target-session",
            "origin": {"platform": "telegram", "chat_id": "200"},
            "updated_at": "2026-01-01T00:00:00",
        }
    }
    (tmp_path / "sessions.json").write_text(json.dumps(sessions))
    destination = ConversationRoute.from_parts(Platform.TELEGRAM, "200")
    with patch("marlow_state.SessionDB") as db_cls:
        db = MagicMock()
        db_cls.return_value = db
        result = DeliveryRecorder().record_success(SuccessfulDelivery("delivery-6", destination, "hello", ("m1",)))
    assert result.mirrored is True
    db.append_message.assert_called_once()
    call = db.append_message.call_args
    assert call.kwargs["session_id"] == "target-session"
    assert call.kwargs["role"] == "assistant"
    assert "[Delivered from gateway] hello" in call.kwargs["content"]


def test_confirmation_text_contains_destination_preview_and_request_id():
    text = format_confirmation_text({
        "request_id": "delivery-7",
        "destination": "telegram:200",
        "preview": "hello",
        "attachment_count": 0,
    })
    assert "telegram:200" in text
    assert "hello" in text
    assert "delivery-7" in text
    assert "Nothing has been sent yet" in text
    assert "/send-approve" in text
    assert "/send-cancel" in text

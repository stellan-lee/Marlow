"""Tests for retained send_message targets and routing."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform
from gateway.turn_context import (
    ActorIdentity,
    ConversationRoute,
    ExecutionMode,
    TurnContext,
    reset_current_turn,
    set_current_turn,
)
from tools.send_message_tool import (
    _parse_target_ref, _sanitize_error_text, _send_to_platform,
    _telegram_retry_delay, send_message_tool,
)


def test_retained_explicit_target_formats():
    assert _parse_target_ref("telegram", "-100123:42") == ("-100123", "42", True)
    assert _parse_target_ref("discord", "123456789:987654321") == ("123456789", "987654321", True)
    assert _parse_target_ref("slack", "C12345678") == ("C12345678", None, True)
    assert _parse_target_ref("feishu", "oc_chat123:thread_1") == ("oc_chat123", "thread_1", True)
    assert _parse_target_ref("email", "user@example.com") == ("user@example.com", None, True)


def test_unknown_name_requires_directory_resolution():
    assert _parse_target_ref("telegram", "general") == (None, None, False)


def test_error_sanitization_redacts_query_secrets():
    text = _sanitize_error_text("failed https://x.test?a=1&token=secret")
    assert "secret" not in text
    assert "token=***" in text


def test_transient_telegram_retry_policy():
    assert _telegram_retry_delay(RuntimeError("502 Bad Gateway"), 1) == 2.0
    assert _telegram_retry_delay(RuntimeError("invalid chat"), 0) is None


def test_list_action_uses_channel_directory():
    with patch("gateway.channel_directory.format_directory_for_display", return_value="targets"):
        result = send_message_tool({"action": "list"})
    assert "targets" in result


def test_send_requires_target_and_message():
    result = send_message_tool({"action": "send", "target": "telegram"})
    assert "required" in result.lower()


def _interactive_turn(chat_id="100"):
    return TurnContext(
        turn_id="turn-test",
        mode=ExecutionMode.INTERACTIVE,
        origin=ConversationRoute.from_parts(Platform.TELEGRAM, chat_id),
        actor=ActorIdentity(platform=Platform.TELEGRAM, user_ids=frozenset({"u1"})),
        session_key="test-session",
        session_id="test-session",
    )


def test_interactive_bare_platform_does_not_resolve_home():
    token = set_current_turn(_interactive_turn())
    try:
        with patch("gateway.config.load_gateway_config") as load_config:
            result = json.loads(send_message_tool({"action": "send", "target": "telegram", "message": "hello"}))
        assert result == {
            "success": False,
            "policy_denied": True,
            "reason": "ambiguous_destination",
        }
        load_config.assert_not_called()
    finally:
        reset_current_turn(token)


def test_interactive_same_origin_send_is_rejected_as_reply_path():
    token = set_current_turn(_interactive_turn(chat_id="100"))
    config = SimpleNamespace(
        platforms={Platform.TELEGRAM: SimpleNamespace(enabled=True)},
        get_home_channel=lambda platform: SimpleNamespace(chat_id="home", thread_id=None),
    )
    try:
        with patch("gateway.config.load_gateway_config", return_value=config):
            result = json.loads(send_message_tool({"action": "send", "target": "telegram:100", "message": "hello"}))
        assert result["error"] == "same_origin_reply"
        assert "Do not use send_message" in result["message"]
    finally:
        reset_current_turn(token)


def test_interactive_cross_conversation_confirms_once_then_sends_live(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.pending_delivery.get_marlow_home", lambda: tmp_path)
    turn = _interactive_turn(chat_id="100")
    token = set_current_turn(turn)
    config = SimpleNamespace(
        platforms={Platform.TELEGRAM: SimpleNamespace(enabled=True)},
        get_home_channel=lambda platform: SimpleNamespace(chat_id="home-1", thread_id=None),
    )
    sender = AsyncMock(return_value={"success": True, "message_id": "m1"})
    try:
        from gateway.outbound import RecordResult
        from gateway.pending_delivery import (
            approve,
            register_gateway_notify,
            unregister_gateway_notify,
        )

        register_gateway_notify("test-session", lambda payload: approve(
            payload["request_id"],
            actor=turn.actor,
            route=turn.origin,
            session_key="test-session",
        ))
        try:
            with patch("gateway.config.load_gateway_config", return_value=config),                  patch("tools.send_message_tool._send_to_platform", sender),                  patch("tools.send_message_tool._send_via_adapter", sender),                  patch("gateway.outbound.OutboundDeliveryService.send", return_value={
                     "success": True,
                     "message_id": "m1",
                     "message_ids": ["m1"],
                 }) as send_service,                  patch("gateway.outbound.DeliveryRecorder.record_success", return_value=RecordResult("delivery-x", mirrored=False)) as record_success:
                result = json.loads(send_message_tool({
                    "action": "send",
                    "target": "telegram:home",
                    "message": "hello",
                }))
        finally:
            unregister_gateway_notify("test-session")

        assert result["success"] is True
        assert result["confirmed"] is True
        assert result["target"] == "telegram:home-1"
        sender.assert_not_called()
        send_service.assert_awaited_once()
        envelope = send_service.await_args.args[1]
        assert envelope.destination.public_label() == "telegram:home-1"
        assert envelope.text == "hello"
        assert envelope.grant_id == envelope.delivery_id
        record_success.assert_called_once()
    finally:
        reset_current_turn(token)


def test_interactive_cross_conversation_cancel_does_not_send_or_mirror(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.pending_delivery.get_marlow_home", lambda: tmp_path)
    turn = _interactive_turn(chat_id="100")
    token = set_current_turn(turn)
    config = SimpleNamespace(
        platforms={Platform.TELEGRAM: SimpleNamespace(enabled=True)},
        get_home_channel=lambda platform: SimpleNamespace(chat_id="home-1", thread_id=None),
    )
    sender = AsyncMock(return_value={"success": True, "message_id": "m1"})
    try:
        from gateway.pending_delivery import (
            cancel,
            register_gateway_notify,
            unregister_gateway_notify,
        )

        register_gateway_notify("test-session", lambda payload: cancel(payload["request_id"]))
        try:
            with patch("gateway.config.load_gateway_config", return_value=config),                  patch("tools.send_message_tool._send_to_platform", sender),                  patch("tools.send_message_tool._send_via_adapter", sender),                  patch("gateway.outbound.OutboundDeliveryService.send") as send_service,                  patch("gateway.outbound.DeliveryRecorder.record_success") as record_success:
                result = json.loads(send_message_tool({
                    "action": "send",
                    "target": "telegram:200",
                    "message": "hello",
                }))
        finally:
            unregister_gateway_notify("test-session")

        assert result["success"] is False
        assert result.get("cancelled") is True or result.get("cancelled") is None
        assert result.get("delivery_id", "").startswith("delivery-")
        sender.assert_not_called()
        send_service.assert_not_called()
        record_success.assert_not_called()
    finally:
        reset_current_turn(token)


def test_interactive_cross_conversation_duplicate_approval_does_not_resend(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.pending_delivery.get_marlow_home", lambda: tmp_path)
    turn = _interactive_turn(chat_id="100")
    token = set_current_turn(turn)
    config = SimpleNamespace(
        platforms={Platform.TELEGRAM: SimpleNamespace(enabled=True)},
        get_home_channel=lambda platform: SimpleNamespace(chat_id="home-1", thread_id=None),
    )
    sender = AsyncMock(return_value={"success": True, "message_id": "m1"})
    try:
        from gateway.outbound import RecordResult
        from gateway.pending_delivery import (
            approve,
            register_gateway_notify,
            unregister_gateway_notify,
        )

        def approve_twice(payload):
            first = approve(
                payload["request_id"],
                actor=turn.actor,
                route=turn.origin,
                session_key="test-session",
            )
            second = approve(
                payload["request_id"],
                actor=turn.actor,
                route=turn.origin,
                session_key="test-session",
            )
            assert first["success"] is True
            assert second["already_resolved"] is True

        register_gateway_notify("test-session", approve_twice)
        try:
            with patch("gateway.config.load_gateway_config", return_value=config),                  patch("tools.send_message_tool._send_to_platform", sender),                  patch("tools.send_message_tool._send_via_adapter", sender),                  patch("gateway.outbound.OutboundDeliveryService.send", return_value={
                     "success": True,
                     "message_id": "m1",
                     "message_ids": ["m1"],
                 }) as send_service,                  patch("gateway.outbound.DeliveryRecorder.record_success", return_value=RecordResult("delivery-x", mirrored=False)):
                result = json.loads(send_message_tool({
                    "action": "send",
                    "target": "telegram:200",
                    "message": "hello",
                }))
        finally:
            unregister_gateway_notify("test-session")

        assert result["success"] is True
        assert result["confirmed"] is True
        sender.assert_not_called()
        send_service.assert_awaited_once()
    finally:
        reset_current_turn(token)


def test_non_interactive_bare_platform_still_uses_home_channel():
    config = SimpleNamespace(
        platforms={Platform.TELEGRAM: SimpleNamespace(enabled=True)},
        get_home_channel=lambda platform: SimpleNamespace(chat_id="home-1", thread_id=None),
    )
    sender = AsyncMock(return_value={"success": True, "message_id": "m1"})
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.send_message_tool._send_to_platform", sender):
        result = json.loads(send_message_tool({"action": "send", "target": "telegram", "message": "hello"}))
    assert result["success"] is True
    sender.assert_awaited_once()
    assert sender.await_args.args[0] is Platform.TELEGRAM
    assert sender.await_args.args[2] == "home-1"


def test_non_interactive_unresolved_target_keeps_legacy_error():
    config = SimpleNamespace(
        platforms={Platform.TELEGRAM: SimpleNamespace(enabled=True)},
        get_home_channel=lambda platform: SimpleNamespace(chat_id="home", thread_id=None),
    )
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("gateway.channel_directory.resolve_channel_name", return_value=None):
        result = json.loads(send_message_tool({"action": "send", "target": "telegram:unknown", "message": "hello"}))
    assert "Could not resolve" in result["error"]


def test_telegram_routes_to_native_sender():
    cfg = SimpleNamespace(token="token", extra={})
    sender = AsyncMock(return_value={"success": True, "platform": "telegram"})
    with patch("tools.send_message_tool._send_telegram", sender):
        result = asyncio.run(_send_to_platform(Platform.TELEGRAM, cfg, "123", "hello"))
    assert result["success"] is True
    sender.assert_awaited_once()


def test_slack_routes_to_native_sender():
    cfg = SimpleNamespace(token="token", extra={})
    sender = AsyncMock(return_value={"success": True, "platform": "slack"})
    with patch("tools.send_message_tool._send_slack", sender):
        result = asyncio.run(_send_to_platform(Platform.SLACK, cfg, "C12345678", "hello"))
    assert result["success"] is True
    sender.assert_awaited_once()


def test_email_routes_to_native_sender():
    cfg = SimpleNamespace(token="", extra={})
    sender = AsyncMock(return_value={"success": True, "platform": "email"})
    with patch("tools.send_message_tool._send_email", sender):
        result = asyncio.run(_send_to_platform(Platform.EMAIL, cfg, "u@example.com", "hello"))
    assert result["success"] is True
    sender.assert_awaited_once()


def test_feishu_routes_to_native_sender():
    cfg = SimpleNamespace(token="", extra={})
    sender = AsyncMock(return_value={"success": True, "platform": "feishu"})
    with patch("tools.send_message_tool._send_feishu", sender):
        result = asyncio.run(_send_to_platform(Platform.FEISHU, cfg, "oc_test", "hello"))
    assert result["success"] is True
    sender.assert_awaited_once()

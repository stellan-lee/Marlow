"""Focused tests for the bundled Microsoft Teams platform adapter."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

teams = load_plugin_adapter("teams")

CLIENT_ID = "11111111-1111-4111-8111-111111111111"
TENANT_ID = "22222222-2222-4222-8222-222222222222"
USER_ID = "33333333-3333-4333-8333-333333333333"
OTHER_USER_ID = "44444444-4444-4444-8444-444444444444"
BOT_ID = CLIENT_ID
SERVICE_URL = "https://smba.trafficmanager.net/teams"


def _cfg(**extra):
    base = {
        "enabled": True,
        "client_id": CLIENT_ID,
        "tenant_id": TENANT_ID,
        "client_secret": "secret",
        "allowed_users": [USER_ID],
        "allow_all_users": False,
        "host": "127.0.0.1",
        "port": 3978,
    }
    base.update(extra)
    return PlatformConfig(extra=base)


def _account(user_id=USER_ID, *, name="Alice", type="person"):
    return teams.Account(id=user_id, aad_object_id=user_id, type=type, name=name)


def _bot():
    return teams.Account(id=CLIENT_ID, aad_object_id=CLIENT_ID, type="bot", name="Marlow")


def _conversation(conversation_type="personal", *, conversation_id="conv-1", tenant_id=TENANT_ID):
    return teams.ConversationAccount(
        id=conversation_id,
        tenant_id=tenant_id,
        conversation_type=conversation_type,
    )


def _activity(
    *,
    activity_id="msg-1",
    conversation_type="personal",
    conversation_id="conv-1",
    text="hello",
    tenant_id=TENANT_ID,
    user_id=USER_ID,
    entities=None,
    channel_data=None,
    attachments=None,
    service_url=SERVICE_URL,
):
    data = {
        "serviceUrl": service_url,
        "channelId": "msteams",
        "from": _account(user_id).model_dump(mode="json", exclude_none=True),
        "conversation": _conversation(conversation_type, conversation_id=conversation_id, tenant_id=tenant_id).model_dump(mode="json", exclude_none=True),
        "recipient": _bot().model_dump(mode="json", exclude_none=True),
        "type": "message",
        "id": activity_id,
    }
    if text is not None:
        data["text"] = text
    if entities:
        data["entities"] = entities
    if channel_data is not None:
        data["channelData"] = channel_data
    if attachments is not None:
        data["attachments"] = attachments
    return teams.Activity.model_validate(data)


def _mention(user_id=CLIENT_ID, *, text="Marlow"):
    return teams.MentionEntity(
        type="mention",
        mentioned=_account(user_id, name=text, type="bot"),
        text=text,
    ).model_dump(mode="json", exclude_none=True)


def _make_adapter(**extra):
    return teams.TeamsPlatformAdapter(_cfg(**extra))


def _start_supervisor(adapter, *, max_active=1, max_pending=1):
    adapter._supervisor = teams.TeamsDispatchSupervisor(max_active, max_pending)
    adapter._supervisor.start_worker(adapter._dispatch_one)
    return adapter


def _approval_activity(*, user_id=USER_ID, tenant_id=TENANT_ID, conversation_id="conv-1", conversation_type="personal", channel_data=None):
    return _activity(
        activity_id="invoke-1",
        user_id=user_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        text=None,
        entities=[_mention(user_id=CLIENT_ID, text="Marlow")],
        channel_data=channel_data,
    )


# ---------------------------------------------------------------------------
# Configuration and helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra,match",
    [
        ({"client_id": "not-a-uuid"}, "teams.client_id"),
        ({"tenant_id": "not-a-uuid"}, "teams.tenant_id"),
        ({"client_secret": ""}, "TEAMS_CLIENT_SECRET"),
        ({"port": 70000}, "teams.port"),
        ({"allowed_users": ["not-a-uuid"]}, "teams.allowed_users"),
        ({"allow_all_users": "yes"}, "teams.allow_all_users"),
    ],
)
def test_enabled_config_validation_rejects_invalid_values(extra, match):
    with pytest.raises(ValueError, match=match):
        teams.TeamsPlatformAdapter(_cfg(**extra))._validate_config()


def test_disabled_config_allows_placeholder_values():
    adapter = teams.TeamsPlatformAdapter(_cfg(enabled=False, client_id="", tenant_id="", client_secret=""))
    assert adapter._validate_config() is None
    assert adapter._enabled is False


def test_normalize_allowed_users_deduplicates_and_normalizes():
    adapter = teams.TeamsPlatformAdapter(_cfg(allowed_users=[CLIENT_ID.upper(), CLIENT_ID.lower()]))
    assert adapter._allowed_users == [CLIENT_ID]


# ---------------------------------------------------------------------------
# Identity, tenant, mention, and source validation
# ---------------------------------------------------------------------------


def test_sender_identity_uses_aad_object_id_not_display_name():
    adapter = _make_adapter()
    activity = _activity(text="hello", entities=[_mention(text="Marlow")])
    assert adapter._validate_activity(activity) is True
    assert adapter._authorize_activity(activity) is True

    activity.from_.aad_object_id = ""
    assert adapter._validate_activity(activity) is False
    assert adapter._authorize_activity(activity) is False



def test_teams_platform_allowlist_and_allow_all_use_aad_object_id():
    adapter = _make_adapter(allowed_users=[OTHER_USER_ID], allow_all_users=False)
    assert adapter._authorize_activity(_activity(user_id=USER_ID)) is False
    assert adapter._authorize_activity(_activity(user_id=OTHER_USER_ID)) is True

    adapter = _make_adapter(allowed_users=[OTHER_USER_ID], allow_all_users=True)
    assert adapter._authorize_activity(_activity(user_id=USER_ID)) is True


def test_tenant_mismatch_is_rejected_and_logged(caplog):
    adapter = _make_adapter()
    activity = _activity(tenant_id=OTHER_USER_ID)
    with caplog.at_level("WARNING"):
        assert adapter._validate_activity(activity) is False
    assert "tenant mismatch" in caplog.text


def test_personal_group_channel_sources_are_separated():
    adapter = _make_adapter()

    personal = adapter._build_source(_activity(conversation_type="personal", conversation_id="dm-1"))
    assert personal.chat_type == "dm"
    assert personal.thread_id is None

    group = adapter._build_source(_activity(conversation_type="groupChat", conversation_id="group-1"))
    assert group.chat_type == "group"
    assert group.thread_id is None

    channel = adapter._build_source(
        _activity(
            conversation_type="channel",
            conversation_id="thread-1",
            channel_data={"team": {"id": "team-1"}, "channel": {"id": "chan-1", "type": "standard"}},
            text=None,
        )
    )
    assert channel.chat_type == "channel"
    assert channel.thread_id == "thread-1"
    assert "team-1" in channel.chat_id
    assert "chan-1" in channel.chat_id
    assert channel.metadata is None


def test_teams_source_metadata_is_preserved_for_adapter_sends():
    adapter = _make_adapter()
    ref = teams._sdk_conversation_reference(SERVICE_URL, _bot(), _conversation(conversation_type="channel", conversation_id="thread-1"))
    source = adapter._build_source(
        _activity(
            conversation_type="channel",
            conversation_id="thread-1",
            channel_data={"team": {"id": "team-1"}, "channel": {"id": "chan-1", "type": "standard"}},
            text=None,
        ),
        reference=ref,
    )

    from gateway.platforms.base import _thread_metadata_for_source

    metadata = _thread_metadata_for_source(source)
    assert metadata is not None
    assert metadata["teams_reference"]["conversation"]["id"] == "thread-1"
    assert metadata["thread_id"] == "thread-1"


def test_group_and_channel_require_structured_bot_mention():
    adapter = _make_adapter()
    group = _activity(conversation_type="groupChat", text="hello Marlow")
    assert adapter._validate_activity(group) is False

    group.entities = [_mention()]
    assert adapter._validate_activity(group) is True

    channel = _activity(
        conversation_type="channel",
        text="hello Marlow",
        channel_data={"team": {"id": "team-1"}, "channel": {"id": "chan-1", "type": "standard"}},
    )
    assert adapter._validate_activity(channel) is False
    channel.entities = [_mention()]
    assert adapter._validate_activity(channel) is True


def test_plain_bot_name_does_not_count_as_mention():
    adapter = _make_adapter()
    activity = _activity(conversation_type="groupChat", text="hello Marlow")
    assert teams._activity_mentions_bot(activity, CLIENT_ID) is False
    assert adapter._validate_activity(activity) is False


def test_bot_mention_is_stripped_but_other_mentions_remain():
    activity = _activity(
        conversation_type="groupChat",
        text="Marlow please answer <at>Bob</at>",
        entities=[
            _mention(text="Marlow"),
            _mention(user_id=OTHER_USER_ID, text="<at>Bob</at>"),
        ],
    )
    text, matched = teams._strip_bot_mentions(activity, CLIENT_ID)
    assert matched is True
    assert text == "please answer <at>Bob</at>"


# ---------------------------------------------------------------------------
# Receipts, supervisor, and async handoff
# ---------------------------------------------------------------------------


def test_receipt_store_claims_duplicates_collisions_and_expires(tmp_path):
    store = teams.TeamsReceiptStore(tmp_path)
    claim, accepted = store.claim("key", "payload-a", 7)
    assert (claim, accepted) == ("claimed", True)
    assert "payload-a" in (tmp_path / "teams_receipts.json").read_text()
    assert "hello" not in (tmp_path / "teams_receipts.json").read_text()

    assert store.claim("key", "payload-a", 7) == ("duplicate", False)
    assert store.claim("key", "payload-b", 7) == ("collision", False)

    expired = tmp_path / "teams_receipts.json"
    data = json.loads(expired.read_text())
    data["records"]["key"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    expired.write_text(json.dumps(data))
    assert store.cleanup_expired() == 1


@pytest.mark.asyncio
async def test_supervisor_saturates_without_unbounded_tasks():
    supervisor = teams.TeamsDispatchSupervisor(max_active=1, max_pending=1)
    seen = []

    async def handler(task):
        seen.append(task.receipt_key)
        await asyncio.Event().wait()

    supervisor.start_worker(handler)
    first = teams.TeamsDispatchTask(
        activity=None,
        event=MagicMock(),
        reference=None,
        receipt_key="first",
        payload_hash="hash",
    )
    second = teams.TeamsDispatchTask(
        activity=None,
        event=MagicMock(),
        reference=None,
        receipt_key="second",
        payload_hash="hash",
    )
    third = teams.TeamsDispatchTask(
        activity=None,
        event=MagicMock(),
        reference=None,
        receipt_key="third",
        payload_hash="hash",
    )
    assert await supervisor.submit(first) is True
    await asyncio.sleep(0)
    assert supervisor.capacity.active == 1
    assert await supervisor.submit(second) is True
    assert await supervisor.submit(third) is False
    await supervisor.stop()
    assert seen == ["first"]


@pytest.mark.asyncio
async def test_handle_teams_activity_dispatches_once_and_returns_200(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(), max_active=2, max_pending=2)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    events = []
    adapter.set_message_handler(lambda event: events.append(event))

    response = await adapter._handle_teams_activity(MagicMock(body=_activity()))
    assert response.status == 200
    await asyncio.sleep(0.05)
    assert len(events) == 1
    assert events[0].text == "hello"

    response = await adapter._handle_teams_activity(MagicMock(body=_activity()))
    assert response.status == 200
    await asyncio.sleep(0.05)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_supervisor_saturation_returns_503_without_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _make_adapter(dispatch_max_active=0, dispatch_max_pending=0)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    response = await adapter._handle_teams_activity(MagicMock(body=_activity()))
    assert response.status == 503
    assert (tmp_path / "teams" / "receipts" / "teams_receipts.json").exists() is False


def test_aiohttp_adapter_to_response_handles_sdk_response_shapes():
    from aiohttp import web

    adapter = teams.TeamsAiohttpAdapter(web.Application())

    http_response = adapter._to_response(teams.HttpResponse(body={"ok": True}))
    assert http_response.status == 200
    assert http_response.text == '{"ok": true}'

    invoke_response = adapter._to_response(teams.InvokeResponse(status=200, body={"success": True}))
    assert invoke_response.status == 200
    assert invoke_response.text == '{"success": true}'

    invoke_without_body = adapter._to_response(teams.InvokeResponse(status=200))
    assert invoke_without_body.status == 200
    assert invoke_without_body.body is None


@pytest.mark.asyncio
async def test_sdk_initialize_overwrites_handler_and_restore_keeps_marlow_authority():
    adapter = _make_adapter()
    adapter._build_http_app()
    adapter._build_sdk_app()

    assert adapter._teams_app.server.on_request.__func__ is adapter._handle_teams_activity.__func__
    assert adapter._teams_app.server.on_request.__self__ is adapter
    await adapter._teams_app.initialize()
    assert adapter._teams_app.server.on_request.__func__ is not adapter._handle_teams_activity.__func__
    assert adapter._teams_app.server.on_request.__name__ == "_process_activity_event"

    adapter._restore_teams_handler()
    assert adapter._teams_app.server.on_request.__func__ is adapter._handle_teams_activity.__func__
    assert adapter._teams_app.server.on_request.__self__ is adapter
    await adapter._teams_app.stop()


# ---------------------------------------------------------------------------
# Outbound text, retries, and images
# ---------------------------------------------------------------------------


def test_text_chunking_prefers_boundaries_and_is_utf16_safe():
    adapter = _make_adapter(text_budget_bytes=10)
    content = "alpha beta gamma delta"
    chunks = adapter._chunk_text(content)
    assert chunks == ["alpha", "beta", "gamma", "delta"]

    emoji_content = "🙂" * 20
    adapter = _make_adapter(text_budget_bytes=10)
    chunks = adapter._chunk_text(emoji_content)
    assert all(teams._utf16_len(chunk) <= 10 for chunk in chunks)
    assert "".join(chunks) == emoji_content


@pytest.mark.asyncio
async def test_send_returns_last_message_id_and_chunks(monkeypatch):
    adapter = _make_adapter(text_budget_bytes=10)
    adapter._teams_app = MagicMock()
    adapter._teams_app.activity_sender.send = AsyncMock(
        side_effect=[
            SimpleNamespace(id="m1"),
            SimpleNamespace(id="m2"),
            SimpleNamespace(id="m3"),
            SimpleNamespace(id="m4"),
        ]
    )

    ref = teams._sdk_conversation_reference(SERVICE_URL, _bot(), _conversation())
    result = await adapter.send("chat", "alpha beta gamma delta", metadata={"teams_reference": teams._conversation_reference_dict(ref)})
    assert result.success is True
    assert result.message_id == "m4"
    assert adapter._teams_app.activity_sender.send.call_count == 4


@pytest.mark.asyncio
async def test_retry_delay_honors_retry_after(monkeypatch):
    adapter = _make_adapter(outbound_base_delay=10)
    monkeypatch.setattr(teams.asyncio, "sleep", AsyncMock())
    adapter._teams_app = MagicMock()
    adapter._teams_app.activity_sender.send = AsyncMock(
        side_effect=[
            Exception("timeout 429 retry-after=1.5"),
            Exception("timeout"),
            MagicMock(id="ok"),
        ]
    )

    result = await adapter._send_activity(MagicMock(), teams._sdk_message_activity("x"))

    assert result.success is True
    assert result.message_id == "ok"
    assert adapter._teams_app.activity_sender.send.call_count == 3
    assert teams.asyncio.sleep.await_args_list[0].args == (1.5,)
    assert teams.asyncio.sleep.await_args_list[1].args == (20.0,)


def test_send_image_rejects_http_url_without_graph_fallback():
    adapter = _make_adapter()
    result = asyncio.run(adapter.send_image("chat", "https://example.com/image.png"))
    assert result.success is False
    assert "validated local files or data URLs" in result.error


def test_send_image_accepts_valid_data_url(monkeypatch, tmp_path):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _make_adapter()
    data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")
    result = asyncio.run(adapter.send_image("chat", data_url))
    assert result.success is False
    assert result.error == "Missing Teams conversation reference"
    cached = teams.cache_image_from_bytes(b"\x89PNG\r\n\x1a\n", ext=".png")
    assert Path(cached).is_file()


# ---------------------------------------------------------------------------
# Media fetch and approval callbacks
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data=b"png-bytes", *, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "image/png"}
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def aiter_bytes(self, chunk_size):
        for start in range(0, len(self._data), chunk_size):
            yield self._data[start : start + chunk_size]


@pytest.mark.asyncio
async def test_fetch_image_attachment_uses_sdk_client_and_safe_https(monkeypatch, tmp_path):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _make_adapter()
    adapter._is_safe_attachment_url = lambda url: True
    adapter._teams_app = MagicMock()
    adapter._teams_app.http_client.get = AsyncMock(return_value=_FakeResponse(b"\x89PNG\r\n\x1a\n"))

    path, mime_type = await adapter._fetch_image_attachment(
        teams.Attachment(content_type="image/png", content_url="https://teams.example/media.png"),
        "https://teams.example/media.png",
    )

    adapter._teams_app.http_client.get.assert_awaited_once()
    assert mime_type == "image/png"
    assert Path(path).is_file()


@pytest.mark.asyncio
async def test_fetch_image_attachment_rejects_redirect(monkeypatch, tmp_path):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _make_adapter()
    adapter._is_safe_attachment_url = lambda url: True
    adapter._teams_app = MagicMock()
    adapter._teams_app.http_client.get = AsyncMock(return_value=_FakeResponse(b"png", status_code=302, headers={"Location": "https://cdn.example/media.png"}))

    with pytest.raises(ValueError, match="redirect"):
        await adapter._fetch_image_attachment(
            teams.Attachment(content_type="image/png", content_url="https://teams.example/media.png"),
            "https://teams.example/media.png",
        )


@pytest.mark.asyncio
async def test_resolve_approval_validates_route_and_resolves_once(monkeypatch, tmp_path):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _make_adapter()
    nonce = "nonce-value"
    future = asyncio.get_running_loop().create_future()
    adapter._approval_state[nonce] = {
        "platform": "teams",
        "request_id": "request-1",
        "session_key": "session-1",
        "user_id": USER_ID,
        "tenant_id": TENANT_ID,
        "chat_id": adapter._build_source(_approval_activity()).chat_id,
        "thread_id": "",
        "expires_at": teams._utc_now() + teams.timedelta(seconds=60),
    }
    adapter._approval_waiters[nonce] = future

    resolve = MagicMock(return_value=1)
    monkeypatch.setattr("tools.approval.resolve_gateway_approval", resolve)

    response = await adapter._resolve_approval(_approval_activity(), "request-1", nonce, "approve")

    assert response.status == 200
    resolve.assert_called_once_with("session-1", "once", request_id="request-1")
    assert await future == "once"
    assert nonce not in adapter._approval_state

    response = await adapter._resolve_approval(_approval_activity(), "request-1", nonce, "approve")
    assert response.status == 400
    resolve.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "activity,chat_id,thread_id",
    [
        (_approval_activity(user_id=OTHER_USER_ID), _make_adapter()._build_source(_approval_activity()).chat_id, ""),
        (_approval_activity(tenant_id=OTHER_USER_ID), _make_adapter()._build_source(_approval_activity()).chat_id, ""),
        (
            _approval_activity(
                conversation_type="channel",
                conversation_id="thread-2",
                channel_data={"team": {"id": "team-1"}, "channel": {"id": "chan-1", "type": "standard"}},
            ),
            _make_adapter()._build_source(
                _approval_activity(
                    conversation_type="channel",
                    conversation_id="thread-1",
                    channel_data={"team": {"id": "team-1"}, "channel": {"id": "chan-1", "type": "standard"}},
                )
            ).chat_id,
            "thread-1",
        ),
    ],
)
async def test_resolve_approval_rejects_wrong_route(monkeypatch, tmp_path, activity, chat_id, thread_id):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _make_adapter()
    nonce = "nonce-value"
    adapter._approval_state[nonce] = {
        "platform": "teams",
        "request_id": "request-1",
        "session_key": "session-1",
        "user_id": USER_ID,
        "tenant_id": TENANT_ID,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "expires_at": teams._utc_now() + teams.timedelta(seconds=60),
    }
    adapter._approval_waiters[nonce] = asyncio.get_running_loop().create_future()
    monkeypatch.setattr("tools.approval.resolve_gateway_approval", MagicMock())

    response = await adapter._resolve_approval(activity, "request-1", nonce, "approve")

    assert response.status == 400


@pytest.mark.asyncio
async def test_shutdown_fails_pending_approvals_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _make_adapter()
    future = asyncio.get_running_loop().create_future()
    adapter._approval_waiters["nonce"] = future
    adapter._approval_state["nonce"] = {
        "session_key": "session-1",
        "request_id": "request-1",
    }
    resolve = MagicMock(return_value=1)
    monkeypatch.setattr("tools.approval.resolve_gateway_approval", resolve)

    await adapter._fail_approval_waiters()

    assert await future == "deny"
    resolve.assert_called_once_with("session-1", "deny", request_id="request-1")


@pytest.mark.asyncio
async def test_delivery_failure_denies_local_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _make_adapter()
    future = asyncio.get_running_loop().create_future()
    adapter._approval_waiters["nonce"] = future
    adapter._approval_state["nonce"] = {
        "session_key": "session-1",
        "request_id": "request-1",
    }
    resolve = MagicMock(return_value=1)
    monkeypatch.setattr("tools.approval.resolve_gateway_approval", resolve)

    await adapter._deny_local_approval("nonce", reason="delivery failed")

    assert await future == "deny"
    resolve.assert_called_once_with("session-1", "deny", request_id="request-1")


# ---------------------------------------------------------------------------
# Plugin registration and toolset exposure
# ---------------------------------------------------------------------------


def test_plugin_registration_and_generated_toolset(monkeypatch):
    from marlow_cli.plugins import discover_plugins
    from gateway.platform_registry import platform_registry
    from toolsets import resolve_toolset, validate_toolset

    discover_plugins()
    assert platform_registry.is_registered("teams") is True
    assert validate_toolset("marlow-teams") is True
    assert validate_toolset("marlow-unknown") is False
    assert len(resolve_toolset("marlow-teams")) > 0

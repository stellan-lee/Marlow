"""Focused tests for the bundled Microsoft Teams platform adapter."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.platforms.base as platform_base
from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, render_external_conversation_snapshot

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


def _bot(account_id=CLIENT_ID):
    return teams.Account(id=account_id, aad_object_id=CLIENT_ID, type="bot", name="Marlow")


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
    recipient_id=CLIENT_ID,
):
    data = {
        "serviceUrl": service_url,
        "channelId": "msteams",
        "from": _account(user_id).model_dump(mode="json", exclude_none=True),
        "conversation": _conversation(conversation_type, conversation_id=conversation_id, tenant_id=tenant_id).model_dump(mode="json", exclude_none=True),
        "recipient": _bot(recipient_id).model_dump(mode="json", exclude_none=True),
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


def test_teams_channel_account_mention_matches_recipient_and_preserves_other_mentions():
    channel_account_id = f"28:{CLIENT_ID}"
    adapter = _make_adapter()
    activity = _activity(
        conversation_type="channel",
        text="Marlow please answer <at>Bob</at>",
        recipient_id=channel_account_id,
        entities=[
            _mention(user_id=channel_account_id, text="Marlow"),
            _mention(user_id=OTHER_USER_ID, text="<at>Bob</at>"),
        ],
        channel_data={"team": {"id": "team-1"}, "channel": {"id": "chan-1", "type": "standard"}},
    )

    assert adapter._validate_activity(activity) is True
    text, matched = teams._strip_bot_mentions(activity, CLIENT_ID)
    assert matched is True
    assert text == "please answer <at>Bob</at>"


def test_configured_client_id_fallback_preserves_bare_mentions():
    activity = _activity(
        conversation_type="groupChat",
        text="Marlow please answer",
        recipient_id=f"28:{CLIENT_ID}",
        entities=[_mention(text="Marlow")],
    )

    assert teams._activity_mentions_bot(activity, CLIENT_ID) is True
    text, matched = teams._strip_bot_mentions(activity, CLIENT_ID)
    assert matched is True
    assert text == "please answer"


def test_conflicting_recipient_id_does_not_fall_back_to_configured_client_id():
    activity = _activity(
        conversation_type="groupChat",
        recipient_id=f"28:{OTHER_USER_ID}",
        entities=[_mention(text="Marlow")],
    )

    assert teams._activity_mentions_bot(activity, CLIENT_ID) is False
    assert _make_adapter()._validate_activity(activity) is False


def test_non_mention_entity_cannot_match_or_be_stripped():
    activity = _activity(
        conversation_type="groupChat",
        text="Marlow please answer",
        entities=[
            {
                "type": "message",
                "mentioned": _account(CLIENT_ID, type="bot").model_dump(mode="json", exclude_none=True),
                "text": "Marlow",
            }
        ],
    )

    assert teams._activity_mentions_bot(activity, CLIENT_ID) is False
    text, matched = teams._strip_bot_mentions(activity, CLIENT_ID)
    assert matched is False
    assert text == "Marlow please answer"


def test_stripping_preserves_other_mention_with_same_display_text():
    activity = _activity(
        conversation_type="groupChat",
        text="Marlow Marlow please answer",
        entities=[
            _mention(text="Marlow"),
            _mention(user_id=OTHER_USER_ID, text="Marlow"),
        ],
    )

    text, matched = teams._strip_bot_mentions(activity, CLIENT_ID)
    assert matched is True
    assert text == "Marlow please answer"


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
async def test_supervisor_stop_returns_after_handler_swallows_cancellation():
    supervisor = teams.TeamsDispatchSupervisor(max_active=1, max_pending=1)
    delivered = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def handler(task):
        delivered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            cleanup_finished.set()

    supervisor.start_worker(handler)
    first = teams.TeamsDispatchTask(None, MagicMock(), None, "first", "hash")
    second = teams.TeamsDispatchTask(None, MagicMock(), None, "second", "hash")
    assert await supervisor.submit(first) is True
    await asyncio.wait_for(delivered.wait(), timeout=1)
    assert await supervisor.submit(second) is True

    await asyncio.wait_for(supervisor.stop(), timeout=1)

    assert cleanup_finished.is_set()
    assert supervisor.capacity.active == 0
    assert supervisor.capacity.pending == 0


@pytest.mark.asyncio
async def test_supervisor_uses_all_active_slots_and_drains_on_stop():
    supervisor = teams.TeamsDispatchSupervisor(max_active=2, max_pending=1)
    started = set()
    both_started = asyncio.Event()
    cancelled = set()

    async def handler(task):
        started.add(task.receipt_key)
        if len(started) == 2:
            both_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(task.receipt_key)
            raise

    supervisor.start_worker(handler)
    tasks = [
        teams.TeamsDispatchTask(None, MagicMock(), None, f"task-{index}", "hash")
        for index in range(4)
    ]
    assert await supervisor.submit(tasks[0]) is True
    assert await supervisor.submit(tasks[1]) is True
    assert await supervisor.submit(tasks[2]) is True
    assert await supervisor.submit(tasks[3]) is False
    await asyncio.wait_for(both_started.wait(), timeout=1)
    assert started == {"task-0", "task-1"}
    assert supervisor.capacity.pending == 1

    await asyncio.wait_for(supervisor.stop(), timeout=1)

    assert cancelled == {"task-0", "task-1"}
    assert supervisor.capacity.active == 0
    assert supervisor.capacity.pending == 0


@pytest.mark.asyncio
async def test_supervisor_continues_after_task_local_cancellation():
    supervisor = teams.TeamsDispatchSupervisor(max_active=1, max_pending=1)
    processed = []

    async def handler(task):
        if task.receipt_key == "cancelled":
            raise asyncio.CancelledError()
        processed.append(task.receipt_key)

    supervisor.start_worker(handler)
    assert await supervisor.submit(teams.TeamsDispatchTask(None, MagicMock(), None, "cancelled", "hash")) is True
    assert await supervisor.submit(teams.TeamsDispatchTask(None, MagicMock(), None, "next", "hash")) is True

    for _ in range(10):
        if processed == ["next"]:
            break
        await asyncio.sleep(0.01)

    assert processed == ["next"]
    await supervisor.stop()


@pytest.mark.asyncio
async def test_supervisor_stop_before_same_session_drain_leaves_no_successor_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(), max_active=2, max_pending=1)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    first_started = asyncio.Event()
    handler_calls = 0

    async def handler(event):
        nonlocal handler_calls
        handler_calls += 1
        if handler_calls == 1:
            first_started.set()
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-1", conversation_id="shared")))).status == 200
    await asyncio.wait_for(first_started.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-2", conversation_id="shared")))).status == 200
    await asyncio.sleep(0)

    await asyncio.wait_for(adapter._supervisor.stop(), timeout=1)
    await asyncio.sleep(0)

    assert handler_calls == 1
    assert not any(not task.done() for task in adapter._background_tasks)
    assert not adapter._processing_successor_map()
    assert not adapter._processing_chain_task_set()
    assert not adapter._session_tasks
    assert not adapter._active_sessions


@pytest.mark.asyncio
async def test_immediately_cancelled_processing_root_leaves_no_base_task_state():
    adapter = _make_adapter()
    session_key = "immediate-cancel"
    event = teams.MessageEvent(text="hello", source=MagicMock())
    task = adapter._start_session_processing(event, session_key)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks
    assert task not in adapter._background_tasks
    assert task not in adapter._processing_chain_task_set()
    assert task not in adapter._processing_successor_map()


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
    await adapter._supervisor.stop()


@pytest.mark.asyncio
async def test_accepted_activity_delivers_handler_response_with_teams_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(), max_active=1, max_pending=1)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    adapter.set_message_handler(AsyncMock(return_value="Hello from Marlow"))
    delivered = asyncio.Event()

    async def capture_send(**kwargs):
        delivered.set()
        return teams.SendResult(success=True, message_id="reply-1")

    adapter.send = AsyncMock(side_effect=capture_send)
    adapter.send_typing = AsyncMock(return_value=teams.SendResult(success=True))

    response = await adapter._handle_teams_activity(MagicMock(body=_activity()))
    assert response.status == 200
    await asyncio.wait_for(delivered.wait(), timeout=1)

    adapter.send.assert_awaited_once()
    sent = adapter.send.await_args.kwargs
    assert sent["content"] == "Hello from Marlow"
    assert sent["metadata"]["teams_reference"] == teams._conversation_reference_dict(
        teams._sdk_conversation_reference(SERVICE_URL, _bot(), _conversation())
    )
    await asyncio.wait_for(adapter._supervisor.stop(), timeout=1)


@pytest.mark.asyncio
async def test_supervisor_saturation_returns_503_without_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _make_adapter(dispatch_max_active=0, dispatch_max_pending=0)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    response = await adapter._handle_teams_activity(MagicMock(body=_activity()))
    assert response.status == 503
    assert (tmp_path / "teams" / "receipts" / "teams_receipts.json").exists() is False


@pytest.mark.asyncio
async def test_supervisor_capacity_is_held_while_base_processing_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(), max_active=1, max_pending=1)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_handler(event):
        started.set()
        await release.wait()
        return None

    adapter.set_message_handler(blocked_handler)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-1", conversation_id="conv-1")))).status == 200
    await asyncio.wait_for(started.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-2", conversation_id="conv-2")))).status == 200
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-3", conversation_id="conv-3")))).status == 503

    release.set()
    await adapter._supervisor.stop()


@pytest.mark.asyncio
async def test_supervisor_stop_cancels_owned_base_processing(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(), max_active=1, max_pending=1)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_handler(event):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    adapter.set_message_handler(blocked_handler)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity()))).status == 200
    await asyncio.wait_for(started.wait(), timeout=1)
    processing_task = next(iter(adapter._background_tasks))

    await adapter._supervisor.stop()

    assert cancelled.is_set()
    assert processing_task.done()
    assert processing_task.cancelled()


@pytest.mark.asyncio
async def test_same_session_drain_remains_supervisor_owned_until_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(), max_active=2, max_pending=1)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def handler(event):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
            return None
        second_started.set()
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-1", conversation_id="shared")))).status == 200
    await asyncio.wait_for(first_started.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-2", conversation_id="shared")))).status == 200
    await asyncio.sleep(0)

    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)
    assert adapter._supervisor.capacity.active == 1

    await asyncio.wait_for(adapter._supervisor.stop(), timeout=1)
    await asyncio.sleep(0)
    assert not any(not task.done() for task in adapter._background_tasks)


@pytest.mark.asyncio
async def test_command_drained_follow_up_remains_supervisor_owned_until_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(), max_active=2, max_pending=1)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    first_started = asyncio.Event()
    command_started = asyncio.Event()
    follow_up_started = asyncio.Event()
    release_command = asyncio.Event()

    async def handler(event):
        if event.text == "first":
            first_started.set()
            await asyncio.Event().wait()
        elif event.get_command() == "new":
            command_started.set()
            await release_command.wait()
        elif event.text == "follow-up":
            follow_up_started.set()
            await asyncio.Event().wait()
        return None

    adapter.set_message_handler(handler)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-1", conversation_id="shared", text="first")))).status == 200
    await asyncio.wait_for(first_started.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-2", conversation_id="shared", text="/new")))).status == 200
    await asyncio.wait_for(command_started.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-3", conversation_id="shared", text="follow-up")))).status == 200

    release_command.set()
    await asyncio.wait_for(follow_up_started.wait(), timeout=1)
    assert adapter._supervisor.capacity.active == 1

    await asyncio.wait_for(adapter._supervisor.stop(), timeout=1)
    await asyncio.sleep(0)
    assert not any(not task.done() for task in adapter._background_tasks)
    assert not adapter._processing_chain_task_set()
    assert not adapter._processing_successor_map()
    assert not adapter._session_tasks
    assert not adapter._active_sessions


@pytest.mark.asyncio
async def test_failed_command_drained_follow_up_remains_supervisor_owned_until_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(), max_active=2, max_pending=1)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    first_started = asyncio.Event()
    first_finished = asyncio.Event()
    command_started = asyncio.Event()
    follow_up_started = asyncio.Event()
    release_first = asyncio.Event()
    fail_command = asyncio.Event()

    async def handler(event):
        if event.text == "first":
            first_started.set()
            await release_first.wait()
            first_finished.set()
        elif event.get_command() == "new":
            command_started.set()
            await fail_command.wait()
            raise RuntimeError("command failed")
        elif event.text == "follow-up":
            follow_up_started.set()
            await asyncio.Event().wait()
        return None

    adapter.set_message_handler(handler)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-1", conversation_id="shared", text="first")))).status == 200
    await asyncio.wait_for(first_started.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-2", conversation_id="shared", text="/new")))).status == 200
    await asyncio.wait_for(command_started.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-3", conversation_id="shared", text="follow-up")))).status == 200

    release_first.set()
    await asyncio.wait_for(first_finished.wait(), timeout=1)
    fail_command.set()
    await asyncio.wait_for(follow_up_started.wait(), timeout=1)
    assert adapter._supervisor.capacity.active == 1

    await asyncio.wait_for(adapter._supervisor.stop(), timeout=1)
    await asyncio.sleep(0)
    assert not any(not task.done() for task in adapter._background_tasks)
    assert not adapter._processing_chain_task_set()
    assert not adapter._processing_successor_map()
    assert not adapter._session_tasks
    assert not adapter._active_sessions


@pytest.mark.asyncio
async def test_supervisor_stop_cancels_nested_command_without_escaping_processing_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(), max_active=2, max_pending=1)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    first_started = asyncio.Event()
    command_started = asyncio.Event()

    async def handler(event):
        if event.text == "first":
            first_started.set()
            await asyncio.Event().wait()
        elif event.get_command() == "new":
            command_started.set()
            await asyncio.Event().wait()
        return None

    adapter.set_message_handler(handler)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-1", conversation_id="shared", text="first")))).status == 200
    await asyncio.wait_for(first_started.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-2", conversation_id="shared", text="/new")))).status == 200
    await asyncio.wait_for(command_started.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-3", conversation_id="shared", text="follow-up")))).status == 200

    await asyncio.wait_for(adapter._supervisor.stop(), timeout=1)
    await asyncio.sleep(0)
    assert not any(not task.done() for task in adapter._background_tasks)
    assert not adapter._processing_chain_task_set()
    assert not adapter._processing_successor_map()
    assert not adapter._processing_guard_map()
    assert not adapter._session_tasks
    assert not adapter._active_sessions


@pytest.mark.asyncio
async def test_supervisor_stop_cancels_command_waiting_for_old_processor_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(), max_active=2, max_pending=1)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    first_started = asyncio.Event()
    old_cleanup_entered = asyncio.Event()
    release_old_cleanup = asyncio.Event()

    async def handler(event):
        if event.text == "first":
            first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                old_cleanup_entered.set()
                await release_old_cleanup.wait()
                raise
        elif event.get_command() == "new":
            return None
        return None

    adapter.set_message_handler(handler)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-1", conversation_id="shared", text="first")))).status == 200
    await asyncio.wait_for(first_started.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-2", conversation_id="shared", text="/new")))).status == 200
    await asyncio.wait_for(old_cleanup_entered.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-3", conversation_id="shared", text="follow-up")))).status == 200

    stop_task = asyncio.create_task(adapter._supervisor.stop())
    await asyncio.sleep(0)
    release_old_cleanup.set()
    await asyncio.wait_for(stop_task, timeout=1)
    await asyncio.sleep(0)
    assert not any(not task.done() for task in adapter._background_tasks)
    assert not adapter._processing_chain_task_set()
    assert not adapter._processing_successor_map()
    assert not adapter._processing_guard_map()
    assert not adapter._session_tasks
    assert not adapter._active_sessions


@pytest.mark.asyncio
async def test_three_message_same_session_drain_chain_stays_owned_until_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(), max_active=2, max_pending=1)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    third_started = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    calls = 0

    async def handler(event):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
        elif calls == 2:
            second_started.set()
            await release_second.wait()
        else:
            third_started.set()
            await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-1", conversation_id="shared")))).status == 200
    await asyncio.wait_for(first_started.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-2", conversation_id="shared")))).status == 200
    await asyncio.sleep(0)

    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-3", conversation_id="shared")))).status == 200
    await asyncio.sleep(0)

    release_second.set()
    await asyncio.wait_for(third_started.wait(), timeout=1)
    assert adapter._supervisor.capacity.active == 1

    await asyncio.wait_for(adapter._supervisor.stop(), timeout=1)
    await asyncio.sleep(0)
    assert not any(not task.done() for task in adapter._background_tasks)
    assert not adapter._processing_chain_task_set()
    assert not adapter._processing_successor_map()
    assert not adapter._session_tasks
    assert not adapter._active_sessions


@pytest.mark.asyncio
async def test_predecessor_cleanup_barrier_preserves_three_message_chain_until_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(), max_active=2, max_pending=1)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    third_started = asyncio.Event()
    root_before_chain_consume = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    release_root_before_chain_consume = asyncio.Event()
    calls = 0

    async def handler(event):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
        elif calls == 2:
            second_started.set()
            await release_second.wait()
        else:
            third_started.set()
            await asyncio.Event().wait()

    original_process_message = adapter._process_message_background

    async def delay_root_before_chain_consume(event, session_key):
        result = await original_process_message(event, session_key)
        if event.message_id == "msg-1":
            root_before_chain_consume.set()
            await release_root_before_chain_consume.wait()
        return result

    adapter._process_message_background = delay_root_before_chain_consume
    adapter.set_message_handler(handler)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-1", conversation_id="shared")))).status == 200
    await asyncio.wait_for(first_started.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-2", conversation_id="shared")))).status == 200

    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)
    await asyncio.wait_for(root_before_chain_consume.wait(), timeout=1)
    assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-3", conversation_id="shared")))).status == 200

    release_second.set()
    await asyncio.wait_for(third_started.wait(), timeout=1)
    assert len(adapter._processing_successor_map()) == 2
    assert adapter._supervisor.capacity.active == 1

    release_root_before_chain_consume.set()
    await asyncio.wait_for(adapter._supervisor.stop(), timeout=1)
    await asyncio.sleep(0)
    assert not any(not task.done() for task in adapter._background_tasks)
    assert not adapter._processing_chain_task_set()
    assert not adapter._processing_successor_map()
    assert not adapter._session_tasks
    assert not adapter._active_sessions


@pytest.mark.asyncio
async def test_cancel_background_tasks_timeout_clears_processing_tracking(monkeypatch):
    adapter = _make_adapter()
    cancellation_started = asyncio.Event()
    allow_straggler_exit = asyncio.Event()

    async def cancellation_swallowing_task():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_started.set()
            await allow_straggler_exit.wait()

    straggler = asyncio.create_task(cancellation_swallowing_task())
    successor = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    adapter._background_tasks.add(straggler)
    adapter._processing_chain_task_set().add(straggler)
    adapter._processing_successor_map()[straggler] = successor
    original_wait_for = asyncio.wait_for

    async def immediate_timeout(*args, **kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(platform_base.asyncio, "wait_for", immediate_timeout)
    await adapter.cancel_background_tasks()
    monkeypatch.setattr(platform_base.asyncio, "wait_for", original_wait_for)
    await original_wait_for(cancellation_started.wait(), timeout=1)

    assert not straggler.done()
    assert not adapter._background_tasks
    assert not adapter._processing_chain_task_set()
    assert not adapter._processing_successor_map()

    allow_straggler_exit.set()
    await original_wait_for(straggler, timeout=1)
    successor.cancel()
    await asyncio.gather(successor, return_exceptions=True)


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


@pytest.mark.asyncio
async def test_later_chunk_failure_does_not_replay_successful_teams_chunk(monkeypatch):
    adapter = _make_adapter(text_budget_bytes=10, outbound_max_attempts=2)
    monkeypatch.setattr(teams.asyncio, "sleep", AsyncMock())
    adapter._teams_app = MagicMock()
    adapter._teams_app.activity_sender.send = AsyncMock(
        side_effect=[
            SimpleNamespace(id="m1"),
            Exception("timeout"),
            Exception("timeout"),
        ]
    )
    ref = teams._sdk_conversation_reference(SERVICE_URL, _bot(), _conversation())

    result = await adapter._send_with_retry(
        "chat",
        "alpha beta gamma",
        metadata={"teams_reference": teams._conversation_reference_dict(ref)},
    )

    assert result.success is False
    assert adapter._teams_app.activity_sender.send.call_count == 3
    assert [call.args[0].text for call in adapter._teams_app.activity_sender.send.await_args_list] == ["alpha", "beta", "beta"]


@pytest.mark.asyncio
async def test_persistent_teams_delivery_failure_uses_only_transport_attempt_limit(monkeypatch, caplog):
    adapter = _make_adapter(outbound_max_attempts=3)
    monkeypatch.setattr(teams.asyncio, "sleep", AsyncMock())
    adapter._teams_app = MagicMock()
    adapter._teams_app.activity_sender.send = AsyncMock(side_effect=[Exception("timeout")] * 3)
    ref = teams._sdk_conversation_reference(SERVICE_URL, _bot(), _conversation())

    result = await adapter._send_with_retry(
        "chat",
        "response with secret text",
        metadata={"teams_reference": teams._conversation_reference_dict(ref)},
    )

    assert result.success is False
    assert adapter._teams_app.activity_sender.send.call_count == 3
    assert "Teams response delivery failed after bounded send attempt" in caplog.text
    assert "response with secret text" not in caplog.text


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

# ---------------------------------------------------------------------------
# Full channel-thread context
# ---------------------------------------------------------------------------

TEAMS_THREAD_TEAM_AAD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TEAMS_THREAD_TEAM_ID = "team-thread-1"
TEAMS_THREAD_CHANNEL_ID = "19:thread-channel@thread.tacv2"
TEAMS_THREAD_CONVERSATION_ID = f"{TEAMS_THREAD_CHANNEL_ID};messageid=1756701234567"
TEAMS_THREAD_TRIGGER_ID = "1756701234570"


def _thread_channel_data(*, channel_type="standard", aad_group_id=TEAMS_THREAD_TEAM_AAD):
    return {
        "team": {"id": TEAMS_THREAD_TEAM_ID, "aadGroupId": aad_group_id},
        "channel": {"id": TEAMS_THREAD_CHANNEL_ID, "type": channel_type},
        "tenant": {"id": TENANT_ID},
    }


def _thread_activity(*, activity_id=TEAMS_THREAD_TRIGGER_ID, text="Marlow summarize", created_date_time="2026-09-01T00:00:02Z", channel_type="standard"):
    return teams.Activity.model_validate(
        {
            "serviceUrl": SERVICE_URL,
            "channelId": "msteams",
            "from": _account(USER_ID).model_dump(mode="json", exclude_none=True),
            "conversation": {
                "id": TEAMS_THREAD_CONVERSATION_ID,
                "tenant_id": TENANT_ID,
                "conversation_type": "channel",
            },
            "recipient": _bot().model_dump(mode="json", exclude_none=True),
            "type": "message",
            "id": activity_id,
            "createdDateTime": created_date_time,
            "text": text,
            "channelData": _thread_channel_data(channel_type=channel_type),
            "entities": [_mention(text="Marlow")],
        }
    )


def _thread_event(*, activity=None, text="summarize"):
    return teams.MessageEvent(
        text=text,
        message_type=teams.MessageType.TEXT,
        source=SimpleNamespace(
            platform=teams.Platform("teams"),
            chat_id=json.dumps([TENANT_ID, CLIENT_ID, TEAMS_THREAD_TEAM_ID, TEAMS_THREAD_CHANNEL_ID], separators=(",", ":")),
            chat_type="channel",
            thread_id=TEAMS_THREAD_CONVERSATION_ID,
            user_id=USER_ID,
            user_name="Alice",
        ),
        raw_message=teams._activity_to_dict(activity or _thread_activity()),
        message_id=TEAMS_THREAD_TRIGGER_ID,
    )


def _graph_message(message_id, text, *, created="2026-09-01T00:00:00Z", deleted=False, edited=False, attachment=None):
    item = {
        "id": str(message_id),
        "replyToId": None if str(message_id) == "1756701234567" else "1756701234567",
        "createdDateTime": created,
        "from": {"user": {"displayName": "Alice", "id": f"user-{message_id}"}},
        "body": {
            "contentType": "html",
            "content": f"<script>window.evil()</script><p>{text}</p><style>.x{{display:none}}</style>",
        },
        "attachments": [],
    }
    if edited:
        item["lastEditedDateTime"] = "2026-09-01T00:00:03Z"
    if deleted:
        item["deletedDateTime"] = "2026-09-01T00:00:04Z"
    if attachment:
        item["attachments"] = [attachment]
    return item


class _FakeGraph:
    def __init__(self, pages):
        self.pages = list(pages)
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return self.pages.pop(0)


def _install_graph(adapter, pages):
    graph = _FakeGraph(pages)
    adapter._teams_app = SimpleNamespace(get_app_graph=lambda tenant_id: graph)
    return graph


def test_thread_context_root_parser_accepts_only_messageid_parameter():
    adapter = _make_adapter(thread_context={"enabled": True})
    assert adapter._parse_root_message_id(_thread_activity()) == "1756701234567"
    bad = _thread_activity()
    bad.conversation.id = "19:opaque@thread.tacv2;messageid=1;messageid=2"
    assert adapter._parse_root_message_id(bad) is None
    bad.conversation.id = "19:opaque@thread.tacv2;foo=1"
    assert adapter._parse_root_message_id(bad) is None


def test_thread_context_disabled_makes_no_graph_call():
    adapter = _make_adapter(thread_context={"enabled": False})
    event = _thread_event()
    out = asyncio.run(adapter.enrich_authorized_event(event))
    assert out.external_conversation_snapshot is None


def test_thread_context_rejects_private_channel_before_graph_call():
    adapter = _make_adapter(thread_context={"enabled": True})
    event = _thread_event(activity=_thread_activity(channel_type="private"))
    with pytest.raises(teams.TeamsThreadContextError) as excinfo:
        asyncio.run(adapter.enrich_authorized_event(event))
    assert "standard" in excinfo.value.user_facing_message


def test_graph_request_failure_logs_graph_response_details(caplog):
    adapter = _make_adapter(thread_context={"enabled": True})
    response = SimpleNamespace(
        status_code=429,
        headers={"request-id": "request-1", "client-request-id": "client-request-1"},
        text="too many requests",
    )
    error = RuntimeError("Graph unavailable")
    error.response = response
    graph = SimpleNamespace(get=MagicMock(side_effect=error))

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="Graph unavailable"):
            asyncio.run(adapter._graph_request_json(graph, "/graph-url", "replies"))

    assert (
        "Teams Graph request failed: operation=replies status=429 url=/graph-url "
        "request_id=request-1 client_request_id=client-request-1 body=too many requests"
    ) in caplog.text


def test_thread_context_pagination_trigger_found_and_later_messages_excluded():
    adapter = _make_adapter(thread_context={"enabled": True})
    graph = _install_graph(
        adapter,
        [
            _graph_message("1756701234567", "root"),
            {
                "value": [
                    _graph_message("1756701234568", "reply 1", created="2026-09-01T00:00:01Z"),
                ],
                "@odata.nextLink": "/next-page",
            },
            {
                "value": [
                    _graph_message(TEAMS_THREAD_TRIGGER_ID, "trigger", created="2026-09-01T00:00:02Z"),
                    _graph_message("1756701234571", "later", created="2026-09-01T00:00:03Z"),
                ]
            },
        ],
    )
    event = _thread_event()
    out = asyncio.run(adapter.enrich_authorized_event(event))
    snapshot = out.external_conversation_snapshot
    assert snapshot is not None
    assert [msg.message_id for msg in snapshot.messages] == ["1756701234567", "1756701234568", TEAMS_THREAD_TRIGGER_ID]
    assert snapshot.messages[-1].is_trigger is True
    assert "/next-page" in graph.urls
    rendered = render_external_conversation_snapshot(snapshot, out)
    assert "[External conversation context" in rendered
    assert "root" in rendered
    assert "reply 1" in rendered
    assert "Marlow summarize" not in rendered.split("[Current authenticated request", 1)[0]
    assert "[Current authenticated request" in rendered


def test_thread_context_trigger_absent_appends_activity_once_and_strips_mention():
    adapter = _make_adapter(thread_context={"enabled": True})
    _install_graph(
        adapter,
        [
            _graph_message("1756701234567", "root"),
            {"value": [_graph_message("1756701234568", "reply 1", created="2026-09-01T00:00:01Z")]},
        ],
    )
    out = asyncio.run(adapter.enrich_authorized_event(_thread_event()))
    snapshot = out.external_conversation_snapshot
    assert [msg.message_id for msg in snapshot.messages] == ["1756701234567", "1756701234568", TEAMS_THREAD_TRIGGER_ID]
    assert snapshot.messages[-1].text == "summarize"
    assert snapshot.messages[-1].is_trigger is True


def test_thread_context_duplicate_conflicting_graph_ids_fail_closed():
    adapter = _make_adapter(thread_context={"enabled": True})
    _install_graph(
        adapter,
        [
            _graph_message("1756701234567", "root"),
            {"value": [_graph_message("1756701234567", "conflict")]},
        ],
    )
    with pytest.raises(teams.TeamsThreadContextError):
        asyncio.run(adapter.enrich_authorized_event(_thread_event()))


def test_thread_context_normalizes_edited_deleted_html_and_attachments():
    adapter = _make_adapter(thread_context={"enabled": True})
    _install_graph(
        adapter,
        [
            _graph_message("1756701234567", "root", edited=True),
            {"value": [_graph_message("1756701234568", "deleted", deleted=True)]},
        ],
    )
    out = asyncio.run(adapter.enrich_authorized_event(_thread_event()))
    snapshot = out.external_conversation_snapshot
    assert snapshot.messages[0].text == "root\n[edited]"
    assert snapshot.messages[1].text == "[message deleted]"
    assert snapshot.messages[0].edited_at is not None
    assert snapshot.messages[1].deleted_at is not None

    attachment_adapter = _make_adapter(thread_context={"enabled": True})
    _install_graph(
        attachment_adapter,
        [
            _graph_message(
                "1756701234567",
                "root",
                attachment={"id": "att-1", "name": "plan.png", "contentType": "image/png"},
            ),
            {"value": []},
        ],
    )
    snapshot = asyncio.run(
        attachment_adapter.enrich_authorized_event(_thread_event())
    ).external_conversation_snapshot
    assert snapshot.messages[0].attachments[0].reference_kind == "image"


def test_thread_context_uses_event_route_identity_for_snapshot():
    adapter = _make_adapter(thread_context={"enabled": True})
    _install_graph(adapter, [_graph_message("1756701234567", "root"), {"value": []}])
    event = _thread_event()
    out = asyncio.run(adapter.enrich_authorized_event(event))
    snapshot = out.external_conversation_snapshot
    assert snapshot.chat_id == event.source.chat_id
    assert snapshot.thread_id == event.source.thread_id


# ---------------------------------------------------------------------------
# Acknowledgement reactions
# ---------------------------------------------------------------------------


def _reaction_event_from_activity(activity):
    adapter = _make_adapter()
    reference = teams._sdk_conversation_reference(
        getattr(activity, "service_url", ""),
        getattr(activity, "recipient", None),
        getattr(activity, "conversation", None),
    )
    source = adapter._build_source(activity, reference=reference)
    return MessageEvent(
        text=getattr(activity, "text", "") or "",
        message_type=MessageType.TEXT,
        source=source,
        raw_message=teams._activity_to_dict(activity),
        message_id=getattr(activity, "id", None),
    )


def _reaction_event(activity=None, *, message_id="msg-1", conversation_type="personal", conversation_id="conv-1", service_url=SERVICE_URL, channel_data=None, text="hello"):
    if activity is None:
        activity = _activity(
            activity_id=message_id,
            conversation_type=conversation_type,
            conversation_id=conversation_id,
            service_url=service_url,
            channel_data=channel_data,
            text=text,
        )
    return _reaction_event_from_activity(activity)


def test_reaction_config_defaults_to_disabled_and_env_can_enable():
    adapter = _make_adapter()
    assert adapter._reaction_config.enabled is False

    adapter = _make_adapter(reactions={"enabled": True})
    assert adapter._reaction_config.enabled is True


def test_reaction_config_env_can_enable_without_yaml(monkeypatch):
    monkeypatch.setenv("TEAMS_REACTIONS", "true")
    adapter = _make_adapter()
    assert adapter._reaction_config.enabled is True

    monkeypatch.setenv("TEAMS_REACTIONS", "false")
    adapter = _make_adapter()
    assert adapter._reaction_config.enabled is False


def test_reaction_config_env_rejects_invalid_without_yaml(monkeypatch):
    monkeypatch.setenv("TEAMS_REACTIONS", "maybe")
    with pytest.raises(ValueError, match="TEAMS_REACTIONS"):
        _make_adapter()


def test_reaction_config_env_override_precedence(monkeypatch):
    adapter = _make_adapter(reactions={"enabled": True})
    assert adapter._reaction_config.enabled is True

    monkeypatch.setenv("TEAMS_REACTIONS", "false")
    adapter = _make_adapter(reactions={"enabled": True})
    assert adapter._reaction_config.enabled is False


def test_reaction_config_rejects_non_bool_yaml():
    with pytest.raises(ValueError, match="teams.reactions.enabled"):
        _make_adapter(reactions={"enabled": "yes"})


def test_reaction_target_extraction_personal_group_and_channel(monkeypatch):
    personal = _reaction_event(message_id="msg-personal", conversation_type="personal", conversation_id="dm-1")
    assert teams._reaction_target_for_event(personal) == teams.TeamsReactionTarget(
        service_url=SERVICE_URL,
        conversation_id="dm-1",
        activity_id="msg-personal",
    )

    group = _reaction_event(message_id="msg-group", conversation_type="groupChat", conversation_id="group-1")
    assert teams._reaction_target_for_event(group).conversation_id == "group-1"

    channel = _reaction_event(
        message_id="msg-channel",
        conversation_type="channel",
        conversation_id="thread-1",
        channel_data={"team": {"id": "team-1"}, "channel": {"id": "chan-1", "type": "standard"}},
        text=None,
    )
    assert teams._reaction_target_for_event(channel) == teams.TeamsReactionTarget(
        service_url=SERVICE_URL,
        conversation_id="thread-1",
        activity_id="msg-channel",
    )


@pytest.mark.parametrize(
    "service_url",
    [
        "http://smba.trafficmanager.net/teams",
        "https://smba.trafficmanager.net/teams?x=1",
        "https://user:pass@smba.trafficmanager.net/teams",
        "https://smba.trafficmanager.net/teams/#frag",
    ],
)
def test_reaction_target_rejects_invalid_service_urls(service_url):
    activity = _activity(service_url=service_url)
    event = _reaction_event(activity=activity)
    assert teams._reaction_target_for_event(event) is None


@pytest.mark.asyncio
async def test_reaction_api_client_factory_is_service_url_bound_and_bounded(monkeypatch):
    adapter = _make_adapter(reactions={"enabled": True})
    app = SimpleNamespace(
        api=SimpleNamespace(http="http-client"),
        options=SimpleNamespace(api_client_settings="api-settings"),
        cloud="public",
    )
    adapter._teams_app = app

    def fake_api_client(service_url, http_client, api_client_settings, *, cloud):
        return MagicMock(service_url=service_url, http_client=http_client, api_client_settings=api_client_settings, cloud=cloud)

    monkeypatch.setattr(teams, "ApiClient", fake_api_client)

    first = adapter._reaction_api_for_service_url("https://service-a.example/teams/")
    second = adapter._reaction_api_for_service_url("https://service-b.example/teams")
    third = adapter._reaction_api_for_service_url("https://service-a.example/teams")

    assert first is third
    assert first is not second
    assert first.service_url == "https://service-a.example/teams"
    assert first.http_client == "http-client"
    assert first.api_client_settings == "api-settings"
    assert first.cloud == "public"

    for index in range(teams.DEFAULT_REACTION_CLIENT_CACHE_SIZE):
        adapter._reaction_api_for_service_url(f"https://evict-{index}.example/teams")

    assert len(adapter._reaction_api_clients) == teams.DEFAULT_REACTION_CLIENT_CACHE_SIZE


@pytest.mark.asyncio
async def test_reaction_rate_limiter_refills_by_monotonic_time(monkeypatch):
    now = 100.0
    monkeypatch.setattr(teams.time, "monotonic", lambda: now)
    limiter = teams.TeamsReactionRateLimiter(rate_per_second=1.0, burst=1)

    assert await limiter.try_acquire() is True
    assert await limiter.try_acquire() is False

    now = 101.0
    assert await limiter.try_acquire() is True


@pytest.mark.asyncio
async def test_on_processing_start_skips_when_disabled():
    adapter = _make_adapter()
    adapter._spawn_reaction_task = MagicMock()
    await adapter.on_processing_start(_reaction_event())
    adapter._spawn_reaction_task.assert_not_called()


@pytest.mark.asyncio
async def test_on_processing_start_schedules_reaction_without_waiting(monkeypatch):
    adapter = _make_adapter(reactions={"enabled": True})
    event = _reaction_event()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_add(target):
        entered.set()
        await release.wait()
        return teams.TeamsReactionResult.SUCCESS

    adapter._add_acknowledgement_reaction = AsyncMock(side_effect=slow_add)
    await adapter.on_processing_start(event)
    await asyncio.wait_for(entered.wait(), timeout=1)
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_ack_reaction_success_records_metric_and_calls_sdk(monkeypatch):
    adapter = _make_adapter(reactions={"enabled": True})
    target = teams._reaction_target_for_event(_reaction_event())
    fake_api = MagicMock()
    fake_api.conversations.add_reaction = AsyncMock()
    adapter._reaction_api_for_service_url = MagicMock(return_value=fake_api)

    result = await adapter._add_acknowledgement_reaction(target)

    assert result == teams.TeamsReactionResult.SUCCESS
    fake_api.conversations.add_reaction.assert_awaited_once_with("conv-1", "msg-1", teams.TEAMS_ACK_REACTION)
    snapshot = adapter._telemetry.snapshot()
    assert snapshot["counters"]["teams_reaction_operations_total"]["{\"operation\": \"add\", \"result\": \"success\"}"] == 1


@pytest.mark.asyncio
async def test_ack_reaction_local_rate_limited_drops_without_retry():
    adapter = _make_adapter(reactions={"enabled": True})
    adapter._reaction_limiter = teams.TeamsReactionRateLimiter(rate_per_second=0.001, burst=0)
    assert await adapter._reaction_limiter.try_acquire()
    target = teams._reaction_target_for_event(_reaction_event())

    result = await adapter._add_acknowledgement_reaction(target)

    assert result == teams.TeamsReactionResult.LOCAL_RATE_LIMITED


@pytest.mark.asyncio
async def test_ack_reaction_timeout_is_bounded_and_recorded():
    adapter = _make_adapter(reactions={"enabled": True})

    async def never_responds(*args, **kwargs):
        await asyncio.Event().wait()

    fake_api = MagicMock()
    fake_api.conversations.add_reaction = AsyncMock(side_effect=never_responds)
    adapter._reaction_api_for_service_url = MagicMock(return_value=fake_api)
    target = teams._reaction_target_for_event(_reaction_event())

    result = await adapter._add_acknowledgement_reaction(target)

    assert result == teams.TeamsReactionResult.TIMEOUT
    fake_api.conversations.add_reaction.assert_awaited_once()
    snapshot = adapter._telemetry.snapshot()
    assert snapshot["counters"]["teams_reaction_operations_total"]["{\"operation\": \"add\", \"result\": \"timeout\"}"] == 1


@pytest.mark.asyncio
async def test_on_processing_start_drops_reaction_when_inflight_cap_reached():
    adapter = _make_adapter(reactions={"enabled": True})
    inflight = {asyncio.create_task(asyncio.sleep(10)) for _ in range(teams.DEFAULT_REACTION_MAX_INFLIGHT)}
    adapter._reaction_tasks.update(inflight)
    adapter._spawn_reaction_task = MagicMock()
    try:
        await adapter.on_processing_start(_reaction_event())
    finally:
        for task in inflight:
            task.cancel()
        await asyncio.gather(*inflight, return_exceptions=True)

    adapter._spawn_reaction_task.assert_not_called()
    snapshot = adapter._telemetry.snapshot()
    assert snapshot["counters"]["teams_reaction_operations_total"]["{\"operation\": \"add\", \"result\": \"local_rate_limited\"}"] == 1


@pytest.mark.asyncio
async def test_queued_same_session_reaction_starts_when_processing_begins(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(reactions={"enabled": True}), max_active=1, max_pending=1)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    seen_activity_ids = []

    async def add_reaction(target):
        seen_activity_ids.append(target.activity_id)
        return teams.TeamsReactionResult.SUCCESS

    async def handler(event):
        if event.text == "first":
            first_started.set()
            await release_first.wait()
        elif event.text == "second":
            second_started.set()
        return "ok"

    adapter._add_acknowledgement_reaction = AsyncMock(side_effect=add_reaction)
    adapter.set_message_handler(handler)

    response = await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-queued-1", text="first", conversation_id="shared")))
    assert response.status == 200
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert seen_activity_ids == ["msg-queued-1"]

    response = await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-queued-2", text="second", conversation_id="shared")))
    assert response.status == 200
    await asyncio.sleep(0)
    assert seen_activity_ids == ["msg-queued-1"]

    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)
    for _ in range(20):
        if seen_activity_ids == ["msg-queued-1", "msg-queued-2"]:
            break
        await asyncio.sleep(0.01)
    assert seen_activity_ids == ["msg-queued-1", "msg-queued-2"]
    await asyncio.wait_for(adapter._supervisor.stop(), timeout=1)


@pytest.mark.asyncio
async def test_duplicate_rejected_activity_does_not_schedule_reaction(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(reactions={"enabled": True}), max_active=2, max_pending=2)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    adapter._spawn_reaction_task = MagicMock()
    adapter.set_message_handler(AsyncMock(return_value="ok"))

    try:
        assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-duplicate")))).status == 200
        await asyncio.sleep(0.05)
        assert adapter._spawn_reaction_task.call_count == 1

        assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-duplicate")))).status == 200
        await asyncio.sleep(0.05)
        assert adapter._spawn_reaction_task.call_count == 1
    finally:
        await adapter._supervisor.stop()


@pytest.mark.asyncio
async def test_rejected_teams_gates_do_not_schedule_reaction(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    adapter = _start_supervisor(_make_adapter(reactions={"enabled": True}), max_active=0, max_pending=0)
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    adapter._spawn_reaction_task = MagicMock()
    try:
        assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-tenant", tenant_id=OTHER_USER_ID)))).status == 200
        assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-group-no-mention", conversation_type="groupChat", text="hello Marlow")))).status == 200
        assert (await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-capacity")))).status == 503
    finally:
        await adapter._supervisor.stop()

    adapter._spawn_reaction_task.assert_not_called()


@pytest.mark.asyncio
async def test_reaction_task_cleanup_cancels_and_clears_state():
    adapter = _make_adapter(reactions={"enabled": True})
    adapter._reaction_api_clients["cached"] = object()
    blocked = asyncio.Event()

    async def never_finishes():
        await blocked.wait()

    task = asyncio.create_task(never_finishes())
    adapter._reaction_tasks.add(task)

    await adapter._cancel_reaction_tasks()

    assert task.done()
    assert task.cancelled()
    assert not adapter._reaction_tasks
    assert not adapter._reaction_api_clients


@pytest.mark.asyncio
async def test_ack_reaction_429_retries_once_with_retry_after(monkeypatch):
    adapter = _make_adapter(reactions={"enabled": True})
    fake_api = MagicMock()
    retry_error = RuntimeError("rate limit")
    retry_error.response = teams.HttpResponse(body={}, status=429, headers={"Retry-After": "0"})
    fake_api.conversations.add_reaction = AsyncMock(side_effect=[retry_error, None])
    adapter._reaction_api_for_service_url = MagicMock(return_value=fake_api)
    target = teams._reaction_target_for_event(_reaction_event())

    result = await adapter._add_acknowledgement_reaction(target)

    assert result == teams.TeamsReactionResult.SUCCESS
    assert fake_api.conversations.add_reaction.await_count == 2


@pytest.mark.asyncio
async def test_ack_reaction_429_long_retry_after_does_not_retry():
    adapter = _make_adapter(reactions={"enabled": True})
    fake_api = MagicMock()
    retry_error = RuntimeError("rate limit")
    retry_error.response = teams.HttpResponse(body={}, status=429, headers={"Retry-After": "60"})
    fake_api.conversations.add_reaction = AsyncMock(side_effect=[retry_error, None])
    adapter._reaction_api_for_service_url = MagicMock(return_value=fake_api)
    target = teams._reaction_target_for_event(_reaction_event())

    result = await adapter._add_acknowledgement_reaction(target)

    assert result == teams.TeamsReactionResult.REMOTE_RATE_LIMITED
    assert fake_api.conversations.add_reaction.await_count == 1


@pytest.mark.asyncio
async def test_ack_reaction_404_and_403_are_not_retried(monkeypatch):
    adapter = _make_adapter(reactions={"enabled": True})
    fake_api = MagicMock()
    not_found_error = RuntimeError("not found")
    not_found_error.response = teams.HttpResponse(body={}, status=404)
    fake_api.conversations.add_reaction = AsyncMock(side_effect=not_found_error)
    adapter._reaction_api_for_service_url = MagicMock(return_value=fake_api)
    target = teams._reaction_target_for_event(_reaction_event())

    result = await adapter._add_acknowledgement_reaction(target)

    assert result == teams.TeamsReactionResult.NOT_FOUND
    assert fake_api.conversations.add_reaction.await_count == 1


@pytest.mark.asyncio
async def test_ack_reaction_failure_does_not_break_final_response():
    adapter = _start_supervisor(_make_adapter(reactions={"enabled": True}))
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    adapter._add_acknowledgement_reaction = AsyncMock(side_effect=RuntimeError("boom"))

    async def handler(event):
        return "ok"

    adapter.set_message_handler(handler)
    adapter.send = AsyncMock(return_value=teams.SendResult(success=True))
    try:
        response = await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-reaction-fail")))
        assert response.status == 200
        for _ in range(20):
            if adapter.send.await_count:
                break
            await asyncio.sleep(0.01)
        assert adapter.send.await_count == 1
    finally:
        await adapter._supervisor.stop()


@pytest.mark.asyncio
async def test_slow_reaction_task_does_not_delay_http_ack_or_handler_start():
    adapter = _start_supervisor(_make_adapter(reactions={"enabled": True}))
    adapter._receipt_store = teams.TeamsReceiptStore(teams.get_marlow_home() / "teams" / "receipts")
    reaction_started = asyncio.Event()
    release_reaction = asyncio.Event()
    handler_started = asyncio.Event()

    async def slow_reaction(target):
        reaction_started.set()
        await release_reaction.wait()
        return teams.TeamsReactionResult.SUCCESS

    async def handler(event):
        handler_started.set()
        return "ok"

    adapter._add_acknowledgement_reaction = AsyncMock(side_effect=slow_reaction)
    adapter.set_message_handler(handler)

    response = await adapter._handle_teams_activity(MagicMock(body=_activity(activity_id="msg-slow-reaction")))
    assert response.status == 200
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    await asyncio.wait_for(reaction_started.wait(), timeout=1)

    release_reaction.set()
    await asyncio.sleep(0)
    await adapter._supervisor.stop()

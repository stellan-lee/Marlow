"""Business policies exercised at the new boundary, without paid inference."""

import json
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace

import pytest
from tests.runtime.test_codex_runtime import profile, new_agent, events


def test_same_session_id_cannot_import_another_chat_history(profile):
    first = new_agent(
        session_id="same", user_id="same-user", chat_id="room-A", platform="telegram"
    )
    assert first.run_conversation("ROOM_A_PRIVATE")["completed"]
    first.close()
    other = new_agent(
        session_id="same", user_id="same-user", chat_id="room-B", platform="telegram"
    )
    result = other.run_conversation("new input")
    assert not result["completed"]
    assert "different" in result["error"]
    assert other._session_messages == []
    other.close()
    assert (
        len([event for event in events(profile) if event["method"] == "turn/start"])
        == 1
    )


def test_stop_during_startup_dispatches_no_model_turn(profile, monkeypatch):
    from agent.transports.codex_app_server_session import CodexAppServerSession

    original = CodexAppServerSession.ensure_started
    agent = new_agent()

    def stop_after_startup(session):
        thread = original(session)
        agent.interrupt("Stop received while initializing")
        return thread

    monkeypatch.setattr(CodexAppServerSession, "ensure_started", stop_after_startup)
    result = agent.run_conversation("cancel before model")
    assert result["status"] == "cancelled"
    assert not any(event["method"] == "turn/start" for event in events(profile))
    agent.close()


def test_profile_directory_plugins_load_and_keep_relative_imports_separate(tmp_path):
    from marlow_constants import set_marlow_home_override, reset_marlow_home_override
    from run_agent import AIAgent

    agents = []
    try:
        for tenant in ("alpha", "beta"):
            root = tmp_path / tenant
            plugin = root / "plugins/tenant"
            plugin.mkdir(parents=True)
            (root / "config.yaml").write_text("plugins:\n  enabled: [tenant]\n")
            (plugin / "plugin.yaml").write_text(
                "name: tenant\nversion: 1.0.0\ndescription: Tenant fixture\n"
            )
            (plugin / "helpers.py").write_text(f"MARKER = {tenant!r}\n")
            (plugin / "__init__.py").write_text(
                "def read(args, **kwargs):\n    from .helpers import MARKER\n    return MARKER\n"
                'def register(ctx):\n    ctx.register_tool(name="tenant_read",toolset="tenant",'
                'schema={"name":"tenant_read","parameters":{"type":"object"}},handler=read)\n'
            )
            token = set_marlow_home_override(root)
            try:
                agents.append(AIAgent(enabled_toolsets=["tenant"], skip_memory=True))
            finally:
                reset_marlow_home_override(token)
        # Import helper lazily after both directory modules have loaded.
        for agent, tenant in zip(agents, ("alpha", "beta")):
            assert "tenant_read" in agent.valid_tool_names
            assert agent._dispatch_business_tool("tenant_read", {}) == tenant
    finally:
        for agent in agents:
            agent.close()


def test_files_use_real_codex_sandbox_atomic_writes_and_v4_patch(profile):
    binary = shutil.which("codex")
    if binary is None:
        pytest.skip("Codex CLI unavailable")
    from agent.codex_execution import execute
    from agent.transports.codex_app_server import CodexAppServerClient
    from agent.transports.codex_app_server_session import CodexAppServerSession

    agent = new_agent()
    session = CodexAppServerSession(environment=agent.environment)
    client = CodexAppServerClient(codex_bin=binary, environment=agent.environment)
    client.initialize(capabilities={"experimentalApi": True})
    session._client = client
    agent._codex_session = session
    target = agent.environment.workspace / "example.txt"
    target.write_bytes(b"\xef\xbb\xbffirst\r\nsecond\r\n")
    try:
        result = execute(
            agent,
            "patch",
            {"path": "example.txt", "old_string": "first", "new_string": "changed"},
        )
        assert result["success"], result
        assert target.read_bytes() == b"\xef\xbb\xbfchanged\r\nsecond\r\n"
        result = execute(
            agent,
            "patch",
            {"path": "example.txt", "old_string": "missing", "new_string": "bad"},
        )
        assert not result["success"]
        assert agent._failed_mutations
        patch = "*** Begin Patch\n*** Update File: example.txt\n@@\n-changed\n+patched\n*** Add File: created.txt\n+created\n*** End Patch"
        result = execute(agent, "patch", {"mode": "patch", "patch": patch})
        assert result["success"], result
        assert b"patched" in target.read_bytes()
        assert (target.parent / "created.txt").read_text() == "created"
        assert not list(target.parent.glob(".marlow-write-*"))
        # A small read must not load the entire sparse log into memory.
        large = target.parent / "large.log"
        with large.open("wb") as stream:
            stream.write(b"first line\n")
            stream.truncate(1024 * 1024 * 1024)
        assert (
            execute(agent, "read_file", {"path": "large.log", "limit": 1})["result"]
            == "first line\n"
        )
        image = target.parent / "input.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        pixels = agent._dispatch_business_tool(
            "vision_analyze", {"image_url": str(image), "question": "Inspect"}
        )
        assert pixels["_multimodal"]
        assert pixels["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )
        with pytest.raises(PermissionError):
            agent._dispatch_business_tool(
                "vision_analyze",
                {"image_url": str(profile / "runtime/codex/auth.json")},
            )
        with pytest.raises(PermissionError):
            execute(
                agent,
                "write_file",
                {
                    "path": str(profile / "runtime/codex/auth.json"),
                    "content": "forbidden",
                },
            )
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_telegram_static_sticker_is_native_input_without_auxiliary_inference(
    profile,
):
    from unittest.mock import AsyncMock
    from gateway.platforms.telegram import TelegramAdapter
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource
    from gateway.config import Platform

    download = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=b"RIFF\x00\x00\x00\x00WEBPfixture")
    )
    sticker = SimpleNamespace(
        is_animated=False,
        is_video=False,
        emoji="x",
        get_file=AsyncMock(return_value=download),
    )
    event = MessageEvent(
        text="Inspect",
        message_type=MessageType.PHOTO,
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id="room", user_id="actor"
        ),
    )
    await TelegramAdapter._handle_sticker(
        object(), SimpleNamespace(sticker=sticker), event
    )
    assert event.media_types == ["image/webp"]
    assert Path(event.media_urls[0]).read_bytes().startswith(b"RIFF")
    assert "Inspect" in event.text
    assert not (profile / "runtime/codex/fixture-events.jsonl").exists()


def test_memory_access_change_cannot_resume_thread_or_history(profile):
    from agent.memory_workspace import gateway_memory_principal
    from run_agent import AIAgent

    routing = dict(
        platform="telegram",
        user_id="alice",
        chat_id="private-chat",
        session_id="stable",
        enabled_toolsets=[],
    )
    owner = AIAgent(
        **routing,
        chat_type="dm",
        memory_principal=gateway_memory_principal("telegram", "alice"),
    )
    try:
        assert owner.environment.memory is not None
        assert owner.run_conversation("private input")["completed"]
    finally:
        owner.close()

    denied = AIAgent(**routing, chat_type="group")
    try:
        assert denied.environment.memory is None
        result = denied.run_conversation("public input")
        assert not result["completed"]
        assert "Session memory access changed" in result["error"]
        assert denied._session_messages == []
    finally:
        denied.close()
    assert sum(event["method"] == "turn/start" for event in events(profile)) == 1


def test_legacy_memory_access_restrictions_are_preserved(profile):
    from agent.codex_execution import allowed_path
    from run_agent import AIAgent

    read_only = AIAgent(enabled_toolsets=[], memory_write_mode="read-only")
    try:
        note = read_only.environment.memory / "note.md"
        assert allowed_path(read_only, str(note)) == note
        with pytest.raises(PermissionError):
            allowed_path(read_only, str(note), write=True)
        filesystem = read_only.environment.overrides()["permissions"]["marlow"]["filesystem"]
        assert filesystem[str(note.parent)] == "read"
    finally:
        read_only.close()

    with (profile / "config.yaml").open("a") as config:
        config.write("memory:\n  memory_enabled: false\n  user_profile_enabled: false\n")
    disabled = AIAgent(enabled_toolsets=[])
    try:
        assert disabled.environment.memory is None
    finally:
        disabled.close()


def test_upgrade_opens_clean_native_thread_and_preserves_session_history(profile):
    agent = new_agent(session_id="legacy")
    try:
        db = agent._get_session_db_for_recall()
        db.create_session("legacy", "sdk")
        db.append_message("legacy", "user", "Earlier visible message")
        agent.state.thread("legacy-native-thread")
        assert agent.run_conversation("Continue")["completed"]
        assert any(
            row.get("content") == "Earlier visible message"
            for row in db.get_messages("legacy")
        )
    finally:
        agent.close()
    recorded = events(profile)
    assert any(event["method"] == "thread/start" for event in recorded)
    assert not any(event["method"] == "thread/resume" for event in recorded)

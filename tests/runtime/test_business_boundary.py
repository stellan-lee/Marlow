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


@pytest.mark.parametrize("local", [True, False])
def test_memory_recall_egress_and_raw_history(profile, local):
    from run_agent import AIAgent
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import MemoryProvider

    class Provider(MemoryProvider):
        name = "fixture"

        def is_available(self):
            return True

        def initialize(self, session_id, **kwargs):
            pass

        def get_tool_schemas(self):
            return []

        def prefetch(self, query, session_id="", **kwargs):
            return "RECALL_PRIVATE_MARKER"

    endpoint = "http://127.0.0.1:9999" if local else "https://provider.invalid/v1"
    config = profile / "runtime/codex/config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        'model_provider="fixture"\n[model_providers.fixture]\nname="Fixture"\nwire_api="responses"\nbase_url="'
        + endpoint
        + '"\nrequires_openai_auth=false\n'
    )
    agent = AIAgent(
        enabled_toolsets=[], skip_memory=False, automatic_memory_ingestion_enabled=False
    )
    manager = MemoryManager()
    manager.add_provider(Provider())
    agent._memory_manager = manager
    agent._memory_sync_enabled = False
    result = agent.run_conversation("authorized recall query")
    assert result["completed"], result
    dispatched = next(
        event for event in events(profile) if event["method"] == "turn/start"
    )
    assert ("RECALL_PRIVATE_MARKER" in json.dumps(dispatched["params"])) is local
    assert "RECALL_PRIVATE_MARKER" not in json.dumps(
        agent._session_db.get_messages(agent.session_id)
    )
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

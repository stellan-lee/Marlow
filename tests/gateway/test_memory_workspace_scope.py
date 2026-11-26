from __future__ import annotations

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from gateway.config import AdminApprovalConfig, Platform
from gateway.run import (
    _telegram_digital_twin_context,
    _private_memory_principal,
)
from gateway.session import SessionSource
from gateway.platforms.base import MessageEvent


def _source(
    *,
    platform: Platform = Platform.TELEGRAM,
    chat_type: str = "dm",
    user_id: str | None = "12345",
    chat_id: str = "12345",
    thread_id: str | None = None,
) -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        thread_id=thread_id,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"stateless_thread": True},
        {"guest": True},
        {"skip_memory": True},
        {"source": _source(chat_type="group")},
        {"source": _source(chat_type="channel")},
        {"source": _source(user_id=None)},
        {"source": _source(platform="email")},
        {"source": _source(platform="webhook")},
        {"event": None},
        {"event": SimpleNamespace(source=_source(), internal=True)},
        {"event": SimpleNamespace(source=_source(user_id="other"), internal=False)},
        {"event": SimpleNamespace(source=_source(), internal=False, external_conversation_snapshot=object())},
    ],
)
def test_private_memory_denied_outside_persistent_authenticated_dm(overrides):
    from gateway.platforms.base import MessageEvent

    source = _source()
    arguments = {
        "source": source,
        "event": MessageEvent(text="remember", source=source),
        "stateless_thread": False,
        "guest": False,
        "skip_memory": False,
    }
    arguments.update(overrides)
    if "event" not in overrides:
        arguments["event"] = MessageEvent(text="remember", source=arguments["source"])
    assert _private_memory_principal(**arguments) is None


def test_private_memory_uses_host_routed_identity():
    from agent.memory_workspace import gateway_memory_principal
    from gateway.platforms.base import MessageEvent

    source = _source()
    assert _private_memory_principal(
        source,
        event=MessageEvent(text="remember", source=source),
        stateless_thread=False,
        guest=False,
        skip_memory=False,
    ) == gateway_memory_principal(Platform.TELEGRAM, "12345")


def test_memory_identity_is_stable_across_private_chats_and_scoped_by_platform():
    from agent.memory_workspace import gateway_memory_principal

    source = _source(chat_id="another-private-chat", thread_id="new-thread")
    assert _private_memory_principal(
        source,
        event=MessageEvent(text="recall", source=source),
        stateless_thread=False,
        guest=False,
        skip_memory=False,
    ) == gateway_memory_principal(Platform.TELEGRAM, "12345")
    assert gateway_memory_principal("telegram", "12345") != gateway_memory_principal("slack", "12345")


def test_every_non_super_admin_telegram_source_resolves_as_digital_twin() -> None:
    config = {
        "experience": {
            "telegram_digital_twin": {
                "enabled": True,
                "system_prompt": "Global guest prompt",
            }
        }
    }
    admin = AdminApprovalConfig(
        enabled=True,
        conversation_mode="super_admin",
        platform=Platform.TELEGRAM,
        user_id="admin-user",
        chat_id="admin-chat",
        thread_id="admin-topic",
    )

    exact_admin = _telegram_digital_twin_context(
        _source(
            user_id="admin-user",
            chat_id="admin-chat",
            thread_id="admin-topic",
        ),
        config,
        admin,
        is_super_admin_source=True,
    )
    sibling_topic = _telegram_digital_twin_context(
        _source(chat_type="group", chat_id="admin-chat", thread_id="other-topic"),
        config,
        admin,
        is_super_admin_source=False,
    )
    other_chat = _telegram_digital_twin_context(
        _source(chat_type="group", chat_id="guest-chat"),
        config,
        admin,
        is_super_admin_source=False,
    )

    assert exact_admin.enabled is False
    assert sibling_topic.enabled is True
    assert sibling_topic.system_prompt == "Global guest prompt"
    assert other_chat.enabled is True


def test_non_admin_user_in_super_admin_chat_is_still_a_guest() -> None:
    config = {
        "experience": {
            "telegram_digital_twin": {
                "enabled": True,
            }
        }
    }
    admin = AdminApprovalConfig(
        enabled=True,
        conversation_mode="super_admin",
        platform=Platform.TELEGRAM,
        user_id="admin-user",
        chat_id="admin-chat",
    )

    exact_admin = _telegram_digital_twin_context(
        _source(
            chat_type="group",
            user_id="admin-user",
            chat_id="admin-chat",
            thread_id="topic-2",
        ),
        config,
        admin,
        is_super_admin_source=True,
    )
    guest_context = _telegram_digital_twin_context(
        _source(
            chat_type="group",
            user_id="guest-user",
            chat_id="admin-chat",
            thread_id="topic-2",
        ),
        config,
        admin,
        is_super_admin_source=False,
    )

    assert exact_admin.enabled is False
    assert guest_context.enabled is True
    assert "digital twin" in guest_context.system_prompt


def test_guest_delivery_scope_uses_same_role_boundary(monkeypatch):
    from gateway.run import GatewayRunner, _guest_memory_workspace_scope

    config = {"experience": {"telegram_digital_twin": {"enabled": True}}}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(admin_approval=AdminApprovalConfig(
        enabled=True,
        conversation_mode="super_admin",
        platform=Platform.TELEGRAM,
        user_id="admin-user",
        chat_id="admin-chat",
    ))
    guest = _source(user_id="guest-user", chat_id="guest-chat")
    owner = _source(user_id="admin-user", chat_id="admin-chat")
    assert runner._guest_workspace_scope(guest) == _guest_memory_workspace_scope(guest)
    assert runner._guest_workspace_scope(owner) is None


class _CapturingAgent:
    last_call: tuple[object, dict[str, object]] | None = None
    last_init: dict[str, object] | None = None
    last_guest_role: bool | None = None

    def __init__(self, *args, **kwargs) -> None:
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message, **kwargs):
        type(self).last_call = (user_message, dict(kwargs))
        type(self).last_guest_role = getattr(
            self,
            "_telegram_digital_twin_guest",
            False,
        )
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


@pytest.mark.asyncio
async def test_run_agent_passes_host_authorized_private_memory_scope(
    monkeypatch,
    tmp_path,
) -> None:
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_marlow_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        gateway_run, "_resolve_gateway_model", lambda config=None: "model"
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "ollama",
            "api_mode": "chat_completions",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_key": "***",
        },
    )

    import marlow_cli.tools_config as tools_config

    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda user_config, platform_key: {"core"},
    )

    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_providers = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace()
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")

    _CapturingAgent.last_call = None
    result = await runner._run_agent(
        message="[System note]\n\nExpanded user message with /tmp/attachment",
        raw_user_message="Please inspect the attachment",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="session-1",
        session_key="agent:main:telegram:dm:12345",
        event=MessageEvent(text="Please inspect the attachment", source=_source()),
    )

    assert result["final_response"] == "ok"
    wire_message, call_kwargs = _CapturingAgent.last_call
    assert "Expanded user message" in wire_message
    assert "raw_user_message" not in call_kwargs
    assert "turn_origin" not in call_kwargs
    from agent.memory_workspace import gateway_memory_principal

    assert _CapturingAgent.last_init["memory_principal"] == gateway_memory_principal("telegram", "12345")
    assert _CapturingAgent.last_init["skip_memory"] is False


@pytest.mark.asyncio
async def test_run_agent_composes_guest_prompt_and_isolates_owner_history(
    monkeypatch,
    tmp_path,
) -> None:
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_marlow_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "experience": {
                "telegram_digital_twin": {
                    "enabled": True,
                    "system_prompt": "Global guest prompt",
                }
            }
        },
    )
    monkeypatch.setattr(
        gateway_run, "_resolve_gateway_model", lambda config=None: "model"
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "ollama",
            "api_mode": "chat_completions",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_key": "***",
        },
    )

    import marlow_cli.tools_config as tools_config

    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda user_config, platform_key: {"core"},
    )

    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_providers = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        streaming=None,
        admin_approval=AdminApprovalConfig(
            enabled=True,
            conversation_mode="super_admin",
            platform=Platform.TELEGRAM,
            user_id="admin-user",
            chat_id="admin-chat",
        ),
    )
    runner.session_store = SimpleNamespace()
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")

    source = _source(
        chat_type="group",
        chat_id="guest-chat",
        user_id="guest-user",
    )
    _CapturingAgent.last_call = None
    _CapturingAgent.last_init = None
    _CapturingAgent.last_guest_role = None
    result = await runner._run_agent(
        message="Please help",
        raw_user_message="Please help",
        context_prompt="Telegram context",
        history=[{"role": "assistant", "content": "owner private memory"}],
        source=source,
        session_id="session-guest",
        session_key="agent:main:telegram:group:guest-chat:guest-user",
        channel_prompt="Channel-specific prompt",
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init["ephemeral_system_prompt"] == (
        "Telegram context\n\n"
        "Global guest prompt\n\n"
        "Channel-specific prompt"
    )
    assert _CapturingAgent.last_init["skip_memory"] is True
    assert _CapturingAgent.last_init["memory_principal"] is None
    assert _CapturingAgent.last_init["session_id"] == gateway_run._guest_session_id(source)
    assert _CapturingAgent.last_init["workspace_scope"] == gateway_run._guest_memory_workspace_scope(source)
    assert "session_search" in _CapturingAgent.last_init["disabled_toolsets"]
    _, call_kwargs = _CapturingAgent.last_call
    assert "turn_origin" not in call_kwargs
    assert "raw_user_message" not in call_kwargs
    assert call_kwargs["conversation_history"] == []
    assert _CapturingAgent.last_guest_role is True

@pytest.mark.asyncio
async def test_guest_turns_preserve_owner_slot_and_resume_only_guest_history(monkeypatch, tmp_path):
    from datetime import datetime
    from gateway.config import GatewayConfig
    from gateway.session import SessionEntry, SessionStore, build_session_key
    from tests.gateway.test_gateway_stateless_teams import _runner

    runner, adapter = _runner()
    source = _source(user_id="guest-user", chat_id="guest-chat", thread_id="topic")
    event = MessageEvent(text="guest request", source=source)
    key = build_session_key(source)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config = GatewayConfig()
    runner._session_db = None
    runner._guest_workspace_scope = lambda source: gateway_run._guest_memory_workspace_scope(source)
    runner._is_session_run_current = lambda *_args: True
    runner._prepare_inbound_message_text = AsyncMock(return_value="guest request")
    runner.session_store = SessionStore(tmp_path / "sessions", runner.config)
    now = datetime.now()
    owner = SessionEntry(key, "owner-session", now, now, origin=source)
    runner.session_store._entries[key] = owner
    runner.session_store._loaded = True
    runner.session_store._save()
    runner.session_store._db.create_session("owner-session", "telegram", user_id=source.user_id)
    runner.session_store.append_to_transcript("owner-session", {"role": "assistant", "content": "OWNER_PRIVATE"})
    owner_history = runner.session_store.load_transcript("owner-session")
    owner_before = owner.to_dict()
    calls = []

    async def run_guest(**kwargs):
        runner.session_store._db.create_session(kwargs["session_id"], "telegram", user_id=source.user_id)
        calls.append(kwargs)
        return {
            "final_response": "guest answer", "messages": [], "api_calls": 1,
            "completed": True, "session_id": kwargs["session_id"],
        }

    runner._run_agent = run_guest
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda *args: "model")
    for _ in range(2):
        result = await runner._handle_message_with_agent(event, source, key, 1)
        assert result == "guest answer"
    expected = gateway_run._guest_session_id(source)
    assert [call["session_id"] for call in calls] == [expected, expected]
    assert calls[0]["history"] == []
    assert any(row.get("content") == "guest answer" for row in calls[1]["history"])
    assert "OWNER_PRIVATE" not in str(calls)
    assert owner.to_dict() == owner_before
    assert runner.session_store._entries[key] is owner
    assert runner.session_store.load_transcript("owner-session") == owner_history
    assert all(row.get("content") != "OWNER_PRIVATE" for row in runner.session_store.load_transcript(expected))


@pytest.mark.parametrize("command", ["resume", "retry", "compress", "topic", "title", "new", "status", "debug"])
def test_guest_private_session_commands_are_rejected(command):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._guest_workspace_scope = lambda _source: "guest-scope"
    assert runner._guest_command_rejection(_source(), command)
    assert runner._guest_command_rejection(_source(), "help") is None
    assert runner._guest_command_rejection(_source(), "whoami") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "role", "projects", "source", "authorization", "native_config"])
async def test_compaction_rechecks_cached_authorization(monkeypatch, change):
    from unittest.mock import Mock
    from agent.memory_workspace import gateway_memory_principal

    runner = object.__new__(gateway_run.GatewayRunner)
    source = _source()
    config = {"runtime": {"codex": {"projects": []}}}
    runner.config = SimpleNamespace(admin_approval=AdminApprovalConfig())
    runner._session_model_overrides = {}
    runner._guest_workspace_scope = lambda _source: None
    runner._is_user_authorized = lambda _source: True
    runner.session_store = None
    key = runner._session_key_for_source(source)
    principal = gateway_memory_principal(source.platform, source.user_id)
    agent = SimpleNamespace(compact=Mock(return_value=True))
    agent._gateway_authorization_fingerprint = runner._gateway_authorization_fingerprint(source, config, principal)
    runner._agent_cache = {key: (agent, "signature")}
    if change == "role":
        runner._guest_workspace_scope = lambda _source: "guest-scope"
    elif change == "projects":
        config["runtime"]["codex"]["projects"] = [{"path": "/changed", "writable": False}]
    elif change == "source":
        source.user_id = "another-user"
        runner._session_key_for_source = lambda _source: key
    elif change == "authorization":
        runner._is_user_authorized = lambda _source: False
    elif change == "native_config":
        from marlow_constants import get_marlow_home

        native = get_marlow_home() / "runtime" / "codex" / "config.toml"
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_text('model = "changed-model"\n')
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    await runner._handle_compress_command(MessageEvent(text="/compress", source=source))
    if change is None:
        agent.compact.assert_called_once_with()
    else:
        agent.compact.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["resume", "retry", "compress", "topic", "title", "new", "status", "debug"])
async def test_gateway_blocks_guest_owner_commands_before_dispatch(monkeypatch, command):
    from unittest.mock import Mock

    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner.session_store = Mock()
    runner._is_user_authorized = lambda _source: True
    runner._guest_workspace_scope = gateway_run._guest_memory_workspace_scope
    monkeypatch.setattr("marlow_cli.plugins.invoke_hook", lambda *args, **kwargs: [])
    result = await runner._handle_message(MessageEvent(text="/" + command, source=_source()))
    assert "private session owner" in result
    runner.session_store.get_or_create_session.assert_not_called()
    runner.session_store.load_transcript.assert_not_called()


def test_guest_attachments_use_authorized_guest_workspace(tmp_path):
    from agent.codex_environment import gateway_environment
    from agent.codex_execution import allowed_path

    runner = object.__new__(gateway_run.GatewayRunner)
    source = _source(user_id="guest-user", chat_id="guest-chat")
    runner._guest_workspace_scope = gateway_run._guest_memory_workspace_scope
    owner_environment = gateway_environment(source)
    private = owner_environment.workspace / "owner-private.md"
    private.write_text("OWNER_PRIVATE")
    received = tmp_path / "received.txt"
    received.write_text("GUEST_ATTACHMENT")
    staged = gateway_run.Path(runner._stage_runtime_attachment(source, str(received)))
    guest_environment = runner._conversation_codex_environment(source)
    agent = SimpleNamespace(environment=guest_environment, session_cwd=str(guest_environment.workspace))
    assert staged.parent == guest_environment.workspace / "inbox"
    assert staged.read_text() == "GUEST_ATTACHMENT"
    assert not staged.is_relative_to(owner_environment.workspace)
    assert allowed_path(agent, staged) == staged
    with pytest.raises(PermissionError):
        allowed_path(agent, private)
    assert private.read_text() == "OWNER_PRIVATE"


def test_guest_can_steer_only_its_own_active_guest_session():
    runner = object.__new__(gateway_run.GatewayRunner)
    source = _source()
    key = "route-key"
    runner._guest_workspace_scope = lambda _source: "guest-scope"
    runner._session_key_for_source = lambda _source: key
    runner._running_agents = {key: SimpleNamespace(session_id="owner-session")}
    assert runner._guest_command_rejection(source, "steer")
    runner._running_agents[key] = SimpleNamespace(
        session_id=gateway_run._guest_session_id(source),
        _telegram_digital_twin_guest=True,
    )
    assert runner._guest_command_rejection(source, "steer") is None

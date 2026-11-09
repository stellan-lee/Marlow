"""Regression tests for memory provider selection during AIAgent init."""

from types import SimpleNamespace
from unittest.mock import patch


class RecordingMemoryProvider:
    name = "recording"

    def __init__(self):
        self.init_kwargs = None
        self.init_session_id = None

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.init_session_id = session_id
        self.init_kwargs = dict(kwargs)

    def get_tool_schemas(self):
        return []

    def shutdown(self):
        pass


def test_blank_memory_provider_does_not_auto_enable_honcho():
    """Blank memory.provider should remain opt-out even if Honcho fallback looks configured."""
    cfg = {"memory": {"provider": ""}, "agent": {}}
    honcho_cfg = SimpleNamespace(enabled=True, api_key="stale-key", base_url=None)

    with (
        patch("marlow_cli.config.load_config", return_value=cfg),
        patch("marlow_cli.config.save_config") as save_config,
        patch(
            "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
            return_value=honcho_cfg,
        ) as from_global_config,
        patch("plugins.memory.load_memory_provider") as load_memory_provider,
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("model_tools.get_tool_definitions", return_value=[]),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
        )

    assert agent._memory_manager is None
    from_global_config.assert_not_called()
    load_memory_provider.assert_not_called()
    save_config.assert_not_called()


def test_gateway_agent_keeps_local_memory_with_retired_provider(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    db_file = tmp_path / "memory_store.db"
    db_file.write_bytes(b"historical data")
    cfg = {"memory": {"provider": "holographic", "memory_enabled": True}, "agent": {}}

    with (
        patch("marlow_cli.config.load_config", return_value=cfg),
        patch("marlow_cli.config.save_config") as save_config,
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("model_tools.get_tool_definitions", return_value=[]),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            model="test",
            quiet_mode=True,
            skip_context_files=True,
            platform="telegram",
        )

    assert agent._memory_store is not None
    assert agent._memory_manager is None
    assert "Holographic memory is no longer supported" in caplog.text
    assert db_file.read_bytes() == b"historical data"
    save_config.assert_not_called()
    agent.close()


def test_aiagent_forwards_user_id_alt_to_memory_provider():
    provider = RecordingMemoryProvider()
    cfg = {"memory": {"provider": "recording"}, "agent": {}}

    with (
        patch("marlow_cli.config.load_config", return_value=cfg),
        patch("plugins.memory.load_memory_provider", return_value=provider),
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("model_tools.get_tool_definitions", return_value=[]),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
            session_id="sess-alt",
            platform="feishu",
            user_id="open-id",
            user_id_alt="union-id",
        )

    assert agent._memory_manager is not None
    assert provider.init_session_id == "sess-alt"
    assert provider.init_kwargs["user_id"] == "open-id"
    assert provider.init_kwargs["user_id_alt"] == "union-id"
    assert provider.init_kwargs["platform"] == "feishu"

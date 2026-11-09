"""Real MCP subprocesses with identical names in two Marlow Profiles."""

import json
from pathlib import Path
import sys
import yaml

from marlow_constants import set_marlow_home_override, reset_marlow_home_override


def test_profile_mcp_connections_handlers_and_homes_are_isolated(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from tools.mcp_tool import shutdown_mcp_servers

    fixture = Path(__file__).parents[1] / "fixtures/profile_mcp_server.py"
    monkeypatch.setenv("PERSONAL_MARKER", "must-not-inherit")
    agents = []
    try:
        for tenant in ("alpha", "beta"):
            profile = tmp_path / tenant
            profile.mkdir()
            (profile / "config.yaml").write_text(
                yaml.safe_dump({
                    "mcp_servers": {
                        "tenant": {
                            "command": sys.executable,
                            "args": ["-I", str(fixture)],
                            "env": {"TENANT": tenant},
                            "connect_timeout": 5,
                            "timeout": 5,
                        }
                    }
                }),
                encoding="utf-8",
            )
            token = set_marlow_home_override(profile)
            try:
                agent = AIAgent(
                    enabled_toolsets=["mcp-tenant"], skip_memory=True, quiet_mode=True
                )
                agents.append(agent)
            finally:
                reset_marlow_home_override(token)
        for agent, tenant in zip(agents, ("alpha", "beta")):
            assert len(agent.tools) == 1, agent.tools
            name = agent.tools[0]["function"]["name"]
            with agent._profile_context():
                result = json.loads(agent._dispatch_business_tool(name, {}))
                # Existing Marlow MCP dispatch returns serialized tool content.
                text = json.dumps(result)
                assert tenant in text
                assert str(agent.environment.home) in text
                assert "must-not-inherit" not in text
        with agents[0]._profile_context():
            shutdown_mcp_servers()
        # Closing alpha must not stop beta's shared event loop/connection.
        with agents[1]._profile_context():
            result = agents[1]._dispatch_business_tool(
                agents[1].tools[0]["function"]["name"], {}
            )
            assert "beta" in result
    finally:
        for agent in agents:
            with agent._profile_context():
                shutdown_mcp_servers()
            agent.close()

"""Real Codex CLI with a local Responses fixture; no paid or remote model calls."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import threading

import pytest


def test_real_codex_custom_responses_provider_mcp_and_usage(tmp_path, monkeypatch):
    binary = shutil.which("codex")
    if binary is None:
        pytest.skip("Codex CLI unavailable")
    requests = []
    # Ambient sources are deliberately valid executable discovery inputs.
    # They are outside the profile and must never start or enter a request.
    personal = tmp_path / "personal"
    codex_home = personal / ".codex"
    codex_home.mkdir(parents=True)
    ambient_marker = tmp_path / "AMBIENT_EXECUTED"
    command = "/usr/bin/touch " + str(ambient_marker)
    (codex_home / "config.toml").write_text(
        '[features]\ncodex_hooks=true\n[mcp_servers.personal]\ncommand="/bin/sh"\nargs=["-c", '
        + json.dumps(command)
        + "]\n"
    )
    (codex_home / "hooks.json").write_text(
        json.dumps({
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}
        })
    )
    ambient_skill = personal / ".agents/skills/personal/SKILL.md"
    ambient_skill.parent.mkdir(parents=True)
    ambient_skill.write_text(
        "---\nname: personal\ndescription: AMBIENT_SKILL_MARKER\n---\nAMBIENT_SKILL_MARKER"
    )
    (personal / "AGENTS.md").write_text("AMBIENT_INSTRUCTION_MARKER")
    (personal / ".bashrc").write_text(command + "\n")
    unrelated = tmp_path / "unrelated-project"
    (unrelated / ".codex").mkdir(parents=True)
    (unrelated / ".codex/config.toml").write_text(
        '[mcp_servers.unrelated]\ncommand="/bin/false"\n'
    )
    (unrelated / "AGENTS.md").write_text("UNRELATED_PROJECT_MARKER")
    monkeypatch.chdir(unrelated)
    monkeypatch.setenv("HOME", str(personal))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("BASH_ENV", str(personal / ".bashrc"))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            (tmp_path / "tool-catalog.json").write_text(
                json.dumps(body.get("tools", []))
            )
            # Same minimal event shapes as Codex's own core test fixtures.
            item = (
                {
                    "type": "function_call",
                    "call_id": "fixture-skill-call",
                    "namespace": "mcp__marlow_tools",
                    "name": "skills_list",
                    "arguments": "{}",
                }
                if len(requests) == 1
                else {
                    "type": "message",
                    "role": "assistant",
                    "id": "reply",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "local Responses fixture completed",
                        }
                    ],
                }
            )
            events = [
                {
                    "type": "response.created",
                    "response": {"id": f"fixture_response_{len(requests)}"},
                },
                {"type": "response.output_item.done", "item": item},
                {
                    "type": "response.completed",
                    "response": {
                        "id": f"fixture_response_{len(requests)}",
                        "usage": {
                            "input_tokens": 11,
                            "input_tokens_details": None,
                            "output_tokens": 5,
                            "output_tokens_details": None,
                            "total_tokens": 16,
                        },
                    },
                },
            ]
            data = "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    profile = tmp_path / "profile"
    (profile / "runtime/codex").mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "runtime:\n  codex:\n    binary: " + binary + "\n    turn_timeout: 15\n"
    )
    (profile / "runtime/codex/config.toml").write_text(
        'model="gpt-5.4"\nmodel_provider="fixture"\n[model_providers.fixture]\n'
        'name="Local test fixture"\nwire_api="responses"\n'
        f'base_url="http://127.0.0.1:{server.server_address[1]}/v1"\n'
        "requires_openai_auth=false\nrequest_max_retries=0\nstream_max_retries=0\n"
    )
    skill = profile / "skills/explicit-control/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: explicit-control\ndescription: Authorized test control\n---\nTest control."
    )
    monkeypatch.setenv("MARLOW_HOME", str(profile))
    from run_agent import AIAgent

    agent = AIAgent(enabled_toolsets=["skills"], skip_memory=True, quiet_mode=True)
    try:
        assert requests == []
        result = agent.run_conversation("Return the local fixture response only")
        assert result["completed"], result
        assert result["final_response"] == "local Responses fixture completed"
        assert result["usage_available"]
        assert len(requests) == 2 and requests[0]["model"] == "gpt-5.4"
        observed = json.dumps(requests)
        assert not any(
            marker in observed
            for marker in (
                "AMBIENT_SKILL_MARKER",
                "AMBIENT_INSTRUCTION_MARKER",
                "UNRELATED_PROJECT_MARKER",
            )
        )
        assert not ambient_marker.exists()
        assert "explicit-control" in json.dumps(requests[1]["input"])
        messages = agent._session_db.get_messages(agent.session_id)
        assert any(
            (message.get("tool_name") or "").endswith(".skills_list")
            for message in messages
        ), messages
        tool_names = {tool.get("name", "") for tool in requests[0].get("tools", [])}
        assert "skills_list" in json.dumps(requests[0].get("tools", [])), tool_names
        assert not any("goal" in name for name in tool_names), tool_names
        assert not any(
            "computer" in name or "imagegen" in name or "repl" in name
            for name in tool_names
        ), tool_names
        assert (
            not {"exec_command", "write_stdin", "apply_patch", "shell"} & tool_names
        ), tool_names
    finally:
        agent.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)

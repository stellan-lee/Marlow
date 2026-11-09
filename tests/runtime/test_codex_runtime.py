"""Runtime behavior tests, using an actual subprocess protocol fixture."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
from agent.codex_environment import CodexEnvironment, CodexConfigurationError
from agent.codex_state import RuntimeState
from agent.transports.codex_app_server import CodexAppServerClient
from agent.transports.codex_app_server_session import (
    _approval_choice_to_codex_decision,
    _coerce_turn_input,
)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    root.mkdir()
    fake = tmp_path / "fixture-codex"
    shutil.copy(Path(__file__).parents[1] / "fixtures" / "codex_app_server.py", fake)
    fake.chmod(0o700)
    (root / "config.yaml").write_text(
        "runtime:\n  codex:\n    binary: " + str(fake) + "\n    turn_timeout: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MARLOW_HOME", str(root))
    return root


def new_agent(**kwargs):
    from run_agent import AIAgent

    return AIAgent(enabled_toolsets=[], skip_memory=True, **kwargs)


def events(root):
    return [
        json.loads(line)
        for line in (root / "runtime/codex/fixture-events.jsonl")
        .read_text()
        .splitlines()
    ]


def test_no_process_or_model_request_on_initialization(profile):
    agent = new_agent()
    assert not (profile / "runtime/codex/fixture-events.jsonl").exists()
    agent.close()


def test_protocol_turn_usage_raw_history_and_restart(profile, monkeypatch):
    monkeypatch.setenv("PERSONAL_MARKER", "must-not-inherit")
    agent = new_agent(session_id="stable")
    result = agent.run_conversation("explicit input")
    assert result["completed"], result
    assert result["final_response"] == "fixture reply"
    assert result["usage_available"]
    thread_id = result["codex_thread_id"]
    assert agent.session_total_tokens == 10
    agent.close()
    resumed = new_agent(session_id="stable")
    result = resumed.run_conversation("next input")
    assert result["completed"], result
    assert result["codex_thread_id"] == thread_id
    resumed.close()
    records = events(profile)
    assert sum(r["method"] == "thread/start" for r in records) == 1
    assert sum(r["method"] == "thread/resume" for r in records) == 1
    assert all(r["personal_marker"] is None for r in records)
    assert all(r["home"] == str(profile / "runtime/home") for r in records)
    assert all(r["cwd"] == str(profile / "workspace") for r in records)
    with sqlite3.connect(profile / "runtime/codex-runs.sqlite3") as db:
        assert db.execute("SELECT status FROM runs").fetchall() == [
            ("completed",),
            ("completed",),
        ]


@pytest.mark.parametrize("prompt", ["EXIT_FIXTURE", "HANG_FIXTURE"])
def test_failure_is_not_replayed_and_child_is_closed(profile, prompt):
    agent = new_agent(session_id="fault")
    result = agent.run_conversation(prompt)
    assert not result["completed"]
    agent.close()
    # No side-effecting tool was dispatched. A fresh user input may continue;
    # the adapter itself never retries/replays the failed turn.
    assert sum(r["method"] == "turn/start" for r in events(profile)) == 1
    later = new_agent(session_id="fault")
    result = later.run_conversation("new explicit input")
    assert result["completed"], result
    later.close()
    assert sum(r["method"] == "turn/start" for r in events(profile)) == 2


def test_scoped_threads_are_distinct(profile):
    ids = []
    for user in ("alice", "bob"):
        agent = new_agent(
            session_id="session-" + user,
            user_id=user,
            chat_id=user,
            platform="telegram",
        )
        result = agent.run_conversation("explicit input")
        assert result["completed"], result
        ids.append(result["codex_thread_id"])
        agent.close()
    assert len(set(ids)) == 2


def test_reused_session_id_cannot_cross_users(profile):
    a = new_agent(session_id="same", user_id="alice", platform="telegram")
    assert a.run_conversation("first")["completed"]
    a.close()
    b = new_agent(session_id="same", user_id="bob", platform="telegram")
    result = b.run_conversation("second")
    assert not result["completed"]
    assert "different user" in result["error"]
    b.close()


def test_profiles_do_not_share_homes_or_threads(tmp_path):
    a, b = (CodexEnvironment.build(tmp_path / name, parent={}) for name in ("a", "b"))
    assert a.home != b.home and a.codex_home != b.codex_home
    assert CodexEnvironment.build(tmp_path / "a", parent={}).codex_home == a.codex_home
    assert not (a.codex_home / "auth.json").exists()


def test_environment_allowlist_and_provider_credentials_stay_out_of_shell(tmp_path):
    e = CodexEnvironment.build(
        tmp_path / "p",
        parent={"HOME": "/personal", "OPENAI_API_KEY": "secret", "BASH_ENV": "/evil"},
    )
    assert e.env["HOME"] != "/personal"
    assert not {"OPENAI_API_KEY", "BASH_ENV"} & e.env.keys()
    (e.codex_home / "config.toml").write_text(
        'model_provider="custom"\n[model_providers.custom]\nname="fixture"\nwire_api="responses"\nenv_key="TEST_API_KEY"\n'
    )
    with pytest.raises(CodexConfigurationError, match="missing"):
        CodexEnvironment.build(tmp_path / "p", parent={})
    e = CodexEnvironment.build(
        tmp_path / "p",
        settings={"pass_env": ["TEST_API_KEY"]},
        parent={"TEST_API_KEY": "explicit"},
    )
    assert e.env["TEST_API_KEY"] == "explicit"
    assert "TEST_API_KEY" not in e.overrides()["shell_environment_policy"]["set"]


def test_invalid_custom_protocol_and_executable_config_fail(tmp_path):
    e = CodexEnvironment.build(tmp_path / "p", parent={})
    for text in (
        '[mcp_servers.personal]\ncommand="evil"',
        '[model_providers.custom]\nwire_api="chat"',
    ):
        (e.codex_home / "config.toml").write_text(text)
        with pytest.raises(CodexConfigurationError):
            CodexEnvironment.build(tmp_path / "p", parent={})


def test_symlinked_home_and_project_grants_fail_closed(tmp_path):
    personal = tmp_path / "personal"
    personal.mkdir()
    profile = tmp_path / "p"
    (profile / "runtime").mkdir(parents=True)
    (profile / "runtime/home").symlink_to(personal, target_is_directory=True)
    with pytest.raises(CodexConfigurationError, match="symlink"):
        CodexEnvironment.build(profile)
    with pytest.raises(CodexConfigurationError):
        CodexEnvironment.build(
            tmp_path / "q", settings={"projects": [{"path": str(tmp_path)}]}
        )


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("once", "accept"),
        ("session", "decline"),
        ("always", "decline"),
        ("deny", "decline"),
        ("expired", "decline"),
    ],
)
def test_approval_is_single_operation_only(choice, expected):
    assert _approval_choice_to_codex_decision(choice) == expected


def test_rich_input_preserves_images_and_rejects_unsupported_attachments():
    assert _coerce_turn_input([
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    ]) == [{"type": "image", "url": "data:image/png;base64,AA=="}]
    with pytest.raises(ValueError, match="Unsupported"):
        _coerce_turn_input([{"type": "audio", "url": "unapproved"}])


def test_explicit_skill_positive_control_and_no_ambient_skills(profile, tmp_path):
    path = tmp_path / "authorized" / "example"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("AUTHORIZED_SKILL_CONTROL")
    agent = new_agent()
    agent.runtime_config["skills"] = [str(path.parent)]
    assert "example" in agent._skill_tool("skills_list", {})
    assert (
        agent._skill_tool("skill_view", {"name": "example"})
        == "AUTHORIZED_SKILL_CONTROL"
    )
    with pytest.raises(PermissionError):
        agent._skill_tool("skill_view", {"name": "../../personal"})
    agent.close()


def test_mcp_bridge_requires_active_turn_and_trusted_tool_allowlist(profile):
    from agent.codex_bridge import BusinessBridge

    agent = new_agent()
    bridge = BusinessBridge(agent)
    try:
        req = {
            "id": 1,
            "method": "tools/call",
            "params": {"name": "send_message", "arguments": {"user_id": "admin"}},
        }
        with pytest.raises(PermissionError):
            bridge.request(req)
        bridge.activate()
        with pytest.raises(PermissionError):
            bridge.request(req)
    finally:
        bridge.close()
        agent.close()


def test_actual_codex_discovery_and_missing_auth_in_temporary_profile(tmp_path):
    """Real CLI, zero turn/start calls, no paid inference or business messages."""
    binary = shutil.which("codex")
    if binary is None:
        pytest.skip("Codex CLI unavailable")
    personal = tmp_path / "personal"
    (personal / ".codex").mkdir(parents=True)
    (personal / ".codex/config.toml").write_text(
        '[mcp_servers.personal]\ncommand="PERSONAL_MCP_MARKER"\n'
    )
    (personal / ".agents/skills/personal").mkdir(parents=True)
    (personal / ".agents/skills/personal/SKILL.md").write_text("PERSONAL_SKILL_MARKER")
    (personal / ".bashrc").write_text("touch PERSONAL_SHELL_MARKER")
    (personal / "AGENTS.md").write_text("PERSONAL_INSTRUCTIONS_MARKER")
    e = CodexEnvironment.build(
        personal / ".marlow/profiles/isolated",
        parent={"HOME": str(personal), "CODEX_HOME": str(personal / ".codex")},
    )
    with CodexAppServerClient(codex_bin=binary, environment=e) as client:
        client.initialize(capabilities={"experimentalApi": True})
        config = client.request(
            "config/read", {"includeLayers": True, "cwd": str(e.workspace)}
        )
        assert "personal" not in (config["config"].get("mcp_servers") or {})
        assert config["config"]["allow_login_shell"] is False
        assert config["config"]["cli_auth_credentials_store"] == "file"
        skills = client.request(
            "skills/list", {"cwds": [str(e.workspace)], "forceReload": True}
        )
        assert all(not entry["skills"] for entry in skills["data"])
        auth = client.request("account/read", {"refreshToken": False})
        assert auth["account"] is None
        assert not (personal / "PERSONAL_SHELL_MARKER").exists()


def test_pending_approval_cannot_turn_into_execution_after_cancel(profile, monkeypatch):
    from agent.codex_execution import approve_operation
    from tools.terminal_tool import set_approval_callback

    agent = new_agent()

    def cancelled(*args, **kwargs):
        agent.interrupt()
        return "once"

    set_approval_callback(cancelled)
    try:
        with pytest.raises(PermissionError, match="cancelled"):
            approve_operation(
                agent,
                "terminal",
                {"command": "rm example", "workdir": str(profile / "workspace")},
                always=True,
            )
    finally:
        set_approval_callback(None)
        agent.close()


def test_native_command_is_approved_before_rpc_and_noninteractive_denies(
    profile, monkeypatch
):
    from agent.codex_execution import execute

    agent = new_agent()

    class NeverExecute:
        def execute_command(self, *args, **kwargs):
            pytest.fail("Denied command reached execution")

    agent._codex_session = NeverExecute()
    with pytest.raises(PermissionError):
        execute(
            agent,
            "terminal",
            {"command": "rm -rf ./important", "purpose": "remove data"},
        )
    agent._codex_session = None
    agent.close()


def test_actual_tool_subprocess_home_project_symlink_and_scope_denials(tmp_path):
    binary = shutil.which("codex")
    if binary is None:
        pytest.skip("Codex CLI unavailable")
    personal = tmp_path / "personal"
    personal.mkdir()
    marker = personal / "secret-marker"
    marker.write_text("PERSONAL_PRIVATE_FILE")
    project = tmp_path / "authorized"
    project.mkdir()
    (project / "readable").write_text("AUTHORIZED_PROJECT_CONTROL")
    e = CodexEnvironment.build(
        tmp_path / "profile",
        settings={"projects": [{"path": str(project), "writable": False}]},
    )
    (e.workspace / "escape").symlink_to(personal, target_is_directory=True)
    with CodexAppServerClient(codex_bin=binary, environment=e) as client:
        client.initialize(capabilities={"experimentalApi": True})

        def command(script, cwd=None):
            return client.request(
                "command/exec",
                {
                    "command": ["/bin/sh", "-c", script],
                    "cwd": str(cwd or e.workspace),
                    "permissionProfile": "marlow",
                    "processId": str(time.monotonic_ns()),
                    "timeoutMs": 3000,
                },
                timeout=5,
            )

        result = command('printf "%s" "$HOME"')
        assert result["exitCode"] == 0, result
        assert result["stdout"] == str(e.home)
        assert command("cat " + str(marker))["exitCode"] != 0
        assert command("cat escape/secret-marker")["exitCode"] != 0
        result = command("cat readable", project)
        assert (
            result["exitCode"] == 0 and result["stdout"] == "AUTHORIZED_PROJECT_CONTROL"
        ), result
        assert command("touch forbidden-write", project)["exitCode"] != 0
        assert not (project / "forbidden-write").exists()
        assert not (e.codex_home / "auth.json").exists()


def test_uncertain_write_blocks_further_writes_in_same_turn(profile):
    from agent.codex_bridge import BusinessBridge

    agent = new_agent()
    agent.tools = [
        {"function": {"name": "send_message", "parameters": {"type": "object"}}}
    ]

    def uncertain(name, args):
        raise TimeoutError("Uncertain network write")

    agent._dispatch_business_tool = uncertain
    bridge = BusinessBridge(agent)
    try:
        bridge.activate()
        with pytest.raises(TimeoutError):
            bridge.request({
                "id": 1,
                "method": "tools/call",
                "params": {"name": "send_message", "arguments": {}},
            })
        assert agent._business_uncertain
    finally:
        bridge.close()
        agent.close()


def test_scoped_registry_and_mcp_connections_do_not_cross_profiles(tmp_path):
    from tools.registry import ToolRegistry
    from agent.profile_scope import ProfileMap
    from marlow_constants import set_marlow_home_override, reset_marlow_home_override

    registry = ToolRegistry()
    servers = ProfileMap()
    for name in ("alice", "bob"):
        token = set_marlow_home_override(tmp_path / name)
        try:
            registry.register(
                "tenant_read",
                "mcp-tenant",
                {"name": "tenant_read"},
                lambda args, tenant=name, **kw: tenant,
            )
            servers["tenant"] = name
        finally:
            reset_marlow_home_override(token)
    for name in ("alice", "bob"):
        token = set_marlow_home_override(tmp_path / name)
        try:
            assert registry.dispatch("tenant_read", {}) == name
            assert servers["tenant"] == name
        finally:
            reset_marlow_home_override(token)


def test_operator_verification_is_exact_and_never_replays(tmp_path):
    e = CodexEnvironment.build(tmp_path / "profile")
    state = RuntimeState(e.profile, {"session": "pending", "profile": str(e.profile)})
    state.thread("thread-A")
    run_id = state.start("explicit input")
    state.turn("turn-A")
    state.finish(
        run_id, thread_id="thread-A", turn_id="turn-A", status="needs_verification"
    )
    with pytest.raises(ValueError):
        state.acknowledge(thread_id="thread-A", turn_id="turn-B")
    state.acknowledge(thread_id="thread-A", turn_id="turn-A")
    assert state.mapping() == ("thread-A", "turn-A", "verified_by_operator")
    with state.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert (
            db.execute("SELECT status FROM runs").fetchone()[0] == "needs_verification"
        )


def test_reset_preserves_old_history_and_uses_new_thread(profile):
    agent = new_agent(session_id="original")
    first = agent.run_conversation("first")
    agent.reset_session_state("replacement")
    assert not agent._session_messages
    second = agent.run_conversation("second")
    assert first["codex_thread_id"] != second["codex_thread_id"]
    assert agent._session_db.get_messages("original")
    agent.close()


def test_oneshot_stdout_is_only_final_result(profile):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "marlow_cli.main",
            "-z",
            "explicit input",
            "-t",
            "memory",
        ],
        cwd=Path(__file__).parents[2],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "fixture reply\n"


def test_mcp_results_scrub_secrets_and_preserve_pixels(profile):
    from agent.codex_bridge import BusinessBridge

    agent = new_agent()
    agent.tools = [
        {"function": {"name": "browser_vision", "parameters": {"type": "object"}}}
    ]
    agent._dispatch_business_tool = lambda *a: {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": "Image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ],
    }
    bridge = BusinessBridge(agent)
    try:
        bridge.activate()
        value = bridge.request({
            "id": 1,
            "method": "tools/call",
            "params": {"name": "browser_vision", "arguments": {}},
        })
        assert value["content"][1] == {
            "type": "image",
            "mimeType": "image/png",
            "data": "AA==",
        }
    finally:
        bridge.close()
        agent.close()

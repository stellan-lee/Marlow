"""Entry-point regressions for terminal queries after removing the chat UIs."""

from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from marlow_cli import oneshot, query
from tools import approval

_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("argv", "expected", "code"),
    [
        ([], "Show available commands", 0),
        (["chat"], "provide a query", 2),
        (["--tui"], "unrecognized arguments", 2),
    ],
)
def test_removed_interface_entrypoints(tmp_path, argv, expected, code):
    result = subprocess.run(
        [sys.executable, str(_ROOT / "marlow"), *argv],
        env={**os.environ, "MARLOW_HOME": str(tmp_path), "MARLOW_TUI": "1"},
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == code
    assert expected in (result.stdout if code == 0 else result.stderr)
    assert "node" not in result.stderr


@pytest.mark.parametrize("yolo", [False, True])
def test_oneshot_checks_protected_actions(monkeypatch, capsys, yolo):
    monkeypatch.delenv("MARLOW_YOLO_MODE", raising=False)
    monkeypatch.delenv("MARLOW_ACCEPT_HOOKS", raising=False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "_get_approval_config", lambda: {"mode": "smart"})
    smart = MagicMock(return_value="approve")
    monkeypatch.setattr(approval, "_smart_approve", smart)
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": []},
    )

    def check():
        return (
            approval.check_all_command_guards("rm -rf /tmp/test-victim", "local"),
            approval.check_execute_code_guard("print('hello')", "local"),
        )

    def run_agent(_prompt, **_kwargs):
        if yolo:
            assert all(result["approved"] for result in check())
        else:
            # Some subagent threads do not inherit the parent's ContextVars.
            with ThreadPoolExecutor(max_workers=1) as pool:
                results = pool.submit(check).result()
            assert all(not result["approved"] for result in results)
            assert all(result["outcome"] == "blocked" for result in results)
            assert all("cannot prompt" in result["message"] for result in results)
        assert approval.check_all_command_guards("echo hello", "local")["approved"]
        assert not approval.check_all_command_guards("rm -rf /", "local")["approved"]
        assert "MARLOW_YOLO_MODE" not in os.environ
        assert "MARLOW_ACCEPT_HOOKS" not in os.environ
        return "final response"

    monkeypatch.setattr(oneshot, "_run_agent", run_agent)

    assert oneshot.run_oneshot("test", yolo=yolo) == 0
    assert capsys.readouterr().out == "final response\n"
    smart.assert_not_called()
    assert approval._headless_local_queries == 0


def test_oneshot_reports_retired_memory_provider_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr("marlow_cli.config.load_config", lambda: {"memory": {"provider": "holographic"}})
    monkeypatch.setattr(oneshot, "_run_agent", lambda *_a, **_kw: "answer")
    assert oneshot.run_oneshot("hello") == 0
    result = capsys.readouterr()
    assert result.out == "answer\n"
    assert "Holographic" in result.err


def test_query_keeps_raw_text_session_and_checkpoint(tmp_path, monkeypatch, capsys):
    from marlow_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("old-id", source="tui", model="local-test")
    db.append_message("old-id", "user", "previous prompt")
    db.end_session("old-id", "tui_close")
    monkeypatch.setattr("marlow_state.SessionDB", lambda: db)
    monkeypatch.setattr("marlow_cli.config.load_config", lambda: {"agent": {}, "memory": {}})
    monkeypatch.setattr("marlow_cli.config.read_raw_config", lambda: {})
    wait_for_mcp = MagicMock()
    monkeypatch.setattr("marlow_cli.mcp_startup.wait_for_mcp_discovery", wait_for_mcp)
    monkeypatch.setattr("marlow_cli.tools_config._get_platform_tools", lambda *_args: {"file"})
    monkeypatch.setattr(
        "marlow_cli.oneshot._resolve_query_runtime",
        lambda *_args: ("local-test", {"provider": "custom", "base_url": "http://localhost"}),
    )
    agent = MagicMock()
    agent.session_id = "old-id"
    agent.run_conversation.return_value = {"final_response": "answer"}
    make_agent = MagicMock(return_value=agent)
    monkeypatch.setattr("run_agent.AIAgent", make_agent)
    args = Namespace(
        query="next prompt", image=None, model=None, provider=None, quiet=True,
        source=None, resume="old-id", continue_last=None, no_restore_cwd=True,
        worktree=False, checkpoints=True, yolo=False, max_turns=None,
        toolsets=None, pass_session_id=True, ignore_user_config=True, ignore_rules=True,
    )

    assert query.run_query(args) == 0
    assert make_agent.call_args.kwargs["session_id"] == "old-id"
    assert make_agent.call_args.kwargs["checkpoints_enabled"]
    assert make_agent.call_args.kwargs["skip_context_files"]
    assert make_agent.call_args.kwargs["skip_memory"]
    sent = agent.run_conversation.call_args.kwargs
    assert sent["conversation_history"][0]["content"] == "previous prompt"
    assert sent["raw_user_message"] == "next prompt"
    assert sent["turn_origin"] == "classic_cli"
    wait_for_mcp.assert_called_once()
    agent.close.assert_called_once()
    output = capsys.readouterr()
    assert output.out == "answer\n"
    assert "session_id: old-id" in output.err


@pytest.mark.parametrize(
    ("leading", "explicit"),
    [
        ('"./a photo.png" compare', '"b photo.ico"'),
        (r"./a\ photo.png compare", r"b\ photo.ico"),
        ("./a photo.png compare", "b photo.ico"),
    ],
)
def test_query_collects_inline_and_explicit_relative_images(tmp_path, monkeypatch, leading, explicit):
    first = tmp_path / "a photo.png"
    second = tmp_path / "b photo.ico"
    first.touch()
    second.touch()
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    assert query._collect_query_images(leading, explicit) == ("compare", [first, second])


def test_oneshot_cannot_execute_dangerous_terminal_command(tmp_path, monkeypatch, capsys):
    import json
    import shlex
    from tools.terminal_tool import cleanup_vm, terminal_tool

    victim = tmp_path / "keep"
    victim.mkdir()
    (victim / "marker.txt").touch()
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "_get_approval_config", lambda: {"mode": "manual"})
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": []},
    )

    def try_delete(_prompt, **_kwargs):
        result = json.loads(terminal_tool(
            "rm -rf " + shlex.quote(str(victim)), task_id="cli-headless-approval-test",
        ))
        assert result["status"] == "blocked"
        assert "cannot prompt" in result["error"]
        return "action blocked"

    monkeypatch.setattr(oneshot, "_run_agent", try_delete)

    try:
        assert oneshot.run_oneshot("test") == 0
        assert (victim / "marker.txt").exists()
        assert capsys.readouterr().out == "action blocked\n"
    finally:
        cleanup_vm("cli-headless-approval-test")


def test_main_applies_oneshot_flags_before_startup(monkeypatch):
    from marlow_cli import main as cli

    monkeypatch.delenv("MARLOW_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.delenv("MARLOW_IGNORE_RULES", raising=False)
    monkeypatch.setattr(
        sys, "argv",
        ["marlow", "-z", "test", "--ignore-user-config", "--ignore-rules", "--yolo"],
    )
    seen = []

    def discover(_args):
        seen.append((os.environ.get("MARLOW_IGNORE_USER_CONFIG"), os.environ.get("MARLOW_IGNORE_RULES")))

    run = MagicMock(return_value=0)
    monkeypatch.setattr(cli, "_prepare_agent_startup", discover)
    monkeypatch.setattr(oneshot, "run_oneshot", run)

    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert seen == [("1", "1")]
    assert run.call_args.kwargs["yolo"] is True
    assert "MARLOW_IGNORE_USER_CONFIG" not in os.environ
    assert "MARLOW_IGNORE_RULES" not in os.environ


def test_ignore_user_config_skips_cached_approval_settings(tmp_path, monkeypatch):
    from marlow_cli.config import load_config, load_config_readonly, read_raw_config

    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    monkeypatch.delenv("_MARLOW_GATEWAY", raising=False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": []},
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text('approvals: {mode: "off"}\nhooks_auto_accept: true\n', encoding="utf-8")
    assert load_config()["approvals"]["mode"] == "off"

    with query._invocation_environment({"MARLOW_IGNORE_USER_CONFIG": "1"}):
        assert read_raw_config() == {}
        assert load_config_readonly()["approvals"]["mode"] == "manual"
        assert not load_config()["hooks_auto_accept"]
        with query.local_query_approvals(can_prompt=False):
            blocked = approval.check_all_command_guards("rm -rf /tmp/test-victim", "local")
            assert not blocked["approved"]
            assert blocked["outcome"] == "blocked"

    assert load_config()["approvals"]["mode"] == "off"


@pytest.mark.parametrize(("allowlisted", "accept_flag"), [(False, False), (True, False), (False, True)])
def test_oneshot_hook_startup_never_prompts_on_a_tty(monkeypatch, capsys, allowlisted, accept_flag):
    from types import SimpleNamespace
    from agent import shell_hooks
    from marlow_cli import main as cli, plugins

    argv = ["marlow", "-z", "test"]
    if accept_flag:
        argv.append("--accept-hooks")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.delenv("MARLOW_ACCEPT_HOOKS", raising=False)
    prompted = MagicMock(return_value="y")
    monkeypatch.setattr("builtins.input", prompted)
    monkeypatch.setattr(shell_hooks, "_registered", set())
    monkeypatch.setattr(shell_hooks, "_is_allowlisted", lambda *_args: allowlisted)
    record_approval = MagicMock()
    monkeypatch.setattr(shell_hooks, "_record_approval", record_approval)
    manager = SimpleNamespace(_hooks={})
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "discover_plugins", lambda: print("plugin startup"))
    monkeypatch.setattr("marlow_cli.config.load_config", lambda: {
        "hooks": {"pre_tool_call": [{"command": "printf test"}]},
    })
    monkeypatch.setattr(oneshot, "_run_agent", lambda *_a, **_kw: "answer")

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 0
    prompted.assert_not_called()
    assert bool(manager._hooks) == (allowlisted or accept_flag)
    assert record_approval.call_count == int(accept_flag)
    captured = capsys.readouterr()
    assert captured.out == "answer\n"
    assert "Allow this hook" not in captured.err

"""Sole shell/file execution boundary: Marlow policy -> app-server command/exec.

The model's Codex environment tools are disabled. This module is deterministic
execution integration, not an agent/tool-reasoning loop or a second SDK.
"""

from __future__ import annotations
import json
from pathlib import Path
import sys


def allowed_path(agent, raw, *, write=False):
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(agent.session_cwd) / path
    path = path.resolve()
    if write:
        from agent.file_safety import is_write_denied

        if is_write_denied(str(path)):
            raise PermissionError("Write is forbidden by Marlow file safety policy")
    grants = [(str(agent.environment.workspace), True), *agent.environment.projects]
    for root, writable in grants:
        if path == Path(root) or path.is_relative_to(Path(root)):
            if not write or writable:
                return path
    raise PermissionError("Path is outside the authorized workspace/project grants")


def approve_operation(agent, name, args, *, always=False):
    from tools.approval import (
        is_admin_action_approval_enabled,
        request_action_intent_approval,
    )
    from tools.action_intent import build_action_intent

    if not always and not is_admin_action_approval_enabled():
        return
    intent = build_action_intent(
        tool_name=name,
        args=args,
        builder={
            "action_type": "runtime.execution",
            "operation": name,
            "target": args.get("path") or args.get("workdir") or agent.session_cwd,
            "reason": args.get("purpose")
            or "Execute this exact operation in the authorized workspace",
            "impact": "May change files or run a command within explicit runtime grants",
        },
    )
    from gateway.turn_context import get_current_turn

    if get_current_turn() is not None:
        decision = request_action_intent_approval(intent)
        if decision.get("approved") is not True:
            raise PermissionError(decision.get("message") or "Approval denied")
    else:
        from tools.terminal_tool import _get_approval_callback

        callback = _get_approval_callback()
        if (
            callback is None
            or callback(intent.summary(), intent.reason, allow_permanent=False)
            != "once"
        ):
            raise PermissionError(
                "This operation requires a single approval; no interactive approval is available"
            )
    # Approval waiting may outlive cancellation. Recheck before dispatch.
    if agent._interrupt_requested or agent._closed:
        raise PermissionError("Operation was cancelled while awaiting approval")


def execute(agent, name, args):
    if name == "terminal":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("A command is required")
        if args.get("background"):
            raise ValueError(
                "Use a durable Cron job or delegated task for background execution"
            )
        cwd = allowed_path(agent, args.get("workdir") or agent.session_cwd)
        from tools.approval import (
            detect_hardline_command,
            detect_dangerous_command,
            _check_sudo_stdin_guard,
        )

        hardline, reason = detect_hardline_command(command)
        sudo, sudo_reason = _check_sudo_stdin_guard(command)
        if hardline or sudo:
            raise PermissionError(reason or sudo_reason)
        dangerous, _, _ = detect_dangerous_command(command)
        from tools.tirith_security import check_command_security

        security = check_command_security(command)
        terminal_config = agent.config.get("terminal") or {}
        requires_review = (
            dangerous
            or security.get("action") != "allow"
            or terminal_config.get("security_mode") == "approval-required"
        )
        approve_operation(
            agent, name, dict(args, workdir=str(cwd)), always=requires_review
        )
        agent._dispatched_write = True
        return agent._codex_session.execute_command(
            ["/bin/sh", "-c", command],
            cwd=str(cwd),
            timeout=min(max(float(args.get("timeout") or 60), 1), agent.turn_timeout),
        )
    write = name in {"write_file", "patch"}
    if args.get("cross_profile"):
        raise PermissionError(
            "Cross-profile file grants are not available through the runtime"
        )
    args = dict(args)
    if name == "patch" and args.get("mode") == "patch":
        from tools.patch_parser import parse_v4a_patch

        operations, error = parse_v4a_patch(args.get("patch", ""))
        if error or not operations:
            raise ValueError(error or "Empty patch")
        targets = {
            raw for op in operations for raw in (op.file_path, op.new_path) if raw
        }
        args["_authorized_patch_paths"] = {
            raw: str(allowed_path(agent, raw, write=True)) for raw in targets
        }
        path = Path(next(iter(args["_authorized_patch_paths"].values())))
    else:
        path = allowed_path(agent, args.get("path") or ".", write=write)
    if write:
        approve_operation(agent, name, dict(args, path=str(path)))
    if args.get("_authorized_patch_paths"):
        root = Path(__file__).parents[1] / "tools"
        args["_parser_sources"] = {
            "tools." + module: (root / (module + ".py")).read_text(encoding="utf-8")
            for module in ("fuzzy_match", "patch_parser")
        }
    program = (
        Path(__file__).with_name("codex_file_worker.py").read_text(encoding="utf-8")
    )
    argv = [sys.executable, "-I", "-c", program, name, str(path), json.dumps(args)]
    prior_dispatch = agent._dispatched_write
    if write:
        agent._dispatched_write = True
    result = agent._codex_session.execute_command(
        argv,
        cwd=agent.session_cwd,
        timeout=min(60, agent.turn_timeout),
        output_limit=8 * 1024 * 1024 if name == "vision_analyze" else 128 * 1024,
    )
    if result.get("exitCode") != 0:
        value = {
            "success": False,
            "error": result.get("stderr")
            or result.get("stdout")
            or "Sandboxed file operation failed",
        }
    else:
        value = json.loads(result.get("stdout", "{}"))
        if write and value.pop("write_state", None) == "not_started":
            agent._dispatched_write = prior_dispatch
    if write:
        targets = args.get("_authorized_patch_paths", {}).values() or [str(path)]
        for target in targets:
            if value.get("success"):
                agent._failed_mutations.pop(target, None)
            else:
                agent._failed_mutations[target] = str(
                    value.get("error") or "File mutation failed"
                )
    if name == "vision_analyze" and value.get("success"):
        return value["result"]
    return value

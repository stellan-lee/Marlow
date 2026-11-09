"""Cron business authorization captured from the host, separate from delivery."""

from contextvars import ContextVar
from pathlib import Path
import hashlib
import json

CURRENT = ContextVar("marlow_cron_execution_scope", default=None)


def visible(job):
    scope = CURRENT.get()
    return (
        scope is None
        or scope.get("operator") is True
        or (job.get("execution_scope") or {}).get("owner") == scope.get("owner")
    )


def dispatch_cron(agent, args):
    from agent.codex_execution import allowed_path, approve_operation

    if args.get("profile") or args.get("script") or args.get("no_agent"):
        raise PermissionError(
            "Cross-profile and direct script jobs require explicit operator configuration"
        )
    if args.get("workdir"):
        allowed_path(agent, args["workdir"])
    if args.get("deliver") not in (None, "", "origin", "local"):
        approve_operation(agent, "cronjob", args, always=True)
    owner = (
        agent._cron_execution_owner
        or hashlib.sha256(
            json.dumps(
                [
                    str(agent.profile),
                    agent.platform,
                    agent._user_id,
                    agent._chat_id,
                    agent._thread_id,
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )
    scope = {
        "owner": owner,
        "operator": agent.platform in {"cli", "sdk"} and agent._user_id is None,
        "user_id": agent._user_id,
        "chat_id": agent._chat_id,
        "thread_id": agent._thread_id,
        "projects": [
            {"path": path, "writable": writable}
            for path, writable in agent.environment.projects
        ],
        "workspace": str(agent.environment.workspace),
        "project_path": agent.session_cwd,
    }
    token = CURRENT.set(scope)
    try:
        from model_tools import handle_function_call

        return handle_function_call(
            "cronjob",
            args,
            task_id=agent._current_task_id,
            enabled_tools=agent.valid_tool_names,
        )
    finally:
        CURRENT.reset(token)

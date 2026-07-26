"""Shared extraction for human-readable terminal approval cards."""

from typing import Any, Optional


def terminal_intent_view(
    action_intent: Any,
    *,
    description: str = "",
) -> Optional[dict[str, Any]]:
    """Return validated terminal intent fields for platform renderers."""
    if not isinstance(action_intent, dict):
        return None
    if action_intent.get("action_type") != "terminal.execute":
        return None
    operation = str(action_intent.get("operation") or "").strip()
    parameters = action_intent.get("parameters")
    if not operation or not isinstance(parameters, dict):
        return None

    raw_plan = parameters.get("command_plan")
    if isinstance(raw_plan, list) and raw_plan:
        commands = []
        for item in raw_plan:
            if not isinstance(item, dict):
                return None
            command = item.get("command")
            if not isinstance(command, str) or not command:
                return None
            commands.append({
                "command": command,
                "workdir": str(item.get("workdir") or "").strip(),
            })
    else:
        command = parameters.get("command")
        if not isinstance(command, str) or not command:
            return None
        commands = [{
            "command": command,
            "workdir": str(parameters.get("workdir") or "").strip(),
        }]

    origin = next(
        (
            line.removeprefix("Origin:").strip()
            for line in description.splitlines()
            if line.startswith("Origin:")
        ),
        "",
    )
    return {
        "operation": operation,
        "target": str(action_intent.get("target") or "").strip(),
        "reason": str(action_intent.get("reason") or "").strip(),
        "impact": str(action_intent.get("impact") or "").strip(),
        "environment": str(parameters.get("environment") or "").strip(),
        "origin": origin,
        "commands": commands,
        "is_plan": isinstance(raw_plan, list) and bool(raw_plan),
    }

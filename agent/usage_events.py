"""Best-effort, metadata-only usage telemetry helpers.

The session database owns persistence.  This module owns classification so
tool execution uses a consistent vocabulary.
Telemetry must never block the user's task and must never receive prompts,
arguments, or results.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _session_db(agent: Any):
    return getattr(agent, "_session_db", None)


def _source_for_agent(agent: Any) -> str:
    platform = str(getattr(agent, "platform", "") or "").strip().lower()
    return "curator" if platform == "curator" else "user_task"


def _tool_shape(tool_name: str, args: dict[str, Any]) -> tuple[str, str, str, Optional[str]]:
    """Return ``(subsystem, action, item_name, parent_name)``."""
    if tool_name == "skill_view":
        return "skill", "load", str(args.get("name") or tool_name), None
    if tool_name == "skill_manage":
        return "skill", "edit", str(args.get("name") or tool_name), None
    try:
        from tools.registry import registry

        toolset = registry.get_toolset_for_tool(tool_name) or ""
    except Exception:
        toolset = ""
    if toolset.startswith("mcp-"):
        return "mcp", "call", tool_name, toolset[4:] or "unknown"
    return "tool", "call", tool_name, None


def record_tool_usage_event(
    agent: Any,
    *,
    tool_name: str,
    args: Optional[dict[str, Any]],
    tool_call_id: Optional[str],
    failed: bool,
    duration_seconds: float,
) -> None:
    """Record one completed tool attempt without retaining its payload."""
    db = _session_db(agent)
    if db is None or not tool_name:
        return
    try:
        subsystem, action, item_name, parent_name = _tool_shape(
            tool_name, args or {}
        )
        session_id = str(getattr(agent, "session_id", "") or "") or None
        event_key = None
        if session_id and tool_call_id:
            event_key = f"tool:{session_id}:{tool_call_id}"
        db.record_usage_event(
            subsystem=subsystem,
            action=action,
            session_id=session_id,
            source=_source_for_agent(agent),
            item_name=item_name,
            parent_name=parent_name,
            success=not failed,
            duration_ms=max(0, int(float(duration_seconds or 0.0) * 1000)),
            event_key=event_key,
        )
    except Exception as exc:
        logger.debug("tool usage telemetry failed: %s", exc, exc_info=True)


def record_event(agent: Any, **event: Any) -> None:
    """Record an arbitrary pre-classified event, fail-open."""
    db = _session_db(agent)
    if db is None:
        return
    try:
        event.setdefault("session_id", str(getattr(agent, "session_id", "") or "") or None)
        event.setdefault("source", _source_for_agent(agent))
        db.record_usage_event(**event)
    except Exception as exc:
        logger.debug("usage telemetry failed: %s", exc, exc_info=True)


__all__ = ["record_event", "record_tool_usage_event"]

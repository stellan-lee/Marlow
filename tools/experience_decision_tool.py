"""Decision Memory governance tool for agent-authored proposals and approvals.

The model may propose or inspect Decisions, but it cannot grant authority. Host
runtime code derives ``DecisionTurnAuthority`` from the authenticated current
user turn and passes it through a hidden runtime keyword.
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

from tools.registry import registry, tool_error


_ACTION_SCHEMA = {
    "type": "string",
    "enum": [
        "propose",
        "remember",
        "approve",
        "supersede",
        "revoke",
        "search",
        "show",
        "related",
    ],
}


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False)


def _now() -> float:
    return time.time()


def _authority_from_kwargs(kw: Mapping[str, Any]) -> Any | None:
    authority = kw.get("authority")
    if authority is None:
        return None
    from agent.experience.authority import DecisionTurnAuthority

    if isinstance(authority, DecisionTurnAuthority):
        return authority
    raise TypeError("authority must be a trusted DecisionTurnAuthority")


def _state_db_path(kw: Mapping[str, Any]) -> str:
    from marlow_constants import get_marlow_home

    return str((get_marlow_home() / "state.db").resolve())


def _open_store(kw: Mapping[str, Any]) -> Any:
    from agent.experience.store import ExperienceStore

    return ExperienceStore(_state_db_path(kw))


def _scope_from_runtime(runtime: Any) -> tuple[str, str, str | None, str | None]:
    scope = getattr(runtime, "scope", None)
    if scope is not None:
        scope_type = getattr(scope, "scope_type", None)
        return (
            getattr(scope_type, "value", str(scope_type)),
            str(getattr(scope, "scope_id")),
            getattr(scope, "repository_id", None),
            getattr(scope, "project_id", None),
        )
    return "profile", "local-owner", None, None


def _in_current_scope(item: Mapping[str, Any], scope_type: str, scope_id: str, repository_id: str | None, project_id: str | None) -> bool:
    return (
        item.get("principal_id") == "local-owner"
        and item.get("scope_type") == scope_type
        and item.get("scope_id") == scope_id
        and item.get("repository_id") == repository_id
        and item.get("project_id") == project_id
    )


def _require_scoped_decision(store: Any, item_id: str, scope_type: str, scope_id: str, repository_id: str | None, project_id: str | None) -> Mapping[str, Any]:
    item = store.get_decision(item_id, include_history=True)
    if item is None or not _in_current_scope(item, scope_type, scope_id, repository_id, project_id):
        raise LookupError("decision is not available in this runtime scope")
    return item


def _decision_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    revision = item.get("revision") or {}
    body = revision.get("body") or {}
    return {
        "id": item.get("id"),
        "family_id": item.get("family_id"),
        "status": item.get("current_status"),
        "revision": revision.get("revision"),
        "title": revision.get("title"),
        "summary": revision.get("summary"),
        "statement": body.get("statement"),
        "rationale": body.get("rationale"),
        "authority": body.get("authority"),
        "source_type": body.get("source_type"),
        "scope_type": item.get("scope_type"),
        "scope_id": item.get("scope_id"),
        "repository_id": item.get("repository_id"),
        "project_id": item.get("project_id"),
        "sensitivity": item.get("sensitivity"),
        "egress_policy": item.get("egress_policy"),
    }


def _action_payload(action: str, item: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    payload = {"status": "ok", "action": action, "decision": _decision_payload(item)}
    payload.update(extra)
    return payload


def experience_decision_tool(
    args: Mapping[str, Any] | None = None,
    **kw: Any,
) -> str:
    """Govern canonical Work Experience Decisions.

    ``authority`` is intentionally not a tool argument. It is supplied by the
    host/runtime only when the current authenticated user turn grants it.
    """

    args = args or {}
    runtime = kw.get("runtime")
    if runtime is None:
        return tool_error("experience_decision requires trusted runtime scope")
    authority = getattr(runtime, "authority", None)
    if authority is None:
        return tool_error("experience_decision requires trusted runtime authority")
    scope_type, scope_id, repository_id, project_id = _scope_from_runtime(runtime)
    repository_root = getattr(runtime, "repository_root", None)
    action = str(args.get("action", "") or "").strip()
    if action not in _ACTION_SCHEMA["enum"]:
        return tool_error("unsupported experience_decision action")

    try:
        with _open_store(kw) as store:
            if action == "propose":
                statement = args.get("statement")
                rationale = args.get("rationale")
                if not statement or not rationale:
                    return tool_error("propose requires statement and rationale")
                body = {
                    "statement": statement,
                    "rationale": rationale,
                    "source_type": "agent_proposal",
                    "authority": "unapproved",
                    "effective_at": _now(),
                    "expires_at": args.get("expires_at"),
                }
                item = store.create_decision(
                    principal_id="local-owner",
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repository_id=repository_id,
                    project_id=project_id,
                    title=args.get("title") or "Agent Decision Proposal",
                    summary=args.get("summary") or "",
                    body=body,
                    tags=args.get("tags"),
                    created_by="agent",
                    source_session_id=getattr(runtime, "source_session_id", None) or kw.get("session_id"),
                    source_turn_id=getattr(runtime, "source_turn_id", None) or kw.get("task_id"),
                    source_hash=None,
                    producer={"proposal_origin": "agent"},
                    review_after=args.get("review_after"),
                )
                return _json(_action_payload("propose", item, active=False))

            if action == "remember":
                statement = args.get("statement")
                rationale = args.get("rationale")
                if not statement or not rationale:
                    return tool_error("remember requires statement and rationale")
                explicit = bool(getattr(authority, "explicit_remember_grant", False))
                body = {
                    "statement": statement,
                    "rationale": rationale,
                    "source_type": "user_turn" if explicit else "agent_proposal",
                    "authority": "unapproved",
                    "effective_at": _now(),
                    "expires_at": args.get("expires_at"),
                }
                item = store.create_decision(
                    principal_id="local-owner",
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repository_id=repository_id,
                    project_id=project_id,
                    title=args.get("title") or "Remembered Decision",
                    summary=args.get("summary") or "",
                    body=body,
                    tags=args.get("tags"),
                    created_by="user" if explicit else "agent",
                    source_session_id=getattr(runtime, "source_session_id", None) or kw.get("session_id"),
                    source_turn_id=getattr(runtime, "source_turn_id", None) or kw.get("task_id"),
                    source_hash=(
                        getattr(authority, "raw_user_text_hash", None)
                        if explicit
                        else None
                    ),
                    review_after=args.get("review_after"),
                )
                if explicit:
                    item = store.activate_decision(
                        item["id"],
                        authority=authority,
                        repository_root=repository_root,
                        repository_id=repository_id,
                    )
                return _json(_action_payload("remember", item, active=item.get("current_status") == "active"))

            if action == "approve":
                item_id = args.get("decision_id")
                if not item_id:
                    return tool_error("approve requires decision_id")
                if not authority or not authority.approves(item_id):
                    return tool_error("approval requires trusted current-turn authority for decision_id")
                _require_scoped_decision(store, item_id, scope_type, scope_id, repository_id, project_id)
                item = store.activate_decision(
                    item_id,
                    authority=authority,
                    repository_root=repository_root,
                    repository_id=repository_id,
                    reason=args.get("reason"),
                )
                return _json(_action_payload("approve", item, active=True))

            if action == "supersede":
                statement = args.get("statement")
                rationale = args.get("rationale")
                replaces = args.get("replaces")
                if not statement or not rationale or not replaces:
                    return tool_error("supersede requires statement, rationale, and replaces")
                if not authority or not authority.supersedes(replaces):
                    return tool_error("supersession requires trusted current-turn authority for replaces")
                body = {
                    "statement": statement,
                    "rationale": rationale,
                    "source_type": "user_turn",
                    "authority": "user",
                    "effective_at": _now(),
                    "expires_at": args.get("expires_at"),
                }
                item = store.supersede_decision(
                    replaces,
                    authority=authority,
                    principal_id="local-owner",
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repository_id=repository_id,
                    project_id=project_id,
                    title=args.get("title") or "Superseding Decision",
                    summary=args.get("summary") or "",
                    body=body,
                    tags=args.get("tags"),
                    repository_root=repository_root,
                    reason=args.get("reason"),
                )
                return _json(_action_payload("supersede", item, supersedes=replaces))

            if action == "revoke":
                item_id = args.get("decision_id")
                if not item_id:
                    return tool_error("revoke requires decision_id")
                if not authority or not authority.revokes(item_id):
                    return tool_error("revocation requires trusted current-turn authority for decision_id")
                _require_scoped_decision(store, item_id, scope_type, scope_id, repository_id, project_id)
                item = store.revoke_decision(
                    item_id,
                    authority=authority,
                    reason=args.get("reason"),
                )
                return _json(_action_payload("revoke", item, revoked=True))

            if action == "show":
                item_id = args.get("decision_id")
                if not item_id:
                    return tool_error("show requires decision_id")
                item = _require_scoped_decision(store, item_id, scope_type, scope_id, repository_id, project_id)
                return _json({"status": "ok", "action": "show", "decision": _decision_payload(item)})

            if action == "search":
                query = (args.get("title") or args.get("statement") or args.get("summary") or "").strip()
                rows = store.search_decisions(
                    principal_id="local-owner",
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repository_id=repository_id,
                    project_id=project_id,
                    provider_trust_domain=getattr(runtime, "provider_trust_domain", None) or "local-runtime",
                    provider_is_local=bool(getattr(runtime, "provider_is_local", True)),
                    query=query,
                    tags=args.get("tags") or {},
                    limit=10,
                    repository_root=repository_root,
                )
                return _json({
                    "status": "ok",
                    "action": "search",
                    "count": len(rows),
                    "decisions": [_decision_payload(row) for row in rows],
                })

            if action == "related":
                item_id = args.get("decision_id")
                if not item_id:
                    return tool_error("related requires decision_id")
                item = _require_scoped_decision(store, item_id, scope_type, scope_id, repository_id, project_id)
                links = store.list_links(item_id) if hasattr(store, "list_links") else []
                related = store.related_decisions(item_id=item_id) if hasattr(store, "related_decisions") else []
                return _json({
                    "status": "ok",
                    "action": "related",
                    "decision": _decision_payload(item),
                    "links": links,
                    "related": related,
                })
    except Exception as exc:
        return tool_error(f"experience_decision failed: {type(exc).__name__}: {exc}")
    return tool_error("unreachable experience_decision action")


def check_experience_decision_requirements() -> bool:
    return True


EXPERIENCE_DECISION_SCHEMA = {
    "name": "experience_decision",
    "description": (
        "Govern canonical Work Experience Decisions. Use 'propose' for agent "
        "suggestions, 'remember' for direct user remember language, and "
        "approve/supersede/revoke only when the current user turn explicitly "
        "names the target decision. The tool ignores any model-supplied "
        "authority; host/runtime must provide trusted authority."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": _ACTION_SCHEMA,
            "statement": {"type": "string"},
            "rationale": {"type": "string"},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "decision_id": {"type": "string"},
            "replaces": {"type": "string"},
            "reason": {"type": "string"},
            "tags": {
                "type": "object",
                "additionalProperties": {"type": "array", "items": {"type": "string"}},
            },
            "expires_at": {"type": "number"},
            "review_after": {"type": "number"},
        },
        "required": ["action"],
    },
}


registry.register(
    name="experience_decision",
    toolset="experience_decision",
    schema=EXPERIENCE_DECISION_SCHEMA,
    handler=lambda args, **kw: experience_decision_tool(args, **kw),
    check_fn=check_experience_decision_requirements,
    emoji="🧭",
)

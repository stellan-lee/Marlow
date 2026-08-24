"""Project-scoped Work Experience tools for the public MCP server.

The MCP client is an external management boundary whose downstream model
provider cannot be attested.  Management operations are therefore fixed to
the server process's current project, preserve honest audit provenance, and
return lesson text only when normal Work Experience injection and egress
policy authorize disclosure to an unknown remote provider.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional


logger = logging.getLogger("marlow.mcp_serve.experience")

_MCP_TRUST_DOMAIN = "provider:mcp-client"
_LESSON_STATUSES = (
    "candidate",
    "active",
    "disputed",
    "deprecated",
    "rejected",
    "retracted",
)
_DECISION_STATUSES = (
    "candidate",
    "active",
    "review_required",
    "superseded",
    "revoked",
)


def _get_state_db_path() -> Path:
    from marlow_constants import get_marlow_home

    return get_marlow_home() / "state.db"


def _get_project_root() -> Path:
    """Return the project boundary selected when the MCP server was launched."""

    return Path.cwd().resolve()


def _global_mode() -> str:
    from marlow_cli.config import load_config

    config = load_config()
    experience = config.get("experience", {}) if isinstance(config, dict) else {}
    return str(experience.get("mode", "off")) if isinstance(experience, dict) else "off"


@contextmanager
def _open_current_scope() -> Iterator[tuple[Any, Any]]:
    """Open the current schema and resolve exactly one configured project."""

    from agent.experience.scope import ScopeResolver
    from agent.experience.service import ExperienceService
    from agent.experience.store import ExperienceStore

    db_path = _get_state_db_path()
    resolver = ScopeResolver(str(db_path.parent))
    # Management must not create or migrate a profile merely because an MCP
    # client probed for tools. The owner initializes policy through the CLI.
    with ExperienceStore.open_current(db_path) as store:
        resolved = ExperienceService(
            store,
            scope_resolver=resolver,
        ).resolve_scope(str(_get_project_root()))
        yield store, resolved


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _tags(
    *,
    task_types: Optional[List[str]] = None,
    technologies: Optional[List[str]] = None,
    entities: Optional[List[str]] = None,
    failure_fingerprints: Optional[List[str]] = None,
) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for namespace, values in (
        ("task_type", task_types),
        ("technology", technologies),
        ("entity", entities),
        ("failure", failure_fingerprints),
    ):
        result.extend((namespace, value) for value in (values or ()))
    return tuple(result)


def _item_in_scope(item: dict[str, Any], resolved: Any) -> bool:
    return (
        item.get("principal_id") == resolved.principal_id
        and item.get("scope_type") == resolved.scope_type.value
        and item.get("repository_id") == resolved.repository_id
        and item.get("project_id") == resolved.project_id
    )


def _require_scoped_item(
    store: Any,
    resolved: Any,
    lesson_id: str,
) -> dict[str, Any]:
    item = store.get_item(lesson_id)
    if item is None or not _item_in_scope(item, resolved):
        raise LookupError("lesson is not available in this MCP project")
    return item


def _content_allowed(
    item: dict[str, Any],
    policy: Any,
    *,
    global_mode: str | None = None,
) -> bool:
    if (
        (global_mode if global_mode is not None else _global_mode()) != "assist"
        or not bool(policy.recall_allowed)
        or not bool(policy.injection_allowed)
    ):
        return False
    from agent.experience.safety import is_egress_allowed

    return is_egress_allowed(
        sensitivity=item.get("sensitivity"),
        egress_policy=item.get("egress_policy"),
        producer_trust_domain=item.get("producer_trust_domain"),
        current_trust_domain=_MCP_TRUST_DOMAIN,
        current_provider_is_local=False,
        max_egress_policy=policy.max_egress_policy,
    )


def _revision_payload(revision: dict[str, Any]) -> dict[str, Any]:
    return {
        key: revision.get(key)
        for key in (
            "revision",
            "title",
            "summary",
            "body",
            "tags",
            "confidence",
            "editor",
            "edit_reason",
            "created_at",
            "last_validated_at",
            "review_after",
        )
    }


def _item_payload(
    item: dict[str, Any],
    policy: Any,
    *,
    include_content: bool,
    global_mode: str | None = None,
) -> dict[str, Any]:
    revision = item.get("revision") or {}
    body = revision.get("body") or {}
    allowed = _content_allowed(item, policy, global_mode=global_mode)
    payload = {
        "id": item.get("id"),
        "kind": item.get("kind"),
        "status": item.get("current_status"),
        "current_revision": item.get("current_revision"),
        "scope_type": item.get("scope_type"),
        "sensitivity": item.get("sensitivity"),
        "egress_policy": item.get("egress_policy"),
        "created_by": item.get("created_by"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "content_available": allowed,
    }
    if allowed and include_content:
        payload["revision"] = _revision_payload(revision)
    elif allowed:
        payload["title"] = revision.get("title")
    return payload


def _require_scoped_decision(
    store: Any,
    resolved: Any,
    decision_id: str,
    *,
    include_history: bool = False,
) -> dict[str, Any]:
    item = store.get_decision(decision_id, include_history=include_history)
    if item is None or not _item_in_scope(item, resolved):
        raise LookupError("decision is not available in this MCP project")
    return item


def _decision_revision_payload(revision: dict[str, Any]) -> dict[str, Any]:
    return {
        key: revision.get(key)
        for key in (
            "revision",
            "title",
            "summary",
            "body",
            "tags",
            "confidence",
            "editor",
            "edit_reason",
            "created_at",
            "last_validated_at",
            "review_after",
        )
    }


def _decision_payload(
    item: dict[str, Any],
    policy: Any,
    *,
    include_content: bool,
    global_mode: str | None = None,
) -> dict[str, Any]:
    revision = item.get("revision") or {}
    body = revision.get("body") or {}
    allowed = _content_allowed(item, policy, global_mode=global_mode)
    payload = {
        "id": item.get("id"),
        "family_id": item.get("family_id"),
        "kind": item.get("kind"),
        "status": item.get("current_status"),
        "current_revision": item.get("current_revision"),
        "scope_type": item.get("scope_type"),
        "scope_id": item.get("scope_id"),
        "repository_id": item.get("repository_id"),
        "project_id": item.get("project_id"),
        "sensitivity": item.get("sensitivity"),
        "egress_policy": item.get("egress_policy"),
        "created_by": item.get("created_by"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "authority": body.get("authority"),
        "source_type": body.get("source_type"),
        "effective_at": body.get("effective_at"),
        "expires_at": body.get("expires_at"),
        "content_available": allowed,
    }
    if allowed and include_content:
        payload["revision"] = _decision_revision_payload(revision)
        payload["revisions"] = [
            _decision_revision_payload(rev)
            for rev in item.get("revisions") or []
        ]
    elif allowed:
        payload["title"] = revision.get("title")
        payload["summary"] = revision.get("summary")
        payload["statement"] = body.get("statement")
    return payload


def _mcp_decision_authority(
    *,
    reason: str,
    approve: str | None = None,
    supersede: str | None = None,
    revoke: str | None = None,
) -> Any:
    import hashlib
    from agent.experience.authority import DecisionTurnAuthority

    approval_text = reason.strip() or "mcp decision approval"
    return DecisionTurnAuthority(
        source_turn_id=f"mcp_approval_{uuid.uuid4().hex}",
        source_session_id="mcp-client",
        raw_user_text_hash=hashlib.sha256(approval_text.encode("utf-8")).hexdigest(),
        explicit_remember_grant=False,
        approved_item_ids=(approve,) if approve is not None else (),
        supersede_target_ids=(supersede,) if supersede is not None else (),
        revoke_target_ids=(revoke,) if revoke is not None else (),
    )


def _response(operation: str, callback: Callable[[], dict[str, Any]]) -> str:
    try:
        return json.dumps(callback(), ensure_ascii=False)
    except Exception as exc:
        # Store and scope errors can contain paths or user-authored lesson
        # content. Log only the exception type and return a bounded response.
        logger.debug(
            "Work Experience MCP %s failed (%s)",
            operation,
            exc.__class__.__name__,
        )
        return json.dumps({
            "status": "error",
            "operation": operation,
            "error": (
                "The Work Experience operation was rejected or is not "
                "available in this MCP server project."
            ),
        })


def recall_work_experience(
    query: str,
    *,
    task_types: Optional[List[str]] = None,
    technologies: Optional[List[str]] = None,
    entities: Optional[List[str]] = None,
    failure_fingerprints: Optional[List[str]] = None,
    limit: int = 3,
) -> str:
    if not isinstance(query, str) or not query.strip():
        return json.dumps({
            "status": "invalid_request",
            "count": 0,
            "context": "",
            "reason": "A non-empty current-task query is required.",
        })
    if _global_mode() != "assist":
        return json.dumps({
            "status": "unavailable",
            "count": 0,
            "context": "",
            "reason": "Work Experience assist mode is not enabled.",
        })

    def run() -> dict[str, Any]:
        from agent.experience.models import RetrievalQuery
        from agent.experience.service import ExperienceService

        bounded_limit = _bounded_int(limit, default=3, minimum=1, maximum=3)
        with _open_current_scope() as (store, resolved):
            service = ExperienceService(
                store,
                max_retrieved_items=bounded_limit,
            )
            retrieval_query = RetrievalQuery(
                scope=resolved.as_ref(),
                query_text=query,
                provider_trust_domain=_MCP_TRUST_DOMAIN,
                provider_is_local=False,
                task_types=tuple(task_types or ()),
                technologies=tuple(technologies or ()),
                entities=tuple(entities or ()),
                failure_fingerprints=tuple(failure_fingerprints or ()),
                limit=bounded_limit,
            )
            request_id = uuid.uuid4().hex
            result = service.retrieve(
                retrieval_query,
                turn_id=f"turn_{request_id}",
                work_id=f"attempt_{request_id}",
            )
            context = service.format_context(
                result,
                provider_trust_domain=_MCP_TRUST_DOMAIN,
                provider_is_local=False,
            )
        return {
            "status": "ok",
            "count": context.count("[lesson "),
            "retrieval_ref": result.diagnostic.id,
            "context": context,
        }

    return _response("recall", run)


def list_work_experience(
    *,
    statuses: Optional[List[str]] = None,
    limit: int = 100,
) -> str:
    def run() -> dict[str, Any]:
        requested = tuple(statuses or ()) or None
        if requested and any(status not in _LESSON_STATUSES for status in requested):
            raise ValueError("unsupported lesson status")
        with _open_current_scope() as (store, resolved):
            global_mode = _global_mode()
            items = store.list_items(
                principal_id=resolved.principal_id,
                repository_id=resolved.repository_id,
                project_id=resolved.project_id,
                status=requested,
                limit=_bounded_int(limit, default=100, minimum=1, maximum=200),
            )
            lessons = [
                _item_payload(
                    item,
                    resolved.policy,
                    include_content=False,
                    global_mode=global_mode,
                )
                for item in items
            ]
        return {"status": "ok", "count": len(lessons), "lessons": lessons}

    return _response("list", run)


def show_work_experience(lesson_id: str) -> str:
    def run() -> dict[str, Any]:
        with _open_current_scope() as (store, resolved):
            item = _require_scoped_item(store, resolved, lesson_id)
            lesson = _item_payload(item, resolved.policy, include_content=True)
        return {"status": "ok", "lesson": lesson}

    return _response("show", run)


def add_work_experience(
    *,
    title: str,
    summary: str,
    applies_when: str,
    guidance: str,
    rationale: str,
    does_not_apply_when: Optional[str] = None,
    task_types: Optional[List[str]] = None,
    technologies: Optional[List[str]] = None,
    entities: Optional[List[str]] = None,
    failure_fingerprints: Optional[List[str]] = None,
    confidence: float = 0.6,
    sensitivity: str = "local_only",
    egress_policy: str = "local_only",
) -> str:
    def run() -> dict[str, Any]:
        from agent.experience.models import CreatedBy, LessonBody

        with _open_current_scope() as (store, resolved):
            scope = resolved.as_ref()
            item = store.create_lesson(
                principal_id=scope.principal_id,
                scope_type=scope.scope_type,
                scope_id=scope.scope_id,
                repository_id=scope.repository_id,
                project_id=scope.project_id,
                title=title,
                summary=summary,
                body=LessonBody(
                    applies_when=applies_when,
                    does_not_apply_when=does_not_apply_when,
                    guidance=guidance,
                    rationale=rationale,
                ),
                tags=_tags(
                    task_types=task_types,
                    technologies=technologies,
                    entities=entities,
                    failure_fingerprints=failure_fingerprints,
                ),
                confidence=confidence,
                sensitivity=sensitivity,
                egress_policy=egress_policy,
                created_by=CreatedBy.AGENT,
            )
            lesson = _item_payload(item, resolved.policy, include_content=True)
        return {"status": "ok", "created": True, "lesson": lesson}

    return _response("add", run)


def approve_work_experience(lesson_id: str, *, reason: str) -> str:
    def run() -> dict[str, Any]:
        if not reason or not reason.strip():
            raise ValueError("approval reason is required")
        with _open_current_scope() as (store, resolved):
            _require_scoped_item(store, resolved, lesson_id)
            item = store.approve_lesson(
                lesson_id,
                reason=reason,
                actor="mcp-client",
            )
            lesson = _item_payload(item, resolved.policy, include_content=False)
        return {"status": "ok", "approved": True, "lesson": lesson}

    return _response("approve", run)


def _updated_tags(
    revision: dict[str, Any],
    *,
    task_types: Optional[List[str]],
    technologies: Optional[List[str]],
    entities: Optional[List[str]],
    failure_fingerprints: Optional[List[str]],
) -> tuple[tuple[str, str], ...]:
    replacements = {
        "task_type": task_types,
        "technology": technologies,
        "entity": entities,
        "failure": failure_fingerprints,
    }
    current: dict[str, list[str]] = {}
    for tag in revision.get("tags") or ():
        current.setdefault(str(tag.get("namespace")), []).append(str(tag.get("value")))
    for namespace, values in replacements.items():
        if values is not None:
            current[namespace] = list(values)
    return tuple(
        (namespace, value) for namespace, values in current.items() for value in values
    )


def edit_work_experience(
    lesson_id: str,
    *,
    reason: str,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    applies_when: Optional[str] = None,
    does_not_apply_when: Optional[str] = None,
    clear_does_not_apply_when: bool = False,
    guidance: Optional[str] = None,
    rationale: Optional[str] = None,
    task_types: Optional[List[str]] = None,
    technologies: Optional[List[str]] = None,
    entities: Optional[List[str]] = None,
    failure_fingerprints: Optional[List[str]] = None,
) -> str:
    def run() -> dict[str, Any]:
        from agent.experience.models import LessonBody

        if not reason or not reason.strip():
            raise ValueError("edit reason is required")
        with _open_current_scope() as (store, resolved):
            current = _require_scoped_item(store, resolved, lesson_id)
            revision = current["revision"]
            body = revision["body"]
            updated = store.edit_lesson(
                lesson_id,
                title=revision["title"] if title is None else title,
                summary=revision["summary"] if summary is None else summary,
                body=LessonBody(
                    applies_when=(
                        body["applies_when"] if applies_when is None else applies_when
                    ),
                    does_not_apply_when=(
                        None
                        if clear_does_not_apply_when
                        else (
                            body.get("does_not_apply_when")
                            if does_not_apply_when is None
                            else does_not_apply_when
                        )
                    ),
                    guidance=body["guidance"] if guidance is None else guidance,
                    rationale=body["rationale"] if rationale is None else rationale,
                ),
                tags=_updated_tags(
                    revision,
                    task_types=task_types,
                    technologies=technologies,
                    entities=entities,
                    failure_fingerprints=failure_fingerprints,
                ),
                editor="mcp-client",
                edit_reason=reason,
            )
            lesson = _item_payload(updated, resolved.policy, include_content=True)
        return {
            "status": "ok",
            "changed": updated["current_revision"] != current["current_revision"],
            "lesson": lesson,
        }

    return _response("edit", run)


def retract_work_experience(lesson_id: str, *, reason: str) -> str:
    def run() -> dict[str, Any]:
        if not reason or not reason.strip():
            raise ValueError("retraction reason is required")
        with _open_current_scope() as (store, resolved):
            _require_scoped_item(store, resolved, lesson_id)
            item = store.retract_lesson(
                lesson_id,
                reason=reason,
                actor="mcp-client",
            )
            lesson = _item_payload(item, resolved.policy, include_content=False)
        return {"status": "ok", "retracted": True, "lesson": lesson}

    return _response("retract", run)


def list_decision_work_experience(
    *,
    statuses: Optional[List[str]] = None,
    limit: int = 100,
) -> str:
    def run() -> dict[str, Any]:
        requested = tuple(statuses or ()) or None
        if requested and any(status not in _DECISION_STATUSES for status in requested):
            raise ValueError("unsupported decision status")
        with _open_current_scope() as (store, resolved):
            global_mode = _global_mode()
            items = store.list_decisions(
                principal_id=resolved.principal_id,
                repository_id=resolved.repository_id,
                project_id=resolved.project_id,
                status=requested,
                limit=_bounded_int(limit, default=100, minimum=1, maximum=200),
            )
            decisions = [
                _decision_payload(
                    item,
                    resolved.policy,
                    include_content=False,
                    global_mode=global_mode,
                )
                for item in items
            ]
        return {"status": "ok", "count": len(decisions), "decisions": decisions}

    return _response("decision_list", run)


def show_decision_work_experience(decision_id: str) -> str:
    def run() -> dict[str, Any]:
        with _open_current_scope() as (store, resolved):
            item = _require_scoped_decision(
                store,
                resolved,
                decision_id,
                include_history=True,
            )
            decision = _decision_payload(
                item,
                resolved.policy,
                include_content=True,
            )
            decision["links"] = store.list_links(item_id=decision_id)
        return {"status": "ok", "decision": decision}

    return _response("decision_show", run)


def add_decision_work_experience(
    *,
    title: str,
    summary: str,
    statement: str,
    rationale: str,
    effective_at: float,
    expires_at: Optional[float] = None,
    task_types: Optional[List[str]] = None,
    technologies: Optional[List[str]] = None,
    entities: Optional[List[str]] = None,
    failure_fingerprints: Optional[List[str]] = None,
    sensitivity: str = "normal",
    egress_policy: str = "local_only",
) -> str:
    def run() -> dict[str, Any]:
        from agent.experience.models import DecisionBody

        with _open_current_scope() as (store, resolved):
            scope = resolved.as_ref()
            body = DecisionBody(
                statement=statement,
                rationale=rationale,
                source_type="agent_proposal",
                authority="unapproved",
                effective_at=effective_at,
                expires_at=expires_at,
            ).to_dict()
            item = store.create_decision(
                principal_id=scope.principal_id,
                scope_type=scope.scope_type,
                scope_id=scope.scope_id,
                repository_id=scope.repository_id,
                project_id=scope.project_id,
                title=title,
                summary=summary,
                body=body,
                tags=_tags(
                    task_types=task_types,
                    technologies=technologies,
                    entities=entities,
                    failure_fingerprints=failure_fingerprints,
                ),
                sensitivity=sensitivity,
                egress_policy=egress_policy,
                created_by="agent",
            )
            decision = _decision_payload(
                item,
                resolved.policy,
                include_content=True,
            )
        return {"status": "ok", "created": True, "decision": decision}

    return _response("decision_add", run)


def approve_decision_work_experience(decision_id: str, *, reason: str) -> str:
    def run() -> dict[str, Any]:
        if not reason or not reason.strip():
            raise ValueError("approval reason is required")
        with _open_current_scope() as (store, resolved):
            _require_scoped_decision(store, resolved, decision_id)
            item = store.activate_decision(
                decision_id,
                authority=_mcp_decision_authority(reason=reason, approve=decision_id),
                repository_root=str(_get_project_root()),
                repository_id=resolved.repository_id,
                actor="mcp-client",
                reason=reason,
            )
            decision = _decision_payload(
                item,
                resolved.policy,
                include_content=False,
            )
        return {"status": "ok", "approved": True, "decision": decision}

    return _response("decision_approve", run)


def supersede_decision_work_experience(
    decision_id: str,
    *,
    title: str,
    summary: str,
    statement: str,
    rationale: str,
    effective_at: float,
    reason: str,
    expires_at: Optional[float] = None,
    task_types: Optional[List[str]] = None,
    technologies: Optional[List[str]] = None,
    entities: Optional[List[str]] = None,
    failure_fingerprints: Optional[List[str]] = None,
    sensitivity: str = "normal",
    egress_policy: str = "local_only",
) -> str:
    def run() -> dict[str, Any]:
        from agent.experience.models import DecisionBody

        if not reason or not reason.strip():
            raise ValueError("supersession reason is required")
        with _open_current_scope() as (store, resolved):
            _require_scoped_decision(store, resolved, decision_id)
            scope = resolved.as_ref()
            body = DecisionBody(
                statement=statement,
                rationale=rationale,
                source_type="user_turn",
                authority="user",
                effective_at=effective_at,
                expires_at=expires_at,
            ).to_dict()
            item = store.supersede_decision(
                decision_id,
                authority=_mcp_decision_authority(reason=reason, supersede=decision_id),
                principal_id=scope.principal_id,
                scope_type=scope.scope_type,
                scope_id=scope.scope_id,
                repository_id=scope.repository_id,
                project_id=scope.project_id,
                title=title,
                summary=summary,
                body=body,
                tags=_tags(
                    task_types=task_types,
                    technologies=technologies,
                    entities=entities,
                    failure_fingerprints=failure_fingerprints,
                ),
                sensitivity=sensitivity,
                egress_policy=egress_policy,
                created_by="agent",
                repository_root=str(_get_project_root()),
                reason=reason,
            )
            decision = _decision_payload(
                item,
                resolved.policy,
                include_content=True,
            )
        return {"status": "ok", "superseded": True, "decision": decision}

    return _response("decision_supersede", run)


def revoke_decision_work_experience(decision_id: str, *, reason: str) -> str:
    def run() -> dict[str, Any]:
        if not reason or not reason.strip():
            raise ValueError("revocation reason is required")
        with _open_current_scope() as (store, resolved):
            _require_scoped_decision(store, resolved, decision_id)
            item = store.revoke_decision(
                decision_id,
                authority=_mcp_decision_authority(reason=reason, revoke=decision_id),
                actor="mcp-client",
                reason=reason,
            )
            decision = _decision_payload(
                item,
                resolved.policy,
                include_content=False,
            )
        return {"status": "ok", "revoked": True, "decision": decision}

    return _response("decision_revoke", run)


def related_decision_work_experience(decision_id: str) -> str:
    def run() -> dict[str, Any]:
        with _open_current_scope() as (store, resolved):
            item = _require_scoped_decision(
                store,
                resolved,
                decision_id,
                include_history=True,
            )
            decision = _decision_payload(
                item,
                resolved.policy,
                include_content=True,
            )
            links = store.list_links(item_id=decision_id) if hasattr(store, "list_links") else []
            related = store.related_decisions(item_id=decision_id) if hasattr(store, "related_decisions") else []
        return {
            "status": "ok",
            "decision": decision,
            "links": links,
            "related": related,
        }

    return _response("decision_related", run)


def register_work_experience_tools(mcp: Any) -> None:
    """Register the seven Work Experience MCP tools on ``mcp``."""

    from mcp.types import ToolAnnotations

    read_only = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        )
    )
    def experience_recall(
        query: str,
        task_types: Optional[List[str]] = None,
        technologies: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
        failure_fingerprints: Optional[List[str]] = None,
        limit: int = 3,
    ) -> str:
        """Recall approved lessons relevant to the current project task."""

        return recall_work_experience(
            query,
            task_types=task_types,
            technologies=technologies,
            entities=entities,
            failure_fingerprints=failure_fingerprints,
            limit=limit,
        )

    @mcp.tool(annotations=read_only)
    def experience_list(
        statuses: Optional[List[str]] = None,
        limit: int = 100,
    ) -> str:
        """List lessons in this MCP server's project.

        Content fields are included only when Work Experience policy permits
        disclosure to the external MCP boundary. Status values are candidate,
        active, disputed, deprecated, rejected, and retracted.
        """

        return list_work_experience(statuses=statuses, limit=limit)

    @mcp.tool(annotations=read_only)
    def experience_show(lesson_id: str) -> str:
        """Show one project lesson and its current revision when authorized."""

        return show_work_experience(lesson_id)

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        )
    )
    def experience_add(
        title: str,
        summary: str,
        applies_when: str,
        guidance: str,
        rationale: str,
        does_not_apply_when: Optional[str] = None,
        task_types: Optional[List[str]] = None,
        technologies: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
        failure_fingerprints: Optional[List[str]] = None,
        confidence: float = 0.6,
        sensitivity: str = "local_only",
        egress_policy: str = "local_only",
    ) -> str:
        """Create an agent-authored candidate lesson in the current project.

        The safe defaults are sensitivity=local_only and
        egress_policy=local_only. The candidate remains inactive until an
        explicit experience_approve call succeeds.
        """

        return add_work_experience(
            title=title,
            summary=summary,
            applies_when=applies_when,
            guidance=guidance,
            rationale=rationale,
            does_not_apply_when=does_not_apply_when,
            task_types=task_types,
            technologies=technologies,
            entities=entities,
            failure_fingerprints=failure_fingerprints,
            confidence=confidence,
            sensitivity=sensitivity,
            egress_policy=egress_policy,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def experience_approve(lesson_id: str, reason: str) -> str:
        """Activate a candidate lesson, recording the MCP audit actor."""

        return approve_work_experience(lesson_id, reason=reason)

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        )
    )
    def experience_edit(
        lesson_id: str,
        reason: str,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        applies_when: Optional[str] = None,
        does_not_apply_when: Optional[str] = None,
        clear_does_not_apply_when: bool = False,
        guidance: Optional[str] = None,
        rationale: Optional[str] = None,
        task_types: Optional[List[str]] = None,
        technologies: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
        failure_fingerprints: Optional[List[str]] = None,
    ) -> str:
        """Append an immutable revision to a nonterminal project lesson.

        Omitted fields retain their current values. Set
        clear_does_not_apply_when=true to remove that optional condition.
        Egress and sensitivity are intentionally not editable here.
        """

        return edit_work_experience(
            lesson_id,
            reason=reason,
            title=title,
            summary=summary,
            applies_when=applies_when,
            does_not_apply_when=does_not_apply_when,
            clear_does_not_apply_when=clear_does_not_apply_when,
            guidance=guidance,
            rationale=rationale,
            task_types=task_types,
            technologies=technologies,
            entities=entities,
            failure_fingerprints=failure_fingerprints,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def experience_retract(lesson_id: str, reason: str) -> str:
        """Retract a project lesson so it is no longer eligible for recall."""

        return retract_work_experience(lesson_id, reason=reason)

    @mcp.tool(annotations=read_only)
    def experience_decision_list(
        statuses: Optional[List[str]] = None,
        limit: int = 100,
    ) -> str:
        """List Decision Memory records in this MCP server's project."""

        return list_decision_work_experience(statuses=statuses, limit=limit)

    @mcp.tool(annotations=read_only)
    def experience_decision_show(decision_id: str) -> str:
        """Show one project Decision and its current revision when authorized."""

        return show_decision_work_experience(decision_id)

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        )
    )
    def experience_decision_add(
        title: str,
        summary: str,
        statement: str,
        rationale: str,
        effective_at: float,
        expires_at: Optional[float] = None,
        task_types: Optional[List[str]] = None,
        technologies: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
        failure_fingerprints: Optional[List[str]] = None,
        sensitivity: str = "normal",
        egress_policy: str = "local_only",
    ) -> str:
        """Create an agent-authored candidate Decision proposal."""

        return add_decision_work_experience(
            title=title,
            summary=summary,
            statement=statement,
            rationale=rationale,
            effective_at=effective_at,
            expires_at=expires_at,
            task_types=task_types,
            technologies=technologies,
            entities=entities,
            failure_fingerprints=failure_fingerprints,
            sensitivity=sensitivity,
            egress_policy=egress_policy,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def experience_decision_approve(decision_id: str, reason: str) -> str:
        """Activate a candidate Decision through this MCP management boundary."""

        return approve_decision_work_experience(decision_id, reason=reason)

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        )
    )
    def experience_decision_supersede(
        decision_id: str,
        title: str,
        summary: str,
        statement: str,
        rationale: str,
        effective_at: float,
        reason: str,
        expires_at: Optional[float] = None,
        task_types: Optional[List[str]] = None,
        technologies: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
        failure_fingerprints: Optional[List[str]] = None,
        sensitivity: str = "normal",
        egress_policy: str = "local_only",
    ) -> str:
        """Supersede a project Decision with a replacement proposal."""

        return supersede_decision_work_experience(
            decision_id,
            title=title,
            summary=summary,
            statement=statement,
            rationale=rationale,
            effective_at=effective_at,
            reason=reason,
            expires_at=expires_at,
            task_types=task_types,
            technologies=technologies,
            entities=entities,
            failure_fingerprints=failure_fingerprints,
            sensitivity=sensitivity,
            egress_policy=egress_policy,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def experience_decision_revoke(decision_id: str, reason: str) -> str:
        """Revoke a project Decision so it is no longer eligible for recall."""

        return revoke_decision_work_experience(decision_id, reason=reason)

    @mcp.tool(annotations=read_only)
    def experience_decision_related(decision_id: str) -> str:
        """Show project Decision relationship links and nearby decisions."""

        return related_decision_work_experience(decision_id)


__all__ = [
    "add_decision_work_experience",
    "add_work_experience",
    "approve_decision_work_experience",
    "approve_work_experience",
    "edit_work_experience",
    "list_decision_work_experience",
    "list_work_experience",
    "recall_work_experience",
    "register_work_experience_tools",
    "related_decision_work_experience",
    "retract_work_experience",
    "revoke_decision_work_experience",
    "show_decision_work_experience",
    "supersede_decision_work_experience",
]

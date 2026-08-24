"""Governance CLI for the Work Experience validation MVP.

This module deliberately stays outside the chat/slash-command surfaces.  It
opens the active profile's ``state.db`` lazily and delegates all persistence,
sanitization, lifecycle, and scope enforcement to ``agent.experience``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


logger = logging.getLogger(__name__)

_MODES = ("off", "capture", "shadow", "assist")
_MODE_RANK = {mode: index for index, mode in enumerate(_MODES)}
_EGRESS_POLICIES = (
    "local_only",
    "same_provider_trust_domain",
    "explicit_any_provider",
)
_SENSITIVITIES = ("normal", "private_repo", "local_only", "blocked")
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
_LATEST_RETRIEVAL_UNAVAILABLE = object()

_PURGE_DISCLOSURE = (
    "Purge permanently removes this item and its dependent rows from the "
    "active state.db on a best-effort basis. It cannot erase copies already "
    "present in database backups, WAL history, filesystem snapshots, exported "
    "files, session transcripts, or model-provider logs."
)


def _state_db_path() -> Path:
    """Resolve the active profile path at command execution time."""

    from marlow_constants import get_marlow_home

    return Path(get_marlow_home()).expanduser().resolve() / "state.db"


def _profile_namespace() -> str:
    # Scope identifiers are intentionally profile-local.  The canonical home
    # path is never persisted as lesson content or sent to a provider.
    return str(_state_db_path().parent)


@contextmanager
def _open_store() -> Iterator[Any]:
    from agent.experience.store import ExperienceStore

    with ExperienceStore(_state_db_path()) as store:
        yield store


def _scope_resolver() -> Any:
    from agent.experience.scope import ScopeResolver

    return ScopeResolver(_profile_namespace())


def _policy_mode_flags(mode: str) -> tuple[bool, bool, bool, bool]:
    """Map the user-facing mode to independent, denied-by-default grants.

    Reflection is held false for the validation MVP even in capture mode.
    Enabling automatic retrospective generation requires a later, explicit
    product gate rather than being smuggled in through this CLI.
    """

    if mode == "off":
        return False, False, False, False
    if mode == "capture":
        return True, False, False, False
    if mode == "shadow":
        return True, True, False, False
    if mode == "assist":
        return True, True, True, False
    raise ValueError(f"unsupported experience mode: {mode}")


def _stored_policy_mode(policy: Any) -> str:
    if _field(policy, "recall_allowed", False) and _field(
        policy, "injection_allowed", False
    ):
        return "assist"
    if _field(policy, "recall_allowed", False):
        return "shadow"
    if _field(policy, "capture_allowed", False):
        return "capture"
    return "off"


def _effective_mode(policy: Any) -> tuple[str, str]:
    from marlow_cli.config import load_config

    configured = load_config().get("experience", {})
    global_mode = configured.get("mode", "off") if isinstance(configured, dict) else "off"
    if global_mode not in _MODES:
        return "off", "invalid global experience.mode"
    if global_mode == "off":
        return "off", "global experience.mode is off"
    capture_allowed = bool(_field(policy, "capture_allowed", False))
    recall_allowed = bool(_field(policy, "recall_allowed", False))
    injection_allowed = bool(_field(policy, "injection_allowed", False))
    if global_mode == "capture":
        if capture_allowed:
            return "capture", "global mode and project policy permit capture"
        return "off", "project policy does not permit experience capture"
    if not recall_allowed:
        if capture_allowed:
            return "capture", "project policy permits capture but not recall"
        return "off", "project policy does not permit experience recall"
    if global_mode == "shadow":
        return "shadow", "global mode and project policy permit recall"
    if not injection_allowed:
        return "shadow", "project policy does not permit injection"
    return "assist", "global mode and project policy permit injection"


def _policies(store: Any) -> Sequence[Any]:
    from agent.experience.models import LOCAL_OWNER_PRINCIPAL, ScopePolicy

    return [
        ScopePolicy(
            principal_id=row["principal_id"],
            repository_id=row["repository_id"],
            project_id=row["project_id"],
            project_root_rel=row["project_root_rel"],
            workspace_root=row.get("workspace_root"),
            capture_allowed=bool(row["capture_allowed"]),
            recall_allowed=bool(row.get("recall_allowed", False)),
            injection_allowed=bool(row["injection_allowed"]),
            reflection_allowed=bool(row["reflection_allowed"]),
            max_egress_policy=row["max_egress_policy"],
            updated_at=row["updated_at"],
        )
        for row in store.list_scope_policies(principal_id=LOCAL_OWNER_PRINCIPAL)
    ]


def _resolved_scope(store: Any, project_root: str | Path | None) -> Any:
    root = Path(project_root or os.getcwd()).expanduser()
    return _scope_resolver().resolve(root, _policies(store))


def _make_policy(args: argparse.Namespace) -> Any:
    # Resolve once against the caller's cwd. Passing a relative path twice to
    # ScopeResolver would otherwise discover from cwd but reinterpret the
    # project root relative to the Git toplevel.
    root = Path(args.project_root).expanduser().resolve()
    capture, recall, injection, reflection = _policy_mode_flags(args.mode)
    resolver = _scope_resolver()
    now = time.time()
    if resolver.discover_git(root) is not None:
        return resolver.make_git_policy(
            root,
            root,
            capture_allowed=capture,
            recall_allowed=recall,
            injection_allowed=injection,
            reflection_allowed=reflection,
            max_egress_policy=args.egress,
            updated_at=now,
        )
    return resolver.make_workspace_policy(
        root,
        capture_allowed=capture,
        recall_allowed=recall,
        injection_allowed=injection,
        reflection_allowed=reflection,
        max_egress_policy=args.egress,
        updated_at=now,
    )


def _promote_global_mode(requested_mode: str) -> None:
    """Raise the global rollout gate so a newly enabled project can run.

    Project policies remain the authorization boundary. The global mode is a
    maximum feature capability, so enabling one project may promote it but
    disabling or narrowing another project never silently downgrades peers.
    """

    current = _global_mode()
    if requested_mode == "off" or _MODE_RANK.get(current, -1) >= _MODE_RANK[requested_mode]:
        return
    from marlow_cli.config import ensure_marlow_home, get_config_path
    from utils import atomic_roundtrip_yaml_update

    ensure_marlow_home()
    atomic_roundtrip_yaml_update(
        get_config_path(),
        "experience.mode",
        requested_mode,
    )


def _global_mode() -> str:
    from marlow_cli.config import load_config

    configured = load_config().get("experience", {})
    mode = configured.get("mode", "off") if isinstance(configured, dict) else "off"
    return mode if mode in _MODES else "off"


def _cmd_policy_set(args: argparse.Namespace) -> int:
    policy = _make_policy(args)
    with _open_store() as store:
        saved = store.upsert_scope_policy(**_plain(policy))
    saved = saved or policy
    _promote_global_mode(args.mode)
    effective, reason = _effective_mode(saved)
    print(f"Experience policy saved for {Path(args.project_root).expanduser().resolve()}")
    print(f"  policy mode:   {_stored_policy_mode(saved)}")
    print(f"  egress:        {_enum_text(_field(saved, 'max_egress_policy'))}")
    print(f"  global mode:   {_global_mode()}")
    print(f"  effective:     {effective} ({reason})")
    return 0


def _cmd_policy_show(args: argparse.Namespace) -> int:
    with _open_store() as store:
        policies = list(_policies(store))
        if not policies:
            selected = []
        elif args.all:
            selected = policies
        else:
            selected = [_resolved_scope(store, args.project_root).policy]

    if not selected:
        print("No Work Experience project policies are configured.")
        return 0
    if args.json:
        payload = []
        for policy in selected:
            effective, reason = _effective_mode(policy)
            payload.append(
                {
                    "policy": _plain(policy),
                    "global_mode": _global_mode(),
                    "effective_mode": effective,
                    "effective_reason": reason,
                }
            )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    for index, policy in enumerate(selected):
        if index:
            print()
        effective, reason = _effective_mode(policy)
        root = _field(policy, "workspace_root") or _field(policy, "project_root_rel")
        print(f"Project:          {root}")
        print(f"Repository ID:    {_field(policy, 'repository_id')}")
        print(f"Project ID:       {_field(policy, 'project_id')}")
        print(f"Policy mode:      {_stored_policy_mode(policy)}")
        print(f"Capture allowed:  {_yes_no(_field(policy, 'capture_allowed', False))}")
        print(f"Recall allowed:   {_yes_no(_field(policy, 'recall_allowed', False))}")
        print(f"Injection allowed: {_yes_no(_field(policy, 'injection_allowed', False))}")
        print(f"Reflection:       {_yes_no(_field(policy, 'reflection_allowed', False))}")
        print(f"Max egress:       {_enum_text(_field(policy, 'max_egress_policy'))}")
        print(f"Effective mode:   {effective} ({reason})")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    with _open_store() as store:
        payload = store.schema_status()
        payload.update(store.diagnostic_stats())
    if args.json:
        print(json.dumps(_plain(payload), indent=2, ensure_ascii=False))
        return 0
    print("Work Experience status")
    print(f"  schema: {_field(payload, 'schema_version')} (current={_yes_no(_field(payload, 'schema_current'))})")
    print(f"  FTS unicode61: {_yes_no(_field(payload, 'unicode61_index'))}")
    print(f"  FTS trigram: {_yes_no(_field(payload, 'trigram_index'))}")
    print(f"  revisions: {_field(payload, 'revision_count')}")
    print(f"  retrievals: {_field(payload, 'retrieval_count')}")
    print(f"  events: {_field(payload, 'event_count')}")
    print(f"  tags: {_field(payload, 'tag_count')}")
    print(f"  links: {_field(payload, 'link_count')}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    with _open_store() as store:
        report = store.doctor(repository_root=args.repository_root or os.getcwd())
    if args.json:
        print(json.dumps(_plain(report), indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 2
    print("Work Experience doctor")
    print(f"  ok: {_yes_no(report['ok'])}")
    print(f"  schema_current: {_yes_no(report['schema_current'])}")
    print(f"  FTS current: {_yes_no(report['fts_current'])}")
    print(f"  foreign_key_violations: {len(report['foreign_key_violations'])}")
    print(f"  orphan_current_revisions: {len(report['orphan_current_revisions'])}")
    print(f"  supersession_cycles: {len(report['supersession_cycles'])}")
    print(f"  active Decision authority violations: {len(report['active_decision_authority_violations'])}")
    print(f"  policy anchor violations: {len(report['policy_anchor_violations'])}")
    print(f"  stale migration mappings: {len(report['stale_migration_mappings'])}")
    return 0 if report["ok"] else 2


def _cmd_rebuild_index(args: argparse.Namespace) -> int:
    with _open_store() as store:
        result = store.rebuild_search_index()
    if args.json:
        print(json.dumps(_plain(result), indent=2, ensure_ascii=False))
        return 0
    if result.get("rebuilt"):
        print("Work Experience search indexes rebuilt")
    else:
        print(f"Work Experience search indexes not rebuilt: {result.get('reason', 'unknown')}")
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    with _open_store() as store:
        if args.apply:
            result = store.prune_diagnostics(
                now=args.now,
                max_age_days=args.max_age_days,
                max_retrievals=args.max_retrievals,
                max_events=args.max_events,
            )
        else:
            result = store.diagnostic_prune_plan(
                now=args.now,
                max_age_days=args.max_age_days,
                max_retrievals=args.max_retrievals,
                max_events=args.max_events,
            )
    if args.json:
        print(json.dumps(_plain(result), indent=2, ensure_ascii=False))
        return 0
    action = "Would remove" if not args.apply else "Removed"
    print(f"Work Experience diagnostics {action}:")
    print(f"  retrievals: {_field(result, 'retrievals_to_remove', _field(result, 'retrievals_removed'))}")
    print(f"  events: {_field(result, 'events_to_remove', _field(result, 'events_removed'))}")
    return 0


def _tags(args: argparse.Namespace) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    for attr, namespace in (
        ("task_type", "task_type"),
        ("technology", "technology"),
        ("entity", "entity"),
        ("failure", "failure"),
    ):
        for value in getattr(args, attr, ()) or ():
            values.append((namespace, value))
    return tuple(values)


def _cmd_add(args: argparse.Namespace) -> int:
    from agent.experience.models import CreatedBy, LessonBody

    body = LessonBody(
        applies_when=args.applies_when,
        does_not_apply_when=args.does_not_apply_when,
        guidance=args.guidance,
        rationale=args.rationale,
    )
    with _open_store() as store:
        resolved = _resolved_scope(store, args.project_root)
        scope = resolved.as_ref()
        lesson = store.create_lesson(
            principal_id=scope.principal_id,
            scope_type=scope.scope_type,
            scope_id=scope.scope_id,
            repository_id=scope.repository_id,
            project_id=scope.project_id,
            title=args.title,
            summary=args.summary,
            body=body,
            tags=_tags(args),
            confidence=getattr(args, "confidence", 0.6),
            sensitivity=args.sensitivity,
            egress_policy=args.egress,
            producer_trust_domain=args.producer_trust_domain,
            created_by=CreatedBy.USER,
        )
    print(f"Candidate lesson created: {_field(lesson, 'id')}")
    print("Approve it before it can enter normal recall.")
    return 0


def _decision_scope(store: Any, project_root: str | Path | None) -> Any:
    resolved = _resolved_scope(store, project_root)
    return resolved.as_ref()


def _resolved_decision_scope(store: Any, project_root: str | Path | None) -> Any:
    return _resolved_scope(store, project_root)


def _decision_authority(raw_intent: str, *, approve: str | None = None, supersede: str | None = None, revoke: str | None = None) -> Any:
    from agent.experience.authority import decision_authority_from_text

    return decision_authority_from_text(
        f"cli_turn_{int(time.time() * 1000)}",
        f"cli_session_{_profile_namespace()}",
        raw_intent,
        approved_item_ids=(approve,) if approve else (),
        supersede_target_ids=(supersede,) if supersede else (),
        revoke_target_ids=(revoke,) if revoke else (),
    )


def _in_current_scope(item: Mapping[str, Any], scope: Any) -> bool:
    return (
        item.get("principal_id") == scope.principal_id
        and item.get("scope_type") == scope.scope_type.value
        and item.get("scope_id") == scope.scope_id
        and item.get("repository_id") == scope.repository_id
        and item.get("project_id") == scope.project_id
    )


def _require_scoped_decision(item: Any, scope: Any) -> None:
    if not _in_current_scope(item, scope):
        raise LookupError("decision is outside the current project scope")


def _decision_body(args: argparse.Namespace, *, source_type: str, authority: str) -> dict[str, Any]:
    from agent.experience.models import DecisionAuthority, DecisionSourceType

    body = {
        "statement": args.statement,
        "rationale": args.rationale,
        "source_type": DecisionSourceType(source_type).value,
        "authority": DecisionAuthority(authority).value,
        "effective_at": args.effective_at,
    }
    if args.expires_at is not None:
        body["expires_at"] = args.expires_at
    if getattr(args, "policy_anchor_path", None):
        body["policy_anchor_path"] = args.policy_anchor_path
    if getattr(args, "policy_anchor_hash", None):
        body["policy_anchor_hash"] = args.policy_anchor_hash
    return body


def _print_decision(decision: Any) -> None:
    revision = _field(decision, "revision")
    body = _field(revision, "body")
    print(f"{_field(decision, 'id')}  [{_enum_text(_field(decision, 'current_status'))}]")
    print(f"Title: {_field(revision, 'title')}")
    print(f"Summary: {_field(revision, 'summary')}")
    print(f"Revision: {_field(revision, 'revision')}")
    print(f"Statement: {_field(body, 'statement')}")
    print(f"Rationale: {_field(body, 'rationale')}")
    print(f"Authority: {_field(body, 'authority')}")
    print(f"Source: {_field(body, 'source_type')}")
    print(f"Scope: {_enum_text(_field(decision, 'scope_type'))}")
    print(f"Sensitivity: {_enum_text(_field(decision, 'sensitivity'))}")
    print(f"Egress: {_enum_text(_field(decision, 'egress_policy'))}")


def _print_decision_links(links: Sequence[Mapping[str, Any]]) -> None:
    if not links:
        return
    print("Relationships:")
    for link in links:
        print(
            f"  {_field(link, 'from_item_id')} r{_field(link, 'from_revision')} "
            f"{_field(link, 'relation')} "
            f"{_field(link, 'to_item_id')} r{_field(link, 'to_revision')}"
        )


def _cmd_decision_add(args: argparse.Namespace) -> int:
    with _open_store() as store:
        scope = _decision_scope(store, args.project_root)
        decision = store.create_decision(
            principal_id=scope.principal_id,
            scope_type=scope.scope_type,
            scope_id=scope.scope_id,
            repository_id=scope.repository_id,
            project_id=scope.project_id,
            title=args.title,
            summary=args.summary,
            body=_decision_body(args, source_type="manual_import", authority="unapproved"),
            tags=_tags(args),
            sensitivity=args.sensitivity,
            egress_policy=args.egress,
            created_by="user",
        )
    print(f"Candidate decision created: {_field(decision, 'id')}")
    print("Approve it before it can enter normal recall.")
    return 0


def _cmd_decision_propose(args: argparse.Namespace) -> int:
    with _open_store() as store:
        scope = _decision_scope(store, args.project_root)
        decision = store.create_decision(
            principal_id=scope.principal_id,
            scope_type=scope.scope_type,
            scope_id=scope.scope_id,
            repository_id=scope.repository_id,
            project_id=scope.project_id,
            title=args.title or "Agent Decision Proposal",
            summary=args.summary or "",
            body=_decision_body(args, source_type="agent_proposal", authority="unapproved"),
            tags=_tags(args),
            created_by="agent",
        )
    print(f"Candidate decision proposal created: {_field(decision, 'id')}")
    return 0


def _cmd_decision_list(args: argparse.Namespace) -> int:
    with _open_store() as store:
        resolved = None if args.all_scopes else _resolved_scope(store, args.project_root)
        scope = None if resolved is None else resolved.as_ref()
        decisions = store.list_decisions(
            principal_id="local-owner",
            repository_id=None if scope is None else scope.repository_id,
            project_id=None if scope is None else scope.project_id,
            status=args.status,
            limit=args.limit,
        )
    if args.json:
        print(json.dumps([_plain(item) for item in decisions], indent=2, ensure_ascii=False))
        return 0
    if not decisions:
        print("No matching Work Experience decisions.")
        return 0
    for decision in decisions:
        revision = _field(decision, "revision", {})
        body = _field(revision, "body", {})
        print(
            f"{_field(decision, 'id')}  {_enum_text(_field(decision, 'current_status')):<14} "
            f"r{_field(revision, 'revision', _field(decision, 'current_revision', '?'))}  "
            f"{_field(body, 'authority'):<17} "
            f"{_field(revision, 'title', _field(decision, 'title', ''))}"
        )
    return 0


def _cmd_decision_show(args: argparse.Namespace) -> int:
    with _open_store() as store:
        decision = store.get_decision(args.decision_id, include_history=True)
        if decision is None:
            raise LookupError("decision not found")
        scope = _resolved_decision_scope(store, args.project_root).as_ref()
        _require_scoped_decision(decision, scope)
        links = store.list_links(item_id=args.decision_id)
    payload = _plain(decision)
    if args.json:
        payload["links"] = _plain(links)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_decision(decision)
        _print_decision_links(links)
    return 0


def _cmd_decision_approve(args: argparse.Namespace) -> int:
    with _open_store() as store:
        resolved = _resolved_decision_scope(store, args.project_root)
        scope = resolved.as_ref()
        current = store.get_decision(args.decision_id)
        if current is None:
            raise LookupError("decision not found")
        _require_scoped_decision(current, scope)
        authority = _decision_authority(f"approve {args.decision_id}", approve=args.decision_id)
        decision = store.activate_decision(
            args.decision_id,
            authority=authority,
            repository_root=resolved.repository_root,
            repository_id=scope.repository_id,
            reason=args.reason or "approved by local owner",
        )
    print(f"Decision approved: {_field(decision, 'id')} (active)")
    return 0


def _cmd_decision_edit(args: argparse.Namespace) -> int:
    kwargs: dict[str, Any] = {}
    if args.reason is not None:
        kwargs["edit_reason"] = args.reason
    if args.title is not None:
        kwargs["title"] = args.title
    if args.summary is not None:
        kwargs["summary"] = args.summary
    if any(getattr(args, attr, None) for attr in ("task_type", "technology", "entity", "failure")):
        kwargs["tags"] = _tags(args)
    if args.review_after is not None:
        kwargs["review_after"] = args.review_after
    if not kwargs and not any(
        getattr(args, attr, None) is not None
        for attr in ("statement", "rationale", "effective_at", "expires_at", "policy_anchor_path", "policy_anchor_hash")
    ):
        raise ValueError("decision edit requires at least one content or metadata change")
    with _open_store() as store:
        scope = _resolved_decision_scope(store, args.project_root).as_ref()
        current = store.get_decision(args.decision_id)
        if current is None:
            raise LookupError("decision not found")
        _require_scoped_decision(current, scope)
        if any(getattr(args, attr, None) is not None for attr in ("statement", "rationale", "effective_at", "expires_at", "policy_anchor_path", "policy_anchor_hash")):
            body = dict(current["revision"]["body"])
            if args.statement is not None:
                body["statement"] = args.statement
            if args.rationale is not None:
                body["rationale"] = args.rationale
            if args.effective_at is not None:
                body["effective_at"] = args.effective_at
            if args.expires_at is not None:
                body["expires_at"] = args.expires_at
            if args.policy_anchor_path is not None:
                body["policy_anchor_path"] = args.policy_anchor_path
            if args.policy_anchor_hash is not None:
                body["policy_anchor_hash"] = args.policy_anchor_hash
            kwargs["body"] = body
        decision = store.edit_decision(args.decision_id, **kwargs)
    print(f"Decision candidate revised: {_field(decision, 'id')} r{_field(decision, 'current_revision')}")
    return 0


def _cmd_decision_supersede(args: argparse.Namespace) -> int:
    with _open_store() as store:
        resolved = _resolved_decision_scope(store, args.project_root)
        scope = resolved.as_ref()
        old = store.get_decision(args.decision_id)
        if old is None:
            raise LookupError("decision not found")
        _require_scoped_decision(old, scope)
        authority = _decision_authority(f"supersede {args.decision_id}", supersede=args.decision_id)
        decision = store.supersede_decision(
            args.decision_id,
            authority=authority,
            principal_id=scope.principal_id,
            scope_type=scope.scope_type,
            scope_id=scope.scope_id,
            repository_id=scope.repository_id,
            project_id=scope.project_id,
            title=args.title or "Superseding Decision",
            summary=args.summary or "",
            body=_decision_body(args, source_type="user_turn", authority="user"),
            tags=_tags(args),
            repository_root=resolved.repository_root,
            reason=args.reason,
        )
    print(f"Decision superseded: {_field(old, 'id')} -> {_field(decision, 'id')}")
    return 0


def _cmd_decision_revoke(args: argparse.Namespace) -> int:
    with _open_store() as store:
        resolved = _resolved_decision_scope(store, args.project_root)
        scope = resolved.as_ref()
        current = store.get_decision(args.decision_id)
        if current is None:
            raise LookupError("decision not found")
        _require_scoped_decision(current, scope)
        authority = _decision_authority(f"revoke {args.decision_id}", revoke=args.decision_id)
        decision = store.revoke_decision(
            args.decision_id,
            authority=authority,
            reason=args.reason or "revoked by local owner",
        )
    print(f"Decision revoked: {_field(decision, 'id')}")
    return 0


def _cmd_decision_reapprove(args: argparse.Namespace) -> int:
    with _open_store() as store:
        resolved = _resolved_decision_scope(store, args.project_root)
        scope = resolved.as_ref()
        current = store.get_decision(args.decision_id)
        if current is None:
            raise LookupError("decision not found")
        _require_scoped_decision(current, scope)
        authority = _decision_authority(f"approve {args.decision_id}", approve=args.decision_id)
        decision = store.reapprove_decision(
            args.decision_id,
            authority=authority,
            repository_root=resolved.repository_root,
            repository_id=scope.repository_id,
            reason=args.reason or "reapproved by local owner",
        )
    print(f"Decision reapproved: {_field(decision, 'id')} (active)")
    return 0


def _cmd_decision_related(args: argparse.Namespace) -> int:
    with _open_store() as store:
        resolved = _resolved_decision_scope(store, args.project_root)
        scope = resolved.as_ref()
        decision = store.get_decision(args.decision_id, include_history=True)
        if decision is None:
            raise LookupError("decision not found")
        _require_scoped_decision(decision, scope)
        payload = {
            "decision": _plain(decision),
            "links": store.list_links(item_id=args.decision_id),
            "related": store.related_decisions(item_id=args.decision_id),
        }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_decision_import_policy(args: argparse.Namespace) -> int:
    with _open_store() as store:
        resolved = _resolved_decision_scope(store, args.project_root)
        scope = resolved.as_ref()
        decision = store.create_authorized_decision(
            principal_id=scope.principal_id,
            scope_type=scope.scope_type,
            scope_id=scope.scope_id,
            repository_id=scope.repository_id,
            project_id=scope.project_id,
            title=args.title,
            summary=args.summary,
            body=_decision_body(args, source_type="repository_policy", authority="repository_policy"),
            tags=_tags(args),
            created_by="import",
            authority=_decision_authority(f"import policy {args.policy_anchor_path}"),
            repository_root=resolved.repository_root,
        )
    if decision["current_status"] == "active":
        print(f"Repository-policy decision imported: {_field(decision, 'id')}")
    else:
        print(f"Repository-policy decision candidate created: {_field(decision, 'id')}")
        print("Review and approve it after fixing the anchored policy file.")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    with _open_store() as store:
        resolved = None if args.all_scopes else _resolved_scope(store, args.project_root)
        lessons = store.list_items(
            principal_id="local-owner",
            repository_id=None if resolved is None else resolved.repository_id,
            project_id=None if resolved is None else resolved.project_id,
            status=args.status,
            limit=args.limit,
        )
    if args.json:
        print(json.dumps([_plain(item) for item in lessons], indent=2, ensure_ascii=False))
        return 0
    if not lessons:
        print("No matching Work Experience lessons.")
        return 0
    for lesson in lessons:
        revision = _field(lesson, "revision", {})
        print(
            f"{_field(lesson, 'id')}  {_enum_text(_field(lesson, 'current_status')):<10} "
            f"r{_field(revision, 'revision', _field(lesson, 'current_revision', '?'))}  "
            f"{_field(revision, 'title', _field(lesson, 'title', ''))}"
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with _open_store() as store:
        lesson = store.get_item(args.lesson_id, include_history=True)
    if lesson is None:
        raise LookupError("lesson not found")
    if args.json:
        print(json.dumps(_plain(lesson), indent=2, ensure_ascii=False))
    else:
        _print_lesson(lesson)
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    with _open_store() as store:
        lesson = store.approve_lesson(
            args.lesson_id,
            reason=args.reason or "approved by local owner",
            actor="user",
        )
    print(f"Lesson approved: {_field(lesson, 'id')} (active)")
    return 0


def _cmd_retract(args: argparse.Namespace) -> int:
    with _open_store() as store:
        lesson = store.retract_lesson(
            args.lesson_id,
            reason=args.reason,
            actor="user",
        )
    print(f"Lesson retracted: {_field(lesson, 'id')}")
    print("It is no longer eligible for retrieval.")
    return 0


def _cmd_edit(args: argparse.Namespace) -> int:
    with _open_store() as store:
        lesson = store.get_item(args.lesson_id)
        if lesson is None:
            raise LookupError("lesson not found")
        current = _editable_document(lesson)
        direct = _direct_edit_document(args, current)
        edited = direct if direct is not None else _edit_json(current)
        if edited is None or edited == current:
            print("No changes; no revision created.")
            return 0
        previous_revision = int(_field(_field(lesson, "revision", {}), "revision", 0))
        body = _body_from_document(edited)
        revised = store.edit_lesson(
            args.lesson_id,
            title=edited["title"],
            summary=edited["summary"],
            body=body,
            tags=_tags_from_document(edited),
            editor="user",
            edit_reason=args.reason or "user edit",
        )
    revision = _field(_field(revised, "revision", {}), "revision", "?")
    if revision == previous_revision:
        print("No changes; no revision created.")
        return 0
    print(f"Lesson revised: {_field(revised, 'id')} (revision {revision})")
    return 0


def _legacy_consolidation_db_path() -> Path:
    from marlow_constants import get_marlow_home

    return Path(get_marlow_home()).expanduser().resolve() / "memory_consolidation.db"


def _cmd_migrate_consolidation(args: argparse.Namespace) -> int:
    from agent.experience.migrate_consolidation import apply_migration, plan_migration

    source_path = Path(args.source).expanduser() if args.source else _legacy_consolidation_db_path()
    if args.dry_run or not args.apply:
        report = plan_migration(
            source_path=source_path,
            include_archived=args.include_archived,
            limit=args.limit,
        )
    else:
        report = apply_migration(
            source_path=source_path,
            target_path=_state_db_path(),
            include_archived=args.include_archived,
            limit=args.limit,
        )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    counts = report.get("counts", {})
    print("Consolidation migration report")
    print(f"  source: {source_path}")
    print(f"  source_hash: {report.get('source_store_hash')}")
    print(f"  scanned: {counts.get('scanned', 0)}")
    print(f"  importable candidates: {counts.get('importable', 0)}")
    print(f"  skipped: {counts.get('skipped', 0)}")
    if "applied" in report:
        print(f"  applied: {len(report.get('applied', []))}")
        print(f"  already imported: {counts.get('already_imported', 0)}")
        print(f"  needs manual review: {counts.get('needs_manual_review', 0)}")
    print("Migrated decisions are candidates only and require review before recall.")
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    item_id = getattr(args, "item_id", None) or getattr(args, "lesson_id", None)
    if not item_id:
        raise ValueError("purge requires an item id")
    print(_PURGE_DISCLOSURE)
    if not args.yes and not _confirm(f"Permanently purge {item_id}?"):
        print("Purge cancelled.")
        return 0
    with _open_store() as store:
        purge_result = store.purge_item(item_id)
    if not purge_result.get("purged"):
        raise LookupError("experience item not found")
    print(f"Purged {item_id} from the active experience database.")
    print("Historical copies outside the active database may still exist as disclosed above.")
    return 0


def _cmd_why_last(args: argparse.Namespace) -> int:
    with _open_store() as store:
        resolved = _resolved_scope(store, args.project_root)
        diagnostic = _get_latest_retrieval(
            store,
            principal_id=resolved.principal_id,
            repository_id=resolved.repository_id,
            project_id=resolved.project_id,
        )
    if diagnostic is _LATEST_RETRIEVAL_UNAVAILABLE:
        print("Latest recall diagnostics are not available in this Marlow build.")
        return 0
    if diagnostic is None:
        print("No Work Experience recall diagnostic exists for this project.")
        return 0
    if args.json:
        print(json.dumps(_plain(diagnostic), indent=2, ensure_ascii=False))
        return 0
    retrieval = _field(diagnostic, "retrieval", diagnostic)
    items = _field(diagnostic, "items", ()) or ()
    print(f"Candidate recall: {_field(retrieval, 'id')}")
    print(f"Created: {_field(retrieval, 'created_at')}")
    print(f"Provider trust domain: {_field(retrieval, 'provider_trust_domain', 'local/none')}")
    print(
        "Diagnostic only: this records ranked candidates, not proof that a "
        "lesson was injected, followed, or caused the outcome."
    )
    if not items:
        print("No lesson passed the recall filters.")
        return 0
    for item in items:
        reasons = ", ".join(_field(item, "match_reasons", ()) or ()) or "no match reasons recorded"
        kind = _enum_text(_field(item, "kind")) or "unknown"
        title = _field(item, "title") or _field(item, "item_id")
        status = _enum_text(_field(item, "status")) or "unknown"
        print(
            f"  #{_field(item, 'rank', '?')} {_field(item, 'item_id')} "
            f"kind={kind} status={status} title={title} "
            f"[{_enum_text(_field(item, 'disposition', 'retrieved'))}] score={_field(item, 'score', '?')}"
        )
        print(f"     why: {reasons}")
    return 0


def _get_latest_retrieval(store: Any, **scope: str) -> Any:
    """Read the latest diagnostic across compatible store revisions.

    The validation MVP's storage worker and CLI land independently. Keeping
    this tiny compatibility seam prevents governance commands from importing
    private SQL while the public diagnostic method name settles.
    """

    for name in ("get_latest_retrieval", "latest_retrieval"):
        method = getattr(store, name, None)
        if callable(method):
            return method(**scope)
    return _LATEST_RETRIEVAL_UNAVAILABLE


def _body_from_document(document: Mapping[str, Any]) -> Any:
    from agent.experience.models import LessonBody

    return LessonBody(
        applies_when=document["applies_when"],
        does_not_apply_when=document.get("does_not_apply_when") or None,
        guidance=document["guidance"],
        rationale=document["rationale"],
    )


def _tags_from_document(document: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    from agent.experience.models import TagNamespace

    result: list[tuple[str, str]] = []
    tags = document.get("tags", {})
    if not isinstance(tags, Mapping):
        raise ValueError("tags must be an object keyed by tag namespace")
    for namespace, values in tags.items():
        ns = TagNamespace(namespace)
        if not isinstance(values, list):
            raise ValueError(f"tags.{namespace} must be a list")
        result.extend((ns.value, value) for value in values)
    return tuple(result)


def _editable_document(lesson: Any) -> dict[str, Any]:
    revision = _field(lesson, "revision")
    body = _field(revision, "body")
    tags: dict[str, list[str]] = {}
    for tag in _field(revision, "tags", ()) or ():
        tags.setdefault(_enum_text(_field(tag, "namespace")), []).append(_field(tag, "value"))
    return {
        "title": _field(revision, "title"),
        "summary": _field(revision, "summary"),
        "applies_when": _field(body, "applies_when"),
        "does_not_apply_when": _field(body, "does_not_apply_when"),
        "guidance": _field(body, "guidance"),
        "rationale": _field(body, "rationale"),
        "tags": tags,
    }


def _direct_edit_document(
    args: argparse.Namespace, current: Mapping[str, Any]
) -> dict[str, Any] | None:
    field_names = (
        "title",
        "summary",
        "applies_when",
        "does_not_apply_when",
        "guidance",
        "rationale",
    )
    if not any(getattr(args, name, None) is not None for name in field_names):
        return None
    result = dict(current)
    for name in field_names:
        value = getattr(args, name, None)
        if value is not None:
            result[name] = value
    return result


def _edit_json(initial: Mapping[str, Any]) -> dict[str, Any] | None:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(initial, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            path = handle.name
        command = [*shlex.split(editor), path]
        if not command:
            raise ValueError("EDITOR is empty")
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            raise RuntimeError("editor exited without saving a valid revision")
        with open(path, encoding="utf-8") as handle:
            edited = json.load(handle)
        if not isinstance(edited, dict):
            raise ValueError("edited lesson must be a JSON object")
        return edited
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("could not read the edited lesson JSON") from exc
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _print_lesson(lesson: Any) -> None:
    revision = _field(lesson, "revision")
    body = _field(revision, "body")
    print(f"{_field(lesson, 'id')}  [{_enum_text(_field(lesson, 'current_status'))}]")
    print(f"Title: {_field(revision, 'title')}")
    print(f"Summary: {_field(revision, 'summary')}")
    print(f"Revision: {_field(revision, 'revision')}")
    print(f"Applies when: {_field(body, 'applies_when')}")
    if _field(body, "does_not_apply_when"):
        print(f"Does not apply when: {_field(body, 'does_not_apply_when')}")
    print(f"Guidance: {_field(body, 'guidance')}")
    print(f"Rationale: {_field(body, 'rationale')}")
    tags = _field(revision, "tags", ()) or ()
    if tags:
        print(
            "Tags: "
            + ", ".join(
                f"{_enum_text(_field(tag, 'namespace'))}={_field(tag, 'value')}"
                for tag in tags
            )
        )
    print(f"Scope: {_enum_text(_field(lesson, 'scope_type'))}")
    print(f"Sensitivity: {_enum_text(_field(lesson, 'sensitivity'))}")
    print(f"Egress: {_enum_text(_field(lesson, 'egress_policy'))}")


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _enum_text(value: Any) -> str:
    scalar = value.value if isinstance(value, Enum) else value
    return "" if scalar is None else str(scalar)


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _confidence(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def _safe_error(exc: Exception) -> str:
    # Experience exceptions contain bounded, payload-free validation text.
    # Unexpected storage/provider errors can contain SQL or user content and
    # are intentionally collapsed to a generic message.
    module = exc.__class__.__module__
    if module.startswith("agent.experience") or isinstance(exc, (ValueError, LookupError)):
        try:
            from agent.experience.safety import sanitize_for_return

            return sanitize_for_return(str(exc))[:500]
        except Exception:
            return "the experience request was rejected by validation"
    return "the experience operation failed safely; no changes were applied"


def _dispatch(handler: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], None]:
    def run(args: argparse.Namespace) -> None:
        try:
            code = handler(args)
        except Exception as exc:
            # Exception messages may carry user-authored lesson content or
            # SQLite fragments. Keep logs payload-free just like stdout.
            logger.debug(
                "experience CLI operation failed (%s)",
                exc.__class__.__name__,
            )
            print(f"experience: {_safe_error(exc)}")
            raise SystemExit(2) from None
        if code:
            raise SystemExit(code)

    return run


def _add_content_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument("--title", required=required, help="Short lesson title")
    parser.add_argument("--summary", required=required, help="Concise lesson summary")
    parser.add_argument(
        "--applies-when", required=required, help="Conditions where the lesson applies"
    )
    parser.add_argument(
        "--does-not-apply-when", help="Conditions or exceptions where it must not be used"
    )
    parser.add_argument("--guidance", required=required, help="Behavior to use in future work")
    parser.add_argument("--rationale", required=required, help="Evidence-based reason for the guidance")


def register_cli(parent: argparse.ArgumentParser) -> None:
    """Attach ``marlow experience`` governance commands to *parent*."""

    parent.set_defaults(func=lambda _args: parent.print_help())
    commands = parent.add_subparsers(dest="experience_command", metavar="COMMAND")

    policy = commands.add_parser("policy", help="Manage project scope and consent policy")
    policy.set_defaults(func=lambda _args: policy.print_help())
    policy_commands = policy.add_subparsers(dest="experience_policy_command", metavar="COMMAND")

    policy_set = policy_commands.add_parser("set", help="Create or replace a project policy")
    policy_set.add_argument(
        "--project-root", required=True, help="Explicit Git project or non-Git workspace root"
    )
    policy_set.add_argument("--mode", required=True, choices=_MODES)
    policy_set.add_argument(
        "--egress", choices=_EGRESS_POLICIES, default="local_only",
        help="Maximum provider egress permitted by this project policy",
    )
    policy_set.set_defaults(func=_dispatch(_cmd_policy_set))

    policy_show = policy_commands.add_parser("show", help="Show the effective project policy")
    policy_show.add_argument("--project-root", help="Directory inside the configured project")
    policy_show.add_argument("--all", action="store_true", help="Show every configured policy")
    policy_show.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    policy_show.set_defaults(func=_dispatch(_cmd_policy_show))

    add = commands.add_parser("add", help="Add a user-authored candidate lesson")
    add.add_argument("--project-root", help="Directory inside the configured project")
    _add_content_arguments(add, required=True)
    add.add_argument("--task-type", action="append", default=[])
    add.add_argument("--technology", action="append", default=[])
    add.add_argument("--entity", action="append", default=[])
    add.add_argument("--failure", action="append", default=[], help="Normalized failure fingerprint")
    add.add_argument(
        "--confidence",
        type=_confidence,
        default=0.6,
        help="Initial evidence confidence for this manual lesson (default: 0.6)",
    )
    add.add_argument("--sensitivity", choices=_SENSITIVITIES, default="local_only")
    add.add_argument("--egress", choices=_EGRESS_POLICIES, default="local_only")
    add.add_argument("--producer-trust-domain")
    add.set_defaults(func=_dispatch(_cmd_add))

    decision = commands.add_parser("decision", help="Govern Decision Memory records")
    decision.set_defaults(func=lambda _args: decision.print_help())
    decision_commands = decision.add_subparsers(dest="experience_decision_command", metavar="COMMAND")

    decision_add = decision_commands.add_parser("add", help="Add a user-authored candidate Decision")
    decision_add.add_argument("--project-root", help="Directory inside the configured project")
    decision_add.add_argument("--title", required=True, help="Short Decision title")
    decision_add.add_argument("--summary", required=True, help="Concise Decision summary")
    decision_add.add_argument("--statement", required=True, help="Behavioral decision statement")
    decision_add.add_argument("--rationale", required=True, help="Evidence-based rationale")
    decision_add.add_argument("--effective-at", type=float, dest="effective_at", required=True)
    decision_add.add_argument("--expires-at", type=float)
    decision_add.add_argument("--task-type", action="append", default=[])
    decision_add.add_argument("--technology", action="append", default=[])
    decision_add.add_argument("--entity", action="append", default=[])
    decision_add.add_argument("--failure", action="append", default=[])
    decision_add.add_argument("--sensitivity", choices=_SENSITIVITIES, default="normal")
    decision_add.add_argument("--egress", choices=_EGRESS_POLICIES, default="local_only")
    decision_add.set_defaults(func=_dispatch(_cmd_decision_add))

    decision_propose = decision_commands.add_parser("propose", help="Add an agent-authored Decision proposal")
    decision_propose.add_argument("--project-root", help="Directory inside the configured project")
    decision_propose.add_argument("--title", help="Short Decision title")
    decision_propose.add_argument("--summary", default="", help="Concise Decision summary")
    decision_propose.add_argument("--statement", required=True, help="Behavioral decision statement")
    decision_propose.add_argument("--rationale", required=True, help="Evidence-based rationale")
    decision_propose.add_argument("--effective-at", type=float, dest="effective_at", required=True)
    decision_propose.add_argument("--expires-at", type=float)
    decision_propose.add_argument("--task-type", action="append", default=[])
    decision_propose.add_argument("--technology", action="append", default=[])
    decision_propose.add_argument("--entity", action="append", default=[])
    decision_propose.add_argument("--failure", action="append", default=[])
    decision_propose.set_defaults(func=_dispatch(_cmd_decision_propose))

    decision_list = decision_commands.add_parser("list", help="List Decisions in the current project")
    decision_list.add_argument("--project-root", help="Directory inside the configured project")
    decision_list.add_argument("--all-scopes", action="store_true", help="List Decisions across this profile")
    decision_list.add_argument("--status", action="append", choices=_DECISION_STATUSES)
    decision_list.add_argument("--limit", type=_positive_int, default=100)
    decision_list.add_argument("--json", action="store_true")
    decision_list.set_defaults(func=_dispatch(_cmd_decision_list))

    decision_show = decision_commands.add_parser("show", help="Show one Decision")
    decision_show.add_argument("decision_id")
    decision_show.add_argument("--project-root", help="Directory inside the configured project")
    decision_show.add_argument("--json", action="store_true")
    decision_show.set_defaults(func=_dispatch(_cmd_decision_show))

    decision_approve = decision_commands.add_parser("approve", help="Activate a candidate Decision")
    decision_approve.add_argument("decision_id")
    decision_approve.add_argument("--project-root", help="Directory inside the configured project")
    decision_approve.add_argument("--reason")
    decision_approve.set_defaults(func=_dispatch(_cmd_decision_approve))

    decision_edit = decision_commands.add_parser("edit", help="Append an immutable Decision revision")
    decision_edit.add_argument("decision_id")
    decision_edit.add_argument("--project-root", help="Directory inside the configured project")
    decision_edit.add_argument("--reason")
    decision_edit.add_argument("--title")
    decision_edit.add_argument("--summary")
    decision_edit.add_argument("--statement")
    decision_edit.add_argument("--rationale")
    decision_edit.add_argument("--effective-at", type=float, dest="effective_at")
    decision_edit.add_argument("--expires-at", type=float)
    decision_edit.add_argument("--policy-anchor-path")
    decision_edit.add_argument("--policy-anchor-hash")
    decision_edit.add_argument("--task-type", action="append", default=[])
    decision_edit.add_argument("--technology", action="append", default=[])
    decision_edit.add_argument("--entity", action="append", default=[])
    decision_edit.add_argument("--failure", action="append", default=[])
    decision_edit.add_argument("--review-after", type=float)
    decision_edit.set_defaults(func=_dispatch(_cmd_decision_edit))

    decision_supersede = decision_commands.add_parser("supersede", help="Replace a Decision")
    decision_supersede.add_argument("decision_id")
    decision_supersede.add_argument("--project-root", help="Directory inside the configured project")
    decision_supersede.add_argument("--title", help="Short replacement Decision title")
    decision_supersede.add_argument("--summary", default="", help="Concise replacement summary")
    decision_supersede.add_argument("--statement", required=True, help="Replacement decision statement")
    decision_supersede.add_argument("--rationale", required=True, help="Replacement rationale")
    decision_supersede.add_argument("--effective-at", type=float, dest="effective_at", required=True)
    decision_supersede.add_argument("--expires-at", type=float)
    decision_supersede.add_argument("--task-type", action="append", default=[])
    decision_supersede.add_argument("--technology", action="append", default=[])
    decision_supersede.add_argument("--entity", action="append", default=[])
    decision_supersede.add_argument("--failure", action="append", default=[])
    decision_supersede.add_argument("--reason")
    decision_supersede.set_defaults(func=_dispatch(_cmd_decision_supersede))

    decision_revoke = decision_commands.add_parser("revoke", help="Revoke a Decision")
    decision_revoke.add_argument("decision_id")
    decision_revoke.add_argument("--project-root", help="Directory inside the configured project")
    decision_revoke.add_argument("--reason")
    decision_revoke.set_defaults(func=_dispatch(_cmd_decision_revoke))

    decision_reapprove = decision_commands.add_parser("reapprove", help="Reactivate a reviewed Decision")
    decision_reapprove.add_argument("decision_id")
    decision_reapprove.add_argument("--project-root", help="Directory inside the configured project")
    decision_reapprove.add_argument("--reason")
    decision_reapprove.set_defaults(func=_dispatch(_cmd_decision_reapprove))

    decision_related = decision_commands.add_parser("related", help="Show Decision relationships")
    decision_related.add_argument("decision_id")
    decision_related.add_argument("--project-root", help="Directory inside the configured project")
    decision_related.set_defaults(func=_dispatch(_cmd_decision_related))

    decision_import_policy = decision_commands.add_parser(
        "import-policy",
        help="Import a repository-policy Decision candidate",
    )
    decision_import_policy.add_argument("--project-root", required=True, help="Configured project root")
    decision_import_policy.add_argument("--title", required=True, help="Policy Decision title")
    decision_import_policy.add_argument("--summary", required=True, help="Policy Decision summary")
    decision_import_policy.add_argument("--statement", required=True, help="Policy Decision statement")
    decision_import_policy.add_argument("--rationale", required=True, help="Policy Decision rationale")
    decision_import_policy.add_argument("--effective-at", type=float, dest="effective_at", required=True)
    decision_import_policy.add_argument("--expires-at", type=float)
    decision_import_policy.add_argument("--policy-anchor-path", required=True)
    decision_import_policy.add_argument("--policy-anchor-hash", required=True)
    decision_import_policy.add_argument("--task-type", action="append", default=[])
    decision_import_policy.add_argument("--technology", action="append", default=[])
    decision_import_policy.add_argument("--entity", action="append", default=[])
    decision_import_policy.add_argument("--failure", action="append", default=[])
    decision_import_policy.set_defaults(func=_dispatch(_cmd_decision_import_policy))

    list_parser = commands.add_parser("list", help="List lessons in the current project")
    list_parser.add_argument("--project-root", help="Directory inside the configured project")
    list_parser.add_argument("--all-scopes", action="store_true", help="List lessons across this profile")
    list_parser.add_argument("--status", action="append", choices=_LESSON_STATUSES)
    list_parser.add_argument("--limit", type=_positive_int, default=100)
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=_dispatch(_cmd_list))

    show = commands.add_parser("show", help="Show one lesson, including its content")
    show.add_argument("lesson_id")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=_dispatch(_cmd_show))

    approve = commands.add_parser("approve", help="Activate a candidate lesson")
    approve.add_argument("lesson_id")
    approve.add_argument("--reason")
    approve.set_defaults(func=_dispatch(_cmd_approve))

    edit = commands.add_parser("edit", help="Append an immutable lesson revision")
    edit.add_argument("lesson_id")
    edit.add_argument("--reason")
    _add_content_arguments(edit, required=False)
    edit.set_defaults(func=_dispatch(_cmd_edit))

    retract = commands.add_parser("retract", help="Remove a lesson from behavioral use")
    retract.add_argument("lesson_id")
    retract.add_argument("--reason", required=True)
    retract.set_defaults(func=_dispatch(_cmd_retract))

    migrate = commands.add_parser("migrate", help="Migration helpers for legacy memory data")
    migrate.set_defaults(func=lambda _args: migrate.print_help())
    migrate_commands = migrate.add_subparsers(dest="experience_migrate_command", metavar="COMMAND")
    migrate_consolidation = migrate_commands.add_parser(
        "consolidation",
        help="Migrate legacy memory consolidation records into review candidates",
    )
    migrate_consolidation.add_argument(
        "--source",
        help="Legacy memory_consolidation.db path; defaults to the active Marlow home",
    )
    migrate_consolidation.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would migrate without writing (default)",
    )
    migrate_consolidation.add_argument(
        "--apply",
        action="store_true",
        help="Import active/conflicted legacy decisions as candidate Decisions",
    )
    migrate_consolidation.add_argument(
        "--include-archived",
        action="store_true",
        help="Report archived/superseded legacy records without activating them",
    )
    migrate_consolidation.add_argument("--limit", type=_positive_int, default=100)
    migrate_consolidation.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    migrate_consolidation.set_defaults(func=_dispatch(_cmd_migrate_consolidation))

    purge = commands.add_parser("purge", help="Best-effort permanent deletion from active state.db")
    purge.add_argument("item_id")
    purge.add_argument("-y", "--yes", action="store_true", help="Skip interactive confirmation")
    purge.set_defaults(func=_dispatch(_cmd_purge))

    delete = commands.add_parser(
        "delete",
        help="Compatibility alias for `purge`; requires the explicit --purge flag",
    )
    delete.add_argument("item_id")
    delete.add_argument(
        "--purge",
        action="store_true",
        required=True,
        help="Confirm that best-effort physical deletion, not retraction, is intended",
    )
    delete.add_argument("-y", "--yes", action="store_true", help="Skip interactive confirmation")
    delete.set_defaults(func=_dispatch(_cmd_purge))

    why = commands.add_parser("why", help="Explain the latest recall decision")
    why.add_argument("--last", action="store_true", required=True)
    why.add_argument("--project-root", help="Directory inside the configured project")
    why.add_argument("--json", action="store_true")
    why.set_defaults(func=_dispatch(_cmd_why_last))

    why_last = commands.add_parser("why-last", help="Alias for `experience why --last`")
    why_last.add_argument("--project-root", help="Directory inside the configured project")
    why_last.add_argument("--json", action="store_true")
    why_last.set_defaults(func=_dispatch(_cmd_why_last), last=True)

    status = commands.add_parser("status", help="Show Work Experience schema, FTS, and metadata counts")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_dispatch(_cmd_status))

    doctor = commands.add_parser("doctor", help="Run metadata-only Work Experience integrity checks")
    doctor.add_argument("--repository-root", help="Repository root for anchored policy checks")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_dispatch(_cmd_doctor))

    rebuild_index = commands.add_parser(
        "rebuild-index",
        help="Rebuild Work Experience unicode61 and trigram search indexes",
    )
    rebuild_index.add_argument("--json", action="store_true")
    rebuild_index.set_defaults(func=_dispatch(_cmd_rebuild_index))

    prune = commands.add_parser(
        "prune",
        help="Dry-run or apply bounded pruning for retrieval diagnostics",
    )
    prune.add_argument("--apply", action="store_true", help="Actually delete diagnostics")
    prune.add_argument("--now", type=float)
    prune.add_argument("--max-age-days", type=_positive_int, default=30)
    prune.add_argument("--max-retrievals", type=_positive_int, default=10_000)
    prune.add_argument("--max-events", type=_positive_int, default=10_000)
    prune.add_argument("--json", action="store_true")
    prune.set_defaults(func=_dispatch(_cmd_prune))

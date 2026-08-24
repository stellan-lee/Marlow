from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent.experience.authority import DecisionTurnAuthority
from agent.experience.models import EgressPolicy, ScopePolicy, ScopeType
from agent.experience.store import ExperienceStore


def _authority(
    source_hash: str,
    *,
    approve: tuple[str, ...] = (),
    supersede: tuple[str, ...] = (),
    revoke: tuple[str, ...] = (),
    explicit: bool = False,
) -> DecisionTurnAuthority:
    return DecisionTurnAuthority(
        source_turn_id="turn-authority",
        source_session_id="session-authority",
        raw_user_text_hash=source_hash,
        explicit_remember_grant=explicit,
        approved_item_ids=approve,
        supersede_target_ids=supersede,
        revoke_target_ids=revoke,
    )


def _policy(project_id: str = "project_test") -> ScopePolicy:
    return ScopePolicy(
        principal_id="local-owner",
        repository_id="repo_test",
        project_id=project_id,
        project_root_rel=".",
        capture_allowed=True,
        recall_allowed=True,
        injection_allowed=True,
        reflection_allowed=False,
        max_egress_policy=EgressPolicy.EXPLICIT_ANY_PROVIDER,
        updated_at=1.0,
    )


def _runtime(db_path: Path, authority: DecisionTurnAuthority, project_id: str = "project_test") -> SimpleNamespace:
    return SimpleNamespace(
        authority=authority,
        scope=SimpleNamespace(
            scope_type=ScopeType.PROJECT,
            scope_id=project_id,
            repository_id="repo_test",
            project_id=project_id,
        ),
        policy=SimpleNamespace(capture_allowed=True),
        repository_root=None,
        provider_trust_domain="local-runtime",
        provider_is_local=True,
    )


def _seed_decision(store: ExperienceStore, item_id: str, project_id: str = "project_test") -> str:
    item = store.create_decision(
        item_id=item_id,
        principal_id="local-owner",
        scope_type="project",
        scope_id=project_id,
        repository_id="repo_test",
        project_id=project_id,
        title=f"Decision {item_id}",
        summary="A candidate Decision for tool tests.",
        body={
            "statement": "Use Decision Memory only after approval.",
            "rationale": "Authority must be explicit.",
            "source_type": "agent_proposal",
            "authority": "unapproved",
            "effective_at": 1.0,
        },
        tags={"component": ["agent/experience"]},
        created_by="agent",
    )
    return item["id"]


def test_experience_decision_propose_creates_candidate(tmp_path: Path, monkeypatch) -> None:
    from tools import experience_decision_tool as tool

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(tool, "_state_db_path", lambda _kw: str(db_path))
    authority = _authority("a" * 64)

    result = json.loads(tool.experience_decision_tool({
        "action": "propose",
        "title": "Use bounded retrieval",
        "summary": "Keep retrieval bounded.",
        "statement": "Use bounded retrieval for long-term memory.",
        "rationale": "Bounded recall reduces stale influence.",
        "tags": {"component": ["agent/experience"]},
    }, runtime=_runtime(db_path, authority)))

    assert result["status"] == "ok"
    assert result["action"] == "propose"
    assert result["decision"]["status"] == "candidate"
    assert result["decision"]["authority"] == "unapproved"
    assert result["active"] is False
    with ExperienceStore(db_path) as store:
        decision = store.get_decision(result["decision"]["id"])
    assert decision is not None
    assert decision["current_status"] == "candidate"
    assert decision["revision"]["body"]["authority"] == "unapproved"


def test_experience_decision_remember_without_grant_creates_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    from tools import experience_decision_tool as tool

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(tool, "_state_db_path", lambda _kw: str(db_path))
    authority = _authority("a" * 64, explicit=False)

    result = json.loads(tool.experience_decision_tool({
        "action": "remember",
        "statement": "Use project-local retrieval.",
        "rationale": "The user asked in ambiguous terms.",
        "title": "Ambiguous remember",
    }, runtime=_runtime(db_path, authority)))

    assert result["status"] == "ok"
    assert result["action"] == "remember"
    assert result["active"] is False
    assert result["decision"]["authority"] == "unapproved"


def test_experience_decision_remember_with_grant_activates_exact_source(
    tmp_path: Path, monkeypatch
) -> None:
    from tools import experience_decision_tool as tool

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(tool, "_state_db_path", lambda _kw: str(db_path))
    source_hash = "b" * 64
    authority = _authority(source_hash, explicit=True)

    result = json.loads(tool.experience_decision_tool({
        "action": "remember",
        "statement": "Use project-local retrieval.",
        "rationale": "The user explicitly asked to remember this.",
    }, runtime=_runtime(db_path, authority)))

    assert result["status"] == "ok"
    assert result["active"] is True
    assert result["decision"]["status"] == "active"
    assert result["decision"]["authority"] == "user"


def test_experience_decision_approve_requires_exact_authority(tmp_path: Path, monkeypatch) -> None:
    from tools import experience_decision_tool as tool

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(tool, "_state_db_path", lambda _kw: str(db_path))
    with ExperienceStore(db_path) as store:
        item_id = _seed_decision(store, "decision_candidate")

    denied = json.loads(tool.experience_decision_tool({
        "action": "approve",
        "decision_id": item_id,
    }, runtime=_runtime(db_path, _authority("c" * 64))))
    assert "error" in denied

    allowed = json.loads(tool.experience_decision_tool({
        "action": "approve",
        "decision_id": item_id,
        "reason": "Approved by exact ID",
    }, runtime=_runtime(db_path, _authority("d" * 64, approve=(item_id,)))))
    assert allowed["status"] == "ok"
    assert allowed["decision"]["status"] == "active"
    assert allowed["decision"]["authority"] == "user"


def test_experience_decision_supersede_and_revoke_require_exact_authority(
    tmp_path: Path, monkeypatch
) -> None:
    from tools import experience_decision_tool as tool

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(tool, "_state_db_path", lambda _kw: str(db_path))
    with ExperienceStore(db_path) as store:
        old_id = _seed_decision(store, "decision_old")
        store.activate_decision(
            old_id,
            authority=_authority("a" * 64, approve=(old_id,)),
            repository_root=None,
            repository_id="repo_test",
        )

    denied = json.loads(tool.experience_decision_tool({
        "action": "supersede",
        "statement": "Use a narrower retrieval policy.",
        "rationale": "The old policy is too broad.",
        "replaces": old_id,
    }, runtime=_runtime(db_path, _authority("b" * 64))))
    assert "error" in denied

    superseded = json.loads(tool.experience_decision_tool({
        "action": "supersede",
        "statement": "Use a narrower retrieval policy.",
        "rationale": "The old policy is too broad.",
        "replaces": old_id,
        "title": "Narrower retrieval policy",
    }, runtime=_runtime(db_path, _authority("c" * 64, supersede=(old_id,)))))
    assert superseded["status"] == "ok"
    replacement_id = superseded["decision"]["id"]

    revoke_denied = json.loads(tool.experience_decision_tool({
        "action": "revoke",
        "decision_id": replacement_id,
    }, runtime=_runtime(db_path, _authority("d" * 64))))
    assert "error" in revoke_denied

    revoked = json.loads(tool.experience_decision_tool({
        "action": "revoke",
        "decision_id": replacement_id,
    }, runtime=_runtime(db_path, _authority("e" * 64, revoke=(replacement_id,)))))
    assert revoked["status"] == "ok"
    assert revoked["decision"]["status"] == "revoked"


def test_experience_decision_show_and_related_reject_out_of_scope(
    tmp_path: Path, monkeypatch
) -> None:
    from tools import experience_decision_tool as tool

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(tool, "_state_db_path", lambda _kw: str(db_path))
    with ExperienceStore(db_path) as store:
        _seed_decision(store, "decision_other", project_id="other_project")
    runtime = _runtime(db_path, _authority("a" * 64), project_id="project_test")

    shown = json.loads(tool.experience_decision_tool({
        "action": "show",
        "decision_id": "decision_other",
    }, runtime=runtime))
    assert "error" in shown

    related = json.loads(tool.experience_decision_tool({
        "action": "related",
        "decision_id": "decision_other",
    }, runtime=runtime))
    assert "error" in related


def test_experience_decision_requires_trusted_runtime(tmp_path: Path) -> None:
    from tools import experience_decision_tool as tool

    result = json.loads(tool.experience_decision_tool({"action": "propose", "statement": "x", "rationale": "y"}))
    assert "error" in result
    assert "trusted runtime" in result["error"]

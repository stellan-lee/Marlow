from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agent.experience.models import (
    CreatedBy,
    Decision,
    DecisionAuthority,
    DecisionBody,
    DecisionRevision,
    DecisionSourceType,
    DecisionStatus,
    EgressPolicy,
    LessonBody,
    LessonRevision,
    LessonStatus,
    LessonTag,
    ScopePolicy,
    ScopeRef,
    ScopeType,
    TagNamespace,
    can_transition_decision,
    can_transition_lesson,
    lesson_content_hash,
    require_decision_transition,
    require_lesson_transition,
)


def test_candidate_is_canonical_and_proposed_is_only_an_input_alias() -> None:
    assert LessonStatus("candidate") is LessonStatus.CANDIDATE
    assert LessonStatus("proposed") is LessonStatus.CANDIDATE
    assert LessonStatus.PROPOSED.value == "candidate"


def test_lesson_lifecycle_is_forward_only_and_replay_safe() -> None:
    assert can_transition_lesson("candidate", "active")
    assert can_transition_lesson("candidate", "rejected")
    assert can_transition_lesson("active", "disputed")
    assert can_transition_lesson("disputed", "deprecated")
    assert can_transition_lesson("active", "active")

    for terminal in ("deprecated", "rejected", "retracted"):
        assert can_transition_lesson(terminal, terminal)
        assert not can_transition_lesson(terminal, "active")

    with pytest.raises(ValueError, match="invalid lesson transition"):
        require_lesson_transition("retracted", "active")


def test_revision_content_and_evidence_metadata_are_immutable() -> None:
    revision = LessonRevision(
        item_id="les_example",
        revision=1,
        title="Use the focused verification path",
        summary="The focused test runner avoids unrelated failures.",
        body=LessonBody(
            applies_when="Changing the experience persistence core",
            does_not_apply_when="A full release gate was requested",
            guidance="Run the focused repository test target.",
            rationale="It keeps the validation signal attributable.",
        ),
        confidence=0.8,
        source_session_id="session-1",
        source_turn_id="turn-1",
        source_work_id="work-1",
        source_hash="a" * 64,
        tags=(
            LessonTag(TagNamespace.TECHNOLOGY, "SQLite"),
            LessonTag(TagNamespace.TASK_TYPE, "Persistence"),
        ),
        producer_metadata=(("provider", "test-provider"),),
        created_at=1.0,
        last_validated_at=2.0,
    )

    assert revision.content_hash == lesson_content_hash(
        revision.body,
        title=revision.title,
        summary=revision.summary,
        tags=revision.tags,
    )
    assert revision.tags == tuple(sorted(revision.tags))
    with pytest.raises(FrozenInstanceError):
        revision.revision = 2  # type: ignore[misc]


def test_scope_rejects_cross_owner_and_incomplete_project_identity() -> None:
    with pytest.raises(ValueError, match="local-owner"):
        ScopeRef("another-user", ScopeType.PROFILE, "profile")
    with pytest.raises(ValueError, match="project scope"):
        ScopeRef("local-owner", ScopeType.PROJECT, "project")

    scope = ScopeRef(
        "local-owner",
        ScopeType.PROJECT,
        "project:abc",
        repository_id="repo:abc",
        project_id="project:abc",
    )
    assert scope.scope_type is ScopeType.PROJECT
    assert EgressPolicy.LOCAL_ONLY.value == "local_only"


def test_scope_ids_must_match_their_authorization_axis() -> None:
    with pytest.raises(ValueError, match="scope_id"):
        ScopeRef(
            "local-owner",
            ScopeType.PROJECT,
            "project:forged",
            repository_id="repo:abc",
            project_id="project:abc",
        )
    with pytest.raises(ValueError, match="only repository_id"):
        ScopeRef(
            "local-owner",
            ScopeType.REPOSITORY,
            "repo:abc",
            repository_id="repo:abc",
            project_id="project:abc",
        )


def test_scope_policy_recall_consent_defaults_denied_and_round_trips() -> None:
    denied = ScopePolicy(
        principal_id="local-owner",
        repository_id="repo:abc",
        project_id="project:abc",
        project_root_rel=".",
    )
    assert denied.recall_allowed is False

    allowed = ScopePolicy.from_mapping(
        {
            **denied.to_dict(),
            "capture_allowed": True,
            "recall_allowed": True,
        }
    )
    assert allowed.recall_allowed is True
    assert allowed.to_dict()["recall_allowed"] is True

    legacy = denied.to_dict()
    legacy.pop("recall_allowed")
    assert ScopePolicy.from_mapping(legacy).recall_allowed is False


def test_lesson_body_mapping_rejects_untyped_extra_fields() -> None:
    with pytest.raises(ValueError, match="unknown lesson body fields"):
        LessonBody.from_mapping(
            {
                **LessonBody("when", "guidance", "rationale").to_dict(),
                "raw_tool_output": "must never become model-visible",
            }
        )


def test_decision_body_rejects_unknown_fields_and_authority_mismatches() -> None:
    with pytest.raises(ValueError, match="unknown decision body fields"):
        DecisionBody.from_mapping(
            {
                **DecisionBody(
                    statement="Use SQLite links",
                    rationale="Relationships remain inspectable.",
                    source_type=DecisionSourceType.MANUAL_IMPORT,
                    authority=DecisionAuthority.UNAPPROVED,
                    effective_at=1.0,
                ).to_dict(),
                "raw_tool_output": "must never become a Decision",
            }
        )

    with pytest.raises(ValueError, match="repository_policy authority"):
        DecisionBody(
            statement="Use the local policy",
            rationale="Policy is explicit.",
            source_type=DecisionSourceType.MANUAL_IMPORT,
            authority=DecisionAuthority.REPOSITORY_POLICY,
            effective_at=1.0,
            policy_anchor_path="AGENTS.md",
            policy_anchor_hash="a" * 64,
        )

    with pytest.raises(ValueError, match="policy_anchor_path"):
        DecisionBody(
            statement="Use the local policy",
            rationale="Policy is explicit.",
            source_type=DecisionSourceType.REPOSITORY_POLICY,
            authority=DecisionAuthority.REPOSITORY_POLICY,
            effective_at=1.0,
            policy_anchor_path="../AGENTS.md",
            policy_anchor_hash="a" * 64,
        )

    with pytest.raises(ValueError, match="repository_policy source_type"):
        DecisionBody(
            statement="Policy source",
            rationale="Source is policy.",
            source_type=DecisionSourceType.REPOSITORY_POLICY,
            authority=DecisionAuthority.UNAPPROVED,
            effective_at=1.0,
        )


def test_decision_lifecycle_is_forward_only_and_replay_safe() -> None:
    assert can_transition_decision("candidate", "active")
    assert can_transition_decision("candidate", "review_required")
    assert can_transition_decision("active", "review_required")
    assert can_transition_decision("review_required", "active")
    assert can_transition_decision("candidate", "candidate")

    for terminal in ("superseded", "revoked"):
        assert can_transition_decision(terminal, terminal)
        assert not can_transition_decision(terminal, "active")

    assert require_decision_transition("candidate", "REVIEW_REQUIRED") is (
        DecisionStatus.REVIEW_REQUIRED
    )
    with pytest.raises(ValueError, match="invalid decision transition"):
        require_decision_transition("revoked", "active")


def test_decision_models_require_consistent_authority() -> None:
    body = DecisionBody(
        statement="Use Decision Memory only when the design is approved.",
        rationale="Authority must be explicit.",
        source_type=DecisionSourceType.MANUAL_IMPORT,
        authority=DecisionAuthority.UNAPPROVED,
        effective_at=1.0,
    )
    revision = DecisionRevision(
        item_id="decision_test",
        revision=1,
        title="Candidate decision",
        summary="Not injectable.",
        body=body,
        source_session_id="session-1",
        source_turn_id="turn-1",
        source_work_id="work-1",
        source_hash="a" * 64,
        tags=(LessonTag(TagNamespace.COMPONENT, "agent/experience"),),
        producer_metadata=(("provider", "test-provider"),),
        created_at=1.0,
    )
    decision = Decision(
        id="decision_test",
        family_id="decision_test",
        status=DecisionStatus.CANDIDATE,
        scope=ScopeRef(
            "local-owner",
            ScopeType.PROJECT,
            "project:abc",
            repository_id="repo:abc",
            project_id="project:abc",
        ),
        sensitivity="normal",
        egress_policy="local_only",
        producer_trust_domain="provider:a",
        created_by="agent",
        created_at=1.0,
        updated_at=2.0,
        revision=revision,
    )
    assert decision.status is DecisionStatus.CANDIDATE

    with pytest.raises(ValueError, match="active decisions require"):
        Decision(
            id=decision.id,
            family_id=decision.family_id,
            status=DecisionStatus.ACTIVE,
            scope=decision.scope,
            sensitivity=decision.sensitivity,
            egress_policy=decision.egress_policy,
            producer_trust_domain=decision.producer_trust_domain,
            created_by=decision.created_by,
            created_at=decision.created_at,
            updated_at=decision.updated_at,
            revision=decision.revision,
        )

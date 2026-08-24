from __future__ import annotations

import hashlib
from pathlib import Path

from agent.experience.models import RetrievalQuery, ScopeRef, ScopeType
from agent.experience.service import ExperienceService
from agent.experience.store import ExperienceStore


def _seed(store: ExperienceStore) -> ScopeRef:
    store.upsert_scope_policy(
        principal_id="local-owner",
        repository_id="repo",
        project_id="project",
        project_root_rel="apps/api",
        recall_allowed=True,
        injection_allowed=True,
        max_egress_policy="same_provider_trust_domain",
    )
    store.create_lesson(
        item_id="lesson",
        principal_id="local-owner",
        scope_type="project",
        scope_id="project",
        repository_id="repo",
        project_id="project",
        title="Avoid synchronized SQLite retries",
        summary="Use bounded jitter for contending writers.",
        body={
            "applies_when": "SQLite returns database is locked",
            "does_not_apply_when": "Only one writer exists",
            "guidance": "Use <BEGIN IMMEDIATE> and bounded random jitter.",
            "rationale": "Deterministic retry intervals form a convoy.",
        },
        tags={
            "technology": ["sqlite"],
            "task_type": ["persistence"],
            "failure": ["database is locked"],
        },
        confidence=0.9,
        sensitivity="private_repo",
        egress_policy="same_provider_trust_domain",
        producer_trust_domain="provider:a",
    )
    store.approve_lesson("lesson")
    return ScopeRef(
        principal_id="local-owner",
        scope_type=ScopeType.PROJECT,
        scope_id="project",
        repository_id="repo",
        project_id="project",
    )


def _query(scope: ScopeRef, *, provider: str = "provider:a") -> RetrievalQuery:
    return RetrievalQuery(
        scope=scope,
        query_text="Fix SQLite writer contention",
        provider_trust_domain=provider,
        technologies=("sqlite",),
        task_types=("persistence",),
        failure_fingerprints=("database is locked",),
    )


def _authority(hash_hex: str) -> object:
    from agent.experience.authority import DecisionTurnAuthority

    return DecisionTurnAuthority(
        source_turn_id="turn-authority",
        source_session_id="session-authority",
        raw_user_text_hash=hash_hex,
        explicit_remember_grant=True,
    )


def _active_decision(store: ExperienceStore) -> dict:
    created = store.create_decision(
        item_id="decision",
        principal_id="local-owner",
        scope_type="project",
        scope_id="project",
        repository_id="repo",
        project_id="project",
        title="Use bounded SQLite retries",
        summary="Decisions outrank historical lessons.",
        body={
            "statement": "Use Decision Memory for SQLite retry policy.",
            "rationale": "The current policy was approved by the user.",
            "source_type": "agent_proposal",
            "authority": "unapproved",
            "effective_at": 1.0,
        },
        tags={
            "technology": ["sqlite"],
            "task_type": ["persistence"],
            "failure": ["database is locked"],
        },
        sensitivity="private_repo",
        egress_policy="same_provider_trust_domain",
        producer_trust_domain="provider:a",
        source_hash="a" * 64,
    )
    assert created["id"] == "decision"
    return store.activate_decision(
        "decision",
        authority=_authority("a" * 64),
        transitioned_at=3.0,
    )


def _active_profile_decision(store: ExperienceStore, *, item_id: str) -> dict:
    created = store.create_decision(
        item_id=item_id,
        principal_id="local-owner",
        scope_type="profile",
        scope_id="local-owner",
        repository_id=None,
        project_id=None,
        title=f"Profile Decision {item_id}",
        summary="Profile-scoped Decision.",
        body={
            "statement": "Use profile policy for this repository.",
            "rationale": "The profile owner approved this durable preference.",
            "source_type": "agent_proposal",
            "authority": "unapproved",
            "effective_at": 1.0,
        },
        tags={"technology": ["sqlite"]},
        sensitivity="normal",
        egress_policy="same_provider_trust_domain",
        producer_trust_domain="provider:a",
        source_hash="a" * 64,
    )
    return store.activate_decision(item_id, authority=_authority("a" * 64), transitioned_at=3.0)


def _active_repository_decision(store: ExperienceStore, *, item_id: str) -> dict:
    created = store.create_decision(
        item_id=item_id,
        principal_id="local-owner",
        scope_type="repository",
        scope_id="repo",
        repository_id="repo",
        project_id=None,
        title=f"Repository Decision {item_id}",
        summary="Repository-scoped Decision.",
        body={
            "statement": "Use repository policy for this codebase.",
            "rationale": "The repository owner approved this durable policy.",
            "source_type": "agent_proposal",
            "authority": "unapproved",
            "effective_at": 1.0,
        },
        tags={"technology": ["sqlite"]},
        sensitivity="normal",
        egress_policy="same_provider_trust_domain",
        producer_trust_domain="provider:a",
        source_hash="a" * 64,
    )
    return store.activate_decision(item_id, authority=_authority("a" * 64), transitioned_at=3.0)


def test_deferred_application_api_is_not_exposed() -> None:
    assert not hasattr(ExperienceService, "declare_applied")


def test_service_retrieves_records_diagnostics_and_formats_bounded_advice(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        scope = _seed(store)
        service = ExperienceService(store, max_context_chars=600)

        result = service.retrieve(
            _query(scope),
            turn_id="turn-1",
            work_id="work-1",
        )

        assert [item.item_id for item in result.items] == ["lesson"]
        assert result.item_diagnostics[0].match_reasons[0] == "project exact"
        stored = store.get_retrieval(result.diagnostic.id)
        assert stored is not None
        assert "Fix SQLite" not in repr(stored)
        assert stored["task_signature_hash"] == result.diagnostic.task_signature_hash

        context = service.format_context(result)
        assert len(context) <= 600
        assert context.startswith("<work-experience-context")
        assert "Historical, fallible evidence" in context
        assert "&lt;BEGIN IMMEDIATE&gt;" in context
        assert context.endswith("</work-experience-context>")


def test_provider_change_and_retraction_fail_closed(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        scope = _seed(store)
        service = ExperienceService(store)

        wrong_provider = service.retrieve(
            _query(scope, provider="provider:b"),
            turn_id="turn-b",
            work_id="work-b",
        )
        assert wrong_provider.items == ()
        assert wrong_provider.item_diagnostics == ()
        assert service.format_context(wrong_provider) == ""

        previously_authorized = service.retrieve(
            _query(scope),
            turn_id="turn-authorized",
            work_id="work-authorized",
        )
        assert previously_authorized.items
        store.retract_lesson("lesson", reason="Superseded by current evidence")
        assert service.format_context(previously_authorized) == ""
        retracted = service.retrieve(
            _query(scope),
            turn_id="turn-c",
            work_id="work-c",
        )
        assert retracted.items == ()


def test_task_signature_metadata_is_deterministic_and_text_is_not_stored(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        scope = _seed(store)
        query = _query(scope)
        service = ExperienceService(store)

        first = service.retrieve(query, turn_id="turn", work_id="work")
        second = service.retrieve(query, turn_id="turn", work_id="work")

        assert first.diagnostic.id == second.diagnostic.id
        assert first.diagnostic.task_signature_hash == second.diagnostic.task_signature_hash
        assert store.diagnostic_stats()["retrieval_count"] == 1


def test_combined_retrieval_separates_decisions_and_lessons(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        scope = _seed(store)
        _active_decision(store)
        service = ExperienceService(store, max_context_chars=2_000)

        result = service.retrieve_decisions_and_lessons(
            _query(scope),
            turn_id="turn-combined",
            work_id="work-combined",
            max_decisions=2,
            max_lessons=2,
        )

        assert [item.item_id for item in result.decisions] == ["decision"]
        assert [item.item_id for item in result.lessons] == ["lesson"]
        assert result.decisions[0].authority == "user"
        assert result.item_diagnostics[0].item_id == "decision"
        assert result.item_diagnostics[1].item_id == "lesson"

        context = service.format_combined_context(result)
        assert context.startswith("<active-decision-context")
        assert context.index("</active-decision-context>") < context.index("<work-experience-context")
        assert "Historical continuing decisions" in context
        assert "Historical, fallible evidence" in context
        assert "Use Decision Memory" in context
        assert "Use &lt;BEGIN IMMEDIATE&gt;" in context


def test_combined_retrieval_includes_broader_applicable_decisions(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        scope = _seed(store)
        _active_decision(store)
        _active_repository_decision(store, item_id="repo_decision")
        _active_profile_decision(store, item_id="profile_decision")
        service = ExperienceService(store, max_context_chars=2_000)

        result = service.retrieve_decisions_and_lessons(
            _query(scope),
            turn_id="turn-broader",
            work_id="work-broader",
            max_decisions=5,
            max_lessons=1,
        )

        assert [item.item_id for item in result.decisions] == [
            "decision",
            "repo_decision",
            "profile_decision",
        ]
        context = service.format_combined_context(result)
        assert "project/project" in context
        assert "repository/repo" in context
        assert "profile/local-owner" in context


def test_combined_context_truncates_long_decision_items(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        scope = _seed(store)
        store.create_decision(
            item_id="long_decision",
            principal_id="local-owner",
            scope_type="project",
            scope_id="project",
            repository_id="repo",
            project_id="project",
            title="Long Decision",
            summary="A long Decision for truncation.",
            body={
                "statement": "Use this very long decision statement " + ("word " * 80),
                "rationale": "Use this very long rationale " + ("reason " * 80),
                "source_type": "agent_proposal",
                "authority": "unapproved",
                "effective_at": 1.0,
            },
            tags={"technology": ["sqlite"]},
            sensitivity="normal",
            egress_policy="explicit_any_provider",
            producer_trust_domain="provider:a",
            source_hash="a" * 64,
        )
        store.activate_decision("long_decision", authority=_authority("a" * 64), transitioned_at=3.0)
        service = ExperienceService(store, max_context_chars=320)

        result = service.retrieve_decisions_and_lessons(
            _query(scope),
            turn_id="turn-long",
            work_id="work-long",
            max_decisions=1,
            max_lessons=0,
        )

        context = service.format_combined_context(result)
        assert len(context) <= 320
        assert context.startswith("<active-decision-context")
        assert "Use this very long decision statement" in context
        assert "…" in context


def test_combined_provider_fallback_reauthorizes_and_filters(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        scope = _seed(store)
        _active_decision(store)
        service = ExperienceService(store, max_context_chars=1_200)

        result = service.retrieve_decisions_and_lessons(
            _query(scope),
            turn_id="turn-provider",
            work_id="work-provider",
        )
        assert result.decisions
        assert service.format_combined_context(result, provider_trust_domain="provider:b") == ""


def test_shadow_retrieval_does_not_require_injection_permission(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        scope = _seed(store)
        store.upsert_scope_policy(
            principal_id="local-owner",
            repository_id="repo",
            project_id="project",
            project_root_rel="apps/api",
            capture_allowed=True,
            recall_allowed=True,
            injection_allowed=False,
            max_egress_policy="same_provider_trust_domain",
        )
        service = ExperienceService(store)

        shadow = service.retrieve(
            _query(scope),
            turn_id="turn-shadow",
            work_id="work-shadow",
            require_injection_allowed=False,
        )
        assist = service.retrieve(
            _query(scope),
            turn_id="turn-assist",
            work_id="work-assist",
            require_injection_allowed=True,
        )

        assert [item.item_id for item in shadow.items] == ["lesson"]
        assert assist.items == ()


def test_record_disclosure_events_records_only_injected_items(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        scope = _seed(store)
        _active_decision(store)
        service = ExperienceService(store)

        result = service.retrieve_decisions_and_lessons(
            _query(scope),
            turn_id="turn-disclosure",
            work_id="work-disclosure",
            max_decisions=1,
            max_lessons=1,
        )
        assert service.record_disclosure_events(result) == 2
        assert service.record_disclosure_events(result) == 2
        events = [
            event for event in store.list_influence_events(retrieval_id=result.diagnostic.id)
            if event["event_type"] == "disclosed"
        ]
        assert len(events) == 2
        assert {event["item_id"] for event in events} == {"decision", "lesson"}

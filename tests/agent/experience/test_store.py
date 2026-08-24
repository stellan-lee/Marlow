from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
from pathlib import Path

import pytest

from agent.experience.store import ExperienceStore
from marlow_state import SessionDB


REPO_ID = "repo_test"
PROJECT_ID = "project_test"


def _body(guidance: str = "Use a focused verification before the full suite.") -> dict[str, str]:
    return {
        "applies_when": "Changing SQLite-backed experience state",
        "does_not_apply_when": "The change is documentation only",
        "guidance": guidance,
        "rationale": "Focused checks keep failures attributable.",
    }


def _policy(store: ExperienceStore, *, injection_allowed: bool = True) -> None:
    store.upsert_scope_policy(
        principal_id="local-owner",
        repository_id=REPO_ID,
        project_id=PROJECT_ID,
        project_root_rel="apps/api",
        recall_allowed=True,
        injection_allowed=injection_allowed,
        max_egress_policy="explicit_any_provider",
        updated_at=1.0,
    )


def _lesson(
    store: ExperienceStore,
    *,
    item_id: str = "lesson_test",
    project_id: str = PROJECT_ID,
    status: str = "active",
    sensitivity: str = "normal",
    egress_policy: str = "same_provider_trust_domain",
    producer_trust_domain: str = "provider:a",
    tags: dict[str, list[str]] | None = None,
) -> dict:
    created = store.create_lesson(
        item_id=item_id,
        idempotency_key=f"create:{item_id}",
        principal_id="local-owner",
        scope_type="project",
        scope_id=project_id,
        repository_id=REPO_ID,
        project_id=project_id,
        title=f"Lesson {item_id}",
        summary="A bounded, manually curated lesson.",
        body=_body(),
        tags=tags or {"technology": ["sqlite"], "task_type": ["persistence"]},
        confidence=0.8,
        sensitivity=sensitivity,
        egress_policy=egress_policy,
        producer_trust_domain=producer_trust_domain,
        created_by="user",
        source_session_id="source-session",
        source_turn_id="source-turn",
        source_work_id="source-work",
        source_hash="a" * 64,
        created_at=2.0,
    )
    if status == "active":
        return store.approve_lesson(item_id, transitioned_at=3.0)
    if status != "candidate":
        return store.transition_lesson(item_id, status, transitioned_at=3.0)
    return created


def _search(store: ExperienceStore, **overrides: object) -> list[dict]:
    values: dict[str, object] = {
        "principal_id": "local-owner",
        "scope_type": "project",
        "scope_id": PROJECT_ID,
        "repository_id": REPO_ID,
        "project_id": PROJECT_ID,
        "provider_trust_domain": "provider:a",
        "provider_is_local": False,
        "tags": {"technology": ["sqlite"]},
        "limit": 10,
    }
    values.update(overrides)
    return store.search_lessons(**values)


def _search_decision(store: ExperienceStore, **overrides: object) -> list[dict]:
    values: dict[str, object] = {
        "principal_id": "local-owner",
        "scope_type": "project",
        "scope_id": PROJECT_ID,
        "repository_id": REPO_ID,
        "project_id": PROJECT_ID,
        "provider_trust_domain": "provider:a",
        "provider_is_local": False,
        "tags": {"component": ["agent/experience"]},
        "limit": 10,
    }
    values.update(overrides)
    return store.search_decisions(**values)


def test_manual_lifecycle_uses_immutable_idempotent_revisions(tmp_path: Path) -> None:
    path = (tmp_path / "profile" / "state.db").resolve()
    with ExperienceStore(path) as store:
        _policy(store)
        first = _lesson(store, status="candidate")
        replay = _lesson(store, status="candidate")
        assert first["id"] == replay["id"] == "lesson_test"
        assert first["current_status"] == "candidate"
        assert first["current_revision"] == 1

        active = store.approve_lesson("lesson_test", transitioned_at=3.0)
        assert active["current_status"] == "active"
        assert store.approve_lesson("lesson_test")["current_status"] == "active"

        edited = store.edit_lesson(
            "lesson_test",
            body=_body("Checkpoint the WAL after the focused verification."),
            tags={"technology": ["sqlite", "wal"], "task_type": ["persistence"]},
            edit_reason="Clarify the verified sequence",
            idempotency_key="edit:lesson_test:2",
            edited_at=4.0,
        )
        edit_replay = store.edit_lesson(
            "lesson_test",
            body=_body("Checkpoint the WAL after the focused verification."),
            tags={"technology": ["sqlite", "wal"], "task_type": ["persistence"]},
            idempotency_key="edit:lesson_test:2",
        )
        assert edited["revision"]["revision"] == 2
        assert edit_replay["revision"]["revision"] == 2
        history = store.get_item("lesson_test", include_history=True)
        assert history is not None
        assert [revision["revision"] for revision in history["revisions"]] == [1, 2]
        assert history["revisions"][0]["body"]["guidance"] != history["revisions"][1]["body"]["guidance"]

        with sqlite3.connect(path) as raw:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                raw.execute(
                    "UPDATE experience_item_revisions SET title = 'rewrite' "
                    "WHERE item_id = 'lesson_test' AND revision = 1"
                )

        retracted = store.retract_lesson(
            "lesson_test", reason="No longer applicable", transitioned_at=5.0
        )
        assert retracted["current_status"] == "retracted"
        assert retracted["deleted_at"] == 5.0
        assert _search(store) == []
        with pytest.raises(ValueError, match="terminal"):
            store.edit_lesson("lesson_test", title="Cannot rewrite history")


def test_search_hard_filters_scope_status_policy_and_provider_egress(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        _lesson(store, item_id="same-domain")
        _lesson(
            store,
            item_id="different-domain",
            egress_policy="same_provider_trust_domain",
            producer_trust_domain="provider:b",
        )
        _lesson(store, item_id="blocked", sensitivity="blocked")
        _lesson(store, item_id="candidate", status="candidate")
        store.upsert_scope_policy(
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id="project_other",
            project_root_rel="apps/other",
            recall_allowed=True,
            injection_allowed=True,
            max_egress_policy="explicit_any_provider",
        )
        _lesson(store, item_id="other-project", project_id="project_other")

        assert [item["id"] for item in _search(store)] == ["same-domain"]
        other = _search(store, project_id="project_other", scope_id="project_other")
        assert [item["id"] for item in other] == ["other-project"]
        assert [
            item["id"] for item in _search(store, provider_trust_domain="provider:b")
        ] == ["different-domain"]
        assert {item["id"] for item in _search(store, provider_is_local=True)} == {
            "same-domain",
            "different-domain",
        }

        store.upsert_scope_policy(
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            project_root_rel="apps/api",
            recall_allowed=True,
            injection_allowed=False,
            max_egress_policy="explicit_any_provider",
        )
        assert _search(store) == []


def test_search_and_reauthorization_require_project_recall_consent(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        lesson = _lesson(store)
        assert _search(store, require_injection_allowed=False)

        store.upsert_scope_policy(
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            project_root_rel="apps/api",
            capture_allowed=True,
            recall_allowed=False,
            injection_allowed=True,
            max_egress_policy="explicit_any_provider",
            updated_at=4.0,
        )

        assert _search(store, require_injection_allowed=False) == []
        assert store.authorized_lesson_revisions(
            principal_id="local-owner",
            scope_type="project",
            scope_id=PROJECT_ID,
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            provider_trust_domain="provider:a",
            candidates=((lesson["id"], lesson["current_revision"]),),
            require_injection_allowed=False,
        ) == set()


def test_list_items_filters_multiple_statuses_in_sql_before_limit(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        _lesson(store, item_id="lesson_active")
        _lesson(store, item_id="lesson_candidate", status="candidate")
        _lesson(store, item_id="lesson_rejected", status="candidate")
        store.transition_lesson(
            "lesson_rejected", "rejected", transitioned_at=3.0
        )

        filtered = store.list_items(
            status=("candidate", "rejected"),
            limit=2,
        )
        candidate_only = store.list_items(status=("candidate",), limit=1)

    assert {item["current_status"] for item in filtered} == {
        "candidate",
        "rejected",
    }
    assert [item["id"] for item in candidate_only] == ["lesson_candidate"]


def _decision_body(statement: str = "Use Decision Memory only after approval.") -> dict[str, object]:
    return {
        "statement": statement,
        "rationale": "The design requires explicit authority before activation.",
        "source_type": "manual_import",
        "authority": "unapproved",
        "effective_at": 1.0,
    }


def _decision(
    store: ExperienceStore,
    *,
    item_id: str = "decision_test",
    project_id: str = PROJECT_ID,
    body: dict[str, object] | None = None,
    source_hash: str | None = "a" * 64,
    sensitivity: str = "normal",
    egress_policy: str = "same_provider_trust_domain",
    producer_trust_domain: str = "provider:a",
    created_by: str = "agent",
    review_after: float | None = None,
) -> dict:
    return store.create_decision(
        item_id=item_id,
        idempotency_key=f"create:{item_id}",
        principal_id="local-owner",
        scope_type="project",
        scope_id=project_id,
        repository_id=REPO_ID,
        project_id=project_id,
        title=f"Decision {item_id}",
        summary="A candidate Decision for governance inspection.",
        body=body or _decision_body(),
        tags={"task_type": ["memory"], "component": ["agent/experience"]},
        sensitivity=sensitivity,
        egress_policy=egress_policy,
        producer_trust_domain=producer_trust_domain,
        source_session_id="source-session",
        source_turn_id="source-turn",
        source_work_id="source-work",
        source_hash=source_hash,
        created_by=created_by,
        review_after=review_after,
        created_at=2.0,
    )


def _activate_decision(store: ExperienceStore, item_id: str, *, source_hash: str = "a" * 64, repository_root: Path | None = None) -> dict:
    store.activate_decision(
        item_id,
        authority=_authority(source_hash),
        repository_root=repository_root,
        transitioned_at=3.0,
    )
    decision = store.get_decision(item_id)
    assert decision is not None
    return decision


def _active_decision(
    store: ExperienceStore,
    *,
    item_id: str,
    statement: str = "Use SQLite decisions for Marlow recall.",
    **kwargs: object,
) -> dict:
    body = dict(kwargs)
    repository_root = body.pop("repository_root", None)
    created_by = body.pop("created_by", "agent")
    review_after = body.pop("review_after", None)
    if "source_type" not in body:
        body["source_type"] = "agent_proposal"
    if "authority" not in body:
        body["authority"] = "unapproved"
    decision = _decision(
        store,
        item_id=item_id,
        body={
            **_decision_body(statement),
            **body,
        },
        source_hash="a" * 64,
        created_by=created_by,
        review_after=review_after,
    )
    assert decision["id"] == item_id
    return _activate_decision(store, item_id, repository_root=repository_root)


def test_search_does_not_cap_or_overflow_large_authorized_candidate_set(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        for index in range(1_001):
            tags = (
                {"failure": ["late metadata match"]}
                if index == 1_000
                else {"technology": ["bulk fixture"]}
            )
            _lesson(
                store,
                item_id=f"lesson_{index:04d}",
                tags=tags,
            )

        matches = _search(
            store,
            tags={"failure": ["late metadata match"]},
            require_injection_allowed=False,
        )

    assert [item["id"] for item in matches] == ["lesson_1000"]


def test_decision_candidates_are_stored_but_not_lesson_retrieved(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        decision = _decision(store)

        assert decision["kind"] == "decision"
        assert decision["current_status"] == "candidate"
        assert decision["revision"]["body"]["authority"] == "unapproved"
        assert decision["revision"]["tags"][0]["namespace"] == "component"
        assert _search(store) == []
        assert store.list_decisions(status="candidate")[0]["id"] == "decision_test"
        assert store.get_decision("decision_test")["id"] == "decision_test"


def test_decision_get_rejects_lesson_kind_mismatch(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        _lesson(store)

        with pytest.raises(KeyError, match="unknown decision"):
            store.get_decision("lesson_test")


def test_decision_idempotency_revision_and_edit_boundaries(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        first = _decision(store)
        replay = _decision(store)
        assert first["id"] == replay["id"] == "decision_test"
        assert first["current_revision"] == replay["current_revision"] == 1

        edited = store.edit_decision(
            "decision_test",
            body=_decision_body(
                "Use Decision Memory only after approval and review."
            ),
            edit_reason="Clarify the approval gate",
            idempotency_key="edit:decision_test:2",
            edited_at=3.0,
        )
        replay_edit = store.edit_decision(
            "decision_test",
            body=_decision_body(
                "Use Decision Memory only after approval and review."
            ),
            idempotency_key="edit:decision_test:2",
        )
        assert edited["revision"]["revision"] == replay_edit["revision"]["revision"] == 2
        history = store.get_decision("decision_test", include_history=True)
        assert history is not None
        assert [revision["revision"] for revision in history["revisions"]] == [1, 2]
        assert history["revisions"][1]["edit_reason"] == "Clarify the approval gate"

        store.revoke_decision(
            "decision_test",
            authority=_authority("b" * 64, revoke=("decision_test",)),
            reason="No longer applicable",
            transitioned_at=4.0,
        )
        with pytest.raises(ValueError, match="can only edit nonterminal decisions"):
            store.edit_decision(
                "decision_test",
                body=_decision_body("Revoked decisions are immutable."),
                edited_at=5.0,
            )


def test_active_decision_meaningful_edit_enters_review_required(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        activated = store.activate_decision(
            _decision(store)["id"],
            authority=_authority("a" * 64),
            transitioned_at=3.0,
        )
        assert activated["current_status"] == "active"

        revised = store.edit_decision(
            "decision_test",
            body=_decision_body("Use Decision Memory only after approval and review."),
            edit_reason="Clarify the approval gate",
            edited_at=4.0,
        )

        assert revised["current_status"] == "review_required"
        assert revised["current_revision"] == 3
        assert revised["revision"]["body"]["statement"] == (
            "Use Decision Memory only after approval and review."
        )
        assert revised["revision"]["body"]["authority"] == "unapproved"
        assert store.list_decisions(status="active") == []
        event_types = {event["event_type"] for event in store.list_events(item_id="decision_test")}
        assert "review_required" in event_types
        assert "edited" in event_types


def test_active_decision_cosmetic_edit_remains_active(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        activated = store.activate_decision(
            _decision(store)["id"],
            authority=_authority("a" * 64),
            transitioned_at=3.0,
        )
        assert activated["current_status"] == "active"

        revised = store.edit_decision(
            "decision_test",
            title="Decision Memory governance",
            edit_reason="Shorten the title",
            edited_at=4.0,
        )

        assert revised["current_status"] == "active"
        assert revised["current_revision"] == 3
        assert revised["revision"]["title"] == "Decision Memory governance"
        assert store.list_decisions(status="active")[0]["id"] == "decision_test"


def test_decision_transitions_are_bounded_and_terminal(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        _decision(store)

        with pytest.raises(ValueError, match="invalid decision transition"):
            store.transition_decision("decision_test", "active", transitioned_at=3.0)

        reviewed = store.transition_decision(
            "decision_test",
            "review_required",
            actor="user",
            reason="Needs design approval",
            idempotency_key="transition:decision_test:review",
            transitioned_at=3.0,
        )
        replay_review = store.transition_decision(
            "decision_test",
            "review_required",
            actor="user",
            reason="Needs design approval",
            idempotency_key="transition:decision_test:review",
            transitioned_at=3.5,
        )
        assert reviewed["current_status"] == "review_required"
        assert replay_review["current_status"] == "review_required"
        assert [item["id"] for item in store.list_decisions(status="review_required")] == [
            "decision_test"
        ]

        revoked = store.transition_decision(
            "decision_test",
            "revoked",
            reason="Not approved",
            transitioned_at=4.0,
        )
        assert revoked["current_status"] == "revoked"
        assert revoked["deleted_at"] == 4.0
        assert store.list_decisions() == []
        assert [item["id"] for item in store.list_decisions(include_deleted=True)] == [
            "decision_test"
        ]


def _authority(
    hash_hex: str,
    *,
    approve: tuple[str, ...] = (),
    supersede: tuple[str, ...] = (),
    revoke: tuple[str, ...] = (),
) -> object:
    from agent.experience.authority import DecisionTurnAuthority

    return DecisionTurnAuthority(
        source_turn_id="turn-authority",
        source_session_id="session-authority",
        raw_user_text_hash=hash_hex,
        explicit_remember_grant=True,
        approved_item_ids=approve,
        supersede_target_ids=supersede,
        revoke_target_ids=revoke,
    )


def test_decision_activation_requires_trusted_user_authority_and_rewrites_revision(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        decision = _decision(
            store,
            body={
                **_decision_body("Use Decision Memory only after explicit approval."),
                "source_type": "agent_proposal",
                "authority": "unapproved",
            },
            source_hash="a" * 64,
        )
        assert decision["revision"]["body"]["authority"] == "unapproved"

        with pytest.raises(ValueError, match="trusted user authority"):
            store.activate_decision(
                "decision_test",
                authority=_authority("b" * 64),
                transitioned_at=3.0,
            )

        activated = store.activate_decision(
            "decision_test",
            authority=_authority("a" * 64),
            reason="Approved by current user turn",
            transitioned_at=3.0,
        )
        assert activated["current_status"] == "active"
        assert activated["current_revision"] == 2
        assert activated["revision"]["body"]["authority"] == "user"
        assert store.list_decisions(status="candidate") == []
        assert store.list_decisions(status="active")[0]["id"] == "decision_test"
        events = store.list_events(item_id="decision_test")
        assert events[0]["event_type"] == "activated"
        assert events[0]["payload"]["to_status"] == "active"


def test_decision_activation_ignores_model_supplied_authority(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        with pytest.raises(ValueError, match="candidate decisions must be unapproved"):
            _decision(
                store,
                body={
                    **_decision_body(),
                    "source_type": "agent_proposal",
                    "authority": "user",
                },
            )

        decision = _decision(
            store,
            body={
                **_decision_body(),
                "source_type": "agent_proposal",
                "authority": "unapproved",
            },
        )
        assert decision["revision"]["body"]["authority"] == "unapproved"

        with pytest.raises(ValueError, match="trusted user authority"):
            store.activate_decision(
                "decision_test",
                authority=_authority("b" * 64),
                transitioned_at=3.0,
            )


def test_repository_policy_decision_requires_live_anchor(tmp_path: Path) -> None:
    policy_file = tmp_path / "policy.md"
    policy_file.write_text("# policy\n", encoding="utf-8")
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        store.upsert_scope_policy(
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            project_root_rel=".",
            recall_allowed=True,
            injection_allowed=True,
            max_egress_policy="explicit_any_provider",
        )
        decision = store.create_decision(
            item_id="decision_policy",
            principal_id="local-owner",
            scope_type="project",
            scope_id=PROJECT_ID,
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            title="Policy decision",
            body={
                **_decision_body("Repository policy is active only while anchored."),
                "source_type": "repository_policy",
                "authority": "repository_policy",
                "policy_anchor_path": "policy.md",
                "policy_anchor_hash": hashlib.sha256(policy_file.read_bytes()).hexdigest(),
            },
            created_by="import",
        )
        assert decision["current_status"] == "candidate"

        activated = store.activate_decision(
            "decision_policy",
            authority=_authority("d" * 64),
            repository_root=tmp_path,
            transitioned_at=2.0,
        )
        assert activated["current_status"] == "active"
        assert activated["revision"]["body"]["authority"] == "repository_policy"

        policy_file.write_text("# changed policy\n", encoding="utf-8")
        reviewed = store.mark_decision_review_required(
            "decision_policy",
            repository_root=tmp_path,
            transitioned_at=3.0,
        )
        assert reviewed["current_status"] == "review_required"
        assert store.list_decisions(status="active") == []
        assert store.list_events(
            item_id="decision_policy",
            event_type="anchor_invalidated",
        )[0]["payload"]["reason"] == "policy anchor hash mismatch"


def test_decision_anchor_rejects_symlink_and_path_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    policy_file = root / "policy.md"
    outside = tmp_path / "outside.md"
    policy_file.write_text("# policy\n", encoding="utf-8")
    outside.write_text("# outside\n", encoding="utf-8")
    (root / "link.md").symlink_to(outside)

    from agent.experience.anchors import validate_repository_anchor

    digest = hashlib.sha256(policy_file.read_bytes()).hexdigest()
    assert validate_repository_anchor(
        "policy.md", digest, repository_root=root
    ).valid
    assert not validate_repository_anchor(
        "../outside.md", digest, repository_root=root
    ).valid
    assert not validate_repository_anchor(
        "link.md", digest, repository_root=root
    ).valid


def test_decision_supersession_is_atomic_and_preserves_history(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        old = _decision(
            store,
            item_id="decision_old",
            body={
                **_decision_body("Old Decision stays active until replaced."),
                "source_type": "agent_proposal",
                "authority": "unapproved",
            },
            source_hash="a" * 64,
        )
        store.activate_decision(
            "decision_old",
            authority=_authority("a" * 64),
            transitioned_at=3.0,
        )
        replacement = store.supersede_decision(
            "decision_old",
            authority=_authority("b" * 64, supersede=("decision_old",)),
            principal_id="local-owner",
            scope_type="project",
            scope_id=PROJECT_ID,
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            title="Replacement decision",
            body={
                **_decision_body("Replacement Decision narrows the old guidance."),
                "source_type": "agent_proposal",
                "authority": "unapproved",
            },
            created_by="user",
            source_hash="b" * 64,
            transitioned_at=4.0,
        )
        assert replacement["current_status"] == "active"
        assert replacement["revision"]["body"]["authority"] == "user"
        assert store.get_decision("decision_old")["current_status"] == "superseded"
        assert store.get_decision("decision_old", include_history=True)["revisions"][-1]["body"]["statement"] == old["revision"]["body"]["statement"]
        assert store.get_decision("decision_old")["deleted_at"] == 4.0
        assert store.list_decisions(status="active")[0]["id"] == replacement["id"]


def test_decision_revocation_is_terminal_and_logical_deleted(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        _decision(store)
        store.activate_decision(
            "decision_test",
            authority=_authority("a" * 64),
            transitioned_at=3.0,
        )
        revoked = store.revoke_decision(
            "decision_test",
            authority=_authority("b" * 64, revoke=("decision_test",)),
            reason="No longer applicable",
            transitioned_at=4.0,
        )
        assert revoked["current_status"] == "revoked"
        assert revoked["deleted_at"] == 4.0
        assert store.list_decisions() == []
        assert store.list_decisions(include_deleted=True)[0]["id"] == "decision_test"
        with pytest.raises(ValueError, match="terminal decisions"):
            store.revoke_decision(
                "decision_test",
                authority=_authority("c" * 64, revoke=("decision_test",)),
                transitioned_at=5.0,
            )


def test_decision_authority_boundaries_are_explicit(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        decision = _decision(store)
        activated = store.activate_decision(
            "decision_test",
            authority=_authority("a" * 64),
            transitioned_at=3.0,
        )
        assert activated["current_status"] == "active"

        with pytest.raises(ValueError, match="only candidate decisions can be activated"):
            store.activate_decision(
                "decision_test",
                authority=_authority("a" * 64),
                transitioned_at=4.0,
            )
        store.mark_decision_review_required(
            "decision_test",
            reason="Needs review",
            transitioned_at=5.0,
        )

        with pytest.raises(ValueError, match="only candidate decisions can be activated"):
            store.activate_decision(
                "decision_test",
                authority=_authority("a" * 64),
                transitioned_at=6.0,
            )
        with pytest.raises(ValueError, match="repository_id does not match"):
            store.reapprove_decision(
                "decision_test",
                authority=_authority("a" * 64),
                repository_id="repo_other",
                transitioned_at=7.0,
            )
        reapproved = store.reapprove_decision(
            "decision_test",
            authority=_authority("a" * 64),
            reason="Reviewed and still valid",
            transitioned_at=8.0,
        )
        assert reapproved["current_status"] == "active"
        assert reapproved["current_revision"] == 3

        with pytest.raises(ValueError, match="only review_required decisions can be reapproved"):
            store.reapprove_decision(
                "decision_test",
                authority=_authority("a" * 64),
                transitioned_at=9.0,
            )
        with pytest.raises(ValueError, match="revocation requires explicit trusted user authority"):
            store.revoke_decision(
                "decision_test",
                authority=_authority("a" * 64),
                transitioned_at=10.0,
            )
        with pytest.raises(ValueError, match="revocation requires explicit trusted user authority"):
            store.revoke_decision(
                "decision_test",
                authority=_authority("a" * 64, approve=("decision_test",)),
                transitioned_at=11.0,
            )

def test_decision_creation_rejects_non_candidate_authority_and_bad_scope(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        with pytest.raises(ValueError, match="candidate decisions must be unapproved or repository_policy"):
            store.create_decision(
                item_id="decision_user",
                principal_id="local-owner",
                scope_type="project",
                scope_id=PROJECT_ID,
                repository_id=REPO_ID,
                project_id=PROJECT_ID,
                title="Decision user",
                body={
                    **_decision_body(),
                    "authority": "user",
                },
            )
        with pytest.raises(ValueError, match="repository_policy decisions cannot be unapproved"):
            store.create_decision(
                item_id="decision_policy_bad",
                principal_id="local-owner",
                scope_type="project",
                scope_id=PROJECT_ID,
                repository_id=REPO_ID,
                project_id=PROJECT_ID,
                title="Decision policy",
                body={
                    **_decision_body(),
                    "source_type": "repository_policy",
                },
            )
        with pytest.raises(ValueError, match="project-scoped decisions"):
            store.create_decision(
                item_id="decision_bad_scope",
                principal_id="local-owner",
                scope_type="project",
                scope_id=PROJECT_ID,
                repository_id=None,
                project_id=None,
                title="Decision bad scope",
                body=_decision_body(),
            )


def test_active_decision_search_returns_only_injectable_decision(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        _active_decision(
            store,
            item_id="decision_search",
            statement="Use SQLite decisions for Marlow recall.",
        )

        rows = _search_decision(store, query="SQLite")

        assert [row["id"] for row in rows] == ["decision_search"]
        assert rows[0]["revision"]["body"]["authority"] == "user"
        assert any(reason.startswith("project exact") for reason in rows[0]["match_reasons"])


def test_decision_search_excludes_non_active_lifecycle_states(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        _active_decision(store, item_id="decision_active")
        _decision(store, item_id="decision_candidate")
        candidate = _decision(store, item_id="decision_review")
        assert candidate["id"] == "decision_review"
        store.transition_decision("decision_review", "review_required", transitioned_at=4.0)
        revoked = _decision(store, item_id="decision_revoked")
        assert revoked["id"] == "decision_revoked"
        store.revoke_decision(
            "decision_revoked",
            authority=_authority("a" * 64, revoke=("decision_revoked",)),
            transitioned_at=5.0,
        )

        rows = _search_decision(store, query="SQLite")

        assert [row["id"] for row in rows] == ["decision_active"]


def test_decision_search_excludes_expired_and_review_due_decisions(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        _active_decision(
            store,
            item_id="decision_expired",
            expires_at=90.0,
        )
        _active_decision(
            store,
            item_id="decision_review_due",
            review_after=90.0,
        )

        rows = _search_decision(store, query="SQLite", now=100.0)

        assert rows == []


def test_repository_policy_decision_search_requires_live_anchor(tmp_path: Path) -> None:
    policy_file = tmp_path / "policy.md"
    policy_file.write_text("# policy\n", encoding="utf-8")
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        _active_decision(
            store,
            item_id="decision_policy_search",
            statement="Repository policy is active only while anchored.",
            source_type="repository_policy",
            authority="repository_policy",
            policy_anchor_path="policy.md",
            policy_anchor_hash=hashlib.sha256(policy_file.read_bytes()).hexdigest(),
            created_by="import",
            repository_root=tmp_path,
        )
        policy_file.write_text("# changed policy\n", encoding="utf-8")

        assert _search_decision(
            store,
            query="policy",
            repository_root=tmp_path,
        ) == []


def test_chinese_and_mixed_language_decision_search_uses_cjk_paths(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        _active_decision(
            store,
            item_id="decision_cjk",
            statement="Marlow 决策 SQLite 默认使用同一策略。",
        )

        rows = _search_decision(store, query="决策 SQLite")

        assert [row["id"] for row in rows] == ["decision_cjk"]
        reasons = " | ".join(rows[0]["match_reasons"])
        assert "short cjk fallback" in reasons.lower() or "trigram term overlap" in reasons.lower()


def test_existing_policy_schema_migrates_recall_consent_as_default_deny(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "state.db").resolve()
    with sqlite3.connect(path) as legacy:
        legacy.execute(
            """
            CREATE TABLE experience_scope_policies (
                principal_id TEXT NOT NULL,
                repository_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                project_root_rel TEXT NOT NULL,
                workspace_root TEXT,
                capture_allowed INTEGER NOT NULL DEFAULT 0,
                injection_allowed INTEGER NOT NULL DEFAULT 0,
                reflection_allowed INTEGER NOT NULL DEFAULT 0,
                max_egress_policy TEXT NOT NULL DEFAULT 'local_only',
                updated_at REAL NOT NULL,
                PRIMARY KEY (principal_id, repository_id, project_id)
            )
            """
        )
        legacy.execute(
            """
            INSERT INTO experience_scope_policies(
                principal_id, repository_id, project_id, project_root_rel,
                capture_allowed, injection_allowed, reflection_allowed,
                max_egress_policy, updated_at
            ) VALUES ('local-owner', ?, ?, 'apps/api', 1, 1, 0,
                      'explicit_any_provider', 1.0)
            """,
            (REPO_ID, PROJECT_ID),
        )

    with ExperienceStore(path) as migrated:
        policy = migrated.get_scope_policy(
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
        )
        assert policy is not None
        assert policy["capture_allowed"] is True
        assert policy["recall_allowed"] is False
        assert policy["injection_allowed"] is True

    with ExperienceStore(path) as reopened:
        assert reopened.get_scope_policy(
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
        )["recall_allowed"] is False

    with sqlite3.connect(path) as raw:
        columns = {
            row[1] for row in raw.execute("PRAGMA table_info(experience_scope_policies)")
        }
        version = raw.execute(
            "SELECT value FROM experience_schema_meta WHERE key = 'version'"
        ).fetchone()[0]
    assert "recall_allowed" in columns
    assert version == "5"


def test_deferred_mutation_surfaces_are_not_exposed() -> None:
    assert not hasattr(ExperienceStore, "add_link")
    assert not hasattr(ExperienceStore, "record_event")
    assert not hasattr(ExperienceStore, "update_retrieval_item")


def test_retrieval_and_item_diagnostics_are_atomic_text_free_and_purge_safe(
    tmp_path: Path,
) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        lesson = _lesson(store)
        result = _search(store)[0]
        retrieval = store.record_retrieval(
            retrieval_id="retrieval_test",
            idempotency_key="retrieval:test",
            turn_id="turn-1",
            work_id="work-1",
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            task_signature_hash="b" * 64,
            provider_trust_domain="provider:a",
            items=[
                {
                    "item_id": lesson["id"],
                    "item_revision": result["revision"]["revision"],
                    "rank": 1,
                    "score": result["score"],
                    "match_reasons": result["match_reasons"],
                }
            ],
            created_at=10.0,
        )
        replay = store.record_retrieval(
            retrieval_id="retrieval_test",
            idempotency_key="retrieval:test",
            turn_id="turn-1",
            work_id="work-1",
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            task_signature_hash="b" * 64,
            provider_trust_domain="provider:a",
            items=retrieval["items"],
        )
        assert replay["id"] == retrieval["id"]
        assert "body" not in repr(retrieval)
        latest = store.get_latest_retrieval(
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
        )
        assert latest is not None
        assert latest["id"] == retrieval["id"]
        assert retrieval["items"][0]["disposition"] == "retrieved"
        assert "planned_effect" not in retrieval["items"][0]

        purged = store.purge_item("lesson_test", vacuum=False)
        assert purged["purged"] is True
        assert store.get_item("lesson_test") is None
        remaining = store.get_retrieval("retrieval_test")
        assert remaining is not None and remaining["items"] == []
        assert store.list_events(item_id="lesson_test") == []


def test_session_delete_does_not_cascade_to_experience(tmp_path: Path) -> None:
    path = (tmp_path / "state.db").resolve()
    session_db = SessionDB(path)
    session_db.create_session("source-session", "cli")
    session_db.close()

    with ExperienceStore(path) as store:
        _policy(store)
        _lesson(store)

    session_db = SessionDB(path)
    assert session_db.delete_session("source-session") is True
    session_db.close()

    with ExperienceStore(path) as store:
        assert store.get_item("lesson_test") is not None


def test_concurrent_idempotent_create_retries_to_one_item(tmp_path: Path) -> None:
    path = (tmp_path / "state.db").resolve()
    stores = (ExperienceStore(path), ExperienceStore(path))
    for store in stores:
        _policy(store)
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def create(store: ExperienceStore) -> None:
        try:
            barrier.wait()
            results.append(_lesson(store, status="candidate")["id"])
        except BaseException as exc:  # surfaced below with its original type
            errors.append(exc)

    threads = [threading.Thread(target=create, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    try:
        assert errors == []
        assert results == ["lesson_test", "lesson_test"]
        assert len(stores[0].list_items()) == 1
    finally:
        for store in stores:
            store.close()


def test_rejects_nonfinite_diagnostic_scores(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        _lesson(store)
        with pytest.raises(ValueError):
            store.record_retrieval(
                turn_id="turn",
                work_id="work",
                principal_id="local-owner",
                repository_id=REPO_ID,
                project_id=PROJECT_ID,
                task_signature_hash="c" * 64,
                provider_trust_domain="provider:a",
                items=[
                    {
                        "item_id": "lesson_test",
                        "item_revision": 1,
                        "rank": 1,
                        "score": math.inf,
                        "match_reasons": ["project exact"],
                    }
                ],
            )


def test_rejects_credentials_in_identifiers_and_json_keys(tmp_path: Path) -> None:
    secret = "sk-" + ("a1B2c3D4" * 8)
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        with pytest.raises(ValueError, match="unsafe item_id"):
            store.create_lesson(
                item_id=secret,
                principal_id="local-owner",
                scope_type="project",
                scope_id=PROJECT_ID,
                repository_id=REPO_ID,
                project_id=PROJECT_ID,
                title="Safe title",
                body=_body(),
            )
        with pytest.raises(ValueError, match="unsafe object key"):
            store.create_lesson(
                item_id="lesson_safe_metadata",
                principal_id="local-owner",
                scope_type="project",
                scope_id=PROJECT_ID,
                repository_id=REPO_ID,
                project_id=PROJECT_ID,
                title="Safe title",
                body=_body(),
                producer={secret: "value"},
            )


def test_idempotency_keys_reject_semantically_different_replays(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        lesson = _lesson(store, status="candidate")

        with pytest.raises(ValueError, match="another lesson"):
            store.create_lesson(
                item_id="lesson_different",
                idempotency_key="create:lesson_test",
                principal_id="local-owner",
                scope_type="project",
                scope_id=PROJECT_ID,
                repository_id=REPO_ID,
                project_id=PROJECT_ID,
                title="Lesson lesson_test",
                summary="A bounded, manually curated lesson.",
                body=_body(),
                tags={"technology": ["sqlite"], "task_type": ["persistence"]},
                confidence=0.8,
                sensitivity="normal",
                egress_policy="same_provider_trust_domain",
                producer_trust_domain="provider:a",
                created_by="user",
                source_session_id="source-session",
                source_turn_id="source-turn",
                source_work_id="source-work",
                source_hash="a" * 64,
            )

        store.approve_lesson(
            lesson["id"],
            actor="user",
            reason="reviewed",
            idempotency_key="transition:lesson_test:active",
            transitioned_at=3.0,
        )
        # An exact replay is a no-op even though the lesson is already active.
        assert store.approve_lesson(
            lesson["id"],
            actor="user",
            reason="reviewed",
            idempotency_key="transition:lesson_test:active",
            transitioned_at=4.0,
        )["current_status"] == "active"
        with pytest.raises(ValueError, match="another transition"):
            store.approve_lesson(
                lesson["id"],
                actor="user",
                reason="different approval",
                idempotency_key="transition:lesson_test:active",
                transitioned_at=4.0,
            )

        result = _search(store)[0]
        retrieval_items = [
            {
                "item_id": lesson["id"],
                "item_revision": result["revision"]["revision"],
                "rank": 1,
                "score": result["score"],
                "match_reasons": result["match_reasons"],
            }
        ]
        store.record_retrieval(
            retrieval_id="retrieval_collision",
            idempotency_key="retrieval:collision",
            turn_id="turn-collision",
            work_id="work-collision",
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            task_signature_hash="d" * 64,
            provider_trust_domain="provider:a",
            items=retrieval_items,
        )
        changed_items = [dict(retrieval_items[0], score=result["score"] + 1.0)]
        with pytest.raises(ValueError, match="different items"):
            store.record_retrieval(
                retrieval_id="retrieval_collision",
                idempotency_key="retrieval:collision",
                turn_id="turn-collision",
                work_id="work-collision",
                principal_id="local-owner",
                repository_id=REPO_ID,
                project_id=PROJECT_ID,
                task_signature_hash="d" * 64,
                provider_trust_domain="provider:a",
                items=changed_items,
            )

def test_mutations_reject_backdated_timestamps(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        _lesson(store, status="candidate")

        with pytest.raises(ValueError, match="transitioned_at must be newer"):
            store.approve_lesson("lesson_test", transitioned_at=2.0)

        store.approve_lesson("lesson_test", transitioned_at=3.0)
        with pytest.raises(ValueError, match="edited_at must be newer"):
            store.edit_lesson(
                "lesson_test",
                body=_body("A backdated mutation must not be accepted."),
                edited_at=2.5,
            )


def test_authorized_decision_requires_host_authority_and_records_influence(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        with pytest.raises(ValueError, match="trusted user authority"):
            store.create_authorized_decision(
                principal_id="local-owner",
                scope_type="project",
                scope_id=PROJECT_ID,
                repository_id=REPO_ID,
                project_id=PROJECT_ID,
                title="Trusted Decision",
                summary="User approved inline.",
                body=_decision_body("Use trusted authority for active decisions."),
                authority=_authority("b" * 64),
                source_hash="a" * 64,
            )

        decision = store.create_authorized_decision(
            principal_id="local-owner",
            scope_type="project",
            scope_id=PROJECT_ID,
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            item_id="decision_authorized",
            title="Trusted Decision",
            summary="User approved inline.",
            body=_decision_body("Use trusted authority for active decisions."),
            authority=_authority("a" * 64, approve=("decision_authorized",)),
            source_hash="a" * 64,
            created_at=1.0,
        )
        assert decision["current_status"] == "active"
        assert decision["revision"]["body"]["authority"] == "user"

        store.record_retrieval(
            retrieval_id="retrieval_influence",
            idempotency_key="retrieval:influence",
            turn_id="turn-influence",
            work_id="work_influence",
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            task_signature_hash="e" * 64,
            provider_trust_domain="provider:a",
            items=[{
                "item_id": "decision_authorized",
                "item_revision": 1,
                "rank": 1,
                "score": 0.9,
                "match_reasons": ("authority approved",),
            }],
        )
        store.record_influence_event(
            event_type="disclosed",
            item_id="decision_authorized",
            item_revision=1,
            retrieval_id="retrieval_influence",
            work_id="work_influence",
            event_id="event_influence",
        )
        replay = store.record_influence_event(
            event_type="disclosed",
            item_id="decision_authorized",
            item_revision=1,
            retrieval_id="retrieval_influence",
            work_id="work_influence",
            event_id="event_influence",
        )
        assert replay["id"] == "event_influence"
        events = store.list_influence_events(retrieval_id="retrieval_influence")
        disclosed = [
            event for event in events
            if event["event_type"] == "disclosed"
        ]
        assert len(disclosed) == 1
        assert disclosed[0]["event_type"] == "disclosed"




def test_retrieval_diagnostics_include_item_kind_status_and_title(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        lesson = _lesson(store, item_id="lesson_diagnostic")
        decision = _decision(store, item_id="decision_diagnostic")
        decision = store.activate_decision(
            decision["id"],
            authority=_authority("a" * 64),
            transitioned_at=3.0,
        )
        lesson_match = _search(store, query="SQLite", tags={})[0]
        decision_match = _search_decision(store, query="Decision Memory", tags={})[0]
        retrieval = store.record_retrieval(
            retrieval_id="retrieval_diagnostics",
            idempotency_key="retrieval:diagnostics",
            turn_id="turn-diagnostic",
            work_id="work-diagnostic",
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            task_signature_hash="f" * 64,
            provider_trust_domain="provider:a",
            items=[
                {
                    "item_id": lesson["id"],
                    "item_revision": lesson_match["revision"]["revision"],
                    "rank": 1,
                    "score": lesson_match["score"],
                    "match_reasons": ("lesson text match",),
                },
                {
                    "item_id": decision["id"],
                    "item_revision": decision_match["revision"]["revision"],
                    "rank": 2,
                    "score": decision_match["score"],
                    "match_reasons": ("decision text match",),
                },
            ],
        )

        assert retrieval["items"][0]["kind"] == "lesson"
        assert retrieval["items"][0]["status"] == "active"
        assert retrieval["items"][0]["title"] == "Lesson lesson_diagnostic"
        assert retrieval["items"][1]["kind"] == "decision"
        assert retrieval["items"][1]["status"] == "active"
        assert retrieval["items"][1]["title"] == "Decision decision_diagnostic"


def test_decision_relationship_links_cover_evidence_contradiction_and_derivation(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        claim = _decision(store, item_id="claim_decision")
        evidence = _decision(store, item_id="evidence_decision")
        contradiction = _decision(store, item_id="contradiction_decision")
        derived = _decision(store, item_id="derived_decision")

        store.add_experience_link(
            from_item_id=evidence["id"],
            from_revision=1,
            relation="evidence_for",
            to_item_id=claim["id"],
            to_revision=1,
            metadata={"note": "supports the claim"},
            event_id="event_evidence_for",
        )
        store.add_experience_link(
            from_item_id=claim["id"],
            from_revision=1,
            relation="contradicts",
            to_item_id=contradiction["id"],
            to_revision=1,
            metadata={"note": "conflicting approach"},
            event_id="event_contradicts",
        )
        store.add_experience_link(
            from_item_id=derived["id"],
            from_revision=1,
            relation="derived_from",
            to_item_id=claim["id"],
            to_revision=1,
            metadata={"note": "derived from claim"},
            event_id="event_derived_from",
        )

        assert store.list_links(item_id=claim["id"], relation="evidence_for", direction="in")[0]["from_item_id"] == evidence["id"]
        assert store.list_links(item_id=claim["id"], relation="contradicts", direction="out")[0]["to_item_id"] == contradiction["id"]
        assert store.list_links(item_id=claim["id"], relation="derived_from", direction="in")[0]["from_item_id"] == derived["id"]
        related = {item["decision"]["id"]: item["link"]["relation"] for item in store.related_decisions(item_id=claim["id"])}
        assert related[evidence["id"]] == "evidence_for"
        assert related[contradiction["id"]] == "contradicts"
        assert related[derived["id"]] == "derived_from"


def test_decision_links_and_migration_source_mappings_are_idempotent(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        first = _decision(store, item_id="decision_one")
        second = _decision(store, item_id="decision_two")
        link = store.add_experience_link(
            from_item_id=first["id"],
            from_revision=1,
            relation="supersedes",
            to_item_id=second["id"],
            to_revision=1,
            metadata={"reason": "test relation"},
            event_id="event_relation",
        )
        replay = store.add_experience_link(
            from_item_id=first["id"],
            from_revision=1,
            relation="supersedes",
            to_item_id=second["id"],
            to_revision=1,
            metadata={"reason": "test relation"},
            event_id="event_relation",
        )
        assert link["replayed"] is False
        assert replay["replayed"] is True
        related = store.related_decisions(item_id=second["id"])
        assert {item["decision"]["id"] for item in related} == {"decision_one"}

        mapping = store.record_migration_source(
            source_system="memory_consolidation",
            source_store_hash="c" * 64,
            source_item_id="legacy_decision",
            source_revision=1,
            target_item_id="decision_one",
            target_revision=1,
            disposition="imported_candidate",
            reason_code="candidate_imported",
        )
        replay_mapping = store.record_migration_source(
            source_system="memory_consolidation",
            source_store_hash="c" * 64,
            source_item_id="legacy_decision",
            source_revision=1,
            target_item_id="decision_one",
            target_revision=1,
            disposition="imported_candidate",
            reason_code="candidate_imported",
        )
        assert replay_mapping["imported_at"] >= mapping["imported_at"]
        mappings = store.list_migration_sources(
            source_system="memory_consolidation",
            source_store_hash="c" * 64,
        )
        assert len(mappings) == 1
        assert mappings[0]["target_item_id"] == "decision_one"


def test_schema_status_rebuild_prune_and_doctor_remain_metadata_only(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _policy(store)
        lesson = _lesson(store, item_id="lesson_diagnostics")
        decision = _decision(store, item_id="decision_diagnostics")

        store.record_migration_source(
            source_system="memory_consolidation",
            source_store_hash="d" * 64,
            source_item_id="legacy_candidate",
            source_revision=1,
            target_item_id=None,
            target_revision=None,
            disposition="skipped",
            reason_code="needs_review",
            imported_at=1.0,
        )
        old_retrieval = store.record_retrieval(
            retrieval_id="retrieval_old",
            idempotency_key="retrieval:old",
            turn_id="turn-old",
            work_id="work-old",
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            task_signature_hash="e" * 64,
            provider_trust_domain="provider:a",
            created_at=1.0,
        )
        new_retrieval = store.record_retrieval(
            retrieval_id="retrieval_new",
            idempotency_key="retrieval:new",
            turn_id="turn-new",
            work_id="work-new",
            principal_id="local-owner",
            repository_id=REPO_ID,
            project_id=PROJECT_ID,
            task_signature_hash="f" * 64,
            provider_trust_domain="provider:a",
            created_at=10.0,
        )

        status = store.schema_status()
        assert status["schema_current"] is True
        assert status["revision_count"] == 2
        assert status["search_content_count"] == 2
        assert status["counts_by_kind_status"]["lesson.active"] == 1
        assert status["counts_by_kind_status"]["decision.candidate"] == 1
        assert status["latest_retrieval"]["id"] == new_retrieval["id"]
        assert status["migration_sources"][0]["source_system"] == "memory_consolidation"
        status_text = repr(status)
        assert lesson["revision"]["body"]["guidance"] not in status_text
        assert decision["revision"]["body"]["statement"] not in status_text

        plan = store.diagnostic_prune_plan(now=20.0, max_age_days=1, max_retrievals=0, max_events=0)
        assert plan == {
            "dry_run": True,
            "retrievals_to_remove": 2,
            "events_to_remove": 2,
            "max_age_days": 1,
            "max_retrievals": 0,
            "max_events": 0,
        }
        assert store.get_retrieval(old_retrieval["id"]) is not None

        rebuilt = store.rebuild_search_index()
        assert rebuilt == {
            "rebuilt": True,
            "fts_enabled": True,
            "fts_rebuild_version": "2",
        }

        report = store.doctor(repository_root=tmp_path, now=20.0)
        assert report["ok"] is True
        assert report["schema_current"] is True
        assert report["fts"]["consistent"] is True
        assert report["foreign_key_violations"] == []
        assert report["orphan_current_revisions"] == []
        assert report["supersession_cycles"] == []
        assert report["active_decision_authority_violations"] == []
        assert report["policy_anchor_violations"] == []
        report_text = repr(report)
        assert lesson["revision"]["body"]["guidance"] not in report_text
        assert decision["revision"]["body"]["statement"] not in report_text

        pruned = store.prune_diagnostics(now=20.0, max_age_days=1, max_retrievals=0, max_events=0)
        assert pruned == {"retrievals_removed": 2, "events_removed": 2}
        assert store.get_retrieval(old_retrieval["id"]) is None
        assert store.get_retrieval(new_retrieval["id"]) is None

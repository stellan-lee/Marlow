from __future__ import annotations

import statistics
import time
from pathlib import Path

from agent.experience.authority import DecisionTurnAuthority
from agent.experience.models import EgressPolicy, ScopePolicy
from agent.experience.store import ExperienceStore


def _policy() -> ScopePolicy:
    return ScopePolicy(
        principal_id="local-owner",
        repository_id="repo",
        project_id="project",
        project_root_rel=".",
        capture_allowed=True,
        recall_allowed=True,
        injection_allowed=True,
        reflection_allowed=False,
        max_egress_policy=EgressPolicy.EXPLICIT_ANY_PROVIDER,
        updated_at=1.0,
    )


def _decision_body(statement: str) -> dict[str, str]:
    return {
        "statement": statement,
        "rationale": "Performance fixture decision rationale.",
        "source_type": "agent_proposal",
        "authority": "unapproved",
        "effective_at": 1.0,
    }


def _lesson_body(guidance: str) -> dict[str, str | None]:
    return {
        "applies_when": "Performance fixture retrieval",
        "does_not_apply_when": None,
        "guidance": guidance,
        "rationale": "Performance fixture lesson rationale.",
    }


def test_local_retrieval_p95_at_target_scale(tmp_path: Path) -> None:
    """Validate the design target: 5,000 records with 10,000 tags."""

    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        store.upsert_scope_policy(**_policy().to_dict())

        item_ids: list[str] = []

        for index in range(1_000):
            decision = store.create_decision(
                principal_id="local-owner",
                scope_type="project",
                scope_id="project",
                repository_id="repo",
                project_id="project",
                title=f"Decision {index}",
                summary="Performance fixture.",
                body=_decision_body(f"Use decision {index} for benchmark retrieval."),
                tags={
                    "component": ["benchmark"],
                    "failure": [f"decision-{index % 100}"],
                },
                created_by="agent",
            )
            item_ids.append(decision["id"])

        for index in range(2_000):
            lesson = store.create_lesson(
                principal_id="local-owner",
                scope_type="project",
                scope_id="project",
                repository_id="repo",
                project_id="project",
                title=f"Lesson {index}",
                summary="Performance fixture.",
                body=_lesson_body(f"Use lesson {index} for benchmark retrieval."),
                egress_policy="explicit_any_provider",
                tags={
                    "component": ["benchmark"],
                    "failure": [f"lesson-{index % 100}"],
                },
            )
            item_ids.append(lesson["id"])
            store.approve_lesson(lesson["id"])

        for index in range(2_000):
            lesson = store.create_lesson(
                principal_id="local-owner",
                scope_type="project",
                scope_id="project",
                repository_id="repo",
                project_id="project",
                title=f"Terminal {index}",
                summary="Performance fixture.",
                body=_lesson_body(f"Do not inject terminal lesson {index}."),
                egress_policy="explicit_any_provider",
                tags={
                    "component": ["benchmark"],
                    "failure": [f"terminal-{index % 100}"],
                },
            )
            item_ids.append(lesson["id"])
        for index in range(1, len(item_ids)):
            store.add_experience_link(
                from_item_id=item_ids[index - 1],
                from_revision=1,
                relation="evidence_for",
                to_item_id=item_ids[index],
                to_revision=1,
                metadata={"note": "performance fixture"},
            )
        store.add_experience_link(
            from_item_id=item_ids[0],
            from_revision=1,
            relation="continues",
            to_item_id=item_ids[1],
            to_revision=1,
            metadata={"note": "performance fixture"},
        )

        assert store.diagnostic_stats()["tag_count"] == 10_000
        assert store.diagnostic_stats()["link_count"] == 5_000

        latencies_ms = []
        for index in range(100):
            start = time.perf_counter()
            rows = store.search_lessons(
                principal_id="local-owner",
                scope_type="project",
                scope_id="project",
                repository_id="repo",
                project_id="project",
                provider_trust_domain="provider:a",
                provider_is_local=False,
                query="",
                tags={"failure": [f"lesson-{index % 100}"]},
                limit=3,
            )
            latencies_ms.append((time.perf_counter() - start) * 1000)

        p95 = sorted(latencies_ms)[94]
        assert len(rows) > 0
        assert p95 < 50.0
        assert statistics.median(latencies_ms) < 50.0

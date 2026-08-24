"""Deterministic paired evaluation harness for Marlow Work Experience.

This is a fixture-based behavioral gate for the long-term-memory design. It
does not call live models or external providers; it exercises the real
ExperienceStore + ExperienceService retrieval, authorization, formatting, and
diagnostic-recording paths against seeded canonical Decisions/Lessons.

Output is metadata-only: task labels, counts, pass/fail criteria, and latency
numbers. It never prints item statements, rationales, lesson guidance, queries,
or formatted memory context.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.experience.authority import DecisionTurnAuthority
from agent.experience.models import (
    EgressPolicy,
    RetrievalQuery,
    ScopeRef,
    ScopeType,
)
from agent.experience.service import ExperienceService
from agent.experience.store import ExperienceStore


LOCAL_OWNER = "local-owner"
REPO = "eval_repo"
PROJECT_A = "project_a"
PROJECT_B = "project_b"
SECRET = "EVAL_SECRET_SHOULD_NOT_LEAK"


@dataclass(frozen=True, slots=True)
class TaskFamily:
    label: str
    kind: str
    project_id: str
    expected_items: tuple[str, ...]
    query_text: str
    query_tags: tuple[str, ...]
    seed: Callable[[ExperienceStore, Path], None]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _authority(
    source_hash: str,
    *,
    approve: tuple[str, ...] = (),
    supersede: tuple[str, ...] = (),
    revoke: tuple[str, ...] = (),
) -> DecisionTurnAuthority:
    return DecisionTurnAuthority(
        source_turn_id="eval-turn",
        source_session_id="eval-session",
        raw_user_text_hash=source_hash,
        explicit_remember_grant=True,
        approved_item_ids=approve,
        supersede_target_ids=supersede,
        revoke_target_ids=revoke,
    )


def _scope(project_id: str) -> ScopeRef:
    return ScopeRef(
        principal_id=LOCAL_OWNER,
        scope_type=ScopeType.PROJECT,
        scope_id=project_id,
        repository_id=REPO,
        project_id=project_id,
    )


def _policy(store: ExperienceStore, project_id: str, *, egress: EgressPolicy) -> None:
    store.upsert_scope_policy(
        principal_id=LOCAL_OWNER,
        repository_id=REPO,
        project_id=project_id,
        project_root_rel=".",
        capture_allowed=True,
        recall_allowed=True,
        injection_allowed=True,
        reflection_allowed=False,
        max_egress_policy=egress,
        updated_at=1.0,
    )


def _decision_body(
    statement: str,
    *,
    authority: str = "unapproved",
    source_type: str = "agent_proposal",
    policy_anchor_path: str | None = None,
    policy_anchor_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "statement": statement,
        "rationale": "Evaluation fixture rationale.",
        "source_type": source_type,
        "authority": authority,
        "effective_at": 1.0,
        "policy_anchor_path": policy_anchor_path,
        "policy_anchor_hash": policy_anchor_hash,
    }


def _create_active_decision(
    store: ExperienceStore,
    *,
    item_id: str,
    statement: str,
    project_id: str = PROJECT_A,
    tags: tuple[str, ...] = ("eval",),
    source_hash: str | None = None,
    repository_root: Path | None = None,
) -> None:
    digest = source_hash or _hash(item_id)
    created = store.create_decision(
        item_id=item_id,
        idempotency_key=f"eval-create:{item_id}",
        principal_id=LOCAL_OWNER,
        scope_type="project",
        scope_id=project_id,
        repository_id=REPO,
        project_id=project_id,
        title=f"Evaluation Decision {item_id}",
        summary="Seeded active Decision for paired evaluation.",
        body=_decision_body(statement),
        tags={"component": ["memory_eval"], "failure": list(tags)},
        sensitivity="normal",
        egress_policy="explicit_any_provider",
        producer_trust_domain="provider:eval",
        created_by="agent",
        source_hash=digest,
        created_at=2.0,
    )
    store.activate_decision(
        created["id"],
        authority=_authority(digest, approve=(created["id"],)),
        repository_root=repository_root,
        repository_id=REPO,
        transitioned_at=3.0,
    )


def _create_candidate_decision(
    store: ExperienceStore,
    *,
    item_id: str,
    statement: str,
    project_id: str = PROJECT_A,
    tags: tuple[str, ...] = ("eval",),
) -> None:
    store.create_decision(
        item_id=item_id,
        idempotency_key=f"eval-create:{item_id}",
        principal_id=LOCAL_OWNER,
        scope_type="project",
        scope_id=project_id,
        repository_id=REPO,
        project_id=project_id,
        title=f"Evaluation Candidate {item_id}",
        summary="Candidate Decision must not inject.",
        body=_decision_body(statement),
        tags={"component": ["memory_eval"], "failure": list(tags)},
        sensitivity="normal",
        egress_policy="explicit_any_provider",
        producer_trust_domain="provider:eval",
        created_by="agent",
        source_hash=_hash(item_id),
        created_at=2.0,
    )


def _create_active_lesson(
    store: ExperienceStore,
    *,
    item_id: str,
    guidance: str,
    project_id: str = PROJECT_A,
    tags: tuple[str, ...] = ("eval",),
    sensitivity: str = "normal",
    egress_policy: str = "explicit_any_provider",
) -> None:
    store.create_lesson(
        item_id=item_id,
        idempotency_key=f"eval-create:{item_id}",
        principal_id=LOCAL_OWNER,
        scope_type="project",
        scope_id=project_id,
        repository_id=REPO,
        project_id=project_id,
        title=f"Evaluation Lesson {item_id}",
        summary="Seeded active lesson for paired evaluation.",
        body={
            "applies_when": "paired evaluation fixture",
            "does_not_apply_when": None,
            "guidance": guidance,
            "rationale": "Evaluation fixture rationale.",
        },
        tags={"component": ["memory_eval"], "failure": list(tags)},
        confidence=0.9,
        sensitivity=sensitivity,
        egress_policy=egress_policy,
        producer_trust_domain="provider:eval",
        created_by="user",
        source_hash=_hash(item_id),
        created_at=2.0,
    )
    store.approve_lesson(item_id, transitioned_at=3.0)


def _query(scope: ScopeRef, *, text: str, tags: tuple[str, ...]) -> RetrievalQuery:
    return RetrievalQuery(
        scope=scope,
        query_text=text,
        provider_trust_domain="provider:eval",
        provider_is_local=False,
        technologies=("memory_eval",),
        failure_fingerprints=tags,
    )


def _generic_family(index: int) -> TaskFamily:
    label = f"generic_{index:02d}"
    tags = (f"generic-{index:02d}",)
    statement = f"Use eval decision {index:02d} for {label}."
    guidance = f"Use eval lesson {index:02d} for {label}."
    query = f"Apply {label} memory_eval decision lesson."

    def seed(store: ExperienceStore, _root: Path) -> None:
        _create_active_decision(
            store,
            item_id=f"decision_{label}",
            statement=statement,
            tags=tags,
        )
        _create_active_lesson(
            store,
            item_id=f"lesson_{label}",
            guidance=guidance,
            tags=tags,
        )

    return TaskFamily(
        label=label,
        kind="memory_aided",
        project_id=PROJECT_A,
        expected_items=("decision_" + label, "lesson_" + label),
        query_text=query,
        query_tags=tags,
        seed=seed,
    )


def _named_families() -> list[TaskFamily]:
    def consistency(store: ExperienceStore, _root: Path) -> None:
        _create_active_decision(
            store,
            item_id="decision_consistency",
            statement="Use Decision Memory consistently for Marlow recall.",
            tags=("consistency",),
        )
        _create_active_lesson(
            store,
            item_id="lesson_consistency",
            guidance="Use Decision Memory consistently for Marlow recall.",
            tags=("consistency",),
        )

    def supersession(store: ExperienceStore, _root: Path) -> None:
        old = store.create_decision(
            item_id="decision_old_superseded",
            principal_id=LOCAL_OWNER,
            scope_type="project",
            scope_id=PROJECT_A,
            repository_id=REPO,
            project_id=PROJECT_A,
            title="Old superseded decision",
            summary="Old decision.",
            body=_decision_body("Old superseded guidance for Marlow recall."),
            tags={"component": ["memory_eval"], "failure": ["supersession"]},
            sensitivity="normal",
            egress_policy="explicit_any_provider",
            producer_trust_domain="provider:eval",
            created_by="agent",
            source_hash=_hash("old"),
            created_at=2.0,
        )
        store.activate_decision(
            old["id"],
            authority=_authority(_hash("old"), approve=(old["id"],)),
            repository_id=REPO,
            transitioned_at=3.0,
        )
        replacement = store.supersede_decision(
            old["id"],
            principal_id=LOCAL_OWNER,
            scope_type="project",
            scope_id=PROJECT_A,
            repository_id=REPO,
            project_id=PROJECT_A,
            item_id="decision_current_supersedes",
            title="Current superseding decision",
            summary="Current decision.",
            body=_decision_body("Use current superseding guidance for Marlow recall."),
            tags={"component": ["memory_eval"], "failure": ["supersession"]},
            egress_policy="explicit_any_provider",
            producer_trust_domain="provider:eval",
            authority=_authority(_hash("new"), supersede=(old["id"],)),
            transitioned_at=4.0,
        )
        assert replacement["id"] == "decision_current_supersedes"

    def current_override(store: ExperienceStore, _root: Path) -> None:
        _create_active_decision(
            store,
            item_id="decision_current_override",
            statement="Historical Decision Memory yields to current user instructions.",
            tags=("current-override",),
        )

    def policy_invalidation(store: ExperienceStore, root: Path) -> None:
        policy_file = root / "eval_policy.txt"
        policy_file.write_text("valid anchor", encoding="utf-8")
        digest = _hash(policy_file.read_bytes().decode("utf-8"))
        created = store.create_decision(
            item_id="decision_policy_anchor",
            principal_id=LOCAL_OWNER,
            scope_type="project",
            scope_id=PROJECT_A,
            repository_id=REPO,
            project_id=PROJECT_A,
            title="Repository policy anchor",
            summary="Policy anchored decision.",
            body=_decision_body(
                "Repository policy anchor must match live file bytes.",
                authority="repository_policy",
                source_type="repository_policy",
                policy_anchor_path="eval_policy.txt",
                policy_anchor_hash=digest,
            ),
            tags={"component": ["memory_eval"], "failure": ["policy-invalidation"]},
            sensitivity="normal",
            egress_policy="explicit_any_provider",
            producer_trust_domain="provider:eval",
            created_by="import",
            source_hash=digest,
            created_at=2.0,
        )
        store.activate_decision(
            created["id"],
            authority=_authority(digest),
            repository_root=root,
            repository_id=REPO,
            transitioned_at=3.0,
        )
        policy_file.write_text("changed anchor", encoding="utf-8")

    def scope_isolation(store: ExperienceStore, _root: Path) -> None:
        _create_active_decision(
            store,
            item_id="decision_scope_a",
            statement="Project A Decision must not leak into Project B.",
            project_id=PROJECT_A,
            tags=("scope-isolation",),
        )
        _create_active_decision(
            store,
            item_id="decision_scope_b",
            statement="Project B has its own Decision.",
            project_id=PROJECT_B,
            tags=("scope-isolation",),
        )

    def authority(store: ExperienceStore, _root: Path) -> None:
        _create_candidate_decision(
            store,
            item_id="decision_unapproved_candidate",
            statement="Candidate authority must remain inactive.",
            tags=("authority",),
        )

    def chinese_recall(store: ExperienceStore, _root: Path) -> None:
        _create_active_decision(
            store,
            item_id="decision_cjk_mixed",
            statement="记住 Marlow 长期记忆 决策 Redis API 限流.",
            tags=("chinese", "redis-api"),
        )
        _create_active_lesson(
            store,
            item_id="lesson_cjk_mixed",
            guidance="审批卡片 Redis API 限流 should stay bounded.",
            tags=("chinese", "redis-api"),
        )

    def non_use(store: ExperienceStore, _root: Path) -> None:
        _create_active_decision(
            store,
            item_id="decision_non_use",
            statement="Unrelated Decision Memory should not inject for Kubernetes manifests.",
            tags=(),
        )

    def stale_harm(store: ExperienceStore, _root: Path) -> None:
        active = store.create_decision(
            item_id="decision_review_required",
            principal_id=LOCAL_OWNER,
            scope_type="project",
            scope_id=PROJECT_A,
            repository_id=REPO,
            project_id=PROJECT_A,
            title="Review required decision",
            summary="Review required.",
            body=_decision_body("Review required guidance must not inject."),
            tags={"component": ["memory_eval"], "failure": ["stale-harm"]},
            sensitivity="normal",
            egress_policy="explicit_any_provider",
            producer_trust_domain="provider:eval",
            created_by="agent",
            source_hash=_hash("review"),
            created_at=2.0,
        )
        store.activate_decision(
            active["id"],
            authority=_authority(_hash("review"), approve=(active["id"],)),
            repository_id=REPO,
            transitioned_at=3.0,
        )
        store.edit_decision(
            active["id"],
            body=_decision_body("Review required guidance changed after review."),
            edit_reason="fixture review required",
            edited_at=4.0,
        )

    def external_conflict(store: ExperienceStore, _root: Path) -> None:
        _create_active_decision(
            store,
            item_id="decision_external_conflict",
            statement="Canonical Decision Memory outranks unverified external recollection.",
            tags=("external-conflict",),
        )

    def secret_egress(store: ExperienceStore, _root: Path) -> None:
        _create_active_lesson(
            store,
            item_id="lesson_local_only_secret",
            guidance=f"Local-only fixture {SECRET} must not leave local provider.",
            tags=("secret-egress",),
            sensitivity="local_only",
            egress_policy="local_only",
        )

    return [
        TaskFamily(
            "consistency",
            "memory_aided",
            PROJECT_A,
            ("decision_consistency", "lesson_consistency"),
            "",
            ("consistency",),
            consistency,
        ),
        TaskFamily(
            "supersession",
            "memory_aided",
            PROJECT_A,
            ("decision_current_supersedes",),
            "",
            ("supersession",),
            supersession,
        ),
        TaskFamily(
            "current_override",
            "current_override",
            PROJECT_A,
            ("decision_current_override",),
            "",
            ("current-override",),
            current_override,
        ),
        TaskFamily(
            "policy_invalidation",
            "policy_invalidation",
            PROJECT_A,
            ("decision_policy_anchor",),
            "",
            ("policy-invalidation",),
            policy_invalidation,
        ),
        TaskFamily(
            "scope_isolation",
            "scope_isolation",
            PROJECT_B,
            ("decision_scope_b",),
            "",
            ("scope-isolation",),
            scope_isolation,
        ),
        TaskFamily(
            "authority",
            "authority",
            PROJECT_A,
            (),
            "",
            ("authority",),
            authority,
        ),
        TaskFamily(
            "chinese_recall",
            "memory_aided",
            PROJECT_A,
            ("decision_cjk_mixed", "lesson_cjk_mixed"),
            "审批卡片 Redis API 限流 memory_eval.",
            ("chinese", "redis-api"),
            chinese_recall,
        ),
        TaskFamily(
            "non_use",
            "routine_control",
            PROJECT_A,
            (),
            "README color work.",
            ("non-use",),
            non_use,
        ),
        TaskFamily(
            "stale_harm",
            "stale_harm",
            PROJECT_A,
            (),
            "",
            ("stale-harm",),
            stale_harm,
        ),
        TaskFamily(
            "external_conflict",
            "memory_aided",
            PROJECT_A,
            ("decision_external_conflict",),
            "",
            ("external-conflict",),
            external_conflict,
        ),
        TaskFamily(
            "secret_egress",
            "secret_egress",
            PROJECT_A,
            (),
            "",
            ("secret-egress",),
            secret_egress,
        ),
    ]


def _families(count: int) -> list[TaskFamily]:
    named = _named_families()
    generic = [_generic_family(index) for index in range(max(0, count - len(named)))]
    return [*named, *generic][:count]


def _run_condition(
    *,
    service: ExperienceService,
    repository_root: Path,
    query: RetrievalQuery,
    condition: str,
    turn_id: str,
    work_id: str,
    max_context_chars: int,
) -> tuple[dict[str, Any], str, float]:
    if condition == "baseline":
        return {}, "", 0.0
    start = time.perf_counter()
    result = service.retrieve_decisions_and_lessons(
        query,
        turn_id=turn_id,
        work_id=work_id,
        max_decisions=2,
        max_lessons=1,
        repository_root=repository_root,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    context = (
        service.format_combined_context(result, max_chars=max_context_chars)
        if condition == "assist"
        else ""
    )
    return {
        "item_ids": [item.item_id for item in (*result.decisions, *result.lessons)],
        "decision_ids": [item.item_id for item in result.decisions],
        "lesson_ids": [item.item_id for item in result.lessons],
    }, context, elapsed_ms


def _contains_all(context: str, expected: tuple[str, ...]) -> bool:
    return all(item_id in context for item_id in expected)


def _contains_none(context: str, forbidden: tuple[str, ...]) -> bool:
    return all(item_id not in context for item_id in forbidden)


def _evaluate_family(
    family: TaskFamily,
    *,
    service: ExperienceService,
    repository_root: Path,
    max_context_chars: int,
) -> dict[str, Any]:
    query = _query(_scope(family.project_id), text=family.query_text, tags=family.query_tags)
    results: dict[str, Any] = {}
    contexts: dict[str, str] = {}
    latencies_ms: list[float] = []
    for condition in ("baseline", "shadow", "assist"):
        result, context, latency = _run_condition(
            service=service,
            repository_root=repository_root,
            query=query,
            condition=condition,
            turn_id=f"eval-{family.label}-{condition}",
            work_id=f"eval-work-{family.label}",
            max_context_chars=max_context_chars,
        )
        results[condition] = result
        contexts[condition] = context
        if condition == "assist":
            latencies_ms.append(latency)

    expected = set(family.expected_items)
    assist_context = contexts["assist"]
    assist_ids = set(results["assist"].get("item_ids", []))
    baseline_ids = set(results["baseline"].get("item_ids", []))
    assist_decision_ids = results["assist"].get("decision_ids", [])
    if family.kind == "memory_aided":
        assist_correct = expected.issubset(assist_ids)
        baseline_correct = expected.issubset(baseline_ids)
        paired_success = int(assist_correct and not baseline_correct)
        first_plan_correct = bool(assist_decision_ids) and assist_decision_ids[0] in expected
    elif family.kind == "current_override":
        assist_correct = expected.issubset(assist_ids) and "live instructions" in assist_context
        baseline_correct = False
        paired_success = int(assist_correct)
        first_plan_correct = assist_correct
    elif family.kind == "policy_invalidation":
        assist_correct = not assist_ids and assist_context == ""
        baseline_correct = False
        paired_success = 0
        first_plan_correct = True
    elif family.kind == "scope_isolation":
        assist_correct = expected.issubset(assist_ids) and "decision_scope_a" not in assist_ids
        baseline_correct = False
        paired_success = int(assist_correct)
        first_plan_correct = bool(assist_decision_ids) and assist_decision_ids[0] in expected
    else:
        assist_correct = not assist_ids and assist_context == ""
        baseline_correct = True
        paired_success = int(assist_correct)
        first_plan_correct = True

    scope_leak = family.project_id == PROJECT_B and (
        "decision_scope_a" in assist_ids or "decision_scope_a" in assist_context
    )
    secret_leak = SECRET in json.dumps(results, ensure_ascii=False) or SECRET in assist_context
    stale_harm = family.kind in {"stale_harm", "policy_invalidation"} and bool(assist_ids)
    context_chars = len(assist_context)
    context_within_budget = context_chars <= max_context_chars
    live_policy_text = (
        "live instructions" in assist_context and expected.issubset(assist_ids)
    ) or family.kind == "policy_invalidation"

    return {
        "label": family.label,
        "kind": family.kind,
        "paired_success": paired_success,
        "assist_correct": assist_correct,
        "baseline_correct": baseline_correct,
        "first_plan_correct": first_plan_correct,
        "scope_leak": scope_leak,
        "secret_leak": secret_leak,
        "stale_harm": stale_harm,
        "context_chars": context_chars,
        "context_within_budget": context_within_budget,
        "retrieval_latency_ms": latencies_ms[0] if latencies_ms else 0.0,
        "live_policy_text_present": live_policy_text,
        "assist_item_ids": results["assist"].get("item_ids", []),
    }


def _active_authority_failures(store: ExperienceStore) -> int:
    failures = 0
    for decision in store.list_decisions(status="active"):
        authority = decision["revision"]["body"].get("authority")
        if authority not in {"user", "repository_policy"}:
            failures += 1
    return failures


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _summarize(rows: list[dict[str, Any]], *, family_count: int, latency_threshold_ms: float, max_context_chars: int) -> dict[str, Any]:
    memory_aided = [row for row in rows if row["kind"] == "memory_aided"]
    controls = [row for row in rows if row["kind"] in {"routine_control", "authority", "stale_harm", "secret_egress"}]
    assist_tasks = [row for row in rows if row["kind"] != "routine_control"]
    paired_gain = sum(row["paired_success"] for row in memory_aided) / max(1, len(memory_aided))
    routine_success = sum(row["assist_correct"] for row in controls) / max(1, len(controls))
    stale_harm_rate = sum(row["stale_harm"] for row in assist_tasks) / max(1, len(assist_tasks))
    scope_leaks = sum(row["scope_leak"] for row in rows)
    secret_leaks = sum(row["secret_leak"] for row in rows)
    first_plan_correct = sum(row["first_plan_correct"] for row in memory_aided) / max(1, len(memory_aided))
    latencies = [row["retrieval_latency_ms"] for row in rows if row["retrieval_latency_ms"] > 0]
    context_chars = [row["context_chars"] for row in rows]
    criteria = {
        "paired_success_at_least_15pp": paired_gain >= 0.15,
        "routine_control_noninferiority": routine_success >= 0.95,
        "harmful_stale_below_5pct": stale_harm_rate <= 0.05,
        "zero_secret_egress_leaks": secret_leaks == 0,
        "zero_cross_scope_retrievals": scope_leaks == 0,
        "active_decision_precision_at_least_90pct": first_plan_correct >= 0.90,
        "superseded_revoked_review_required_injection_rate_zero": stale_harm_rate == 0,
        "p95_local_retrieval_below_50ms": _percentile(latencies, 0.95) <= latency_threshold_ms,
        "median_context_within_budget": (statistics.median(context_chars) if context_chars else 0.0) <= max_context_chars,
        "no_candidate_activation_without_valid_authority": True,
        "all_families_covered": family_count >= 20,
    }
    failures = [name for name, passed in criteria.items() if not passed]
    return {
        "family_count": family_count,
        "memory_aided_families": len(memory_aided),
        "routine_control_families": len(controls),
        "paired_success_rate": paired_gain,
        "routine_success_rate": routine_success,
        "first_plan_correctness": first_plan_correct,
        "stale_harm_rate": stale_harm_rate,
        "scope_leaks": scope_leaks,
        "secret_leaks": secret_leaks,
        "retrieval_latency_p95_ms": _percentile(latencies, 0.95),
        "median_context_chars": statistics.median(context_chars) if context_chars else 0.0,
        "criteria": criteria,
        "failures": failures,
        "pass": not failures,
        "broad_assist_rollout_recommendation": "fixture_gate_pass_live_paired_evaluation_still_required" if not failures else "blocked",
    }


def run(*, family_count: int = 24, max_context_chars: int = 1_500, latency_threshold_ms: float = 50.0) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="marlow-eval-") as tmp:
        root = Path(tmp)
        db_path = (root / "state.db").resolve()
        with ExperienceStore(db_path) as store:
            _policy(store, PROJECT_A, egress=EgressPolicy.EXPLICIT_ANY_PROVIDER)
            _policy(store, PROJECT_B, egress=EgressPolicy.EXPLICIT_ANY_PROVIDER)
            for family in _families(family_count):
                family.seed(store, root)
            service = ExperienceService(store, max_context_chars=max_context_chars)
            rows = [
                _evaluate_family(
                    family,
                    service=service,
                    repository_root=root,
                    max_context_chars=max_context_chars,
                )
                for family in _families(family_count)
            ]
            authority_failures = _active_authority_failures(store)
            summary = _summarize(
                rows,
                family_count=family_count,
                latency_threshold_ms=latency_threshold_ms,
                max_context_chars=max_context_chars,
            )
            summary["active_authority_failures"] = authority_failures
            summary["criteria"]["no_candidate_activation_without_valid_authority"] = authority_failures == 0
            summary["pass"] = not summary["failures"] and authority_failures == 0
            summary["family_rows"] = [
                {
                    "label": row["label"],
                    "kind": row["kind"],
                    "paired_success": row["paired_success"],
                    "assist_correct": row["assist_correct"],
                    "first_plan_correct": row["first_plan_correct"],
                    "scope_leak": row["scope_leak"],
                    "secret_leak": row["secret_leak"],
                    "stale_harm": row["stale_harm"],
                    "context_chars": row["context_chars"],
                    "context_within_budget": row["context_within_budget"],
                    "retrieval_latency_ms": row["retrieval_latency_ms"],
                    "live_policy_text_present": row["live_policy_text_present"],
                    "assist_item_ids": row["assist_item_ids"],
                }
                for row in rows
            ]
            return summary


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic Work Experience paired evaluation.")
    parser.add_argument("--families", type=int, default=24, help="Number of fixture task families (20-30 recommended).")
    parser.add_argument("--max-context-chars", type=int, default=1_500, help="Context budget per retrieval.")
    parser.add_argument("--latency-threshold-ms", type=float, default=50.0, help="P95 retrieval latency threshold.")
    parser.add_argument("--output", choices=("json", "text"), default="text", help="Output format.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if not 20 <= args.families <= 30:
        raise SystemExit("--families must be between 20 and 30")
    summary = run(
        family_count=args.families,
        max_context_chars=args.max_context_chars,
        latency_threshold_ms=args.latency_threshold_ms,
    )
    if args.output == "json":
        print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print("Work Experience paired evaluation")
        print(f"  families: {summary['family_count']}")
        print(f"  pass: {summary['pass']}")
        print(f"  paired_success_rate: {summary['paired_success_rate']:.3f}")
        print(f"  routine_success_rate: {summary['routine_success_rate']:.3f}")
        print(f"  first_plan_correctness: {summary['first_plan_correctness']:.3f}")
        print(f"  stale_harm_rate: {summary['stale_harm_rate']:.3f}")
        print(f"  scope_leaks: {summary['scope_leaks']}")
        print(f"  secret_leaks: {summary['secret_leaks']}")
        print(f"  retrieval_latency_p95_ms: {summary['retrieval_latency_p95_ms']:.3f}")
        if summary["failures"]:
            print("  failures: " + ", ".join(summary["failures"]))
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

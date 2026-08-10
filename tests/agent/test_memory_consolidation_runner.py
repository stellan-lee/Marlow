from datetime import datetime, timedelta, timezone

from agent.memory_consolidation_runner import (
    CandidateExtractor,
    ConsolidationMetrics,
    ConsolidationTracer,
    ConsolidationPlanner,
    ConsolidationRunner,
    ConsolidationSchedule,
    EvidenceEvent,
    LlmCandidateExtractor,
    LlmConsolidationPlanner,
    MemoryConsolidationStoreRepository,
    WeeklyVerificationRunner,
    append_conversation_evidence,
)
from agent.memory_consolidation import MemoryConsolidationStore


class FakeRepository:
    def __init__(self, events=(), matches=None):
        self.events = list(events)
        self.matches = matches or {}
        self.created = []
        self.committed = []
        self.succeeded = []
        self.failed = []

    def create_consolidation_run(self, previous, cutoff, *, dry_run):
        self.created.append((previous, cutoff, dry_run))
        return {"id": "run-1"}

    def load_evidence(self, previous, cutoff):
        return self.events

    def find_relevant_memories(self, candidate):
        return self.matches.get(candidate.content, [])

    def commit_consolidation(self, run, operations, cutoff):
        self.committed.append((run, tuple(operations), cutoff))

    def mark_consolidation_succeeded(self, run, *, dry_run):
        self.succeeded.append((run, dry_run))

    def mark_consolidation_failed(self, run, reason):
        self.failed.append((run, reason))


def test_extractor_is_deterministic_and_requires_evidence():
    event = EvidenceEvent("evt-1", "Use American English", origin="explicit")
    first = CandidateExtractor().extract([event])
    second = CandidateExtractor().extract([event])
    assert first == second
    assert first[0].evidence_ids == ("evt-1",)
    assert first[0].confidence == 1.0


def test_llm_extractor_enforces_provenance_and_scope_with_mocked_structured_call():
    calls = []

    def structured_call(**kwargs):
        calls.append(kwargs)
        return {"parsed": {"candidates": [
            {"content": "Use American English", "evidence_ids": ["evt-1"], "origin": "explicit", "confidence": 0.98},
            # Unknown and cross-scope IDs are rejected deterministically.
            {"content": "forged", "evidence_ids": ["missing"], "origin": "inferred", "confidence": 1.0},
        ]}}

    event = EvidenceEvent("evt-1", "Use American English", scope_type="user", scope_id="u1", origin="explicit")
    candidates = LlmCandidateExtractor(llm_call=structured_call).extract([event])
    assert len(candidates) == 1
    assert candidates[0].scope_type == "user"
    assert candidates[0].scope_id == "u1"
    assert candidates[0].evidence_ids == ("evt-1",)
    assert candidates[0].id.startswith("candidate_")
    assert calls and calls[0]["json_schema"]["required"] == ["candidates"]


def test_llm_extractor_bounded_failure_degrades_to_empty_noop():
    calls = []

    def broken(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("provider unavailable")

    result = LlmCandidateExtractor(llm_call=broken, max_retries=2).extract([EvidenceEvent("evt-1", "fact")])
    assert result == []
    assert len(calls) == 3


def test_llm_planner_only_targets_known_matches_and_preserves_candidate_id():
    def structured_call(**kwargs):
        return {"parsed": {
            "operation": "REINFORCE", "candidate_id": "model-forged", "target_memory_ids": ["mem-1"],
            "result": {}, "confidence": 0.9, "reason": "confirmed",
        }}

    candidate = CandidateExtractor().extract([EvidenceEvent("evt-1", "fact")])[0]
    planned = LlmConsolidationPlanner(llm_call=structured_call).plan(candidate, [{"id": "mem-1", "content": "fact"}])
    assert planned.operation == "REINFORCE"
    assert planned.candidate_id == candidate.id
    assert planned.target_memory_ids == ("mem-1",)


def test_llm_planner_unknown_target_is_noop():
    def structured_call(**kwargs):
        return {"parsed": {
            "operation": "ARCHIVE", "candidate_id": "x", "target_memory_ids": ["not-allowed"],
            "result": {}, "confidence": 0.9, "reason": "archive",
        }}

    candidate = CandidateExtractor().extract([EvidenceEvent("evt-1", "fact")])[0]
    planned = LlmConsolidationPlanner(llm_call=structured_call).plan(candidate, [{"id": "mem-1", "content": "fact"}])
    assert planned.operation == "NOOP"


def test_runner_defaults_to_disabled_and_does_not_create_run():
    repo = FakeRepository([{"id": "evt-1", "content": "fact"}])
    result = ConsolidationRunner(repo).run(cutoff_watermark=10)
    assert result.status == "disabled"
    assert not repo.created


def test_observe_phase_plans_but_does_not_commit():
    repo = FakeRepository([{"id": "evt-1", "content": "fact", "origin": "explicit"}])
    result = ConsolidationRunner(repo, enabled=True, dry_run=False, phase="observe").run(cutoff_watermark=10)
    assert result.status == "dry_run"
    assert result.operations[0].operation == "CREATE"
    assert not repo.committed
    assert repo.succeeded == [({"id": "run-1"}, True)]


def test_safe_phase_commits_and_exact_match_reinforces():
    repo = FakeRepository(
        [{"id": "evt-1", "content": "fact", "origin": "explicit"}],
        {"fact": [{"id": "mem-1", "content": "FACT"}]},
    )
    result = ConsolidationRunner(repo, enabled=True, dry_run=False, phase="safe").run(cutoff_watermark=10)
    assert result.status == "committed"
    assert result.operations[0].operation == "REINFORCE"
    assert len(repo.committed) == 1


def test_runner_marks_failure_and_does_not_advance_on_repository_error():
    class Broken(FakeRepository):
        def commit_consolidation(self, run, operations, cutoff):
            raise RuntimeError("db unavailable")

    repo = Broken([{"id": "evt-1", "content": "fact"}])
    result = ConsolidationRunner(repo, enabled=True, dry_run=False, phase="safe").run(cutoff_watermark=10)
    assert result.status == "failed"
    assert not repo.succeeded
    assert repo.failed and "db unavailable" in repo.failed[0][1]


def test_runner_records_guard_rejection_metric():
    class GuardRejected(FakeRepository):
        def commit_consolidation(self, run, operations, cutoff):
            raise ValueError("protected memory cannot be archived")

    metrics = ConsolidationMetrics()
    result = ConsolidationRunner(
        GuardRejected([{"id": "evt-1", "content": "fact"}]),
        enabled=True, dry_run=False, phase="safe", metrics=metrics,
    ).run(cutoff_watermark=10)
    assert result.status == "failed"
    assert metrics.snapshot()["memory_guard_rejections_total"] == 1


def test_schedule_is_default_off_and_daily_guard_prevents_duplicates():
    schedule = ConsolidationSchedule()
    assert not schedule.is_due()
    schedule = ConsolidationSchedule(enabled=True)
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    assert schedule.is_due(now=now)
    assert not schedule.is_due(now=now, last_run=now - timedelta(hours=1))
    assert schedule.is_due(now=now, last_run=now - timedelta(days=1))


def test_store_repository_commits_create_and_preserves_scope(tmp_path):
    path = (tmp_path / "state.db").resolve()
    with MemoryConsolidationStore(path) as store:
        event = store.append_evidence(scope_id="person_a", source_key="turn:1", content="Use vim.")
        repository = MemoryConsolidationStoreRepository(store, scope_id="person_a")
        result = ConsolidationRunner(repository, enabled=True, dry_run=False, phase="safe").run(
            previous_watermark=0, cutoff_watermark=1
        )
        assert result.status == "committed"
        assert store.cursor("person_a") == 1
        assert store.cursor("person_b") == 0
        assert event["event_id"] in result.candidates[0].evidence_ids


def test_identical_turns_with_distinct_turn_ids_are_preserved(tmp_path):
    path = (tmp_path / "state.db").resolve()
    first = append_conversation_evidence(
        scope_id="person_a", session_id="s1", turn_id=1,
        user_content="hello", assistant_content="hi", state_db_path=str(path),
    )
    second = append_conversation_evidence(
        scope_id="person_a", session_id="s1", turn_id=2,
        user_content="hello", assistant_content="hi", state_db_path=str(path),
    )
    assert first["event_id"] != second["event_id"]


def test_observe_plan_can_be_promoted_to_safe_commit(tmp_path):
    path = (tmp_path / "state.db").resolve()
    with MemoryConsolidationStore(path) as store:
        store.append_evidence(scope_id="person_a", source_key="turn:1", content="fact")
        repo = MemoryConsolidationStoreRepository(store, scope_id="person_a")
        observed = ConsolidationRunner(repo, enabled=True, dry_run=True, phase="observe").run(
            previous_watermark=0, cutoff_watermark=1,
        )
        committed = ConsolidationRunner(repo, enabled=True, dry_run=False, phase="safe").run(
            previous_watermark=0, cutoff_watermark=1,
        )
        assert observed.status == "dry_run"
        assert committed.status == "committed"
        assert store.cursor("person_a") == 1


def test_runner_emits_metrics_structured_log_and_spans(caplog):
    caplog.set_level("INFO")
    repo = FakeRepository([{"id": "evt-1", "content": "fact", "origin": "explicit"}])
    metrics = ConsolidationMetrics()
    tracer = ConsolidationTracer()
    result = ConsolidationRunner(repo, enabled=True, dry_run=False, phase="safe", metrics=metrics, tracer=tracer).run(cutoff_watermark=10)
    assert result.status == "committed"
    assert metrics.snapshot()["memory_consolidation_runs_total"] == 1
    assert metrics.snapshot()["memory_created_total"] == 1
    assert {span["name"] for span in tracer.spans} >= {"collect_evidence", "extract_candidates", "match_memories", "plan_operations", "commit"}
    assert "memory_consolidation_completed" in caplog.text


def test_weekly_verification_flags_low_confidence_without_mutating(tmp_path):
    path = (tmp_path / "state.db").resolve()
    with MemoryConsolidationStore(path) as store:
        event = store.append_evidence(scope_id="person_a", source_key="turn:1", content="I use vim.")
        store.commit(scope_id="person_a", operations=[{"type": "create", "candidate": {"kind": "preference", "claim": "The preferred editor is emacs", "evidence_event_ids": [event["event_id"]]}}], end_seq=1)
        repo = MemoryConsolidationStoreRepository(store, scope_id="person_a")
        result = WeeklyVerificationRunner(repo, threshold=0.9).run(scope_id="person_a")
        assert result.status == "observed"
        assert result.selected == result.verified == 1
        assert result.flagged == 1
        assert result.proposals[0].operation == "FLAG_CONFLICT"
        assert store.cursor("person_a") == 1


def test_weekly_verification_can_use_guarded_commit(tmp_path):
    path = (tmp_path / "state.db").resolve()
    with MemoryConsolidationStore(path) as store:
        event = store.append_evidence(scope_id="person_a", source_key="turn:1", content="I use vim.")
        store.commit(scope_id="person_a", operations=[{"type": "create", "candidate": {"kind": "preference", "claim": "The preferred editor is emacs", "evidence_event_ids": [event["event_id"]]}}], end_seq=1)
        repo = MemoryConsolidationStoreRepository(store, scope_id="person_a")
        result = WeeklyVerificationRunner(repo, threshold=0.9, apply=True).run(scope_id="person_a")
        assert result.status == "committed"
        # The proposal is persisted through the same guarded operation path;
        # no direct status mutation is performed by the verifier.
        import sqlite3
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT status FROM memory_items").fetchone()[0] == "conflicted"
        flags = store.conflict_flags(scope_id="person_a")
        assert len(flags) == 1
        store.resolve_conflict_flag(flags[0]["flag_id"], scope_id="person_a")
        assert store.conflict_flags(scope_id="person_a") == []

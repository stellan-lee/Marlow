from datetime import datetime, timedelta, timezone

from agent.memory_consolidation_runner import (
    CandidateExtractor,
    ConsolidationPlanner,
    ConsolidationRunner,
    ConsolidationSchedule,
    EvidenceEvent,
    MemoryConsolidationStoreRepository,
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

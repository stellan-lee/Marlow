from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.memory_consolidation import MemoryConsolidationStore, deterministic_key


def _candidate(event: str, claim: str = "The preferred editor is vim.") -> dict:
    return {"type": "create", "candidate": {"kind": "preference", "claim": claim, "evidence_event_ids": [event]}}


def test_scope_cursor_and_duplicate_source_delivery(tmp_path: Path) -> None:
    with MemoryConsolidationStore((tmp_path / "state.db").resolve()) as store:
        one = store.append_evidence(scope_id="person_a", source_key="turn:1", content="I use vim.", created_at=10)
        replay = store.append_evidence(scope_id="person_a", source_key="turn:1", content="ignored", created_at=11)
        two = store.append_evidence(scope_id="person_a", source_key="turn:2", content="I use vim again.", created_at=12)
        other = store.append_evidence(scope_id="person_b", source_key="turn:1", content="I use emacs.")
        assert replay["event_id"] == one["event_id"]
        assert [event["ingestion_seq"] for event in store.evidence_after_cursor("person_a")] == [1, 2]
        result = store.commit(scope_id="person_a", operations=[_candidate(one["event_id"])], end_seq=1)
        assert result["cursor"] == 1 and store.cursor("person_a") == 1
        assert [event["ingestion_seq"] for event in store.evidence_after_cursor("person_a")] == [2]
        assert store.cursor("person_b") == 0 and other["ingestion_seq"] == 1


def test_failed_run_is_recorded_without_advancing_cursor(tmp_path: Path) -> None:
    with MemoryConsolidationStore((tmp_path / "state.db").resolve()) as store:
        store.append_evidence(scope_id="person_a", source_key="turn:1", content="fact")
        first = store.record_failed_run(
            scope_id="person_a", run_id="failed-run", start_seq=0, end_seq=1, failed_at=10
        )
        replay = store.record_failed_run(
            scope_id="person_a", run_id="failed-run", start_seq=0, end_seq=1, failed_at=20
        )
        assert first["replayed"] is False
        assert replay["replayed"] is True
        assert store.cursor("person_a") == 0
        assert store.last_run_at("person_a") == 20


def test_typed_scopes_do_not_collide(tmp_path: Path) -> None:
    with MemoryConsolidationStore((tmp_path / "state.db").resolve()) as store:
        first = store.append_evidence(scope_type="a:b", scope_id="c", source_key="same", content="one")
        second = store.append_evidence(scope_type="a", scope_id="b:c", source_key="same", content="two")
        assert first["event_id"] != second["event_id"]


def test_low_confidence_inference_cannot_be_protected(tmp_path: Path) -> None:
    with MemoryConsolidationStore((tmp_path / "state.db").resolve()) as store:
        event = store.append_evidence(scope_id="person_a", source_key="turn:1", content="maybe")
        with pytest.raises(ValueError, match="low-confidence"):
            store.commit(
                scope_id="person_a",
                operations=[
                    {
                        "type": "create",
                        "candidate": {
                            "kind": "fact",
                            "claim": "maybe",
                            "origin": "inferred",
                            "confidence": 0.2,
                            "retention_class": "protected",
                            "evidence_event_ids": [event["event_id"]],
                        },
                    }
                ],
                end_seq=1,
            )


def test_deterministic_idempotent_commit_and_atomic_rollback(tmp_path: Path) -> None:
    path = (tmp_path / "state.db").resolve()
    with MemoryConsolidationStore(path) as store:
        event = store.append_evidence(scope_id="person_a", source_key="turn:1", content="I use vim.")
        op = _candidate(event["event_id"])
        first = store.commit(scope_id="person_a", operations=[op], end_seq=1, run_id="run_one")
        replay = store.commit(scope_id="person_a", operations=[op], end_seq=1, run_id="run_two")
        assert not first["replayed"] and replay["replayed"]
        assert replay["run_id"] == "run_one"
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM memory_items").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM memory_operations").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM memory_runs").fetchone()[0] == 1
        bad = _candidate("event_missing", "This must not persist.")
        with pytest.raises(ValueError, match="evidence does not belong"):
            store.commit(scope_id="person_a", operations=[bad], end_seq=1)
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM memory_items").fetchone()[0] == 1


def test_validation_and_deterministic_key_are_order_independent(tmp_path: Path) -> None:
    assert deterministic_key({"b": 2, "a": 1}) == deterministic_key({"a": 1, "b": 2})
    with MemoryConsolidationStore((tmp_path / "state.db").resolve()) as store:
        with pytest.raises(ValueError, match="invalid scope_id"):
            store.append_evidence(scope_id="bad scope", source_key="turn:1", content="x")
        event = store.append_evidence(scope_id="person_a", source_key="turn:1", content="x")
        with pytest.raises(ValueError, match="candidate requires"):
            store.commit(scope_id="person_a", operations=[{"type": "create", "candidate": {"claim": "x"}}], end_seq=1)
        with pytest.raises(ValueError, match="outside"):
            store.commit(scope_id="person_a", operations=[_candidate(event["event_id"])], end_seq=99)


def test_noop_window_advances_cursor_and_revisions_preserve_sources(tmp_path: Path) -> None:
    with MemoryConsolidationStore((tmp_path / "state.db").resolve()) as store:
        first = store.append_evidence(scope_id="person_a", source_key="turn:1", content="vim")
        second = store.append_evidence(scope_id="person_a", source_key="turn:2", content="vim again")
        created = store.commit(
            scope_id="person_a",
            operations=[_candidate(first["event_id"], "Use vim")],
            end_seq=1,
        )
        with sqlite3.connect((tmp_path / "state.db").resolve()) as conn:
            item_id = conn.execute("SELECT item_id FROM memory_items").fetchone()[0]
        store.commit(
            scope_id="person_a",
            operations=[
                {
                    "type": "revise",
                    "target_item_id": item_id,
                    "candidate": {
                        "kind": "preference",
                        "claim": "Use vim consistently",
                        "evidence_event_ids": [second["event_id"]],
                    },
                }
            ],
            end_seq=2,
        )
        assert created["cursor"] == 1
        with sqlite3.connect((tmp_path / "state.db").resolve()) as conn:
            evidence = conn.execute(
                "SELECT evidence_json FROM memory_revisions WHERE item_id=? ORDER BY revision DESC LIMIT 1",
                (item_id,),
            ).fetchone()[0]
            assert first["event_id"] in evidence and second["event_id"] in evidence
        store.commit(scope_id="person_a", operations=[], end_seq=2, run_id="noop-run")
        assert store.cursor("person_a") == 2


def test_index_outbox_delivery_is_idempotent_and_tracks_latest_revision(tmp_path: Path) -> None:
    path = (tmp_path / "state.db").resolve()
    seen = []
    with MemoryConsolidationStore(path) as store:
        first = store.append_evidence(scope_id="person_a", source_key="turn:1", content="vim")
        store.commit(scope_id="person_a", operations=[_candidate(first["event_id"], "Use vim")], end_seq=1)
        item_id = store.find_relevant_memories(scope_id="person_a", claim="Use vim")[0]["id"]
        second = store.append_evidence(scope_id="person_a", source_key="turn:2", content="vim consistently")
        store.commit(scope_id="person_a", operations=[{
            "type": "revise", "target_item_id": item_id,
            "candidate": {"kind": "preference", "claim": "Use vim consistently",
                           "evidence_event_ids": [second["event_id"]]},
        }], end_seq=2)
        result = store.consume_index_outbox(scope_id="person_a", indexer=seen.append)
        assert result == {"seen": 2, "delivered": 2, "failed": 0}
        assert seen[0]["operation_key"] != seen[1]["operation_key"]
        assert seen[-1]["current"]["revision"] == 2
        assert store.pending_index_outbox(scope_id="person_a") == []
        # A retry has no effects because both rows are acknowledged.
        assert store.consume_index_outbox(scope_id="person_a") == {"seen": 0, "delivered": 0, "failed": 0}


def test_indexer_failure_leaves_event_pending_and_rebuild_is_authoritative(tmp_path: Path) -> None:
    path = (tmp_path / "state.db").resolve()
    with MemoryConsolidationStore(path) as store:
        event = store.append_evidence(scope_id="person_a", source_key="turn:1", content="vim")
        store.commit(scope_id="person_a", operations=[_candidate(event["event_id"], "Use vim")], end_seq=1)
        assert store.consume_index_outbox(scope_id="person_a", indexer=lambda _: (_ for _ in ()).throw(RuntimeError("offline"))) == {
            "seen": 1, "delivered": 0, "failed": 1
        }
        assert len(store.pending_index_outbox(scope_id="person_a")) == 1
        assert store.rebuild_retrieval_index(scope_id="person_a") == {"indexed": 1}
        row = store._conn.execute("SELECT claim,revision,status FROM memory_retrieval_index").fetchone()
        assert tuple(row) == ("Use vim", 1, "active")


def test_relevant_memory_includes_original_supporting_evidence(tmp_path: Path) -> None:
    path = (tmp_path / "state.db").resolve()
    with MemoryConsolidationStore(path) as store:
        event = store.append_evidence(scope_id="person_a", source_key="turn:1", content="I use vim every day")
        store.commit(scope_id="person_a", operations=[_candidate(event["event_id"], "The preferred editor is vim")], end_seq=1)
        match = store.find_relevant_memories(scope_id="person_a", claim="preferred editor")[0]
        assert match["evidence"][0]["event_id"] == event["event_id"]
        assert match["evidence"][0]["content"] == "I use vim every day"


def test_store_hash_and_current_memories_support_migration_review(tmp_path: Path) -> None:
    with MemoryConsolidationStore((tmp_path / "state.db").resolve()) as store:
        active = store.append_evidence(scope_id="person_a", source_key="turn:1", content="Use Decision Memory.")
        conflicted = store.append_evidence(scope_id="person_a", source_key="turn:2", content="Conflict.")
        archived = store.append_evidence(scope_id="person_a", source_key="turn:3", content="Archived.")
        store.commit(
            scope_id="person_a",
            operations=[
                _candidate(active["event_id"]),
                {
                    "type": "conflict",
                    "candidate": {
                        "kind": "decision",
                        "claim": "Conflict",
                        "evidence_event_ids": [conflicted["event_id"]],
                    },
                },
                {
                    "type": "create",
                    "candidate": {
                        "kind": "decision",
                        "claim": "Archived",
                        "evidence_event_ids": [archived["event_id"]],
                    },
                },
            ],
            end_seq=3,
        )
        archived_item_id = store.find_relevant_memories(scope_id="person_a", claim="Archived")[0]["id"]
        store.commit(
            scope_id="person_a",
            operations=[
                {
                    "type": "archive",
                    "target_item_id": archived_item_id,
                    "candidate": {
                        "kind": "decision",
                        "claim": "Archived",
                        "evidence_event_ids": [archived["event_id"]],
                    },
                }
            ],
            end_seq=3,
        )
        active_rows = store.list_current_memories(scope_id="person_a")
        archived_rows = store.list_current_memories(scope_id="person_a", include_archived=True)
        assert [row["status"] for row in active_rows] == ["conflicted", "active"]
        assert {row["status"] for row in archived_rows} == {"active", "conflicted", "archived"}
        assert len(store.store_hash(scope_id="person_a")) == 64

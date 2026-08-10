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

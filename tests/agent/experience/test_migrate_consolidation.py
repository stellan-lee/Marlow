from __future__ import annotations

import sqlite3
from pathlib import Path

from agent.experience.migrate_consolidation import apply_migration, plan_migration
from agent.experience.store import ExperienceStore


def _legacy_consolidation(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE memory_items (
            item_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            retention_class TEXT NOT NULL,
            pinned INTEGER NOT NULL,
            origin TEXT NOT NULL,
            confidence REAL NOT NULL,
            current_revision INTEGER NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE memory_revisions (
            item_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            claim TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            candidate_key TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (item_id, revision)
        );
        CREATE TABLE memory_events (
            event_id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            source_key TEXT NOT NULL,
            observed_at REAL NOT NULL,
            created_at REAL NOT NULL,
            ingestion_seq INTEGER NOT NULL
        );
        CREATE TABLE memory_sources (
            event_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            PRIMARY KEY (event_id, item_id, revision),
            FOREIGN KEY (event_id) REFERENCES memory_events(event_id),
            FOREIGN KEY (item_id, revision) REFERENCES memory_revisions(item_id, revision)
        );
        """
    )

    def row(item_id: str, scope: str, updated_at: float) -> None:
        conn.execute(
            "INSERT INTO memory_items VALUES (?, ?, 'decision', 'active', "
            "'ephemeral', 0, 'explicit', 0.9, 1, ?)",
            (item_id, scope, updated_at),
        )
        conn.execute(
            "INSERT INTO memory_revisions VALUES (?, 1, ?, '', '', ?)",
            (item_id, f"Decision claim for {item_id}", updated_at),
        )
        conn.execute(
            "INSERT INTO memory_events VALUES (?, 'evidence', 'turn:1', ?, ?, 1)",
            (f"event_{item_id}", updated_at, updated_at),
        )
        conn.execute(
            "INSERT INTO memory_sources VALUES (?, ?, 1)",
            (f"event_{item_id}", item_id),
        )

    row("session_derived", '{"scope_type":"profile","scope_id":"20260821_123456_abcdef"}', 1.0)
    row("stable_profile", '{"scope_type":"profile","scope_id":"stable-profile"}', 2.0)
    conn.commit()
    conn.close()


def test_plan_migration_skips_session_derived_profile_scope(tmp_path: Path) -> None:
    legacy = tmp_path / "memory_consolidation.db"
    _legacy_consolidation(legacy)

    report = plan_migration(source_path=legacy, limit=10)

    assert report["counts"]["scanned"] == 2
    assert report["counts"]["importable"] == 1
    assert report["counts"]["session_derived_profile"] == 1
    assert report["skipped"][0]["reason"] == "session_derived_profile_scope"
    assert report["items"][0]["source_item_id"] == "stable_profile"


def test_apply_migration_imports_stable_scope_candidates_only(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.db"
    target = tmp_path / "state.db"
    _legacy_consolidation(legacy)

    result = apply_migration(source_path=legacy, target_path=target.resolve(), limit=10)

    assert result["counts"]["applied"] == 1
    assert result["counts"]["session_derived_profile"] == 1
    with ExperienceStore(target) as store:
        decisions = store.list_decisions(principal_id="local-owner")
    assert [decision["id"] for decision in decisions]
    assert "session_derived" not in result["applied"][0]["source_item_id"]

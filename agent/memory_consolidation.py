"""Transactional, scope-isolated persistence for memory consolidation.

This module is deliberately independent from provider selection.  Callers must
resolve an authenticated scope before calling it; no method accepts a principal
or scope derived from evidence text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_KINDS = frozenset({"fact", "preference", "decision", "procedure"})
_STATUSES = frozenset({"active", "superseded", "archived", "conflicted", "retracted"})
_OP_TYPES = frozenset({"create", "revise", "supersede", "conflict", "archive", "retract"})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def deterministic_key(value: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 key for a fully specified candidate/operation."""
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class MemoryConsolidationStore:
    """SQLite store with an atomic outbox and one cursor per trusted scope."""

    def __init__(self, state_db_path: str | Path) -> None:
        self.db_path = Path(state_db_path)
        if not self.db_path.is_absolute() or self.db_path != self.db_path.resolve(strict=False):
            raise ValueError("state_db_path must be an explicit resolved absolute path")
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt" and stat.S_IMODE(self.db_path.parent.stat().st_mode) != 0o700:
            raise PermissionError("memory state directory must be owner-only")
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._closed = False
        os.chmod(self.db_path, 0o600)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def __enter__(self) -> "MemoryConsolidationStore": return self
    def __exit__(self, *_: object) -> None: self.close()
    def close(self) -> None:
        if not self._closed:
            self._conn.close(); self._closed = True

    def _write(self, action: Any) -> Any:
        with self._lock:
            if self._closed: raise RuntimeError("MemoryConsolidationStore is closed")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = action(self._conn); self._conn.commit(); return result
            except BaseException:
                self._conn.rollback(); raise

    def _init_schema(self) -> None:
        statements = (
            "CREATE TABLE IF NOT EXISTS memory_events (event_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, ingestion_seq INTEGER NOT NULL, source_key TEXT NOT NULL, observed_at REAL, created_at REAL NOT NULL, content TEXT NOT NULL, metadata_json TEXT NOT NULL, UNIQUE(scope_id, ingestion_seq), UNIQUE(scope_id, source_key))",
            "CREATE TABLE IF NOT EXISTS memory_scope_cursors (scope_id TEXT PRIMARY KEY, ingestion_seq INTEGER NOT NULL DEFAULT 0, event_id TEXT, updated_at REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS memory_items (item_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('fact','preference','decision','procedure')), status TEXT NOT NULL CHECK(status IN ('active','superseded','archived','conflicted','retracted')), retention_class TEXT NOT NULL CHECK(retention_class IN ('ephemeral','standard','protected')), pinned INTEGER NOT NULL DEFAULT 0, origin TEXT NOT NULL, confidence REAL NOT NULL, current_revision INTEGER NOT NULL, candidate_key TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(scope_id, candidate_key))",
            "CREATE TABLE IF NOT EXISTS memory_revisions (item_id TEXT NOT NULL, revision INTEGER NOT NULL, claim TEXT NOT NULL, evidence_json TEXT NOT NULL, candidate_key TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(item_id, revision), FOREIGN KEY(item_id) REFERENCES memory_items(item_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS memory_sources (item_id TEXT NOT NULL, revision INTEGER NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY(item_id, revision, event_id), FOREIGN KEY(item_id, revision) REFERENCES memory_revisions(item_id, revision) ON DELETE CASCADE, FOREIGN KEY(event_id) REFERENCES memory_events(event_id))",
            "CREATE TABLE IF NOT EXISTS memory_conflicts (left_item_id TEXT NOT NULL, right_item_id TEXT NOT NULL, scope_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('open','resolved')), created_at REAL NOT NULL, resolved_at REAL, CHECK(left_item_id < right_item_id), PRIMARY KEY(left_item_id, right_item_id), FOREIGN KEY(left_item_id) REFERENCES memory_items(item_id), FOREIGN KEY(right_item_id) REFERENCES memory_items(item_id))",
            "CREATE TABLE IF NOT EXISTS memory_conflict_flags (flag_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, item_id TEXT NOT NULL, revision INTEGER NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('open','resolved')), created_at REAL NOT NULL, resolved_at REAL, FOREIGN KEY(item_id) REFERENCES memory_items(item_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS memory_runs (run_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, start_seq INTEGER NOT NULL, end_seq INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('committed','observed','failed','rolled_back')), created_at REAL NOT NULL, committed_at REAL)",
            "CREATE TABLE IF NOT EXISTS memory_operations (operation_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, scope_id TEXT NOT NULL, operation_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('proposed','committed')), created_at REAL NOT NULL, FOREIGN KEY(run_id) REFERENCES memory_runs(run_id))",
            "CREATE TABLE IF NOT EXISTS memory_index_outbox (operation_key TEXT PRIMARY KEY, scope_id TEXT NOT NULL, item_id TEXT NOT NULL, revision INTEGER NOT NULL, created_at REAL NOT NULL, delivered_at REAL, FOREIGN KEY(operation_key) REFERENCES memory_operations(operation_key))",
            "CREATE TABLE IF NOT EXISTS memory_verification_runs (scope_id TEXT PRIMARY KEY, verified_at REAL NOT NULL)",
            # Derived retrieval state.  ``memory_items`` and its revisions are
            # authoritative; this table is disposable and can always be
            # rebuilt from them.  Keeping it separate makes asynchronous
            # index consumers safe around a committed memory transaction.
            "CREATE TABLE IF NOT EXISTS memory_retrieval_index (item_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, revision INTEGER NOT NULL, claim TEXT NOT NULL, status TEXT NOT NULL, updated_at REAL NOT NULL, FOREIGN KEY(item_id) REFERENCES memory_items(item_id) ON DELETE CASCADE)",
            "CREATE INDEX IF NOT EXISTS idx_memory_retrieval_index_scope ON memory_retrieval_index(scope_id, status, updated_at)",
        )
        for statement in statements: self._conn.execute(statement)

    @staticmethod
    def _scope_key(scope_type: Any, scope_id: Any) -> str:
        """Encode the typed scope as a collision-proof canonical key."""
        return _canonical({
            "scope_type": MemoryConsolidationStore._id(scope_type, "scope_type"),
            "scope_id": MemoryConsolidationStore._id(scope_id, "scope_id"),
        })

    @staticmethod
    def decode_scope_key(value: str) -> tuple[str, str]:
        parsed = json.loads(value)
        return str(parsed["scope_type"]), str(parsed["scope_id"])

    def scopes_with_evidence(self) -> list[tuple[str, str]]:
        rows = self._conn.execute("SELECT DISTINCT scope_id FROM memory_events ORDER BY scope_id").fetchall()
        return [self.decode_scope_key(row[0]) for row in rows]

    def select_memories_for_verification(self, *, scope_id: str, scope_type: str = "profile", limit: int = 100) -> list[dict[str, Any]]:
        """Select important, still-readable memories for the weekly audit.

        Protected and pinned memories are always preferred.  The remaining
        slots are filled by recently modified active/conflicted memories so a
        quiet profile is still periodically checked.
        """
        scope = self._scope_key(scope_type, scope_id)
        rows = self._conn.execute(
            """SELECT item_id, kind, status, retention_class, pinned, origin,
                      confidence, current_revision, updated_at
                 FROM memory_items
                WHERE scope_id=? AND status IN ('active','conflicted')
                ORDER BY CASE WHEN retention_class='protected' OR pinned=1 THEN 0 ELSE 1 END,
                         updated_at DESC, item_id
                LIMIT ?""",
            (scope, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def load_memory_for_verification(self, *, scope_id: str, scope_type: str = "profile", item_id: str) -> dict[str, Any]:
        """Load a current revision and all immutable source evidence."""
        scope = self._scope_key(scope_type, scope_id)
        item_key = self._id(item_id, "item_id")
        row = self._conn.execute(
            """SELECT i.item_id, i.scope_id, i.kind, i.status, i.retention_class,
                      i.pinned, i.origin, i.confidence, i.current_revision,
                      r.claim, r.evidence_json
                 FROM memory_items i JOIN memory_revisions r
                   ON r.item_id=i.item_id AND r.revision=i.current_revision
                WHERE i.scope_id=? AND i.item_id=?""",
            (scope, item_key),
        ).fetchone()
        if row is None:
            raise ValueError("memory item not found in scope")
        event_ids = json.loads(row["evidence_json"])
        if not isinstance(event_ids, list):
            raise ValueError("memory evidence is malformed")
        evidence = [
            dict(event)
            for event in self._conn.execute(
                "SELECT event_id, content, source_key, observed_at, created_at, metadata_json FROM memory_events WHERE scope_id=? AND event_id IN (%s) ORDER BY ingestion_seq" % ",".join("?" for _ in event_ids),
                (scope, *event_ids),
            ).fetchall()
        ] if event_ids else []
        result = dict(row)
        result.pop("evidence_json", None)
        result["evidence"] = evidence
        return result

    def memory_status_counts(self, *, scope_id: str, scope_type: str = "profile") -> dict[str, int]:
        scope = self._scope_key(scope_type, scope_id)
        rows = self._conn.execute("SELECT status, COUNT(*) FROM memory_items WHERE scope_id=? GROUP BY status", (scope,)).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def memories_without_evidence(self, *, scope_id: str, scope_type: str = "profile") -> int:
        scope = self._scope_key(scope_type, scope_id)
        return int(self._conn.execute(
            """SELECT COUNT(*) FROM memory_items i
                JOIN memory_revisions r ON r.item_id=i.item_id AND r.revision=i.current_revision
                WHERE i.scope_id=? AND (r.evidence_json='[]' OR r.evidence_json IS NULL)""",
            (scope,),
        ).fetchone()[0])

    @staticmethod
    def _id(value: Any, name: str) -> str:
        result = str(value).strip()
        if not _ID.fullmatch(result): raise ValueError(f"invalid {name}")
        return result

    @staticmethod
    def _text(value: Any, name: str, maximum: int = 8000) -> str:
        result = str(value).strip()
        if not result or len(result) > maximum or "\x00" in result: raise ValueError(f"invalid {name}")
        return result

    def append_evidence(self, *, scope_id: str, scope_type: str = "profile", source_key: str, content: str,
                        observed_at: float | None = None, metadata: Mapping[str, Any] | None = None,
                        created_at: float | None = None) -> dict[str, Any]:
        scope, source = self._scope_key(scope_type, scope_id), self._id(source_key, "source_key")
        content = self._text(content, "content")
        metadata_json = _canonical(dict(metadata or {}))
        if len(metadata_json) > 8192: raise ValueError("metadata is too large")
        now = time.time() if created_at is None else float(created_at)
        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            existing = conn.execute("SELECT * FROM memory_events WHERE scope_id=? AND source_key=?", (scope, source)).fetchone()
            if existing: return dict(existing)
            sequence = conn.execute("SELECT COALESCE(MAX(ingestion_seq), 0) + 1 FROM memory_events WHERE scope_id=?", (scope,)).fetchone()[0]
            event_id = "event_" + uuid.uuid4().hex
            conn.execute("INSERT INTO memory_events VALUES(?,?,?,?,?,?,?,?)", (event_id, scope, sequence, source, observed_at, now, content, metadata_json))
            return dict(conn.execute("SELECT * FROM memory_events WHERE event_id=?", (event_id,)).fetchone())
        return self._write(action)

    def record_plan(self, *, scope_id: str, scope_type: str = "profile", run_id: str, start_seq: int, end_seq: int,
                    operations: Sequence[Mapping[str, Any]], recorded_at: float | None = None) -> dict[str, Any]:
        """Persist an observe-only plan without applying memory mutations."""
        scope = self._scope_key(scope_type, scope_id)
        run = self._id(run_id, "run_id")
        now = time.time() if recorded_at is None else float(recorded_at)
        normalized = [self._operation(scope, operation) for operation in operations]

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            existing = conn.execute("SELECT status FROM memory_runs WHERE run_id=?", (run,)).fetchone()
            if existing:
                return {"run_id": run, "replayed": True}
            conn.execute(
                "INSERT INTO memory_runs VALUES(?,?,?,?,?,?,?)",
                (run, scope, int(start_seq), int(end_seq), "observed", now, now),
            )
            for operation in normalized:
                key = operation["operation_key"]
                conn.execute(
                    "INSERT OR IGNORE INTO memory_operations VALUES(?,?,?,?,?,?)",
                    (key, run, scope, _canonical(operation), "proposed", now),
                )
            return {"run_id": run, "replayed": False}

        return self._write(action)

    def record_failed_run(self, *, scope_id: str, scope_type: str = "profile", run_id: str,
                          start_seq: int, end_seq: int, failed_at: float | None = None) -> dict[str, Any]:
        """Record a failed scheduled attempt without changing its evidence cursor."""
        scope = self._scope_key(scope_type, scope_id)
        run = self._id(run_id, "run_id")
        now = time.time() if failed_at is None else float(failed_at)

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            existing = conn.execute(
                "SELECT status FROM memory_runs WHERE run_id=?", (run,)
            ).fetchone()
            if existing is not None:
                if existing["status"] == "failed":
                    conn.execute(
                        "UPDATE memory_runs SET created_at=? WHERE run_id=?", (now, run)
                    )
                return {"run_id": run, "replayed": True}
            conn.execute(
                "INSERT INTO memory_runs VALUES(?,?,?,?,?,?,?)",
                (run, scope, int(start_seq), int(end_seq), "failed", now, None),
            )
            return {"run_id": run, "replayed": False}

        return self._write(action)

    def evidence_after_cursor(self, scope_id: str, scope_type: str = "profile") -> list[dict[str, Any]]:
        scope = self._scope_key(scope_type, scope_id)
        row = self._conn.execute("SELECT ingestion_seq FROM memory_scope_cursors WHERE scope_id=?", (scope,)).fetchone()
        cursor = int(row[0]) if row else 0
        return [dict(row) for row in self._conn.execute("SELECT * FROM memory_events WHERE scope_id=? AND ingestion_seq>? ORDER BY ingestion_seq,event_id", (scope, cursor))]

    def store_hash(self, *, scope_id: str | None = None, scope_type: str = "profile") -> str:
        """Return a stable, text-free fingerprint for migration idempotency."""
        scope = None if scope_id is None else self._scope_key(scope_type, scope_id)
        if scope is None:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM memory_items GROUP BY status ORDER BY status"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM memory_items WHERE scope_id=? GROUP BY status ORDER BY status",
                (scope,),
            ).fetchall()
        import hashlib

        material = json.dumps([dict(row) for row in rows], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def list_current_memories(self, *, scope_id: str, scope_type: str = "profile",
                              include_archived: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        """Return bounded current revision summaries for migration review."""
        scope = self._scope_key(scope_type, scope_id)
        statuses = ("active", "conflicted", "archived", "superseded") if include_archived else ("active", "conflicted")
        placeholders = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"""SELECT i.item_id, i.kind, i.status, i.retention_class,
                       i.pinned, i.origin, i.confidence, i.current_revision,
                       i.updated_at, r.claim, r.candidate_key
                  FROM memory_items i
                  JOIN memory_revisions r
                    ON r.item_id=i.item_id AND r.revision=i.current_revision
                 WHERE i.scope_id=? AND i.status IN ({placeholders})
                 ORDER BY CASE i.status
                            WHEN 'conflicted' THEN 0
                            WHEN 'active' THEN 1
                            WHEN 'superseded' THEN 2
                            WHEN 'archived' THEN 3
                            ELSE 4
                          END,
                          i.updated_at DESC, i.item_id
                 LIMIT ?""",
            (scope, *statuses, max(1, min(int(limit), 1_000))),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            evidence = [
                dict(item)
                for item in self._conn.execute(
                    "SELECT e.event_id, e.content, e.source_key, e.observed_at, e.created_at "
                    "FROM memory_sources s JOIN memory_events e ON e.event_id=s.event_id "
                    "WHERE s.item_id=? AND s.revision=? ORDER BY e.ingestion_seq, e.event_id",
                    (row["item_id"], int(row["current_revision"])),
                ).fetchall()
            ]
            result = dict(row)
            result["evidence"] = evidence
            results.append(result)
        return results

    def cursor(self, scope_id: str, scope_type: str = "profile") -> int:
        row = self._conn.execute("SELECT ingestion_seq FROM memory_scope_cursors WHERE scope_id=?", (self._scope_key(scope_type, scope_id),)).fetchone()
        return int(row[0]) if row else 0

    def pending_index_outbox(self, *, scope_id: str | None = None,
                             scope_type: str = "profile", limit: int = 100) -> list[dict[str, Any]]:
        """Return undelivered derived-index updates in commit order.

        The outbox is intentionally durable and append-only.  Consumers may
        safely retry a row; ``operation_key`` is the idempotency key exposed
        to external indexers.
        """
        bounded = max(1, min(int(limit), 10_000))
        if scope_id is None:
            rows = self._conn.execute(
                "SELECT * FROM memory_index_outbox WHERE delivered_at IS NULL "
                "ORDER BY created_at, operation_key LIMIT ?", (bounded,)
            ).fetchall()
        else:
            scope = self._scope_key(scope_type, scope_id)
            rows = self._conn.execute(
                "SELECT * FROM memory_index_outbox WHERE scope_id=? AND delivered_at IS NULL "
                "ORDER BY created_at, operation_key LIMIT ?", (scope, bounded)
            ).fetchall()
        return [dict(row) for row in rows]

    def _index_item(self, conn: sqlite3.Connection, *, item_id: str,
                    expected_revision: int, updated_at: float) -> dict[str, Any] | None:
        """Materialize one current memory item into the disposable index."""
        item = conn.execute(
            "SELECT i.scope_id, i.current_revision, i.status, r.claim "
            "FROM memory_items i JOIN memory_revisions r "
            "ON r.item_id=i.item_id AND r.revision=i.current_revision "
            "WHERE i.item_id=?", (item_id,)
        ).fetchone()
        if item is None:
            conn.execute("DELETE FROM memory_retrieval_index WHERE item_id=?", (item_id,))
            return None
        # A stale outbox event must never overwrite a newer revision.
        conn.execute(
            "INSERT INTO memory_retrieval_index(item_id,scope_id,revision,claim,status,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET "
            "scope_id=excluded.scope_id,revision=excluded.revision,claim=excluded.claim,"
            "status=excluded.status,updated_at=excluded.updated_at "
            "WHERE excluded.revision >= memory_retrieval_index.revision",
            (item_id, item[0], int(item[1]), item[3], item[2], updated_at),
        )
        return {"item_id": item_id, "scope_id": item[0], "revision": int(item[1]),
                "expected_revision": int(expected_revision), "claim": item[3], "status": item[2]}

    def consume_index_outbox(self, *, scope_id: str | None = None,
                             scope_type: str = "profile", limit: int = 100,
                             indexer: Any = None,
                             delivered_at: float | None = None) -> dict[str, int]:
        """Deliver pending index updates and materialize the local index.

        ``indexer`` is an optional callable receiving a text-free event.  It
        must be idempotent by ``operation_key``.  Failures leave the row
        pending for retry and are propagated after prior successful rows.
        """
        rows = self.pending_index_outbox(scope_id=scope_id, scope_type=scope_type, limit=limit)
        delivered = 0
        failed = 0
        now = time.time() if delivered_at is None else float(delivered_at)
        for row in rows:
            try:
                def action(conn: sqlite3.Connection) -> dict[str, Any] | None:
                    return self._index_item(conn, item_id=row["item_id"],
                                            expected_revision=int(row["revision"]), updated_at=now)
                current = self._write(action)
                event_scope_type, event_scope_id = self.decode_scope_key(row["scope_id"])
                event = {"operation_key": row["operation_key"], "scope_id": row["scope_id"],
                         "scope_type": event_scope_type, "scope_identifier": event_scope_id,
                         "item_id": row["item_id"], "revision": row["revision"],
                         "current": current}
                if indexer is not None:
                    indexer(event)
                self._write(lambda conn: conn.execute(
                    "UPDATE memory_index_outbox SET delivered_at=? WHERE operation_key=? AND delivered_at IS NULL",
                    (now, row["operation_key"])))
                delivered += 1
            except Exception:
                failed += 1
                # Keep processing independent rows, but expose the failure to
                # the caller through the count so schedulers can alert/retry.
                continue
        return {"seen": len(rows), "delivered": delivered, "failed": failed}

    def rebuild_retrieval_index(self, *, scope_id: str | None = None,
                                scope_type: str = "profile") -> dict[str, int]:
        """Rebuild disposable retrieval rows from authoritative revisions.

        Rebuild does not acknowledge the outbox: external indexers still need
        their durable events, while local retrieval becomes immediately
        consistent.  ``scope_id=None`` rebuilds all scopes.
        """
        scope = None if scope_id is None else self._scope_key(scope_type, scope_id)
        def action(conn: sqlite3.Connection) -> int:
            if scope is None:
                conn.execute("DELETE FROM memory_retrieval_index")
                rows = conn.execute(
                    "SELECT i.item_id,i.scope_id,i.current_revision,r.claim,i.status,i.updated_at "
                    "FROM memory_items i JOIN memory_revisions r ON r.item_id=i.item_id AND r.revision=i.current_revision "
                    "WHERE i.status IN ('active','conflicted')"
                ).fetchall()
            else:
                conn.execute("DELETE FROM memory_retrieval_index WHERE scope_id=?", (scope,))
                rows = conn.execute(
                    "SELECT i.item_id,i.scope_id,i.current_revision,r.claim,i.status,i.updated_at "
                    "FROM memory_items i JOIN memory_revisions r ON r.item_id=i.item_id AND r.revision=i.current_revision "
                    "WHERE i.scope_id=? AND i.status IN ('active','conflicted')", (scope,)
                ).fetchall()
            conn.executemany(
                "INSERT INTO memory_retrieval_index(item_id,scope_id,revision,claim,status,updated_at) VALUES(?,?,?,?,?,?)",
                [tuple(row) for row in rows],
            )
            return len(rows)
        return {"indexed": int(self._write(action))}

    # Friendly aliases used by scheduler integrations.  Keep the canonical
    # names above explicit in documentation while allowing older callers to
    # use the shorter index terminology.
    def drain_index_outbox(self, **kwargs: Any) -> dict[str, int]:
        return self.consume_index_outbox(**kwargs)

    def rebuild_index(self, **kwargs: Any) -> dict[str, int]:
        return self.rebuild_retrieval_index(**kwargs)

    def last_run_at(self, scope_id: str, scope_type: str = "profile") -> float | None:
        scope = self._scope_key(scope_type, scope_id)
        row = self._conn.execute(
            "SELECT created_at FROM memory_runs WHERE scope_id=? ORDER BY created_at DESC LIMIT 1",
            (scope,),
        ).fetchone()
        return float(row[0]) if row else None

    def last_verification_at(self, scope_id: str, scope_type: str = "profile") -> float | None:
        scope = self._scope_key(scope_type, scope_id)
        row = self._conn.execute(
            "SELECT verified_at FROM memory_verification_runs WHERE scope_id=?", (scope,)
        ).fetchone()
        return float(row[0]) if row else None

    def record_verification_run(self, scope_id: str, scope_type: str = "profile",
                                verified_at: float | None = None) -> None:
        scope = self._scope_key(scope_type, scope_id)
        timestamp = time.time() if verified_at is None else float(verified_at)
        self._write(lambda conn: conn.execute(
            "INSERT INTO memory_verification_runs(scope_id,verified_at) VALUES(?,?) "
            "ON CONFLICT(scope_id) DO UPDATE SET verified_at=excluded.verified_at",
            (scope, timestamp),
        ))

    def find_relevant_memories(self, *, scope_id: str, scope_type: str = "profile", claim: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return active same-scope memories for conservative matching.

        SQLite's simple token search is intentionally narrow; callers may
        provide a richer matcher, but this baseline never crosses scopes.
        """
        scope = self._scope_key(scope_type, scope_id)
        text = self._text(claim, "claim")
        rows = self._conn.execute(
            """SELECT i.item_id AS id, i.kind, i.status, r.claim AS content,
                      i.current_revision AS revision
                 FROM memory_items i
                 JOIN memory_revisions r
                   ON r.item_id=i.item_id AND r.revision=i.current_revision
                WHERE i.scope_id=? AND i.status IN ('active','conflicted')
                  AND (lower(r.claim)=lower(?) OR lower(r.claim) LIKE lower(?))
                ORDER BY i.updated_at DESC LIMIT ?""",
            (scope, text, f"%{text}%", max(1, int(limit))),
        ).fetchall()
        results = [dict(row) for row in rows]
        for result in results:
            evidence_rows = self._conn.execute(
                "SELECT s.event_id, e.content, e.created_at "
                "FROM memory_sources s JOIN memory_events e ON e.event_id=s.event_id "
                "JOIN memory_items i ON i.item_id=s.item_id "
                "WHERE s.item_id=? AND s.revision=? AND i.scope_id=? "
                "ORDER BY e.ingestion_seq, e.event_id",
                (result["id"], int(result["revision"]), scope),
            ).fetchall()
            result["evidence"] = [dict(item) for item in evidence_rows]
        return results

    def conflict_flags(self, *, scope_id: str, scope_type: str = "profile",
                       state: str = "open") -> list[dict[str, Any]]:
        scope = self._scope_key(scope_type, scope_id)
        if state not in {"open", "resolved"}:
            raise ValueError("invalid conflict flag state")
        rows = self._conn.execute(
            "SELECT * FROM memory_conflict_flags WHERE scope_id=? AND state=? "
            "ORDER BY created_at, flag_id", (scope, state)
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve_conflict_flag(self, flag_id: str, *, scope_id: str,
                              scope_type: str = "profile",
                              resolved_at: float | None = None) -> None:
        flag = self._id(flag_id, "flag_id")
        scope = self._scope_key(scope_type, scope_id)
        timestamp = time.time() if resolved_at is None else float(resolved_at)
        def action(conn: sqlite3.Connection) -> None:
            updated = conn.execute(
                "UPDATE memory_conflict_flags SET state='resolved',resolved_at=? "
                "WHERE flag_id=? AND scope_id=? AND state='open'",
                (timestamp, flag, scope),
            ).rowcount
            if not updated:
                raise ValueError("open conflict flag not found in scope")
        self._write(action)

    def search_retrieval_index(self, *, scope_id: str, scope_type: str = "profile",
                               query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Search the disposable same-scope retrieval index.

        Retrieval is deliberately lexical and bounded.  The authoritative
        memory/revision tables remain the source of truth; callers can rebuild
        this table after corruption or an indexer outage.  Scope filtering is
        applied before scoring so a query can never leak another tenant's
        memory.
        """
        scope = self._scope_key(scope_type, scope_id)
        text = str(query or "").strip().lower()
        if not text:
            return []
        tokens = tuple(dict.fromkeys(re.findall(r"[a-z0-9_:-]+", text)))
        rows = self._conn.execute(
            "SELECT item_id, revision, claim, status, updated_at "
            "FROM memory_retrieval_index WHERE scope_id=? "
            "AND status IN ('active','conflicted') ORDER BY updated_at DESC LIMIT 500",
            (scope,),
        ).fetchall()
        scored: list[tuple[int, float, dict[str, Any]]] = []
        for row in rows:
            claim = str(row[2])
            lower_claim = claim.lower()
            score = sum(1 for token in tokens if token in lower_claim)
            if score:
                scored.append((score, float(row[4]), {
                    "item_id": row[0], "revision": int(row[1]), "claim": claim,
                    "status": row[3], "score": score,
                }))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]["item_id"]))
        return [item[2] for item in scored[:max(1, min(int(limit), 50))]]

    def commit(self, *, scope_id: str, scope_type: str = "profile", operations: Sequence[Mapping[str, Any]], end_seq: int,
               run_id: str | None = None, committed_at: float | None = None) -> dict[str, Any]:
        """Commit a plan through its explicit evidence cutoff atomically.

        Replays are audited no-ops: they return the original committed run ID
        and never create another run, operation, or outbox record.  Requiring
        ``end_seq`` prevents a plan based on older evidence from consuming
        later, unplanned events.
        """
        scope = self._scope_key(scope_type, scope_id)
        now, run = time.time() if committed_at is None else float(committed_at), self._id(run_id or "run_" + uuid.uuid4().hex, "run_id")
        normalized = [self._operation(scope, operation) for operation in operations]
        if len({op["operation_key"] for op in normalized}) != len(normalized): raise ValueError("duplicate operation_key")
        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            start = conn.execute("SELECT ingestion_seq FROM memory_scope_cursors WHERE scope_id=?", (scope,)).fetchone()
            start_seq = int(start[0]) if start else 0
            max_seq = conn.execute("SELECT COALESCE(MAX(ingestion_seq),0) FROM memory_events WHERE scope_id=?", (scope,)).fetchone()[0]
            target = int(end_seq)
            if target < start_seq or target > max_seq: raise ValueError("end_seq is outside this scope's evidence")
            existing_rows = [conn.execute("SELECT status FROM memory_operations WHERE operation_key=?", (op["operation_key"],)).fetchone() for op in normalized]
            existing = [row for row in existing_rows if row is not None]
            committed_keys = ["commit_" + op["operation_key"] for op in normalized]
            committed_rows = [conn.execute("SELECT status FROM memory_operations WHERE operation_key=?", (key,)).fetchone() for key in committed_keys]
            if normalized and all(row is not None and row[0] == "committed" for row in committed_rows):
                original = conn.execute("SELECT run_id FROM memory_operations WHERE operation_key=?", (committed_keys[0],)).fetchone()[0]
                return {"run_id": original, "replayed": True, "cursor": start_seq}
            existing_run = conn.execute(
                "SELECT run_id,start_seq,end_seq,status FROM memory_runs "
                "WHERE run_id=? AND scope_id=?", (run, scope)
            ).fetchone()
            if (
                existing_run is not None
                and existing_run[3] == "committed"
                and int(existing_run[1]) == start_seq
                and int(existing_run[2]) == target
            ):
                return {"run_id": run, "replayed": True, "cursor": start_seq}
            if existing and len(existing) != len(normalized): raise ValueError("partial replay is not allowed")
            commit_operations = [{**op, "operation_key": "commit_" + op["operation_key"]} for op in normalized]
            conn.execute("INSERT INTO memory_runs VALUES(?,?,?,?,?,?,?)", (run, scope, start_seq, target, "committed", now, now))
            for op in commit_operations: self._apply(conn, run, scope, op, now)
            event = conn.execute("SELECT event_id FROM memory_events WHERE scope_id=? AND ingestion_seq=?", (scope, target)).fetchone()
            conn.execute("INSERT INTO memory_scope_cursors(scope_id,ingestion_seq,event_id,updated_at) VALUES(?,?,?,?) ON CONFLICT(scope_id) DO UPDATE SET ingestion_seq=excluded.ingestion_seq,event_id=excluded.event_id,updated_at=excluded.updated_at", (scope, target, event[0] if event else None, now))
            return {"run_id": run, "replayed": False, "cursor": target}
        return self._write(action)

    def _operation(self, scope: str, operation: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(operation, Mapping): raise ValueError("operation must be a mapping")
        kind = str(operation.get("type", "")); candidate = operation.get("candidate")
        if kind not in _OP_TYPES or not isinstance(candidate, Mapping): raise ValueError("invalid operation")
        claim = self._text(candidate.get("claim"), "candidate.claim")
        item_kind = str(candidate.get("kind", "fact"))
        if item_kind not in _KINDS: raise ValueError("invalid candidate.kind")
        evidence = sorted({self._id(x, "evidence event_id") for x in candidate.get("evidence_event_ids", [])})
        if not evidence: raise ValueError("candidate requires evidence_event_ids")
        origin = str(candidate.get("origin", "observed"))
        if origin not in {"explicit", "observed", "inferred", "imported"}:
            origin = "observed"
        confidence = min(1.0, max(0.0, float(candidate.get("confidence", 0.0))))
        retention = str(candidate.get("retention_class", "standard"))
        if retention not in {"ephemeral", "standard", "protected"}:
            raise ValueError("invalid retention_class")
        pinned = bool(candidate.get("pinned", False))
        canonical = {"scope_id": scope, "type": kind, "candidate": {"claim": claim, "kind": item_kind, "evidence_event_ids": evidence, "origin": origin, "confidence": confidence, "retention_class": retention, "pinned": pinned}, "target_item_id": operation.get("target_item_id"), "supersedes_item_id": operation.get("supersedes_item_id"), "supersedes_item_ids": sorted(set(operation.get("supersedes_item_ids") or [])), "conflicts_with": operation.get("conflicts_with")}
        key = deterministic_key(canonical)
        return {**canonical, "operation_key": key}

    def _apply(self, conn: sqlite3.Connection, run: str, scope: str, op: Mapping[str, Any], now: float) -> None:
        candidate, key, target = op["candidate"], op["operation_key"], op.get("target_item_id")
        for event_id in candidate["evidence_event_ids"]:
            row = conn.execute("SELECT 1 FROM memory_events WHERE event_id=? AND scope_id=?", (event_id, scope)).fetchone()
            if not row: raise ValueError("evidence does not belong to scope")
        item_id = self._id(target, "target_item_id") if target else "memory_" + key[:32]
        row = conn.execute("SELECT * FROM memory_items WHERE item_id=?", (item_id,)).fetchone()
        if row and row["scope_id"] != scope: raise ValueError("target item does not belong to scope")
        if op["type"] == "create" and row: raise ValueError("target item already exists")
        retention = candidate["retention_class"]
        pinned = int(bool(candidate["pinned"]))
        origin = candidate["origin"]
        confidence = float(candidate["confidence"])
        if op["type"] in {"archive", "retract"} and row is not None and (row["retention_class"] == "protected" or row["pinned"]):
            raise ValueError("protected or pinned memory cannot be archived")
        if op["type"] == "revise" and row is not None:
            retention = row["retention_class"]
            pinned = row["pinned"]
        if op["type"] == "revise" and row is not None and candidate.get("retention_class") == "protected":
            retention = "protected"
        if candidate["retention_class"] == "protected" and origin != "explicit" and confidence < 0.8:
            raise ValueError("low-confidence inference cannot become protected")
        revision = 1 if row is None else int(row["current_revision"]) + 1
        status = {
            "archive": "archived",
            "retract": "retracted",
            "supersede": "superseded",
            "conflict": "conflicted",
        }.get(op["type"], "active")
        if row is None:
            conn.execute("INSERT INTO memory_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (item_id, scope, candidate["kind"], status, retention, pinned, origin, confidence, revision, deterministic_key(candidate), now, now))
        else:
            conn.execute("UPDATE memory_items SET status=?,retention_class=?,pinned=?,origin=?,confidence=?,current_revision=?,updated_at=? WHERE item_id=?", (status, retention, pinned, origin, confidence, revision, now, item_id))
        # A revision must retain the full supporting history.  Reinforcement,
        # merge, and supersession therefore union prior source events with the
        # new candidate evidence before writing the immutable revision.
        prior_events = []
        if row is not None:
            prior_events = [
                source[0]
                for source in conn.execute(
                    "SELECT event_id FROM memory_sources WHERE item_id=? AND revision=?",
                    (item_id, row["current_revision"]),
                )
            ]
        superseded_ids = list(op.get("supersedes_item_ids") or [])
        if op.get("supersedes_item_id"):
            superseded_ids.append(op["supersedes_item_id"])
        for superseded_id in superseded_ids:
            old = conn.execute("SELECT scope_id,current_revision FROM memory_items WHERE item_id=?", (superseded_id,)).fetchone()
            if not old or old[0] != scope:
                raise ValueError("superseded item does not belong to scope")
            prior_events.extend(
                source[0]
                for source in conn.execute(
                    "SELECT event_id FROM memory_sources WHERE item_id=? AND revision=?",
                    (superseded_id, old[1]),
                )
            )
        revision_events = sorted(set(prior_events).union(candidate["evidence_event_ids"]))
        evidence_json = _canonical(revision_events)
        conn.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?)", (item_id, revision, candidate["claim"], evidence_json, deterministic_key(candidate), now))
        conn.executemany("INSERT INTO memory_sources VALUES(?,?,?)", [(item_id, revision, event) for event in revision_events])
        audit = dict(op)
        audit["undo"] = {
            "item_id": item_id,
            "item_existed": row is not None,
            "previous_revision": int(row["current_revision"]) if row is not None else None,
            "previous_status": row["status"] if row is not None else None,
            "previous_retention_class": row["retention_class"] if row is not None else None,
            "previous_pinned": row["pinned"] if row is not None else None,
            "previous_origin": row["origin"] if row is not None else None,
            "previous_confidence": row["confidence"] if row is not None else None,
            "superseded_item_id": op.get("supersedes_item_id"),
            "superseded_previous_status": (
                conn.execute("SELECT status FROM memory_items WHERE item_id=?", (op["supersedes_item_id"],)).fetchone()[0]
                if op.get("supersedes_item_id") and conn.execute("SELECT 1 FROM memory_items WHERE item_id=?", (op["supersedes_item_id"],)).fetchone()
                else None
            ),
            "superseded_items": [
                {"item_id": item_id, "status": conn.execute("SELECT status FROM memory_items WHERE item_id=?", (item_id,)).fetchone()[0]}
                for item_id in list(op.get("supersedes_item_ids") or [])
                if conn.execute("SELECT 1 FROM memory_items WHERE item_id=?", (item_id,)).fetchone()
            ],
        }
        conn.execute("INSERT INTO memory_operations VALUES(?,?,?,?,?,?)", (key, run, scope, _canonical(audit), "committed", now))
        conn.execute("INSERT INTO memory_index_outbox VALUES(?,?,?,?,?,NULL)", (key, scope, item_id, revision, now))
        other = op.get("conflicts_with")
        if other:
            other_id = self._id(other, "conflicts_with")
            other_row = conn.execute("SELECT scope_id FROM memory_items WHERE item_id=?", (other_id,)).fetchone()
            if not other_row or other_row[0] != scope: raise ValueError("conflict item does not belong to scope")
            left, right = sorted((item_id, other_id))
            conn.execute("INSERT OR IGNORE INTO memory_conflicts VALUES(?,?,?,?,?,NULL)", (left, right, scope, "open", now))
            conn.execute("UPDATE memory_items SET status='conflicted',updated_at=? WHERE item_id IN (?,?)", (now, left, right))
        elif op["type"] == "conflict":
            # Verification can identify a single memory whose representation
            # no longer agrees with its immutable evidence.  Preserve that
            # finding as a first-class, resolvable flag rather than relying on
            # the item's status alone.
            conn.execute(
                "INSERT OR IGNORE INTO memory_conflict_flags "
                "(flag_id,scope_id,item_id,revision,reason,evidence_json,state,created_at,resolved_at) "
                "VALUES(?,?,?,?,?,?, 'open', ?, NULL)",
                (key, scope, item_id, revision, op.get("reason") or candidate["claim"],
                 evidence_json, now),
            )
        for superseded in superseded_ids:
            conn.execute("UPDATE memory_items SET status='superseded',updated_at=? WHERE item_id=?", (now, superseded))

    def rollback_run(self, run_id: str) -> dict[str, Any]:
        """Apply compensating revisions for the latest committed run.

        Historical rows remain intact.  A run is rejected when a later commit
        touched the same scope, which prevents rollback from clobbering newer
        interpretations.
        """
        run_key = self._id(run_id, "run_id")

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            run = conn.execute(
                "SELECT * FROM memory_runs WHERE run_id=? AND status='committed'",
                (run_key,),
            ).fetchone()
            if run is None:
                raise ValueError("committed run not found")
            later = conn.execute(
                "SELECT 1 FROM memory_runs WHERE scope_id=? AND status='committed' AND committed_at>? LIMIT 1",
                (run["scope_id"], run["committed_at"]),
            ).fetchone()
            if later:
                raise ValueError("only the latest committed run may be rolled back")
            operations = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT operation_json FROM memory_operations WHERE run_id=? ORDER BY created_at DESC",
                    (run_key,),
                )
            ]
            restored = 0
            for operation in operations:
                undo = operation.get("undo") or {}
                # Conflict verification flags are part of the operation's
                # compensating history and must disappear on rollback.
                if operation.get("operation_key"):
                    conn.execute(
                        "DELETE FROM memory_conflict_flags WHERE flag_id=?",
                        (operation["operation_key"],),
                    )
                item_id = (operation.get("undo") or {}).get("item_id") or operation.get("target_item_id")
                if not item_id:
                    raise ValueError("rollback item identity missing")
                item = conn.execute("SELECT * FROM memory_items WHERE item_id=?", (item_id,)).fetchone()
                if item is None:
                    continue
                previous_revision = undo.get("previous_revision")
                previous_status = undo.get("previous_status")
                if not previous_revision:
                    now = time.time()
                    conn.execute("UPDATE memory_items SET status='retracted',updated_at=? WHERE item_id=?", (now, item_id))
                    rollback_key = "rollback_" + deterministic_key({"run_id": run_key, "item_id": item_id})
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_operations VALUES(?,?,?,?,?,?)",
                        (rollback_key, run_key, run["scope_id"], _canonical({"type": "rollback", "item_id": item_id}), "committed", now),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_index_outbox VALUES(?,?,?,?,?,NULL)",
                        (rollback_key, run["scope_id"], item_id, int(item["current_revision"]), now),
                    )
                    restored += 1
                else:
                    previous = conn.execute(
                        "SELECT claim,evidence_json FROM memory_revisions WHERE item_id=? AND revision=?",
                        (item_id, int(previous_revision)),
                    ).fetchone()
                    if previous is None:
                        raise ValueError("rollback source revision missing")
                    new_revision = int(item["current_revision"]) + 1
                    now = time.time()
                    candidate_key = deterministic_key({"claim": previous[0], "evidence": json.loads(previous[1])})
                    conn.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?)", (item_id, new_revision, previous[0], previous[1], candidate_key, now))
                    conn.executemany("INSERT INTO memory_sources VALUES(?,?,?)", [(item_id, new_revision, event) for event in json.loads(previous[1])])
                    conn.execute("UPDATE memory_items SET status=?,retention_class=?,pinned=?,origin=?,confidence=?,current_revision=?,updated_at=? WHERE item_id=?", (previous_status or "active", undo.get("previous_retention_class") or "standard", int(undo.get("previous_pinned") or 0), undo.get("previous_origin") or "observed", float(undo.get("previous_confidence") or 0.0), new_revision, now, item_id))
                    rollback_key = "rollback_" + deterministic_key({"run_id": run_key, "item_id": item_id, "revision": new_revision})
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_operations VALUES(?,?,?,?,?,?)",
                        (rollback_key, run_key, run["scope_id"], _canonical({"type": "rollback", "item_id": item_id, "revision": new_revision}), "committed", now),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_index_outbox VALUES(?,?,?,?,?,NULL)",
                        (rollback_key, run["scope_id"], item_id, new_revision, now),
                    )
                    restored += 1
                superseded_id = undo.get("superseded_item_id")
                if superseded_id:
                    conn.execute(
                        "UPDATE memory_items SET status=?,updated_at=? WHERE item_id=?",
                        (undo.get("superseded_previous_status") or "active", time.time(), superseded_id),
                    )
                for old in undo.get("superseded_items") or []:
                    conn.execute(
                        "UPDATE memory_items SET status=?,updated_at=? WHERE item_id=?",
                        (old.get("status") or "active", time.time(), old.get("item_id")),
                    )
                if operation.get("conflicts_with"):
                    left, right = sorted((item_id, operation["conflicts_with"]))
                    conn.execute(
                        "UPDATE memory_conflicts SET state='resolved',resolved_at=? WHERE left_item_id=? AND right_item_id=?",
                        (time.time(), left, right),
                    )
                continue
            conn.execute("UPDATE memory_runs SET status='rolled_back' WHERE run_id=?", (run_key,))
            return {"run_id": run_key, "restored": restored}

        return self._write(action)

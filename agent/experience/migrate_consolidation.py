"""Conservative migration from legacy memory consolidation into ExperienceStore.

The migration is intentionally one-way and candidate-only: active legacy
Decision records become unapproved Work Experience Decision candidates that
require local-owner review before they can influence behavior. The legacy
database is never modified.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.experience.models import LOCAL_OWNER_PRINCIPAL
from agent.experience.safety import sanitize_for_storage
from agent.experience.store import ExperienceStore


_SOURCE_SYSTEM = "memory_consolidation"
_PROFILE_SCOPE_ID = "local-owner"
_MAX_CLAIM_CHARS = 4_000
_MAX_SUMMARY_CHARS = 512
_MAX_RATIONALE_CHARS = 1_000
_SESSION_SCOPE_RE = re.compile(r"\d{8}_\d{6}_[A-Za-z0-9]{6,}\Z")


@dataclass(frozen=True, slots=True)
class MigrationPlanItem:
    source_item_id: str
    source_revision: int
    target_item_id: str
    title: str
    summary: str
    statement: str
    rationale: str
    effective_at: float
    evidence_count: int


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _scope_key(scope_type: str, scope_id: str) -> str:
    return _canonical({"scope_type": scope_type, "scope_id": scope_id})


def _target_item_id(source_hash: str, source_item_id: str) -> str:
    item_hash = hashlib.sha256(str(source_item_id).encode("utf-8")).hexdigest()
    return f"decision_{source_hash[:16]}{item_hash[:16]}"


def _open_legacy(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"legacy consolidation database not found: {path}")
    uri = path.as_uri() if path.as_posix().startswith("/") else path.as_posix()
    last_error: sqlite3.OperationalError | None = None
    for mode in ("ro", "ro&immutable=1"):
        try:
            conn = sqlite3.connect(f"file:{uri}?mode={mode}", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            return conn
        except sqlite3.OperationalError as exc:
            last_error = exc
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _legacy_store_hash(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT i.item_id, i.scope_id, i.kind, i.status, i.retention_class,
               i.pinned, i.origin, i.confidence, i.current_revision,
               r.claim, r.candidate_key
          FROM memory_items AS i
          JOIN memory_revisions AS r
            ON r.item_id = i.item_id
           AND r.revision = i.current_revision
         ORDER BY i.item_id, i.current_revision
        """
    ).fetchall()
    material = json.dumps([dict(row) for row in rows], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _list_current_memories(
    conn: sqlite3.Connection,
    *,
    include_archived: bool = False,
    limit: int,
) -> list[dict[str, Any]]:
    statuses = (
        ("active", "conflicted", "archived", "superseded")
        if include_archived
        else ("active", "conflicted")
    )
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"""
        SELECT i.item_id, i.scope_id, i.kind, i.status, i.retention_class,
               i.pinned, i.origin, i.confidence, i.current_revision,
               i.updated_at, r.claim, r.candidate_key
          FROM memory_items i
          JOIN memory_revisions r
            ON r.item_id = i.item_id AND r.revision = i.current_revision
         WHERE i.status IN ({placeholders})
         ORDER BY i.updated_at DESC, i.item_id
         LIMIT ?
        """,
        (*statuses, max(1, min(int(limit), 1_000))),
    ).fetchall()
    memories: list[dict[str, Any]] = []
    for row in rows:
        evidence = [
            dict(item)
            for item in conn.execute(
                """
                SELECT s.event_id, e.content, e.source_key, e.observed_at, e.created_at
                  FROM memory_sources s
                  JOIN memory_events e ON e.event_id = s.event_id
                 WHERE s.item_id = ? AND s.revision = ?
                 ORDER BY e.ingestion_seq, e.event_id
                """,
                (row["item_id"], int(row["current_revision"])),
            ).fetchall()
        ]
        memory = dict(row)
        memory["evidence"] = evidence
        memories.append(memory)
    return memories



def _decode_scope_id(value: str) -> tuple[str, str]:
    try:
        parsed = json.loads(value)
        return str(parsed.get("scope_type", "")), str(parsed.get("scope_id", ""))
    except Exception:
        return "", ""


def _is_session_derived_profile_scope(memory: Mapping[str, Any]) -> bool:
    scope_type, scope_id = _decode_scope_id(str(memory.get("scope_id", "")))
    return scope_type == "profile" and bool(_SESSION_SCOPE_RE.fullmatch(scope_id))


def _safe_text(value: Any, *, field_name: str, max_chars: int) -> str:
    raw = "" if value is None else str(value)
    try:
        return sanitize_for_storage(raw, field_name=field_name, max_chars=max_chars)[:max_chars]
    except Exception:
        return ""


def _effective_at(memory: Mapping[str, Any]) -> float:
    evidence = memory.get("evidence") or ()
    observed = [
        float(item.get("observed_at") or item.get("created_at") or 0.0)
        for item in evidence
        if item.get("observed_at") or item.get("created_at")
    ]
    if observed:
        return max(min(observed), time.time())
    updated_at = memory.get("updated_at")
    if isinstance(updated_at, (int, float)):
        return float(updated_at)
    return time.time()


def _plan_item(source_hash: str, memory: Mapping[str, Any]) -> MigrationPlanItem | None:
    claim = _safe_text(memory.get("claim"), field_name="legacy_decision_claim", max_chars=_MAX_CLAIM_CHARS)
    if not claim:
        return None
    title = claim[:120]
    summary_raw = (memory.get("claim") or "").strip()
    summary = _safe_text(
        summary_raw[: _MAX_SUMMARY_CHARS],
        field_name="legacy_decision_summary",
        max_chars=_MAX_SUMMARY_CHARS,
    ) or claim[:_MAX_SUMMARY_CHARS]
    rationale = (
        "Migrated from legacy memory consolidation. "
        "Review scope, source evidence, and current policy before approval."
    )
    return MigrationPlanItem(
        source_item_id=str(memory["item_id"]),
        source_revision=int(memory["current_revision"]),
        target_item_id=_target_item_id(source_hash, str(memory["item_id"])),
        title=title,
        summary=summary,
        statement=claim,
        rationale=rationale,
        effective_at=_effective_at(memory),
        evidence_count=len(memory.get("evidence") or ()),
    )


def plan_migration(
    *,
    source_path: str | Path,
    include_archived: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Build a dry-run migration report without writing to either database."""

    path = Path(source_path).expanduser().resolve()
    with _open_legacy(path) as conn:
        missing = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required = {"memory_items", "memory_revisions", "memory_events", "memory_sources"}
        if not required.issubset(missing):
            raise ValueError("legacy consolidation database is missing required tables")
        source_hash = _legacy_store_hash(conn)
        memories = _list_current_memories(
            conn,
            include_archived=include_archived,
            limit=limit,
        )

    counts = {
        "scanned": len(memories),
        "importable": 0,
        "skipped": 0,
        "conflicted": 0,
        "non_decision": 0,
        "session_derived_profile": 0,
    }
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for memory in memories:
        if memory["kind"] != "decision":
            counts["skipped"] += 1
            counts["non_decision"] += 1
            skipped.append(
                {
                    "source_item_id": memory["item_id"],
                    "source_revision": int(memory["current_revision"]),
                    "status": memory["status"],
                    "reason": "non_decision_kind",
                }
            )
            continue
        if memory["status"] == "conflicted":
            counts["skipped"] += 1
            counts["conflicted"] += 1
            skipped.append(
                {
                    "source_item_id": memory["item_id"],
                    "source_revision": int(memory["current_revision"]),
                    "status": memory["status"],
                    "reason": "conflicted_legacy_decision",
                }
            )
            continue
        if _is_session_derived_profile_scope(memory):
            counts["skipped"] += 1
            counts["session_derived_profile"] += 1
            skipped.append(
                {
                    "source_item_id": memory["item_id"],
                    "source_revision": int(memory["current_revision"]),
                    "status": memory["status"],
                    "scope_type": _decode_scope_id(str(memory.get("scope_id", "")))[0],
                    "scope_id": _decode_scope_id(str(memory.get("scope_id", "")))[1],
                    "reason": "session_derived_profile_scope",
                }
            )
            continue
        planned = _plan_item(source_hash, memory)
        if planned is None:
            counts["skipped"] += 1
            skipped.append(
                {
                    "source_item_id": memory["item_id"],
                    "source_revision": int(memory["current_revision"]),
                    "status": memory["status"],
                    "reason": "claim_blocked_or_empty",
                }
            )
            continue
        counts["importable"] += 1
        items.append(
            {
                "source_item_id": planned.source_item_id,
                "source_revision": planned.source_revision,
                "target_item_id": planned.target_item_id,
                "title": planned.title,
                "summary": planned.summary,
                "statement": planned.statement,
                "rationale": planned.rationale,
                "effective_at": planned.effective_at,
                "evidence_count": planned.evidence_count,
            }
        )
    return {
        "source_path": str(path),
        "source_system": _SOURCE_SYSTEM,
        "source_store_hash": source_hash,
        "target_scope": {
            "principal_id": LOCAL_OWNER_PRINCIPAL,
            "scope_type": "profile",
            "scope_id": _PROFILE_SCOPE_ID,
        },
        "include_archived": include_archived,
        "counts": counts,
        "items": items,
        "skipped": skipped,
    }


def apply_migration(
    *,
    source_path: str | Path,
    target_path: str | Path,
    include_archived: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Apply a dry-run report as candidate-only ExperienceStore records."""

    report = plan_migration(
        source_path=source_path,
        include_archived=include_archived,
        limit=limit,
    )
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    with ExperienceStore(target_path) as store:
        for item in report["items"]:
            existing = store.get_decision(item["target_item_id"])
            if existing is not None:
                disposition = "imported_candidate"
                store.record_migration_source(
                    source_system=_SOURCE_SYSTEM,
                    source_store_hash=report["source_store_hash"],
                    source_item_id=item["source_item_id"],
                    source_revision=int(item["source_revision"]),
                    target_item_id=item["target_item_id"],
                    target_revision=int(existing["current_revision"]),
                    disposition=disposition,
                    reason_code="already_imported",
                )
                skipped.append({**item, "reason": "already_imported"})
                continue
            try:
                created = store.create_decision(
                    principal_id=LOCAL_OWNER_PRINCIPAL,
                    scope_type="profile",
                    scope_id=_PROFILE_SCOPE_ID,
                    repository_id=None,
                    project_id=None,
                    item_id=item["target_item_id"],
                    idempotency_key=(
                        f"retrieved:{report['source_store_hash'][:16]}:"
                        f"{item['target_item_id']}"
                    ),
                    title=item["title"],
                    summary=item["summary"],
                    body={
                        "statement": item["statement"],
                        "rationale": item["rationale"],
                        "source_type": "migration",
                        "authority": "unapproved",
                        "effective_at": float(item["effective_at"]),
                    },
                    tags={"component": ["memory_consolidation"]},
                    created_by="import",
                    source_session_id=_SOURCE_SYSTEM,
                    source_turn_id=item["source_item_id"],
                    source_work_id=f"revision:{item['source_revision']}",
                    source_hash=report["source_store_hash"],
                    created_at=float(item["effective_at"]),
                )
                store.record_migration_source(
                    source_system=_SOURCE_SYSTEM,
                    source_store_hash=report["source_store_hash"],
                    source_item_id=item["source_item_id"],
                    source_revision=int(item["source_revision"]),
                    target_item_id=item["target_item_id"],
                    target_revision=int(created["current_revision"]),
                    disposition="imported_candidate",
                    reason_code="candidate_imported",
                    imported_at=time.time(),
                )
                applied.append({**item, "target_revision": created["current_revision"]})
            except Exception as exc:
                store.record_migration_source(
                    source_system=_SOURCE_SYSTEM,
                    source_store_hash=report["source_store_hash"],
                    source_item_id=item["source_item_id"],
                    source_revision=int(item["source_revision"]),
                    target_item_id=item["target_item_id"],
                    target_revision=None,
                    disposition="needs_manual_review",
                    reason_code=f"import_failed:{type(exc).__name__}",
                    imported_at=time.time(),
                )
                skipped.append({**item, "reason": f"import_failed:{type(exc).__name__}"})
    return {
        **report,
        "applied": applied,
        "skipped": [*report["skipped"], *skipped],
        "counts": {
            **report["counts"],
            "applied": len(applied),
            "already_imported": sum(
                1 for item in skipped if item.get("reason") == "already_imported"
            ),
            "needs_manual_review": sum(
                1 for item in skipped
                if item.get("reason") == "needs_manual_review"
                or str(item.get("reason", "")).startswith("import_failed:")
            ),
        },
    }


__all__ = ["apply_migration", "plan_migration"]

"""SQLite persistence for the Work Experience validation MVP.

``ExperienceStore`` deliberately accepts an explicit, already-resolved
``state.db`` path.  It does not consult Marlow profile configuration or expose
session-message APIs.  The facade owns only additive ``experience_*`` tables,
immutable lesson revisions, lifecycle transitions, scoped retrieval, and the
small diagnostic ledger used by MVP0.

Text is sanitized on both sides of the database boundary.  Callers may inject
sanitizer functions for tests; production defaults are imported lazily from
``agent.experience.safety`` to keep the package dependency graph acyclic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import re
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from agent.experience.anchors import validate_repository_anchor
from agent.experience.authority import DecisionTurnAuthority


logger = logging.getLogger(__name__)

T = TypeVar("T")
_UNSET = object()
_CURRENT_SCHEMA_VERSION = "5"
_CURRENT_FTS_VERSION = "2"
_CURRENT_SCHEMA_TABLES = frozenset(
    {
        "experience_schema_meta",
        "experience_items",
        "experience_item_revisions",
        "experience_scope_policies",
        "experience_tags",
        "experience_retrievals",
        "experience_retrieval_items",
        "experience_events",
        "experience_search_content",
        "experience_links",
        "experience_migration_sources",
    }
)

_LESSON_STATUSES = frozenset(
    {"candidate", "active", "disputed", "deprecated", "rejected", "retracted"}
)
_DECISION_STATUSES = frozenset(
    {"candidate", "active", "review_required", "superseded", "revoked"}
)
_SCOPE_TYPES = frozenset({"project", "repository", "profile"})
_SENSITIVITIES = frozenset({"normal", "private_repo", "local_only", "blocked"})
_EGRESS_POLICIES = frozenset(
    {"local_only", "same_provider_trust_domain", "explicit_any_provider"}
)
_CREATED_BY = frozenset({"user", "agent", "import"})
_TAG_NAMESPACES = frozenset(
    {"task_type", "technology", "entity", "failure", "component"}
)
_RETRIEVAL_DISPOSITIONS = frozenset({"retrieved"})
_EVENT_TYPES = frozenset(
    {
        "approved",
        "edited",
        "disputed",
        "deprecated",
        "rejected",
        "retracted",
        "retrieved",
        "candidate_created",
        "activated",
        "review_required",
        "reapproved",
        "anchor_invalidated",
        "superseded",
        "revoked",
        "disclosed",
        "declared_applied",
        "overridden",
        "not_applicable",
        "migration_imported",
        "migration_skipped",
        "relation_added",
    }
)
_LESSON_TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"active", "rejected", "retracted"}),
    "active": frozenset({"disputed", "deprecated", "retracted"}),
    "disputed": frozenset({"deprecated", "retracted"}),
    "deprecated": frozenset(),
    "rejected": frozenset(),
    "retracted": frozenset(),
}
_DECISION_TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"review_required", "revoked"}),
    "active": frozenset({"review_required", "superseded", "revoked"}),
    "review_required": frozenset({"revoked", "superseded"}),
    "superseded": frozenset(),
    "revoked": frozenset(),
}
_MEANINGFUL_DECISION_BODY_FIELDS = frozenset(
    {
        "statement",
        "rationale",
        "source_type",
        "authority",
        "effective_at",
        "expires_at",
        "policy_anchor_path",
        "policy_anchor_hash",
    }
)

_MAX_TITLE_CHARS = 240
_MAX_SUMMARY_CHARS = 2_000
_MAX_BODY_JSON_BYTES = 32_000
_MAX_PRODUCER_JSON_BYTES = 4_000
_MAX_METADATA_JSON_BYTES = 8_000
_MAX_EVENT_JSON_BYTES = 8_000
_MAX_REASON_CHARS = 1_000
_MAX_TAG_CHARS = 160

_SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_SAFE_TYPED_DIGEST_ID_RE = re.compile(
    r"(?:repo|project|workspace)_[0-9a-f]{64}\Z"
)
_SAFE_GENERATED_ID_RE = re.compile(
    r"(?:turn|attempt|retrieval|event|lesson|decision)_[0-9a-f]{32,64}\Z"
)
_SAFE_INTERNAL_IDEMPOTENCY_RE = re.compile(
    r"retrieved:[A-Za-z0-9._-]+:[A-Za-z0-9._-]+\Z"
)
_SAFE_METADATA_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")

_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "chain_of_thought",
        "command_output",
        "conversation",
        "diff",
        "logs",
        "patch",
        "raw_input",
        "raw_request",
        "raw_text",
        "reasoning",
        "stderr",
        "stdout",
        "system_prompt",
        "tool_output",
        "transcript",
    }
)

_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.020
_WRITE_RETRY_MAX_S = 0.150
_CHECKPOINT_EVERY_N_WRITES = 50

_FTS_TRIGGER_NAMES = (
    "experience_search_content_ai",
    "experience_search_content_ad",
    "experience_search_content_au",
    "experience_search_trigram_ai",
    "experience_search_trigram_ad",
    "experience_search_trigram_au",
)


_BASE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS experience_schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience_items (
        id TEXT PRIMARY KEY,
        family_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('work_record', 'lesson', 'decision')),
        current_status TEXT NOT NULL,
        current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
        principal_id TEXT NOT NULL CHECK (length(principal_id) > 0),
        scope_type TEXT NOT NULL CHECK (scope_type IN ('project', 'repository', 'profile')),
        scope_id TEXT NOT NULL CHECK (length(scope_id) > 0),
        repository_id TEXT,
        project_id TEXT,
        sensitivity TEXT NOT NULL CHECK (
            sensitivity IN ('normal', 'private_repo', 'local_only', 'blocked')
        ),
        egress_policy TEXT NOT NULL CHECK (
            egress_policy IN (
                'local_only', 'same_provider_trust_domain', 'explicit_any_provider'
            )
        ),
        producer_trust_domain TEXT,
        created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent', 'import')),
        idempotency_key TEXT UNIQUE,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        deleted_at REAL,
        CHECK (
            (kind = 'lesson' AND current_status IN (
                'candidate', 'active', 'disputed', 'deprecated', 'rejected', 'retracted'
            )) OR
            (kind = 'work_record' AND current_status IN ('recorded', 'archived')) OR
            (kind = 'decision' AND current_status IN (
                'candidate', 'active', 'review_required', 'superseded', 'revoked'
            ))
        ),
        CHECK (
            (scope_type = 'project' AND repository_id IS NOT NULL AND project_id IS NOT NULL)
            OR (scope_type = 'repository' AND repository_id IS NOT NULL)
            OR scope_type = 'profile'
        ),
        FOREIGN KEY (id, current_revision)
            REFERENCES experience_item_revisions(item_id, revision)
            DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience_item_revisions (
        item_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        body_json TEXT NOT NULL,
        searchable_text TEXT NOT NULL,
        confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
        source_session_id TEXT,
        source_turn_id TEXT,
        source_work_id TEXT,
        source_hash TEXT,
        content_hash TEXT NOT NULL,
        editor TEXT NOT NULL,
        edit_reason TEXT,
        producer_json TEXT NOT NULL,
        idempotency_key TEXT,
        created_at REAL NOT NULL,
        last_validated_at REAL,
        review_after REAL,
        PRIMARY KEY (item_id, revision),
        UNIQUE (item_id, idempotency_key),
        FOREIGN KEY (item_id) REFERENCES experience_items(id) ON DELETE CASCADE
            DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience_scope_policies (
        principal_id TEXT NOT NULL,
        repository_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        project_root_rel TEXT NOT NULL,
        workspace_root TEXT,
        capture_allowed INTEGER NOT NULL DEFAULT 0 CHECK (capture_allowed IN (0, 1)),
        recall_allowed INTEGER NOT NULL DEFAULT 0 CHECK (recall_allowed IN (0, 1)),
        injection_allowed INTEGER NOT NULL DEFAULT 0 CHECK (injection_allowed IN (0, 1)),
        reflection_allowed INTEGER NOT NULL DEFAULT 0 CHECK (reflection_allowed IN (0, 1)),
        max_egress_policy TEXT NOT NULL DEFAULT 'local_only' CHECK (
            max_egress_policy IN (
                'local_only', 'same_provider_trust_domain', 'explicit_any_provider'
            )
        ),
        updated_at REAL NOT NULL,
        PRIMARY KEY (principal_id, repository_id, project_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience_tags (
        item_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        namespace TEXT NOT NULL CHECK (
            namespace IN ('task_type', 'technology', 'entity', 'failure', 'component')
        ),
        value TEXT NOT NULL,
        PRIMARY KEY (item_id, revision, namespace, value),
        FOREIGN KEY (item_id, revision)
            REFERENCES experience_item_revisions(item_id, revision) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience_links (
        from_item_id TEXT NOT NULL,
        from_revision INTEGER NOT NULL,
        relation TEXT NOT NULL CHECK (
            relation IN (
                'evidence_for', 'derived_from', 'contradicts', 'supersedes',
                'duplicate_of', 'continues'
            )
        ),
        to_item_id TEXT NOT NULL,
        to_revision INTEGER NOT NULL,
        created_at REAL NOT NULL,
        metadata_json TEXT NOT NULL,
        PRIMARY KEY (
            from_item_id, from_revision, relation, to_item_id, to_revision
        ),
        FOREIGN KEY (from_item_id, from_revision)
            REFERENCES experience_item_revisions(item_id, revision) ON DELETE CASCADE,
        FOREIGN KEY (to_item_id, to_revision)
            REFERENCES experience_item_revisions(item_id, revision) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience_retrievals (
        id TEXT PRIMARY KEY,
        turn_id TEXT NOT NULL,
        work_id TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        repository_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        task_signature_hash TEXT NOT NULL,
        provider_trust_domain TEXT NOT NULL,
        idempotency_key TEXT UNIQUE,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience_retrieval_items (
        retrieval_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        item_revision INTEGER NOT NULL,
        rank INTEGER NOT NULL CHECK (rank >= 1),
        score REAL NOT NULL,
        match_reasons_json TEXT NOT NULL,
        disposition TEXT NOT NULL CHECK (disposition = 'retrieved'),
        PRIMARY KEY (retrieval_id, item_id),
        FOREIGN KEY (retrieval_id) REFERENCES experience_retrievals(id) ON DELETE CASCADE,
        FOREIGN KEY (item_id, item_revision)
            REFERENCES experience_item_revisions(item_id, revision) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience_events (
        id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL CHECK (
            event_type IN (
                'approved', 'edited', 'disputed', 'deprecated', 'rejected',
                'retracted', 'retrieved', 'candidate_created', 'activated',
                'review_required', 'reapproved', 'anchor_invalidated',
                'superseded', 'revoked', 'disclosed', 'declared_applied',
                'overridden', 'not_applicable', 'migration_imported',
                'migration_skipped', 'relation_added'
            )
        ),
        item_id TEXT,
        item_revision INTEGER,
        retrieval_id TEXT,
        work_id TEXT,
        payload_json TEXT NOT NULL,
        idempotency_key TEXT UNIQUE,
        created_at REAL NOT NULL,
        CHECK (
            (item_id IS NULL AND item_revision IS NULL)
            OR (item_id IS NOT NULL AND item_revision IS NOT NULL)
        ),
        FOREIGN KEY (item_id, item_revision)
            REFERENCES experience_item_revisions(item_id, revision) ON DELETE CASCADE,
        FOREIGN KEY (retrieval_id) REFERENCES experience_retrievals(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience_migration_sources (
        source_system TEXT NOT NULL,
        source_store_hash TEXT NOT NULL,
        source_item_id TEXT NOT NULL,
        source_revision INTEGER NOT NULL,
        target_item_id TEXT,
        target_revision INTEGER,
        disposition TEXT NOT NULL CHECK (
            disposition IN (
                'imported_candidate',
                'skipped',
                'needs_manual_review'
            )
        ),
        reason_code TEXT,
        imported_at REAL NOT NULL,
        PRIMARY KEY (
            source_system,
            source_store_hash,
            source_item_id,
            source_revision
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience_search_content (
        rowid INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        searchable_text TEXT NOT NULL,
        tags TEXT NOT NULL DEFAULT '',
        UNIQUE (item_id, revision),
        FOREIGN KEY (item_id, revision)
            REFERENCES experience_item_revisions(item_id, revision) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_experience_items_scope_status
        ON experience_items(
            principal_id, repository_id, project_id, scope_type, scope_id,
            current_status, deleted_at
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_experience_items_family
        ON experience_items(family_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_experience_links_to
        ON experience_links(to_item_id, to_revision, relation)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_experience_tags_lookup
        ON experience_tags(namespace, value, item_id, revision)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_experience_retrievals_created
        ON experience_retrievals(created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_experience_events_created
        ON experience_events(created_at)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS experience_revision_immutable
    BEFORE UPDATE ON experience_item_revisions
    BEGIN
        SELECT RAISE(ABORT, 'experience revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS experience_revision_search_insert
    AFTER INSERT ON experience_item_revisions
    BEGIN
        INSERT INTO experience_search_content(
            item_id, revision, kind, title, searchable_text, tags
        )
        SELECT new.item_id, new.revision, i.kind, new.title,
               new.searchable_text, ''
        FROM experience_items AS i
        WHERE i.id = new.item_id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS experience_tag_search_insert
    AFTER INSERT ON experience_tags
    BEGIN
        UPDATE experience_search_content
        SET tags = COALESCE((
            SELECT group_concat(value, ' ')
            FROM (
                SELECT value
                FROM experience_tags
                WHERE item_id = new.item_id AND revision = new.revision
                ORDER BY namespace, value
            )
        ), '')
        WHERE item_id = new.item_id AND revision = new.revision;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS experience_tag_search_delete
    AFTER DELETE ON experience_tags
    BEGIN
        UPDATE experience_search_content
        SET tags = COALESCE((
            SELECT group_concat(value, ' ')
            FROM (
                SELECT value
                FROM experience_tags
                WHERE item_id = old.item_id AND revision = old.revision
                ORDER BY namespace, value
            )
        ), '')
        WHERE item_id = old.item_id AND revision = old.revision;
    END
    """,
)


_FTS_SCHEMA_STATEMENTS = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS experience_search USING fts5(
        title,
        searchable_text,
        tags,
        content='experience_search_content',
        content_rowid='rowid',
        tokenize='unicode61'
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS experience_search_trigram USING fts5(
        title,
        searchable_text,
        tags,
        content='experience_search_content',
        content_rowid='rowid',
        tokenize='trigram'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS experience_search_content_ai
    AFTER INSERT ON experience_search_content
    BEGIN
        INSERT INTO experience_search(rowid, title, searchable_text, tags)
        VALUES (new.rowid, new.title, new.searchable_text, new.tags);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS experience_search_content_ad
    AFTER DELETE ON experience_search_content
    BEGIN
        INSERT INTO experience_search(
            experience_search, rowid, title, searchable_text, tags
        ) VALUES ('delete', old.rowid, old.title, old.searchable_text, old.tags);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS experience_search_trigram_ai
    AFTER INSERT ON experience_search_content
    BEGIN
        INSERT INTO experience_search_trigram(rowid, title, searchable_text, tags)
        VALUES (new.rowid, new.title, new.searchable_text, new.tags);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS experience_search_trigram_ad
    AFTER DELETE ON experience_search_content
    BEGIN
        INSERT INTO experience_search_trigram(
            experience_search_trigram, rowid, title, searchable_text, tags
        ) VALUES ('delete', old.rowid, old.title, old.searchable_text, old.tags);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS experience_search_content_au
    AFTER UPDATE ON experience_search_content
    BEGIN
        INSERT INTO experience_search(
            experience_search, rowid, title, searchable_text, tags
        ) VALUES ('delete', old.rowid, old.title, old.searchable_text, old.tags);
        INSERT INTO experience_search(rowid, title, searchable_text, tags)
        VALUES (new.rowid, new.title, new.searchable_text, new.tags);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS experience_search_trigram_au
    AFTER UPDATE ON experience_search_content
    BEGIN
        INSERT INTO experience_search_trigram(
            experience_search_trigram, rowid, title, searchable_text, tags
        ) VALUES ('delete', old.rowid, old.title, old.searchable_text, old.tags);
        INSERT INTO experience_search_trigram(rowid, title, searchable_text, tags)
        VALUES (new.rowid, new.title, new.searchable_text, new.tags);
    END
    """,
)


def _enum_value(value: Any) -> Any:
    """Return the scalar value of a string enum without importing models."""
    return getattr(value, "value", value)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now(value: float | None = None) -> float:
    timestamp = float(time.time() if value is None else value)
    if timestamp < 0 or not math.isfinite(timestamp):
        raise ValueError("timestamp must be finite and non-negative")
    return timestamp


def _require_choice(name: str, value: Any, choices: frozenset[str]) -> str:
    normalized = str(_enum_value(value)).strip().lower()
    if normalized == "proposed" and name == "status":
        normalized = "candidate"
    if normalized not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"invalid {name}: {normalized!r}; expected one of {allowed}")
    return normalized


def _validate_confidence(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("confidence must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return normalized


def _optional_timestamp(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{field} must be numeric")
    timestamp = float(value)
    if timestamp < 0 or not math.isfinite(timestamp):
        raise ValueError(f"{field} must be finite and non-negative")
    return timestamp


_FTS_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "by",
        "can",
        "could",
        "for",
        "from",
        "i",
        "in",
        "into",
        "is",
        "it",
        "its",
        "my",
        "of",
        "on",
        "or",
        "our",
        "please",
        "should",
        "that",
        "the",
        "then",
        "this",
        "to",
        "was",
        "we",
        "were",
        "when",
        "with",
        "would",
        "you",
        "your",
    }
)


def _fts_term_root(token: str) -> str:
    """Return a conservative English morphology root for prefix matching."""

    value = token.casefold()
    if not value.isascii() or not value.isalnum():
        return value
    if value.endswith("ies") and len(value) > 6:
        return value[:-3] + "y"
    for suffix in ("izations", "ization", "ations", "ation", "ments", "ment", "ions", "ion", "ing", "ed"):
        if value.endswith(suffix) and len(value) - len(suffix) >= 4:
            value = value[: -len(suffix)]
            if len(value) >= 2 and value[-1] == value[-2]:
                value = value[:-1]
            return value
    if value.endswith("s") and not value.endswith(("ss", "us")) and len(value) > 4:
        return value[:-1]
    return value


def _fts_query_terms(query: str, *, limit: int = 32) -> tuple[str, ...]:
    """Extract bounded, unique, non-boilerplate terms without retaining text."""

    result: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[^\W_]+", query, flags=re.UNICODE):
        folded = raw.casefold()
        if folded in _FTS_STOP_WORDS or (len(folded) < 3 and folded.isascii()):
            continue
        root = _fts_term_root(folded)
        if not root or root in seen:
            continue
        seen.add(root)
        result.append(root)
        if len(result) >= limit:
            break
    return tuple(result)


def _is_cjk_codepoint(codepoint: int) -> bool:
    return (
        0x4E00 <= codepoint <= 0x9FFF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x3000 <= codepoint <= 0x303F
        or 0x3040 <= codepoint <= 0x309F
        or 0x30A0 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _contains_cjk(text: str) -> bool:
    return any(_is_cjk_codepoint(ord(char)) for char in text)


def _count_cjk(text: str) -> int:
    return sum(1 for char in text if _is_cjk_codepoint(ord(char)))


def _short_cjk_terms(query: str, *, limit: int = 8) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[^\W_]+", query, flags=re.UNICODE):
        if not _contains_cjk(raw):
            continue
        count = _count_cjk(raw)
        if 1 <= count < 3 and raw not in seen:
            seen.add(raw)
            terms.append(raw)
            if len(terms) >= limit:
                break
    return tuple(terms)


def _sanitize_trigram_query(query: str) -> str:
    """Build a deterministic trigram query from CJK tokens of 3+ chars."""

    parts: list[str] = []
    for raw in re.findall(r"[^\W_]+", query, flags=re.UNICODE):
        if not _contains_cjk(raw):
            continue
        folded = raw.casefold()
        if folded in {"and", "or", "not"}:
            continue
        if _short_cjk_terms(raw):
            continue
        parts.append(f'"{raw}"')
    return " OR ".join(dict.fromkeys(parts))


def _sanitize_fts_query(query: str) -> str:
    """Make free text a literal, deterministic FTS5 prefix-OR query.

    Candidate generation optimizes recall; the caller separately requires
    meaningful term overlap (or exact structured metadata) before a lesson is
    eligible. This avoids the old all-terms query, which made natural-language
    paraphrases almost impossible to retrieve.
    """

    return " OR ".join(f'"{term}"*' for term in _fts_query_terms(query))


def _default_storage_sanitizer(text: str) -> str:
    from agent.experience.safety import sanitize_for_storage

    return sanitize_for_storage(text)


def _default_return_sanitizer(text: str) -> str:
    from agent.experience.safety import sanitize_for_return

    return sanitize_for_return(text)


class ExperienceSchemaNotCurrentError(RuntimeError):
    """The experience schema needs initialization or migration."""


class ExperienceStore:
    """Narrow, profile-local storage facade for approved work experience.

    Args:
        state_db_path: Absolute path selected by the profile owner.  There is
            intentionally no default.
        sanitizer: Optional supplemental write hook composed inside the forced
            sanitizer; it cannot replace the mandatory boundary.
        return_sanitizer: Optional supplemental read hook, also wrapped by the
            mandatory boundary.
    """

    def __init__(
        self,
        state_db_path: str | Path,
        *,
        sanitizer: Callable[[str], str] | None = None,
        return_sanitizer: Callable[[str], str] | None = None,
        initialize_schema: bool = True,
        current_schema_only: bool = False,
    ) -> None:
        if state_db_path is None:
            raise TypeError("state_db_path is required")
        if not isinstance(initialize_schema, bool):
            raise TypeError("initialize_schema must be bool")
        if not isinstance(current_schema_only, bool):
            raise TypeError("current_schema_only must be bool")
        if initialize_schema and current_schema_only:
            raise ValueError(
                "current_schema_only cannot be combined with initialize_schema"
            )
        self.db_path = Path(state_db_path)
        if not self.db_path.is_absolute():
            raise ValueError("state_db_path must be an explicit absolute path")
        if self.db_path != self.db_path.resolve(strict=False):
            raise ValueError("state_db_path must be explicitly resolved")
        if current_schema_only and not self.db_path.is_file():
            raise ExperienceSchemaNotCurrentError(
                "experience state database does not exist"
            )
        if not current_schema_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.db_path.parent, 0o700)
        if os.name != "nt" and stat.S_IMODE(self.db_path.parent.stat().st_mode) != 0o700:
            raise PermissionError("experience state directory must be owner-only")

        self._sanitize_write_hook = sanitizer
        self._sanitize_return_hook = return_sanitizer
        self._lock = threading.Lock()
        self._write_count = 0
        self._fts_enabled = False
        self._closed = False
        self._initialize_schema = initialize_schema
        self._current_schema_only = current_schema_only

        if not self._initialize_schema and not self.db_path.is_file():
            raise FileNotFoundError("experience state database does not exist")

        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        try:
            if not self._current_schema_only:
                os.chmod(self.db_path, 0o600)
            if os.name != "nt" and stat.S_IMODE(self.db_path.stat().st_mode) != 0o600:
                raise PermissionError("experience state database must be owner-only")
            if self._current_schema_only:
                self._conn.execute("PRAGMA foreign_keys=ON")
            else:
                self._configure_connection()
            if self._initialize_schema:
                self._init_schema()
            elif self._current_schema_only:
                self._verify_current_schema()
            elif self._conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'experience_items'"
            ).fetchone() is None:
                raise RuntimeError("experience schema is not initialized")
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    @classmethod
    def open_current(
        cls,
        state_db_path: str | Path,
        *,
        sanitizer: Callable[[str], str] | None = None,
        return_sanitizer: Callable[[str], str] | None = None,
    ) -> "ExperienceStore":
        """Open the current schema after read-only validation, without setup writes."""

        return cls(
            state_db_path,
            sanitizer=sanitizer,
            return_sanitizer=return_sanitizer,
            initialize_schema=False,
            current_schema_only=True,
        )

    @property
    def fts_enabled(self) -> bool:
        """Whether this Python SQLite runtime has a usable FTS5 module."""
        return self._fts_enabled

    @property
    def closed(self) -> bool:
        """Whether this facade's SQLite connection has been closed."""

        return self._closed

    def __enter__(self) -> "ExperienceStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _configure_connection(self) -> None:
        # Import lazily: importing marlow_state at this module's import time
        # would resolve a default profile path, which this store must never do.
        from marlow_state import apply_wal_with_fallback

        apply_wal_with_fallback(self._conn, db_label=str(self.db_path))
        self._conn.execute("PRAGMA foreign_keys=ON")

    def _connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("ExperienceStore is closed")
        return self._conn

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run one ``BEGIN IMMEDIATE`` transaction with SessionDB-style retry."""
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(_WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    conn = self._connection()
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(conn)
                        conn.commit()
                    except BaseException:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        raise
                self._write_count += 1
                if self._write_count % _CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                last_error = exc
                if attempt == _WRITE_MAX_RETRIES - 1:
                    raise
                time.sleep(random.uniform(_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S))
        raise last_error or sqlite3.OperationalError(
            "database is locked after max retries"
        )

    def _try_wal_checkpoint(self) -> bool:
        try:
            with self._lock:
                self._connection().execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            return True
        except Exception:
            return False

    @staticmethod
    def _fts_unavailable(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).lower()
        return "no such module" in message and "fts5" in message

    def _init_schema(self) -> None:
        fts_unavailable_error: sqlite3.OperationalError | None = None
        try:
            with self._lock:
                cursor = self._connection().cursor()
                cursor.execute("CREATE VIRTUAL TABLE temp._experience_fts_probe USING fts5(x)")
                cursor.execute("DROP TABLE temp._experience_fts_probe")
        except sqlite3.OperationalError as exc:
            if not self._fts_unavailable(exc):
                raise
            # Drop old FTS-writing triggers before any shadow-table repair.
            # Otherwise a database created by an FTS-capable runtime could
            # make ordinary writes fail when reopened without the module.
            self._disable_fts_triggers()
            fts_unavailable_error = exc

        def create_base(conn: sqlite3.Connection) -> None:
            for statement in _BASE_SCHEMA_STATEMENTS:
                conn.execute(statement)
            policy_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(experience_scope_policies)"
                ).fetchall()
            }
            if "recall_allowed" not in policy_columns:
                conn.execute(
                    "ALTER TABLE experience_scope_policies "
                    "ADD COLUMN recall_allowed INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (recall_allowed IN (0, 1))"
                )
            self._migrate_to_current_schema(conn)
            conn.execute(
                "INSERT INTO experience_schema_meta(key, value) VALUES('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ,
                (_CURRENT_SCHEMA_VERSION,),
            )
            # Repair shadow rows if an earlier process stopped before optional
            # FTS initialization.  The shadow is ordinary SQLite and remains
            # useful even when FTS5 is unavailable.
            conn.execute(
                """
                INSERT OR IGNORE INTO experience_search_content(
                    item_id, revision, kind, title, searchable_text, tags
                )
                SELECT r.item_id, r.revision, i.kind, r.title,
                       r.searchable_text,
                       COALESCE((
                           SELECT group_concat(t.value, ' ')
                           FROM (
                               SELECT value
                               FROM experience_tags
                               WHERE item_id = r.item_id AND revision = r.revision
                               ORDER BY namespace, value
                           ) AS t
                       ), '')
                FROM experience_item_revisions AS r
                JOIN experience_items AS i ON i.id = r.item_id
                """
            )

        self._execute_write(create_base)
        if fts_unavailable_error is not None:
            logger.warning(
                "SQLite FTS5 unavailable; experience retrieval uses metadata only"
            )
            return

        def create_fts(conn: sqlite3.Connection) -> None:
            existed = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'experience_search'"
            ).fetchone() is not None
            for statement in _FTS_SCHEMA_STATEMENTS:
                conn.execute(statement)
            rebuild_marker = conn.execute(
                "SELECT value FROM experience_schema_meta "
                "WHERE key = 'fts_rebuild_version'"
            ).fetchone()
            # Rebuild only when the index was created or a no-FTS runtime
            # deliberately invalidated the marker. Normal opens remain O(1)
            # and do not take an index-sized write lock in shared state.db.
            if (
                not existed
                or rebuild_marker is None
                or rebuild_marker["value"] != _CURRENT_FTS_VERSION
            ):
                conn.execute(
                    "INSERT INTO experience_search(experience_search) VALUES('rebuild')"
                )
                conn.execute(
                    "INSERT INTO experience_schema_meta(key, value) "
                    "VALUES('fts_rebuild_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (_CURRENT_FTS_VERSION,),
                )

        try:
            self._execute_write(create_fts)
        except sqlite3.OperationalError as exc:
            if not self._fts_unavailable(exc):
                raise
            self._disable_fts_triggers()
            logger.warning(
                "SQLite FTS5 became unavailable; experience retrieval uses metadata only"
            )
            return
        self._fts_enabled = True

    @staticmethod
    def _rebuild_v3_items_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS experience_items_v3 (
                id TEXT PRIMARY KEY,
                family_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('work_record', 'lesson', 'decision')),
                current_status TEXT NOT NULL,
                current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
                principal_id TEXT NOT NULL CHECK (length(principal_id) > 0),
                scope_type TEXT NOT NULL CHECK (scope_type IN ('project', 'repository', 'profile')),
                scope_id TEXT NOT NULL CHECK (length(scope_id) > 0),
                repository_id TEXT,
                project_id TEXT,
                sensitivity TEXT NOT NULL CHECK (
                    sensitivity IN ('normal', 'private_repo', 'local_only', 'blocked')
                ),
                egress_policy TEXT NOT NULL CHECK (
                    egress_policy IN (
                        'local_only', 'same_provider_trust_domain', 'explicit_any_provider'
                    )
                ),
                producer_trust_domain TEXT,
                created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent', 'import')),
                idempotency_key TEXT UNIQUE,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                deleted_at REAL,
                CHECK (
                    (kind = 'lesson' AND current_status IN (
                        'candidate', 'active', 'disputed', 'deprecated', 'rejected', 'retracted'
                    )) OR
                    (kind = 'work_record' AND current_status IN ('recorded', 'archived')) OR
                    (kind = 'decision' AND current_status IN (
                        'candidate', 'active', 'review_required', 'superseded', 'revoked'
                    ))
                ),
                CHECK (
                    (scope_type = 'project' AND repository_id IS NOT NULL AND project_id IS NOT NULL)
                    OR (scope_type = 'repository' AND repository_id IS NOT NULL)
                    OR scope_type = 'profile'
                ),
                FOREIGN KEY (id, current_revision)
                    REFERENCES experience_item_revisions(item_id, revision)
                    DEFERRABLE INITIALLY DEFERRED
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO experience_items_v3(
                id, family_id, kind, current_status, current_revision,
                principal_id, scope_type, scope_id, repository_id, project_id,
                sensitivity, egress_policy, producer_trust_domain, created_by,
                idempotency_key, created_at, updated_at, deleted_at
            )
            SELECT
                id, family_id, kind, current_status, current_revision,
                principal_id, scope_type, scope_id, repository_id, project_id,
                sensitivity, egress_policy, producer_trust_domain, created_by,
                idempotency_key, created_at, updated_at, deleted_at
            FROM experience_items
            """
        )
        conn.execute("DROP TABLE experience_items")
        conn.execute("ALTER TABLE experience_items_v3 RENAME TO experience_items")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_experience_items_scope_status
                ON experience_items(
                    principal_id, repository_id, project_id, scope_type, scope_id,
                    current_status, deleted_at
                )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_experience_items_family
                ON experience_items(family_id, created_at)
            """
        )

    @staticmethod
    def _rebuild_v3_tags_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS experience_tags_v3 (
                item_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                namespace TEXT NOT NULL CHECK (
                    namespace IN ('task_type', 'technology', 'entity', 'failure', 'component')
                ),
                value TEXT NOT NULL,
                PRIMARY KEY (item_id, revision, namespace, value),
                FOREIGN KEY (item_id, revision)
                    REFERENCES experience_item_revisions(item_id, revision) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO experience_tags_v3(item_id, revision, namespace, value)
            SELECT item_id, revision, namespace, value
            FROM experience_tags
            """
        )
        conn.execute("DROP TABLE experience_tags")
        conn.execute("ALTER TABLE experience_tags_v3 RENAME TO experience_tags")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_experience_tags_lookup
                ON experience_tags(namespace, value, item_id, revision)
            """
        )

    @staticmethod
    def _rebuild_v3_events_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS experience_events_v3 (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL CHECK (
                    event_type IN (
                        'approved', 'edited', 'disputed', 'deprecated', 'rejected',
                        'retracted', 'retrieved', 'candidate_created', 'activated',
                        'review_required', 'reapproved', 'anchor_invalidated',
                        'superseded', 'revoked', 'disclosed', 'declared_applied',
                        'overridden', 'not_applicable', 'migration_imported',
                        'migration_skipped', 'relation_added'
                    )
                ),
                item_id TEXT,
                item_revision INTEGER,
                retrieval_id TEXT,
                work_id TEXT,
                payload_json TEXT NOT NULL,
                idempotency_key TEXT UNIQUE,
                created_at REAL NOT NULL,
                CHECK (
                    (item_id IS NULL AND item_revision IS NULL)
                    OR (item_id IS NOT NULL AND item_revision IS NOT NULL)
                ),
                FOREIGN KEY (item_id, item_revision)
                    REFERENCES experience_item_revisions(item_id, revision) ON DELETE CASCADE,
                FOREIGN KEY (retrieval_id) REFERENCES experience_retrievals(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO experience_events_v3(
                id, event_type, item_id, item_revision, retrieval_id,
                work_id, payload_json, idempotency_key, created_at
            )
            SELECT
                id, event_type, item_id, item_revision, retrieval_id,
                work_id, payload_json, idempotency_key, created_at
            FROM experience_events
            """
        )
        conn.execute("DROP TABLE experience_events")
        conn.execute("ALTER TABLE experience_events_v3 RENAME TO experience_events")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_experience_events_created
                ON experience_events(created_at)
            """
        )

    @staticmethod
    def _recreate_base_triggers(conn: sqlite3.Connection) -> None:
        for statement in _BASE_SCHEMA_STATEMENTS:
            if statement.lstrip().startswith("CREATE TRIGGER"):
                conn.execute(statement)

    def _migrate_to_current_schema(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT value FROM experience_schema_meta WHERE key = 'version'"
        ).fetchone()
        if row is None:
            return
        version = row["value"]
        if version == _CURRENT_SCHEMA_VERSION:
            return
        if version == "2":
            self._rebuild_v3_items_table(conn)
            self._rebuild_v3_tags_table(conn)
            self._rebuild_v3_events_table(conn)
            self._recreate_base_triggers(conn)
            version = "3"
        if version == "3":
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS experience_migration_sources (
                    source_system TEXT NOT NULL,
                    source_store_hash TEXT NOT NULL,
                    source_item_id TEXT NOT NULL,
                    source_revision INTEGER NOT NULL,
                    target_item_id TEXT,
                    target_revision INTEGER,
                    disposition TEXT NOT NULL CHECK (
                        disposition IN (
                            'imported_candidate',
                            'skipped',
                            'needs_manual_review'
                        )
                    ),
                    reason_code TEXT,
                    imported_at REAL NOT NULL,
                    PRIMARY KEY (
                        source_system,
                        source_store_hash,
                        source_item_id,
                        source_revision
                    )
                )
                """
            )
            return
        raise ExperienceSchemaNotCurrentError(
            f"unsupported experience schema version: {version}"
        )

    def _verify_current_schema(self) -> None:
        """Validate current tables and FTS state using read-only statements."""

        conn = self._connection()
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not _CURRENT_SCHEMA_TABLES.issubset(tables):
            raise ExperienceSchemaNotCurrentError(
                "experience schema is missing required tables"
            )
        version = conn.execute(
            "SELECT value FROM experience_schema_meta WHERE key = 'version'"
        ).fetchone()
        if version is None or version["value"] != _CURRENT_SCHEMA_VERSION:
            raise ExperienceSchemaNotCurrentError(
                "experience schema version is not current"
            )
        policy_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(experience_scope_policies)"
            ).fetchall()
        }
        if "recall_allowed" not in policy_columns:
            raise ExperienceSchemaNotCurrentError(
                "experience recall consent migration is missing"
            )

        module_available = conn.execute(
            "SELECT 1 FROM pragma_module_list WHERE name = 'fts5'"
        ).fetchone() is not None
        if not module_available:
            self._fts_enabled = False
            return
        if "experience_search" not in tables:
            raise ExperienceSchemaNotCurrentError(
                "experience FTS index is not initialized"
            )
        rebuild_marker = conn.execute(
            "SELECT value FROM experience_schema_meta "
            "WHERE key = 'fts_rebuild_version'"
        ).fetchone()
        if (
            rebuild_marker is None
            or rebuild_marker["value"] != _CURRENT_FTS_VERSION
        ):
            raise ExperienceSchemaNotCurrentError(
                "experience FTS index needs rebuilding"
            )
        try:
            conn.execute(
                "SELECT rowid FROM experience_search "
                "WHERE experience_search MATCH ? LIMIT 1",
                ("marlow_fts_usability_probe",),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if self._fts_unavailable(exc):
                self._fts_enabled = False
                return
            raise ExperienceSchemaNotCurrentError(
                "experience FTS index is not usable"
            ) from exc
        self._fts_enabled = True

    def _disable_fts_triggers(self) -> None:
        def drop(conn: sqlite3.Connection) -> None:
            for name in _FTS_TRIGGER_NAMES:
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            # Shadow-table writes performed without FTS triggers require one
            # rebuild when an FTS-capable runtime next opens the database.
            if conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'experience_schema_meta'"
            ).fetchone() is not None:
                conn.execute(
                    "DELETE FROM experience_schema_meta "
                    "WHERE key = 'fts_rebuild_version'"
                )

        self._execute_write(drop)
        self._fts_enabled = False

    def close(self) -> None:
        """Checkpoint best-effort and close this store's connection."""
        with self._lock:
            if self._closed:
                return
            if self._initialize_schema:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception:
                    pass
            self._conn.close()
            self._closed = True

    # ------------------------------------------------------------------
    # Validation and serialization
    # ------------------------------------------------------------------

    @staticmethod
    def _identifier(value: Any, field: str, *, nullable: bool = False) -> str | None:
        if value is None:
            if nullable:
                return None
            raise ValueError(f"{field} is required")
        normalized = str(_enum_value(value)).strip()
        if not normalized:
            if nullable:
                return None
            raise ValueError(f"{field} is required")
        if not _SAFE_IDENTIFIER_RE.fullmatch(normalized):
            raise ValueError(f"invalid {field}")
        # Generated repository/project digest identifiers are intentionally
        # opaque. Other identifiers must survive the forced sanitizer exactly;
        # this rejects credentials smuggled into provenance/idempotency fields.
        if (
            not _SAFE_TYPED_DIGEST_ID_RE.fullmatch(normalized)
            and not _SAFE_GENERATED_ID_RE.fullmatch(normalized)
            and not (
                field == "idempotency_key"
                and _SAFE_INTERNAL_IDEMPOTENCY_RE.fullmatch(normalized)
            )
            and _default_storage_sanitizer(normalized) != normalized
        ):
            raise ValueError(f"unsafe {field}")
        return normalized

    @classmethod
    def _digest(cls, value: Any, field: str, *, nullable: bool = False) -> str | None:
        if value is None:
            if nullable:
                return None
            raise ValueError(f"{field} is required")
        normalized = str(_enum_value(value)).strip().casefold()
        if not normalized and nullable:
            return None
        if len(normalized) != 64 or any(
            char not in "0123456789abcdef" for char in normalized
        ):
            raise ValueError(f"{field} must be a SHA-256 hex digest")
        return normalized

    @classmethod
    def _principal(cls, value: Any) -> str:
        principal = cls._identifier(value, "principal_id")
        if principal != "local-owner":
            raise ValueError("the validation MVP supports only local-owner")
        return principal

    @staticmethod
    def _trust_domain(value: Any, *, nullable: bool = False) -> str | None:
        if value is None and nullable:
            return None
        from agent.experience.safety import normalize_trust_domain

        return normalize_trust_domain(value)

    @staticmethod
    def _project_root_rel(value: Any) -> str:
        raw = str(value).strip()
        if not raw or len(raw) > 4_096 or "\x00" in raw or "\\" in raw:
            raise ValueError("invalid project_root_rel")
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("project_root_rel must remain inside the repository")
        return path.as_posix() or "."

    @staticmethod
    def _workspace_root(value: Any, *, nullable: bool = False) -> str | None:
        if value is None and nullable:
            return None
        raw = str(value).strip()
        if not raw or len(raw) > 4_096 or "\x00" in raw:
            raise ValueError("invalid workspace_root")
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError("workspace_root must be absolute")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError("workspace_root is unavailable") from exc
        if resolved != path or not resolved.is_dir():
            raise ValueError("workspace_root must be a canonical directory")
        return str(resolved)

    @staticmethod
    def _coerce_sanitizer_result(value: Any, field: str) -> str:
        # Safety implementations may return a string directly or a small
        # result object.  Supporting both keeps the storage seam independent
        # from the safety layer's diagnostic representation.
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            for key in ("text", "value", "sanitized"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    return candidate
        for attribute in ("text", "value", "sanitized"):
            candidate = getattr(value, attribute, None)
            if isinstance(candidate, str):
                return candidate
        raise TypeError(f"sanitizer returned a non-text value for {field}")

    def _text(
        self,
        value: Any,
        field: str,
        *,
        max_chars: int,
        nullable: bool = False,
        allow_empty: bool = True,
    ) -> str | None:
        if value is None:
            if nullable:
                return None
            value = ""
        raw = str(_enum_value(value))
        sanitized = _default_storage_sanitizer(raw)
        if self._sanitize_write_hook is not None:
            sanitized = self._coerce_sanitizer_result(
                self._sanitize_write_hook(sanitized), field
            )
        # Re-run after the hook so a callback cannot introduce unsafe text.
        sanitized = _default_storage_sanitizer(sanitized).strip()
        if not allow_empty and not sanitized:
            raise ValueError(f"{field} must not be empty")
        if len(sanitized) > max_chars:
            raise ValueError(f"{field} exceeds {max_chars} characters")
        return sanitized

    def _returned_text(self, value: Any, field: str) -> Any:
        if value is None:
            return None
        sanitized = _default_return_sanitizer(str(value))
        if self._sanitize_return_hook is not None:
            sanitized = self._coerce_sanitizer_result(
                self._sanitize_return_hook(sanitized), field
            )
        return _default_return_sanitizer(sanitized)

    @staticmethod
    def _mapping(value: Any, field: str) -> dict[str, Any]:
        if value is None:
            return {}
        if is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        elif hasattr(value, "to_dict") and callable(value.to_dict):
            value = value.to_dict()
        if not isinstance(value, Mapping):
            raise TypeError(f"{field} must be a mapping")
        return {str(key): item for key, item in value.items()}

    def _sanitize_json_value(self, value: Any, field: str) -> Any:
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"{field} contains a non-finite number")
            return value
        if isinstance(value, str) or hasattr(value, "value"):
            raw = _enum_value(value)
            if (
                field.endswith("_hash")
                and isinstance(raw, str)
                and len(raw) == 64
                and all(char in "0123456789abcdefABCDEF" for char in raw)
            ):
                return raw.casefold()
            return self._text(raw, field, max_chars=_MAX_BODY_JSON_BYTES)
        if is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for raw_key, item in value.items():
                key = str(raw_key)
                if not _SAFE_METADATA_KEY_RE.fullmatch(key):
                    raise ValueError(f"{field} contains an invalid object key")
                if _default_storage_sanitizer(key) != key:
                    raise ValueError(f"{field} contains an unsafe object key")
                if key.casefold() in _FORBIDDEN_METADATA_KEYS:
                    raise ValueError(
                        f"{field} contains forbidden non-metadata field {key!r}"
                    )
                result[key] = self._sanitize_json_value(item, f"{field}.{key}")
            return result
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            if len(value) > 128:
                raise ValueError(f"{field} contains too many entries")
            return [
                self._sanitize_json_value(item, f"{field}[{index}]")
                for index, item in enumerate(value)
            ]
        raise TypeError(f"{field} contains unsupported value {type(value).__name__}")

    @staticmethod
    def _json_dumps(value: Any, field: str, max_bytes: int) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > max_bytes:
            raise ValueError(f"{field} exceeds {max_bytes} bytes")
        return encoded

    def _json_for_storage(
        self,
        value: Any,
        field: str,
        max_bytes: int,
    ) -> tuple[dict[str, Any], str]:
        sanitized = self._sanitize_json_value(
            self._mapping(value, field), field
        )
        return sanitized, self._json_dumps(sanitized, field, max_bytes)

    def _json_value_for_storage(
        self,
        value: Any,
        field: str,
        max_bytes: int,
    ) -> tuple[Any, str]:
        sanitized = self._sanitize_json_value(value, field)
        return sanitized, self._json_dumps(sanitized, field, max_bytes)

    def _json_from_storage(self, value: str, field: str) -> Any:
        decoded = json.loads(value)

        def sanitize(item: Any, path: str) -> Any:
            if isinstance(item, str):
                normalized = item.casefold()
                if path.endswith(("_hash", "policy_anchor_hash")) and len(normalized) == 64 and all(char in "0123456789abcdef" for char in normalized):
                    return normalized
                return self._returned_text(item, path)
            if isinstance(item, list):
                return [sanitize(child, f"{path}[]") for child in item]
            if isinstance(item, dict):
                return {
                    key: sanitize(child, f"{path}.{key}")
                    for key, child in item.items()
                }
            return item

        return sanitize(decoded, field)

    def _normalize_tags(
        self,
        tags: Mapping[Any, Iterable[Any]] | Iterable[tuple[Any, Any]] | None,
    ) -> tuple[tuple[str, str], ...]:
        if tags is None:
            return ()
        pairs: list[tuple[Any, Any]] = []
        if isinstance(tags, Mapping):
            for namespace, values in tags.items():
                if isinstance(values, (str, bytes)):
                    values = (values,)
                for value in values:
                    pairs.append((namespace, value))
        else:
            for tag in tags:
                if hasattr(tag, "namespace") and hasattr(tag, "value"):
                    pairs.append((tag.namespace, tag.value))
                else:
                    namespace, value = tag
                    pairs.append((namespace, value))

        normalized: set[tuple[str, str]] = set()
        for namespace, value in pairs:
            safe_namespace = _require_choice(
                "tag namespace", namespace, _TAG_NAMESPACES
            )
            safe_value = self._text(
                value,
                "tag value",
                max_chars=_MAX_TAG_CHARS,
                allow_empty=False,
            )
            assert safe_value is not None
            normalized.add((safe_namespace, safe_value.casefold()))
        return tuple(sorted(normalized))

    @staticmethod
    def _lesson_body_search_text(body: Mapping[str, Any]) -> str:
        fields = (
            body.get("applies_when"),
            body.get("does_not_apply_when"),
            body.get("guidance"),
            body.get("rationale"),
        )
        return " ".join(str(value) for value in fields if value)

    @staticmethod
    def _validate_lesson_body(body: Mapping[str, Any]) -> None:
        allowed = {
            "applies_when",
            "does_not_apply_when",
            "guidance",
            "rationale",
        }
        unknown = set(body) - allowed
        if unknown:
            raise ValueError(f"unknown lesson body fields: {sorted(unknown)!r}")
        for key in ("applies_when", "guidance", "rationale"):
            value = body.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"lesson body requires non-empty {key}")
            if len(value) > 4_000:
                raise ValueError(f"lesson body field {key} exceeds 4000 characters")
        does_not_apply = body.get("does_not_apply_when")
        if does_not_apply is not None and not isinstance(does_not_apply, str):
            raise ValueError("does_not_apply_when must be text or null")
        if isinstance(does_not_apply, str) and len(does_not_apply) > 4_000:
            raise ValueError(
                "lesson body field does_not_apply_when exceeds 4000 characters"
            )

    @staticmethod
    def _content_hash(
        *,
        scope_type: str,
        scope_id: str,
        title: str,
        summary: str,
        body: Mapping[str, Any],
        tags: Sequence[tuple[str, str]],
    ) -> str:
        canonical = json.dumps(
            {
                "kind": "lesson",
                "scope_type": scope_type,
                "scope_id": scope_id,
                "title": title,
                "summary": summary,
                "body": body,
                "tags": list(tags),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Lesson lifecycle
    # ------------------------------------------------------------------

    def create_lesson(
        self,
        *,
        principal_id: str,
        scope_type: str,
        scope_id: str,
        repository_id: str | None,
        project_id: str | None,
        title: str,
        body: Mapping[str, Any] | Any,
        summary: str = "",
        tags: Mapping[Any, Iterable[Any]] | Iterable[tuple[Any, Any]] | None = None,
        confidence: float | None = None,
        sensitivity: str = "normal",
        egress_policy: str = "local_only",
        producer_trust_domain: str | None = None,
        created_by: str = "user",
        source_session_id: str | None = None,
        source_turn_id: str | None = None,
        source_work_id: str | None = None,
        source_hash: str | None = None,
        producer: Mapping[str, Any] | Any | None = None,
        last_validated_at: float | None = None,
        review_after: float | None = None,
        item_id: str | None = None,
        family_id: str | None = None,
        idempotency_key: str | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        """Create an inactive manual lesson candidate at immutable revision 1.

        Supplying ``item_id`` or ``idempotency_key`` makes caller retries
        idempotent.  Reusing either key with different sanitized content fails
        instead of silently overwriting data.
        """
        safe_principal = self._principal(principal_id)
        safe_scope_type = _require_choice("scope_type", scope_type, _SCOPE_TYPES)
        safe_scope_id = self._identifier(scope_id, "scope_id")
        safe_repository = self._identifier(
            repository_id, "repository_id", nullable=True
        )
        safe_project = self._identifier(project_id, "project_id", nullable=True)
        if safe_scope_type == "project" and (not safe_repository or not safe_project):
            raise ValueError("project-scoped lessons require repository_id and project_id")
        if safe_scope_type == "repository" and not safe_repository:
            raise ValueError("repository-scoped lessons require repository_id")
        if safe_scope_type == "project" and safe_scope_id != safe_project:
            raise ValueError("project scope_id must equal project_id")
        if safe_scope_type == "repository" and (
            safe_scope_id != safe_repository or safe_project is not None
        ):
            raise ValueError(
                "repository scope_id must equal repository_id and project_id must be null"
            )
        if safe_scope_type == "profile" and (
            safe_repository is not None or safe_project is not None
        ):
            raise ValueError("profile-scoped lessons cannot carry repository/project ids")

        safe_title = self._text(
            title, "title", max_chars=_MAX_TITLE_CHARS, allow_empty=False
        )
        safe_summary = self._text(
            summary, "summary", max_chars=_MAX_SUMMARY_CHARS
        )
        safe_body, body_json = self._json_for_storage(
            body, "body", _MAX_BODY_JSON_BYTES
        )
        self._validate_lesson_body(safe_body)
        safe_tags = self._normalize_tags(tags)
        safe_producer, producer_json = self._json_for_storage(
            producer or {}, "producer", _MAX_PRODUCER_JSON_BYTES
        )
        del safe_producer

        safe_sensitivity = _require_choice(
            "sensitivity", sensitivity, _SENSITIVITIES
        )
        safe_egress = _require_choice(
            "egress_policy", egress_policy, _EGRESS_POLICIES
        )
        safe_creator = _require_choice("created_by", created_by, _CREATED_BY)
        safe_confidence = _validate_confidence(confidence)
        safe_last_validated = _optional_timestamp(
            last_validated_at, "last_validated_at"
        )
        safe_review_after = _optional_timestamp(review_after, "review_after")
        safe_trust_domain = self._trust_domain(
            producer_trust_domain, nullable=True
        )
        if safe_egress == "same_provider_trust_domain" and not safe_trust_domain:
            raise ValueError(
                "same_provider_trust_domain requires producer_trust_domain"
            )

        safe_item_id = self._identifier(
            item_id or _new_id("lesson"), "item_id"
        )
        safe_family_id = self._identifier(
            family_id or safe_item_id, "family_id"
        )
        safe_idempotency = self._identifier(
            idempotency_key, "idempotency_key", nullable=True
        )
        timestamp = _now(created_at)
        content_hash = self._content_hash(
            scope_type=safe_scope_type,
            scope_id=safe_scope_id,
            title=safe_title or "",
            summary=safe_summary or "",
            body=safe_body,
            tags=safe_tags,
        )
        searchable_text = self._lesson_body_search_text(safe_body)

        provenance = {
            "source_session_id": self._identifier(
                source_session_id, "source_session_id", nullable=True
            ),
            "source_turn_id": self._identifier(
                source_turn_id, "source_turn_id", nullable=True
            ),
            "source_work_id": self._identifier(
                source_work_id, "source_work_id", nullable=True
            ),
            "source_hash": self._digest(
                source_hash, "source_hash", nullable=True
            ),
        }

        def insert(conn: sqlite3.Connection) -> str:
            existing = None
            if safe_idempotency:
                existing = conn.execute(
                    "SELECT * FROM experience_items "
                    "WHERE idempotency_key = ?",
                    (safe_idempotency,),
                ).fetchone()
            if existing is None:
                existing = conn.execute(
                    "SELECT * FROM experience_items WHERE id = ?",
                    (safe_item_id,),
                ).fetchone()
            if existing is not None:
                revision = conn.execute(
                    "SELECT * FROM experience_item_revisions "
                    "WHERE item_id = ? AND revision = 1",
                    (existing["id"],),
                ).fetchone()
                expected_item = (
                    safe_principal,
                    safe_scope_type,
                    safe_scope_id,
                    safe_repository,
                    safe_project,
                    safe_sensitivity,
                    safe_egress,
                    safe_trust_domain,
                    safe_creator,
                )
                observed_item = tuple(
                    existing[field]
                    for field in (
                        "principal_id",
                        "scope_type",
                        "scope_id",
                        "repository_id",
                        "project_id",
                        "sensitivity",
                        "egress_policy",
                        "producer_trust_domain",
                        "created_by",
                    )
                )
                expected_revision = (
                    content_hash,
                    safe_confidence,
                    provenance["source_session_id"],
                    provenance["source_turn_id"],
                    provenance["source_work_id"],
                    provenance["source_hash"],
                    producer_json,
                    safe_last_validated,
                    safe_review_after,
                )
                observed_revision = (
                    None
                    if revision is None
                    else tuple(
                        revision[field]
                        for field in (
                            "content_hash",
                            "confidence",
                            "source_session_id",
                            "source_turn_id",
                            "source_work_id",
                            "source_hash",
                            "producer_json",
                            "last_validated_at",
                            "review_after",
                        )
                    )
                )
                identity_mismatch = (
                    (item_id is not None and existing["id"] != safe_item_id)
                    or (
                        (family_id is not None or item_id is not None)
                        and existing["family_id"] != safe_family_id
                    )
                )
                if (
                    identity_mismatch
                    or observed_item != expected_item
                    or observed_revision != expected_revision
                ):
                    raise ValueError("idempotency key already identifies another lesson")
                return str(existing["id"])

            conn.execute(
                """
                INSERT INTO experience_items(
                    id, family_id, kind, current_status, current_revision,
                    principal_id, scope_type, scope_id, repository_id, project_id,
                    sensitivity, egress_policy, producer_trust_domain,
                    created_by, idempotency_key, created_at, updated_at, deleted_at
                ) VALUES (?, ?, 'lesson', 'candidate', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    safe_item_id,
                    safe_family_id,
                    safe_principal,
                    safe_scope_type,
                    safe_scope_id,
                    safe_repository,
                    safe_project,
                    safe_sensitivity,
                    safe_egress,
                    safe_trust_domain,
                    safe_creator,
                    safe_idempotency,
                    timestamp,
                    timestamp,
                ),
            )
            conn.execute(
                """
                INSERT INTO experience_item_revisions(
                    item_id, revision, title, summary, body_json, searchable_text,
                    confidence, source_session_id, source_turn_id, source_work_id,
                    source_hash, content_hash, editor, edit_reason, producer_json,
                    idempotency_key, created_at, last_validated_at, review_after
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?)
                """,
                (
                    safe_item_id,
                    safe_title,
                    safe_summary,
                    body_json,
                    searchable_text,
                    safe_confidence,
                    provenance["source_session_id"],
                    provenance["source_turn_id"],
                    provenance["source_work_id"],
                    provenance["source_hash"],
                    content_hash,
                    safe_creator,
                    producer_json,
                    timestamp,
                    safe_last_validated,
                    safe_review_after,
                ),
            )
            conn.executemany(
                "INSERT INTO experience_tags(item_id, revision, namespace, value) "
                "VALUES (?, 1, ?, ?)",
                [
                    (safe_item_id, namespace, value)
                    for namespace, value in safe_tags
                ],
            )
            return safe_item_id

        created_id = self._execute_write(insert)
        item = self.get_item(created_id)
        assert item is not None
        return item

    @staticmethod
    def _decision_body_search_text(body: Mapping[str, Any]) -> str:
        fields = (body.get("statement"), body.get("rationale"))
        return " ".join(str(value) for value in fields if value)

    @staticmethod
    def _decision_content_hash(
        *,
        scope_type: str,
        scope_id: str,
        title: str,
        summary: str,
        body: Mapping[str, Any],
        tags: Sequence[tuple[str, str]],
    ) -> str:
        canonical = json.dumps(
            {
                "kind": "decision",
                "scope_type": scope_type,
                "scope_id": scope_id,
                "title": title,
                "summary": summary,
                "body": body,
                "tags": list(tags),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _validate_decision_body(self, body: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "statement",
            "rationale",
            "source_type",
            "authority",
            "effective_at",
            "expires_at",
            "policy_anchor_path",
            "policy_anchor_hash",
        }
        unknown = set(body) - allowed
        if unknown:
            raise ValueError(f"unknown decision body fields: {sorted(unknown)!r}")
        statement = self._text(
            body.get("statement", ""),
            "statement",
            max_chars=4_000,
            allow_empty=False,
        )
        rationale = self._text(
            body.get("rationale", ""),
            "rationale",
            max_chars=4_000,
            allow_empty=False,
        )
        source_type = _require_choice(
            "source_type",
            body.get("source_type", "manual_import"),
            {"user_turn", "repository_policy", "agent_proposal", "migration", "manual_import"},
        )
        authority = _require_choice(
            "authority",
            body.get("authority", "unapproved"),
            {"unapproved", "user", "repository_policy"},
        )
        effective_at = _optional_timestamp(
            body.get("effective_at", 0.0), "effective_at"
        )
        if effective_at is None:
            raise ValueError("decision body requires effective_at")
        expires_at = _optional_timestamp(body.get("expires_at"), "expires_at")
        if expires_at is not None and expires_at < effective_at:
            raise ValueError("expires_at must not be earlier than effective_at")
        policy_anchor_path = self._text(
            body.get("policy_anchor_path"),
            "policy_anchor_path",
            max_chars=1_024,
            nullable=True,
        )
        if policy_anchor_path is not None:
            path = PurePosixPath(policy_anchor_path)
            if path.is_absolute() or ".." in path.parts or "\\" in policy_anchor_path:
                raise ValueError("policy_anchor_path must be repository-relative")
            policy_anchor_path = path.as_posix() or "."
        policy_anchor_hash = self._digest(
            body.get("policy_anchor_hash"), "policy_anchor_hash", nullable=True
        )
        if authority == "repository_policy":
            if source_type != "repository_policy":
                raise ValueError(
                    "repository_policy authority requires repository_policy source_type"
                )
            if not policy_anchor_path or not policy_anchor_hash:
                raise ValueError(
                    "repository_policy authority requires policy_anchor_path and policy_anchor_hash"
                )
        if authority == "unapproved" and source_type == "repository_policy":
            raise ValueError("repository_policy decisions cannot be unapproved")
        if authority == "user" and source_type not in {
            "user_turn", "agent_proposal", "manual_import"
        }:
            raise ValueError("user authority requires a user-approved source type")
        return {
            "statement": statement,
            "rationale": rationale,
            "source_type": source_type,
            "authority": authority,
            "effective_at": effective_at,
            "expires_at": expires_at,
            "policy_anchor_path": policy_anchor_path,
            "policy_anchor_hash": policy_anchor_hash,
        }

    def create_decision(
        self,
        *,
        principal_id: str,
        scope_type: str,
        scope_id: str,
        repository_id: str | None,
        project_id: str | None,
        title: str,
        body: Mapping[str, Any] | Any,
        summary: str = "",
        tags: Mapping[Any, Iterable[Any]] | Iterable[tuple[Any, Any]] | None = None,
        sensitivity: str = "normal",
        egress_policy: str = "local_only",
        producer_trust_domain: str | None = None,
        created_by: str = "agent",
        source_session_id: str | None = None,
        source_turn_id: str | None = None,
        source_work_id: str | None = None,
        source_hash: str | None = None,
        producer: Mapping[str, Any] | Any | None = None,
        review_after: float | None = None,
        item_id: str | None = None,
        family_id: str | None = None,
        idempotency_key: str | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        """Create a candidate Decision at immutable revision 1.

        PR 2 stores and inspects Decisions but does not activate, recall, or
        inject them. Candidate Decisions are therefore never returned by the
        Lesson retrieval path.
        """
        safe_principal = self._principal(principal_id)
        safe_scope_type = _require_choice("scope_type", scope_type, _SCOPE_TYPES)
        safe_scope_id = self._identifier(scope_id, "scope_id")
        safe_repository = self._identifier(
            repository_id, "repository_id", nullable=True
        )
        safe_project = self._identifier(project_id, "project_id", nullable=True)
        if safe_scope_type == "project" and (not safe_repository or not safe_project):
            raise ValueError("project-scoped decisions require repository_id and project_id")
        if safe_scope_type == "repository" and not safe_repository:
            raise ValueError("repository-scoped decisions require repository_id")
        if safe_scope_type == "project" and safe_scope_id != safe_project:
            raise ValueError("project scope_id must equal project_id")
        if safe_scope_type == "repository" and (
            safe_scope_id != safe_repository or safe_project is not None
        ):
            raise ValueError(
                "repository scope_id must equal repository_id and project_id must be null"
            )

        safe_title = self._text(
            title, "title", max_chars=_MAX_TITLE_CHARS, allow_empty=False
        )
        safe_summary = self._text(
            summary, "summary", max_chars=_MAX_SUMMARY_CHARS
        )
        safe_body = self._validate_decision_body(self._mapping(body, "body"))
        _, body_json = self._json_for_storage(safe_body, "body", _MAX_BODY_JSON_BYTES)
        if safe_body["authority"] not in {"unapproved", "repository_policy"}:
            raise ValueError("candidate decisions must be unapproved or repository_policy")
        safe_tags = self._normalize_tags(tags)
        safe_producer, producer_json = self._json_for_storage(
            producer or {}, "producer", _MAX_PRODUCER_JSON_BYTES
        )
        del safe_producer

        safe_sensitivity = _require_choice(
            "sensitivity", sensitivity, _SENSITIVITIES
        )
        safe_egress = _require_choice(
            "egress_policy", egress_policy, _EGRESS_POLICIES
        )
        safe_creator = _require_choice("created_by", created_by, _CREATED_BY)
        safe_review_after = _optional_timestamp(review_after, "review_after")
        safe_trust_domain = self._trust_domain(
            producer_trust_domain, nullable=True
        )
        if safe_egress == "same_provider_trust_domain" and not safe_trust_domain:
            raise ValueError(
                "same_provider_trust_domain requires producer_trust_domain"
            )

        safe_item_id = self._identifier(
            item_id or _new_id("decision"), "item_id"
        )
        safe_family_id = self._identifier(
            family_id or safe_item_id, "family_id"
        )
        safe_idempotency = self._identifier(
            idempotency_key, "idempotency_key", nullable=True
        )
        timestamp = _now(created_at)
        content_hash = self._decision_content_hash(
            scope_type=safe_scope_type,
            scope_id=safe_scope_id,
            title=safe_title or "",
            summary=safe_summary or "",
            body=safe_body,
            tags=safe_tags,
        )
        searchable_text = self._decision_body_search_text(safe_body)
        provenance = {
            "source_session_id": self._identifier(
                source_session_id, "source_session_id", nullable=True
            ),
            "source_turn_id": self._identifier(
                source_turn_id, "source_turn_id", nullable=True
            ),
            "source_work_id": self._identifier(
                source_work_id, "source_work_id", nullable=True
            ),
            "source_hash": self._digest(
                source_hash, "source_hash", nullable=True
            ),
        }

        def insert(conn: sqlite3.Connection) -> str:
            existing = None
            if safe_idempotency:
                existing = conn.execute(
                    "SELECT * FROM experience_items "
                    "WHERE idempotency_key = ?",
                    (safe_idempotency,),
                ).fetchone()
            if existing is None:
                existing = conn.execute(
                    "SELECT * FROM experience_items WHERE id = ?",
                    (safe_item_id,),
                ).fetchone()
            if existing is not None:
                revision = conn.execute(
                    "SELECT * FROM experience_item_revisions "
                    "WHERE item_id = ? AND revision = 1",
                    (existing["id"],),
                ).fetchone()
                expected_item = (
                    safe_principal,
                    safe_scope_type,
                    safe_scope_id,
                    safe_repository,
                    safe_project,
                    safe_sensitivity,
                    safe_egress,
                    safe_trust_domain,
                    safe_creator,
                )
                observed_item = tuple(
                    existing[field]
                    for field in (
                        "principal_id",
                        "scope_type",
                        "scope_id",
                        "repository_id",
                        "project_id",
                        "sensitivity",
                        "egress_policy",
                        "producer_trust_domain",
                        "created_by",
                    )
                )
                expected_revision = (
                    content_hash,
                    provenance["source_session_id"],
                    provenance["source_turn_id"],
                    provenance["source_work_id"],
                    provenance["source_hash"],
                    producer_json,
                    safe_review_after,
                )
                observed_revision = (
                    None
                    if revision is None
                    else tuple(
                        revision[field]
                        for field in (
                            "content_hash",
                            "source_session_id",
                            "source_turn_id",
                            "source_work_id",
                            "source_hash",
                            "producer_json",
                            "review_after",
                        )
                    )
                )
                identity_mismatch = (
                    (item_id is not None and existing["id"] != safe_item_id)
                    or (
                        (family_id is not None or item_id is not None)
                        and existing["family_id"] != safe_family_id
                    )
                )
                if (
                    identity_mismatch
                    or observed_item != expected_item
                    or observed_revision != expected_revision
                ):
                    raise ValueError("idempotency key already identifies another decision")
                return str(existing["id"])

            conn.execute(
                """
                INSERT INTO experience_items(
                    id, family_id, kind, current_status, current_revision,
                    principal_id, scope_type, scope_id, repository_id, project_id,
                    sensitivity, egress_policy, producer_trust_domain,
                    created_by, idempotency_key, created_at, updated_at, deleted_at
                ) VALUES (?, ?, 'decision', 'candidate', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    safe_item_id,
                    safe_family_id,
                    safe_principal,
                    safe_scope_type,
                    safe_scope_id,
                    safe_repository,
                    safe_project,
                    safe_sensitivity,
                    safe_egress,
                    safe_trust_domain,
                    safe_creator,
                    safe_idempotency,
                    timestamp,
                    timestamp,
                ),
            )
            conn.execute(
                """
                INSERT INTO experience_item_revisions(
                    item_id, revision, title, summary, body_json, searchable_text,
                    confidence, source_session_id, source_turn_id, source_work_id,
                    source_hash, content_hash, editor, edit_reason, producer_json,
                    idempotency_key, created_at, last_validated_at, review_after
                ) VALUES (?, 1, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, NULL, ?)
                """,
                (
                    safe_item_id,
                    safe_title,
                    safe_summary,
                    body_json,
                    searchable_text,
                    provenance["source_session_id"],
                    provenance["source_turn_id"],
                    provenance["source_work_id"],
                    provenance["source_hash"],
                    content_hash,
                    safe_creator,
                    producer_json,
                    timestamp,
                    safe_review_after,
                ),
            )
            conn.executemany(
                "INSERT INTO experience_tags(item_id, revision, namespace, value) "
                "VALUES (?, 1, ?, ?)",
                [
                    (safe_item_id, namespace, value)
                    for namespace, value in safe_tags
                ],
            )
            payload = self._json_dumps(
                {"status": "candidate"}, "event payload", _MAX_EVENT_JSON_BYTES
            )
            event_id = self._identifier(_new_id("event"), "event_id")
            conn.execute(
                """
                INSERT INTO experience_events(
                    id, event_type, item_id, item_revision, retrieval_id,
                    work_id, payload_json, idempotency_key, created_at
                ) VALUES (?, 'candidate_created', ?, 1, NULL, NULL, ?, NULL, ?)
                """,
                (event_id, safe_item_id, payload, timestamp),
            )
            return safe_item_id

        created_id = self._execute_write(insert)
        item = self.get_item(created_id)
        assert item is not None
        return item

    def create_authorized_decision(
        self,
        *,
        principal_id: str,
        scope_type: str,
        scope_id: str,
        repository_id: str | None,
        project_id: str | None,
        title: str,
        body: Mapping[str, Any] | Any,
        authority: DecisionTurnAuthority,
        repository_root: str | Path | None = None,
        policy_repository_id: str | None = None,
        policy_project_id: str | None = None,
        summary: str = "",
        tags: Mapping[Any, Iterable[Any]] | Iterable[tuple[Any, Any]] | None = None,
        sensitivity: str = "normal",
        egress_policy: str = "local_only",
        producer_trust_domain: str | None = None,
        created_by: str = "user",
        source_session_id: str | None = None,
        source_turn_id: str | None = None,
        source_work_id: str | None = None,
        source_hash: str | None = None,
        producer: Mapping[str, Any] | Any | None = None,
        review_after: float | None = None,
        item_id: str | None = None,
        family_id: str | None = None,
        idempotency_key: str | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        """Atomically create and activate a Decision when host authority is trusted."""

        trusted_authority = self._require_decision_authority(authority)
        safe_principal = self._principal(principal_id)
        safe_scope_type = _require_choice("scope_type", scope_type, _SCOPE_TYPES)
        safe_scope_id = self._identifier(scope_id, "scope_id")
        safe_repository = self._identifier(
            repository_id, "repository_id", nullable=True
        )
        safe_project = self._identifier(project_id, "project_id", nullable=True)
        if safe_scope_type == "project" and (not safe_repository or not safe_project):
            raise ValueError("project-scoped decisions require repository_id and project_id")
        if safe_scope_type == "repository" and not safe_repository:
            raise ValueError("repository-scoped decisions require repository_id")
        if safe_scope_type == "project" and safe_scope_id != safe_project:
            raise ValueError("project scope_id must equal project_id")
        if safe_scope_type == "repository" and (
            safe_scope_id != safe_repository or safe_project is not None
        ):
            raise ValueError(
                "repository scope_id must equal repository_id and project_id must be null"
            )

        safe_title = self._text(
            title, "title", max_chars=_MAX_TITLE_CHARS, allow_empty=False
        )
        safe_summary = self._text(
            summary, "summary", max_chars=_MAX_SUMMARY_CHARS
        )
        safe_body = self._validate_decision_body(self._mapping(body, "body"))
        if safe_body["authority"] == "repository_policy" and (
            repository_root is None
        ):
            return self.create_decision(
                principal_id=principal_id,
                scope_type=scope_type,
                scope_id=scope_id,
                repository_id=repository_id,
                project_id=project_id,
                title=title,
                summary=summary,
                body=safe_body,
                tags=tags,
                sensitivity=sensitivity,
                egress_policy=egress_policy,
                producer_trust_domain=producer_trust_domain,
                created_by=created_by,
                source_session_id=source_session_id,
                source_turn_id=source_turn_id,
                source_work_id=source_work_id,
                source_hash=source_hash,
                producer=producer,
                review_after=review_after,
                item_id=item_id,
                family_id=family_id,
                idempotency_key=idempotency_key,
                created_at=created_at,
            )
        if safe_body["authority"] == "repository_policy":
            validation = validate_repository_anchor(
                safe_body["policy_anchor_path"],
                safe_body["policy_anchor_hash"],
                repository_root=repository_root,
            )
            if not validation.valid:
                return self.create_decision(
                    principal_id=principal_id,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repository_id=repository_id,
                    project_id=project_id,
                    title=title,
                    summary=summary,
                    body=safe_body,
                    tags=tags,
                    sensitivity=sensitivity,
                    egress_policy=egress_policy,
                    producer_trust_domain=producer_trust_domain,
                    created_by=created_by,
                    source_session_id=source_session_id,
                    source_turn_id=source_turn_id,
                    source_work_id=source_work_id,
                    source_hash=source_hash,
                    producer=producer,
                    review_after=review_after,
                    item_id=item_id,
                    family_id=family_id,
                    idempotency_key=idempotency_key,
                    created_at=created_at,
                )
        elif safe_body["authority"] == "user":
            provisional_item_id = item_id or _new_id("decision")
            if not (
                trusted_authority.approves(provisional_item_id)
                or trusted_authority.matches_source_hash(source_hash)
            ):
                raise ValueError("active decision requires trusted user authority")
        elif safe_body["authority"] == "unapproved":
            provisional_item_id = item_id or _new_id("decision")
            if not (
                trusted_authority.approves(provisional_item_id)
                or trusted_authority.matches_source_hash(source_hash)
            ):
                raise ValueError("authorized decision requires trusted user authority")
            safe_body = dict(safe_body)
            safe_body["authority"] = "user"
        else:
            raise ValueError("unsupported decision authority")
        _, body_json = self._json_for_storage(safe_body, "body", _MAX_BODY_JSON_BYTES)
        safe_tags = self._normalize_tags(tags)
        safe_producer, producer_json = self._json_for_storage(
            producer or {}, "producer", _MAX_PRODUCER_JSON_BYTES
        )
        del safe_producer

        safe_sensitivity = _require_choice(
            "sensitivity", sensitivity, _SENSITIVITIES
        )
        safe_egress = _require_choice(
            "egress_policy", egress_policy, _EGRESS_POLICIES
        )
        safe_creator = _require_choice("created_by", created_by, _CREATED_BY)
        safe_review_after = _optional_timestamp(review_after, "review_after")
        safe_trust_domain = self._trust_domain(
            producer_trust_domain, nullable=True
        )
        if safe_egress == "same_provider_trust_domain" and not safe_trust_domain:
            raise ValueError(
                "same_provider_trust_domain requires producer_trust_domain"
            )

        safe_item_id = self._identifier(
            item_id or _new_id("decision"), "item_id"
        )
        safe_family_id = self._identifier(
            family_id or safe_item_id, "family_id"
        )
        safe_idempotency = self._identifier(
            idempotency_key, "idempotency_key", nullable=True
        )
        timestamp = _now(created_at)
        content_hash = self._decision_content_hash(
            scope_type=safe_scope_type,
            scope_id=safe_scope_id,
            title=safe_title or "",
            summary=safe_summary or "",
            body=safe_body,
            tags=safe_tags,
        )
        searchable_text = self._decision_body_search_text(safe_body)
        provenance = {
            "source_session_id": self._identifier(
                source_session_id, "source_session_id", nullable=True
            ),
            "source_turn_id": self._identifier(
                source_turn_id, "source_turn_id", nullable=True
            ),
            "source_work_id": self._identifier(
                source_work_id, "source_work_id", nullable=True
            ),
            "source_hash": self._digest(
                source_hash, "source_hash", nullable=True
            ),
        }

        def insert(conn: sqlite3.Connection) -> str:
            expected_item = (
                safe_principal,
                safe_scope_type,
                safe_scope_id,
                safe_repository,
                safe_project,
                safe_sensitivity,
                safe_egress,
                safe_trust_domain,
                safe_creator,
            )
            expected_revision = (
                content_hash,
                provenance["source_session_id"],
                provenance["source_turn_id"],
                provenance["source_work_id"],
                provenance["source_hash"],
                producer_json,
                safe_review_after,
            )
            if safe_idempotency:
                existing = conn.execute(
                    "SELECT * FROM experience_items "
                    "WHERE idempotency_key = ?", (safe_idempotency,)
                ).fetchone()
                if existing is not None:
                    revision = conn.execute(
                        "SELECT * FROM experience_item_revisions "
                        "WHERE item_id = ? AND revision = 1",
                        (existing["id"],),
                    ).fetchone()
                    observed_item = tuple(
                        existing[field]
                        for field in (
                            "principal_id",
                            "scope_type",
                            "scope_id",
                            "repository_id",
                            "project_id",
                            "sensitivity",
                            "egress_policy",
                            "producer_trust_domain",
                            "created_by",
                        )
                    )
                    observed_revision = (
                        None
                        if revision is None
                        else tuple(
                            revision[field]
                            for field in (
                                "content_hash",
                                "source_session_id",
                                "source_turn_id",
                                "source_work_id",
                                "source_hash",
                                "producer_json",
                                "review_after",
                            )
                        )
                    )
                    if observed_item != expected_item or observed_revision != expected_revision:
                        raise ValueError(
                            "authorized decision idempotency key identifies different content"
                        )
                    return str(existing["id"])
            if conn.execute(
                "SELECT 1 FROM experience_items WHERE id = ?", (safe_item_id,)
            ).fetchone() is not None:
                raise ValueError("authorized decision already exists")
            conn.execute(
                """
                INSERT INTO experience_items(
                    id, family_id, kind, current_status, current_revision,
                    principal_id, scope_type, scope_id, repository_id, project_id,
                    sensitivity, egress_policy, producer_trust_domain,
                    created_by, idempotency_key, created_at, updated_at, deleted_at
                ) VALUES (?, ?, 'decision', 'active', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    safe_item_id,
                    safe_family_id,
                    safe_principal,
                    safe_scope_type,
                    safe_scope_id,
                    safe_repository,
                    safe_project,
                    safe_sensitivity,
                    safe_egress,
                    safe_trust_domain,
                    safe_creator,
                    safe_idempotency,
                    timestamp,
                    timestamp,
                ),
            )
            conn.execute(
                """
                INSERT INTO experience_item_revisions(
                    item_id, revision, title, summary, body_json, searchable_text,
                    confidence, source_session_id, source_turn_id, source_work_id,
                    source_hash, content_hash, editor, edit_reason, producer_json,
                    idempotency_key, created_at, last_validated_at, review_after
                ) VALUES (?, 1, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, NULL, ?)
                """,
                (
                    safe_item_id,
                    safe_title,
                    safe_summary,
                    body_json,
                    searchable_text,
                    provenance["source_session_id"],
                    provenance["source_turn_id"],
                    provenance["source_work_id"],
                    provenance["source_hash"],
                    content_hash,
                    safe_creator,
                    producer_json,
                    timestamp,
                    safe_review_after,
                ),
            )
            conn.executemany(
                "INSERT INTO experience_tags(item_id, revision, namespace, value) "
                "VALUES (?, 1, ?, ?)",
                [
                    (safe_item_id, namespace, value)
                    for namespace, value in safe_tags
                ],
            )
            event_id = self._identifier(_new_id("event"), "event_id")
            payload = self._decision_event_payload(
                actor=safe_creator,
                reason=None,
                from_status="candidate",
                to_status="active",
            )
            conn.execute(
                """
                INSERT INTO experience_events(
                    id, event_type, item_id, item_revision, retrieval_id,
                    work_id, payload_json, idempotency_key, created_at
                ) VALUES (?, 'candidate_created', ?, 1, NULL, NULL, ?, NULL, ?)
                """,
                (event_id, safe_item_id, payload, timestamp),
            )
            event_id = self._identifier(_new_id("event"), "event_id")
            conn.execute(
                """
                INSERT INTO experience_events(
                    id, event_type, item_id, item_revision, retrieval_id,
                    work_id, payload_json, idempotency_key, created_at
                ) VALUES (?, 'activated', ?, 1, NULL, NULL, ?, NULL, ?)
                """,
                (event_id, safe_item_id, payload, timestamp),
            )
            return safe_item_id

        created_id = self._execute_write(insert)
        item = self.get_decision(created_id)
        assert item is not None
        return item

    def get_item(
        self,
        item_id: str,
        *,
        revision: int | None = None,
        include_history: bool = False,
    ) -> dict[str, Any] | None:
        """Return one item with sanitized current (or requested) revision."""
        safe_item_id = self._identifier(item_id, "item_id")
        with self._lock:
            conn = self._connection()
            item_row = conn.execute(
                "SELECT * FROM experience_items WHERE id = ?", (safe_item_id,)
            ).fetchone()
            if item_row is None:
                return None
            target_revision = int(
                item_row["current_revision"] if revision is None else revision
            )
            revision_row = conn.execute(
                "SELECT * FROM experience_item_revisions "
                "WHERE item_id = ? AND revision = ?",
                (safe_item_id, target_revision),
            ).fetchone()
            if revision_row is None:
                return None
            tags = conn.execute(
                "SELECT namespace, value FROM experience_tags "
                "WHERE item_id = ? AND revision = ? ORDER BY namespace, value",
                (safe_item_id, target_revision),
            ).fetchall()
            history_rows: list[sqlite3.Row] = []
            if include_history:
                history_rows = conn.execute(
                    "SELECT * FROM experience_item_revisions WHERE item_id = ? "
                    "ORDER BY revision",
                    (safe_item_id,),
                ).fetchall()
        result = dict(item_row)
        result["revision"] = self._revision_dict(revision_row, tags)
        if include_history:
            history: list[dict[str, Any]] = []
            for row in history_rows:
                with self._lock:
                    row_tags = self._connection().execute(
                        "SELECT namespace, value FROM experience_tags "
                        "WHERE item_id = ? AND revision = ? ORDER BY namespace, value",
                        (safe_item_id, row["revision"]),
                    ).fetchall()
                history.append(self._revision_dict(row, row_tags))
            result["revisions"] = history
        return result

    def _revision_dict(
        self,
        row: sqlite3.Row,
        tags: Iterable[sqlite3.Row],
    ) -> dict[str, Any]:
        result = dict(row)
        result.pop("searchable_text", None)
        result["title"] = self._returned_text(result["title"], "title")
        result["summary"] = self._returned_text(result["summary"], "summary")
        result["body"] = self._json_from_storage(result.pop("body_json"), "body")
        result["producer"] = self._json_from_storage(
            result.pop("producer_json"), "producer"
        )
        if result.get("edit_reason") is not None:
            result["edit_reason"] = self._returned_text(
                result["edit_reason"], "edit_reason"
            )
        result["tags"] = [
            {
                "namespace": tag["namespace"],
                "value": self._returned_text(tag["value"], "tag value"),
            }
            for tag in tags
        ]
        return result

    def list_items(
        self,
        *,
        kind: str = "lesson",
        status: str | Iterable[str] | None = None,
        principal_id: str | None = None,
        repository_id: str | None = None,
        project_id: str | None = None,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List sanitized governance records in stable newest-first order."""
        if kind not in {"lesson", "decision"}:
            raise ValueError("kind must be lesson or decision")
        status_choices = _LESSON_STATUSES if kind == "lesson" else _DECISION_STATUSES
        clauses = ["kind = ?"]
        params: list[Any] = [kind]
        if status is not None:
            requested_statuses = (
                (status,) if isinstance(status, str) else tuple(status)
            )
            if not requested_statuses:
                return []
            safe_statuses = tuple(
                dict.fromkeys(
                    _require_choice("status", value, status_choices)
                    for value in requested_statuses
                )
            )
            placeholders = ", ".join("?" for _ in safe_statuses)
            clauses.append(f"current_status IN ({placeholders})")
            params.extend(safe_statuses)
        for column, value in (
            ("principal_id", principal_id),
            ("repository_id", repository_id),
            ("project_id", project_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(self._identifier(value, column))
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        safe_limit = max(0, min(int(limit), 1_000))
        safe_offset = max(0, int(offset))
        params.extend((safe_limit, safe_offset))
        with self._lock:
            rows = self._connection().execute(
                f"SELECT id FROM experience_items WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self.get_item(row["id"])
            if item is not None:
                result.append(item)
        return result

    def get_decision(
        self,
        item_id: str,
        *,
        revision: int | None = None,
        include_history: bool = False,
    ) -> dict[str, Any] | None:
        """Return one Decision, rejecting kind mismatch."""
        item = self.get_item(item_id, revision=revision, include_history=include_history)
        if item is not None and item["kind"] != "decision":
            raise KeyError(f"unknown decision {self._identifier(item_id, 'item_id')}")
        return item

    def list_decisions(self, **kwargs: Any) -> list[dict[str, Any]]:
        """List Decision governance records."""
        return self.list_items(kind="decision", **kwargs)

    def list_links(
        self,
        *,
        item_id: str,
        revision: int | None = None,
        direction: str = "both",
        relation: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return bounded direct relationship edges for one experience item."""
        safe_item_id = self._identifier(item_id, "item_id")
        safe_direction = _require_choice("direction", direction, frozenset({"in", "out", "both"}))
        safe_relation = self._text(relation, "relation", max_chars=64, nullable=True)
        if safe_relation is not None:
            safe_relation = _require_choice(
                "relation",
                safe_relation,
                frozenset({
                    "evidence_for",
                    "derived_from",
                    "contradicts",
                    "supersedes",
                    "duplicate_of",
                    "continues",
                }),
            )
        clauses: list[str] = []
        params: list[Any] = []
        def add_clause(prefix: str) -> None:
            parts = [f"{prefix}_item_id = ?"]
            local_params = [safe_item_id]
            if revision is not None:
                if isinstance(revision, bool) or int(revision) < 1:
                    raise ValueError("revision must be a positive integer")
                parts.append(f"{prefix}_revision = ?")
                local_params.append(int(revision))
            if safe_relation is not None:
                parts.append("relation = ?")
                local_params.append(safe_relation)
            clauses.append(f"({' AND '.join(parts)})")
            params.extend(local_params)
        if safe_direction in {"out", "both"}:
            add_clause("from")
        if safe_direction in {"in", "both"}:
            add_clause("to")
        if not clauses:
            return []
        query = f"""
            SELECT * FROM experience_links
            WHERE {' OR '.join(f'({clause})' for clause in clauses)}
            ORDER BY created_at, from_item_id, relation, to_item_id
            LIMIT 200
        """
        with self._lock:
            rows = self._connection().execute(query, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["metadata"] = self._json_from_storage(row["metadata_json"], "link metadata")
            payload.pop("metadata_json", None)
            result.append(payload)
        return result

    def add_experience_link(
        self,
        *,
        from_item_id: str,
        from_revision: int,
        relation: str,
        to_item_id: str,
        to_revision: int,
        metadata: Mapping[str, Any] | None = None,
        created_at: float | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a bounded relationship edge between immutable experience revisions."""

        safe_relation = _require_choice(
            "relation",
            relation,
            frozenset({
                "evidence_for",
                "derived_from",
                "contradicts",
                "supersedes",
                "duplicate_of",
                "continues",
            }),
        )
        safe_from_item = self._identifier(from_item_id, "from_item_id")
        safe_to_item = self._identifier(to_item_id, "to_item_id")
        if isinstance(from_revision, bool) or int(from_revision) < 1:
            raise ValueError("from_revision must be a positive integer")
        if isinstance(to_revision, bool) or int(to_revision) < 1:
            raise ValueError("to_revision must be a positive integer")
        safe_from_revision = int(from_revision)
        safe_to_revision = int(to_revision)
        safe_metadata, metadata_json = self._json_for_storage(
            metadata or {}, "link metadata", _MAX_EVENT_JSON_BYTES
        )
        timestamp = _now(created_at)
        safe_event_id = self._identifier(event_id or _new_id("event"), "event_id")

        def add(conn: sqlite3.Connection) -> tuple[bool, dict[str, Any]]:
            for item_id, revision in (
                (safe_from_item, safe_from_revision),
                (safe_to_item, safe_to_revision),
            ):
                if conn.execute(
                    "SELECT 1 FROM experience_item_revisions "
                    "WHERE item_id = ? AND revision = ?",
                    (item_id, revision),
                ).fetchone() is None:
                    raise ValueError("relationship endpoint revision does not exist")
            existing = conn.execute(
                """
                SELECT created_at FROM experience_links
                WHERE from_item_id = ? AND from_revision = ?
                  AND relation = ? AND to_item_id = ? AND to_revision = ?
                """,
                (
                    safe_from_item,
                    safe_from_revision,
                    safe_relation,
                    safe_to_item,
                    safe_to_revision,
                ),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO experience_links(
                        from_item_id, from_revision, relation, to_item_id,
                        to_revision, created_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        safe_from_item,
                        safe_from_revision,
                        safe_relation,
                        safe_to_item,
                        safe_to_revision,
                        timestamp,
                        metadata_json,
                    ),
                )
            if event_id:
                existing_event = conn.execute(
                    "SELECT * FROM experience_events WHERE id = ?", (safe_event_id,)
                ).fetchone()
                if existing_event is None:
                    conn.execute(
                        """
                        INSERT INTO experience_events(
                            id, event_type, item_id, item_revision, retrieval_id,
                            work_id, payload_json, idempotency_key, created_at
                        ) VALUES (?, 'relation_added', ?, ?, NULL, NULL, ?, NULL, ?)
                        """,
                        (
                            safe_event_id,
                            safe_from_item,
                            safe_from_revision,
                            self._json_dumps(
                                {
                                    "relation": safe_relation,
                                    "to_item_id": safe_to_item,
                                    "to_revision": safe_to_revision,
                                },
                                "event payload",
                                _MAX_EVENT_JSON_BYTES,
                            ),
                            timestamp,
                        ),
                    )
            return existing is not None, safe_metadata

        replayed, metadata_value = self._execute_write(add)
        return {
            "from_item_id": safe_from_item,
            "from_revision": safe_from_revision,
            "relation": safe_relation,
            "to_item_id": safe_to_item,
            "to_revision": safe_to_revision,
            "created_at": timestamp,
            "metadata": metadata_value,
            "replayed": replayed,
        }

    def related_decisions(
        self,
        *,
        item_id: str,
        max_depth: int = 2,
        max_nodes: int = 50,
    ) -> list[dict[str, Any]]:
        """Return shallow Decision relationship traversal without unbounded SQL."""
        safe_item_id = self._identifier(item_id, "item_id")
        initial = self.get_decision(safe_item_id, include_history=False)
        if initial is None:
            return []
        initial_revision = int(initial["current_revision"])
        safe_depth = max(0, min(int(max_depth), 3))
        safe_nodes = max(1, min(int(max_nodes), 100))
        visited: set[tuple[str, int]] = {(safe_item_id, initial_revision)}
        frontier: list[tuple[str, int, int]] = [(safe_item_id, initial_revision, 0)]
        related: list[dict[str, Any]] = []
        while frontier and len(related) < safe_nodes:
            current_id, current_revision, depth = frontier.pop(0)
            if depth >= safe_depth:
                continue
            for link in self.list_links(
                item_id=current_id,
                revision=current_revision,
                direction="both",
                relation=None,
            ):
                from_id = str(link["from_item_id"])
                to_id = str(link["to_item_id"])
                for next_id, next_revision in (
                    (from_id, int(link["from_revision"])),
                    (to_id, int(link["to_revision"])),
                ):
                    key = (next_id, next_revision)
                    if key in visited:
                        continue
                    visited.add(key)
                    decision = self.get_decision(next_id, revision=next_revision, include_history=False)
                    if decision is None or decision.get("kind") != "decision":
                        continue
                    related.append({
                        "decision": decision,
                        "link": link,
                        "depth": depth + 1,
                    })
                    if len(related) >= safe_nodes:
                        break
                    frontier.append((next_id, next_revision, depth + 1))
        return related

    def record_influence_event(
        self,
        *,
        event_type: str,
        item_id: str,
        item_revision: int,
        retrieval_id: str | None,
        work_id: str | None,
        payload: Mapping[str, Any] | None = None,
        created_at: float | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a bounded, text-free influence event."""
        safe_event_type = _require_choice("event_type", event_type, _EVENT_TYPES)
        if safe_event_type not in {
            "disclosed",
            "declared_applied",
            "overridden",
            "not_applicable",
            "retrieved",
        }:
            raise ValueError("event_type is not an influence event")
        safe_item_id = self._identifier(item_id, "item_id")
        if isinstance(item_revision, bool) or int(item_revision) < 1:
            raise ValueError("item_revision must be a positive integer")
        safe_revision = int(item_revision)
        safe_retrieval_id = self._identifier(retrieval_id, "retrieval_id", nullable=True)
        safe_work_id = self._identifier(work_id, "work_id", nullable=True)
        safe_payload = self._json_dumps(payload or {}, "event payload", _MAX_EVENT_JSON_BYTES)
        timestamp = _now(created_at)
        safe_event_id = self._identifier(event_id or _new_id("event"), "event_id")

        def insert(conn: sqlite3.Connection) -> None:
            existing = conn.execute(
                "SELECT * FROM experience_events WHERE id = ?", (safe_event_id,)
            ).fetchone()
            if existing is not None:
                return
            conn.execute(
                """
                INSERT INTO experience_events(
                    id, event_type, item_id, item_revision, retrieval_id,
                    work_id, payload_json, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    safe_event_id,
                    safe_event_type,
                    safe_item_id,
                    safe_revision,
                    safe_retrieval_id,
                    safe_work_id,
                    safe_payload,
                    timestamp,
                ),
            )

        self._execute_write(insert)
        return {
            "id": safe_event_id,
            "event_type": safe_event_type,
            "item_id": safe_item_id,
            "item_revision": safe_revision,
            "retrieval_id": safe_retrieval_id,
            "work_id": safe_work_id,
            "created_at": timestamp,
        }

    def list_influence_events(
        self,
        *,
        item_id: str | None = None,
        retrieval_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return bounded influence events without memory text."""
        safe_item_id = self._identifier(item_id, "item_id", nullable=True)
        safe_retrieval_id = self._identifier(retrieval_id, "retrieval_id", nullable=True)
        safe_event_type = _require_choice("event_type", event_type, _EVENT_TYPES) if event_type else None
        clauses: list[str] = []
        params: list[Any] = []
        if safe_item_id is not None:
            clauses.append("item_id = ?")
            params.append(safe_item_id)
        if safe_retrieval_id is not None:
            clauses.append("retrieval_id = ?")
            params.append(safe_retrieval_id)
        if safe_event_type is not None:
            clauses.append("event_type = ?")
            params.append(safe_event_type)
        safe_limit = max(0, min(int(limit), 500))
        where = f"AND {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT * FROM experience_events
            WHERE event_type IN (
                'disclosed', 'declared_applied', 'overridden',
                'not_applicable', 'retrieved'
            )
            {where}
            ORDER BY created_at DESC, id
            LIMIT ?
        """
        with self._lock:
            rows = self._connection().execute(query, (*params, safe_limit)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["payload"] = self._json_from_storage(row["payload_json"], "event payload")
            payload.pop("payload_json", None)
            result.append(payload)
        return result

    def record_migration_source(
        self,
        *,
        source_system: str,
        source_store_hash: str,
        source_item_id: str,
        source_revision: int,
        target_item_id: str | None,
        target_revision: int | None = None,
        disposition: str = "imported_candidate",
        reason_code: str | None = None,
        imported_at: float | None = None,
    ) -> dict[str, Any]:
        """Persist an idempotent legacy migration mapping."""
        safe_source_system = self._text(source_system, "source_system", max_chars=128)
        safe_store_hash = self._digest(source_store_hash, "source_store_hash")
        safe_source_item_id = self._text(source_item_id, "source_item_id", max_chars=256)
        if isinstance(source_revision, bool) or int(source_revision) < 1:
            raise ValueError("source_revision must be a positive integer")
        safe_source_revision = int(source_revision)
        safe_target_item_id = self._identifier(
            target_item_id, "target_item_id", nullable=True
        )
        safe_target_revision = None
        if target_revision is not None:
            if isinstance(target_revision, bool) or int(target_revision) < 1:
                raise ValueError("target_revision must be a positive integer")
            safe_target_revision = int(target_revision)
        safe_disposition = _require_choice(
            "disposition",
            disposition,
            frozenset({"imported_candidate", "skipped", "needs_manual_review"}),
        )
        safe_reason = self._text(reason_code, "reason_code", max_chars=128, nullable=True)
        timestamp = _now(imported_at)

        def upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO experience_migration_sources(
                    source_system, source_store_hash, source_item_id,
                    source_revision, target_item_id, target_revision,
                    disposition, reason_code, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    source_system, source_store_hash, source_item_id, source_revision
                ) DO UPDATE SET
                    target_item_id = excluded.target_item_id,
                    target_revision = excluded.target_revision,
                    disposition = excluded.disposition,
                    reason_code = excluded.reason_code,
                    imported_at = excluded.imported_at
                """,
                (
                    safe_source_system,
                    safe_store_hash,
                    safe_source_item_id,
                    safe_source_revision,
                    safe_target_item_id,
                    safe_target_revision,
                    safe_disposition,
                    safe_reason,
                    timestamp,
                ),
            )

        self._execute_write(upsert)
        return {
            "source_system": safe_source_system,
            "source_store_hash": safe_store_hash,
            "source_item_id": safe_source_item_id,
            "source_revision": safe_source_revision,
            "target_item_id": safe_target_item_id,
            "target_revision": safe_target_revision,
            "disposition": safe_disposition,
            "reason_code": safe_reason,
            "imported_at": timestamp,
        }

    def list_migration_sources(
        self,
        *,
        source_system: str | None = None,
        source_store_hash: str | None = None,
        source_item_id: str | None = None,
        source_revision: int | None = None,
        disposition: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return bounded migration mappings by source identity."""
        clauses: list[str] = []
        params: list[Any] = []
        if source_system is not None:
            clauses.append("source_system = ?")
            params.append(self._text(source_system, "source_system", max_chars=128))
        if source_store_hash is not None:
            clauses.append("source_store_hash = ?")
            params.append(self._digest(source_store_hash, "source_store_hash"))
        if source_item_id is not None:
            clauses.append("source_item_id = ?")
            params.append(self._text(source_item_id, "source_item_id", max_chars=256))
        if source_revision is not None:
            if isinstance(source_revision, bool) or int(source_revision) < 1:
                raise ValueError("source_revision must be a positive integer")
            clauses.append("source_revision = ?")
            params.append(int(source_revision))
        if disposition is not None:
            clauses.append("disposition = ?")
            params.append(
                _require_choice(
                    "disposition",
                    disposition,
                    frozenset({"imported_candidate", "skipped", "needs_manual_review"}),
                )
            )
        safe_limit = max(0, min(int(limit), 1_000))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(safe_limit)
        with self._lock:
            rows = self._connection().execute(
                f"""
                SELECT * FROM experience_migration_sources
                {where}
                ORDER BY imported_at DESC, source_item_id, source_revision
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def edit_lesson(
        self,
        item_id: str,
        *,
        title: Any = _UNSET,
        summary: Any = _UNSET,
        body: Any = _UNSET,
        tags: Any = _UNSET,
        confidence: Any = _UNSET,
        source_session_id: Any = _UNSET,
        source_turn_id: Any = _UNSET,
        source_work_id: Any = _UNSET,
        source_hash: Any = _UNSET,
        producer: Any = _UNSET,
        last_validated_at: Any = _UNSET,
        review_after: Any = _UNSET,
        editor: str = "user",
        edit_reason: str | None = None,
        idempotency_key: str | None = None,
        edited_at: float | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Append an immutable revision to a nonterminal lesson.

        Unspecified fields inherit from the current revision.  An edit that is
        byte-for-byte identical after sanitization is a no-op.  Terminal
        history is never reopened by editing.
        """
        safe_item_id = self._identifier(item_id, "item_id")
        safe_editor = self._text(
            editor, "editor", max_chars=128, allow_empty=False
        )
        safe_reason = self._text(
            edit_reason,
            "edit_reason",
            max_chars=_MAX_REASON_CHARS,
            nullable=True,
        )
        safe_idempotency = self._identifier(
            idempotency_key, "idempotency_key", nullable=True
        )
        safe_event_id = self._identifier(
            event_id or _new_id("event"), "event_id"
        )
        timestamp = _now(edited_at)

        def edit(conn: sqlite3.Connection) -> int:
            item = conn.execute(
                "SELECT * FROM experience_items WHERE id = ?", (safe_item_id,)
            ).fetchone()
            if item is None or item["kind"] != "lesson":
                raise KeyError(f"unknown lesson {safe_item_id}")
            if item["current_status"] in {"deprecated", "rejected", "retracted"}:
                raise ValueError(
                    f"cannot edit terminal lesson in {item['current_status']} status"
                )
            replay = None
            if safe_idempotency:
                replay = conn.execute(
                    "SELECT revision FROM experience_item_revisions "
                    "WHERE item_id = ? AND idempotency_key = ?",
                    (safe_item_id, safe_idempotency),
                ).fetchone()

            current_revision = int(item["current_revision"])
            current = conn.execute(
                "SELECT * FROM experience_item_revisions "
                "WHERE item_id = ? AND revision = ?",
                (safe_item_id, current_revision),
            ).fetchone()
            if current is None:
                raise RuntimeError("lesson current revision is missing")
            current_tags = tuple(
                (row["namespace"], row["value"])
                for row in conn.execute(
                    "SELECT namespace, value FROM experience_tags "
                    "WHERE item_id = ? AND revision = ? ORDER BY namespace, value",
                    (safe_item_id, current_revision),
                ).fetchall()
            )

            new_title = (
                current["title"]
                if title is _UNSET
                else self._text(
                    title, "title", max_chars=_MAX_TITLE_CHARS, allow_empty=False
                )
            )
            new_summary = (
                current["summary"]
                if summary is _UNSET
                else self._text(summary, "summary", max_chars=_MAX_SUMMARY_CHARS)
            )
            if body is _UNSET:
                new_body = json.loads(current["body_json"])
                new_body_json = current["body_json"]
            else:
                new_body, new_body_json = self._json_for_storage(
                    body, "body", _MAX_BODY_JSON_BYTES
                )
                self._validate_lesson_body(new_body)
            new_tags = current_tags if tags is _UNSET else self._normalize_tags(tags)
            new_confidence = (
                current["confidence"]
                if confidence is _UNSET
                else _validate_confidence(confidence)
            )
            if producer is _UNSET:
                new_producer_json = current["producer_json"]
            else:
                _, new_producer_json = self._json_for_storage(
                    producer or {}, "producer", _MAX_PRODUCER_JSON_BYTES
                )

            def inherited_identifier(value: Any, column: str) -> str | None:
                if value is _UNSET:
                    return current[column]
                return self._identifier(value, column, nullable=True)

            def inherited_digest(value: Any, column: str) -> str | None:
                if value is _UNSET:
                    return current[column]
                return self._digest(value, column, nullable=True)

            new_source_session = inherited_identifier(
                source_session_id, "source_session_id"
            )
            new_source_turn = inherited_identifier(source_turn_id, "source_turn_id")
            new_source_work = inherited_identifier(source_work_id, "source_work_id")
            new_source_hash = inherited_digest(source_hash, "source_hash")
            new_last_validated = (
                current["last_validated_at"]
                if last_validated_at is _UNSET
                else _optional_timestamp(last_validated_at, "last_validated_at")
            )
            new_review_after = (
                current["review_after"]
                if review_after is _UNSET
                else _optional_timestamp(review_after, "review_after")
            )
            new_content_hash = self._content_hash(
                scope_type=item["scope_type"],
                scope_id=item["scope_id"],
                title=new_title or "",
                summary=new_summary or "",
                body=new_body,
                tags=new_tags,
            )
            if (
                new_content_hash == current["content_hash"]
                and new_confidence == current["confidence"]
                and new_source_session == current["source_session_id"]
                and new_source_turn == current["source_turn_id"]
                and new_source_work == current["source_work_id"]
                and new_source_hash == current["source_hash"]
                and new_producer_json == current["producer_json"]
                and new_last_validated == current["last_validated_at"]
                and new_review_after == current["review_after"]
            ):
                return current_revision

            if replay is not None:
                raise ValueError(
                    "idempotent edit replay has different sanitized content"
                )
            if timestamp <= float(item["updated_at"]):
                raise ValueError("edited_at must be newer than the current lesson")

            new_revision = current_revision + 1
            conn.execute(
                """
                INSERT INTO experience_item_revisions(
                    item_id, revision, title, summary, body_json, searchable_text,
                    confidence, source_session_id, source_turn_id, source_work_id,
                    source_hash, content_hash, editor, edit_reason, producer_json,
                    idempotency_key, created_at, last_validated_at, review_after
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    safe_item_id,
                    new_revision,
                    new_title,
                    new_summary,
                    new_body_json,
                    self._lesson_body_search_text(new_body),
                    new_confidence,
                    new_source_session,
                    new_source_turn,
                    new_source_work,
                    new_source_hash,
                    new_content_hash,
                    safe_editor,
                    safe_reason,
                    new_producer_json,
                    safe_idempotency,
                    timestamp,
                    new_last_validated,
                    new_review_after,
                ),
            )
            conn.executemany(
                "INSERT INTO experience_tags(item_id, revision, namespace, value) "
                "VALUES (?, ?, ?, ?)",
                [
                    (safe_item_id, new_revision, namespace, value)
                    for namespace, value in new_tags
                ],
            )
            conn.execute(
                "UPDATE experience_items SET current_revision = ?, updated_at = ? "
                "WHERE id = ?",
                (new_revision, timestamp, safe_item_id),
            )
            payload = self._json_dumps(
                {
                    "editor": safe_editor,
                    "edit_reason": safe_reason,
                    "from_revision": current_revision,
                    "to_revision": new_revision,
                },
                "event payload",
                _MAX_EVENT_JSON_BYTES,
            )
            conn.execute(
                """
                INSERT INTO experience_events(
                    id, event_type, item_id, item_revision, retrieval_id,
                    work_id, payload_json, idempotency_key, created_at
                ) VALUES (?, 'edited', ?, ?, NULL, NULL, ?, NULL, ?)
                """,
                (safe_event_id, safe_item_id, new_revision, payload, timestamp),
            )
            return new_revision

        revision_number = self._execute_write(edit)
        item = self.get_item(safe_item_id, revision=revision_number)
        assert item is not None
        return item

    def edit_decision(
        self,
        item_id: str,
        *,
        title: Any = _UNSET,
        summary: Any = _UNSET,
        body: Any = _UNSET,
        tags: Any = _UNSET,
        source_session_id: Any = _UNSET,
        source_turn_id: Any = _UNSET,
        source_work_id: Any = _UNSET,
        source_hash: Any = _UNSET,
        producer: Any = _UNSET,
        review_after: Any = _UNSET,
        editor: str = "user",
        edit_reason: str | None = None,
        idempotency_key: str | None = None,
        edited_at: float | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Append an immutable revision to a nonterminal Decision.

        Meaningful edits to active Decisions become review_required rather
        than silently changing an active constraint.
        """
        safe_item_id = self._identifier(item_id, "item_id")
        safe_editor = self._text(
            editor, "editor", max_chars=128, allow_empty=False
        )
        safe_reason = self._text(
            edit_reason,
            "edit_reason",
            max_chars=_MAX_REASON_CHARS,
            nullable=True,
        )
        safe_idempotency = self._identifier(
            idempotency_key, "idempotency_key", nullable=True
        )
        safe_event_id = self._identifier(
            event_id or _new_id("event"), "event_id"
        )
        timestamp = _now(edited_at)

        def edit(conn: sqlite3.Connection) -> int:
            item = conn.execute(
                "SELECT * FROM experience_items WHERE id = ?", (safe_item_id,)
            ).fetchone()
            if item is None or item["kind"] != "decision":
                raise KeyError(f"unknown decision {safe_item_id}")
            if item["current_status"] not in {"candidate", "active", "review_required"}:
                raise ValueError(
                    f"can only edit nonterminal decisions, current status is {item['current_status']}"
                )
            replay = None
            if safe_idempotency:
                replay = conn.execute(
                    "SELECT revision FROM experience_item_revisions "
                    "WHERE item_id = ? AND idempotency_key = ?",
                    (safe_item_id, safe_idempotency),
                ).fetchone()

            current_revision = int(item["current_revision"])
            current = conn.execute(
                "SELECT * FROM experience_item_revisions "
                "WHERE item_id = ? AND revision = ?",
                (safe_item_id, current_revision),
            ).fetchone()
            if current is None:
                raise RuntimeError("decision current revision is missing")
            current_tags = tuple(
                (row["namespace"], row["value"])
                for row in conn.execute(
                    "SELECT namespace, value FROM experience_tags "
                    "WHERE item_id = ? AND revision = ? ORDER BY namespace, value",
                    (safe_item_id, current_revision),
                ).fetchall()
            )

            new_title = (
                current["title"]
                if title is _UNSET
                else self._text(
                    title, "title", max_chars=_MAX_TITLE_CHARS, allow_empty=False
                )
            )
            new_summary = (
                current["summary"]
                if summary is _UNSET
                else self._text(summary, "summary", max_chars=_MAX_SUMMARY_CHARS)
            )
            if body is _UNSET:
                new_body = json.loads(current["body_json"])
                new_body_json = current["body_json"]
            else:
                new_body = self._validate_decision_body(self._mapping(body, "body"))
                _, new_body_json = self._json_for_storage(
                    new_body, "body", _MAX_BODY_JSON_BYTES
                )
                if item["current_status"] == "candidate" and new_body["authority"] not in {"unapproved", "repository_policy"}:
                    raise ValueError("candidate decisions must be unapproved or repository_policy")
            new_tags = current_tags if tags is _UNSET else self._normalize_tags(tags)
            meaningful_body_change = False
            if body is not _UNSET:
                old_body = json.loads(current["body_json"])
                meaningful_body_change = any(
                    new_body.get(field) != old_body.get(field)
                    for field in _MEANINGFUL_DECISION_BODY_FIELDS
                )
            status_after_edit = (
                "review_required"
                if item["current_status"] == "active" and meaningful_body_change
                else item["current_status"]
            )

            def inherited_identifier(value: Any, column: str) -> str | None:
                if value is _UNSET:
                    return current[column]
                return self._identifier(value, column, nullable=True)

            def inherited_digest(value: Any, column: str) -> str | None:
                if value is _UNSET:
                    return current[column]
                return self._digest(value, column, nullable=True)

            new_source_session = inherited_identifier(
                source_session_id, "source_session_id"
            )
            new_source_turn = inherited_identifier(source_turn_id, "source_turn_id")
            new_source_work = inherited_identifier(source_work_id, "source_work_id")
            new_source_hash = inherited_digest(source_hash, "source_hash")
            if producer is _UNSET:
                new_producer_json = current["producer_json"]
            else:
                _, new_producer_json = self._json_for_storage(
                    producer or {}, "producer", _MAX_PRODUCER_JSON_BYTES
                )
            new_review_after = (
                current["review_after"]
                if review_after is _UNSET
                else _optional_timestamp(review_after, "review_after")
            )
            new_content_hash = self._decision_content_hash(
                scope_type=item["scope_type"],
                scope_id=item["scope_id"],
                title=new_title or "",
                summary=new_summary or "",
                body=new_body,
                tags=new_tags,
            )
            if (
                new_content_hash == current["content_hash"]
                and new_source_session == current["source_session_id"]
                and new_source_turn == current["source_turn_id"]
                and new_source_work == current["source_work_id"]
                and new_source_hash == current["source_hash"]
                and new_producer_json == current["producer_json"]
                and new_review_after == current["review_after"]
            ):
                return current_revision

            if replay is not None:
                raise ValueError(
                    "idempotent edit replay has different sanitized content"
                )
            if timestamp <= float(item["updated_at"]):
                raise ValueError("edited_at must be newer than the current decision")

            new_revision = current_revision + 1
            conn.execute(
                """
                INSERT INTO experience_item_revisions(
                    item_id, revision, title, summary, body_json, searchable_text,
                    confidence, source_session_id, source_turn_id, source_work_id,
                    source_hash, content_hash, editor, edit_reason, producer_json,
                    idempotency_key, created_at, last_validated_at, review_after
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    safe_item_id,
                    new_revision,
                    new_title,
                    new_summary,
                    new_body_json,
                    self._decision_body_search_text(new_body),
                    new_source_session,
                    new_source_turn,
                    new_source_work,
                    new_source_hash,
                    new_content_hash,
                    safe_editor,
                    safe_reason,
                    new_producer_json,
                    safe_idempotency,
                    timestamp,
                    new_review_after,
                ),
            )
            conn.executemany(
                "INSERT INTO experience_tags(item_id, revision, namespace, value) "
                "VALUES (?, ?, ?, ?)",
                [
                    (safe_item_id, new_revision, namespace, value)
                    for namespace, value in new_tags
                ],
            )
            conn.execute(
                "UPDATE experience_items SET current_revision = ?, current_status = ?, "
                "updated_at = ?, deleted_at = NULL WHERE id = ?",
                (new_revision, status_after_edit, timestamp, safe_item_id),
            )
            payload = self._json_dumps(
                {
                    "editor": safe_editor,
                    "edit_reason": safe_reason,
                    "from_revision": current_revision,
                    "to_revision": new_revision,
                },
                "event payload",
                _MAX_EVENT_JSON_BYTES,
            )
            conn.execute(
                """
                INSERT INTO experience_events(
                    id, event_type, item_id, item_revision, retrieval_id,
                    work_id, payload_json, idempotency_key, created_at
                ) VALUES (?, 'edited', ?, ?, NULL, NULL, ?, NULL, ?)
                """,
                (safe_event_id, safe_item_id, new_revision, payload, timestamp),
            )
            if status_after_edit == "review_required" and item["current_status"] == "active":
                review_payload = self._decision_event_payload(
                    actor=safe_editor,
                    reason="meaningful decision edit requires review",
                    from_status="active",
                    to_status="review_required",
                    extra={"from_revision": current_revision, "to_revision": new_revision},
                )
                self._insert_decision_event(
                    conn,
                    event_id=_new_id("event"),
                    event_type="review_required",
                    item_id=safe_item_id,
                    item_revision=new_revision,
                    payload=review_payload,
                    idempotency_key=None,
                    created_at=timestamp,
                )
            return new_revision

        revision_number = self._execute_write(edit)
        item = self.get_item(safe_item_id, revision=revision_number)
        assert item is not None
        return item

    def _decision_scope_for_hash(self, conn: sqlite3.Connection, item_id: str) -> tuple[str, str]:
        row = conn.execute(
            "SELECT scope_type, scope_id FROM experience_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown decision {item_id}")
        return str(row["scope_type"]), str(row["scope_id"])

    def _decision_content_hash_for_item(
        self,
        conn: sqlite3.Connection,
        item_id: str,
        *,
        title: str,
        summary: str,
        body: Mapping[str, Any],
        tags: Sequence[tuple[str, str]],
    ) -> str:
        scope_type, scope_id = self._decision_scope_for_hash(conn, item_id)
        return self._decision_content_hash(
            scope_type=scope_type,
            scope_id=scope_id,
            title=title or "",
            summary=summary or "",
            body=body,
            tags=tags,
        )

    def _insert_decision_revision(
        self,
        conn: sqlite3.Connection,
        *,
        item_id: str,
        revision: int,
        title: str,
        summary: str,
        body: Mapping[str, Any],
        tags: Sequence[tuple[str, str]],
        editor: str,
        edit_reason: str | None,
        source_session_id: str | None,
        source_turn_id: str | None,
        source_work_id: str | None,
        source_hash: str | None,
        producer_json: str,
        idempotency_key: str | None,
        created_at: float,
        review_after: float | None,
    ) -> str:
        content_hash = self._decision_content_hash_for_item(
            conn,
            item_id,
            title=title,
            summary=summary,
            body=body,
            tags=tags,
        )
        conn.execute(
            """
            INSERT INTO experience_item_revisions(
                item_id, revision, title, summary, body_json, searchable_text,
                confidence, source_session_id, source_turn_id, source_work_id,
                source_hash, content_hash, editor, edit_reason, producer_json,
                idempotency_key, created_at, last_validated_at, review_after
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                item_id,
                revision,
                title,
                summary,
                self._json_dumps(body, "body", _MAX_BODY_JSON_BYTES),
                self._decision_body_search_text(body),
                source_session_id,
                source_turn_id,
                source_work_id,
                source_hash,
                content_hash,
                editor,
                edit_reason,
                producer_json,
                idempotency_key,
                created_at,
                review_after,
            ),
        )
        conn.executemany(
            "INSERT INTO experience_tags(item_id, revision, namespace, value) "
            "VALUES (?, ?, ?, ?)",
            [
                (item_id, revision, namespace, value)
                for namespace, value in tags
            ],
        )
        return content_hash

    @staticmethod
    def _require_decision_authority(authority: Any) -> DecisionTurnAuthority:
        if not isinstance(authority, DecisionTurnAuthority):
            raise TypeError("authority must be a DecisionTurnAuthority")
        return authority

    def _decision_event_payload(
        self,
        *,
        actor: str,
        reason: str | None,
        from_status: str,
        to_status: str,
        extra: Mapping[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "actor": actor,
            "from_status": from_status,
            "reason": reason,
            "to_status": to_status,
        }
        if extra:
            payload.update(extra)
        return self._json_dumps(payload, "event payload", _MAX_EVENT_JSON_BYTES)

    def _insert_decision_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        event_type: str,
        item_id: str,
        item_revision: int,
        payload: str,
        idempotency_key: str | None,
        created_at: float,
    ) -> None:
        if idempotency_key:
            replay = conn.execute(
                "SELECT * FROM experience_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if replay is not None:
                try:
                    replay_payload = json.loads(replay["payload_json"])
                except (TypeError, ValueError):
                    replay_payload = {}
                payload_obj = json.loads(payload)
                if (
                    replay["event_type"] != event_type
                    or replay["item_id"] != item_id
                    or replay_payload.get("actor") != payload_obj.get("actor")
                    or replay_payload.get("reason") != payload_obj.get("reason")
                    or replay_payload.get("to_status") != payload_obj.get("to_status")
                ):
                    raise ValueError(
                        "idempotency key already identifies another decision transition"
                    )
                return
        conn.execute(
            """
            INSERT INTO experience_events(
                id, event_type, item_id, item_revision, retrieval_id,
                work_id, payload_json, idempotency_key, created_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                item_id,
                item_revision,
                payload,
                idempotency_key,
                created_at,
            ),
        )

    def _decision_revision_fields(
        self,
        conn: sqlite3.Connection,
        item: sqlite3.Row,
        current: sqlite3.Row,
        current_tags: tuple[tuple[str, str], ...],
        *,
        title: Any = _UNSET,
        summary: Any = _UNSET,
        body: Any = _UNSET,
        tags: Any = _UNSET,
        source_session_id: Any = _UNSET,
        source_turn_id: Any = _UNSET,
        source_work_id: Any = _UNSET,
        source_hash: Any = _UNSET,
        producer: Any = _UNSET,
        review_after: Any = _UNSET,
    ) -> dict[str, Any]:
        new_title = (
            current["title"]
            if title is _UNSET
            else self._text(title, "title", max_chars=_MAX_TITLE_CHARS, allow_empty=False)
        )
        new_summary = (
            current["summary"]
            if summary is _UNSET
            else self._text(summary, "summary", max_chars=_MAX_SUMMARY_CHARS)
        )
        if body is _UNSET:
            new_body = json.loads(current["body_json"])
            new_body_json = current["body_json"]
        else:
            new_body = self._validate_decision_body(self._mapping(body, "body"))
            _, new_body_json = self._json_for_storage(new_body, "body", _MAX_BODY_JSON_BYTES)
        new_tags = current_tags if tags is _UNSET else self._normalize_tags(tags)

        def inherited_identifier(value: Any, column: str) -> str | None:
            return current[column] if value is _UNSET else self._identifier(value, column, nullable=True)

        def inherited_digest(value: Any, column: str) -> str | None:
            return current[column] if value is _UNSET else self._digest(value, column, nullable=True)

        new_source_session = inherited_identifier(source_session_id, "source_session_id")
        new_source_turn = inherited_identifier(source_turn_id, "source_turn_id")
        new_source_work = inherited_identifier(source_work_id, "source_work_id")
        new_source_hash = inherited_digest(source_hash, "source_hash")
        new_producer_json = (
            current["producer_json"]
            if producer is _UNSET
            else self._json_for_storage(producer or {}, "producer", _MAX_PRODUCER_JSON_BYTES)[1]
        )
        new_review_after = (
            current["review_after"]
            if review_after is _UNSET
            else _optional_timestamp(review_after, "review_after")
        )
        new_content_hash = self._decision_content_hash_for_item(
            conn,
            str(item["id"]),
            title=new_title or "",
            summary=new_summary or "",
            body=new_body,
            tags=new_tags,
        )
        return {
            "title": new_title,
            "summary": new_summary,
            "body": new_body,
            "body_json": new_body_json,
            "tags": new_tags,
            "source_session_id": new_source_session,
            "source_turn_id": new_source_turn,
            "source_work_id": new_source_work,
            "source_hash": new_source_hash,
            "producer_json": new_producer_json,
            "review_after": new_review_after,
            "content_hash": new_content_hash,
        }

    def _decision_authority_revision(
        self,
        conn: sqlite3.Connection,
        item: sqlite3.Row,
        current: sqlite3.Row,
        current_tags: tuple[tuple[str, str], ...],
        *,
        authority: DecisionTurnAuthority,
        repository_root: str | Path | None,
        repository_id: str | None = None,
        title: Any = _UNSET,
        summary: Any = _UNSET,
        body: Any = _UNSET,
        tags: Any = _UNSET,
        source_session_id: Any = _UNSET,
        source_turn_id: Any = _UNSET,
        source_work_id: Any = _UNSET,
        source_hash: Any = _UNSET,
        producer: Any = _UNSET,
        review_after: Any = _UNSET,
    ) -> dict[str, Any]:
        fields = self._decision_revision_fields(
            conn,
            item,
            current,
            current_tags,
            title=title,
            summary=summary,
            body=body,
            tags=tags,
            source_session_id=source_session_id,
            source_turn_id=source_turn_id,
            source_work_id=source_work_id,
            source_hash=source_hash,
            producer=producer,
            review_after=review_after,
        )
        new_body = dict(fields["body"])
        if repository_id is not None and item["repository_id"] != repository_id:
            raise ValueError("repository_id does not match the current decision")
        if new_body["authority"] == "repository_policy":
            if repository_root is None:
                raise ValueError("repository_root is required for repository_policy decisions")
            validation = validate_repository_anchor(
                new_body["policy_anchor_path"],
                new_body["policy_anchor_hash"],
                repository_root=repository_root,
            )
            if not validation.valid:
                raise ValueError(f"repository policy anchor invalid: {validation.reason}")
            fields["anchor_validation"] = validation
        elif new_body["authority"] == "user":
            if not (
                authority.approves(str(item["id"]))
                or authority.matches_source_hash(fields["source_hash"])
            ):
                raise ValueError("active decision requires trusted user authority")
        elif new_body["authority"] == "unapproved":
            if not (
                authority.approves(str(item["id"]))
                or authority.matches_source_hash(fields["source_hash"])
            ):
                raise ValueError("candidate decision requires trusted user authority")
            new_body["authority"] = "user"
            _, fields["body_json"] = self._json_for_storage(new_body, "body", _MAX_BODY_JSON_BYTES)
            fields["body"] = new_body
            fields["content_hash"] = self._decision_content_hash_for_item(
                conn,
                str(item["id"]),
                title=fields["title"] or "",
                summary=fields["summary"] or "",
                body=new_body,
                tags=fields["tags"],
            )
        else:
            raise ValueError("unsupported decision authority")
        return fields

    def activate_decision(
        self,
        item_id: str,
        *,
        authority: DecisionTurnAuthority,
        repository_root: str | Path | None = None,
        repository_id: str | None = None,
        actor: str = "user",
        reason: str | None = None,
        transitioned_at: float | None = None,
        event_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Activate a Decision only through trusted host authority."""

        trusted_authority = self._require_decision_authority(authority)
        safe_item_id = self._identifier(item_id, "item_id")
        safe_actor = self._text(actor, "actor", max_chars=128, allow_empty=False)
        safe_reason = self._text(reason, "reason", max_chars=_MAX_REASON_CHARS, nullable=True)
        safe_event_id = self._identifier(event_id or _new_id("event"), "event_id")
        safe_idempotency = self._identifier(idempotency_key, "idempotency_key", nullable=True)
        timestamp = _now(transitioned_at)

        def activate(conn: sqlite3.Connection) -> int:
            item = conn.execute("SELECT * FROM experience_items WHERE id = ?", (safe_item_id,)).fetchone()
            if item is None or item["kind"] != "decision":
                raise KeyError(f"unknown decision {safe_item_id}")
            if item["current_status"] != "candidate":
                raise ValueError("only candidate decisions can be activated")
            current_revision = int(item["current_revision"])
            current = conn.execute(
                "SELECT * FROM experience_item_revisions WHERE item_id = ? AND revision = ?",
                (safe_item_id, current_revision),
            ).fetchone()
            if current is None:
                raise RuntimeError("decision current revision is missing")
            current_tags = tuple(
                (row["namespace"], row["value"])
                for row in conn.execute(
                    "SELECT namespace, value FROM experience_tags "
                    "WHERE item_id = ? AND revision = ? ORDER BY namespace, value",
                    (safe_item_id, current_revision),
                ).fetchall()
            )
            fields = self._decision_authority_revision(
                conn,
                item,
                current,
                current_tags,
                authority=trusted_authority,
                repository_root=repository_root,
                repository_id=repository_id,
            )
            if fields["content_hash"] != current["content_hash"]:
                current_revision += 1
                self._insert_decision_revision(
                    conn,
                    item_id=safe_item_id,
                    revision=current_revision,
                    title=fields["title"],
                    summary=fields["summary"],
                    body=fields["body"],
                    tags=fields["tags"],
                    editor=safe_actor,
                    edit_reason=safe_reason,
                    source_session_id=fields["source_session_id"],
                    source_turn_id=fields["source_turn_id"],
                    source_work_id=fields["source_work_id"],
                    source_hash=fields["source_hash"],
                    producer_json=fields["producer_json"],
                    idempotency_key=safe_idempotency,
                    created_at=timestamp,
                    review_after=fields["review_after"],
                )
            conn.execute(
                "UPDATE experience_items SET current_revision = ?, current_status = 'active', "
                "updated_at = ?, deleted_at = NULL WHERE id = ?",
                (current_revision, timestamp, safe_item_id),
            )
            payload = self._decision_event_payload(
                actor=safe_actor,
                reason=safe_reason,
                from_status=str(item["current_status"]),
                to_status="active",
            )
            self._insert_decision_event(
                conn,
                event_id=safe_event_id,
                event_type="activated",
                item_id=safe_item_id,
                item_revision=current_revision,
                payload=payload,
                idempotency_key=safe_idempotency,
                created_at=timestamp,
            )
            return current_revision

        revision_number = self._execute_write(activate)
        result = self.get_decision(safe_item_id, revision=revision_number)
        assert result is not None
        return result

    def mark_decision_review_required(
        self,
        item_id: str,
        *,
        actor: str = "user",
        reason: str | None = None,
        repository_root: str | Path | None = None,
        transitioned_at: float | None = None,
        event_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Move a Decision into the non-injectable review state."""

        safe_item_id = self._identifier(item_id, "item_id")
        safe_actor = self._text(actor, "actor", max_chars=128, allow_empty=False)
        safe_reason = self._text(reason, "reason", max_chars=_MAX_REASON_CHARS, nullable=True)
        safe_event_id = self._identifier(event_id or _new_id("event"), "event_id")
        safe_idempotency = self._identifier(idempotency_key, "idempotency_key", nullable=True)
        timestamp = _now(transitioned_at)

        def review(conn: sqlite3.Connection) -> tuple[int, str]:
            item = conn.execute("SELECT * FROM experience_items WHERE id = ?", (safe_item_id,)).fetchone()
            if item is None or item["kind"] != "decision":
                raise KeyError(f"unknown decision {safe_item_id}")
            if item["current_status"] not in {"candidate", "active", "review_required"}:
                raise ValueError("only nonterminal decisions can enter review_required")
            current_revision = int(item["current_revision"])
            current = conn.execute(
                "SELECT * FROM experience_item_revisions WHERE item_id = ? AND revision = ?",
                (safe_item_id, current_revision),
            ).fetchone()
            event_type = "review_required"
            review_reason = safe_reason
            if repository_root is not None:
                body = json.loads(current["body_json"])
                if body["authority"] == "repository_policy":
                    validation = validate_repository_anchor(
                        body["policy_anchor_path"],
                        body["policy_anchor_hash"],
                        repository_root=repository_root,
                    )
                    if not validation.valid:
                        event_type = "anchor_invalidated"
                        review_reason = review_reason or validation.reason
            if safe_idempotency:
                replay = conn.execute(
                    "SELECT * FROM experience_events WHERE idempotency_key = ?",
                    (safe_idempotency,),
                ).fetchone()
                if replay is not None:
                    try:
                        replay_payload = json.loads(replay["payload_json"])
                    except (TypeError, ValueError):
                        replay_payload = {}
                    if (
                        replay["event_type"] != event_type
                        or replay["item_id"] != safe_item_id
                        or replay_payload.get("actor") != safe_actor
                        or replay_payload.get("reason") != review_reason
                        or replay_payload.get("to_status") != "review_required"
                    ):
                        raise ValueError(
                            "idempotency key already identifies another decision review"
                        )
                    return current_revision, event_type
            if item["current_status"] == "review_required":
                return current_revision, event_type
            conn.execute(
                "UPDATE experience_items SET current_status = 'review_required', "
                "updated_at = ?, deleted_at = NULL WHERE id = ?",
                (timestamp, safe_item_id),
            )
            payload = self._decision_event_payload(
                actor=safe_actor,
                reason=review_reason,
                from_status=str(item["current_status"]),
                to_status="review_required",
            )
            self._insert_decision_event(
                conn,
                event_id=safe_event_id,
                event_type=event_type,
                item_id=safe_item_id,
                item_revision=current_revision,
                payload=payload,
                idempotency_key=safe_idempotency,
                created_at=timestamp,
            )
            return current_revision, event_type

        revision_number, _event_type = self._execute_write(review)
        result = self.get_decision(safe_item_id, revision=revision_number)
        assert result is not None
        return result

    def reapprove_decision(
        self,
        item_id: str,
        *,
        authority: DecisionTurnAuthority,
        repository_root: str | Path | None = None,
        repository_id: str | None = None,
        title: Any = _UNSET,
        summary: Any = _UNSET,
        body: Any = _UNSET,
        tags: Any = _UNSET,
        source_session_id: Any = _UNSET,
        source_turn_id: Any = _UNSET,
        source_work_id: Any = _UNSET,
        source_hash: Any = _UNSET,
        producer: Any = _UNSET,
        review_after: Any = _UNSET,
        actor: str = "user",
        reason: str | None = None,
        transitioned_at: float | None = None,
        event_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Reapprove a reviewed Decision and transition it back to active."""

        trusted_authority = self._require_decision_authority(authority)
        safe_item_id = self._identifier(item_id, "item_id")
        safe_actor = self._text(actor, "actor", max_chars=128, allow_empty=False)
        safe_reason = self._text(reason, "reason", max_chars=_MAX_REASON_CHARS, nullable=True)
        safe_event_id = self._identifier(event_id or _new_id("event"), "event_id")
        safe_idempotency = self._identifier(idempotency_key, "idempotency_key", nullable=True)
        timestamp = _now(transitioned_at)

        def reapprove(conn: sqlite3.Connection) -> int:
            item = conn.execute("SELECT * FROM experience_items WHERE id = ?", (safe_item_id,)).fetchone()
            if item is None or item["kind"] != "decision":
                raise KeyError(f"unknown decision {safe_item_id}")
            if item["current_status"] != "review_required":
                raise ValueError("only review_required decisions can be reapproved")
            if repository_id is not None and item["repository_id"] != repository_id:
                raise ValueError("repository_id does not match the current decision")
            current_revision = int(item["current_revision"])
            current = conn.execute(
                "SELECT * FROM experience_item_revisions WHERE item_id = ? AND revision = ?",
                (safe_item_id, current_revision),
            ).fetchone()
            if current is None:
                raise RuntimeError("decision current revision is missing")
            current_tags = tuple(
                (row["namespace"], row["value"])
                for row in conn.execute(
                    "SELECT namespace, value FROM experience_tags "
                    "WHERE item_id = ? AND revision = ? ORDER BY namespace, value",
                    (safe_item_id, current_revision),
                ).fetchall()
            )
            fields = self._decision_authority_revision(
                conn,
                item,
                current,
                current_tags,
                authority=trusted_authority,
                repository_root=repository_root,
                title=title,
                summary=summary,
                body=body,
                tags=tags,
                source_session_id=source_session_id,
                source_turn_id=source_turn_id,
                source_work_id=source_work_id,
                source_hash=source_hash,
                producer=producer,
                review_after=review_after,
            )
            changed = fields["content_hash"] != current["content_hash"]
            changed = changed or fields["source_session_id"] != current["source_session_id"]
            changed = changed or fields["source_turn_id"] != current["source_turn_id"]
            changed = changed or fields["source_work_id"] != current["source_work_id"]
            changed = changed or fields["source_hash"] != current["source_hash"]
            changed = changed or fields["producer_json"] != current["producer_json"]
            changed = changed or fields["review_after"] != current["review_after"]
            if changed or item["current_status"] != "active":
                current_revision += 1
                self._insert_decision_revision(
                    conn,
                    item_id=safe_item_id,
                    revision=current_revision,
                    title=fields["title"],
                    summary=fields["summary"],
                    body=fields["body"],
                    tags=fields["tags"],
                    editor=safe_actor,
                    edit_reason=safe_reason,
                    source_session_id=fields["source_session_id"],
                    source_turn_id=fields["source_turn_id"],
                    source_work_id=fields["source_work_id"],
                    source_hash=fields["source_hash"],
                    producer_json=fields["producer_json"],
                    idempotency_key=safe_idempotency,
                    created_at=timestamp,
                    review_after=fields["review_after"],
                )
            conn.execute(
                "UPDATE experience_items SET current_revision = ?, current_status = 'active', "
                "updated_at = ?, deleted_at = NULL WHERE id = ?",
                (current_revision, timestamp, safe_item_id),
            )
            payload = self._decision_event_payload(
                actor=safe_actor,
                reason=safe_reason,
                from_status=str(item["current_status"]),
                to_status="active",
            )
            self._insert_decision_event(
                conn,
                event_id=safe_event_id,
                event_type="reapproved",
                item_id=safe_item_id,
                item_revision=current_revision,
                payload=payload,
                idempotency_key=safe_idempotency,
                created_at=timestamp,
            )
            return current_revision

        revision_number = self._execute_write(reapprove)
        result = self.get_decision(safe_item_id, revision=revision_number)
        assert result is not None
        return result

    def supersede_decision(
        self,
        old_item_id: str,
        *,
        authority: DecisionTurnAuthority,
        principal_id: str,
        scope_type: str,
        scope_id: str,
        repository_id: str | None,
        project_id: str | None,
        title: str,
        body: Mapping[str, Any] | Any,
        summary: str = "",
        tags: Mapping[Any, Iterable[Any]] | Iterable[tuple[Any, Any]] | None = None,
        sensitivity: str = "normal",
        egress_policy: str = "local_only",
        producer_trust_domain: str | None = None,
        created_by: str = "user",
        source_session_id: str | None = None,
        source_turn_id: str | None = None,
        source_work_id: str | None = None,
        source_hash: str | None = None,
        producer: Mapping[str, Any] | Any | None = None,
        review_after: float | None = None,
        item_id: str | None = None,
        family_id: str | None = None,
        repository_root: str | Path | None = None,
        reason: str | None = None,
        transitioned_at: float | None = None,
        event_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Atomically create a replacement Decision and supersede the old one."""

        trusted_authority = self._require_decision_authority(authority)
        safe_old_item_id = self._identifier(old_item_id, "item_id")
        safe_principal = self._principal(principal_id)
        safe_scope_type = _require_choice("scope_type", scope_type, _SCOPE_TYPES)
        safe_scope_id = self._identifier(scope_id, "scope_id")
        safe_repository = self._identifier(repository_id, "repository_id", nullable=True)
        safe_project = self._identifier(project_id, "project_id", nullable=True)
        if safe_scope_type == "project" and (not safe_repository or not safe_project):
            raise ValueError("project-scoped decisions require repository_id and project_id")
        if safe_scope_type == "repository" and not safe_repository:
            raise ValueError("repository-scoped decisions require repository_id")
        if safe_scope_type == "project" and safe_scope_id != safe_project:
            raise ValueError("project scope_id must equal project_id")
        if safe_scope_type == "repository" and (
            safe_scope_id != safe_repository or safe_project is not None
        ):
            raise ValueError("repository scope_id must equal repository_id and project_id must be null")

        safe_title = self._text(title, "title", max_chars=_MAX_TITLE_CHARS, allow_empty=False)
        safe_summary = self._text(summary, "summary", max_chars=_MAX_SUMMARY_CHARS)
        safe_body = self._validate_decision_body(self._mapping(body, "body"))
        _, safe_body_json = self._json_for_storage(safe_body, "body", _MAX_BODY_JSON_BYTES)
        safe_tags = self._normalize_tags(tags)
        safe_producer, safe_producer_json = self._json_for_storage(
            producer or {}, "producer", _MAX_PRODUCER_JSON_BYTES
        )
        del safe_producer
        safe_sensitivity = _require_choice("sensitivity", sensitivity, _SENSITIVITIES)
        safe_egress = _require_choice("egress_policy", egress_policy, _EGRESS_POLICIES)
        safe_creator = _require_choice("created_by", created_by, _CREATED_BY)
        safe_review_after = _optional_timestamp(review_after, "review_after")
        safe_trust_domain = self._trust_domain(producer_trust_domain, nullable=True)
        if safe_egress == "same_provider_trust_domain" and not safe_trust_domain:
            raise ValueError("same_provider_trust_domain requires producer_trust_domain")
        safe_item_id = self._identifier(item_id or _new_id("decision"), "item_id")
        safe_family_id = self._identifier(family_id or safe_item_id, "family_id")
        safe_idempotency = self._identifier(idempotency_key, "idempotency_key", nullable=True)
        safe_event_id = self._identifier(event_id or _new_id("event"), "event_id")
        timestamp = _now(transitioned_at)
        safe_reason = self._text(reason, "reason", max_chars=_MAX_REASON_CHARS, nullable=True)
        safe_actor = self._principal(safe_principal)
        provenance = {
            "source_session_id": self._identifier(source_session_id, "source_session_id", nullable=True),
            "source_turn_id": self._identifier(source_turn_id, "source_turn_id", nullable=True),
            "source_work_id": self._identifier(source_work_id, "source_work_id", nullable=True),
            "source_hash": self._digest(source_hash, "source_hash", nullable=True),
        }

        def supersede(conn: sqlite3.Connection) -> tuple[str, int]:
            old_item = conn.execute(
                "SELECT * FROM experience_items WHERE id = ?",
                (safe_old_item_id,),
            ).fetchone()
            if old_item is None or old_item["kind"] != "decision":
                raise KeyError(f"unknown decision {safe_old_item_id}")
            if old_item["current_status"] not in {"active", "review_required"}:
                raise ValueError("only active or review_required decisions can be superseded")
            if old_item["principal_id"] != safe_principal:
                raise ValueError("replacement decision principal must match the old decision")
            if not trusted_authority.supersedes(safe_old_item_id):
                raise ValueError("supersession requires explicit trusted user authority")
            if repository_id is not None and old_item["repository_id"] != repository_id:
                raise ValueError("repository_id does not match the current decision")
            from agent.experience.authority import require_scope_not_broadened

            require_scope_not_broadened(
                old_item["scope_type"],
                old_item["scope_id"],
                old_item["repository_id"],
                old_item["project_id"],
                safe_scope_type,
                safe_scope_id,
                safe_repository,
                safe_project,
            )
            replacement_body = safe_body
            replacement_body_json = safe_body_json
            body_authority = replacement_body["authority"]
            if body_authority == "repository_policy":
                if repository_root is None:
                    raise ValueError("repository_root is required for repository_policy decisions")
                validation = validate_repository_anchor(
                    replacement_body["policy_anchor_path"],
                    replacement_body["policy_anchor_hash"],
                    repository_root=repository_root,
                )
                if not validation.valid:
                    raise ValueError(f"repository policy anchor invalid: {validation.reason}")
            elif body_authority == "user":
                if not trusted_authority.supersedes(safe_old_item_id):
                    raise ValueError("replacement decision requires trusted user authority")
            elif body_authority == "unapproved":
                replacement_body = dict(replacement_body)
                if trusted_authority.supersedes(safe_old_item_id):
                    replacement_body["authority"] = "user"
                    _, replacement_body_json = self._json_for_storage(replacement_body, "body", _MAX_BODY_JSON_BYTES)
            else:
                raise ValueError("unsupported decision authority")
            replacement_status = "active" if replacement_body["authority"] in {"user", "repository_policy"} else "candidate"
            old_revision = conn.execute(
                "SELECT * FROM experience_item_revisions "
                "WHERE item_id = ? AND revision = ?",
                (safe_old_item_id, old_item["current_revision"]),
            ).fetchone()
            if old_revision is None:
                raise RuntimeError("superseded decision current revision is missing")
            replacement_content_hash = self._decision_content_hash(
                scope_type=safe_scope_type,
                scope_id=safe_scope_id,
                title=safe_title or "",
                summary=safe_summary or "",
                body=replacement_body,
                tags=safe_tags,
            )
            if (
                replacement_content_hash == self._decision_content_hash(
                    scope_type=old_item["scope_type"],
                    scope_id=old_item["scope_id"],
                    title=old_revision["title"],
                    summary=old_revision["summary"],
                    body=json.loads(old_revision["body_json"]),
                    tags=tuple(
                        (row["namespace"], row["value"])
                        for row in conn.execute(
                            "SELECT namespace, value FROM experience_tags "
                            "WHERE item_id = ? AND revision = ? ORDER BY namespace, value",
                            (safe_old_item_id, old_item["current_revision"]),
                        ).fetchall()
                    ),
                )
                and safe_scope_type == old_item["scope_type"]
                and safe_scope_id == old_item["scope_id"]
                and safe_repository == old_item["repository_id"]
                and safe_project == old_item["project_id"]
            ):
                raise ValueError("replacement decision is an exact duplicate")
            if safe_idempotency:
                existing = conn.execute(
                    "SELECT * FROM experience_items WHERE idempotency_key = ?",
                    (safe_idempotency,),
                ).fetchone()
                if existing is not None and existing["id"] != safe_item_id:
                    raise ValueError("idempotency key already identifies another decision")
            if not safe_idempotency and conn.execute(
                "SELECT 1 FROM experience_items WHERE id = ?",
                (safe_item_id,),
            ).fetchone() is not None:
                raise ValueError("replacement decision already exists")
            conn.execute(
                """
                INSERT INTO experience_items(
                    id, family_id, kind, current_status, current_revision,
                    principal_id, scope_type, scope_id, repository_id, project_id,
                    sensitivity, egress_policy, producer_trust_domain,
                    created_by, idempotency_key, created_at, updated_at, deleted_at
                ) VALUES (?, ?, 'decision', ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    safe_item_id,
                    safe_family_id,
                    replacement_status,
                    safe_principal,
                    safe_scope_type,
                    safe_scope_id,
                    safe_repository,
                    safe_project,
                    safe_sensitivity,
                    safe_egress,
                    safe_trust_domain,
                    safe_creator,
                    safe_idempotency,
                    timestamp,
                    timestamp,
                ),
            )
            conn.execute(
                """
                INSERT INTO experience_item_revisions(
                    item_id, revision, title, summary, body_json, searchable_text,
                    confidence, source_session_id, source_turn_id, source_work_id,
                    source_hash, content_hash, editor, edit_reason, producer_json,
                    idempotency_key, created_at, last_validated_at, review_after
                ) VALUES (?, 1, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    safe_item_id,
                    safe_title,
                    safe_summary,
                    replacement_body_json,
                    self._decision_body_search_text(replacement_body),
                    provenance["source_session_id"],
                    provenance["source_turn_id"],
                    provenance["source_work_id"],
                    provenance["source_hash"],
                    replacement_content_hash,
                    safe_actor,
                    safe_reason,
                    safe_producer_json,
                    safe_idempotency,
                    timestamp,
                    safe_review_after,
                ),
            )
            conn.executemany(
                "INSERT INTO experience_tags(item_id, revision, namespace, value) "
                "VALUES (?, 1, ?, ?)",
                [
                    (safe_item_id, namespace, value)
                    for namespace, value in safe_tags
                ],
            )
            conn.execute(
                """
                INSERT INTO experience_links(
                    from_item_id, from_revision, relation, to_item_id,
                    to_revision, created_at, metadata_json
                ) VALUES (?, 1, 'supersedes', ?, ?, ?, ?)
                """,
                (
                    safe_item_id,
                    safe_old_item_id,
                    old_item["current_revision"],
                    timestamp,
                    self._json_dumps({"reason": safe_reason}, "event payload", _MAX_EVENT_JSON_BYTES),
                ),
            )
            old_event_type = None
            if replacement_status == "active":
                conn.execute(
                    "UPDATE experience_items SET current_status = 'superseded', "
                    "updated_at = ?, deleted_at = ? WHERE id = ?",
                    (timestamp, timestamp, safe_old_item_id),
                )
                old_event_type = "superseded"
            activation_payload = self._decision_event_payload(
                actor=safe_actor,
                reason=safe_reason,
                from_status="candidate",
                to_status=replacement_status,
                extra={"supersedes": safe_old_item_id},
            )
            self._insert_decision_event(
                conn,
                event_id=safe_event_id,
                event_type="activated" if replacement_status == "active" else "candidate_created",
                item_id=safe_item_id,
                item_revision=1,
                payload=activation_payload,
                idempotency_key=safe_idempotency,
                created_at=timestamp,
            )
            if old_event_type is not None:
                old_payload = self._decision_event_payload(
                    actor=safe_actor,
                    reason=safe_reason,
                    from_status=str(old_item["current_status"]),
                    to_status=old_event_type,
                    extra={"superseded_by": safe_item_id},
                )
                self._insert_decision_event(
                    conn,
                    event_id=_new_id("event"),
                    event_type=old_event_type,
                    item_id=safe_old_item_id,
                    item_revision=old_item["current_revision"],
                    payload=old_payload,
                    idempotency_key=None,
                    created_at=timestamp,
                )
            return safe_item_id, 1

        replacement_id, revision_number = self._execute_write(supersede)
        result = self.get_decision(replacement_id, revision=revision_number)
        assert result is not None
        return result

    def revoke_decision(
        self,
        item_id: str,
        *,
        authority: DecisionTurnAuthority,
        actor: str = "user",
        reason: str | None = None,
        transitioned_at: float | None = None,
        event_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Terminal logical deletion for a Decision with explicit authority."""

        trusted_authority = self._require_decision_authority(authority)
        safe_item_id = self._identifier(item_id, "item_id")
        safe_actor = self._text(actor, "actor", max_chars=128, allow_empty=False)
        safe_reason = self._text(reason, "reason", max_chars=_MAX_REASON_CHARS, nullable=True)
        safe_event_id = self._identifier(event_id or _new_id("event"), "event_id")
        safe_idempotency = self._identifier(idempotency_key, "idempotency_key", nullable=True)
        timestamp = _now(transitioned_at)

        def revoke(conn: sqlite3.Connection) -> int:
            item = conn.execute("SELECT * FROM experience_items WHERE id = ?", (safe_item_id,)).fetchone()
            if item is None or item["kind"] != "decision":
                raise KeyError(f"unknown decision {safe_item_id}")
            if item["current_status"] not in {"candidate", "active", "review_required"}:
                raise ValueError("terminal decisions cannot be revoked again")
            if not trusted_authority.revokes(safe_item_id):
                raise ValueError("revocation requires explicit trusted user authority")
            if item["current_status"] == "revoked":
                return int(item["current_revision"])
            conn.execute(
                "UPDATE experience_items SET current_status = 'revoked', "
                "updated_at = ?, deleted_at = ? WHERE id = ?",
                (timestamp, timestamp, safe_item_id),
            )
            payload = self._decision_event_payload(
                actor=safe_actor,
                reason=safe_reason,
                from_status=str(item["current_status"]),
                to_status="revoked",
            )
            self._insert_decision_event(
                conn,
                event_id=safe_event_id,
                event_type="revoked",
                item_id=safe_item_id,
                item_revision=item["current_revision"],
                payload=payload,
                idempotency_key=safe_idempotency,
                created_at=timestamp,
            )
            return int(item["current_revision"])

        revision_number = self._execute_write(revoke)
        result = self.get_decision(safe_item_id, revision=revision_number)
        assert result is not None
        return result


    def transition_lesson(
        self,
        item_id: str,
        new_status: str,
        *,
        actor: str = "user",
        reason: str | None = None,
        transitioned_at: float | None = None,
        event_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply one allowed lifecycle edge without rewriting history."""
        safe_item_id = self._identifier(item_id, "item_id")
        safe_status = _require_choice("status", new_status, _LESSON_STATUSES)
        safe_actor = self._text(
            actor, "actor", max_chars=128, allow_empty=False
        )
        safe_reason = self._text(
            reason, "reason", max_chars=_MAX_REASON_CHARS, nullable=True
        )
        safe_event_id = self._identifier(
            event_id or _new_id("event"), "event_id"
        )
        safe_idempotency = self._identifier(
            idempotency_key, "idempotency_key", nullable=True
        )
        timestamp = _now(transitioned_at)

        def transition(conn: sqlite3.Connection) -> None:
            item = conn.execute(
                "SELECT kind, current_status, current_revision, updated_at "
                "FROM experience_items "
                "WHERE id = ?",
                (safe_item_id,),
            ).fetchone()
            if item is None or item["kind"] != "lesson":
                raise KeyError(f"unknown lesson {safe_item_id}")
            if safe_idempotency:
                replay = conn.execute(
                    "SELECT * FROM experience_events WHERE idempotency_key = ?",
                    (safe_idempotency,),
                ).fetchone()
                if replay is not None:
                    try:
                        replay_payload = json.loads(replay["payload_json"])
                    except (TypeError, ValueError):
                        replay_payload = {}
                    expected_type = (
                        "approved" if safe_status == "active" else safe_status
                    )
                    if (
                        replay["event_type"] != expected_type
                        or replay["item_id"] != safe_item_id
                        or replay["retrieval_id"] is not None
                        or replay["work_id"] is not None
                        or replay_payload.get("actor") != safe_actor
                        or replay_payload.get("reason") != safe_reason
                        or replay_payload.get("to_status") != safe_status
                    ):
                        raise ValueError(
                            "idempotency key already identifies another transition"
                        )
                    return
            old_status = str(item["current_status"])
            if old_status == safe_status:
                return
            if safe_status not in _LESSON_TRANSITIONS[old_status]:
                raise ValueError(
                    f"invalid lesson transition {old_status} -> {safe_status}"
                )
            if timestamp <= float(item["updated_at"]):
                raise ValueError(
                    "transitioned_at must be newer than the current lesson"
                )
            deleted_at = timestamp if safe_status == "retracted" else None
            conn.execute(
                "UPDATE experience_items SET current_status = ?, updated_at = ?, "
                "deleted_at = ? WHERE id = ?",
                (safe_status, timestamp, deleted_at, safe_item_id),
            )
            event_type = "approved" if safe_status == "active" else safe_status
            payload = self._json_dumps(
                {
                    "actor": safe_actor,
                    "from_status": old_status,
                    "reason": safe_reason,
                    "to_status": safe_status,
                },
                "event payload",
                _MAX_EVENT_JSON_BYTES,
            )
            conn.execute(
                """
                INSERT INTO experience_events(
                    id, event_type, item_id, item_revision, retrieval_id,
                    work_id, payload_json, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    safe_event_id,
                    event_type,
                    safe_item_id,
                    item["current_revision"],
                    payload,
                    safe_idempotency,
                    timestamp,
                ),
            )

        self._execute_write(transition)
        result = self.get_item(safe_item_id)
        assert result is not None
        return result
    def transition_decision(self, item_id: str, status: str, **kwargs: Any) -> dict[str, Any]:
        """Apply a bounded Decision lifecycle transition.

        This generic transition path never activates Decisions. Use
        activate_decision or reapprove_decision with trusted host authority.
        """
        safe_item_id = self._identifier(item_id, "item_id")
        safe_status = _require_choice("status", status, _DECISION_STATUSES)
        safe_actor = self._text(
            kwargs.get("actor", "user"), "actor", max_chars=128, allow_empty=False
        )
        safe_reason = self._text(
            kwargs.get("reason"), "reason", max_chars=1_000, nullable=True
        )
        safe_idempotency = self._identifier(
            kwargs.get("idempotency_key"), "idempotency_key", nullable=True
        )
        safe_event_id = self._identifier(
            kwargs.get("event_id") or _new_id("event"), "event_id"
        )
        timestamp = _now(kwargs.get("transitioned_at"))
        event_type = {
            "review_required": "review_required",
            "superseded": "superseded",
            "revoked": "revoked",
        }.get(safe_status, safe_status)

        def transition(conn: sqlite3.Connection) -> None:
            item = conn.execute(
                "SELECT kind, current_status, current_revision, updated_at "
                "FROM experience_items WHERE id = ?", (safe_item_id,)
            ).fetchone()
            if item is None or item["kind"] != "decision":
                raise KeyError(f"unknown decision {safe_item_id}")
            if safe_idempotency:
                replay = conn.execute(
                    "SELECT * FROM experience_events WHERE idempotency_key = ?",
                    (safe_idempotency,),
                ).fetchone()
                if replay is not None:
                    try:
                        replay_payload = json.loads(replay["payload_json"])
                    except (TypeError, ValueError):
                        replay_payload = {}
                    if (
                        replay["event_type"] != event_type
                        or replay["item_id"] != safe_item_id
                        or replay_payload.get("actor") != safe_actor
                        or replay_payload.get("reason") != safe_reason
                        or replay_payload.get("to_status") != safe_status
                    ):
                        raise ValueError(
                            "idempotency key already identifies another decision transition"
                        )
                    return
            old_status = str(item["current_status"])
            if old_status == safe_status:
                return
            if safe_status not in _DECISION_TRANSITIONS[old_status]:
                raise ValueError(
                    f"invalid decision transition {old_status} -> {safe_status}"
                )
            if safe_status == "active":
                raise ValueError("activate_decision requires trusted authority")
            if timestamp <= float(item["updated_at"]):
                raise ValueError("transitioned_at must be newer than the current decision")
            deleted_at = timestamp if safe_status in {"superseded", "revoked"} else None
            conn.execute(
                "UPDATE experience_items SET current_status = ?, updated_at = ?, "
                "deleted_at = ? WHERE id = ?",
                (safe_status, timestamp, deleted_at, safe_item_id),
            )
            payload = self._json_dumps(
                {
                    "actor": safe_actor,
                    "from_status": old_status,
                    "reason": safe_reason,
                    "to_status": safe_status,
                },
                "event payload",
                _MAX_EVENT_JSON_BYTES,
            )
            conn.execute(
                """
                INSERT INTO experience_events(
                    id, event_type, item_id, item_revision, retrieval_id,
                    work_id, payload_json, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    safe_event_id,
                    event_type,
                    safe_item_id,
                    item["current_revision"],
                    payload,
                    safe_idempotency,
                    timestamp,
                ),
            )

        self._execute_write(transition)
        result = self.get_item(safe_item_id)
        assert result is not None
        return result

    def approve_lesson(self, item_id: str, **kwargs: Any) -> dict[str, Any]:
        """Activate a candidate; approval is always an explicit transition."""
        return self.transition_lesson(item_id, "active", **kwargs)

    def retract_lesson(self, item_id: str, **kwargs: Any) -> dict[str, Any]:
        """Logically delete a candidate/active/disputed lesson immediately."""
        return self.transition_lesson(item_id, "retracted", **kwargs)

    def purge_item(
        self,
        item_id: str,
        *,
        vacuum: bool = True,
    ) -> dict[str, Any]:
        """Best-effort physical purge of an item and all dependent rows.

        SQLite secure deletion is enabled before deleting.  The committed
        purge is not rolled back if a later WAL checkpoint or exclusive
        ``VACUUM`` cannot obtain its maintenance lock.
        """
        if not isinstance(vacuum, bool):
            raise TypeError("vacuum must be bool")
        safe_item_id = self._identifier(item_id, "item_id")

        def delete(conn: sqlite3.Connection) -> bool:
            conn.execute("PRAGMA secure_delete=ON")
            cursor = conn.execute(
                "DELETE FROM experience_items WHERE id = ?", (safe_item_id,)
            )
            return cursor.rowcount > 0

        purged = self._execute_write(delete)
        checkpointed = False
        vacuumed = False
        if purged:
            try:
                with self._lock:
                    self._connection().execute("PRAGMA wal_checkpoint(TRUNCATE)")
                checkpointed = True
            except sqlite3.Error:
                pass
            if vacuum:
                try:
                    with self._lock:
                        self._connection().execute("VACUUM")
                    vacuumed = True
                except sqlite3.Error:
                    pass
        return {
            "item_id": safe_item_id,
            "purged": purged,
            "checkpointed": checkpointed,
            "vacuumed": vacuumed,
        }

    # ------------------------------------------------------------------
    # Scope policy
    # ------------------------------------------------------------------

    def upsert_scope_policy(
        self,
        *,
        principal_id: str,
        repository_id: str,
        project_id: str,
        project_root_rel: str,
        workspace_root: str | None = None,
        capture_allowed: bool = False,
        recall_allowed: bool = False,
        injection_allowed: bool = False,
        reflection_allowed: bool = False,
        max_egress_policy: str = "local_only",
        updated_at: float | None = None,
    ) -> dict[str, Any]:
        """Create or replace one explicit, default-deny project policy."""
        for field, value in (
            ("capture_allowed", capture_allowed),
            ("recall_allowed", recall_allowed),
            ("injection_allowed", injection_allowed),
            ("reflection_allowed", reflection_allowed),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{field} must be bool")
        safe_principal = self._principal(principal_id)
        safe_repository = self._identifier(repository_id, "repository_id")
        safe_project = self._identifier(project_id, "project_id")
        # These two paths are authorization inputs, not display text.  Preserve
        # their exact canonical form; callers sanitize them separately for UI.
        safe_project_root = self._project_root_rel(project_root_rel)
        safe_workspace_root = self._workspace_root(
            workspace_root, nullable=True
        )
        if (
            safe_project_root.startswith("/")
            or "\\" in safe_project_root
            or ".." in Path(safe_project_root).parts
        ):
            raise ValueError("project_root_rel must remain repository-relative")
        if safe_workspace_root is not None and safe_project_root != ".":
            raise ValueError("workspace policies require project_root_rel='.'")
        safe_egress = _require_choice(
            "max_egress_policy", max_egress_policy, _EGRESS_POLICIES
        )
        timestamp = _now(updated_at)

        def upsert(conn: sqlite3.Connection) -> None:
            existing = conn.execute(
                "SELECT * FROM experience_scope_policies "
                "WHERE principal_id = ? AND repository_id = ? AND project_id = ?",
                (safe_principal, safe_repository, safe_project),
            ).fetchone()
            desired = (
                safe_project_root,
                safe_workspace_root,
                int(capture_allowed),
                int(recall_allowed),
                int(injection_allowed),
                int(reflection_allowed),
                safe_egress,
            )
            if existing is not None:
                observed = tuple(
                    existing[field]
                    for field in (
                        "project_root_rel",
                        "workspace_root",
                        "capture_allowed",
                        "recall_allowed",
                        "injection_allowed",
                        "reflection_allowed",
                        "max_egress_policy",
                    )
                )
                if timestamp < float(existing["updated_at"]):
                    raise ValueError("stale scope policy update")
                if timestamp == float(existing["updated_at"]):
                    if observed != desired:
                        raise ValueError("conflicting scope policy update timestamp")
                    return
            conn.execute(
                """
                INSERT INTO experience_scope_policies(
                    principal_id, repository_id, project_id, project_root_rel,
                    workspace_root, capture_allowed, recall_allowed,
                    injection_allowed, reflection_allowed, max_egress_policy,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(principal_id, repository_id, project_id) DO UPDATE SET
                    project_root_rel = excluded.project_root_rel,
                    workspace_root = excluded.workspace_root,
                    capture_allowed = excluded.capture_allowed,
                    recall_allowed = excluded.recall_allowed,
                    injection_allowed = excluded.injection_allowed,
                    reflection_allowed = excluded.reflection_allowed,
                    max_egress_policy = excluded.max_egress_policy,
                    updated_at = excluded.updated_at
                """,
                (
                    safe_principal,
                    safe_repository,
                    safe_project,
                    safe_project_root,
                    safe_workspace_root,
                    int(capture_allowed),
                    int(recall_allowed),
                    int(injection_allowed),
                    int(reflection_allowed),
                    safe_egress,
                    timestamp,
                ),
            )

        self._execute_write(upsert)
        result = self.get_scope_policy(
            principal_id=safe_principal,
            repository_id=safe_repository,
            project_id=safe_project,
        )
        assert result is not None
        return result

    def get_scope_policy(
        self,
        *,
        principal_id: str,
        repository_id: str,
        project_id: str,
    ) -> dict[str, Any] | None:
        """Return exact operational policy data for scope authorization."""
        key = (
            self._principal(principal_id),
            self._identifier(repository_id, "repository_id"),
            self._identifier(project_id, "project_id"),
        )
        with self._lock:
            row = self._connection().execute(
                "SELECT * FROM experience_scope_policies "
                "WHERE principal_id = ? AND repository_id = ? AND project_id = ?",
                key,
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in (
            "capture_allowed",
            "recall_allowed",
            "injection_allowed",
            "reflection_allowed",
        ):
            result[field] = bool(result.get(field, False))
        return result

    def list_scope_policies(
        self,
        *,
        principal_id: str,
        repository_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["principal_id = ?"]
        params: list[Any] = [self._principal(principal_id)]
        if repository_id is not None:
            clauses.append("repository_id = ?")
            params.append(self._identifier(repository_id, "repository_id"))
        with self._lock:
            rows = self._connection().execute(
                f"SELECT * FROM experience_scope_policies WHERE {' AND '.join(clauses)} "
                "ORDER BY repository_id, length(project_root_rel) DESC, "
                "project_root_rel, project_id",
                params,
            ).fetchall()
        result = [dict(row) for row in rows]
        for policy in result:
            for field in (
                "capture_allowed",
                "recall_allowed",
                "injection_allowed",
                "reflection_allowed",
            ):
                policy[field] = bool(policy.get(field, False))
        return result

    # ------------------------------------------------------------------
    # Authorized deterministic retrieval
    # ------------------------------------------------------------------

    @staticmethod
    def _authorization_sql(
        *,
        require_injection_allowed: bool,
        kind: str = "lesson",
        require_decision_validity: bool = False,
    ) -> str:
        safe_kind = _require_choice("kind", kind, {"lesson", "decision"})
        injection_clause = (
            "AND p.injection_allowed = 1" if require_injection_allowed else ""
        )
        decision_validity_clause = ""
        if require_decision_validity:
            decision_validity_clause = """
                AND json_extract(r.body_json, '$.authority') IN ('user', 'repository_policy')
                AND json_extract(r.body_json, '$.effective_at') <= ?
                AND (
                    json_extract(r.body_json, '$.expires_at') IS NULL
                    OR json_extract(r.body_json, '$.expires_at') > ?
                )
                AND (
                    r.review_after IS NULL
                    OR r.review_after > ?
                )
            """
        return f"""
            i.kind = '{safe_kind}'
            AND i.current_status = 'active'
            AND i.deleted_at IS NULL
            AND i.principal_id = ?
            AND i.scope_type = ?
            AND i.scope_id = ?
            AND i.repository_id IS ?
            AND i.project_id IS ?
            AND i.sensitivity != 'blocked'
            AND p.recall_allowed = 1
            AND (
                ? = 1
                OR (
                    i.sensitivity != 'local_only'
                    AND i.egress_policy != 'local_only'
                    AND p.max_egress_policy != 'local_only'
                    AND (
                        (
                            (
                                i.sensitivity = 'private_repo'
                                OR i.egress_policy = 'same_provider_trust_domain'
                                OR p.max_egress_policy = 'same_provider_trust_domain'
                            )
                            AND i.producer_trust_domain = ?
                        )
                        OR (
                            i.sensitivity != 'private_repo'
                            AND i.egress_policy = 'explicit_any_provider'
                            AND p.max_egress_policy = 'explicit_any_provider'
                        )
                    )
                )
            )
            {decision_validity_clause}
            {injection_clause}
        """

    @staticmethod
    def _authorization_params(
        *,
        principal_id: str,
        scope_type: str,
        scope_id: str,
        repository_id: str | None,
        project_id: str | None,
        provider_trust_domain: str,
        provider_is_local: bool,
    ) -> tuple[Any, ...]:
        return (
            principal_id,
            scope_type,
            scope_id,
            repository_id,
            project_id,
            int(provider_is_local),
            provider_trust_domain,
        )

    @staticmethod
    def _decision_authority_params(now: float) -> tuple[float, float, float]:
        return (now, now, now)

    def _trigram_table_available(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'experience_search_trigram'"
        ).fetchone()
        return row is not None

    def _search_trigram(
        self,
        *,
        ids: Sequence[str],
        query: str,
    ) -> tuple[dict[str, int], dict[str, int]]:
        if not query or not ids:
            return {}, {}
        trigram_order: dict[str, int] = {}
        trigram_match_counts: dict[str, int] = {}
        with self._lock:
            conn = self._connection()
            if not self._trigram_table_available(conn):
                return trigram_order, trigram_match_counts
            try:
                for start in range(0, len(ids), 200):
                    chunk = ids[start : start + 200]
                    chunk_placeholders = ",".join("?" for _ in chunk)
                    trigram_rows = conn.execute(
                        f"""
                        SELECT c.item_id, c.revision, c.title,
                               c.searchable_text, c.tags,
                               bm25(experience_search_trigram) AS text_rank
                        FROM experience_search_trigram
                        JOIN experience_search_content AS c
                          ON c.rowid = experience_search_trigram.rowid
                        WHERE experience_search_trigram MATCH ?
                          AND c.item_id IN ({chunk_placeholders})
                        ORDER BY text_rank, c.item_id
                        """,
                        (query, *chunk),
                    ).fetchall()
                for row in trigram_rows:
                    item_id = row["item_id"]
                    if item_id not in trigram_order:
                        trigram_order[item_id] = len(trigram_order)
                        trigram_match_counts[item_id] = 1
                    else:
                        trigram_match_counts[item_id] += 1
            except sqlite3.OperationalError:
                trigram_order = {}
                trigram_match_counts = {}
        return trigram_order, trigram_match_counts

    def _search_unicode61(
        self,
        *,
        ids: Sequence[str],
        query: str,
    ) -> tuple[dict[str, int], dict[str, int]]:
        if not query or not ids:
            return {}, {}
        fts_order: dict[str, int] = {}
        fts_match_counts: dict[str, int] = {}
        with self._lock:
            conn = self._connection()
            try:
                for start in range(0, len(ids), 200):
                    chunk = ids[start : start + 200]
                    chunk_placeholders = ",".join("?" for _ in chunk)
                    fts_rows = conn.execute(
                        f"""
                        SELECT c.item_id, c.revision, c.title,
                               c.searchable_text, c.tags,
                               bm25(experience_search) AS text_rank
                        FROM experience_search
                        JOIN experience_search_content AS c
                          ON c.rowid = experience_search.rowid
                        WHERE experience_search MATCH ?
                          AND c.item_id IN ({chunk_placeholders})
                        ORDER BY text_rank, c.item_id
                        """,
                        (query, *chunk),
                    ).fetchall()
                for row in fts_rows:
                    item_id = row["item_id"]
                    if item_id not in fts_order:
                        fts_order[item_id] = len(fts_order)
                        fts_match_counts[item_id] = 1
                    else:
                        fts_match_counts[item_id] += 1
            except sqlite3.OperationalError:
                fts_order = {}
                fts_match_counts = {}
        return fts_order, fts_match_counts

    def _search_short_cjk(
        self,
        *,
        ids: Sequence[str],
        terms: Sequence[str],
    ) -> dict[str, int]:
        if not terms or not ids:
            return {}
        matches: dict[str, int] = {}
        like_clauses = " OR ".join(
            "title LIKE ? ESCAPE '\\' OR searchable_text LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\'" for _ in terms
        )
        params = [f"%{term}%" for term in terms for _ in range(3)]
        with self._lock:
            conn = self._connection()
            for start in range(0, len(ids), 200):
                chunk = ids[start : start + 200]
                chunk_placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT item_id, revision
                    FROM experience_search_content
                    WHERE item_id IN ({chunk_placeholders})
                      AND ({like_clauses})
                    """,
                    (*chunk, *params),
                ).fetchall()
                for row in rows:
                    matches[row["item_id"]] = matches.get(row["item_id"], 0) + 1
        return matches

    def search_decisions(
        self,
        *,
        principal_id: str,
        scope_type: str,
        scope_id: str,
        repository_id: str | None,
        project_id: str | None,
        provider_trust_domain: str,
        provider_is_local: bool = False,
        query: str = "",
        tags: Mapping[Any, Iterable[Any]] | Iterable[tuple[Any, Any]] | None = None,
        min_confidence: float = 0.0,
        require_injection_allowed: bool = True,
        limit: int = 20,
        repository_root: str | Path | None = None,
        policy_repository_id: str | None = None,
        policy_project_id: str | None = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve exact-scope active decisions with hard pre-text filters.

        Decision retrieval is intentionally stricter than lesson retrieval:
        only active, non-expired, non-review-required decisions with valid
        authority and egress policy may be returned. Repository-policy
        decisions are additionally rechecked against the live anchor when
        ``repository_root`` is supplied.
        """
        safe_principal = self._principal(principal_id)
        safe_scope_type = _require_choice("scope_type", scope_type, _SCOPE_TYPES)
        safe_scope_id = self._identifier(scope_id, "scope_id")
        safe_repository = self._identifier(repository_id, "repository_id", nullable=True)
        safe_project = self._identifier(project_id, "project_id", nullable=True)
        safe_provider = self._trust_domain(provider_trust_domain)
        if not isinstance(provider_is_local, bool):
            raise TypeError("provider_is_local must be bool")
        if not isinstance(require_injection_allowed, bool):
            raise TypeError("require_injection_allowed must be bool")
        if safe_scope_type == "project" and (not safe_repository or not safe_project):
            raise ValueError("project retrieval requires repository_id and project_id")
        if safe_scope_type == "repository" and not safe_repository:
            raise ValueError("repository retrieval requires repository_id")
        if safe_scope_type == "project" and safe_scope_id != safe_project:
            raise ValueError("project scope_id must equal project_id")
        if safe_scope_type == "repository" and safe_scope_id != safe_repository:
            raise ValueError("repository scope_id must equal repository_id")
        safe_query = self._text(query, "retrieval query", max_chars=4_000) or ""
        requested_tags = self._normalize_tags(tags)
        safe_min_confidence = _validate_confidence(float(min_confidence))
        assert safe_min_confidence is not None
        safe_limit = max(0, min(int(limit), 100))
        if safe_limit == 0:
            return []
        policy_repository = (
            self._identifier(policy_repository_id, "policy_repository_id", nullable=True)
            if policy_repository_id is not None
            else safe_repository
        )
        policy_project = (
            self._identifier(policy_project_id, "policy_project_id", nullable=True)
            if policy_project_id is not None
            else safe_project
        )
        if not policy_repository or not policy_project:
            raise ValueError("retrieval requires a current repository and project policy")
        current_time = float(time.time() if now is None else now)
        auth_sql = self._authorization_sql(
            require_injection_allowed=require_injection_allowed,
            kind="decision",
            require_decision_validity=True,
        )
        item_repository = safe_repository if safe_scope_type != "profile" else None
        item_project = safe_project if safe_scope_type == "project" else None
        auth_params = self._authorization_params(
            principal_id=safe_principal,
            scope_type=safe_scope_type,
            scope_id=safe_scope_id,
            repository_id=item_repository,
            project_id=item_project,
            provider_trust_domain=safe_provider,
            provider_is_local=bool(provider_is_local),
        ) + self._decision_authority_params(current_time)
        with self._lock:
            eligible_rows = self._connection().execute(
                f"""
                SELECT i.id, i.current_revision, i.created_at, i.updated_at,
                       r.confidence, r.last_validated_at
                FROM experience_items AS i
                JOIN experience_item_revisions AS r
                  ON r.item_id = i.id AND r.revision = i.current_revision
                JOIN experience_scope_policies AS p
                  ON p.principal_id = i.principal_id
                 AND p.repository_id = ?
                 AND p.project_id = ?
                WHERE {auth_sql}
                  AND COALESCE(r.confidence, 0) >= ?
                ORDER BY i.id
                """,
                (
                    policy_repository,
                    policy_project,
                    *auth_params,
                    safe_min_confidence,
                ),
            ).fetchall()
        if not eligible_rows:
            return []
        eligible = {
            row["id"]: {
                "revision": int(row["current_revision"]),
                "confidence": float(row["confidence"] or 0.0),
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
                "last_validated_at": row["last_validated_at"],
            }
            for row in eligible_rows
        }
        ids = sorted(eligible)
        requested_tag_pairs = list(requested_tags)
        if requested_tag_pairs and not safe_query:
            pair_sql = " OR ".join(
                "(t.namespace = ? AND t.value = ?)"
                for _ in requested_tag_pairs
            )
            tag_rows: list[sqlite3.Row] = []
            with self._lock:
                for start in range(0, len(ids), 200):
                    chunk = ids[start : start + 200]
                    placeholders = ",".join("?" for _ in chunk)
                    tag_rows.extend(
                        self._connection().execute(
                            f"""
                            SELECT DISTINCT t.item_id, t.revision,
                                   t.namespace, t.value
                            FROM experience_tags AS t
                            JOIN experience_items AS i
                              ON i.id = t.item_id
                             AND i.current_revision = t.revision
                            WHERE ({pair_sql})
                              AND i.id IN ({placeholders})
                            """,
                            (
                                *(
                                    value
                                    for namespace, value in requested_tag_pairs
                                    for value in (namespace, value)
                                ),
                                *chunk,
                            ),
                        ).fetchall()
                    )
            if not tag_rows:
                return []
            ids = sorted({row["item_id"] for row in tag_rows} & set(ids))
        else:
            tag_rows: list[sqlite3.Row] = []
        for start in range(0, len(ids), 200):
            chunk = ids[start : start + 200]
            placeholders = ",".join("?" for _ in chunk)
            with self._lock:
                tag_rows.extend(
                    self._connection().execute(
                        f"""
                        SELECT item_id, revision, namespace, value
                        FROM experience_tags
                        WHERE item_id IN ({placeholders})
                        ORDER BY item_id, namespace, value
                        """,
                        chunk,
                    ).fetchall()
                )
        tags_by_item: dict[str, set[tuple[str, str]]] = {
            item_id: set() for item_id in ids
        }
        for row in tag_rows:
            if int(row["revision"]) == eligible[row["item_id"]]["revision"]:
                tags_by_item[row["item_id"]].add((row["namespace"], row["value"]))
        namespace_weights = {
            "failure": 8.0,
            "task_type": 6.0,
            "technology": 4.0,
            "component": 3.0,
            "entity": 2.0,
        }
        namespace_order = {
            "failure": 0,
            "task_type": 1,
            "technology": 2,
            "component": 3,
            "entity": 4,
        }
        requested_set = set(requested_tags)
        query_terms = set(_fts_query_terms(safe_query))
        metadata_matches: dict[str, list[tuple[str, str]]] = {}
        inferred_metadata_matches: dict[str, list[tuple[str, str]]] = {}
        for item_id in ids:
            metadata_matches[item_id] = sorted(
                tags_by_item[item_id] & requested_set,
                key=lambda pair: (namespace_order[pair[0]], pair[1]),
            )
            inferred_metadata_matches[item_id] = sorted(
                (
                    pair
                    for pair in tags_by_item[item_id] - requested_set
                    if self._fts_enabled
                    and (tag_terms := set(_fts_query_terms(pair[1], limit=8)))
                    and tag_terms.issubset(query_terms)
                ),
                key=lambda pair: (namespace_order[pair[0]], pair[1]),
            )
        ascii_query = _sanitize_fts_query(" ".join(sorted(term for term in _fts_query_terms(safe_query) if not _contains_cjk(term))))
        trigram_query = _sanitize_trigram_query(safe_query)
        short_cjk_terms = _short_cjk_terms(safe_query)
        fts_order, fts_match_counts = self._search_unicode61(ids=ids, query=ascii_query)
        trigram_order, trigram_match_counts = self._search_trigram(ids=ids, query=trigram_query)
        short_cjk_matches = self._search_short_cjk(ids=ids, terms=short_cjk_terms)
        ranked: list[dict[str, Any]] = []
        for item_id in ids:
            exact_tags = metadata_matches[item_id]
            inferred_tags = inferred_metadata_matches[item_id]
            has_text_match = item_id in fts_order
            has_trigram_match = item_id in trigram_order
            has_short_cjk_match = item_id in short_cjk_matches
            if (
                (safe_query or requested_tags)
                and not exact_tags
                and not inferred_tags
                and not has_text_match
                and not has_trigram_match
                and not has_short_cjk_match
            ):
                continue
            reasons = [f"{safe_scope_type} exact"]
            tag_score = 0.0
            for namespace, value in exact_tags:
                tag_score += namespace_weights[namespace]
                reasons.append(f"{namespace.replace('_', ' ')} exact: {value}")
            for namespace, value in inferred_tags:
                tag_score += namespace_weights[namespace] * 0.75
                reasons.append(f"{namespace.replace('_', ' ')} query match: {value}")
            text_score = 0.0
            if has_text_match:
                text_score = 1.0 / (1.0 + fts_order[item_id])
                reasons.append(f"text term overlap ({fts_match_counts[item_id]})")
            if has_trigram_match:
                text_score += 1.5 / (1.0 + trigram_order[item_id])
                reasons.append(f"trigram term overlap ({trigram_match_counts[item_id]})")
            if has_short_cjk_match:
                text_score += 0.75
                reasons.append(f"short CJK fallback ({short_cjk_matches[item_id]})")
            confidence_score = eligible[item_id]["confidence"]
            ranked.append(
                {
                    "item_id": item_id,
                    "revision": eligible[item_id]["revision"],
                    "score": tag_score + text_score + confidence_score,
                    "confidence": confidence_score,
                    "match_reasons": reasons,
                    "last_validated_at": eligible[item_id]["last_validated_at"],
                    "updated_at": eligible[item_id]["updated_at"],
                }
            )
        ranked.sort(
            key=lambda result: (
                -result["score"],
                -result["confidence"],
                -float(result["last_validated_at"] or 0.0),
                -result["updated_at"],
                result["item_id"],
            )
        )
        results: list[dict[str, Any]] = []
        for candidate in ranked[:safe_limit]:
            authorized = self._get_authorized_item(
                candidate["item_id"],
                revision=candidate["revision"],
                repository_id=safe_repository,
                project_id=safe_project,
                policy_repository_id=policy_repository,
                policy_project_id=policy_project,
                auth_sql=auth_sql,
                auth_params=auth_params,
            )
            if authorized is None:
                continue
            body = authorized["revision"]["body"]
            if repository_root is not None and body.get("authority") == "repository_policy":
                validation = validate_repository_anchor(
                    body["policy_anchor_path"],
                    body["policy_anchor_hash"],
                    repository_root=repository_root,
                )
                if not validation.valid:
                    continue
            authorized["score"] = candidate["score"]
            authorized["match_reasons"] = [
                self._returned_text(reason, "match reason")
                for reason in candidate["match_reasons"]
            ]
            results.append(authorized)
        return results

    def authorized_decision_revisions(
        self,
        *,
        principal_id: str,
        scope_type: str,
        scope_id: str,
        repository_id: str | None,
        project_id: str | None,
        provider_trust_domain: str,
        provider_is_local: bool = False,
        candidates: Iterable[tuple[str, int]],
        require_injection_allowed: bool = True,
        now: float | None = None,
        repository_root: str | Path | None = None,
        policy_repository_id: str | None = None,
        policy_project_id: str | None = None,
    ) -> set[tuple[str, int]]:
        """Revalidate a bounded cached Decision candidate set without reading text."""
        safe_principal = self._principal(principal_id)
        safe_scope_type = _require_choice("scope_type", scope_type, _SCOPE_TYPES)
        safe_scope_id = self._identifier(scope_id, "scope_id")
        safe_repository = self._identifier(repository_id, "repository_id", nullable=True)
        safe_project = self._identifier(project_id, "project_id", nullable=True)
        safe_provider = self._trust_domain(provider_trust_domain)
        if not isinstance(provider_is_local, bool):
            raise TypeError("provider_is_local must be bool")
        if not isinstance(require_injection_allowed, bool):
            raise TypeError("require_injection_allowed must be bool")
        policy_repository = (
            self._identifier(policy_repository_id, "policy_repository_id", nullable=True)
            if policy_repository_id is not None
            else safe_repository
        )
        policy_project = (
            self._identifier(policy_project_id, "policy_project_id", nullable=True)
            if policy_project_id is not None
            else safe_project
        )
        if not policy_repository or not policy_project:
            raise ValueError("authorization requires a current repository and project policy")
        if safe_scope_type == "project" and safe_scope_id != safe_project:
            raise ValueError("project scope_id must equal project_id")
        if safe_scope_type == "repository" and safe_scope_id != safe_repository:
            raise ValueError("repository scope_id must equal repository_id")
        requested: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        for item_id, revision in candidates:
            safe_item = self._identifier(item_id, "item_id")
            if isinstance(revision, bool) or int(revision) < 1:
                raise ValueError("revision must be a positive integer")
            key = (safe_item, int(revision))
            if key not in seen:
                seen.add(key)
                requested.append(key)
            if len(requested) > 100:
                raise ValueError("authorization candidate set exceeds 100 items")
        if not requested:
            return set()
        current_time = float(time.time() if now is None else now)
        auth_sql = self._authorization_sql(
            require_injection_allowed=require_injection_allowed,
            kind="decision",
            require_decision_validity=True,
        )
        item_repository = safe_repository if safe_scope_type != "profile" else None
        item_project = safe_project if safe_scope_type == "project" else None
        auth_params = self._authorization_params(
            principal_id=safe_principal,
            scope_type=safe_scope_type,
            scope_id=safe_scope_id,
            repository_id=item_repository,
            project_id=item_project,
            provider_trust_domain=safe_provider,
            provider_is_local=provider_is_local,
        ) + self._decision_authority_params(current_time)
        values_sql = ", ".join("(?, ?)" for _ in requested)
        requested_params = [value for pair in requested for value in pair]
        with self._lock:
            rows = self._connection().execute(
                f"""
                WITH requested(item_id, revision) AS (VALUES {values_sql})
                SELECT i.id, i.current_revision, r.body_json
                FROM requested AS q
                JOIN experience_items AS i
                  ON i.id = q.item_id AND i.current_revision = q.revision
                JOIN experience_item_revisions AS r
                  ON r.item_id = i.id AND r.revision = i.current_revision
                JOIN experience_scope_policies AS p
                  ON p.principal_id = i.principal_id
                 AND p.repository_id = ?
                 AND p.project_id = ?
                WHERE {auth_sql}
                ORDER BY i.id
                """,
                (
                    *requested_params,
                    policy_repository,
                    policy_project,
                    *auth_params,
                ),
            ).fetchall()
        authorized: set[tuple[str, int]] = set()
        for row in rows:
            item_id = str(row["id"])
            revision = int(row["current_revision"])
            body = json.loads(row["body_json"])
            if repository_root is not None and body.get("authority") == "repository_policy":
                validation = validate_repository_anchor(
                    body["policy_anchor_path"],
                    body["policy_anchor_hash"],
                    repository_root=repository_root,
                )
                if not validation.valid:
                    continue
            authorized.add((item_id, revision))
        return authorized

    def search_lessons(
        self,
        *,
        principal_id: str,
        scope_type: str,
        scope_id: str,
        repository_id: str | None,
        project_id: str | None,
        provider_trust_domain: str,
        provider_is_local: bool = False,
        query: str = "",
        tags: Mapping[Any, Iterable[Any]] | Iterable[tuple[Any, Any]] | None = None,
        min_confidence: float = 0.0,
        require_injection_allowed: bool = True,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Retrieve exact-scope active lessons with hard pre-text filters.

        FTS5 is an optional ranking feature, never an authorization feature.
        When FTS5 is absent a tag query still works; a free-text-only query
        safely returns no lessons instead of injecting unrelated content.
        ``provider_is_local`` is explicit and defaults false so ``local_only``
        material fails closed for unknown providers.
        """
        safe_principal = self._principal(principal_id)
        safe_scope_type = _require_choice("scope_type", scope_type, _SCOPE_TYPES)
        safe_scope_id = self._identifier(scope_id, "scope_id")
        safe_repository = self._identifier(
            repository_id, "repository_id", nullable=True
        )
        safe_project = self._identifier(project_id, "project_id", nullable=True)
        safe_provider = self._trust_domain(provider_trust_domain)
        if not isinstance(provider_is_local, bool):
            raise TypeError("provider_is_local must be bool")
        if not isinstance(require_injection_allowed, bool):
            raise TypeError("require_injection_allowed must be bool")
        if safe_scope_type != "profile" and (not safe_repository or not safe_project):
            raise ValueError("retrieval requires a current repository and project policy")
        if safe_scope_type == "project" and safe_scope_id != safe_project:
            raise ValueError("project scope_id must equal project_id")
        if safe_scope_type == "repository" and safe_scope_id != safe_repository:
            raise ValueError("repository scope_id must equal repository_id")
        safe_query = self._text(
            query, "retrieval query", max_chars=4_000
        ) or ""
        requested_tags = self._normalize_tags(tags)
        safe_min_confidence = _validate_confidence(float(min_confidence))
        assert safe_min_confidence is not None
        safe_limit = max(0, min(int(limit), 100))
        if safe_limit == 0:
            return []

        auth_sql = self._authorization_sql(
            require_injection_allowed=require_injection_allowed
        )
        item_repository = safe_repository if safe_scope_type != "profile" else None
        item_project = safe_project if safe_scope_type == "project" else None
        auth_params = self._authorization_params(
            principal_id=safe_principal,
            scope_type=safe_scope_type,
            scope_id=safe_scope_id,
            repository_id=item_repository,
            project_id=item_project,
            provider_trust_domain=safe_provider,
            provider_is_local=bool(provider_is_local),
        )
        with self._lock:
            if requested_tags and not safe_query:
                pair_sql = " OR ".join(
                    "(t.namespace = ? AND t.value = ?)"
                    for _ in requested_tags
                )
                # Tag-only retrieval is the hot path for scoped engineering
                # guidance. Start from the tag index rather than scanning all
                # active lessons in the project.
                eligible_rows = self._connection().execute(
                    f"""
                    SELECT DISTINCT i.id, i.current_revision, i.created_at,
                           i.updated_at, r.confidence, r.last_validated_at
                    FROM experience_tags AS t
                    JOIN experience_items AS i
                      ON i.id = t.item_id
                     AND i.current_revision = t.revision
                    JOIN experience_item_revisions AS r
                      ON r.item_id = i.id AND r.revision = i.current_revision
                    JOIN experience_scope_policies AS p
                      ON p.principal_id = i.principal_id
                     AND p.repository_id = i.repository_id
                     AND p.project_id = i.project_id
                    WHERE ({pair_sql})
                      AND {auth_sql}
                      AND COALESCE(r.confidence, 0) >= ?
                    ORDER BY i.id
                    """,
                    (
                        *(
                            value
                            for namespace, value in requested_tags
                            for value in (namespace, value)
                        ),
                        *auth_params,
                        safe_min_confidence,
                    ),
                ).fetchall()
            else:
                # This first stage intentionally selects no revision text.
                eligible_rows = self._connection().execute(
                    f"""
                    SELECT i.id, i.current_revision, i.created_at, i.updated_at,
                           r.confidence, r.last_validated_at
                    FROM experience_items AS i
                    JOIN experience_item_revisions AS r
                      ON r.item_id = i.id AND r.revision = i.current_revision
                    JOIN experience_scope_policies AS p
                      ON p.principal_id = i.principal_id
                     AND p.repository_id = i.repository_id
                     AND p.project_id = i.project_id
                    WHERE {auth_sql}
                      AND COALESCE(r.confidence, 0) >= ?
                    ORDER BY i.id
                    """,
                    (
                        *auth_params,
                        safe_min_confidence,
                    ),
                ).fetchall()
        if not eligible_rows:
            return []

        eligible = {
            row["id"]: {
                "revision": int(row["current_revision"]),
                "confidence": float(row["confidence"] or 0.0),
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
                "last_validated_at": row["last_validated_at"],
            }
            for row in eligible_rows
        }
        ids = sorted(eligible)
        requested_tag_pairs = list(requested_tags)
        if requested_tag_pairs and not safe_query:
            pair_sql = " OR ".join(
                "(t.namespace = ? AND t.value = ?)"
                for _ in requested_tag_pairs
            )
            tag_rows: list[sqlite3.Row] = []
            with self._lock:
                for start in range(0, len(ids), 200):
                    chunk = ids[start : start + 200]
                    placeholders = ",".join("?" for _ in chunk)
                    tag_rows.extend(
                        self._connection().execute(
                            f"""
                            SELECT DISTINCT t.item_id, t.revision,
                                   t.namespace, t.value
                            FROM experience_tags AS t
                            JOIN experience_items AS i
                              ON i.id = t.item_id
                             AND i.current_revision = t.revision
                            WHERE ({pair_sql})
                              AND i.id IN ({placeholders})
                            """,
                            (
                                *(
                                    value
                                    for namespace, value in requested_tag_pairs
                                    for value in (namespace, value)
                                ),
                                *chunk,
                            ),
                        ).fetchall()
                    )
            if not tag_rows:
                return []
            ids = sorted({row["item_id"] for row in tag_rows} & set(ids))
        else:
            tag_rows: list[sqlite3.Row] = []
        for start in range(0, len(ids), 200):
            chunk = ids[start : start + 200]
            placeholders = ",".join("?" for _ in chunk)
            with self._lock:
                tag_rows.extend(
                    self._connection().execute(
                        f"""
                        SELECT item_id, revision, namespace, value
                        FROM experience_tags
                        WHERE item_id IN ({placeholders})
                        ORDER BY item_id, namespace, value
                        """,
                        chunk,
                    ).fetchall()
                )

        tags_by_item: dict[str, set[tuple[str, str]]] = {
            item_id: set() for item_id in ids
        }
        for row in tag_rows:
            if int(row["revision"]) == eligible[row["item_id"]]["revision"]:
                tags_by_item[row["item_id"]].add(
                    (row["namespace"], row["value"])
                )

        namespace_weights = {
            "failure": 8.0,
            "task_type": 6.0,
            "technology": 4.0,
            "component": 3.0,
            "entity": 2.0,
        }
        namespace_order = {
            "failure": 0,
            "task_type": 1,
            "technology": 2,
            "component": 3,
            "entity": 4,
        }
        requested_set = set(requested_tags)
        query_terms = set(_fts_query_terms(safe_query))
        metadata_matches: dict[str, list[tuple[str, str]]] = {}
        inferred_metadata_matches: dict[str, list[tuple[str, str]]] = {}
        for item_id in ids:
            metadata_matches[item_id] = sorted(
                tags_by_item[item_id] & requested_set,
                key=lambda pair: (namespace_order[pair[0]], pair[1]),
            )
            inferred_metadata_matches[item_id] = sorted(
                (
                    pair
                    for pair in tags_by_item[item_id] - requested_set
                    if self._fts_enabled
                    and (tag_terms := set(_fts_query_terms(pair[1], limit=8)))
                    and tag_terms.issubset(query_terms)
                ),
                key=lambda pair: (namespace_order[pair[0]], pair[1]),
            )

        fts_order: dict[str, int] = {}
        fts_match_counts: dict[str, int] = {}
        fts_query = _sanitize_fts_query(safe_query)
        if self._fts_enabled and fts_query:
            # Restrict each FTS query to already-authorized identifiers.  Keep
            # chunks below SQLite's conservative variable limit.
            fts_rows: list[sqlite3.Row] = []
            for start in range(0, len(ids), 200):
                chunk = ids[start : start + 200]
                chunk_placeholders = ",".join("?" for _ in chunk)
                try:
                    with self._lock:
                        fts_rows.extend(
                            self._connection().execute(
                                f"""
                                SELECT c.item_id, c.revision, c.title,
                                       c.searchable_text, c.tags,
                                       bm25(experience_search) AS text_rank
                                FROM experience_search
                                JOIN experience_search_content AS c
                                  ON c.rowid = experience_search.rowid
                                WHERE experience_search MATCH ?
                                  AND c.item_id IN ({chunk_placeholders})
                                ORDER BY text_rank, c.item_id
                                """,
                                (fts_query, *chunk),
                            ).fetchall()
                        )
                except sqlite3.OperationalError:
                    # A corrupt/missing optional FTS index must never fall back
                    # to returning unranked body text.
                    fts_rows = []
                    break
            required_overlap = 1 if len(query_terms) <= 2 else 2
            valid_fts = []
            for row in fts_rows:
                if (
                    row["item_id"] not in eligible
                    or int(row["revision"]) != eligible[row["item_id"]]["revision"]
                ):
                    continue
                document_terms = set(
                    _fts_query_terms(
                        " ".join(
                            (
                                row["title"] or "",
                                row["searchable_text"] or "",
                                row["tags"] or "",
                            )
                        ),
                        limit=512,
                    )
                )
                overlap_count = len(query_terms & document_terms)
                if overlap_count < required_overlap:
                    continue
                fts_match_counts[row["item_id"]] = overlap_count
                valid_fts.append(row)
            valid_fts.sort(key=lambda row: (float(row["text_rank"]), row["item_id"]))
            for index, row in enumerate(valid_fts):
                fts_order.setdefault(row["item_id"], index)

        ranked: list[dict[str, Any]] = []
        for item_id in ids:
            exact_tags = metadata_matches[item_id]
            inferred_tags = inferred_metadata_matches[item_id]
            has_text_match = item_id in fts_order
            if (
                (safe_query or requested_tags)
                and not exact_tags
                and not inferred_tags
                and not has_text_match
            ):
                continue
            reasons = [f"{safe_scope_type} exact"]
            tag_score = 0.0
            for namespace, value in exact_tags:
                tag_score += namespace_weights[namespace]
                reasons.append(
                    f"{namespace.replace('_', ' ')} exact: {value}"
                )
            for namespace, value in inferred_tags:
                tag_score += namespace_weights[namespace] * 0.75
                reasons.append(
                    f"{namespace.replace('_', ' ')} query match: {value}"
                )
            text_score = 0.0
            if has_text_match:
                text_score = 1.0 / (1.0 + fts_order[item_id])
                reasons.append(
                    f"text term overlap ({fts_match_counts[item_id]})"
                )
            confidence_score = eligible[item_id]["confidence"]
            ranked.append(
                {
                    "item_id": item_id,
                    "revision": eligible[item_id]["revision"],
                    "score": tag_score + text_score + confidence_score,
                    "confidence": confidence_score,
                    "match_reasons": reasons,
                    "last_validated_at": eligible[item_id]["last_validated_at"],
                    "updated_at": eligible[item_id]["updated_at"],
                }
            )
        ranked.sort(
            key=lambda result: (
                -result["score"],
                -result["confidence"],
                -float(result["last_validated_at"] or 0.0),
                -result["updated_at"],
                result["item_id"],
            )
        )

        results: list[dict[str, Any]] = []
        for candidate in ranked[:safe_limit]:
            authorized = self._get_authorized_item(
                candidate["item_id"],
                revision=candidate["revision"],
                repository_id=safe_repository,
                project_id=safe_project,
                auth_sql=auth_sql,
                auth_params=auth_params,
            )
            if authorized is None:
                continue
            authorized["score"] = candidate["score"]
            authorized["match_reasons"] = [
                self._returned_text(reason, "match reason")
                for reason in candidate["match_reasons"]
            ]
            results.append(authorized)
        return results

    def authorized_lesson_revisions(
        self,
        *,
        principal_id: str,
        scope_type: str,
        scope_id: str,
        repository_id: str | None,
        project_id: str | None,
        provider_trust_domain: str,
        provider_is_local: bool = False,
        candidates: Iterable[tuple[str, int]],
        require_injection_allowed: bool = True,
    ) -> set[tuple[str, int]]:
        """Revalidate a bounded cached candidate set without reading text.

        This is the per-request governance check used after ranking. It keeps
        retraction, revision changes, policy revocation, and provider fallback
        effective immediately without repeating FTS or loading lesson bodies.
        """

        safe_principal = self._principal(principal_id)
        safe_scope_type = _require_choice("scope_type", scope_type, _SCOPE_TYPES)
        safe_scope_id = self._identifier(scope_id, "scope_id")
        safe_repository = self._identifier(
            repository_id, "repository_id", nullable=True
        )
        safe_project = self._identifier(project_id, "project_id", nullable=True)
        safe_provider = self._trust_domain(provider_trust_domain)
        if not isinstance(provider_is_local, bool):
            raise TypeError("provider_is_local must be bool")
        if not isinstance(require_injection_allowed, bool):
            raise TypeError("require_injection_allowed must be bool")
        if safe_scope_type == "project" and (not safe_repository or not safe_project):
            raise ValueError("project authorization requires repository_id and project_id")
        if safe_scope_type == "repository" and not safe_repository:
            raise ValueError("repository authorization requires repository_id")
        if safe_scope_type == "project" and safe_scope_id != safe_project:
            raise ValueError("project scope_id must equal project_id")
        if safe_scope_type == "repository" and safe_scope_id != safe_repository:
            raise ValueError("repository scope_id must equal repository_id")

        requested: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        for item_id, revision in candidates:
            safe_item = self._identifier(item_id, "item_id")
            if isinstance(revision, bool) or int(revision) < 1:
                raise ValueError("revision must be a positive integer")
            key = (safe_item, int(revision))
            if key not in seen:
                seen.add(key)
                requested.append(key)
            if len(requested) > 100:
                raise ValueError("authorization candidate set exceeds 100 items")
        if not requested:
            return set()

        auth_sql = self._authorization_sql(
            require_injection_allowed=require_injection_allowed
        )
        item_repository = safe_repository if safe_scope_type != "profile" else None
        item_project = safe_project if safe_scope_type == "project" else None
        auth_params = self._authorization_params(
            principal_id=safe_principal,
            scope_type=safe_scope_type,
            scope_id=safe_scope_id,
            repository_id=item_repository,
            project_id=item_project,
            provider_trust_domain=safe_provider,
            provider_is_local=provider_is_local,
        )
        values_sql = ", ".join("(?, ?)" for _ in requested)
        requested_params = [value for pair in requested for value in pair]
        with self._lock:
            rows = self._connection().execute(
                f"""
                WITH requested(item_id, revision) AS (VALUES {values_sql})
                SELECT i.id, i.current_revision, r.body_json
                FROM requested AS q
                JOIN experience_items AS i
                  ON i.id = q.item_id AND i.current_revision = q.revision
                JOIN experience_item_revisions AS r
                  ON r.item_id = i.id AND r.revision = i.current_revision
                JOIN experience_scope_policies AS p
                  ON p.principal_id = i.principal_id
                 AND p.repository_id = ?
                 AND p.project_id = ?
                WHERE {auth_sql}
                ORDER BY i.id
                """,
                (
                    *requested_params,
                    safe_repository,
                    safe_project,
                    *auth_params,
                ),
            ).fetchall()
        authorized: set[tuple[str, int]] = set()
        for row in rows:
            item_id = str(row["id"])
            revision = int(row["current_revision"])
            authorized.add((item_id, revision))
        return authorized

    def _get_authorized_item(
        self,
        item_id: str,
        *,
        revision: int,
        repository_id: str | None,
        project_id: str | None,
        policy_repository_id: str | None = None,
        policy_project_id: str | None = None,
        auth_sql: str,
        auth_params: tuple[Any, ...],
    ) -> dict[str, Any] | None:
        """Re-check all hard filters in the statement that fetches text."""
        with self._lock:
            conn = self._connection()
            row = conn.execute(
                f"""
                SELECT i.*,
                       r.item_id AS r_item_id,
                       r.revision AS r_revision,
                       r.title AS r_title,
                       r.summary AS r_summary,
                       r.body_json AS r_body_json,
                       r.searchable_text AS r_searchable_text,
                       r.confidence AS r_confidence,
                       r.source_session_id AS r_source_session_id,
                       r.source_turn_id AS r_source_turn_id,
                       r.source_work_id AS r_source_work_id,
                       r.source_hash AS r_source_hash,
                       r.content_hash AS r_content_hash,
                       r.editor AS r_editor,
                       r.edit_reason AS r_edit_reason,
                       r.producer_json AS r_producer_json,
                       r.created_at AS r_created_at,
                       r.last_validated_at AS r_last_validated_at,
                       r.review_after AS r_review_after
                FROM experience_items AS i
                JOIN experience_item_revisions AS r
                  ON r.item_id = i.id AND r.revision = i.current_revision
                JOIN experience_scope_policies AS p
                  ON p.principal_id = i.principal_id
                 AND p.repository_id = ?
                 AND p.project_id = ?
                WHERE i.id = ? AND i.current_revision = ? AND {auth_sql}
                """,
                (
                    policy_repository_id or repository_id,
                    policy_project_id or project_id,
                    item_id,
                    revision,
                    *auth_params,
                ),
            ).fetchone()
            if row is None:
                return None
            tag_rows = conn.execute(
                "SELECT namespace, value FROM experience_tags "
                "WHERE item_id = ? AND revision = ? ORDER BY namespace, value",
                (item_id, revision),
            ).fetchall()
        item = {
            key: row[key]
            for key in (
                "id",
                "family_id",
                "kind",
                "current_status",
                "current_revision",
                "principal_id",
                "scope_type",
                "scope_id",
                "repository_id",
                "project_id",
                "sensitivity",
                "egress_policy",
                "producer_trust_domain",
                "created_by",
                "created_at",
                "updated_at",
                "deleted_at",
            )
        }
        revision_row = {
            key: row[f"r_{key}"]
            for key in (
                "item_id", "revision", "title", "summary", "body_json",
                "searchable_text", "confidence", "source_session_id",
                "source_turn_id", "source_work_id", "source_hash",
                "content_hash", "editor", "edit_reason", "producer_json",
                "created_at", "last_validated_at", "review_after",
            )
        }
        # _revision_dict only relies on mapping access; keep this private
        # flexibility useful for explicit SELECT projections.
        item["revision"] = self._revision_dict(revision_row, tag_rows)  # type: ignore[arg-type]
        return item

    # ------------------------------------------------------------------
    # Retrieval diagnostics
    # ------------------------------------------------------------------

    @staticmethod
    def _retrieval_item_revision(item: Mapping[str, Any]) -> int:
        value: Any = item.get("item_revision")
        if value is None:
            value = item.get("revision")
        if isinstance(value, Mapping):
            value = value.get("revision")
        if value is None:
            value = item.get("current_revision")
        revision = int(value)
        if revision < 1:
            raise ValueError("retrieval item revision must be positive")
        return revision

    def record_retrieval(
        self,
        *,
        turn_id: str,
        work_id: str,
        principal_id: str,
        repository_id: str,
        project_id: str,
        task_signature_hash: str,
        provider_trust_domain: str,
        items: Sequence[Mapping[str, Any]] = (),
        retrieval_id: str | None = None,
        idempotency_key: str | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        """Atomically persist one retrieval and its item-level diagnostics."""
        safe_retrieval_id = self._identifier(
            retrieval_id or _new_id("retrieval"), "retrieval_id"
        )
        safe_turn = self._identifier(turn_id, "turn_id")
        safe_work = self._identifier(work_id, "work_id")
        safe_principal = self._principal(principal_id)
        safe_repository = self._identifier(repository_id, "repository_id")
        safe_project = self._identifier(project_id, "project_id")
        safe_signature = self._digest(
            task_signature_hash, "task_signature_hash"
        )
        safe_provider = self._trust_domain(provider_trust_domain)
        safe_idempotency = self._identifier(
            idempotency_key, "idempotency_key", nullable=True
        )
        timestamp = _now(created_at)

        normalized_items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            item_id = self._identifier(item.get("item_id") or item.get("id"), "item_id")
            if item_id in seen:
                raise ValueError("a retrieval may contain an item only once")
            seen.add(item_id)
            revision = self._retrieval_item_revision(item)
            rank = int(item.get("rank", index + 1))
            if rank < 1:
                raise ValueError("retrieval rank must be positive")
            score = float(item.get("score", 0.0))
            if not math.isfinite(score):
                raise ValueError("retrieval score must be finite")
            raw_reasons = item.get("match_reasons", ())
            if not isinstance(raw_reasons, Sequence) or isinstance(
                raw_reasons, (str, bytes)
            ):
                raise TypeError("match_reasons must be a sequence")
            safe_reasons, reasons_json = self._json_value_for_storage(
                list(raw_reasons), "match reasons", _MAX_METADATA_JSON_BYTES
            )
            if not safe_reasons:
                raise ValueError("match_reasons must not be empty")
            disposition = _require_choice(
                "disposition",
                item.get("disposition", "retrieved"),
                _RETRIEVAL_DISPOSITIONS,
            )
            normalized_items.append(
                {
                    "item_id": item_id,
                    "item_revision": revision,
                    "rank": rank,
                    "score": score,
                    "match_reasons_json": reasons_json,
                    "disposition": disposition,
                }
            )
        normalized_items.sort(key=lambda item: (item["rank"], item["item_id"]))

        def insert(conn: sqlite3.Connection) -> str:
            existing = None
            if safe_idempotency:
                existing = conn.execute(
                    "SELECT * FROM experience_retrievals WHERE idempotency_key = ?",
                    (safe_idempotency,),
                ).fetchone()
            if existing is None:
                existing = conn.execute(
                    "SELECT * FROM experience_retrievals WHERE id = ?",
                    (safe_retrieval_id,),
                ).fetchone()
            if existing is not None:
                expected = (
                    safe_turn,
                    safe_work,
                    safe_principal,
                    safe_repository,
                    safe_project,
                    safe_signature,
                    safe_provider,
                )
                observed = tuple(
                    existing[key]
                    for key in (
                        "turn_id",
                        "work_id",
                        "principal_id",
                        "repository_id",
                        "project_id",
                        "task_signature_hash",
                        "provider_trust_domain",
                    )
                )
                if observed != expected:
                    raise ValueError(
                        "idempotency key already identifies another retrieval"
                    )
                stored_items = conn.execute(
                    "SELECT item_id, item_revision, rank, score, "
                    "match_reasons_json, disposition "
                    "FROM experience_retrieval_items WHERE retrieval_id = ? "
                    "ORDER BY rank, item_id",
                    (existing["id"],),
                ).fetchall()
                observed_items = tuple(
                    (
                        row["item_id"],
                        int(row["item_revision"]),
                        int(row["rank"]),
                        float(row["score"]),
                        row["match_reasons_json"],
                        row["disposition"],
                    )
                    for row in stored_items
                )
                expected_items = tuple(
                    (
                        item["item_id"],
                        item["item_revision"],
                        item["rank"],
                        item["score"],
                        item["match_reasons_json"],
                        item["disposition"],
                    )
                    for item in normalized_items
                )
                if observed_items != expected_items:
                    raise ValueError(
                        "idempotent retrieval replay has different items"
                    )
                return str(existing["id"])

            conn.execute(
                """
                INSERT INTO experience_retrievals(
                    id, turn_id, work_id, principal_id, repository_id,
                    project_id, task_signature_hash, provider_trust_domain,
                    idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    safe_retrieval_id,
                    safe_turn,
                    safe_work,
                    safe_principal,
                    safe_repository,
                    safe_project,
                    safe_signature,
                    safe_provider,
                    safe_idempotency,
                    timestamp,
                ),
            )
            for item in normalized_items:
                conn.execute(
                    """
                    INSERT INTO experience_retrieval_items(
                        retrieval_id, item_id, item_revision, rank, score,
                        match_reasons_json, disposition
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        safe_retrieval_id,
                        item["item_id"],
                        item["item_revision"],
                        item["rank"],
                        item["score"],
                        item["match_reasons_json"],
                        item["disposition"],
                    ),
                )
                event_key = f"retrieved:{safe_retrieval_id}:{item['item_id']}"
                event_id = "event_" + hashlib.sha256(event_key.encode()).hexdigest()[:32]
                payload = self._json_dumps(
                    {
                        "match_reasons": json.loads(item["match_reasons_json"]),
                        "rank": item["rank"],
                        "score": item["score"],
                    },
                    "event payload",
                    _MAX_EVENT_JSON_BYTES,
                )
                conn.execute(
                    """
                    INSERT INTO experience_events(
                        id, event_type, item_id, item_revision, retrieval_id,
                        work_id, payload_json, idempotency_key, created_at
                    ) VALUES (?, 'retrieved', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        item["item_id"],
                        item["item_revision"],
                        safe_retrieval_id,
                        safe_work,
                        payload,
                        event_key,
                        timestamp,
                    ),
                )
            return safe_retrieval_id

        stored_id = self._execute_write(insert)
        result = self.get_retrieval(stored_id)
        assert result is not None
        return result

    def get_retrieval(self, retrieval_id: str) -> dict[str, Any] | None:
        """Return retrieval and per-item diagnostics; never lesson body text."""
        safe_id = self._identifier(retrieval_id, "retrieval_id")
        with self._lock:
            conn = self._connection()
            row = conn.execute(
                "SELECT * FROM experience_retrievals WHERE id = ?", (safe_id,)
            ).fetchone()
            if row is None:
                return None
            items = conn.execute(
                """
                SELECT ri.*, i.kind, i.current_status, r.title
                FROM experience_retrieval_items AS ri
                LEFT JOIN experience_items AS i
                  ON i.id = ri.item_id
                LEFT JOIN experience_item_revisions AS r
                  ON r.item_id = i.id AND r.revision = ri.item_revision
                WHERE ri.retrieval_id = ?
                ORDER BY ri.rank, ri.item_id
                """,
                (safe_id,),
            ).fetchall()
        result = dict(row)
        result["items"] = []
        for item_row in items:
            item = dict(item_row)
            item["match_reasons"] = self._json_from_storage(
                item.pop("match_reasons_json"), "match reasons"
            )
            item["kind"] = item.get("kind")
            item["status"] = item.get("current_status")
            item["title"] = self._returned_text(item.get("title"), "title")
            # Older pre-release schemas may still carry this deferred field.
            item.pop("planned_effect", None)
            result["items"].append(item)
        return result

    def get_latest_retrieval(
        self,
        *,
        principal_id: str,
        repository_id: str,
        project_id: str,
    ) -> dict[str, Any] | None:
        """Return the newest diagnostic in one exact project scope."""

        params = (
            self._principal(principal_id),
            self._identifier(repository_id, "repository_id"),
            self._identifier(project_id, "project_id"),
        )
        with self._lock:
            row = self._connection().execute(
                """
                SELECT id
                FROM experience_retrievals
                WHERE principal_id = ? AND repository_id = ? AND project_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return None if row is None else self.get_retrieval(row["id"])

    def _event_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        event = dict(row)
        event["payload"] = self._json_from_storage(
            event.pop("payload_json"), "event payload"
        )
        return event

    def list_events(
        self,
        *,
        item_id: str | None = None,
        retrieval_id: str | None = None,
        work_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return bounded newest-first audit diagnostics."""
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("item_id", item_id),
            ("retrieval_id", retrieval_id),
            ("work_id", work_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(self._identifier(value, column))
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(_require_choice("event_type", event_type, _EVENT_TYPES))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(0, min(int(limit), 1_000)))
        with self._lock:
            rows = self._connection().execute(
                f"SELECT * FROM experience_events {where} "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._event_dict(row) for row in rows]

    def prune_diagnostics(
        self,
        *,
        now: float | None = None,
        max_age_days: int = 30,
        max_retrievals: int = 10_000,
        max_events: int = 10_000,
    ) -> dict[str, int]:
        """Apply the MVP diagnostic age and count bounds atomically."""
        if max_age_days < 0 or max_retrievals < 0 or max_events < 0:
            raise ValueError("diagnostic retention bounds must be non-negative")
        cutoff = _now(now) - (int(max_age_days) * 86_400.0)

        def prune(conn: sqlite3.Connection) -> dict[str, int]:
            retrievals_removed = conn.execute(
                "DELETE FROM experience_retrievals WHERE created_at < ?", (cutoff,)
            ).rowcount
            events_removed = conn.execute(
                "DELETE FROM experience_events WHERE created_at < ?", (cutoff,)
            ).rowcount
            if max_retrievals == 0:
                retrievals_removed += conn.execute(
                    "DELETE FROM experience_retrievals"
                ).rowcount
            else:
                retrievals_removed += conn.execute(
                    """
                    DELETE FROM experience_retrievals
                    WHERE id NOT IN (
                        SELECT id FROM experience_retrievals
                        ORDER BY created_at DESC, id DESC LIMIT ?
                    )
                    """,
                    (int(max_retrievals),),
                ).rowcount
            if max_events == 0:
                events_removed += conn.execute("DELETE FROM experience_events").rowcount
            else:
                events_removed += conn.execute(
                    """
                    DELETE FROM experience_events
                    WHERE id NOT IN (
                        SELECT id FROM experience_events
                        ORDER BY created_at DESC, id DESC LIMIT ?
                    )
                    """,
                    (int(max_events),),
                ).rowcount
            return {
                "retrievals_removed": retrievals_removed,
                "events_removed": events_removed,
            }

        return self._execute_write(prune)

    def diagnostic_stats(self) -> dict[str, Any]:
        """Return metadata-only counts for governance/status output."""
        with self._lock:
            conn = self._connection()
            retrieval = conn.execute(
                "SELECT COUNT(*) AS count, MIN(created_at) AS oldest "
                "FROM experience_retrievals"
            ).fetchone()
            events = conn.execute(
                "SELECT COUNT(*) AS count, MIN(created_at) AS oldest "
                "FROM experience_events"
            ).fetchone()
            tags = conn.execute(
                "SELECT COUNT(*) AS count FROM experience_tags t JOIN experience_items i ON i.id = t.item_id AND i.current_revision = t.revision"
            ).fetchone()
            links = conn.execute(
                "SELECT COUNT(*) AS count FROM experience_links"
            ).fetchone()
        return {
            "retrieval_count": int(retrieval["count"]),
            "oldest_retrieval_at": retrieval["oldest"],
            "event_count": int(events["count"]),
            "oldest_event_at": events["oldest"],
            "tag_count": int(tags["count"]),
            "link_count": int(links["count"]),
        }

    def schema_status(self) -> dict[str, Any]:
        """Return metadata-only schema, FTS, and governance counts."""

        with self._lock:
            conn = self._connection()
            version = conn.execute(
                "SELECT value FROM experience_schema_meta WHERE key = 'version'"
            ).fetchone()
            fts_version = conn.execute(
                "SELECT value FROM experience_schema_meta "
                "WHERE key = 'fts_rebuild_version'"
            ).fetchone()
            fts_module = conn.execute(
                "SELECT 1 FROM pragma_module_list WHERE name = 'fts5'"
            ).fetchone() is not None
            unicode61 = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'experience_search'"
            ).fetchone() is not None
            trigram = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'experience_search_trigram'"
            ).fetchone() is not None
            revisions = conn.execute(
                "SELECT COUNT(*) AS count FROM experience_item_revisions"
            ).fetchone()
            search_content = conn.execute(
                "SELECT COUNT(*) AS count FROM experience_search_content"
            ).fetchone()
            search_rows = 0
            trigram_rows = 0
            if unicode61:
                search_rows = int(
                    conn.execute(
                        "SELECT COUNT(*) AS count FROM experience_search"
                    ).fetchone()["count"]
                )
            if trigram:
                trigram_rows = int(
                    conn.execute(
                        "SELECT COUNT(*) AS count FROM experience_search_trigram"
                    ).fetchone()["count"]
                )
            counts_rows = conn.execute(
                """
                SELECT kind, current_status, COUNT(*) AS count
                FROM experience_items
                GROUP BY kind, current_status
                ORDER BY kind, current_status
                """
            ).fetchall()
            migration_rows = conn.execute(
                """
                SELECT source_system, source_store_hash, COUNT(*) AS count,
                       MAX(imported_at) AS latest_imported_at
                FROM experience_migration_sources
                GROUP BY source_system, source_store_hash
                ORDER BY latest_imported_at DESC, source_system
                """
            ).fetchall()
            latest_retrieval = conn.execute(
                """
                SELECT id, created_at
                FROM experience_retrievals
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        return {
            "schema_version": version["value"] if version else None,
            "schema_current": (
                version is not None and version["value"] == _CURRENT_SCHEMA_VERSION
            ),
            "fts_rebuild_version": fts_version["value"] if fts_version else None,
            "fts_current": (
                fts_version is not None
                and fts_version["value"] == _CURRENT_FTS_VERSION
            ),
            "fts_module_available": fts_module,
            "unicode61_index": unicode61,
            "trigram_index": trigram,
            "revision_count": int(revisions["count"]),
            "search_content_count": int(search_content["count"]),
            "unicode61_row_count": search_rows,
            "trigram_row_count": trigram_rows,
            "counts_by_kind_status": {
                f"{row['kind']}.{row['current_status']}": int(row["count"])
                for row in counts_rows
            },
            "migration_sources": [
                {
                    "source_system": row["source_system"],
                    "source_store_hash": row["source_store_hash"],
                    "count": int(row["count"]),
                    "latest_imported_at": row["latest_imported_at"],
                }
                for row in migration_rows
            ],
            "latest_retrieval": (
                {
                    "id": latest_retrieval["id"],
                    "created_at": latest_retrieval["created_at"],
                }
                if latest_retrieval
                else None
            ),
        }

    def rebuild_search_index(self) -> dict[str, Any]:
        """Rebuild unicode61 and trigram FTS indexes metadata-only."""

        with self._lock:
            conn = self._connection()
            module_available = conn.execute(
                "SELECT 1 FROM pragma_module_list WHERE name = 'fts5'"
            ).fetchone() is not None
            if not module_available:
                self._disable_fts_triggers()
                logger.warning(
                    "SQLite FTS5 unavailable; experience search indexes disabled"
                )
                return {
                    "rebuilt": False,
                    "fts_enabled": False,
                    "reason": "fts5_unavailable",
                }
            conn.execute(
                "INSERT INTO experience_search(experience_search) VALUES('rebuild')"
            )
            conn.execute(
                "INSERT INTO experience_search_trigram("
                "experience_search_trigram) VALUES('rebuild')"
            )
            conn.execute(
                "INSERT INTO experience_schema_meta(key, value) "
                "VALUES('fts_rebuild_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_CURRENT_FTS_VERSION,),
            )
        self._fts_enabled = True
        return {
            "rebuilt": True,
            "fts_enabled": True,
            "fts_rebuild_version": _CURRENT_FTS_VERSION,
        }

    def diagnostic_prune_plan(
        self,
        *,
        now: float | None = None,
        max_age_days: int = 30,
        max_retrievals: int = 10_000,
        max_events: int = 10_000,
    ) -> dict[str, Any]:
        """Return metadata-only diagnostic pruning counts without deleting."""

        if max_age_days < 0 or max_retrievals < 0 or max_events < 0:
            raise ValueError("diagnostic retention bounds must be non-negative")
        cutoff = _now(now) - (int(max_age_days) * 86_400.0)
        with self._lock:
            conn = self._connection()
            retrievals_removed = conn.execute(
                "SELECT COUNT(*) AS count FROM experience_retrievals "
                "WHERE created_at < ?",
                (cutoff,),
            ).fetchone()["count"]
            events_removed = conn.execute(
                "SELECT COUNT(*) AS count FROM experience_events WHERE created_at < ?",
                (cutoff,),
            ).fetchone()["count"]
            if max_retrievals == 0:
                retrievals_removed += conn.execute(
                    "SELECT COUNT(*) AS count FROM experience_retrievals"
                ).fetchone()["count"]
            else:
                retrievals_removed += conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM experience_retrievals
                    WHERE id NOT IN (
                        SELECT id FROM experience_retrievals
                        ORDER BY created_at DESC, id DESC LIMIT ?
                    )
                    """,
                    (int(max_retrievals),),
                ).fetchone()["count"]
            if max_events == 0:
                events_removed += conn.execute(
                    "SELECT COUNT(*) AS count FROM experience_events"
                ).fetchone()["count"]
            else:
                events_removed += conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM experience_events
                    WHERE id NOT IN (
                        SELECT id FROM experience_events
                        ORDER BY created_at DESC, id DESC LIMIT ?
                    )
                    """,
                    (int(max_events),),
                ).fetchone()["count"]
        return {
            "dry_run": True,
            "retrievals_to_remove": int(retrievals_removed),
            "events_to_remove": int(events_removed),
            "max_age_days": int(max_age_days),
            "max_retrievals": int(max_retrievals),
            "max_events": int(max_events),
        }

    def doctor(
        self,
        *,
        repository_root: str | Path | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Run metadata-only integrity and governance checks."""

        current_time = float(time.time() if now is None else now)
        with self._lock:
            conn = self._connection()
            version = conn.execute(
                "SELECT value FROM experience_schema_meta WHERE key = 'version'"
            ).fetchone()
            foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            orphan_current_revisions = [
                row["id"]
                for row in conn.execute(
                    """
                    SELECT i.id
                    FROM experience_items AS i
                    LEFT JOIN experience_item_revisions AS r
                      ON r.item_id = i.id AND r.revision = i.current_revision
                    WHERE r.item_id IS NULL
                    """
                ).fetchall()
            ]
            revision_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM experience_item_revisions"
                ).fetchone()["count"]
            )
            search_content_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM experience_search_content"
                ).fetchone()["count"]
            )
            unicode61 = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'experience_search'"
            ).fetchone() is not None
            trigram = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'experience_search_trigram'"
            ).fetchone() is not None
            search_count = 0
            trigram_count = 0
            fts_marker = conn.execute(
                "SELECT value FROM experience_schema_meta "
                "WHERE key = 'fts_rebuild_version'"
            ).fetchone()
            if unicode61:
                search_count = int(
                    conn.execute(
                        "SELECT COUNT(*) AS count FROM experience_search"
                    ).fetchone()["count"]
                )
            if trigram:
                trigram_count = int(
                    conn.execute(
                        "SELECT COUNT(*) AS count FROM experience_search_trigram"
                    ).fetchone()["count"]
                )
            authority_violations = [
                row["id"]
                for row in conn.execute(
                    """
                    SELECT i.id
                    FROM experience_items AS i
                    JOIN experience_item_revisions AS r
                      ON r.item_id = i.id AND r.revision = i.current_revision
                    WHERE i.kind = 'decision'
                      AND i.current_status = 'active'
                      AND (
                        json_extract(r.body_json, '$.authority') NOT IN ('user', 'repository_policy')
                        OR json_extract(r.body_json, '$.effective_at') > ?
                        OR (
                          json_extract(r.body_json, '$.expires_at') IS NOT NULL
                          AND json_extract(r.body_json, '$.expires_at') <= ?
                        )
                        OR (
                          r.review_after IS NOT NULL
                          AND r.review_after <= ?
                        )
                      )
                    """,
                    (current_time, current_time, current_time),
                ).fetchall()
            ]
            stale_migration_rows = conn.execute(
                """
                SELECT source_system, source_store_hash, source_item_id,
                       source_revision, target_item_id, disposition, reason_code
                FROM experience_migration_sources
                WHERE target_item_id IS NULL
                ORDER BY imported_at DESC, source_system, source_item_id
                LIMIT 100
                """
            ).fetchall()
        schema_current = version is not None and version["value"] == _CURRENT_SCHEMA_VERSION
        fts_current = (
            fts_marker is not None and fts_marker["value"] == _CURRENT_FTS_VERSION
        )
        active_decisions = self.list_decisions(status="active")
        policy_anchor_violations = self._doctor_policy_anchor_violations(
            active_decisions,
            repository_root=repository_root,
        )
        supersession_cycles = self._doctor_supersession_cycles()
        db_mode = stat.S_IMODE(self.db_path.stat().st_mode) if self.db_path.exists() else None
        parent_mode = (
            stat.S_IMODE(self.db_path.parent.stat().st_mode)
            if self.db_path.parent.exists()
            else None
        )
        return {
            "ok": (
                schema_current
                and not foreign_key_rows
                and not orphan_current_revisions
                and revision_count == search_content_count
                and (not unicode61 or search_count == revision_count)
                and (not trigram or trigram_count == revision_count)
                and not authority_violations
                and not supersession_cycles
                and not policy_anchor_violations
                and db_mode == 0o600
                and parent_mode == 0o700
            ),
            "schema_version": version["value"] if version else None,
            "schema_current": schema_current,
            "fts_current": fts_current,
            "foreign_key_violations": [dict(row) for row in foreign_key_rows],
            "orphan_current_revisions": orphan_current_revisions,
            "fts": {
                "unicode61_index": unicode61,
                "trigram_index": trigram,
                "revision_count": revision_count,
                "search_content_count": search_content_count,
                "unicode61_row_count": search_count,
                "trigram_row_count": trigram_count,
                "consistent": revision_count == search_content_count
                and (not unicode61 or search_count == revision_count)
                and (not trigram or trigram_count == revision_count),
            },
            "supersession_cycles": supersession_cycles,
            "active_decision_authority_violations": authority_violations,
            "file_permissions": {
                "db_mode": oct(db_mode) if db_mode is not None else None,
                "parent_mode": oct(parent_mode) if parent_mode is not None else None,
                "ok": db_mode == 0o600 and parent_mode == 0o700,
            },
            "stale_migration_mappings": [dict(row) for row in stale_migration_rows],
            "policy_anchor_violations": policy_anchor_violations,
        }

    def _doctor_policy_anchor_violations(
        self,
        active_decisions: Sequence[dict[str, Any]],
        *,
        repository_root: str | Path | None,
    ) -> list[dict[str, Any]]:
        violations: list[dict[str, Any]] = []
        for decision in active_decisions:
            body = decision["revision"]["body"]
            if body.get("authority") != "repository_policy":
                continue
            if repository_root is None:
                violations.append({
                    "item_id": decision["id"],
                    "reason": "repository_root_required_for_anchor_check",
                })
                continue
            validation = validate_repository_anchor(
                body["policy_anchor_path"],
                body["policy_anchor_hash"],
                repository_root=repository_root,
            )
            if not validation.valid:
                violations.append({
                    "item_id": decision["id"],
                    "path": validation.path,
                    "reason": validation.reason,
                })
        return violations

    def _doctor_supersession_cycles(self) -> list[dict[str, Any]]:
        """Return bounded supersession cycles without body text."""

        with self._lock:
            rows = self._connection().execute(
                """
                SELECT from_item_id, from_revision, to_item_id, to_revision
                FROM experience_links
                WHERE relation = 'supersedes'
                ORDER BY from_item_id, from_revision, to_item_id, to_revision
                """
            ).fetchall()
        graph: dict[tuple[str, int], set[tuple[str, int]]] = {}
        for row in rows:
            graph.setdefault(
                (row["from_item_id"], int(row["from_revision"])), set()
            ).add((row["to_item_id"], int(row["to_revision"])))
        cycles: list[dict[str, Any]] = []
        seen_cycles: set[tuple[tuple[str, int], ...]] = set()
        for start in graph:
            stack: list[tuple[tuple[str, int], tuple[tuple[str, int], ...]]] = [
                (start, (start,))
            ]
            while stack:
                node, path = stack.pop()
                for nxt in graph.get(node, ()):  # noqa: SIM118
                    if nxt == start:
                        cycle_key = self._canonical_cycle(path)
                        if cycle_key in seen_cycles:
                            continue
                        seen_cycles.add(cycle_key)
                        cycles.append({
                            "cycle": [
                                {"item_id": item_id, "revision": revision}
                                for item_id, revision in path
                            ],
                        })
                    elif nxt not in path and len(path) < 100:
                        stack.append((nxt, (*path, nxt)))
        return cycles

    @staticmethod
    def _canonical_cycle(path: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], ...]:
        rotations = [path[index:] + path[:index] for index in range(len(path))]
        return min(rotations)

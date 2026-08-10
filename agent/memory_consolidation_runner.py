"""Scheduled memory-consolidation orchestration.

The runner deliberately knows nothing about SQLite (or any other storage
engine).  A repository supplies the small protocol below, while extraction
and planning remain pure functions.  This keeps the planner from acquiring
mutation credentials and gives callers a safe observe-only default.

The first implementation is intentionally conservative: the built-in
extractor turns non-empty evidence into candidates and the built-in planner
only creates, reinforces exact matches, or emits ``NOOP``.  More capable
extractors/planners can be injected without changing scheduling or commit
semantics.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)

_OPERATIONS = frozenset(
    {"CREATE", "REINFORCE", "MERGE", "SUPERSEDE", "PROMOTE", "ARCHIVE", "FLAG_CONFLICT", "NOOP"}
)
_ORIGINS = frozenset({"explicit", "observed", "inferred", "imported"})


def _text(value: Any, *, limit: int = 16_384) -> str:
    value = "" if value is None else str(value)
    value = value.replace("\x00", "").strip()
    return value[:limit]


def _canonical_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class EvidenceEvent:
    """Immutable source event consumed by the extractor."""

    id: str
    content: str
    scope_type: str = "profile"
    scope_id: str = ""
    source_type: str = "conversation"
    source_id: str = ""
    created_at: float = 0.0
    origin: str = "observed"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvidenceEvent":
        event_id = _text(value.get("id"))
        content = _text(value.get("content", value.get("text", "")))
        if not event_id:
            raise ValueError("evidence event id is required")
        origin = _text(value.get("origin", value.get("source_type", "observed")), limit=32).lower()
        if origin not in _ORIGINS:
            origin = "observed"
        created = value.get("created_at", 0.0)
        try:
            created = float(created)
        except (TypeError, ValueError):
            created = 0.0
        return cls(
            id=event_id,
            content=content,
            scope_type=_text(value.get("scope_type", "profile"), limit=64) or "profile",
            scope_id=_text(value.get("scope_id", ""), limit=512),
            source_type=_text(value.get("source_type", "conversation"), limit=64),
            source_id=_text(value.get("source_id", ""), limit=512),
            created_at=created,
            origin=origin,
            metadata=value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {},
        )


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    id: str
    scope_type: str
    scope_id: str
    content: str
    origin: str
    confidence: float
    evidence_ids: tuple[str, ...]
    extractor_version: str = "v1"


@dataclass(frozen=True, slots=True)
class ConsolidationOperation:
    operation: str
    candidate_id: str
    target_memory_ids: tuple[str, ...] = ()
    result: Mapping[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""

    def __post_init__(self) -> None:
        operation = str(self.operation).upper()
        if operation not in _OPERATIONS:
            raise ValueError(f"unsupported consolidation operation: {self.operation!r}")
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "candidate_id", _text(self.candidate_id, limit=256))
        object.__setattr__(self, "target_memory_ids", tuple(_text(x, limit=256) for x in self.target_memory_ids if _text(x, limit=256)))
        object.__setattr__(self, "confidence", min(1.0, max(0.0, float(self.confidence))))
        object.__setattr__(self, "reason", _text(self.reason, limit=1_000))

    def as_mapping(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "candidate_id": self.candidate_id,
            "target_memory_ids": list(self.target_memory_ids),
            "result": dict(self.result),
            "confidence": self.confidence,
            "reason": self.reason,
        }


class ConsolidationRepository(Protocol):
    """Storage adapter required by :class:`ConsolidationRunner`.

    Implementations may persist runs/operations in one transaction.  The
    runner never calls SQL or mutates a memory object directly.
    """

    def create_consolidation_run(self, previous_watermark: float, cutoff_watermark: float, *, dry_run: bool) -> Any: ...
    def load_evidence(self, previous_watermark: float, cutoff_watermark: float) -> Iterable[EvidenceEvent | Mapping[str, Any]]: ...
    def find_relevant_memories(self, candidate: MemoryCandidate) -> Sequence[Mapping[str, Any]]: ...
    def commit_consolidation(self, run: Any, operations: Sequence[ConsolidationOperation], cutoff_watermark: float) -> None: ...
    def mark_consolidation_failed(self, run: Any, reason: str) -> None: ...
    def mark_consolidation_succeeded(self, run: Any, *, dry_run: bool) -> None: ...


class CandidateExtractor:
    """Pure, deterministic baseline extractor.

    A custom ``extract_fn`` can call an auxiliary model, but its output is
    normalized and every candidate must retain at least one evidence id.
    """

    def __init__(self, extract_fn: Callable[[Sequence[EvidenceEvent]], Iterable[MemoryCandidate | Mapping[str, Any]]] | None = None, *, version: str = "v1") -> None:
        self.extract_fn = extract_fn
        self.version = _text(version, limit=64) or "v1"

    def extract(self, events: Iterable[EvidenceEvent | Mapping[str, Any]]) -> list[MemoryCandidate]:
        normalized = [event if isinstance(event, EvidenceEvent) else EvidenceEvent.from_mapping(event) for event in events]
        if self.extract_fn is not None:
            raw = self.extract_fn(tuple(normalized))
            return [self._normalize_candidate(item) for item in raw if self._candidate_content(item)]
        candidates: list[MemoryCandidate] = []
        for event in normalized:
            if not event.content:
                continue
            candidates.append(MemoryCandidate(
                id=_canonical_id("candidate", self.version, event.id, event.content),
                scope_type=event.scope_type,
                scope_id=event.scope_id,
                content=event.content,
                origin=event.origin,
                confidence=1.0 if event.origin == "explicit" else 0.75,
                evidence_ids=(event.id,),
                extractor_version=self.version,
            ))
        return candidates

    @staticmethod
    def _candidate_content(item: MemoryCandidate | Mapping[str, Any]) -> str:
        return item.content if isinstance(item, MemoryCandidate) else _text(item.get("content", item.get("text", "")))

    def _normalize_candidate(self, item: MemoryCandidate | Mapping[str, Any]) -> MemoryCandidate:
        if isinstance(item, MemoryCandidate):
            candidate = item
        else:
            evidence_ids = tuple(_text(value, limit=256) for value in item.get("evidence_ids", ()) if _text(value, limit=256))
            content = _text(item.get("content", item.get("text", "")))
            candidate = MemoryCandidate(
                id=_text(item.get("id"), limit=256) or _canonical_id("candidate", self.version, evidence_ids, content),
                scope_type=_text(item.get("scope_type", "profile"), limit=64) or "profile",
                scope_id=_text(item.get("scope_id", ""), limit=512), content=content,
                origin=_text(item.get("origin", "observed"), limit=32).lower(),
                confidence=float(item.get("confidence", 0.0)), evidence_ids=evidence_ids,
                extractor_version=_text(item.get("extractor_version", self.version), limit=64) or self.version,
            )
        if not candidate.evidence_ids:
            raise ValueError("memory candidate must reference evidence")
        origin = candidate.origin if candidate.origin in _ORIGINS else "observed"
        return MemoryCandidate(candidate.id, candidate.scope_type, candidate.scope_id, _text(candidate.content), origin, min(1.0, max(0.0, float(candidate.confidence))), tuple(candidate.evidence_ids), candidate.extractor_version)


class ConsolidationPlanner:
    """Conservative planner with an injectable structured-output callback."""

    def __init__(self, plan_fn: Callable[[MemoryCandidate, Sequence[Mapping[str, Any]]], ConsolidationOperation | Mapping[str, Any]] | None = None) -> None:
        self.plan_fn = plan_fn

    def plan(self, candidate: MemoryCandidate, matches: Sequence[Mapping[str, Any]]) -> ConsolidationOperation:
        if self.plan_fn is not None:
            raw = self.plan_fn(candidate, matches)
            if isinstance(raw, ConsolidationOperation):
                return raw
            return ConsolidationOperation(
                operation=raw.get("operation", raw.get("type", "NOOP")),
                candidate_id=raw.get("candidate_id", candidate.id),
                target_memory_ids=tuple(raw.get("target_memory_ids", ())),
                result=raw.get("result", {}), confidence=raw.get("confidence", 0.0), reason=raw.get("reason", ""),
            )
        exact = next((item for item in matches if _text(item.get("content", item.get("summary", ""))).casefold() == candidate.content.casefold() and _text(item.get("id"), limit=256)), None)
        if exact is not None:
            return ConsolidationOperation("REINFORCE", candidate.id, (_text(exact.get("id"), limit=256),), confidence=candidate.confidence, reason="exact evidence reinforces existing memory")
        if matches:
            return ConsolidationOperation("NOOP", candidate.id, confidence=candidate.confidence, reason="ambiguous match; conservative planner declined mutation")
        return ConsolidationOperation("CREATE", candidate.id, result={"content": candidate.content, "evidence_ids": list(candidate.evidence_ids)}, confidence=candidate.confidence, reason="new evidence has no matching memory")


@dataclass(frozen=True, slots=True)
class ConsolidationRunResult:
    status: str
    run_id: str | None
    previous_watermark: float
    cutoff_watermark: float
    candidates: tuple[MemoryCandidate, ...] = ()
    operations: tuple[ConsolidationOperation, ...] = ()
    error: str | None = None


class ConsolidationRunner:
    """Execute one bounded, idempotent consolidation window."""

    def __init__(self, repository: ConsolidationRepository, *, extractor: CandidateExtractor | None = None, planner: ConsolidationPlanner | None = None, enabled: bool = False, dry_run: bool = True, phase: str = "observe") -> None:
        self.repository = repository
        self.extractor = extractor or CandidateExtractor()
        self.planner = planner or ConsolidationPlanner()
        self.enabled = bool(enabled)
        self.dry_run = bool(dry_run)
        self.phase = _text(phase, limit=32).lower() or "observe"

    def run(self, *, previous_watermark: float = 0.0, cutoff_watermark: float | None = None) -> ConsolidationRunResult:
        previous = float(previous_watermark)
        if cutoff_watermark is None:
            watermark_fn = getattr(self.repository, "current_watermark", None)
            cutoff = float(watermark_fn() if callable(watermark_fn) else datetime.now(timezone.utc).timestamp())
        else:
            cutoff = float(cutoff_watermark)
        if cutoff < previous:
            raise ValueError("cutoff_watermark must not precede previous_watermark")
        if not self.enabled:
            return ConsolidationRunResult("disabled", None, previous, cutoff)
        dry_run = self.dry_run or self.phase in {"observe", "dry_run"}
        run = self.repository.create_consolidation_run(previous, cutoff, dry_run=dry_run)
        run_id = _text(run.get("id"), limit=256) if isinstance(run, Mapping) else _text(run, limit=256)
        try:
            events = self.repository.load_evidence(previous, cutoff)
            candidates = self.extractor.extract(events)
            planned: list[ConsolidationOperation] = []
            for candidate in candidates:
                operation = self.planner.plan(candidate, self.repository.find_relevant_memories(candidate))
                # Candidate identity is assigned by the extractor; planner
                # output cannot redirect an operation to another candidate.
                if operation.candidate_id != candidate.id:
                    operation = replace(operation, candidate_id=candidate.id)
                # Carry provenance through the planner boundary so repository
                # adapters never have to trust model-generated evidence IDs.
                if operation.operation != "NOOP":
                    result = dict(operation.result)
                    result.setdefault("content", candidate.content)
                    result["evidence_ids"] = list(candidate.evidence_ids)
                    result.setdefault("origin", candidate.origin)
                    result.setdefault("confidence", candidate.confidence)
                    if operation.operation == "PROMOTE":
                        result["retention_class"] = "protected"
                    operation = replace(operation, result=result)
                planned.append(operation)
            operations = tuple(planned)
            if not dry_run:
                self.repository.commit_consolidation(run, operations, cutoff)
            elif hasattr(self.repository, "record_consolidation_plan"):
                self.repository.record_consolidation_plan(run, operations, cutoff)
            self.repository.mark_consolidation_succeeded(run, dry_run=dry_run)
            return ConsolidationRunResult("dry_run" if dry_run else "committed", run_id, previous, cutoff, tuple(candidates), operations)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"[:1_000]
            try:
                self.repository.mark_consolidation_failed(run, reason)
            except Exception:
                logger.exception("failed to mark consolidation run failed")
            return ConsolidationRunResult("failed", run_id, previous, cutoff, error=reason)


class MemoryConsolidationStoreRepository:
    """Adapter for :class:`agent.memory_consolidation.MemoryConsolidationStore`.

    The store's per-scope ingestion sequence is used as the consolidation
    watermark.  This is intentionally an adapter rather than a dependency in
    the runner so alternate providers can implement the protocol directly.
    Unsupported or ambiguous planner operations are treated as ``NOOP`` and
    never reach persistence.
    """

    def __init__(self, store: Any, *, scope_id: str, scope_type: str = "profile") -> None:
        if not hasattr(store, "append_evidence") or not hasattr(store, "commit"):
            raise TypeError("store does not implement MemoryConsolidationStore")
        self.store = store
        self.scope_id = _text(scope_id, limit=256)
        self.scope_type = _text(scope_type, limit=64) or "profile"
        if not self.scope_id:
            raise ValueError("scope_id is required")

    def create_consolidation_run(self, previous_watermark: float, cutoff_watermark: float, *, dry_run: bool) -> dict[str, Any]:
        return {"id": _canonical_id("run", self.scope_id, int(previous_watermark), int(cutoff_watermark), dry_run)}

    def current_watermark(self) -> int:
        """Return the latest per-scope ingestion sequence for this store."""
        events = self.store.evidence_after_cursor(self.scope_id, self.scope_type)
        return int(events[-1]["ingestion_seq"]) if events else int(self.store.cursor(self.scope_id, self.scope_type))

    def load_evidence(self, previous_watermark: float, cutoff_watermark: float) -> list[dict[str, Any]]:
        start = int(previous_watermark)
        end = int(cutoff_watermark)
        events = self.store.evidence_after_cursor(self.scope_id, self.scope_type)
        return [
            {
                "id": event["event_id"],
                "content": event["content"],
                "scope_type": "profile",
                "scope_id": self.scope_id,
                "source_type": "conversation",
                "source_id": event["source_key"],
                "created_at": event.get("created_at", 0),
                "origin": "observed",
                "metadata": event.get("metadata_json", {}),
                "ingestion_seq": event["ingestion_seq"],
            }
            for event in events
            if start < int(event["ingestion_seq"]) <= end
        ]

    def find_relevant_memories(self, candidate: MemoryCandidate) -> list[Mapping[str, Any]]:
        finder = getattr(self.store, "find_relevant_memories", None)
        if finder is None:
            return []
        return list(finder(scope_id=self.scope_id, scope_type=self.scope_type, claim=candidate.content))

    @staticmethod
    def _store_operation(operation: ConsolidationOperation, *, scope_id: str) -> dict[str, Any] | None:
        if operation.operation == "NOOP":
            return None
        kind_map = {
            "CREATE": "create", "REINFORCE": "revise", "MERGE": "revise",
            "SUPERSEDE": "supersede", "PROMOTE": "revise",
            "ARCHIVE": "archive", "FLAG_CONFLICT": "conflict",
        }
        op_type = kind_map.get(operation.operation)
        if op_type is None:
            return None
        result = dict(operation.result)
        claim = _text(result.get("claim", result.get("content", "")))
        evidence_ids = tuple(_text(value, limit=256) for value in result.get("evidence_ids", ()) if _text(value, limit=256))
        if not claim or not evidence_ids:
            # Candidate evidence is not carried in operation fields by design;
            # malformed planner output must never be persisted.
            return None
        candidate = {
            "kind": _text(result.get("kind", "fact"), limit=32) or "fact",
            "claim": claim,
            "evidence_event_ids": list(evidence_ids),
            "origin": _text(result.get("origin", "observed"), limit=32) or "observed",
            "confidence": result.get("confidence", operation.confidence),
            "retention_class": _text(result.get("retention_class", "standard"), limit=32) or "standard",
            "pinned": bool(result.get("pinned", False)),
        }
        mapped: dict[str, Any] = {"type": op_type, "candidate": candidate}
        if operation.operation in {"MERGE", "SUPERSEDE"} and operation.target_memory_ids:
            mapped["supersedes_item_ids"] = list(operation.target_memory_ids)
        elif operation.target_memory_ids:
            mapped["target_item_id"] = operation.target_memory_ids[0]
        if operation.operation == "FLAG_CONFLICT" and len(operation.target_memory_ids) > 1:
            mapped["conflicts_with"] = operation.target_memory_ids[1]
        return mapped

    def commit_consolidation(self, run: Any, operations: Sequence[ConsolidationOperation], cutoff_watermark: float) -> None:
        mapped = [item for operation in operations if (item := self._store_operation(operation, scope_id=self.scope_id)) is not None]
        self.store.commit(scope_id=self.scope_id, scope_type=self.scope_type, operations=mapped, end_seq=int(cutoff_watermark), run_id=_text(run.get("id"), limit=256))

    def record_consolidation_plan(self, run: Any, operations: Sequence[ConsolidationOperation], cutoff_watermark: float) -> None:
        mapped = [item for operation in operations if (item := self._store_operation(operation, scope_id=self.scope_id)) is not None]
        self.store.record_plan(
            scope_id=self.scope_id,
            scope_type=self.scope_type,
            run_id=_text(run.get("id"), limit=256),
            start_seq=int(self.store.cursor(self.scope_id, self.scope_type)),
            end_seq=int(cutoff_watermark),
            operations=mapped,
        )

    def mark_consolidation_failed(self, run: Any, reason: str) -> None:
        # MemoryConsolidationStore rolls back failed commits; no failed-run
        # mutation is necessary for this adapter.
        logger.warning("memory consolidation failed run=%s reason=%s", _text(run.get("id"), limit=256), _text(reason, limit=1_000))

    def mark_consolidation_succeeded(self, run: Any, *, dry_run: bool) -> None:
        logger.info("memory consolidation succeeded run=%s dry_run=%s", _text(run.get("id"), limit=256), dry_run)


@dataclass(frozen=True, slots=True)
class ConsolidationSchedule:
    enabled: bool = False
    schedule: str = "0 23 * * *"
    dry_run: bool = True
    phase: str = "observe"

    def is_due(self, *, now: datetime | None = None, last_run: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        now = now or datetime.now(timezone.utc)
        if last_run is None:
            return True
        try:
            from croniter import croniter
            return croniter(self.schedule, last_run).get_next(datetime) <= now
        except Exception:
            # Conservative fallback when croniter is unavailable.
            return now.date() > last_run.date()

    def build_runner(self, repository: ConsolidationRepository, *, extractor: CandidateExtractor | None = None, planner: ConsolidationPlanner | None = None) -> ConsolidationRunner:
        return ConsolidationRunner(repository, extractor=extractor, planner=planner, enabled=self.enabled, dry_run=self.dry_run, phase=self.phase)


def run_configured_consolidation(*, config: Mapping[str, Any] | None = None,
                                  scope_id: str = "default", scope_type: str = "profile",
                                  state_db_path: str | None = None) -> ConsolidationRunResult:
    """Run the configured daily pass for a trusted profile scope.

    The scheduler owns invocation; this helper owns no identity resolution and
    never derives a scope from evidence.  It is safe to call on every scheduler
    tick because the store cursor and run idempotency provide the checkpoint.
    """
    cfg = dict(config or {})
    settings = cfg.get("memory", {}).get("consolidation", {}) if isinstance(cfg.get("memory", {}), Mapping) else {}
    schedule = ConsolidationSchedule(
        enabled=bool(settings.get("enabled", False)),
        schedule=_text(settings.get("schedule", "0 23 * * *"), limit=64),
        dry_run=bool(settings.get("dry_run", True)),
        phase=_text(settings.get("phase", "observe"), limit=32),
    )
    if not schedule.enabled:
        return ConsolidationRunResult("disabled", None, 0.0, datetime.now(timezone.utc).timestamp())
    if state_db_path is None:
        from marlow_constants import get_marlow_home
        state_db_path = str((get_marlow_home() / "memory_consolidation.db").resolve())
    from agent.memory_consolidation import MemoryConsolidationStore
    with MemoryConsolidationStore(state_db_path) as store:
        last_run = store.last_run_at(scope_id, scope_type)
        last_dt = datetime.fromtimestamp(last_run, tz=timezone.utc) if last_run is not None else None
        if not schedule.is_due(last_run=last_dt):
            cursor = float(store.cursor(scope_id, scope_type))
            return ConsolidationRunResult("not_due", None, cursor, cursor)
        repository = MemoryConsolidationStoreRepository(store, scope_id=scope_id, scope_type=scope_type)
        return schedule.build_runner(repository).run(previous_watermark=float(store.cursor(scope_id, scope_type)))


def append_conversation_evidence(*, scope_id: str, scope_type: str = "profile",
                                 session_id: str, user_content: Any,
                                 assistant_content: Any,
                                 turn_id: str | int | None = None,
                                 state_db_path: str | None = None) -> dict[str, Any]:
    """Append one completed turn with a deterministic delivery key."""
    import hashlib

    user = _text(user_content)
    assistant = _text(assistant_content)
    if not user or not assistant:
        raise ValueError("completed turn content is required")
    if turn_id is not None and _text(turn_id, limit=256):
        source_key = f"turn:{_text(session_id, limit=128)}:{_text(turn_id, limit=128)}"
    else:
        source_key = "turn:" + hashlib.sha256(
            json.dumps([_text(session_id, limit=256), user, assistant], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    content = f"User: {user}\nAssistant: {assistant}"
    if state_db_path is None:
        from marlow_constants import get_marlow_home
        state_db_path = str((get_marlow_home() / "memory_consolidation.db").resolve())
    from agent.memory_consolidation import MemoryConsolidationStore
    with MemoryConsolidationStore(state_db_path) as store:
        return store.append_evidence(
            scope_id=scope_id,
            scope_type=scope_type,
            source_key=source_key,
            content=content,
            metadata={"source_type": "conversation", "session_id": _text(session_id, limit=256)},
        )

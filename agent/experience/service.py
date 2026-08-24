"""Typed orchestration for the Work Experience validation MVP.

This module has no agent-loop integration and performs no capture or model
reflection.  It turns one already-scoped, already-separated user request into
an authorized retrieval, records text-free diagnostics, and formats an
advisory block that a later runtime integration may attach to a wire-only copy
of the current user message.
"""

from __future__ import annotations

import hashlib
import html
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from agent.experience.anchors import validate_repository_anchor
from agent.experience.authority import (
    DecisionTurnAuthority,
    decision_authority_from_text,
)
from agent.experience.models import (
    LOCAL_OWNER_PRINCIPAL,
    DecisionBody,
    LessonBody,
    LessonTag,
    RetrievalDiagnostic,
    RetrievalDisposition,
    RetrievalItemDiagnostic,
    DecisionMatch,
    RetrievalMatch,
    RetrievalQuery,
    ScopePolicy,
    TagNamespace,
)
from agent.experience.safety import is_egress_allowed, sanitize_for_return
from agent.experience.scope import ResolvedScope, ScopeResolver
from agent.experience.store import ExperienceStore


_LOCAL_TRUST_DOMAIN = "local-runtime"


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Authorized lesson text plus the text-free diagnostic that names it."""

    diagnostic: RetrievalDiagnostic
    query: RetrievalQuery
    items: tuple[RetrievalMatch, ...]
    item_diagnostics: tuple[RetrievalItemDiagnostic, ...]
    fts_enabled: bool
    disclosures: tuple["RetrievalDisclosure", ...] = ()


@dataclass(frozen=True, slots=True)
class CombinedRetrievalResult:
    """Authorized Decision and Lesson retrieval for one turn."""

    diagnostic: RetrievalDiagnostic
    query: RetrievalQuery
    decisions: tuple[DecisionMatch, ...]
    lessons: tuple[RetrievalMatch, ...]
    item_diagnostics: tuple[RetrievalItemDiagnostic, ...]
    fts_enabled: bool
    disclosures: tuple["RetrievalDisclosure", ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalDisclosure:
    """Immutable item policy retained for per-request fallback checks."""

    item_id: str
    item_revision: int
    sensitivity: str
    egress_policy: str
    producer_trust_domain: str | None


class ExperienceService:
    """Retrieve and format manually approved work-experience lessons.

    Callers remain responsible for supplying only the explicit raw request
    text, not rendered attachments, diffs, fetched pages, or skill content.
    The service stores only its deterministic hash.
    """

    def __init__(
        self,
        store: ExperienceStore,
        *,
        scope_resolver: ScopeResolver | None = None,
        max_retrieved_items: int = 3,
        max_context_chars: int = 1_500,
        min_confidence: float = 0.0,
    ) -> None:
        if not isinstance(store, ExperienceStore):
            raise TypeError("store must be an ExperienceStore")
        if not 1 <= int(max_retrieved_items) <= 50:
            raise ValueError("max_retrieved_items must be between 1 and 50")
        if not 256 <= int(max_context_chars) <= 16_384:
            raise ValueError("max_context_chars must be between 256 and 16384")
        if not 0.0 <= float(min_confidence) <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        self.store = store
        self.scope_resolver = scope_resolver
        self.max_retrieved_items = int(max_retrieved_items)
        self.max_context_chars = int(max_context_chars)
        self.min_confidence = float(min_confidence)

    @contextmanager
    def _available_store(self) -> Iterator[ExperienceStore]:
        """Yield the owned facade or reopen its explicit profile DB briefly.

        Runtime integration may cache a frozen retrieval after its setup
        transaction has closed. Governance checks and declarations must still
        consult current state, so those operations reopen the same explicit
        database rather than trusting cached authorization.
        """

        if not self.store.closed:
            yield self.store
            return
        with ExperienceStore(
            self.store.db_path,
            initialize_schema=False,
        ) as reopened:
            yield reopened

    def resolve_scope(self, cwd: str) -> ResolvedScope:
        """Resolve the most-specific stored policy for a logical runtime cwd."""

        if self.scope_resolver is None:
            raise RuntimeError("scope_resolver is required to resolve a cwd")
        policies = self.store.list_scope_policies(
            principal_id=LOCAL_OWNER_PRINCIPAL
        )
        return self.scope_resolver.resolve(cwd, policies)

    def default_decision_scope(self, scope: ResolvedScope) -> dict[str, str | None]:
        """Return the narrowest default persistence scope for a resolved cwd."""

        if not isinstance(scope, ResolvedScope):
            raise TypeError("scope must be a ResolvedScope")
        ref = scope.as_ref()
        return {
            "scope_type": ref.scope_type.value,
            "scope_id": ref.scope_id,
            "repository_id": ref.repository_id,
            "project_id": ref.project_id,
        }

    @staticmethod
    def decision_authority_from_text(
        source_turn_id: str,
        source_session_id: str,
        raw_user_text: str,
        **kwargs: object,
    ) -> DecisionTurnAuthority:
        """Build trusted Decision authority from authenticated current-turn text."""

        return decision_authority_from_text(
            source_turn_id,
            source_session_id,
            raw_user_text,
            **kwargs,
        )

    @staticmethod
    def task_signature_hash(query: RetrievalQuery) -> str:
        """Return stable metadata for diagnostics without persisting raw text."""

        payload = {
            "query": query.query_text,
            "scope": {
                "principal": query.scope.principal_id,
                "type": query.scope.scope_type.value,
                "id": query.scope.scope_id,
                "repository": query.scope.repository_id,
                "project": query.scope.project_id,
            },
            "task_types": query.task_types,
            "technologies": query.technologies,
            "entities": query.entities,
            "failure_fingerprints": query.failure_fingerprints,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _retrieval_id(
        *,
        turn_id: str,
        work_id: str,
        signature_hash: str,
        query: RetrievalQuery,
    ) -> str:
        material = "\0".join(
            (
                turn_id,
                work_id,
                signature_hash,
                query.scope.principal_id,
                query.scope.scope_id,
                query.provider_trust_domain or _LOCAL_TRUST_DOMAIN,
                "local" if query.provider_is_local else "remote",
            )
        )
        return "retrieval_" + hashlib.sha256(material.encode()).hexdigest()[:32]

    @staticmethod
    def _query_tags(query: RetrievalQuery) -> dict[str, tuple[str, ...]]:
        return {
            TagNamespace.TASK_TYPE.value: query.task_types,
            TagNamespace.TECHNOLOGY.value: query.technologies,
            TagNamespace.ENTITY.value: query.entities,
            TagNamespace.FAILURE.value: query.failure_fingerprints,
        }

    @staticmethod
    def _lesson_match_from_mapping(
        value: Mapping[str, Any],
        *,
        rank: int,
    ) -> RetrievalMatch:
        revision = value["revision"]
        tags = tuple(
            LessonTag(TagNamespace(tag["namespace"]), tag["value"])
            for tag in revision.get("tags", ())
        )
        return RetrievalMatch(
            item_id=value["id"],
            item_revision=int(revision["revision"]),
            title=revision["title"],
            summary=revision["summary"],
            body=LessonBody.from_mapping(revision["body"]),
            rank=rank,
            score=float(value["score"]),
            match_reasons=tuple(value["match_reasons"]),
            confidence=revision.get("confidence"),
            tags=tags,
        )

    @staticmethod
    def _decision_match_from_mapping(
        value: Mapping[str, Any],
        *,
        rank: int,
    ) -> DecisionMatch:
        revision = value["revision"]
        body = revision["body"]
        tags = tuple(
            LessonTag(TagNamespace(tag["namespace"]), tag["value"])
            for tag in revision.get("tags", ())
        )
        return DecisionMatch(
            item_id=value["id"],
            family_id=value["family_id"],
            item_revision=int(revision["revision"]),
            title=revision["title"],
            summary=revision["summary"],
            body=DecisionBody.from_mapping(body),
            authority=body["authority"],
            scope_type=value["scope_type"],
            scope_id=value["scope_id"],
            repository_id=value.get("repository_id"),
            project_id=value.get("project_id"),
            source_session_id=revision.get("source_session_id"),
            source_turn_id=revision.get("source_turn_id"),
            source_work_id=revision.get("source_work_id"),
            rank=rank,
            score=float(value["score"]),
            match_reasons=tuple(value["match_reasons"]),
            confidence=revision.get("confidence"),
            tags=tags,
            updated_at=float(value["updated_at"]),
        )

    def retrieve(
        self,
        query: RetrievalQuery,
        *,
        turn_id: str,
        work_id: str,
        retrieval_id: str | None = None,
        idempotency_key: str | None = None,
        require_injection_allowed: bool = True,
    ) -> RetrievalResult:
        """Run one authorized search and atomically record its diagnostics."""

        if not isinstance(query, RetrievalQuery):
            raise TypeError("query must be a RetrievalQuery")
        if not isinstance(require_injection_allowed, bool):
            raise TypeError("require_injection_allowed must be bool")
        signature_hash = self.task_signature_hash(query)
        provider_domain = query.provider_trust_domain or _LOCAL_TRUST_DOMAIN
        limit = min(query.limit, self.max_retrieved_items)
        rows = self.store.search_lessons(
            principal_id=query.scope.principal_id,
            scope_type=query.scope.scope_type,
            scope_id=query.scope.scope_id,
            repository_id=query.scope.repository_id,
            project_id=query.scope.project_id,
            provider_trust_domain=provider_domain,
            provider_is_local=query.provider_is_local,
            query=query.query_text,
            tags=self._query_tags(query),
            min_confidence=self.min_confidence,
            require_injection_allowed=require_injection_allowed,
            limit=limit,
        )

        policy = None
        if query.scope.repository_id and query.scope.project_id:
            raw_policy = self.store.get_scope_policy(
                principal_id=query.scope.principal_id,
                repository_id=query.scope.repository_id,
                project_id=query.scope.project_id,
            )
            if raw_policy is not None:
                policy = ScopePolicy.from_mapping(raw_policy)

        matches: list[RetrievalMatch] = []
        disclosures: list[RetrievalDisclosure] = []
        for row in rows:
            # Defense in depth: SQL performs this check before selecting any
            # revision text.  Re-check here so a future store cannot weaken
            # the typed service boundary accidentally.
            if policy is None or not is_egress_allowed(
                sensitivity=row["sensitivity"],
                egress_policy=row["egress_policy"],
                producer_trust_domain=row.get("producer_trust_domain"),
                current_trust_domain=provider_domain,
                current_provider_is_local=query.provider_is_local,
                max_egress_policy=policy.max_egress_policy,
            ):
                continue
            match = self._lesson_match_from_mapping(row, rank=len(matches) + 1)
            matches.append(match)
            disclosures.append(
                RetrievalDisclosure(
                    item_id=match.item_id,
                    item_revision=match.item_revision,
                    sensitivity=str(row["sensitivity"]),
                    egress_policy=str(row["egress_policy"]),
                    producer_trust_domain=row.get("producer_trust_domain"),
                )
            )

        resolved_retrieval_id = retrieval_id or self._retrieval_id(
            turn_id=turn_id,
            work_id=work_id,
            signature_hash=signature_hash,
            query=query,
        )
        stored = self.store.record_retrieval(
            retrieval_id=resolved_retrieval_id,
            idempotency_key=idempotency_key or resolved_retrieval_id,
            turn_id=turn_id,
            work_id=work_id,
            principal_id=query.scope.principal_id,
            repository_id=query.scope.repository_id or "profile",
            project_id=query.scope.project_id or query.scope.scope_id,
            task_signature_hash=signature_hash,
            provider_trust_domain=provider_domain,
            items=[
                {
                    "item_id": match.item_id,
                    "item_revision": match.item_revision,
                    "rank": match.rank,
                    "score": match.score,
                    "match_reasons": match.match_reasons,
                }
                for match in matches
            ],
        )
        diagnostic = RetrievalDiagnostic(
            id=stored["id"],
            turn_id=stored["turn_id"],
            work_id=stored["work_id"],
            principal_id=stored["principal_id"],
            repository_id=stored["repository_id"],
            project_id=stored["project_id"],
            task_signature_hash=stored["task_signature_hash"],
            provider_trust_domain=stored["provider_trust_domain"],
            created_at=stored["created_at"],
        )
        item_diagnostics = tuple(
            RetrievalItemDiagnostic(
                retrieval_id=item["retrieval_id"],
                item_id=item["item_id"],
                item_revision=item["item_revision"],
                rank=item["rank"],
                score=item["score"],
                match_reasons=tuple(item["match_reasons"]),
                disposition=RetrievalDisposition(item["disposition"]),
            )
            for item in stored["items"]
        )
        return RetrievalResult(
            diagnostic=diagnostic,
            query=query,
            items=tuple(matches),
            item_diagnostics=item_diagnostics,
            fts_enabled=self.store.fts_enabled,
            disclosures=tuple(disclosures),
        )

    def retrieve_decisions_and_lessons(
        self,
        query: RetrievalQuery,
        *,
        turn_id: str,
        work_id: str,
        retrieval_id: str | None = None,
        idempotency_key: str | None = None,
        require_injection_allowed: bool = True,
        max_decisions: int = 2,
        max_lessons: int = 1,
        repository_root: str | None = None,
    ) -> CombinedRetrievalResult:
        """Retrieve active Decisions and Lessons in separate ranked buckets."""
        if not isinstance(query, RetrievalQuery):
            raise TypeError("query must be a RetrievalQuery")
        if not isinstance(require_injection_allowed, bool):
            raise TypeError("require_injection_allowed must be bool")
        if not 0 <= int(max_decisions) <= 50:
            raise ValueError("max_decisions must be between 0 and 50")
        if not 0 <= int(max_lessons) <= 50:
            raise ValueError("max_lessons must be between 0 and 50")
        signature_hash = self.task_signature_hash(query)
        provider_domain = query.provider_trust_domain or _LOCAL_TRUST_DOMAIN
        limit = min(query.limit, self.max_retrieved_items)
        decision_scope_specs: list[tuple[str, str, str | None, str | None]] = []
        if query.scope.scope_type == "project":
            decision_scope_specs.extend((
                ("project", query.scope.scope_id, query.scope.repository_id, query.scope.project_id),
                ("repository", query.scope.repository_id, query.scope.repository_id, query.scope.project_id),
                ("profile", query.scope.principal_id, None, None),
            ))
        elif query.scope.scope_type == "repository":
            decision_scope_specs.extend((
                ("repository", query.scope.scope_id, query.scope.repository_id, query.scope.project_id),
                ("profile", query.scope.principal_id, None, None),
            ))
        else:
            decision_scope_specs.append((
                "profile", query.scope.scope_id, None, None
            ))
        decision_rows: list[dict[str, Any]] = []
        with self._available_store() as store:
            for scope_type, scope_id, repository_id, project_id in decision_scope_specs:
                decision_rows.extend(
                    store.search_decisions(
                        principal_id=query.scope.principal_id,
                        scope_type=scope_type,
                        scope_id=scope_id,
                        repository_id=repository_id,
                        project_id=project_id,
                        policy_repository_id=query.scope.repository_id,
                        policy_project_id=query.scope.project_id,
                        provider_trust_domain=provider_domain,
                        provider_is_local=query.provider_is_local,
                        query=query.query_text,
                        tags=self._query_tags(query),
                        min_confidence=self.min_confidence,
                        require_injection_allowed=require_injection_allowed,
                        limit=max(0, int(max_decisions)),
                        repository_root=repository_root,
                    )
                )
            lesson_rows = store.search_lessons(
                principal_id=query.scope.principal_id,
                scope_type=query.scope.scope_type,
                scope_id=query.scope.scope_id,
                repository_id=query.scope.repository_id,
                project_id=query.scope.project_id,
                provider_trust_domain=provider_domain,
                provider_is_local=query.provider_is_local,
                query=query.query_text,
                tags=self._query_tags(query),
                min_confidence=self.min_confidence,
                require_injection_allowed=require_injection_allowed,
                limit=min(max(0, int(max_lessons)), limit),
            )
        policy = None
        if query.scope.repository_id and query.scope.project_id:
            raw_policy = self.store.get_scope_policy(
                principal_id=query.scope.principal_id,
                repository_id=query.scope.repository_id,
                project_id=query.scope.project_id,
            )
            if raw_policy is not None:
                policy = ScopePolicy.from_mapping(raw_policy)
        decisions: list[DecisionMatch] = []
        lessons: list[RetrievalMatch] = []
        disclosures: list[RetrievalDisclosure] = []
        seen_decision_ids: set[str] = set()
        for row in decision_rows:
            if row["id"] in seen_decision_ids:
                continue
            seen_decision_ids.add(row["id"])
            if policy is None or not is_egress_allowed(
                sensitivity=row["sensitivity"],
                egress_policy=row["egress_policy"],
                producer_trust_domain=row.get("producer_trust_domain"),
                current_trust_domain=provider_domain,
                current_provider_is_local=query.provider_is_local,
                max_egress_policy=policy.max_egress_policy,
            ):
                continue
            match = self._decision_match_from_mapping(row, rank=len(decisions) + 1)
            decisions.append(match)
            disclosures.append(
                RetrievalDisclosure(
                    item_id=match.item_id,
                    item_revision=match.item_revision,
                    sensitivity=str(row["sensitivity"]),
                    egress_policy=str(row["egress_policy"]),
                    producer_trust_domain=row.get("producer_trust_domain"),
                )
            )
        for row in lesson_rows:
            if policy is None or not is_egress_allowed(
                sensitivity=row["sensitivity"],
                egress_policy=row["egress_policy"],
                producer_trust_domain=row.get("producer_trust_domain"),
                current_trust_domain=provider_domain,
                current_provider_is_local=query.provider_is_local,
                max_egress_policy=policy.max_egress_policy,
            ):
                continue
            match = self._lesson_match_from_mapping(row, rank=len(lessons) + 1)
            lessons.append(match)
            disclosures.append(
                RetrievalDisclosure(
                    item_id=match.item_id,
                    item_revision=match.item_revision,
                    sensitivity=str(row["sensitivity"]),
                    egress_policy=str(row["egress_policy"]),
                    producer_trust_domain=row.get("producer_trust_domain"),
                )
            )
        resolved_retrieval_id = retrieval_id or self._retrieval_id(
            turn_id=turn_id,
            work_id=work_id,
            signature_hash=signature_hash,
            query=query,
        )
        with self._available_store() as store:
            stored = store.record_retrieval(
                retrieval_id=resolved_retrieval_id,
                idempotency_key=idempotency_key or resolved_retrieval_id,
                turn_id=turn_id,
                work_id=work_id,
                principal_id=query.scope.principal_id,
                repository_id=query.scope.repository_id or "profile",
                project_id=query.scope.project_id or query.scope.scope_id,
                task_signature_hash=signature_hash,
                provider_trust_domain=provider_domain,
                items=[
                    {
                        "item_id": item.item_id,
                        "item_revision": item.item_revision,
                        "rank": item.rank,
                        "score": item.score,
                        "match_reasons": item.match_reasons,
                    }
                    for item in (*decisions, *lessons)
                ],
            )
        diagnostic = RetrievalDiagnostic(
            id=stored["id"],
            turn_id=stored["turn_id"],
            work_id=stored["work_id"],
            principal_id=stored["principal_id"],
            repository_id=stored["repository_id"],
            project_id=stored["project_id"],
            task_signature_hash=stored["task_signature_hash"],
            provider_trust_domain=stored["provider_trust_domain"],
            created_at=stored["created_at"],
        )
        item_diagnostics = tuple(
            RetrievalItemDiagnostic(
                retrieval_id=item["retrieval_id"],
                item_id=item["item_id"],
                item_revision=item["item_revision"],
                rank=item["rank"],
                score=item["score"],
                match_reasons=tuple(item["match_reasons"]),
                disposition=RetrievalDisposition(item["disposition"]),
            )
            for item in stored["items"]
        )
        return CombinedRetrievalResult(
            diagnostic=diagnostic,
            query=query,
            decisions=tuple(decisions),
            lessons=tuple(lessons),
            item_diagnostics=item_diagnostics,
            fts_enabled=self.store.fts_enabled,
            disclosures=tuple(disclosures),
        )

    def record_disclosure_events(
        self,
        result: RetrievalResult | CombinedRetrievalResult,
        *,
        retrieval_id: str | None = None,
        work_id: str | None = None,
        provider_trust_domain: str | None = None,
        provider_is_local: bool | None = None,
        created_at: float | None = None,
    ) -> int:
        """Record disclosure events for items that were actually injected."""

        if hasattr(result, "decisions") and hasattr(result, "lessons"):
            items = tuple(result.decisions) + tuple(result.lessons)
        else:
            items = tuple(result.items)
        if not items:
            return 0
        diagnostic = getattr(result, "diagnostic", None)
        resolved_retrieval_id = retrieval_id or getattr(diagnostic, "id", None)
        resolved_work_id = work_id or getattr(diagnostic, "work_id", None)
        if not resolved_retrieval_id or not resolved_work_id:
            return 0
        query = getattr(result, "query", None)
        current_domain = (
            provider_trust_domain
            or getattr(query, "provider_trust_domain", None)
            or "local-runtime"
        )
        current_is_local = (
            bool(provider_is_local)
            if provider_is_local is not None
            else bool(getattr(query, "provider_is_local", False))
        )
        if hasattr(result, "decisions") and hasattr(result, "lessons"):
            rendered = self.format_combined_context(
                result,
                provider_trust_domain=current_domain,
                provider_is_local=current_is_local,
            )
        else:
            rendered = self.format_context(
                result,
                provider_trust_domain=current_domain,
                provider_is_local=current_is_local,
            )
        if not rendered:
            return 0
        recorded = 0
        with self._available_store() as store:
            for item in items:
                if hasattr(item, "body") and hasattr(item.body, "statement"):
                    marker = f"[decision {html.escape(item.item_id[:24])}"
                else:
                    marker = f"[lesson {html.escape(item.item_id[:24])}"
                if marker not in rendered:
                    continue
                material = "\0".join(
                    (
                        "disclosed",
                        resolved_retrieval_id,
                        item.item_id,
                        str(item.item_revision),
                    )
                )
                event_id = "event_" + hashlib.sha256(material.encode()).hexdigest()[:32]
                store.record_influence_event(
                    event_type="disclosed",
                    item_id=item.item_id,
                    item_revision=item.item_revision,
                    retrieval_id=resolved_retrieval_id,
                    work_id=resolved_work_id,
                    payload={
                        "provider_trust_domain": current_domain,
                        "provider_is_local": current_is_local,
                    },
                    created_at=created_at,
                    event_id=event_id,
                )
                recorded += 1
        return recorded

    def activate_decision(
        self,
        item_id: str,
        *,
        authority: DecisionTurnAuthority,
        repository_root: str | None = None,
        repository_id: str | None = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        """Activate a Decision through trusted authority."""

        with self._available_store() as store:
            return store.activate_decision(
                item_id,
                authority=authority,
                repository_root=repository_root,
                repository_id=repository_id,
                **kwargs,
            )

    def reapprove_decision(
        self,
        item_id: str,
        *,
        authority: DecisionTurnAuthority,
        repository_root: str | None = None,
        repository_id: str | None = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        """Reapprove a reviewed Decision through trusted authority."""

        with self._available_store() as store:
            return store.reapprove_decision(
                item_id,
                authority=authority,
                repository_root=repository_root,
                repository_id=repository_id,
                **kwargs,
            )

    def supersede_decision(
        self,
        old_item_id: str,
        *,
        authority: DecisionTurnAuthority,
        repository_root: str | None = None,
        repository_id: str | None = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        """Create a replacement Decision and supersede the old one."""

        with self._available_store() as store:
            return store.supersede_decision(
                old_item_id,
                authority=authority,
                repository_root=repository_root,
                repository_id=repository_id,
                **kwargs,
            )

    def revoke_decision(
        self,
        item_id: str,
        *,
        authority: DecisionTurnAuthority,
        **kwargs: object,
    ) -> dict[str, Any]:
        """Revoke a Decision through explicit trusted authority."""

        with self._available_store() as store:
            return store.revoke_decision(item_id, authority=authority, **kwargs)

    def mark_decision_review_required(
        self,
        item_id: str,
        *,
        repository_root: str | None = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        """Move a Decision to review_required without exposing it."""

        with self._available_store() as store:
            return store.mark_decision_review_required(
                item_id,
                repository_root=repository_root,
                **kwargs,
            )

    def validate_active_decision(
        self,
        item_id: str,
        *,
        repository_root: str | None = None,
        now: float | None = None,
    ) -> bool:
        """Return whether an active Decision remains valid for disclosure."""

        current_time = float(time.time() if now is None else now)
        with self._available_store() as store:
            item = store.get_decision(item_id)
            if item is None or item["current_status"] != "active":
                return False
            body = item["revision"]["body"]
            if body["authority"] not in {"user", "repository_policy"}:
                return False
            if (
                body.get("expires_at") is not None
                and float(body["expires_at"]) <= current_time
            ):
                store.mark_decision_review_required(
                    item_id,
                    reason="Decision expired",
                    transitioned_at=current_time,
                )
                return False
            if (
                body.get("review_after") is not None
                and float(body["review_after"]) <= current_time
            ):
                store.mark_decision_review_required(
                    item_id,
                    reason="Decision review date reached",
                    transitioned_at=current_time,
                )
                return False
            if body["authority"] == "repository_policy" and repository_root is not None:
                validation = validate_repository_anchor(
                    body["policy_anchor_path"],
                    body["policy_anchor_hash"],
                    repository_root=repository_root,
                )
                if not validation.valid:
                    store.mark_decision_review_required(
                        item_id,
                        reason=validation.reason,
                        repository_root=repository_root,
                        transitioned_at=current_time,
                    )
                    return False
            return True

    def format_context(
        self,
        result: RetrievalResult,
        *,
        max_chars: int | None = None,
        provider_trust_domain: str | None = None,
        provider_is_local: bool | None = None,
        repository_root: str | None = None,
    ) -> str:
        """Build bounded advice after rechecking current policy and provider.

        The optional provider fields are for a fallback request whose egress
        identity differs from the provider used to rank the cached result.
        """

        if not result.items:
            return ""
        budget = self.max_context_chars if max_chars is None else int(max_chars)
        if not 256 <= budget <= 16_384:
            raise ValueError("max_chars must be between 256 and 16384")
        opening = (
            "<work-experience-context>\n"
            f"retrieval_ref: {html.escape(result.diagnostic.id[:24])}\n"
            "Historical, fallible evidence. Current user instructions, repository "
            "state, tests, and project policy take precedence.\n"
        )
        closing = "</work-experience-context>"
        current_is_local = (
            result.query.provider_is_local
            if provider_is_local is None
            else provider_is_local
        )
        if not isinstance(current_is_local, bool):
            raise TypeError("provider_is_local must be bool")
        current_domain = (
            provider_trust_domain
            if provider_trust_domain is not None
            else result.query.provider_trust_domain
        )
        if current_is_local and current_domain is None:
            current_domain = _LOCAL_TRUST_DOMAIN
        if not current_is_local and not current_domain:
            return ""
        with self._available_store() as store:
            still_authorized = store.authorized_lesson_revisions(
                principal_id=result.query.scope.principal_id,
                scope_type=result.query.scope.scope_type,
                scope_id=result.query.scope.scope_id,
                repository_id=result.query.scope.repository_id,
                project_id=result.query.scope.project_id,
                provider_trust_domain=current_domain,
                provider_is_local=current_is_local,
                candidates=(
                    (item.item_id, item.item_revision) for item in result.items
                ),
                require_injection_allowed=True,
            )
        eligible_items = tuple(
            item
            for item in result.items
            if (item.item_id, item.item_revision) in still_authorized
        )
        if not eligible_items:
            return ""
        chunks: list[str] = []
        remaining = budget - len(opening) - len(closing) - 1
        for item in eligible_items:
            chunk = self._format_item(item, max_chars=max(96, remaining))
            needed = len(chunk) + (1 if chunks else 0)
            if needed > remaining:
                break
            chunks.append(chunk)
            remaining -= needed
        if not chunks:
            return ""
        rendered = opening + "\n".join(chunks) + "\n" + closing
        return sanitize_for_return(rendered, max_chars=budget)

    def format_combined_context(
        self,
        result: CombinedRetrievalResult,
        *,
        max_chars: int | None = None,
        provider_trust_domain: str | None = None,
        provider_is_local: bool | None = None,
        repository_root: str | None = None,
    ) -> str:
        """Render separate active Decision and Lesson blocks after reauthorization."""

        budget = self.max_context_chars if max_chars is None else int(max_chars)
        if not 256 <= budget <= 16_384:
            raise ValueError("max_chars must be between 256 and 16384")
        current_is_local = (
            result.query.provider_is_local
            if provider_is_local is None
            else provider_is_local
        )
        if not isinstance(current_is_local, bool):
            raise TypeError("provider_is_local must be bool")
        current_domain = (
            provider_trust_domain
            if provider_trust_domain is not None
            else result.query.provider_trust_domain
        )
        if current_is_local and current_domain is None:
            current_domain = _LOCAL_TRUST_DOMAIN
        if not current_is_local and not current_domain:
            return ""
        with self._available_store() as store:
            decision_ids: set[tuple[str, int]] = set()
            grouped_decisions: dict[
                tuple[str, str, str | None, str | None], list[DecisionMatch]
            ] = {}
            for item in result.decisions:
                key = (
                    item.scope_type,
                    item.scope_id,
                    item.repository_id,
                    item.project_id,
                )
                grouped_decisions.setdefault(key, []).append(item)
            for (scope_type, scope_id, repository_id, project_id), items in grouped_decisions.items():
                policy_repository_id = (
                    result.query.scope.repository_id
                    if scope_type == "profile"
                    else repository_id
                )
                policy_project_id = (
                    result.query.scope.project_id
                    if scope_type == "profile"
                    else project_id
                )
                try:
                    decision_ids.update(
                        store.authorized_decision_revisions(
                            principal_id=result.query.scope.principal_id,
                            scope_type=scope_type,
                            scope_id=scope_id,
                            repository_id=repository_id,
                            project_id=project_id,
                            policy_repository_id=result.query.scope.repository_id,
                            policy_project_id=result.query.scope.project_id,
                            provider_trust_domain=current_domain,
                            provider_is_local=current_is_local,
                            candidates=(
                                (item.item_id, item.item_revision) for item in items
                            ),
                            require_injection_allowed=True,
                            repository_root=repository_root,
                        )
                    )
                except ValueError:
                    continue
            lesson_ids = set(
                store.authorized_lesson_revisions(
                    principal_id=result.query.scope.principal_id,
                    scope_type=result.query.scope.scope_type,
                    scope_id=result.query.scope.scope_id,
                    repository_id=result.query.scope.repository_id,
                    project_id=result.query.scope.project_id,
                    provider_trust_domain=current_domain,
                    provider_is_local=current_is_local,
                    candidates=(
                        (item.item_id, item.item_revision) for item in result.lessons
                    ),
                    require_injection_allowed=True,
                )
            )
        decision_items = tuple(
            item
            for item in result.decisions
            if (item.item_id, item.item_revision) in decision_ids
        )
        lesson_items = tuple(
            item
            for item in result.lessons
            if (item.item_id, item.item_revision) in lesson_ids
        )
        chunks: list[str] = []
        remaining = budget - 1
        if decision_items:
            decision_chunk = self._format_decision_block(result, decision_items, max_chars=remaining)
            needed = len(decision_chunk) + (1 if chunks else 0)
            if needed <= remaining:
                chunks.append(decision_chunk)
                remaining -= needed
        if lesson_items:
            lesson_chunk = self._format_lesson_block(result, lesson_items, max_chars=remaining)
            needed = len(lesson_chunk) + (1 if chunks else 0)
            if needed <= remaining:
                chunks.append(lesson_chunk)
        if not chunks:
            return ""
        return sanitize_for_return("\n\n".join(chunks), max_chars=budget)

    @staticmethod
    def _bounded_context_text(text: str, max_chars: int) -> str:
        escaped = html.escape(text)
        if len(escaped) <= max_chars:
            return escaped
        if max_chars <= 1:
            return ""
        return escaped[: max_chars - 1].rstrip() + "…"

    @staticmethod
    def _format_decision_item(item: DecisionMatch, *, max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        source = item.source_turn_id or item.source_session_id or item.source_work_id or "opaque"
        if item.repository_id and item.project_id:
            scope = f"project/{item.scope_id}"
        elif item.repository_id:
            scope = f"repository/{item.scope_id}"
        else:
            scope = f"profile/{item.scope_id}"
        header = (
            f"[decision {html.escape(item.item_id[:24])} "
            f"family={html.escape(item.family_id[:24])} "
            f"revision={item.item_revision}]"
        )
        authority = f"authority: {html.escape(item.authority)}"
        scope_line = f"scope: {html.escape(scope)}"
        statement_prefix = "statement: "
        rationale_prefix = "rationale: "
        source_line = f"source: {html.escape(source)}"
        match_line = "match: " + html.escape("; ".join(item.match_reasons))

        if max_chars <= len(header) + 1 + len(statement_prefix):
            return ExperienceService._bounded_context_text(
                header, max_chars
            )

        statement_budget = max_chars - len(header) - 1 - len(statement_prefix)
        if max_chars >= 180:
            rationale_budget = max(
                24,
                max_chars - len(header) - 1 * 4
                - len(authority) - len(scope_line) - len(source_line)
                - len(match_line) - len(statement_prefix) - len(rationale_prefix),
            )
            lines = [
                header,
                authority,
                scope_line,
                statement_prefix + ExperienceService._bounded_context_text(
                    item.body.statement, statement_budget
                ),
                rationale_prefix + ExperienceService._bounded_context_text(
                    item.body.rationale, rationale_budget
                ),
                source_line,
                match_line,
            ]
            rendered = "\n".join(lines)
            if len(rendered) <= max_chars:
                return rendered
            return rendered[: max(0, max_chars - 1)].rstrip() + "…"

        statement = ExperienceService._bounded_context_text(
            item.body.statement, statement_budget
        )
        return "\n".join((header, statement_prefix + statement))[: max(0, max_chars - 1)].rstrip() + "…"

    @staticmethod
    def _format_decision_block(
        result: CombinedRetrievalResult,
        items: tuple[DecisionMatch, ...],
        *,
        max_chars: int,
    ) -> str:
        opening = (
            "<active-decision-context>\n"
            f"retrieval_ref: {html.escape(result.diagnostic.id[:24])}\n"
            "Historical continuing decisions; live instructions, policy, "
            "tests, and repo state win.\n"
        )
        lines = [opening.rstrip()]
        remaining = max_chars - len(opening) - len("\n</active-decision-context>")
        for item in items:
            if remaining <= 0:
                break
            item_budget = max(1, min(remaining - (1 if lines else 0), int(max_chars * 0.45)))
            chunk = ExperienceService._format_decision_item(item, max_chars=item_budget)
            needed = len(chunk) + (1 if lines else 0)
            if needed > remaining:
                break
            lines.append(chunk)
            remaining -= needed
        rendered = "\n".join(lines) + "\n</active-decision-context>"
        if len(rendered) > max_chars:
            rendered = rendered[: max(0, max_chars - 1)].rstrip() + "…"
        return sanitize_for_return(rendered, max_chars=max_chars)

    def _format_lesson_block(
        self,
        result: CombinedRetrievalResult,
        items: tuple[RetrievalMatch, ...],
        *,
        max_chars: int,
    ) -> str:
        opening = (
            "<work-experience-context>\n"
            f"retrieval_ref: {html.escape(result.diagnostic.id[:24])}\n"
            "Historical, fallible evidence. Current user instructions, repository "
            "state, tests, and project policy take precedence.\n"
        )
        chunks: list[str] = []
        remaining = max_chars - len(opening) - len("\n</work-experience-context>")
        for item in items:
            if remaining <= 0:
                break
            chunk = self._format_item(item, max_chars=remaining)
            needed = len(chunk) + (1 if chunks else 0)
            if needed > remaining:
                break
            chunks.append(chunk)
            remaining -= needed
        rendered = opening + "\n".join(chunks) + "\n</work-experience-context>"
        return sanitize_for_return(rendered, max_chars=max_chars)

    @staticmethod
    def _format_item(item: RetrievalMatch, *, max_chars: int) -> str:
        confidence = (
            "unknown" if item.confidence is None else f"{item.confidence:.2f}"
        )
        lines = [
            f"[lesson {html.escape(item.item_id[:24])} rev={item.item_revision} "
            f"status=active confidence={confidence}]",
            "applies_when: " + html.escape(item.body.applies_when),
            "guidance: " + html.escape(item.body.guidance),
            "rationale: " + html.escape(item.body.rationale),
            "match: " + html.escape("; ".join(item.match_reasons)),
        ]
        if item.body.does_not_apply_when:
            lines.insert(
                2,
                "does_not_apply_when: "
                + html.escape(item.body.does_not_apply_when),
            )
        rendered = "\n".join(lines)
        if len(rendered) <= max_chars:
            return rendered
        # Keep a structurally complete item and favor trigger/guidance over
        # rationale when the configured context budget is tight.
        compact = "\n".join(
            (
                lines[0],
                "applies_when: " + html.escape(item.body.applies_when),
                "guidance: " + html.escape(item.body.guidance),
                lines[-1],
            )
        )
        if len(compact) <= max_chars:
            return compact
        return compact[: max(0, max_chars - 1)].rstrip() + "…"


__all__ = [
    "CombinedRetrievalResult",
    "ExperienceService",
    "RetrievalDisclosure",
    "RetrievalResult",
]

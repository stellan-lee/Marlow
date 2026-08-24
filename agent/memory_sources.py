"""Adapters from legacy and Experience recall into typed memory candidates."""

from __future__ import annotations

import hashlib
from typing import Any, Sequence

from agent.memory_types import (
    DynamicMemoryAuthority,
    DynamicMemoryKind,
    DynamicMemoryStatus,
    MemoryCandidate,
)


def legacy_provider_candidate(provider_name: str, content: str) -> MemoryCandidate:
    """Wrap one legacy provider string without promoting its claims."""

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return MemoryCandidate(
        id=f"legacy:{provider_name}:{digest[:24]}",
        kind=DynamicMemoryKind.RECOLLECTION,
        content=content,
        authority=DynamicMemoryAuthority.UNVERIFIED_EXTERNAL,
        status=DynamicMemoryStatus.ACTIVE,
        scope_type="provider",
        scope_id=provider_name,
        confidence=None,
        source_provider=provider_name,
        source_ref=digest,
        match_reasons=("legacy provider recall",),
        sensitivity="local_only",
        egress_policy="same_provider_trust_domain",
        producer_trust_domain=f"memory-provider:{provider_name}",
        canonical=False,
    )


def experience_result_candidates(result: Any) -> tuple[MemoryCandidate, ...]:
    """Map authorized Experience matches to canonical typed candidates."""

    candidates: list[MemoryCandidate] = []
    for match in tuple(getattr(result, "decisions", ()) or ()):
        body = match.body
        authority = (
            DynamicMemoryAuthority.REPOSITORY_POLICY
            if str(match.authority) == "repository_policy"
            else DynamicMemoryAuthority.USER_APPROVED
        )
        content = f"{match.title}\n{body.statement}\nRationale: {body.rationale}".strip()
        candidates.append(MemoryCandidate(
            id=f"{match.item_id}:r{match.item_revision}",
            kind=DynamicMemoryKind.DECISION,
            content=content,
            authority=authority,
            status=DynamicMemoryStatus.ACTIVE,
            scope_type=str(match.scope_type),
            scope_id=match.scope_id,
            confidence=match.confidence,
            source_provider="experience",
            source_ref=match.item_id,
            updated_at=match.updated_at,
            match_reasons=tuple(match.match_reasons),
            sensitivity="local_only",
            egress_policy="local_only",
            canonical=True,
        ))
    for match in tuple(getattr(result, "lessons", ()) or ()):
        body = match.body
        content = f"{match.title}\n{body.guidance}\nRationale: {body.rationale}".strip()
        candidates.append(MemoryCandidate(
            id=f"{match.item_id}:r{match.item_revision}",
            kind=DynamicMemoryKind.LESSON,
            content=content,
            authority=DynamicMemoryAuthority.ADVISORY,
            status=DynamicMemoryStatus.ACTIVE,
            scope_type="project",
            scope_id="experience",
            confidence=match.confidence,
            source_provider="experience",
            source_ref=match.item_id,
            match_reasons=tuple(match.match_reasons),
            sensitivity="local_only",
            egress_policy="local_only",
            canonical=True,
        ))
    return tuple(candidates)


def render_dynamic_candidates(candidates: Sequence[MemoryCandidate]) -> str:
    """Render bounded active candidates while preserving trust labels."""

    sections: list[str] = []
    for candidate in candidates:
        if candidate.status is not DynamicMemoryStatus.ACTIVE:
            continue
        label = (
            "canonical decision"
            if candidate.kind is DynamicMemoryKind.DECISION and candidate.canonical
            else "advisory recollection"
        )
        sections.append(
            f"[{label}; source={candidate.source_provider}; authority={candidate.authority.value}]\n"
            f"{candidate.content}"
        )
    return "\n\n".join(sections)


__all__ = [
    "experience_result_candidates",
    "legacy_provider_candidate",
    "render_dynamic_candidates",
]

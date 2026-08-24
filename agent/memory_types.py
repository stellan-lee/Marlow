"""Provider-neutral typed dynamic-memory contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum


class DynamicMemoryKind(StrEnum):
    PROFILE = "profile"
    DECISION = "decision"
    LESSON = "lesson"
    FACT = "fact"
    RECOLLECTION = "recollection"


class DynamicMemoryAuthority(StrEnum):
    USER_APPROVED = "user_approved"
    REPOSITORY_POLICY = "repository_policy"
    ADVISORY = "advisory"
    UNVERIFIED_EXTERNAL = "unverified_external"


class DynamicMemoryStatus(StrEnum):
    ACTIVE = "active"
    CANDIDATE = "candidate"
    REVIEW_REQUIRED = "review_required"
    CONFLICTED = "conflicted"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


def _text(value: object, name: str, limit: int, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    result = value.strip()
    if not result and not optional:
        raise ValueError(f"{name} must not be empty")
    if len(result) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return result or None


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    id: str
    kind: DynamicMemoryKind
    content: str
    authority: DynamicMemoryAuthority
    status: DynamicMemoryStatus
    scope_type: str
    scope_id: str
    confidence: float | None
    source_provider: str
    source_ref: str | None = None
    updated_at: float | None = None
    match_reasons: tuple[str, ...] = field(default_factory=tuple)
    sensitivity: str = "normal"
    egress_policy: str = "local_only"
    producer_trust_domain: str | None = None
    canonical: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id", 256))
        object.__setattr__(self, "kind", DynamicMemoryKind(self.kind))
        object.__setattr__(self, "content", _text(self.content, "content", 16_384))
        object.__setattr__(self, "authority", DynamicMemoryAuthority(self.authority))
        object.__setattr__(self, "status", DynamicMemoryStatus(self.status))
        object.__setattr__(self, "scope_type", _text(self.scope_type, "scope_type", 64))
        object.__setattr__(self, "scope_id", _text(self.scope_id, "scope_id", 256))
        object.__setattr__(self, "source_provider", _text(self.source_provider, "source_provider", 128))
        object.__setattr__(self, "source_ref", _text(self.source_ref, "source_ref", 512, optional=True))
        object.__setattr__(self, "sensitivity", _text(self.sensitivity, "sensitivity", 64))
        object.__setattr__(self, "egress_policy", _text(self.egress_policy, "egress_policy", 64))
        object.__setattr__(
            self,
            "producer_trust_domain",
            _text(self.producer_trust_domain, "producer_trust_domain", 256, optional=True),
        )
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
                raise TypeError("confidence must be numeric")
            if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
                raise ValueError("confidence must be between 0 and 1")
            object.__setattr__(self, "confidence", float(self.confidence))
        if self.updated_at is not None:
            if isinstance(self.updated_at, bool) or not isinstance(self.updated_at, (int, float)):
                raise TypeError("updated_at must be numeric")
            if not math.isfinite(self.updated_at) or self.updated_at < 0:
                raise ValueError("updated_at must be a non-negative timestamp")
            object.__setattr__(self, "updated_at", float(self.updated_at))
        reasons = tuple(dict.fromkeys(
            _text(reason, "match reason", 500) for reason in self.match_reasons
        ))
        object.__setattr__(self, "match_reasons", reasons)
        if not isinstance(self.canonical, bool):
            raise TypeError("canonical must be bool")


@dataclass(frozen=True, slots=True)
class MemoryRecallRequest:
    query_text: str
    session_id: str
    turn_id: str
    principal_id: str
    repository_id: str | None
    project_id: str | None
    provider_trust_domain: str | None
    provider_is_local: bool
    max_candidates: int = 8

    def __post_init__(self) -> None:
        object.__setattr__(self, "query_text", _text(self.query_text, "query_text", 4_000))
        object.__setattr__(self, "session_id", _text(self.session_id, "session_id", 256))
        object.__setattr__(self, "turn_id", _text(self.turn_id, "turn_id", 256))
        object.__setattr__(self, "principal_id", _text(self.principal_id, "principal_id", 256))
        object.__setattr__(self, "repository_id", _text(self.repository_id, "repository_id", 256, optional=True))
        object.__setattr__(self, "project_id", _text(self.project_id, "project_id", 256, optional=True))
        object.__setattr__(
            self,
            "provider_trust_domain",
            _text(self.provider_trust_domain, "provider_trust_domain", 256, optional=True),
        )
        if not isinstance(self.provider_is_local, bool):
            raise TypeError("provider_is_local must be bool")
        if isinstance(self.max_candidates, bool) or not isinstance(self.max_candidates, int):
            raise TypeError("max_candidates must be an integer")
        if not 1 <= self.max_candidates <= 50:
            raise ValueError("max_candidates must be between 1 and 50")


__all__ = [
    "DynamicMemoryAuthority",
    "DynamicMemoryKind",
    "DynamicMemoryStatus",
    "MemoryCandidate",
    "MemoryRecallRequest",
]

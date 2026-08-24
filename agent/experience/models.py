"""Small typed contract for the work-experience validation MVP.

Manually managed lessons and candidate Decisions are modeled here.  Durable views are frozen so
editing means creating a new :class:`LessonRevision`, never mutating evidence
or provenance attached to an older revision.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


LOCAL_OWNER_PRINCIPAL = "local-owner"


class LessonStatus(StrEnum):
    """Canonical lesson lifecycle; ``proposed`` is a legacy input alias."""

    CANDIDATE = "candidate"
    PROPOSED = "candidate"
    ACTIVE = "active"
    DISPUTED = "disputed"
    DEPRECATED = "deprecated"
    REJECTED = "rejected"
    RETRACTED = "retracted"

    @classmethod
    def _missing_(cls, value: object) -> "LessonStatus | None":
        if isinstance(value, str) and value.strip().lower() == "proposed":
            return cls.CANDIDATE
        return None


class ScopeType(StrEnum):
    PROJECT = "project"
    REPOSITORY = "repository"
    PROFILE = "profile"


class Sensitivity(StrEnum):
    NORMAL = "normal"
    PRIVATE_REPO = "private_repo"
    LOCAL_ONLY = "local_only"
    BLOCKED = "blocked"


class EgressPolicy(StrEnum):
    LOCAL_ONLY = "local_only"
    SAME_PROVIDER_TRUST_DOMAIN = "same_provider_trust_domain"
    EXPLICIT_ANY_PROVIDER = "explicit_any_provider"


class CreatedBy(StrEnum):
    USER = "user"
    AGENT = "agent"
    IMPORT = "import"


class DecisionAuthority(StrEnum):
    """Authority that permits a Decision to become active."""

    UNAPPROVED = "unapproved"
    USER = "user"
    REPOSITORY_POLICY = "repository_policy"


class DecisionSourceType(StrEnum):
    """Origin of a Decision body."""

    USER_TURN = "user_turn"
    REPOSITORY_POLICY = "repository_policy"
    AGENT_PROPOSAL = "agent_proposal"
    MIGRATION = "migration"
    MANUAL_IMPORT = "manual_import"


class TagNamespace(StrEnum):
    TASK_TYPE = "task_type"
    TECHNOLOGY = "technology"
    ENTITY = "entity"
    FAILURE = "failure"
    COMPONENT = "component"


class DecisionStatus(StrEnum):
    """Canonical Decision lifecycle; candidates are not injectable."""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    REVIEW_REQUIRED = "review_required"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class RetrievalDisposition(StrEnum):
    RETRIEVED = "retrieved"


_TRANSITIONS = {
    LessonStatus.CANDIDATE: {LessonStatus.ACTIVE, LessonStatus.REJECTED, LessonStatus.RETRACTED},
    LessonStatus.ACTIVE: {LessonStatus.DISPUTED, LessonStatus.DEPRECATED, LessonStatus.RETRACTED},
    LessonStatus.DISPUTED: {LessonStatus.DEPRECATED, LessonStatus.RETRACTED},
    LessonStatus.DEPRECATED: set(),
    LessonStatus.REJECTED: set(),
    LessonStatus.RETRACTED: set(),
}

_DECISION_TRANSITIONS = {
    DecisionStatus.CANDIDATE: {DecisionStatus.ACTIVE, DecisionStatus.REVIEW_REQUIRED, DecisionStatus.REVOKED},
    DecisionStatus.ACTIVE: {DecisionStatus.REVIEW_REQUIRED, DecisionStatus.SUPERSEDED, DecisionStatus.REVOKED},
    DecisionStatus.REVIEW_REQUIRED: {DecisionStatus.ACTIVE, DecisionStatus.SUPERSEDED, DecisionStatus.REVOKED},
    DecisionStatus.SUPERSEDED: set(),
    DecisionStatus.REVOKED: set(),
}


def normalize_lesson_status(value: LessonStatus | str) -> LessonStatus:
    """Return the stored spelling, accepting the old ``proposed`` term."""

    if isinstance(value, LessonStatus):
        return value
    try:
        return LessonStatus(value.strip().lower())
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"unsupported lesson status: {value!r}") from exc


def can_transition_lesson(current: LessonStatus | str, target: LessonStatus | str) -> bool:
    """Return whether a transition is legal; same-state retries are idempotent."""

    before, after = normalize_lesson_status(current), normalize_lesson_status(target)
    return before == after or after in _TRANSITIONS[before]


def require_lesson_transition(current: LessonStatus | str, target: LessonStatus | str) -> LessonStatus:
    """Validate a lifecycle transition and return its canonical target."""

    before, after = normalize_lesson_status(current), normalize_lesson_status(target)
    if not can_transition_lesson(before, after):
        raise ValueError(f"invalid lesson transition: {before.value} -> {after.value}")
    return after


def normalize_decision_status(value: DecisionStatus | str) -> DecisionStatus:
    """Return the stored spelling for a Decision status."""

    if isinstance(value, DecisionStatus):
        return value
    try:
        return DecisionStatus(value.strip().lower())
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"unsupported decision status: {value!r}") from exc


def can_transition_decision(current: DecisionStatus | str, target: DecisionStatus | str) -> bool:
    """Return whether a Decision transition is legal; same-state retries are idempotent."""

    before, after = normalize_decision_status(current), normalize_decision_status(target)
    return before == after or after in _DECISION_TRANSITIONS[before]


def require_decision_transition(current: DecisionStatus | str, target: DecisionStatus | str) -> DecisionStatus:
    """Validate a Decision lifecycle transition and return its canonical target."""

    before, after = normalize_decision_status(current), normalize_decision_status(target)
    if not can_transition_decision(before, after):
        raise ValueError(f"invalid decision transition: {before.value} -> {after.value}")
    return after


def _text(value: object, name: str, limit: int, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    value = value.strip()
    if not value:
        if optional:
            return None
        raise ValueError(f"{name} must not be empty")
    if "\x00" in value or len(value) > limit:
        raise ValueError(f"{name} is invalid or exceeds {limit} characters")
    return value


def _possibly_empty_text(value: object, name: str, limit: int) -> str:
    """Validate a bounded string while preserving an intentional empty value."""

    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    value = value.strip()
    if "\x00" in value or len(value) > limit:
        raise ValueError(f"{name} is invalid or exceeds {limit} characters")
    return value


def _optional_digest(value: object, name: str) -> str | None:
    normalized = _text(value, name, 64, optional=True)
    if normalized is None:
        return None
    normalized = normalized.casefold()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return normalized


def _optional_trust_domain(value: object) -> str | None:
    normalized = _text(value, "provider_trust_domain", 128, optional=True)
    if normalized is None:
        return None
    from agent.experience.safety import normalize_trust_domain

    return normalize_trust_domain(normalized)


def _time(value: float | int | None, name: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a timestamp")
    value = float(value)
    if value < 0 or not math.isfinite(value):
        raise ValueError(f"{name} must be finite and non-negative")
    return value


@dataclass(frozen=True, slots=True)
class LessonBody:
    """Bounded safe fields that may become searchable or model-visible."""

    applies_when: str
    guidance: str
    rationale: str
    does_not_apply_when: str | None = None

    def __post_init__(self) -> None:
        for name in ("applies_when", "guidance", "rationale"):
            object.__setattr__(self, name, _text(getattr(self, name), name, 4_000))
        object.__setattr__(
            self, "does_not_apply_when", _text(self.does_not_apply_when, "does_not_apply_when", 4_000, optional=True)
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "applies_when": self.applies_when,
            "does_not_apply_when": self.does_not_apply_when,
            "guidance": self.guidance,
            "rationale": self.rationale,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LessonBody":
        if not isinstance(value, Mapping):
            raise TypeError("lesson body must be a mapping")
        allowed = {
            "applies_when",
            "does_not_apply_when",
            "guidance",
            "rationale",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown lesson body fields: {sorted(unknown)!r}")
        return cls(
            applies_when=value.get("applies_when", ""),
            does_not_apply_when=value.get("does_not_apply_when"),
            guidance=value.get("guidance", ""),
            rationale=value.get("rationale", ""),
        )


@dataclass(frozen=True, slots=True)
class DecisionBody:
    """Bounded, revisioned Decision statement and authority metadata."""

    statement: str
    rationale: str
    source_type: DecisionSourceType
    authority: DecisionAuthority
    effective_at: float
    expires_at: float | None = None
    policy_anchor_path: str | None = None
    policy_anchor_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "statement", _text(self.statement, "statement", 4_000))
        object.__setattr__(self, "rationale", _text(self.rationale, "rationale", 4_000))
        object.__setattr__(self, "source_type", DecisionSourceType(self.source_type))
        object.__setattr__(self, "authority", DecisionAuthority(self.authority))
        object.__setattr__(self, "effective_at", _time(self.effective_at, "effective_at"))
        object.__setattr__(self, "expires_at", _time(self.expires_at, "expires_at", optional=True))
        if self.expires_at is not None and self.expires_at < self.effective_at:
            raise ValueError("expires_at must not be earlier than effective_at")
        if self.policy_anchor_path is None:
            object.__setattr__(self, "policy_anchor_path", None)
        else:
            path = PurePosixPath(_text(self.policy_anchor_path, "policy_anchor_path", 1_024))
            if path.is_absolute() or ".." in path.parts or "\\" in self.policy_anchor_path:
                raise ValueError("policy_anchor_path must be repository-relative")
            object.__setattr__(self, "policy_anchor_path", path.as_posix() or ".")
        object.__setattr__(self, "policy_anchor_hash", _optional_digest(self.policy_anchor_hash, "policy_anchor_hash"))
        if self.authority is DecisionAuthority.REPOSITORY_POLICY:
            if self.source_type is not DecisionSourceType.REPOSITORY_POLICY:
                raise ValueError("repository_policy authority requires repository_policy source_type")
            if not self.policy_anchor_path or not self.policy_anchor_hash:
                raise ValueError("repository_policy authority requires policy_anchor_path and policy_anchor_hash")
        if self.authority is DecisionAuthority.UNAPPROVED:
            if self.source_type is DecisionSourceType.REPOSITORY_POLICY:
                raise ValueError("repository_policy source_type cannot be unapproved")
        if self.authority is DecisionAuthority.USER:
            if self.source_type not in {DecisionSourceType.USER_TURN, DecisionSourceType.AGENT_PROPOSAL, DecisionSourceType.MANUAL_IMPORT}:
                raise ValueError("user authority requires a user-approved source type")

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "rationale": self.rationale,
            "source_type": self.source_type.value,
            "authority": self.authority.value,
            "effective_at": self.effective_at,
            "expires_at": self.expires_at,
            "policy_anchor_path": self.policy_anchor_path,
            "policy_anchor_hash": self.policy_anchor_hash,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DecisionBody":
        if not isinstance(value, Mapping):
            raise TypeError("decision body must be a mapping")
        allowed = {
            "statement", "rationale", "source_type", "authority", "effective_at",
            "expires_at", "policy_anchor_path", "policy_anchor_hash",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown decision body fields: {sorted(unknown)!r}")
        return cls(
            statement=value.get("statement", ""),
            rationale=value.get("rationale", ""),
            source_type=value.get("source_type", DecisionSourceType.MANUAL_IMPORT),
            authority=value.get("authority", DecisionAuthority.UNAPPROVED),
            effective_at=value.get("effective_at", 0.0),
            expires_at=value.get("expires_at"),
            policy_anchor_path=value.get("policy_anchor_path"),
            policy_anchor_hash=value.get("policy_anchor_hash"),
        )


@dataclass(frozen=True, slots=True, order=True)
class LessonTag:
    """Normalized tag owned by one exact lesson revision."""

    namespace: TagNamespace
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespace", TagNamespace(self.namespace))
        object.__setattr__(self, "value", _text(self.value, "tag", 160).casefold())


# Compatibility alias while callers migrate to the shared tag name.
ExperienceTag = LessonTag


@dataclass(frozen=True, slots=True)
class ScopeRef:
    """Opaque authorization identifiers attached to a lesson."""

    principal_id: str
    scope_type: ScopeType
    scope_id: str
    repository_id: str | None = None
    project_id: str | None = None

    def __post_init__(self) -> None:
        if self.principal_id != LOCAL_OWNER_PRINCIPAL:
            raise ValueError("MVP scope supports only local-owner")
        object.__setattr__(self, "scope_type", ScopeType(self.scope_type))
        for name in ("principal_id", "scope_id", "repository_id", "project_id"):
            value = getattr(self, name)
            object.__setattr__(self, name, _text(value, name, 256, optional=value is None))
        if self.scope_type is ScopeType.PROJECT and not (self.repository_id and self.project_id):
            raise ValueError("project scope requires repository_id and project_id")
        if self.scope_type is ScopeType.PROJECT and self.scope_id != self.project_id:
            raise ValueError("project scope_id must equal project_id")
        if self.scope_type is ScopeType.REPOSITORY:
            if not self.repository_id or self.project_id is not None:
                raise ValueError("repository scope requires only repository_id")
            if self.scope_id != self.repository_id:
                raise ValueError("repository scope_id must equal repository_id")
        if self.scope_type is ScopeType.PROFILE and (self.repository_id or self.project_id):
            raise ValueError("profile scope cannot carry repository/project ids")


@dataclass(frozen=True, slots=True)
class ScopePolicy:
    """Explicit project root plus independently denied-by-default consents."""

    principal_id: str
    repository_id: str
    project_id: str
    project_root_rel: str
    capture_allowed: bool = False
    recall_allowed: bool = False
    injection_allowed: bool = False
    reflection_allowed: bool = False
    max_egress_policy: EgressPolicy = EgressPolicy.LOCAL_ONLY
    updated_at: float = 0.0
    workspace_root: str | None = None

    def __post_init__(self) -> None:
        if self.principal_id != LOCAL_OWNER_PRINCIPAL:
            raise ValueError("MVP policy supports only local-owner")
        for name in ("principal_id", "repository_id", "project_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name, 256))
        rel = PurePosixPath(_text(self.project_root_rel, "project_root_rel", 1_024))
        if rel.is_absolute() or ".." in rel.parts or "\\" in self.project_root_rel:
            raise ValueError("project_root_rel must remain repository-relative")
        object.__setattr__(self, "project_root_rel", rel.as_posix() or ".")
        for name in (
            "capture_allowed",
            "recall_allowed",
            "injection_allowed",
            "reflection_allowed",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        object.__setattr__(self, "max_egress_policy", EgressPolicy(self.max_egress_policy))
        object.__setattr__(self, "updated_at", _time(self.updated_at, "updated_at"))
        if self.workspace_root is not None:
            root = Path(_text(self.workspace_root, "workspace_root", 4_096)).expanduser()
            if not root.is_absolute() or self.project_root_rel != ".":
                raise ValueError("workspace policy requires an absolute root and project_root_rel='.'")
            object.__setattr__(self, "workspace_root", str(root))

    @property
    def is_workspace(self) -> bool:
        return self.workspace_root is not None

    def to_dict(self) -> dict[str, Any]:
        """Return the exact store-facing policy shape."""

        return {
            "principal_id": self.principal_id,
            "repository_id": self.repository_id,
            "project_id": self.project_id,
            "project_root_rel": self.project_root_rel,
            "workspace_root": self.workspace_root,
            "capture_allowed": self.capture_allowed,
            "recall_allowed": self.recall_allowed,
            "injection_allowed": self.injection_allowed,
            "reflection_allowed": self.reflection_allowed,
            "max_egress_policy": self.max_egress_policy.value,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ScopePolicy":
        """Strictly validate a policy returned by storage."""

        allowed = {
            "principal_id", "repository_id", "project_id", "project_root_rel",
            "workspace_root", "capture_allowed", "recall_allowed",
            "injection_allowed", "reflection_allowed", "max_egress_policy",
            "updated_at",
        }
        # ``recall_allowed`` was added additively. Treat a pre-migration
        # mapping as an explicit denial so old callers fail closed.
        required = allowed - {"workspace_root", "recall_allowed"}
        unknown, missing = set(value) - allowed, required - set(value)
        if unknown or missing:
            raise ValueError(
                f"invalid scope policy fields; missing={sorted(missing)!r}, "
                f"unknown={sorted(unknown)!r}"
            )
        fields = {name: value.get(name) for name in allowed}
        fields["recall_allowed"] = value.get("recall_allowed", False)
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class LessonRevision:
    """Immutable, revision-specific lesson content and evidence provenance."""

    item_id: str
    revision: int
    title: str
    summary: str
    body: LessonBody
    created_at: float
    content_hash: str = ""
    confidence: float | None = None
    source_session_id: str | None = None
    source_turn_id: str | None = None
    source_work_id: str | None = None
    source_hash: str | None = None
    editor: str = "user"
    edit_reason: str | None = None
    producer_metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    tags: tuple[LessonTag, ...] = field(default_factory=tuple)
    last_validated_at: float | None = None
    review_after: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _text(self.item_id, "item_id", 256))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        object.__setattr__(self, "title", _text(self.title, "title", 240))
        object.__setattr__(self, "summary", _possibly_empty_text(self.summary, "summary", 2_000))
        if not isinstance(self.body, LessonBody):
            raise TypeError("body must be LessonBody")
        object.__setattr__(self, "created_at", _time(self.created_at, "created_at"))
        if self.confidence is not None:
            if (
                isinstance(self.confidence, bool)
                or not isinstance(self.confidence, (int, float))
                or not math.isfinite(self.confidence)
                or not 0 <= self.confidence <= 1
            ):
                raise ValueError("confidence must be between 0 and 1")
            object.__setattr__(self, "confidence", float(self.confidence))
        for name in ("source_session_id", "source_turn_id", "source_work_id", "edit_reason"):
            value = getattr(self, name)
            object.__setattr__(self, name, _text(value, name, 500, optional=True))
        object.__setattr__(self, "source_hash", _optional_digest(self.source_hash, "source_hash"))
        object.__setattr__(self, "editor", _text(self.editor, "editor", 256))
        producer_pairs: set[tuple[str, str]] = set()
        for pair in self.producer_metadata:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise TypeError("producer_metadata entries must be key/value pairs")
            producer_pairs.add(
                (
                    _text(pair[0], "producer_metadata key", 128),
                    _possibly_empty_text(pair[1], "producer_metadata value", 500),
                )
            )
        object.__setattr__(self, "producer_metadata", tuple(sorted(producer_pairs)))
        object.__setattr__(self, "tags", tuple(sorted(set(self.tags))))
        object.__setattr__(self, "last_validated_at", _time(self.last_validated_at, "last_validated_at", optional=True))
        object.__setattr__(self, "review_after", _time(self.review_after, "review_after", optional=True))
        digest = self.content_hash.strip().lower() or lesson_content_hash(
            self.body, title=self.title, summary=self.summary, tags=self.tags
        )
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("content_hash must be a SHA-256 hex digest")
        object.__setattr__(self, "content_hash", digest)


@dataclass(frozen=True, slots=True)
class DecisionRevision:
    """Immutable, revision-specific Decision content and provenance."""

    item_id: str
    revision: int
    title: str
    summary: str
    body: DecisionBody
    created_at: float
    content_hash: str = ""
    source_session_id: str | None = None
    source_turn_id: str | None = None
    source_work_id: str | None = None
    source_hash: str | None = None
    editor: str = "user"
    edit_reason: str | None = None
    producer_metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    tags: tuple[ExperienceTag, ...] = field(default_factory=tuple)
    last_validated_at: float | None = None
    review_after: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _text(self.item_id, "item_id", 256))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        object.__setattr__(self, "title", _text(self.title, "title", 240))
        object.__setattr__(self, "summary", _possibly_empty_text(self.summary, "summary", 2_000))
        if not isinstance(self.body, DecisionBody):
            raise TypeError("body must be DecisionBody")
        object.__setattr__(self, "created_at", _time(self.created_at, "created_at"))
        for name in ("source_session_id", "source_turn_id", "source_work_id", "edit_reason"):
            value = getattr(self, name)
            object.__setattr__(self, name, _text(value, name, 500, optional=True))
        object.__setattr__(self, "source_hash", _optional_digest(self.source_hash, "source_hash"))
        object.__setattr__(self, "editor", _text(self.editor, "editor", 256))
        producer_pairs: set[tuple[str, str]] = set()
        for pair in self.producer_metadata:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise TypeError("producer_metadata entries must be key/value pairs")
            producer_pairs.add(
                (
                    _text(pair[0], "producer_metadata key", 128),
                    _possibly_empty_text(pair[1], "producer_metadata value", 500),
                )
            )
        object.__setattr__(self, "producer_metadata", tuple(sorted(producer_pairs)))
        object.__setattr__(self, "tags", tuple(sorted(set(self.tags))))
        object.__setattr__(self, "last_validated_at", _time(self.last_validated_at, "last_validated_at", optional=True))
        object.__setattr__(self, "review_after", _time(self.review_after, "review_after", optional=True))
        digest = self.content_hash.strip().lower() or decision_content_hash(
            self.body,
            title=self.title,
            summary=self.summary,
            tags=self.tags,
        )
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("content_hash must be a SHA-256 hex digest")
        object.__setattr__(self, "content_hash", digest)


@dataclass(frozen=True, slots=True)
class Decision:
    """Current Decision metadata paired with its immutable revision."""

    id: str
    family_id: str
    status: DecisionStatus
    scope: ScopeRef
    sensitivity: Sensitivity
    egress_policy: EgressPolicy
    producer_trust_domain: str | None
    created_by: CreatedBy
    created_at: float
    updated_at: float
    revision: DecisionRevision
    deleted_at: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id", 256))
        object.__setattr__(self, "family_id", _text(self.family_id, "family_id", 256))
        object.__setattr__(self, "status", normalize_decision_status(self.status))
        object.__setattr__(self, "sensitivity", Sensitivity(self.sensitivity))
        object.__setattr__(self, "egress_policy", EgressPolicy(self.egress_policy))
        object.__setattr__(self, "created_by", CreatedBy(self.created_by))
        object.__setattr__(self, "producer_trust_domain", _optional_trust_domain(self.producer_trust_domain))
        object.__setattr__(self, "created_at", _time(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _time(self.updated_at, "updated_at"))
        object.__setattr__(self, "deleted_at", _time(self.deleted_at, "deleted_at", optional=True))
        if not isinstance(self.scope, ScopeRef) or not isinstance(self.revision, DecisionRevision):
            raise TypeError("scope/revision have invalid types")
        if self.revision.item_id != self.id or self.updated_at < self.created_at:
            raise ValueError("decision revision or timestamps are inconsistent")
        if self.status is DecisionStatus.ACTIVE and self.revision.body.authority is DecisionAuthority.UNAPPROVED:
            raise ValueError("active decisions require user or repository_policy authority")

    @property
    def current_revision(self) -> int:
        return self.revision.revision


@dataclass(frozen=True, slots=True)
class Lesson:
    """Current item metadata paired with its current immutable revision."""

    id: str
    family_id: str
    status: LessonStatus
    scope: ScopeRef
    sensitivity: Sensitivity
    egress_policy: EgressPolicy
    producer_trust_domain: str | None
    created_by: CreatedBy
    created_at: float
    updated_at: float
    revision: LessonRevision
    deleted_at: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id", 256))
        object.__setattr__(self, "family_id", _text(self.family_id, "family_id", 256))
        object.__setattr__(self, "status", normalize_lesson_status(self.status))
        object.__setattr__(self, "sensitivity", Sensitivity(self.sensitivity))
        object.__setattr__(self, "egress_policy", EgressPolicy(self.egress_policy))
        object.__setattr__(self, "created_by", CreatedBy(self.created_by))
        object.__setattr__(self, "producer_trust_domain", _optional_trust_domain(self.producer_trust_domain))
        object.__setattr__(self, "created_at", _time(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _time(self.updated_at, "updated_at"))
        object.__setattr__(self, "deleted_at", _time(self.deleted_at, "deleted_at", optional=True))
        if not isinstance(self.scope, ScopeRef) or not isinstance(self.revision, LessonRevision):
            raise TypeError("scope/revision have invalid types")
        if self.revision.item_id != self.id or self.updated_at < self.created_at:
            raise ValueError("lesson revision or timestamps are inconsistent")

    @property
    def current_revision(self) -> int:
        return self.revision.revision


def _terms(values: Iterable[str], name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    return tuple(sorted({_text(value, name, 160).casefold() for value in values}))


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """Already-sanitized text and metadata for one authorized project scope."""

    scope: ScopeRef
    query_text: str
    provider_trust_domain: str | None
    provider_is_local: bool = False
    task_types: tuple[str, ...] = field(default_factory=tuple)
    technologies: tuple[str, ...] = field(default_factory=tuple)
    entities: tuple[str, ...] = field(default_factory=tuple)
    failure_fingerprints: tuple[str, ...] = field(default_factory=tuple)
    limit: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ScopeRef):
            raise TypeError("scope must be ScopeRef")
        object.__setattr__(self, "query_text", _text(self.query_text, "query_text", 4_000, optional=True) or "")
        object.__setattr__(self, "provider_trust_domain", _optional_trust_domain(self.provider_trust_domain))
        if not isinstance(self.provider_is_local, bool):
            raise TypeError("provider_is_local must be bool")
        if not self.provider_is_local and not self.provider_trust_domain:
            raise ValueError("remote retrieval requires provider_trust_domain")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 50:
            raise ValueError("limit must be an integer from 1 through 50")
        for name in ("task_types", "technologies", "entities", "failure_fingerprints"):
            object.__setattr__(self, name, _terms(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class RetrievalMatch:
    """One active result and its deterministic, human-readable explanation."""

    item_id: str
    item_revision: int
    title: str
    summary: str
    body: LessonBody
    rank: int
    score: float
    match_reasons: tuple[str, ...]
    confidence: float | None = None
    tags: tuple[LessonTag, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _text(self.item_id, "item_id", 256))
        if isinstance(self.item_revision, bool) or not isinstance(self.item_revision, int) or self.item_revision < 1:
            raise ValueError("item_revision must be positive")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise ValueError("rank must be positive")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) or not math.isfinite(self.score):
            raise ValueError("score must be finite")
        if not isinstance(self.body, LessonBody):
            raise TypeError("body must be LessonBody")
        object.__setattr__(self, "title", _text(self.title, "title", 240))
        object.__setattr__(self, "summary", _possibly_empty_text(self.summary, "summary", 2_000))
        object.__setattr__(self, "score", float(self.score))
        reasons = tuple(dict.fromkeys(_text(value, "match reason", 500) for value in self.match_reasons))
        if not reasons:
            raise ValueError("match_reasons must not be empty")
        object.__setattr__(self, "match_reasons", reasons)
        object.__setattr__(self, "tags", tuple(sorted(set(self.tags))))
        if self.confidence is not None:
            if (
                isinstance(self.confidence, bool)
                or not isinstance(self.confidence, (int, float))
                or not math.isfinite(self.confidence)
                or not 0 <= self.confidence <= 1
            ):
                raise ValueError("confidence must be between 0 and 1")
            object.__setattr__(self, "confidence", float(self.confidence))



@dataclass(frozen=True, slots=True)
class DecisionMatch:
    """One active Decision result and its deterministic explanation."""

    item_id: str
    family_id: str
    item_revision: int
    title: str
    summary: str
    body: DecisionBody
    authority: str
    scope_type: str
    scope_id: str
    repository_id: str | None
    project_id: str | None
    source_session_id: str | None
    source_turn_id: str | None
    source_work_id: str | None
    rank: int
    score: float
    match_reasons: tuple[str, ...]
    confidence: float | None = None
    tags: tuple[LessonTag, ...] = field(default_factory=tuple)
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _text(self.item_id, "item_id", 256))
        object.__setattr__(self, "family_id", _text(self.family_id, "family_id", 256))
        if (
            isinstance(self.item_revision, bool)
            or not isinstance(self.item_revision, int)
            or self.item_revision < 1
        ):
            raise ValueError("item_revision must be positive")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise ValueError("rank must be positive")
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not math.isfinite(self.score)
        ):
            raise ValueError("score must be finite")
        if not isinstance(self.body, DecisionBody):
            raise TypeError("body must be DecisionBody")
        object.__setattr__(self, "authority", _text(self.authority, "authority", 64))
        if self.authority not in {"user", "repository_policy"}:
            raise ValueError("decision match authority must be user or repository_policy")
        object.__setattr__(self, "scope_type", ScopeType(self.scope_type))
        object.__setattr__(self, "scope_id", _text(self.scope_id, "scope_id", 256))
        object.__setattr__(
            self,
            "repository_id",
            _text(self.repository_id, "repository_id", 256, optional=True),
        )
        object.__setattr__(
            self,
            "project_id",
            _text(self.project_id, "project_id", 256, optional=True),
        )
        for name in ("source_session_id", "source_turn_id", "source_work_id"):
            object.__setattr__(
                self,
                name,
                _text(getattr(self, name), name, 256, optional=True),
            )
        object.__setattr__(self, "title", _text(self.title, "title", 240))
        object.__setattr__(self, "summary", _possibly_empty_text(self.summary, "summary", 2_000))
        object.__setattr__(self, "score", float(self.score))
        reasons = tuple(
            dict.fromkeys(_text(value, "match reason", 500) for value in self.match_reasons)
        )
        if not reasons:
            raise ValueError("match_reasons must not be empty")
        object.__setattr__(self, "match_reasons", reasons)
        object.__setattr__(self, "tags", tuple(sorted(set(self.tags))))
        if self.confidence is not None:
            if (
                isinstance(self.confidence, bool)
                or not isinstance(self.confidence, (int, float))
                or not math.isfinite(self.confidence)
                or not 0 <= self.confidence <= 1
            ):
                raise ValueError("confidence must be between 0 and 1")
            object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "updated_at", _time(self.updated_at, "updated_at"))


@dataclass(frozen=True, slots=True)
class RetrievalDiagnostic:
    """Text-free metadata for one retrieval attempt."""

    id: str
    turn_id: str
    work_id: str
    principal_id: str
    repository_id: str
    project_id: str
    task_signature_hash: str
    provider_trust_domain: str | None
    created_at: float

    def __post_init__(self) -> None:
        for name in ("id", "turn_id", "work_id", "principal_id", "repository_id", "project_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name, 256))
        if self.principal_id != LOCAL_OWNER_PRINCIPAL:
            raise ValueError("retrieval principal must be local-owner")
        digest = self.task_signature_hash.strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("task_signature_hash must be a SHA-256 hex digest")
        object.__setattr__(self, "task_signature_hash", digest)
        object.__setattr__(self, "provider_trust_domain", _optional_trust_domain(self.provider_trust_domain))
        object.__setattr__(self, "created_at", _time(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class RetrievalItemDiagnostic:
    """Text-free metadata for one retrieved immutable revision."""

    retrieval_id: str
    item_id: str
    item_revision: int
    rank: int
    score: float
    match_reasons: tuple[str, ...]
    disposition: RetrievalDisposition = RetrievalDisposition.RETRIEVED

    def __post_init__(self) -> None:
        for name in ("retrieval_id", "item_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name, 256))
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in (self.item_revision, self.rank)):
            raise ValueError("item_revision and rank must be positive integers")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) or not math.isfinite(self.score):
            raise ValueError("score must be finite")
        object.__setattr__(self, "score", float(self.score))
        reasons = tuple(
            dict.fromkeys(
                _text(value, "match reason", 500) for value in self.match_reasons
            )
        )
        if not reasons:
            raise ValueError("match_reasons must not be empty")
        object.__setattr__(self, "match_reasons", reasons)
        object.__setattr__(self, "disposition", RetrievalDisposition(self.disposition))


def decision_content_hash(
    body: DecisionBody,
    *,
    title: str = "",
    summary: str = "",
    tags: Iterable[ExperienceTag] = (),
    scope: ScopeRef | None = None,
) -> str:
    """Return deterministic, optionally scope-aware Decision content identity."""

    payload: dict[str, Any] = {
        "kind": "decision",
        "title": title.strip(),
        "summary": summary.strip(),
        "body": body.to_dict(),
        "tags": sorted((tag.namespace.value, tag.value) for tag in tags),
    }
    if scope:
        payload["scope"] = [
            scope.principal_id,
            scope.scope_type.value,
            scope.scope_id,
            scope.repository_id,
            scope.project_id,
        ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def lesson_content_hash(
    body: LessonBody,
    *,
    title: str = "",
    summary: str = "",
    tags: Iterable[LessonTag] = (),
    scope: ScopeRef | None = None,
) -> str:
    """Return deterministic, optionally scope-aware lesson content identity."""

    payload: dict[str, Any] = {
        "kind": "lesson",
        "title": title.strip(),
        "summary": summary.strip(),
        "body": body.to_dict(),
        "tags": sorted((tag.namespace.value, tag.value) for tag in tags),
    }
    if scope:
        payload["scope"] = [
            scope.principal_id,
            scope.scope_type.value,
            scope.scope_id,
            scope.repository_id,
            scope.project_id,
        ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()

"""Public core API for Marlow Work Experience validation.

The package is intentionally integration-neutral: callers explicitly resolve
profile storage and scope, then opt into retrieval.  Nothing here captures a
conversation, invokes a model, or mutates the agent loop.
"""

from agent.experience.models import (
    CreatedBy,
    Decision,
    DecisionAuthority,
    DecisionBody,
    DecisionMatch,
    DecisionRevision,
    DecisionSourceType,
    DecisionStatus,
    EgressPolicy,
    Lesson,
    LessonBody,
    LessonRevision,
    LessonStatus,
    LessonTag,
    RetrievalDiagnostic,
    RetrievalDisposition,
    RetrievalItemDiagnostic,
    RetrievalMatch,
    RetrievalQuery,
    ScopePolicy,
    ScopeRef,
    ScopeType,
    Sensitivity,
    TagNamespace,
)
from agent.experience.anchors import AnchorValidationResult, validate_repository_anchor
from agent.experience.authority import (
    DecisionTurnAuthority,
    decision_authority_from_text,
    require_scope_not_broadened,
    scope_is_equal_or_narrower,
)
from agent.experience.safety import (
    ExperienceEgressError,
    ExperienceSafety,
    ExperienceSafetyError,
    ExperienceThreatError,
)
from agent.experience.scope import (
    AmbiguousScopeError,
    GitDiscoveryError,
    InvalidScopePolicyError,
    ResolvedScope,
    ScopeNotConfiguredError,
    ScopeResolutionError,
    ScopeResolver,
)
from agent.experience.service import ExperienceService, RetrievalResult
from agent.experience.store import ExperienceStore

__all__ = [
    "AmbiguousScopeError",
    "AnchorValidationResult",
    "CreatedBy",
    "Decision",
    "DecisionAuthority",
    "DecisionBody",
    "DecisionMatch",
    "DecisionRevision",
    "DecisionSourceType",
    "DecisionTurnAuthority",
    "DecisionStatus",
    "EgressPolicy",
    "ExperienceEgressError",
    "ExperienceSafety",
    "ExperienceSafetyError",
    "ExperienceService",
    "ExperienceStore",
    "ExperienceThreatError",
    "GitDiscoveryError",
    "InvalidScopePolicyError",
    "Lesson",
    "LessonBody",
    "LessonRevision",
    "LessonStatus",
    "LessonTag",
    "ResolvedScope",
    "RetrievalDiagnostic",
    "RetrievalDisposition",
    "RetrievalItemDiagnostic",
    "RetrievalMatch",
    "RetrievalQuery",
    "RetrievalResult",
    "ScopeNotConfiguredError",
    "ScopePolicy",
    "ScopeRef",
    "ScopeResolutionError",
    "ScopeResolver",
    "ScopeType",
    "Sensitivity",
    "TagNamespace",
    "decision_authority_from_text",
    "require_scope_not_broadened",
    "scope_is_equal_or_narrower",
    "validate_repository_anchor",
]

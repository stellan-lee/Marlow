"""Host-owned authority checks for Decision Memory."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


_DIRECT_DECISION_GRANT_RE = re.compile(
    r"(记住|以后都|从现在开始|我们决定|把\s*.*\s*作为默认|不要再|remember\s+(that|this)|from\s+now\s+on|we\s+decided)",
    re.IGNORECASE,
)
_APPROVE_DECISION_RE = re.compile(
    r"(?:批准|approve)\s+(decision_[A-Za-z0-9._:-]{1,512})",
    re.IGNORECASE,
)
_SUPERSEDE_DECISION_RE = re.compile(
    r"(?:替换|supersede|replace)\s+(decision_[A-Za-z0-9._:-]{1,512})",
    re.IGNORECASE,
)
_REVOKE_DECISION_RE = re.compile(
    r"(?:撤销|revoke)\s+(decision_[A-Za-z0-9._:-]{1,512})",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DecisionTurnAuthority:
    """Trusted current-turn authority supplied by the host, not the model."""

    source_turn_id: str
    source_session_id: str
    raw_user_text_hash: str
    explicit_remember_grant: bool
    approved_item_ids: tuple[str, ...] = ()
    supersede_target_ids: tuple[str, ...] = ()
    revoke_target_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("source_turn_id", "source_session_id", "raw_user_text_hash"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")
        if len(self.raw_user_text_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.raw_user_text_hash
        ):
            raise ValueError("raw_user_text_hash must be a SHA-256 hex digest")
        object.__setattr__(self, "approved_item_ids", tuple(self.approved_item_ids))
        object.__setattr__(self, "supersede_target_ids", tuple(self.supersede_target_ids))
        object.__setattr__(self, "revoke_target_ids", tuple(self.revoke_target_ids))

    def approves(self, item_id: str) -> bool:
        return item_id in self.approved_item_ids

    def supersedes(self, item_id: str) -> bool:
        return item_id in self.supersede_target_ids

    def revokes(self, item_id: str) -> bool:
        return item_id in self.revoke_target_ids

    def matches_source_hash(self, source_hash: str | None) -> bool:
        return bool(source_hash) and source_hash.casefold() == self.raw_user_text_hash

    def approves_decision_source(self, item_id: str, source_hash: str | None) -> bool:
        return self.approves(item_id) or self.matches_source_hash(source_hash)


def decision_authority_from_text(
    source_turn_id: str,
    source_session_id: str,
    raw_user_text: str,
    *,
    approved_item_ids: tuple[str, ...] = (),
    supersede_target_ids: tuple[str, ...] = (),
    revoke_target_ids: tuple[str, ...] = (),
) -> DecisionTurnAuthority:
    """Build host-owned authority from the authenticated current turn.

    Recognition is intentionally conservative: ambiguous language does not
    create an approval grant. The returned object hashes the raw text and keeps
    approval/supersession/revocation lists separate from model payload.
    """

    if not isinstance(raw_user_text, str):
        raise TypeError("raw_user_text must be a string")
    raw_user_text_hash = hashlib.sha256(raw_user_text.encode("utf-8")).hexdigest()
    approved = set(approved_item_ids)
    supersede = set(supersede_target_ids)
    revoke = set(revoke_target_ids)
    for regex, target in (
        (_APPROVE_DECISION_RE, approved),
        (_SUPERSEDE_DECISION_RE, supersede),
        (_REVOKE_DECISION_RE, revoke),
    ):
        target.update(match.group(1) for match in regex.finditer(raw_user_text))
    return DecisionTurnAuthority(
        source_turn_id=source_turn_id,
        source_session_id=source_session_id,
        raw_user_text_hash=raw_user_text_hash,
        explicit_remember_grant=bool(_DIRECT_DECISION_GRANT_RE.search(raw_user_text)),
        approved_item_ids=tuple(sorted(approved)),
        supersede_target_ids=tuple(sorted(supersede)),
        revoke_target_ids=tuple(sorted(revoke)),
    )


__all__ = [
    "DecisionTurnAuthority",
    "decision_authority_from_text",
    "require_scope_not_broadened",
    "scope_is_equal_or_narrower",
]


_SCOPE_RANK = {"project": 0, "repository": 1, "profile": 2}


def scope_is_equal_or_narrower(
    old_scope_type: str,
    old_scope_id: str,
    old_repository_id: str | None,
    old_project_id: str | None,
    new_scope_type: str,
    new_scope_id: str,
    new_repository_id: str | None,
    new_project_id: str | None,
) -> bool:
    """Return whether a replacement Decision scope is equal or narrower.

    Project scope is narrower than repository scope; profile scope is the
    broadest scope and may only remain profile-scoped with the same id.
    """

    old_type = str(old_scope_type).strip().lower()
    new_type = str(new_scope_type).strip().lower()
    if old_type not in _SCOPE_RANK or new_type not in _SCOPE_RANK:
        return False
    if _SCOPE_RANK[new_type] > _SCOPE_RANK[old_type]:
        return False
    if old_type == "project" and new_type == "project":
        return (
            old_scope_id == new_scope_id
            and old_repository_id == new_repository_id
            and old_project_id == new_project_id
        )
    if old_type == "repository" and new_type == "project":
        return new_project_id is not None and new_repository_id == old_repository_id
    if old_type == "repository" and new_type == "repository":
        return old_scope_id == new_scope_id and old_repository_id == new_repository_id
    if old_type == "profile" and new_type == "profile":
        return old_scope_id == new_scope_id
    if old_type == "profile" and new_type in {"repository", "project"}:
        return new_repository_id is not None and (
            new_type != "project" or new_project_id is not None
        )
    return False


def require_scope_not_broadened(
    old_scope_type: str,
    old_scope_id: str,
    old_repository_id: str | None,
    old_project_id: str | None,
    new_scope_type: str,
    new_scope_id: str,
    new_repository_id: str | None,
    new_project_id: str | None,
) -> None:
    if not scope_is_equal_or_narrower(
        old_scope_type,
        old_scope_id,
        old_repository_id,
        old_project_id,
        new_scope_type,
        new_scope_id,
        new_repository_id,
        new_project_id,
    ):
        raise ValueError("replacement decision scope cannot broaden user authority")

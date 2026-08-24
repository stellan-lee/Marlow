from __future__ import annotations

import pytest

from agent.experience.authority import (
    decision_authority_from_text,
    require_scope_not_broadened,
    scope_is_equal_or_narrower,
)


def test_decision_authority_from_text_extracts_exact_intents() -> None:
    authority = decision_authority_from_text(
        "turn-1",
        "session-1",
        "remember this, approve decision_old, supersede decision_replaced, revoke decision_bad",
    )

    assert authority.source_turn_id == "turn-1"
    assert authority.source_session_id == "session-1"
    assert authority.explicit_remember_grant is True
    assert authority.approves("decision_old")
    assert authority.supersedes("decision_replaced")
    assert authority.revokes("decision_bad")


def test_decision_authority_from_text_is_conservative() -> None:
    authority = decision_authority_from_text(
        "turn-1",
        "session-1",
        "The agent suggested remembering a preference.",
    )

    assert authority.explicit_remember_grant is False
    assert authority.approved_item_ids == ()
    assert authority.supersede_target_ids == ()
    assert authority.revoke_target_ids == ()


def test_scope_narrowing_prevents_broadening() -> None:
    assert scope_is_equal_or_narrower(
        "repository",
        "repo",
        "repo",
        None,
        "project",
        "project",
        "repo",
        "project",
    )
    assert not scope_is_equal_or_narrower(
        "project",
        "project",
        "repo",
        "project",
        "repository",
        "repo",
        "repo",
        None,
    )
    require_scope_not_broadened(
        "repository",
        "repo",
        "repo",
        None,
        "project",
        "project",
        "repo",
        "project",
    )


def test_scope_narrowing_rejects_broadening() -> None:
    with pytest.raises(ValueError, match="cannot broaden"):
        require_scope_not_broadened(
            "project",
            "project",
            "repo",
            "project",
            "repository",
            "repo",
            "repo",
            None,
        )

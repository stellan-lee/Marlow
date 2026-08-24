"""Post-turn integration tests for structured memory cards (PR4).

Exercises ``AIAgent._sync_external_memory_for_turn`` with the structured
card path on/off, asserting the core invariants: normal ``sync_all`` is
unchanged, structured-card sync is gated and fail-open, no prefetch is
queued, and the current API user message is never modified.
"""
from unittest.mock import MagicMock

import pytest


DECISION_TURN = (
    "We decided to use compact inline approval cards. This is final.",
    "Got it.",
)


def _bare_agent(*, structured_enabled=False):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._memory_manager = MagicMock()
    agent.session_id = "sess-card-1"
    agent._memory_post_turn_prefetch_enabled = False
    agent._memory_structured_cards_enabled = structured_enabled
    agent._memory_structured_cards_max_per_turn = 5
    agent._memory_structured_cards_max_chars = 2500
    agent._memory_structured_cards_fallback_sync_turn_enabled = True
    agent._memory_structured_conflict_resolution_enabled = False
    agent._memory_structured_conflict_require_explicit_override = True
    agent._memory_structured_conflict_min_entity_overlap = 1
    agent._memory_structured_conflict_max_candidates = 8
    agent._decision_turn = None
    return agent


def test_disabled_does_not_extract_or_sync_cards():
    agent = _bare_agent(structured_enabled=False)
    agent._sync_external_memory_for_turn(
        original_user_message=DECISION_TURN[0],
        final_response=DECISION_TURN[1],
        interrupted=False,
    )
    agent._memory_manager.sync_all.assert_called_once()
    agent._memory_manager.sync_structured_cards_all.assert_not_called()


def test_enabled_with_signal_syncs_structured_cards():
    agent = _bare_agent(structured_enabled=True)
    agent._sync_external_memory_for_turn(
        original_user_message=DECISION_TURN[0],
        final_response=DECISION_TURN[1],
        interrupted=False,
    )
    agent._memory_manager.sync_structured_cards_all.assert_called_once()
    cards = agent._memory_manager.sync_structured_cards_all.call_args.args[0]
    assert cards  # at least one card extracted
    kwargs = agent._memory_manager.sync_structured_cards_all.call_args.kwargs
    assert kwargs["session_id"] == "sess-card-1"
    assert kwargs["fallback_sync_turn_enabled"] is True


def test_normal_sync_all_still_runs_when_enabled():
    agent = _bare_agent(structured_enabled=True)
    agent._sync_external_memory_for_turn(
        original_user_message=DECISION_TURN[0],
        final_response=DECISION_TURN[1],
        interrupted=False,
    )
    agent._memory_manager.sync_all.assert_called_once_with(
        DECISION_TURN[0], DECISION_TURN[1], session_id="sess-card-1"
    )


def test_card_sync_does_not_queue_prefetch():
    agent = _bare_agent(structured_enabled=True)
    agent._sync_external_memory_for_turn(
        original_user_message=DECISION_TURN[0],
        final_response=DECISION_TURN[1],
        interrupted=False,
    )
    agent._memory_manager.queue_prefetch_all.assert_not_called()


def test_low_signal_turn_writes_no_cards():
    agent = _bare_agent(structured_enabled=True)
    agent._sync_external_memory_for_turn(
        original_user_message="what's the weather?",
        final_response="It's sunny and 22 degrees today.",
        interrupted=False,
    )
    agent._memory_manager.sync_all.assert_called_once()
    agent._memory_manager.sync_structured_cards_all.assert_not_called()


def test_interrupted_turn_skips_card_sync():
    agent = _bare_agent(structured_enabled=True)
    agent._sync_external_memory_for_turn(
        original_user_message=DECISION_TURN[0],
        final_response=DECISION_TURN[1],
        interrupted=True,
    )
    agent._memory_manager.sync_all.assert_not_called()
    agent._memory_manager.sync_structured_cards_all.assert_not_called()


def test_extraction_failure_does_not_block_turn(monkeypatch):
    agent = _bare_agent(structured_enabled=True)

    def boom(*a, **k):
        raise RuntimeError("extractor exploded")

    monkeypatch.setattr("agent.memory_cards.extract_memory_cards", boom)

    # Must not raise; normal sync_all already happened.
    agent._sync_external_memory_for_turn(
        original_user_message=DECISION_TURN[0],
        final_response=DECISION_TURN[1],
        interrupted=False,
    )
    agent._memory_manager.sync_all.assert_called_once()
    agent._memory_manager.sync_structured_cards_all.assert_not_called()


def test_card_sync_failure_does_not_block_turn():
    agent = _bare_agent(structured_enabled=True)
    agent._memory_manager.sync_structured_cards_all.side_effect = RuntimeError(
        "provider down"
    )

    # Must not raise.
    agent._sync_external_memory_for_turn(
        original_user_message=DECISION_TURN[0],
        final_response=DECISION_TURN[1],
        interrupted=False,
    )
    agent._memory_manager.sync_all.assert_called_once()
    agent._memory_manager.sync_structured_cards_all.assert_called_once()


def test_user_message_not_modified_by_cards():
    agent = _bare_agent(structured_enabled=True)
    user = DECISION_TURN[0]
    agent._sync_external_memory_for_turn(
        original_user_message=user,
        final_response=DECISION_TURN[1],
        interrupted=False,
    )
    # sync_all received the verbatim original user message.
    assert agent._memory_manager.sync_all.call_args.args[0] == user
    assert user == DECISION_TURN[0]


# ---------------------------------------------------------------------------
# PR5: supersession / conflict resolution post-turn integration
# ---------------------------------------------------------------------------

from agent.memory_cards import MemoryCard, format_memory_cards_for_sync  # noqa: E402

# A turn whose assistant response is a single-sentence decision carrying both
# a decision keyword and explicit override language, with a quoted entity that
# cleanly matches the prior card below.
OVERRIDE_TURN = (
    'Final decision: use one row instead of two rows for "ApprovalButtons".',
    "Got it.",
)


def _prior_decision_text(card_id="OLDPRIOR1", summary="Use two rows for ApprovalButtons."):
    return format_memory_cards_for_sync(
        [
            MemoryCard(
                card_id=card_id, type="decision", status="active",
                title="ApprovalButtons", summary=summary,
                entities=["ApprovalButtons"], source_session_id="sess-card-1",
            )
        ]
    )


def _conflict_agent(*, resolution_enabled=True):
    agent = _bare_agent(structured_enabled=True)
    agent._memory_structured_conflict_resolution_enabled = resolution_enabled
    agent._memory_structured_conflict_require_explicit_override = True
    agent._memory_structured_conflict_min_entity_overlap = 1
    agent._memory_structured_conflict_max_candidates = 8
    return agent


def test_resolution_off_does_not_look_up_candidates():
    agent = _conflict_agent(resolution_enabled=False)
    agent._sync_external_memory_for_turn(
        original_user_message=OVERRIDE_TURN[0],
        final_response=OVERRIDE_TURN[1],
        interrupted=False,
    )
    agent._memory_manager.lookup_structured_card_candidates.assert_not_called()
    agent._memory_manager.sync_structured_cards_all.assert_called_once()


def test_resolution_on_looks_up_then_writes_new_and_marker():
    agent = _conflict_agent()
    agent._memory_manager.lookup_structured_card_candidates.return_value = (
        _prior_decision_text()
    )
    agent._sync_external_memory_for_turn(
        original_user_message=OVERRIDE_TURN[0],
        final_response=OVERRIDE_TURN[1],
        interrupted=False,
    )
    # Candidate lookup happened (before the write).
    assert agent._memory_manager.lookup_structured_card_candidates.called
    agent._memory_manager.sync_structured_cards_all.assert_called_once()
    written = agent._memory_manager.sync_structured_cards_all.call_args.args[0]
    statuses = [c.status for c in written]
    assert "active" in statuses
    assert "superseded" in statuses  # a marker card was appended
    marker = next(c for c in written if c.status == "superseded")
    assert marker.superseded_by  # points at the new card id
    active = next(c for c in written if c.status == "active")
    assert "OLDPRIOR1" in active.supersedes


def test_resolution_does_not_queue_prefetch():
    agent = _conflict_agent()
    agent._memory_manager.lookup_structured_card_candidates.return_value = (
        _prior_decision_text()
    )
    agent._sync_external_memory_for_turn(
        original_user_message=OVERRIDE_TURN[0],
        final_response=OVERRIDE_TURN[1],
        interrupted=False,
    )
    agent._memory_manager.queue_prefetch_all.assert_not_called()


def test_candidate_lookup_failure_fails_open_and_writes_new_card():
    agent = _conflict_agent()
    agent._memory_manager.lookup_structured_card_candidates.side_effect = (
        RuntimeError("provider down")
    )
    # Must not raise; new cards still written without supersession metadata.
    agent._sync_external_memory_for_turn(
        original_user_message=OVERRIDE_TURN[0],
        final_response=OVERRIDE_TURN[1],
        interrupted=False,
    )
    agent._memory_manager.sync_structured_cards_all.assert_called_once()
    written = agent._memory_manager.sync_structured_cards_all.call_args.args[0]
    assert all(c.status != "superseded" for c in written)  # no markers
    assert all(not c.supersedes for c in written)


def test_resolution_logs_redact_raw_candidate_text(caplog):
    agent = _conflict_agent()
    leaky = _prior_decision_text(summary="LEAK_CANDIDATE_SUMMARY two-row.")
    agent._memory_manager.lookup_structured_card_candidates.return_value = leaky

    with caplog.at_level("DEBUG", logger="run_agent"):
        agent._sync_external_memory_for_turn(
            original_user_message=OVERRIDE_TURN[0],
            final_response=OVERRIDE_TURN[1],
            interrupted=False,
        )
    assert "LEAK_CANDIDATE_SUMMARY" not in caplog.text
    assert "structured-memory-cards" not in caplog.text


def test_resolution_failure_fails_open(monkeypatch):
    agent = _conflict_agent()

    def boom(*a, **k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr("agent.memory_card_conflicts.resolve_card_conflicts", boom)
    agent._memory_manager.lookup_structured_card_candidates.return_value = (
        _prior_decision_text()
    )
    # Must not raise; new cards still written (fail-open returns original cards).
    agent._sync_external_memory_for_turn(
        original_user_message=OVERRIDE_TURN[0],
        final_response=OVERRIDE_TURN[1],
        interrupted=False,
    )
    agent._memory_manager.sync_structured_cards_all.assert_called_once()

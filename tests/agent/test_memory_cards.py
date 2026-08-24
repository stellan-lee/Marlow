"""Tests for deterministic structured memory cards (PR4).

Covers extraction heuristics (EN + ZH), sanitization, bounding/caps,
determinism, dedupe, and the search-friendly sync formatting.
"""

from agent.memory_cards import (
    MemoryCard,
    MemoryCardStatus,
    MemoryCardType,
    extract_memory_cards,
    format_memory_cards_for_sync,
)


# ---------------------------------------------------------------------------
# Input robustness
# ---------------------------------------------------------------------------


def test_none_and_non_string_inputs_do_not_crash():
    assert extract_memory_cards(None, None) == []
    assert extract_memory_cards({"content": "x"}, 12345) == []
    assert extract_memory_cards([], object()) == []


def test_empty_strings_return_no_cards():
    assert extract_memory_cards("", "") == []


def test_generic_conversation_has_no_durable_signal():
    cards = extract_memory_cards(
        "what's the weather today?",
        "It's sunny and around 22 degrees this afternoon.",
    )
    assert cards == []


# ---------------------------------------------------------------------------
# Per-type extraction (English + Chinese)
# ---------------------------------------------------------------------------


def _types(cards):
    return {c.type for c in cards}


def test_decision_extraction_english():
    cards = extract_memory_cards(
        "We decided to use compact inline approval cards with Approve/Reject "
        "buttons. This is final.",
        "Got it.",
    )
    assert MemoryCardType.DECISION in _types(cards)
    decision = next(c for c in cards if c.type == MemoryCardType.DECISION)
    assert decision.status == MemoryCardStatus.ACTIVE


def test_decision_extraction_chinese():
    cards = extract_memory_cards(
        "我们最终决定就用紧凑型内联审批卡片。",
        "好的。",
    )
    assert MemoryCardType.DECISION in _types(cards)


def test_preference_extraction():
    cards = extract_memory_cards(
        "I prefer dark mode and a compact layout for the dashboard.",
        "Got it, noting that down.",
    )
    assert MemoryCardType.PREFERENCE in _types(cards)


def test_preference_extraction_chinese():
    cards = extract_memory_cards(
        "我喜欢深色风格的界面。",
        "好的。",
    )
    assert MemoryCardType.PREFERENCE in _types(cards)


def test_todo_extraction():
    cards = extract_memory_cards(
        "Next step is to wire the cache invalidation and add tests.",
        "ok",
    )
    assert MemoryCardType.TODO in _types(cards)


def test_constraint_extraction():
    cards = extract_memory_cards(
        "The worker must not log raw user text and cannot block the turn.",
        "ok",
    )
    assert MemoryCardType.CONSTRAINT in _types(cards)


def test_constraint_extraction_chinese():
    cards = extract_memory_cards(
        "必须保证不能阻塞用户回合。",
        "好的。",
    )
    assert MemoryCardType.CONSTRAINT in _types(cards)


def test_implementation_detail_extraction():
    cards = extract_memory_cards(
        "how does recall work?",
        "We implement the cache keyed by query and session in the prefetch "
        "queue path.",
    )
    assert MemoryCardType.IMPLEMENTATION_DETAIL in _types(cards)


def test_dotted_identifiers_and_paths_are_not_split_into_fragments():
    # Regression: a `.` inside a file path / dotted name / URL must not be
    # treated as a sentence boundary (it would spawn garbled fragment cards).
    cards = extract_memory_cards(
        "We decided to use queue_prefetch_all in agent/memory_manager.py and "
        "the MemoryManager class.",
        "ok",
    )
    decisions = [c for c in cards if c.type == MemoryCardType.DECISION]
    assert len(decisions) == 1
    summary = decisions[0].summary
    assert "agent/memory_manager.py" in summary
    # No spurious fragment card whose summary starts mid-token.
    assert not any(c.summary.startswith("py ") for c in cards)


def test_url_with_dots_not_fragmented():
    cards = extract_memory_cards(
        "We decided to call api.example.com for the cache lookup. This is final.",
        "ok",
    )
    decisions = [c for c in cards if c.type == MemoryCardType.DECISION]
    assert decisions
    assert any("api.example.com" in c.summary for c in decisions)


def test_implementation_detail_natural_phrasing():
    # Regression: "Implementation detail:" must be detected even though
    # "implement" alone won't match "Implementation" under word boundaries.
    cards = extract_memory_cards(
        "how did we implement PR3?",
        "Implementation detail: multi-query recall runs queue_prefetch_all then "
        "prefetch_all for each subquery.",
    )
    assert MemoryCardType.IMPLEMENTATION_DETAIL in _types(cards)


def test_open_question_extraction():
    cards = extract_memory_cards(
        "One open question remains: the mobile layout is still TBD.",
        "ok",
    )
    assert MemoryCardType.OPEN_QUESTION in _types(cards)
    oq = next(c for c in cards if c.type == MemoryCardType.OPEN_QUESTION)
    assert oq.status == MemoryCardStatus.OPEN


# ---------------------------------------------------------------------------
# Logistics plan extraction (auto-captured, no explicit "remember")
# ---------------------------------------------------------------------------


def test_logistics_airport_bus_plan_english():
    cards = extract_memory_cards(
        "for my Tokyo trip the airport bus departs at 23:30, fare about 35 RMB, "
        "duration around 1h09, backup taxi.",
        "Got it.",
    )
    assert MemoryCardType.LOGISTICS_PLAN in _types(cards)
    plan = next(c for c in cards if c.type == MemoryCardType.LOGISTICS_PLAN)
    assert plan.status == MemoryCardStatus.ACTIVE
    # Concrete details are preserved in the summary.
    assert "23:30" in plan.summary
    assert "35 RMB" in plan.summary


def test_logistics_airport_bus_plan_chinese():
    cards = extract_memory_cards(
        "我的机场巴士计划是23:30出发，票价大概35元，备选打车。",
        "好的。",
    )
    assert MemoryCardType.LOGISTICS_PLAN in _types(cards)


def test_logistics_flight_and_hotel_booking():
    cards = extract_memory_cards(
        "I booked flight CA123 departing 09:00 and the Grand Hyatt hotel for "
        "two nights.",
        "Noted.",
    )
    assert MemoryCardType.LOGISTICS_PLAN in _types(cards)


def test_logistics_airport_transfer_with_price_duration_backup():
    cards = extract_memory_cards(
        "Airport transfer is the express bus, fare 40 RMB, about 50 minutes, "
        "with a taxi backup.",
        "Understood.",
    )
    assert MemoryCardType.LOGISTICS_PLAN in _types(cards)


def test_logistics_plan_can_come_from_assistant():
    cards = extract_memory_cards(
        "ok",
        "Your airport shuttle departs at 06:15 and the fare is 30 RMB; backup "
        "is a taxi.",
    )
    assert MemoryCardType.LOGISTICS_PLAN in _types(cards)


def test_vague_travel_interest_does_not_extract_logistics():
    cards = extract_memory_cards(
        "I'd love to travel more someday and see the world.",
        "That sounds lovely.",
    )
    assert MemoryCardType.LOGISTICS_PLAN not in _types(cards)


def test_airport_bus_without_concrete_anchor_does_not_extract():
    # Trigger word present but no time/price/date/route/backup anchor.
    cards = extract_memory_cards(
        "maybe I'll take the airport bus, seems okay.",
        "Sure.",
    )
    assert MemoryCardType.LOGISTICS_PLAN not in _types(cards)


def test_engineering_prose_with_incidental_anchor_is_not_logistics():
    # Software vocabulary (route/transfer/train/cache) plus an incidental
    # duration must NOT be mistaken for a travel plan. These previously
    # mis-typed as logistics_plan when the needles were too broad.
    samples = [
        "we cache the route table for 5 minutes in the prefetch queue.",
        "the transfer learning model trained for 30 minutes.",
        "the train job ran for 2 hours overnight.",
        "let me route this via the staging proxy.",
    ]
    for text in samples:
        cards = extract_memory_cards("ok", text)
        assert MemoryCardType.LOGISTICS_PLAN not in _types(cards), text


def test_logistics_is_additive_existing_impl_classification_unchanged():
    # The implementation_detail sentence still classifies as impl, not
    # logistics (logistics is purely additive / last in the signal order).
    cards = extract_memory_cards(
        "how does recall work?",
        "We implement the cache keyed by query and session in the prefetch "
        "queue path.",
    )
    assert MemoryCardType.IMPLEMENTATION_DETAIL in _types(cards)
    assert MemoryCardType.LOGISTICS_PLAN not in _types(cards)


def test_logistics_plan_strips_memory_context_and_does_not_leak():
    cards = extract_memory_cards(
        "ok",
        "Your airport bus departs 23:30. "
        "<memory-context>secret recalled itinerary booked flight QF1 at 06:00"
        "</memory-context>",
    )
    assert MemoryCardType.LOGISTICS_PLAN in _types(cards)
    blob = " ".join(c.summary for c in cards) + " ".join(
        " ".join(c.entities) for c in cards
    )
    assert "secret recalled" not in blob
    assert "QF1" not in blob
    assert "memory-context" not in blob


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def test_memory_context_blocks_are_stripped():
    cards = extract_memory_cards(
        "We decided to use Foo. "
        "<memory-context>secret recalled text decided to use Bar</memory-context>",
        "ok",
    )
    blob = " ".join(c.summary for c in cards) + " ".join(
        " ".join(c.entities) for c in cards
    )
    assert "secret recalled text" not in blob
    assert "memory-context" not in blob


def test_code_and_tool_blobs_are_stripped_or_ignored():
    fenced = (
        "We decided to use the queue.\n"
        "```python\n"
        "def hack():\n"
        "    return 'decided to use evil'\n"
        "```\n"
    )
    cards = extract_memory_cards("ok", fenced)
    blob = " ".join(c.summary for c in cards)
    assert "def hack" not in blob
    assert "evil" not in blob


# ---------------------------------------------------------------------------
# Bounding, caps, dedupe, determinism
# ---------------------------------------------------------------------------


def test_max_cards_respected():
    text = (
        "We decided to use Foo. "
        "The worker must not block. "
        "Next step is to add tests. "
        "I prefer dark mode. "
        "We implement the cache in the queue. "
        "One open question: layout is TBD."
    )
    cards = extract_memory_cards(text, text, max_cards=2)
    assert len(cards) == 2


def test_max_cards_zero_returns_empty():
    cards = extract_memory_cards("We decided to use Foo", "final decision", max_cards=0)
    assert cards == []


def test_max_chars_respected_truncates_processed_text():
    # The decision sentence sits beyond the char budget, so it is truncated
    # away before classification and never becomes a card.
    assistant = ("x" * 200) + ". We decided to use Foo."
    cards = extract_memory_cards("ok", assistant, max_chars=50)
    assert MemoryCardType.DECISION not in _types(cards)


def test_summaries_are_bounded():
    long_decision = "We decided to use " + ("a very long phrase " * 60) + "."
    cards = extract_memory_cards(long_decision, "ok")
    assert cards
    assert all(len(c.summary) <= 240 for c in cards)


def test_card_id_is_deterministic():
    args = ("We decided to use compact cards. This is final.", "Got it.")
    first = extract_memory_cards(*args, session_id="s1")
    second = extract_memory_cards(*args, session_id="s1")
    assert [c.card_id for c in first] == [c.card_id for c in second]
    assert all(c.card_id for c in first)


def test_card_id_changes_with_session():
    args = ("We decided to use compact cards. This is final.", "Got it.")
    a = extract_memory_cards(*args, session_id="s1")
    b = extract_memory_cards(*args, session_id="s2")
    assert a and b
    assert a[0].card_id != b[0].card_id


def test_duplicate_sentences_within_turn_are_deduped():
    repeated = (
        "We decided to use the compact cards. "
        "We decided to use the compact cards. "
        "We decided to use the compact cards."
    )
    cards = extract_memory_cards(repeated, "ok")
    decisions = [c for c in cards if c.type == MemoryCardType.DECISION]
    assert len(decisions) == 1


def test_source_turn_hash_does_not_expose_raw_text():
    cards = extract_memory_cards(
        "secretuserphrase decided to use compact cards finally.", "secretassistantphrase"
    )
    assert cards
    for c in cards:
        assert "secretuserphrase" not in c.source_turn_hash
        assert "secretassistantphrase" not in c.source_turn_hash
        assert len(c.source_turn_hash) <= 32



def test_user_origin_priority_and_source_role():
    cards = extract_memory_cards(
        "I prefer dark mode. We implement the cache in Redis.",
        "We decided to use dark mode. We implement the queue in Redis.",
        max_cards=4,
    )
    by_type = {card.type: card for card in cards}
    assert by_type[MemoryCardType.PREFERENCE].source_role == "user"
    assert by_type[MemoryCardType.IMPLEMENTATION_DETAIL].source_role == "assistant"
    assert MemoryCardType.DECISION not in {card.type for card in cards}


def test_assistant_only_durable_card_types_are_limited():
    cards = extract_memory_cards(
        "ok",
        "We decided to use compact cards. Next step is to add tests. "
        "The worker must not block. We implement the cache in Redis. "
        "One open question: rollout timing.",
        max_cards=10,
    )
    assert {card.type for card in cards} == {MemoryCardType.IMPLEMENTATION_DETAIL}
    assert cards[0].source_role == "assistant"


def test_format_includes_source_role():
    cards = extract_memory_cards("I prefer dark mode.", "ok")
    formatted = format_memory_cards_for_sync(cards)
    assert "source_role: user" in formatted


def test_parse_includes_source_role():
    parsed = parse_memory_cards_from_text(
        "<structured-memory-cards>\n"
        "- type: preference\n"
        "  card_id: ID1\n"
        "  source_role: user\n"
        "</structured-memory-cards>"
    )
    assert len(parsed) == 1
    assert parsed[0].source_role == "user"


def test_unicode_preserved_safely():
    cards = extract_memory_cards(
        "我喜欢简洁的设计。",
        "好的。",
    )
    assert cards
    summaries = " ".join(c.summary for c in cards)
    assert "简洁" in summaries


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _sample_card(**overrides):
    base = dict(
        card_id="abc123",
        type=MemoryCardType.DECISION,
        status=MemoryCardStatus.ACTIVE,
        title="Telegram approval cards UX",
        summary="Final decision: use compact inline approval cards.",
        entities=["Telegram approval cards", "Approve", "Reject"],
        confidence="medium",
        source_session_id="sess-1",
        source_turn_hash="deadbeef",
    )
    base.update(overrides)
    return MemoryCard(**base)


def test_format_empty_cards_returns_empty_string():
    assert format_memory_cards_for_sync([]) == ""
    assert format_memory_cards_for_sync(None) == ""


def test_format_includes_core_fields():
    out = format_memory_cards_for_sync([_sample_card()])
    assert 'structured-memory-cards version="1"' in out
    assert "type: decision" in out
    assert "status: active" in out
    assert "title: Telegram approval cards UX" in out
    assert "summary: Final decision" in out
    assert "entities: Telegram approval cards; Approve; Reject" in out
    assert "confidence: medium" in out
    assert out.strip().endswith("</structured-memory-cards>")


def test_format_includes_search_friendly_labels():
    decision = format_memory_cards_for_sync([_sample_card()])
    assert "final decision" in decision
    assert "previous decision" in decision

    pref = format_memory_cards_for_sync(
        [_sample_card(type=MemoryCardType.PREFERENCE)]
    )
    assert "user preference" in pref

    todo = format_memory_cards_for_sync([_sample_card(type=MemoryCardType.TODO)])
    assert "todo" in todo

    constraint = format_memory_cards_for_sync(
        [_sample_card(type=MemoryCardType.CONSTRAINT)]
    )
    assert "constraint" in constraint

    impl = format_memory_cards_for_sync(
        [_sample_card(type=MemoryCardType.IMPLEMENTATION_DETAIL)]
    )
    assert "implementation detail" in impl

    oq = format_memory_cards_for_sync(
        [_sample_card(type=MemoryCardType.OPEN_QUESTION)]
    )
    assert "open question" in oq

    logistics = format_memory_cards_for_sync(
        [_sample_card(type=MemoryCardType.LOGISTICS_PLAN)]
    )
    assert "type: logistics_plan" in logistics
    assert "logistics plan" in logistics
    assert "travel plan" in logistics
    assert "airport transfer" in logistics
    assert "itinerary" in logistics


def test_format_respects_max_chars():
    cards = [_sample_card() for _ in range(20)]
    out = format_memory_cards_for_sync(cards, max_chars=400)
    assert len(out) <= 400
    # Still well-formed when non-empty.
    if out:
        assert out.startswith('<structured-memory-cards version="1">')
        assert out.endswith("</structured-memory-cards>")


def test_format_does_not_leak_memory_context_content():
    # Even if a (malformed) card carried context markers, the formatter must
    # not emit a usable memory-context block.
    out = format_memory_cards_for_sync(
        [_sample_card(summary="harmless summary", entities=["plain"])]
    )
    assert "<memory-context>" not in out
    assert "[System note:" not in out


# ---------------------------------------------------------------------------
# PR5: schema, formatting, conflict-group id, parser
# ---------------------------------------------------------------------------

from agent.memory_cards import (  # noqa: E402
    ParsedMemoryCard,
    compute_conflict_group_id,
    parse_memory_cards_from_text,
)


def test_memorycard_supports_supersession_fields():
    c = _sample_card(
        supersedes=["a", "b"], superseded_by="z", conflict_group_id="g1"
    )
    assert c.supersedes == ["a", "b"]
    assert c.superseded_by == "z"
    assert c.conflict_group_id == "g1"


def test_pr4_card_defaults_keep_supersession_empty():
    # Backward compat: a card built the PR4 way has empty supersession fields.
    cards = extract_memory_cards("We decided to use Foo. This is final.", "ok")
    assert cards
    for c in cards:
        assert c.supersedes == []
        assert c.superseded_by is None
        assert c.conflict_group_id is None


def test_status_superseded_constant_exists():
    assert MemoryCardStatus.SUPERSEDED == "superseded"


def test_format_includes_card_id():
    out = format_memory_cards_for_sync([_sample_card(card_id="CARD42")])
    assert "card_id: CARD42" in out


def test_format_includes_supersession_fields_only_when_present():
    plain = format_memory_cards_for_sync([_sample_card()])
    assert "supersedes:" not in plain
    assert "superseded_by:" not in plain
    assert "conflict_group:" not in plain

    rich = format_memory_cards_for_sync(
        [_sample_card(supersedes=["OLD1", "OLD2"], conflict_group_id="G9")]
    )
    assert "supersedes: OLD1; OLD2" in rich
    assert "conflict_group: G9" in rich

    marker = format_memory_cards_for_sync(
        [_sample_card(status=MemoryCardStatus.SUPERSEDED, superseded_by="NEW1")]
    )
    assert "status: superseded" in marker
    assert "superseded_by: NEW1" in marker


def test_format_with_supersession_still_bounded():
    cards = [
        _sample_card(card_id=str(i), supersedes=["x", "y"], conflict_group_id="g")
        for i in range(30)
    ]
    out = format_memory_cards_for_sync(cards, max_chars=500)
    assert len(out) <= 500
    if out:
        assert out.endswith("</structured-memory-cards>")


def test_conflict_group_id_deterministic_and_topic_based():
    a = compute_conflict_group_id("decision", ["Telegram cards", "Approve"], "t")
    b = compute_conflict_group_id("decision", ["Approve", "Telegram cards"], "t2")
    assert a == b  # entity-order independent
    c = compute_conflict_group_id("preference", ["Telegram cards", "Approve"], "t")
    assert c != a  # type participates
    assert compute_conflict_group_id("decision", [], "") == ""


def test_parse_single_card():
    text = format_memory_cards_for_sync([_sample_card(card_id="ID1")])
    parsed = parse_memory_cards_from_text(text)
    assert len(parsed) == 1
    p = parsed[0]
    assert p.card_id == "ID1"
    assert p.type == "decision"
    assert p.status == "active"
    assert p.title == "Telegram approval cards UX"
    assert "Final decision" in p.summary
    assert "Telegram approval cards" in p.entities


def test_parse_multiple_cards():
    text = format_memory_cards_for_sync(
        [
            _sample_card(card_id="ID1"),
            _sample_card(card_id="ID2", type=MemoryCardType.PREFERENCE),
        ]
    )
    parsed = parse_memory_cards_from_text(text)
    assert [p.card_id for p in parsed] == ["ID1", "ID2"]
    assert [p.type for p in parsed] == ["decision", "preference"]


def test_parse_supersession_fields():
    text = format_memory_cards_for_sync(
        [
            _sample_card(
                card_id="NEW", supersedes=["OLD1", "OLD2"], conflict_group_id="G"
            ),
            _sample_card(
                card_id="MARK",
                status=MemoryCardStatus.SUPERSEDED,
                superseded_by="NEW",
            ),
        ]
    )
    parsed = parse_memory_cards_from_text(text)
    by_id = {p.card_id: p for p in parsed}
    assert by_id["NEW"].supersedes == ["OLD1", "OLD2"]
    assert by_id["NEW"].conflict_group == "G"
    assert by_id["MARK"].status == "superseded"
    assert by_id["MARK"].superseded_by == "NEW"


def test_parse_malformed_returns_empty():
    assert parse_memory_cards_from_text("just normal prose, no cards") == []
    assert parse_memory_cards_from_text("") == []
    assert parse_memory_cards_from_text(None) == []
    assert parse_memory_cards_from_text(12345) == []
    # Wrapper present but no real card fields → no usable cards.
    assert (
        parse_memory_cards_from_text(
            "<structured-memory-cards>\n  nonsense line\n</structured-memory-cards>"
        )
        == []
    )


def test_parse_strips_memory_context_blocks():
    text = (
        "<memory-context>SECRET_LEAK decided to use evil</memory-context>\n"
        + format_memory_cards_for_sync([_sample_card(card_id="ID1")])
    )
    parsed = parse_memory_cards_from_text(text)
    assert len(parsed) == 1
    blob = " ".join(p.summary + p.title + " ".join(p.entities) for p in parsed)
    assert "SECRET_LEAK" not in blob


def test_parse_unicode_safe():
    card = _sample_card(
        card_id="ID1",
        title="审批卡片",
        summary="最终决定就用紧凑卡片。",
        entities=["审批卡片"],
    )
    parsed = parse_memory_cards_from_text(format_memory_cards_for_sync([card]))
    assert len(parsed) == 1
    assert parsed[0].title == "审批卡片"
    assert "紧凑卡片" in parsed[0].summary

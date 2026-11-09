"""Regression coverage for keyed external-memory prefetch caches."""

from __future__ import annotations

import threading
import logging

from agent.memory_prefetch import (
    make_prefetch_entry,
    normalize_prefetch_query,
    prefetch_entry_matches,
    prefetch_entry_result,
    short_hash,
)
from agent.memory_provider import MemoryProvider


class _QueuedProvider(MemoryProvider):
    def __init__(self) -> None:
        self._entry = None
        self.scope = "scope-a"

    @property
    def name(self) -> str:
        return "queued"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def get_tool_schemas(self):
        return []

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self._entry = make_prefetch_entry(
            "queued result",
            query,
            session_id=session_id,
            effective_scope=self.scope,
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        entry = self._entry
        self._entry = None
        if not prefetch_entry_matches(
            entry,
            query,
            session_id=session_id,
            effective_scope=self.scope,
        ):
            return ""
        return prefetch_entry_result(entry)


class _KeyedSingleSlotProvider(MemoryProvider):
    """External provider with a single keyed result slot.

    Refreshes its slot on every queue_prefetch and only returns a result whose
    key matches the prefetch query/scope — so it can only serve a result that
    was queued for that exact query immediately beforehand.
    """

    def __init__(self) -> None:
        self._entry = None
        self.scope = "scope-x"

    @property
    def name(self) -> str:
        return "keyed-single-slot"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def get_tool_schemas(self):
        return []

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self._entry = make_prefetch_entry(
            f"- mem {short_hash(query)}",
            query,
            session_id=session_id,
            effective_scope=self.scope,
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        entry = self._entry
        self._entry = None
        if not prefetch_entry_matches(
            entry, query, session_id=session_id, effective_scope=self.scope
        ):
            return ""
        return prefetch_entry_result(entry)


def test_multi_query_orchestration_returns_each_subquery_own_keyed_result():
    """queue-then-prefetch PER subquery survives a single-slot keyed provider.

    If the orchestrator batched (queue all, then prefetch all), the single slot
    would only hold the last subquery and all earlier prefetches would miss.
    Multiple distinct sections surviving proves per-subquery interleaving.
    """
    from types import SimpleNamespace

    from agent.memory_recall import _recall_multi_query
    from agent.memory_manager import MemoryManager

    manager = MemoryManager()
    manager.add_provider(_KeyedSingleSlotProvider())
    agent = SimpleNamespace(_memory_manager=manager)

    merged = _recall_multi_query(
        agent,
        'what did we decide about Telegram approval cards and "compact mode"?',
        "sess-1",
        None,
    )

    assert merged.count("- mem ") >= 2


def test_multi_query_orchestration_never_injects_stale_query_a_for_query_b():
    """A stale entry keyed to query A must never surface for other subqueries."""
    from types import SimpleNamespace

    from agent.memory_recall import _recall_multi_query
    from agent.memory_manager import MemoryManager
    from agent.memory_recall_query import build_recall_query_plan

    class _StaleProvider(MemoryProvider):
        def __init__(self) -> None:
            self.scope = "scope-x"
            # A leftover result keyed to a query none of the subqueries equal.
            self._entry = make_prefetch_entry(
                "STALE-A-SECRET",
                "totally unrelated query A",
                session_id="sess-1",
                effective_scope=self.scope,
            )

        @property
        def name(self) -> str:
            return "stale"

        def is_available(self) -> bool:
            return True

        def initialize(self, session_id, **kwargs):
            pass

        def get_tool_schemas(self):
            return []

        def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
            # Simulate a provider that fails to refresh: the stale entry stays.
            pass

        def prefetch(self, query: str, *, session_id: str = "") -> str:
            if not prefetch_entry_matches(
                self._entry, query, session_id=session_id, effective_scope=self.scope
            ):
                return ""
            return prefetch_entry_result(self._entry)

    prompt = 'what did we decide about Telegram approval cards and "compact mode"?'
    # Guard: no generated subquery equals the stale entry's query.
    subqueries = build_recall_query_plan(prompt).subqueries
    assert "totally unrelated query A" not in subqueries

    manager = MemoryManager()
    manager.add_provider(_StaleProvider())
    agent = SimpleNamespace(_memory_manager=manager)

    merged = _recall_multi_query(agent, prompt, "sess-1", None)

    assert merged == ""
    assert "STALE-A-SECRET" not in merged


def test_queued_result_for_query_a_is_not_returned_for_query_b():
    provider = _QueuedProvider()
    provider.queue_prefetch("query A", session_id="sess-1")

    assert provider.prefetch("query B", session_id="sess-1") == ""


def test_same_query_same_scope_consumes_queued_result():
    provider = _QueuedProvider()
    provider.queue_prefetch("query A", session_id="sess-1")

    assert provider.prefetch("query A", session_id="sess-1") == "queued result"


def test_same_query_different_session_scope_does_not_consume_result():
    provider = _QueuedProvider()
    provider.queue_prefetch("query A", session_id="sess-1")

    assert provider.prefetch("query A", session_id="sess-2") == ""


def test_prefetch_query_normalization_handles_edge_values():
    assert normalize_prefetch_query(None) == ""
    assert normalize_prefetch_query("") == ""
    assert normalize_prefetch_query("  Hello\tWorld \n") == "hello world"
    assert normalize_prefetch_query("HELLO world") == "hello world"
    assert normalize_prefetch_query(123) == "123"
    assert normalize_prefetch_query("  café  東京  ") == "café 東京"


def test_short_hash_handles_edge_values_stably():
    assert short_hash(None) == ""
    assert short_hash("") == ""
    assert short_hash("  Hello World  ") == short_hash("  Hello World  ")
    assert short_hash(123) == short_hash("123")
    assert short_hash("café 東京") == short_hash("café 東京")
















def test_honcho_pending_dialectic_requires_query_and_scope_match():
    from plugins.memory.honcho import HonchoMemoryProvider

    provider = object.__new__(HonchoMemoryProvider)
    provider._cron_skipped = False
    provider._recall_mode = "context"
    provider._session_ready = lambda: True
    provider._injection_frequency = "always"
    provider._turn_count = 1
    provider._is_trivial_prompt = lambda _query: False
    provider._base_context_lock = threading.Lock()
    provider._base_context_cache = ""
    provider._manager = None
    provider._prefetch_lock = threading.Lock()
    provider._prefetch_thread = None
    provider._prefetch_thread_started_at = 0.0
    provider._prefetch_result_fired_at = 1
    provider._last_dialectic_turn = 1
    provider._dialectic_cadence = 1
    provider._config = None
    provider._session_key = "honcho-session"
    provider._prefetch_result = make_prefetch_entry(
        "remembered",
        "query A",
        session_id="sess-1",
        effective_scope=provider._session_key,
        fired_at=1,
    )

    assert provider.prefetch("query B", session_id="sess-1") == ""

    provider._prefetch_result_fired_at = 1
    provider._prefetch_result = make_prefetch_entry(
        "remembered",
        "query A",
        session_id="sess-1",
        effective_scope=provider._session_key,
        fired_at=1,
    )
    assert "remembered" in provider.prefetch("query A", session_id="sess-1")

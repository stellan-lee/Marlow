"""Deterministic memory recall, independent of any inference loop."""

import logging
import time
from agent.memory_prefetch import short_hash
from agent.memory_recall_query import build_recall_query_plan
from agent.memory_recall_merge import merge_recall_results

logger = logging.getLogger(__name__)


def _recall_one_subquery(
    manager,
    subquery: str,
    sid: str,
    provider: str | None = None,
    base_url: str | None = None,
) -> str:
    """Queue-then-prefetch a single subquery synchronously.

    Queue and prefetch use the *identical* subquery and the same session id,
    so the provider's keyed cache (PR1) only returns a result that was queued
    for this exact subquery/scope.
    """
    manager.queue_prefetch_all(subquery, session_id=sid)
    kwargs = {"session_id": sid}
    if provider:
        kwargs["provider"] = provider
        if base_url:
            kwargs["base_url"] = base_url
    return manager.prefetch_all(subquery, **kwargs) or ""


def _recall_multi_query(
    agent,
    raw_query: str,
    sid: str,
    recent_messages,
    provider: str | None = None,
    base_url: str | None = None,
) -> str:
    """Deterministic multi-query recall: plan → per-subquery retrieve → merge.

    Builds the subquery plan via the recall query builder (always, regardless
    of ``recall_query_builder_enabled`` — multi-query implies the builder),
    retrieves each subquery with queue-then-prefetch *per subquery* (never
    queue-all-then-prefetch-all, which would clobber the provider's single
    result slot), then merges/dedupes into one block.

    Combined with PR1's keyed caches, doing queue+prefetch synchronously per
    subquery guarantees a result queued for subquery A is never injected for
    subquery B. Subqueries are recall-only: never persisted, never logged raw.
    """
    plan = build_recall_query_plan(
        raw_query,
        recent_messages=recent_messages,
        max_recent_turns=getattr(agent, "_memory_recall_query_recent_turns", 6),
        max_recent_chars=getattr(agent, "_memory_recall_query_max_recent_chars", 1200),
        max_query_chars=getattr(agent, "_memory_recall_query_max_chars", 1800),
        max_queries=getattr(agent, "_memory_multi_query_recall_max_queries", 4),
    )
    subqueries = plan.subqueries
    if not subqueries:
        return ""

    timeout_ms = int(
        getattr(agent, "_memory_multi_query_recall_per_query_timeout_ms", 3000) or 0
    )
    manager = agent._memory_manager
    results: list[str] = []
    sub_meta: list[str] = []
    for idx, subquery in enumerate(subqueries):
        started = time.monotonic()
        try:
            result = _recall_one_subquery(
                manager,
                subquery,
                sid,
                provider=provider,
                base_url=base_url,
            )
        except Exception:
            result = ""
        latency_ms = int((time.monotonic() - started) * 1000)
        sub_meta.append(
            "q%d:hash=%s qlen=%d rlen=%d lat_ms=%d"
            % (idx, short_hash(subquery), len(subquery), len(result), latency_ms)
        )
        if result and result.strip():
            results.append(result)
        # Cooperative ceiling: providers already self-bound each prefetch; if a
        # subquery still reaches the per-query budget, stop issuing more rather
        # than compounding latency across the remaining subqueries.
        if timeout_ms > 0 and latency_ms >= timeout_ms:
            break

    merged = merge_recall_results(
        results,
        max_total_chars=getattr(
            agent, "_memory_multi_query_recall_max_total_chars", 6000
        ),
        suppress_superseded=getattr(
            agent, "_memory_structured_conflict_filter_enabled", False
        ),
    )
    logger.debug(
        "memory multi-query recall: enabled=True planned=%d retrieved=%d [%s] "
        "raw_total=%d final=%d removed_chars=%d removed_sections=%d "
        "parsed_cards=%d suppressed_cards=%d injected=%s",
        len(subqueries),
        len(results),
        "; ".join(sub_meta),
        merged.raw_chars,
        merged.final_chars,
        merged.removed_chars,
        max(0, merged.input_sections - merged.output_sections),
        merged.parsed_card_count,
        merged.suppressed_card_count,
        bool(merged.text),
    )
    return merged.text


def recall_external(agent, query, history):
    manager = agent._memory_manager
    if manager is None or not query:
        return ""
    manager.reset_prefetch_stats()
    try:
        manager.on_turn_start(agent._user_turn_count + 1, query)
        if agent._memory_multi_query_recall_enabled:
            return _recall_multi_query(
                agent,
                query,
                agent.session_id,
                history,
                provider=agent.provider,
                base_url=agent.base_url,
            )
        recalled_query = query
        if agent._memory_recall_query_builder_enabled:
            plan = build_recall_query_plan(
                query,
                recent_messages=history,
                max_recent_turns=agent._memory_recall_query_recent_turns,
                max_recent_chars=agent._memory_recall_query_max_recent_chars,
                max_query_chars=agent._memory_recall_query_max_chars,
            )
            recalled_query = plan.recall_query or query
        result = _recall_one_subquery(
            manager,
            recalled_query,
            agent.session_id,
            provider=agent.provider,
            base_url=agent.base_url,
        )
        if result and agent._memory_structured_conflict_filter_enabled:
            result = merge_recall_results(
                [result], max_total_chars=len(result) + 1, suppress_superseded=True
            ).text
        return result
    except Exception as exc:
        logger.debug("Memory recall failed: %s", type(exc).__name__)
        return ""

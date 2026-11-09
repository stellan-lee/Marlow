"""
Session Insights Engine for Marlow Agent.

Analyzes historical session data from the SQLite state database to produce
comprehensive usage insights — token consumption, cost estimates, tool usage
patterns, activity trends, model/platform breakdowns, and session metrics.

Inspired by Claude Code's /insights command, adapted for Marlow Agent's
multi-platform architecture with additional cost estimation and platform
breakdown capabilities.

Usage:
    from agent.insights import InsightsEngine
    engine = InsightsEngine(db)
    report = engine.generate(days=30)
    print(engine.format_terminal(report))
"""

import json
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List

from agent.usage_pricing import (
    CanonicalUsage,
    DEFAULT_PRICING,
    estimate_usage_cost,
    format_duration_compact,
    has_known_pricing,
)

_DEFAULT_PRICING = DEFAULT_PRICING


def _has_known_pricing(model_name: str, provider: str = None, base_url: str = None) -> bool:
    """Check if a model has known pricing (vs unknown/custom endpoint)."""
    return has_known_pricing(model_name, provider=provider, base_url=base_url)


def _estimate_cost(
    session_or_model: Dict[str, Any] | str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    provider: str = None,
    base_url: str = None,
) -> tuple[float, str]:
    """Estimate the USD cost for a session row or a model/token tuple."""
    if isinstance(session_or_model, dict):
        session = session_or_model
        model = session.get("model") or ""
        usage = CanonicalUsage(
            input_tokens=session.get("input_tokens") or 0,
            output_tokens=session.get("output_tokens") or 0,
            cache_read_tokens=session.get("cache_read_tokens") or 0,
            cache_write_tokens=session.get("cache_write_tokens") or 0,
        )
        provider = session.get("billing_provider")
        base_url = session.get("billing_base_url")
    else:
        model = session_or_model or ""
        usage = CanonicalUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
    result = estimate_usage_cost(
        model,
        usage,
        provider=provider,
        base_url=base_url,
    )
    return float(result.amount_usd or 0.0), result.status


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    return format_duration_compact(seconds)


def _bar_chart(values: List[int], max_width: int = 20) -> List[str]:
    """Create simple horizontal bar chart strings from values."""
    peak = max(values) if values else 1
    if peak == 0:
        return ["" for _ in values]
    return ["█" * max(1, int(v / peak * max_width)) if v > 0 else "" for v in values]


class InsightsEngine:
    """
    Analyzes session history and produces usage insights.

    Works directly with a SessionDB instance (or raw sqlite3 connection)
    to query session and message data.
    """

    def __init__(self, db):
        """
        Initialize with a SessionDB instance.

        Args:
            db: A SessionDB instance (from marlow_state.py)
        """
        self.db = db
        self._conn = db._conn

    def generate(self, days: int = 30, source: str = None) -> Dict[str, Any]:
        """
        Generate a complete insights report.

        Args:
            days: Number of days to look back (default: 30)
            source: Optional filter by source platform

        Returns:
            Dict with all computed insights
        """
        cutoff = time.time() - (days * 86400)

        # Gather raw data
        sessions = self._get_sessions(cutoff, source)
        tool_usage = self._get_tool_usage(cutoff, source)
        skill_usage = self._get_skill_usage(cutoff, source)
        usage_events = self._get_usage_events(cutoff, source)
        tracking_started_at = self._usage_tracking_started_at()
        experience = self._get_experience_usage(cutoff, usage_events, source=source)
        message_stats = self._get_message_stats(cutoff, source)

        if not sessions and not usage_events and not experience.get("summary", {}).get("retrieval_attempts"):
            return {
                "days": days,
                "source_filter": source,
                "empty": True,
                "overview": {},
                "models": [],
                "platforms": [],
                "tools": [],
                "mcp": self._empty_mcp_breakdown(),
                "memory": self._empty_memory_breakdown(),
                "experience": self._empty_experience_breakdown(),
                "skills": {
                    "summary": {
                        "total_skill_loads": 0,
                        "total_skill_edits": 0,
                        "total_skill_actions": 0,
                        "distinct_skills_used": 0,
                    },
                    "top_skills": [],
                },
                "activity": {},
                "top_sessions": [],
                "usage_tracking_started_at": tracking_started_at,
            }

        # Compute insights
        overview = self._compute_overview(sessions, message_stats)
        models = self._compute_model_breakdown(sessions)
        platforms = self._compute_platform_breakdown(sessions)
        tools = self._compute_tool_breakdown(
            self._merge_regular_tool_usage(tool_usage, usage_events)
        )
        skills = self._compute_skill_breakdown(
            self._merge_skill_usage(skill_usage, usage_events)
        )
        mcp = self._compute_mcp_breakdown(tool_usage, usage_events)
        memory = self._compute_memory_breakdown(usage_events)
        activity = self._compute_activity_patterns(sessions)
        top_sessions = self._compute_top_sessions(sessions)

        return {
            "days": days,
            "source_filter": source,
            "empty": False,
            "generated_at": time.time(),
            "overview": overview,
            "models": models,
            "platforms": platforms,
            "tools": tools,
            "skills": skills,
            "mcp": mcp,
            "memory": memory,
            "experience": experience,
            "activity": activity,
            "top_sessions": top_sessions,
            "usage_tracking_started_at": tracking_started_at,
        }

    # =========================================================================
    # Data gathering (SQL queries)
    # =========================================================================

    # Columns we actually need (skip system_prompt, model_config blobs)
    _SESSION_COLS = ("id, source, model, started_at, ended_at, "
                     "message_count, tool_call_count, input_tokens, output_tokens, "
                     "cache_read_tokens, cache_write_tokens, billing_provider, "
                     "billing_base_url, billing_mode, estimated_cost_usd, "
                     "actual_cost_usd, cost_status, cost_source")

    # Pre-computed query strings — f-string evaluated once at class definition,
    # not at runtime, so no user-controlled value can alter the query structure.
    _GET_SESSIONS_WITH_SOURCE = (
        f"SELECT {_SESSION_COLS} FROM sessions"
        " WHERE started_at >= ? AND source = ?"
        " ORDER BY started_at DESC"
    )
    _GET_SESSIONS_ALL = (
        f"SELECT {_SESSION_COLS} FROM sessions"
        " WHERE started_at >= ?"
        " ORDER BY started_at DESC"
    )

    def _get_sessions(self, cutoff: float, source: str = None) -> List[Dict]:
        """Fetch sessions within the time window."""
        if source:
            cursor = self._conn.execute(self._GET_SESSIONS_WITH_SOURCE, (cutoff, source))
        else:
            cursor = self._conn.execute(self._GET_SESSIONS_ALL, (cutoff,))
        return [dict(row) for row in cursor.fetchall()]

    def _get_usage_events(self, cutoff: float, source: str = None) -> List[Dict]:
        try:
            return self.db.list_usage_events(since=cutoff, source=source)
        except Exception:
            return []

    def _usage_tracking_started_at(self) -> float | None:
        try:
            return self.db.usage_tracking_started_at()
        except Exception:
            return None

    def _get_tool_usage(self, cutoff: float, source: str = None) -> List[Dict]:
        """Get tool call counts from messages.

        Uses two sources:
        1. tool_name column on 'tool' role messages (set by gateway)
        2. tool_calls JSON on 'assistant' role messages (covers CLI where
           tool_name is not populated on tool responses)
        """
        tool_counts = Counter()

        # Source 1: explicit tool_name on tool response messages
        if source:
            cursor = self._conn.execute(
                """SELECT m.tool_name, COUNT(*) as count
                   FROM messages m
                   JOIN sessions s ON s.id = m.session_id
                   WHERE s.started_at >= ? AND s.source = ?
                     AND m.role = 'tool' AND m.tool_name IS NOT NULL
                   GROUP BY m.tool_name
                   ORDER BY count DESC""",
                (cutoff, source),
            )
        else:
            cursor = self._conn.execute(
                """SELECT m.tool_name, COUNT(*) as count
                   FROM messages m
                   JOIN sessions s ON s.id = m.session_id
                   WHERE s.started_at >= ?
                     AND m.role = 'tool' AND m.tool_name IS NOT NULL
                   GROUP BY m.tool_name
                   ORDER BY count DESC""",
                (cutoff,),
            )
        for row in cursor.fetchall():
            tool_counts[row["tool_name"]] += row["count"]

        # Source 2: extract from tool_calls JSON on assistant messages
        # (covers CLI sessions where tool_name is NULL on tool responses)
        if source:
            cursor2 = self._conn.execute(
                """SELECT m.tool_calls
                   FROM messages m
                   JOIN sessions s ON s.id = m.session_id
                   WHERE s.started_at >= ? AND s.source = ?
                     AND m.role = 'assistant' AND m.tool_calls IS NOT NULL""",
                (cutoff, source),
            )
        else:
            cursor2 = self._conn.execute(
                """SELECT m.tool_calls
                   FROM messages m
                   JOIN sessions s ON s.id = m.session_id
                   WHERE s.started_at >= ?
                     AND m.role = 'assistant' AND m.tool_calls IS NOT NULL""",
                (cutoff,),
            )

        tool_calls_counts = Counter()
        for row in cursor2.fetchall():
            try:
                calls = row["tool_calls"]
                if isinstance(calls, str):
                    calls = json.loads(calls)
                if isinstance(calls, list):
                    for call in calls:
                        func = call.get("function", {}) if isinstance(call, dict) else {}
                        name = func.get("name")
                        if name:
                            tool_calls_counts[name] += 1
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue

        # Merge: prefer tool_name source, supplement with tool_calls source
        # for tools not already counted
        if not tool_counts and tool_calls_counts:
            # No tool_name data at all — use tool_calls exclusively
            tool_counts = tool_calls_counts
        elif tool_counts and tool_calls_counts:
            # Both sources have data — use whichever has the higher count per tool
            # (they may overlap, so take the max to avoid double-counting)
            all_tools = set(tool_counts) | set(tool_calls_counts)
            merged = Counter()
            for tool in all_tools:
                merged[tool] = max(tool_counts.get(tool, 0), tool_calls_counts.get(tool, 0))
            tool_counts = merged

        # Convert to the expected format
        return [
            {"tool_name": name, "count": count}
            for name, count in tool_counts.most_common()
        ]

    def _get_skill_usage(self, cutoff: float, source: str = None) -> List[Dict]:
        """Extract per-skill usage from assistant tool calls."""
        skill_counts: Dict[str, Dict[str, Any]] = {}

        if source:
            cursor = self._conn.execute(
                """SELECT m.tool_calls, m.timestamp
                   FROM messages m
                   JOIN sessions s ON s.id = m.session_id
                   WHERE s.started_at >= ? AND s.source = ?
                     AND m.role = 'assistant' AND m.tool_calls IS NOT NULL""",
                (cutoff, source),
            )
        else:
            cursor = self._conn.execute(
                """SELECT m.tool_calls, m.timestamp
                   FROM messages m
                   JOIN sessions s ON s.id = m.session_id
                   WHERE s.started_at >= ?
                     AND m.role = 'assistant' AND m.tool_calls IS NOT NULL""",
                (cutoff,),
            )

        for row in cursor.fetchall():
            try:
                calls = row["tool_calls"]
                if isinstance(calls, str):
                    calls = json.loads(calls)
                if not isinstance(calls, list):
                    continue
            except (json.JSONDecodeError, TypeError):
                continue

            timestamp = row["timestamp"]
            for call in calls:
                if not isinstance(call, dict):
                    continue
                func = call.get("function", {})
                tool_name = func.get("name")
                if tool_name not in {"skill_view", "skill_manage"}:
                    continue

                args = func.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        continue
                if not isinstance(args, dict):
                    continue

                skill_name = args.get("name")
                if not isinstance(skill_name, str) or not skill_name.strip():
                    continue

                entry = skill_counts.setdefault(
                    skill_name,
                    {
                        "skill": skill_name,
                        "view_count": 0,
                        "manage_count": 0,
                        "last_used_at": None,
                    },
                )
                if tool_name == "skill_view":
                    entry["view_count"] += 1
                else:
                    entry["manage_count"] += 1

                if timestamp is not None and (
                    entry["last_used_at"] is None or timestamp > entry["last_used_at"]
                ):
                    entry["last_used_at"] = timestamp

        return list(skill_counts.values())

    @staticmethod
    def _event_count(event: Dict[str, Any]) -> int:
        try:
            return max(0, int(event.get("value") or 0))
        except (TypeError, ValueError):
            return 0

    def _merge_regular_tool_usage(
        self,
        legacy_usage: List[Dict],
        events: List[Dict],
    ) -> List[Dict]:
        """Combine historical message counts with source-aware tool events.

        Session history already contains newly instrumented calls, so event
        counts replace (rather than add to) the overlapping per-tool slice.
        This preserves historical totals while providing success/failure
        metadata from the tracking start onward.
        """
        counts = Counter()
        for row in legacy_usage:
            name = row.get("tool_name")
            if not name or name in {"skill_view", "skill_manage", "memory"}:
                continue
            if str(name).startswith("mcp_"):
                continue
            counts[str(name)] += int(row.get("count") or 0)

        event_counts = Counter()
        success_counts = Counter()
        failure_counts = Counter()
        for event in events:
            if event.get("subsystem") != "tool" or event.get("action") != "call":
                continue
            name = str(event.get("item_name") or "unknown")
            amount = self._event_count(event) or 1
            event_counts[name] += amount
            if event.get("success") == 0:
                failure_counts[name] += amount
            elif event.get("success") == 1:
                success_counts[name] += amount
        for name, amount in event_counts.items():
            counts[name] = max(counts.get(name, 0), amount)

        return [
            {
                "tool_name": name,
                "count": count,
                "success_count": success_counts.get(name, 0),
                "failure_count": failure_counts.get(name, 0),
            }
            for name, count in counts.most_common()
        ]

    def _merge_skill_usage(
        self,
        legacy_usage: List[Dict],
        events: List[Dict],
    ) -> List[Dict]:
        merged: Dict[str, Dict[str, Any]] = {}
        for row in legacy_usage:
            name = str(row.get("skill") or "").strip()
            if not name:
                continue
            merged[name] = {
                **row,
                "task_load_count": int(row.get("view_count") or 0),
                "slash_load_count": 0,
                "curator_load_count": 0,
            }

        event_rows: Dict[str, Dict[str, Any]] = {}
        for event in events:
            if event.get("subsystem") != "skill":
                continue
            name = str(event.get("item_name") or "").strip()
            if not name:
                continue
            entry = event_rows.setdefault(
                name,
                {
                    "skill": name,
                    "view_count": 0,
                    "manage_count": 0,
                    "task_load_count": 0,
                    "slash_load_count": 0,
                    "curator_load_count": 0,
                    "last_used_at": None,
                },
            )
            amount = self._event_count(event) or 1
            action = event.get("action")
            source = event.get("source")
            if action == "load":
                entry["view_count"] += amount
                if source == "curator":
                    entry["curator_load_count"] += amount
                elif source == "slash_command":
                    entry["slash_load_count"] += amount
                else:
                    entry["task_load_count"] += amount
            elif action == "edit":
                entry["manage_count"] += amount
            timestamp = event.get("created_at")
            if timestamp is not None and (
                entry["last_used_at"] is None or timestamp > entry["last_used_at"]
            ):
                entry["last_used_at"] = timestamp

        for name, event_row in event_rows.items():
            current = merged.get(name)
            if current is None:
                merged[name] = event_row
                continue
            # Tool-call events overlap session-history rows. Preserve the
            # larger total, while slash-command loads are event-only.
            non_slash_event_loads = (
                event_row["task_load_count"] + event_row["curator_load_count"]
            )
            historical_loads = int(current.get("view_count") or 0)
            current["view_count"] = max(historical_loads, non_slash_event_loads)
            current["view_count"] += event_row["slash_load_count"]
            current["manage_count"] = max(
                int(current.get("manage_count") or 0),
                event_row["manage_count"],
            )
            current["task_load_count"] = max(
                event_row["task_load_count"],
                historical_loads - event_row["curator_load_count"],
            )
            current["slash_load_count"] = event_row["slash_load_count"]
            current["curator_load_count"] = event_row["curator_load_count"]
            current["last_used_at"] = max(
                current.get("last_used_at") or 0,
                event_row.get("last_used_at") or 0,
            ) or None
        return list(merged.values())

    def _get_experience_usage(
        self,
        cutoff: float,
        events: List[Dict],
        *,
        source: str | None = None,
    ) -> Dict[str, Any]:
        result = self._empty_experience_breakdown()
        if source is None:
            try:
                retrieval = self._conn.execute(
                    """
                    SELECT COUNT(*) AS attempts
                    FROM experience_retrievals WHERE created_at >= ?
                    """,
                    (cutoff,),
                ).fetchone()
                matched = self._conn.execute(
                    """
                    SELECT COUNT(DISTINCT ri.retrieval_id) AS matched_attempts,
                           COUNT(*) AS lessons_returned
                    FROM experience_retrieval_items ri
                    JOIN experience_retrievals r ON r.id = ri.retrieval_id
                    WHERE r.created_at >= ?
                    """,
                    (cutoff,),
                ).fetchone()
                top_rows = self._conn.execute(
                    """
                    SELECT ri.item_id, COUNT(*) AS recall_count,
                           MAX(r.created_at) AS last_recalled_at
                    FROM experience_retrieval_items ri
                    JOIN experience_retrievals r ON r.id = ri.retrieval_id
                    WHERE r.created_at >= ?
                    GROUP BY ri.item_id
                    ORDER BY recall_count DESC, last_recalled_at DESC
                    LIMIT 10
                    """,
                    (cutoff,),
                ).fetchall()
                result["summary"].update(
                    {
                        "retrieval_attempts": int(retrieval["attempts"] or 0),
                        "attempts_with_matches": int(matched["matched_attempts"] or 0),
                        "lessons_returned": int(matched["lessons_returned"] or 0),
                    }
                )
                result["top_lessons"] = [dict(row) for row in top_rows]
            except Exception:
                pass

        attempts = [
            event
            for event in events
            if event.get("subsystem") == "experience"
            and event.get("action") == "recall_attempt"
        ]
        hits = [
            event
            for event in events
            if event.get("subsystem") == "experience"
            and event.get("action") == "recall_hit"
        ]
        result["summary"]["retrieval_attempts"] = max(
            result["summary"]["retrieval_attempts"],
            sum(self._event_count(event) or 1 for event in attempts),
        )
        result["summary"]["attempts_with_matches"] = max(
            result["summary"]["attempts_with_matches"],
            len(hits),
        )
        result["summary"]["lessons_returned"] = max(
            result["summary"]["lessons_returned"],
            sum(self._event_count(event) for event in hits),
        )

        injections = [
            event
            for event in events
            if event.get("subsystem") == "experience"
            and event.get("action") == "context_injected"
        ]
        result["summary"]["context_injections"] = len(injections)
        result["summary"]["lessons_injected"] = sum(
            self._event_count(event) for event in injections
        )
        return result

    def _get_message_stats(self, cutoff: float, source: str = None) -> Dict:
        """Get aggregate message statistics."""
        if source:
            cursor = self._conn.execute(
                """SELECT
                     COUNT(*) as total_messages,
                     SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) as user_messages,
                     SUM(CASE WHEN m.role = 'assistant' THEN 1 ELSE 0 END) as assistant_messages,
                     SUM(CASE WHEN m.role = 'tool' THEN 1 ELSE 0 END) as tool_messages
                   FROM messages m
                   JOIN sessions s ON s.id = m.session_id
                   WHERE s.started_at >= ? AND s.source = ?""",
                (cutoff, source),
            )
        else:
            cursor = self._conn.execute(
                """SELECT
                     COUNT(*) as total_messages,
                     SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) as user_messages,
                     SUM(CASE WHEN m.role = 'assistant' THEN 1 ELSE 0 END) as assistant_messages,
                     SUM(CASE WHEN m.role = 'tool' THEN 1 ELSE 0 END) as tool_messages
                   FROM messages m
                   JOIN sessions s ON s.id = m.session_id
                   WHERE s.started_at >= ?""",
                (cutoff,),
            )
        row = cursor.fetchone()
        return dict(row) if row else {
            "total_messages": 0, "user_messages": 0,
            "assistant_messages": 0, "tool_messages": 0,
        }

    # =========================================================================
    # Computation
    # =========================================================================

    @staticmethod
    def _empty_mcp_breakdown() -> Dict[str, Any]:
        return {
            "summary": {
                "total_calls": 0,
                "successful_calls": 0,
                "failed_calls": 0,
                "distinct_servers": 0,
                "distinct_tools": 0,
            },
            "top_servers": [],
            "top_tools": [],
        }

    @staticmethod
    def _empty_memory_breakdown() -> Dict[str, Any]:
        return {
            "summary": {
                "recall_attempts": 0,
                "recall_hits": 0,
                "context_injections": 0,
                "hit_rate": 0.0,
                "writes": 0,
                "explicit_calls": 0,
            },
            "top_providers": [],
        }

    @staticmethod
    def _empty_experience_breakdown() -> Dict[str, Any]:
        return {
            "summary": {
                "retrieval_attempts": 0,
                "attempts_with_matches": 0,
                "lessons_returned": 0,
                "context_injections": 0,
                "lessons_injected": 0,
            },
            "top_lessons": [],
        }

    def _compute_mcp_breakdown(
        self,
        legacy_usage: List[Dict],
        events: List[Dict],
    ) -> Dict[str, Any]:
        tool_counts = Counter()
        for row in legacy_usage:
            name = str(row.get("tool_name") or "")
            if name.startswith("mcp_"):
                tool_counts[name] += int(row.get("count") or 0)

        event_tool_counts = Counter()
        server_counts = Counter()
        success = failure = 0
        tool_servers: Dict[str, str] = {}
        for event in events:
            if event.get("subsystem") != "mcp" or event.get("action") != "call":
                continue
            name = str(event.get("item_name") or "unknown")
            server = str(event.get("parent_name") or "unknown")
            amount = self._event_count(event) or 1
            event_tool_counts[name] += amount
            server_counts[server] += amount
            tool_servers[name] = server
            if event.get("success") == 0:
                failure += amount
            elif event.get("success") == 1:
                success += amount
        for name, amount in event_tool_counts.items():
            tool_counts[name] = max(tool_counts.get(name, 0), amount)

        # Historical MCP calls predate exact server provenance. Keep them in
        # the tool ranking and group only their unmatched remainder as legacy.
        legacy_unknown = max(0, sum(tool_counts.values()) - sum(event_tool_counts.values()))
        if legacy_unknown:
            server_counts["historical/unknown"] += legacy_unknown
        total = sum(tool_counts.values())
        return {
            "summary": {
                "total_calls": total,
                "successful_calls": success,
                "failed_calls": failure,
                "distinct_servers": len(server_counts),
                "distinct_tools": len(tool_counts),
            },
            "top_servers": [
                {"server": name, "count": count}
                for name, count in server_counts.most_common(10)
            ],
            "top_tools": [
                {
                    "tool": name,
                    "server": tool_servers.get(name, "historical/unknown"),
                    "count": count,
                }
                for name, count in tool_counts.most_common(10)
            ],
        }

    def _compute_memory_breakdown(self, events: List[Dict]) -> Dict[str, Any]:
        summary = self._empty_memory_breakdown()["summary"]
        providers: Dict[str, Counter] = defaultdict(Counter)
        for event in events:
            if event.get("subsystem") != "memory":
                continue
            action = str(event.get("action") or "")
            provider = str(
                event.get("parent_name")
                or event.get("item_name")
                or "unknown"
            )
            amount = self._event_count(event) or 1
            if action == "recall_attempt":
                summary["recall_attempts"] += amount
                providers[provider]["attempts"] += amount
            elif action == "recall_hit":
                summary["recall_hits"] += amount
                providers[provider]["hits"] += amount
            elif action == "context_injected":
                summary["context_injections"] += amount
                providers[provider]["injections"] += amount
            elif action == "write":
                summary["writes"] += amount
            elif action == "call":
                summary["explicit_calls"] += amount
        if summary["recall_attempts"]:
            summary["hit_rate"] = (
                summary["recall_hits"] / summary["recall_attempts"] * 100
            )
        top_providers = []
        for provider, counts in providers.items():
            attempts = counts["attempts"]
            top_providers.append(
                {
                    "provider": provider,
                    "attempts": attempts,
                    "hits": counts["hits"],
                    "injections": counts["injections"],
                    "hit_rate": counts["hits"] / attempts * 100 if attempts else 0.0,
                }
            )
        top_providers.sort(
            key=lambda row: (row["injections"], row["hits"], row["attempts"]),
            reverse=True,
        )
        return {"summary": summary, "top_providers": top_providers[:10]}

    def _compute_overview(self, sessions: List[Dict], message_stats: Dict) -> Dict:
        """Compute high-level overview statistics."""
        total_input = sum(s.get("input_tokens") or 0 for s in sessions)
        total_output = sum(s.get("output_tokens") or 0 for s in sessions)
        total_cache_read = sum(s.get("cache_read_tokens") or 0 for s in sessions)
        total_cache_write = sum(s.get("cache_write_tokens") or 0 for s in sessions)
        total_tokens = total_input + total_output + total_cache_read + total_cache_write
        total_tool_calls = sum(s.get("tool_call_count") or 0 for s in sessions)
        total_messages = sum(s.get("message_count") or 0 for s in sessions)

        # Cost estimation (weighted by model)
        total_cost = 0.0
        actual_cost = 0.0
        models_with_pricing = set()
        models_without_pricing = set()
        unknown_cost_sessions = 0
        included_cost_sessions = 0
        for s in sessions:
            model = s.get("model") or ""
            estimated, status = _estimate_cost(s)
            total_cost += estimated
            actual_cost += s.get("actual_cost_usd") or 0.0
            display = model.split("/")[-1] if "/" in model else (model or "unknown")
            if status == "included":
                included_cost_sessions += 1
            elif status == "unknown":
                unknown_cost_sessions += 1
            if _has_known_pricing(model, s.get("billing_provider"), s.get("billing_base_url")):
                models_with_pricing.add(display)
            else:
                models_without_pricing.add(display)

        # Session duration stats (guard against negative durations from clock drift)
        durations = []
        for s in sessions:
            start = s.get("started_at")
            end = s.get("ended_at")
            if start and end and end > start:
                durations.append(end - start)

        total_hours = sum(durations) / 3600 if durations else 0
        avg_duration = sum(durations) / len(durations) if durations else 0

        # Earliest and latest session
        started_timestamps = [s["started_at"] for s in sessions if s.get("started_at")]
        date_range_start = min(started_timestamps) if started_timestamps else None
        date_range_end = max(started_timestamps) if started_timestamps else None

        return {
            "total_sessions": len(sessions),
            "total_messages": total_messages,
            "total_tool_calls": total_tool_calls,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_read_tokens": total_cache_read,
            "total_cache_write_tokens": total_cache_write,
            "total_tokens": total_tokens,
            "estimated_cost": total_cost,
            "actual_cost": actual_cost,
            "total_hours": total_hours,
            "avg_session_duration": avg_duration,
            "avg_messages_per_session": total_messages / len(sessions) if sessions else 0,
            "avg_tokens_per_session": total_tokens / len(sessions) if sessions else 0,
            "user_messages": message_stats.get("user_messages") or 0,
            "assistant_messages": message_stats.get("assistant_messages") or 0,
            "tool_messages": message_stats.get("tool_messages") or 0,
            "date_range_start": date_range_start,
            "date_range_end": date_range_end,
            "models_with_pricing": sorted(models_with_pricing),
            "models_without_pricing": sorted(models_without_pricing),
            "unknown_cost_sessions": unknown_cost_sessions,
            "included_cost_sessions": included_cost_sessions,
        }

    def _compute_model_breakdown(self, sessions: List[Dict]) -> List[Dict]:
        """Break down usage by model."""
        model_data = defaultdict(lambda: {
            "sessions": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "total_tokens": 0, "tool_calls": 0, "cost": 0.0,
        })

        for s in sessions:
            model = s.get("model") or "unknown"
            # Normalize: strip provider prefix for display
            display_model = model.split("/")[-1] if "/" in model else model
            d = model_data[display_model]
            d["sessions"] += 1
            inp = s.get("input_tokens") or 0
            out = s.get("output_tokens") or 0
            cache_read = s.get("cache_read_tokens") or 0
            cache_write = s.get("cache_write_tokens") or 0
            d["input_tokens"] += inp
            d["output_tokens"] += out
            d["cache_read_tokens"] += cache_read
            d["cache_write_tokens"] += cache_write
            d["total_tokens"] += inp + out + cache_read + cache_write
            d["tool_calls"] += s.get("tool_call_count") or 0
            estimate, status = _estimate_cost(s)
            d["cost"] += estimate
            d["has_pricing"] = _has_known_pricing(model, s.get("billing_provider"), s.get("billing_base_url"))
            d["cost_status"] = status

        result = [
            {"model": model, **data}
            for model, data in model_data.items()
        ]
        # Sort by tokens first, fall back to session count when tokens are 0
        result.sort(key=lambda x: (x["total_tokens"], x["sessions"]), reverse=True)
        return result

    def _compute_platform_breakdown(self, sessions: List[Dict]) -> List[Dict]:
        """Break down usage by platform/source."""
        platform_data = defaultdict(lambda: {
            "sessions": 0, "messages": 0, "input_tokens": 0,
            "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "total_tokens": 0, "tool_calls": 0,
        })

        for s in sessions:
            source = s.get("source") or "unknown"
            d = platform_data[source]
            d["sessions"] += 1
            d["messages"] += s.get("message_count") or 0
            inp = s.get("input_tokens") or 0
            out = s.get("output_tokens") or 0
            cache_read = s.get("cache_read_tokens") or 0
            cache_write = s.get("cache_write_tokens") or 0
            d["input_tokens"] += inp
            d["output_tokens"] += out
            d["cache_read_tokens"] += cache_read
            d["cache_write_tokens"] += cache_write
            d["total_tokens"] += inp + out + cache_read + cache_write
            d["tool_calls"] += s.get("tool_call_count") or 0

        result = [
            {"platform": platform, **data}
            for platform, data in platform_data.items()
        ]
        result.sort(key=lambda x: x["sessions"], reverse=True)
        return result

    def _compute_tool_breakdown(self, tool_usage: List[Dict]) -> List[Dict]:
        """Process tool usage data into a ranked list with percentages."""
        total_calls = sum(t["count"] for t in tool_usage) if tool_usage else 0
        result = []
        for t in tool_usage:
            pct = (t["count"] / total_calls * 100) if total_calls else 0
            result.append({
                "tool": t["tool_name"],
                "count": t["count"],
                "percentage": pct,
                "success_count": t.get("success_count", 0),
                "failure_count": t.get("failure_count", 0),
            })
        return result

    def _compute_skill_breakdown(self, skill_usage: List[Dict]) -> Dict[str, Any]:
        """Process per-skill usage into summary + ranked list."""
        total_skill_loads = sum(s["view_count"] for s in skill_usage) if skill_usage else 0
        total_skill_edits = sum(s["manage_count"] for s in skill_usage) if skill_usage else 0
        total_skill_actions = total_skill_loads + total_skill_edits
        task_loads = sum(int(s.get("task_load_count") or 0) for s in skill_usage)
        slash_loads = sum(int(s.get("slash_load_count") or 0) for s in skill_usage)
        curator_loads = sum(int(s.get("curator_load_count") or 0) for s in skill_usage)
        meaningful_actions = task_loads + slash_loads + total_skill_edits

        top_skills = []
        for skill in skill_usage:
            task_count = int(skill.get("task_load_count") or 0)
            slash_count = int(skill.get("slash_load_count") or 0)
            curator_count = int(skill.get("curator_load_count") or 0)
            # Curator inspection is visible but excluded from the ranking's
            # meaningful-use total.
            total_count = task_count + slash_count + skill["manage_count"]
            percentage = (total_count / meaningful_actions * 100) if meaningful_actions else 0
            top_skills.append({
                "skill": skill["skill"],
                "view_count": skill["view_count"],
                "manage_count": skill["manage_count"],
                "task_load_count": task_count,
                "slash_load_count": slash_count,
                "curator_load_count": curator_count,
                "total_count": total_count,
                "percentage": percentage,
                "last_used_at": skill.get("last_used_at"),
            })

        top_skills.sort(
            key=lambda s: (
                s["total_count"],
                s["view_count"],
                s["manage_count"],
                s["last_used_at"] or 0,
                s["skill"],
            ),
            reverse=True,
        )

        return {
            "summary": {
                "total_skill_loads": total_skill_loads,
                "total_skill_edits": total_skill_edits,
                "total_skill_actions": total_skill_actions,
                "distinct_skills_used": len(skill_usage),
                "task_loads": task_loads,
                "slash_loads": slash_loads,
                "curator_inspections": curator_loads,
            },
            "top_skills": top_skills,
        }

    def _compute_activity_patterns(self, sessions: List[Dict]) -> Dict:
        """Analyze activity patterns by day of week and hour."""
        day_counts = Counter()  # 0=Monday ... 6=Sunday
        hour_counts = Counter()
        daily_counts = Counter()  # date string -> count

        for s in sessions:
            ts = s.get("started_at")
            if not ts:
                continue
            dt = datetime.fromtimestamp(ts)
            day_counts[dt.weekday()] += 1
            hour_counts[dt.hour] += 1
            daily_counts[dt.strftime("%Y-%m-%d")] += 1

        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        day_breakdown = [
            {"day": day_names[i], "count": day_counts.get(i, 0)}
            for i in range(7)
        ]

        hour_breakdown = [
            {"hour": i, "count": hour_counts.get(i, 0)}
            for i in range(24)
        ]

        # Busiest day and hour
        busiest_day = max(day_breakdown, key=lambda x: x["count"]) if day_breakdown else None
        busiest_hour = max(hour_breakdown, key=lambda x: x["count"]) if hour_breakdown else None

        # Active days (days with at least one session)
        active_days = len(daily_counts)

        # Streak calculation
        if daily_counts:
            all_dates = sorted(daily_counts.keys())
            current_streak = 1
            max_streak = 1
            for i in range(1, len(all_dates)):
                d1 = datetime.strptime(all_dates[i - 1], "%Y-%m-%d")
                d2 = datetime.strptime(all_dates[i], "%Y-%m-%d")
                if (d2 - d1).days == 1:
                    current_streak += 1
                    max_streak = max(max_streak, current_streak)
                else:
                    current_streak = 1
        else:
            max_streak = 0

        return {
            "by_day": day_breakdown,
            "by_hour": hour_breakdown,
            "busiest_day": busiest_day,
            "busiest_hour": busiest_hour,
            "active_days": active_days,
            "max_streak": max_streak,
        }

    def _compute_top_sessions(self, sessions: List[Dict]) -> List[Dict]:
        """Find notable sessions (longest, most messages, most tokens)."""
        top = []

        # Longest by duration
        sessions_with_duration = [
            s for s in sessions
            if s.get("started_at") and s.get("ended_at")
        ]
        if sessions_with_duration:
            longest = max(
                sessions_with_duration,
                key=lambda s: (s["ended_at"] - s["started_at"]),
            )
            dur = longest["ended_at"] - longest["started_at"]
            top.append({
                "label": "Longest session",
                "session_id": longest["id"][:16],
                "value": _format_duration(dur),
                "date": datetime.fromtimestamp(longest["started_at"]).strftime("%b %d"),
            })

        # Most messages
        most_msgs = max(sessions, key=lambda s: s.get("message_count") or 0)
        if (most_msgs.get("message_count") or 0) > 0:
            top.append({
                "label": "Most messages",
                "session_id": most_msgs["id"][:16],
                "value": f"{most_msgs['message_count']} msgs",
                "date": datetime.fromtimestamp(most_msgs["started_at"]).strftime("%b %d") if most_msgs.get("started_at") else "?",
            })

        # Most tokens
        most_tokens = max(
            sessions,
            key=lambda s: (s.get("input_tokens") or 0) + (s.get("output_tokens") or 0),
        )
        token_total = (most_tokens.get("input_tokens") or 0) + (most_tokens.get("output_tokens") or 0)
        if token_total > 0:
            top.append({
                "label": "Most tokens",
                "session_id": most_tokens["id"][:16],
                "value": f"{token_total:,} tokens",
                "date": datetime.fromtimestamp(most_tokens["started_at"]).strftime("%b %d") if most_tokens.get("started_at") else "?",
            })

        # Most tool calls
        most_tools = max(sessions, key=lambda s: s.get("tool_call_count") or 0)
        if (most_tools.get("tool_call_count") or 0) > 0:
            top.append({
                "label": "Most tool calls",
                "session_id": most_tools["id"][:16],
                "value": f"{most_tools['tool_call_count']} calls",
                "date": datetime.fromtimestamp(most_tools["started_at"]).strftime("%b %d") if most_tools.get("started_at") else "?",
            })

        return top

    # =========================================================================
    # Formatting
    # =========================================================================

    def format_terminal(self, report: Dict) -> str:
        """Format the insights report for terminal display (CLI)."""
        if report.get("empty"):
            days = report.get("days", 30)
            src = f" (source: {report['source_filter']})" if report.get("source_filter") else ""
            return f"  No sessions found in the last {days} days{src}."

        lines = []
        o = report["overview"]
        days = report["days"]
        src_filter = report.get("source_filter")

        # Header
        lines.append("")
        lines.append("  ╔══════════════════════════════════════════════════════════╗")
        lines.append("  ║                    📊 Marlow Insights                    ║")
        period_label = f"Last {days} days"
        if src_filter:
            period_label += f" ({src_filter})"
        padding = 58 - len(period_label) - 2
        left_pad = padding // 2
        right_pad = padding - left_pad
        lines.append(f"  ║{' ' * left_pad} {period_label} {' ' * right_pad}║")
        lines.append("  ╚══════════════════════════════════════════════════════════╝")
        lines.append("")

        # Date range
        if o.get("date_range_start") and o.get("date_range_end"):
            start_str = datetime.fromtimestamp(o["date_range_start"]).strftime("%b %d, %Y")
            end_str = datetime.fromtimestamp(o["date_range_end"]).strftime("%b %d, %Y")
            lines.append(f"  Period: {start_str} — {end_str}")
            lines.append("")

        # Overview
        lines.append("  📋 Overview")
        lines.append("  " + "─" * 56)
        lines.append(f"  Sessions:          {o['total_sessions']:<12}  Messages:        {o['total_messages']:,}")
        lines.append(f"  Tool calls:        {o['total_tool_calls']:<12,}  User messages:   {o['user_messages']:,}")
        lines.append(f"  Input tokens:      {o['total_input_tokens']:<12,}  Output tokens:   {o['total_output_tokens']:,}")
        lines.append(f"  Total tokens:      {o['total_tokens']:,}")
        if o["total_hours"] > 0:
            lines.append(f"  Active time:       ~{_format_duration(o['total_hours'] * 3600):<11}  Avg session:     ~{_format_duration(o['avg_session_duration'])}")
        lines.append(f"  Avg msgs/session:  {o['avg_messages_per_session']:.1f}")
        lines.append("")

        # Model breakdown
        if report["models"]:
            lines.append("  🤖 Models Used")
            lines.append("  " + "─" * 56)
            lines.append(f"  {'Model':<30} {'Sessions':>8} {'Tokens':>12}")
            for m in report["models"]:
                model_name = m["model"][:28]
                lines.append(f"  {model_name:<30} {m['sessions']:>8} {m['total_tokens']:>12,}")
            lines.append("")

        # Platform breakdown
        if len(report["platforms"]) > 1 or (report["platforms"] and report["platforms"][0]["platform"] != "cli"):
            lines.append("  📱 Platforms")
            lines.append("  " + "─" * 56)
            lines.append(f"  {'Platform':<14} {'Sessions':>8} {'Messages':>10} {'Tokens':>14}")
            for p in report["platforms"]:
                lines.append(f"  {p['platform']:<14} {p['sessions']:>8} {p['messages']:>10,} {p['total_tokens']:>14,}")
            lines.append("")

        # Tool usage
        if report["tools"]:
            lines.append("  🔧 Top Tools")
            lines.append("  " + "─" * 56)
            lines.append(f"  {'Tool':<28} {'Calls':>8} {'%':>8}")
            for t in report["tools"][:15]:  # Top 15
                lines.append(f"  {t['tool']:<28} {t['count']:>8,} {t['percentage']:>7.1f}%")
            if len(report["tools"]) > 15:
                lines.append(f"  ... and {len(report['tools']) - 15} more tools")
            lines.append("")

        mcp = report.get("mcp", {})
        mcp_summary = mcp.get("summary", {})
        if mcp_summary.get("total_calls"):
            lines.append("  🔌 MCP Usage")
            lines.append("  " + "─" * 56)
            lines.append(
                f"  Calls: {mcp_summary.get('total_calls', 0):,}  "
                f"Servers: {mcp_summary.get('distinct_servers', 0)}  "
                f"Tools: {mcp_summary.get('distinct_tools', 0)}  "
                f"Tracked failures: {mcp_summary.get('failed_calls', 0):,}"
            )
            if mcp.get("top_servers"):
                lines.append("  Top servers: " + ", ".join(
                    f"{row['server']} ({row['count']})"
                    for row in mcp["top_servers"][:5]
                ))
            if mcp.get("top_tools"):
                lines.append("  Top MCP tools:")
                for row in mcp["top_tools"][:5]:
                    lines.append(
                        f"    {row['tool'][:34]:<34} {row['count']:>7,}  [{row['server']}]"
                    )
            lines.append("")

        # Skill usage
        skills = report.get("skills", {})
        top_skills = skills.get("top_skills", [])
        if top_skills:
            lines.append("  🧠 Top Skills")
            lines.append("  " + "─" * 56)
            lines.append(f"  {'Skill':<28} {'Loads':>7} {'Edits':>7} {'Last used':>11}")
            for skill in top_skills[:10]:
                last_used = "—"
                if skill.get("last_used_at"):
                    last_used = datetime.fromtimestamp(skill["last_used_at"]).strftime("%b %d")
                lines.append(
                    f"  {skill['skill'][:28]:<28} {skill['view_count']:>7,} {skill['manage_count']:>7,} {last_used:>11}"
                )
            summary = skills.get("summary", {})
            lines.append(
                f"  Distinct skills: {summary.get('distinct_skills_used', 0)}  "
                f"Loads: {summary.get('total_skill_loads', 0):,}  "
                f"Edits: {summary.get('total_skill_edits', 0):,}"
            )
            if summary.get("slash_loads") or summary.get("curator_inspections"):
                lines.append(
                    f"  Task loads: {summary.get('task_loads', 0):,}  "
                    f"Slash loads: {summary.get('slash_loads', 0):,}  "
                    f"Historical inspections: {summary.get('curator_inspections', 0):,}"
                )
            lines.append("")

        memory = report.get("memory", {})
        memory_summary = memory.get("summary", {})
        if any(memory_summary.values()):
            lines.append("  🧠 Memory Recall")
            lines.append("  " + "─" * 56)
            lines.append(
                f"  Attempts: {memory_summary.get('recall_attempts', 0):,}  "
                f"Hits: {memory_summary.get('recall_hits', 0):,}  "
                f"Hit rate: {memory_summary.get('hit_rate', 0.0):.1f}%  "
                f"Injections: {memory_summary.get('context_injections', 0):,}"
            )
            if memory_summary.get("writes") or memory_summary.get("explicit_calls"):
                lines.append(
                    f"  Writes: {memory_summary.get('writes', 0):,}  "
                    f"Explicit memory calls: {memory_summary.get('explicit_calls', 0):,}"
                )
            for row in memory.get("top_providers", [])[:5]:
                lines.append(
                    f"    {row['provider'][:24]:<24} attempts={row['attempts']:<5,} "
                    f"hits={row['hits']:<5,} injected={row['injections']:<5,}"
                )
            lines.append("")

        experience = report.get("experience", {})
        experience_summary = experience.get("summary", {})
        if any(experience_summary.values()):
            lines.append("  🧭 Work Experience")
            lines.append("  " + "─" * 56)
            lines.append(
                f"  Retrievals: {experience_summary.get('retrieval_attempts', 0):,}  "
                f"With matches: {experience_summary.get('attempts_with_matches', 0):,}  "
                f"Lessons returned: {experience_summary.get('lessons_returned', 0):,}"
            )
            lines.append(
                f"  Context injections: {experience_summary.get('context_injections', 0):,}  "
                f"Lessons injected: {experience_summary.get('lessons_injected', 0):,}"
            )
            if experience.get("top_lessons"):
                lines.append("  Top recalled lessons:")
                for row in experience["top_lessons"][:5]:
                    lines.append(
                        f"    {row['item_id'][:36]:<36} {row['recall_count']:>7,}"
                    )
            lines.append("  Recall is diagnostic; it does not prove application or benefit.")
            lines.append("")

        tracking_started = report.get("usage_tracking_started_at")
        if tracking_started:
            lines.append(
                "  Detailed source/recall tracking since "
                + datetime.fromtimestamp(tracking_started).strftime("%b %d, %Y %H:%M")
            )
            lines.append("")

        # Activity patterns
        act = report.get("activity", {})
        if act.get("by_day"):
            lines.append("  📅 Activity Patterns")
            lines.append("  " + "─" * 56)

            # Day of week chart
            day_values = [d["count"] for d in act["by_day"]]
            bars = _bar_chart(day_values, max_width=15)
            for i, d in enumerate(act["by_day"]):
                bar = bars[i]
                lines.append(f"  {d['day']}  {bar:<15} {d['count']}")

            lines.append("")

            # Peak hours (show top 5 busiest hours)
            busy_hours = sorted(act["by_hour"], key=lambda x: x["count"], reverse=True)
            busy_hours = [h for h in busy_hours if h["count"] > 0][:5]
            if busy_hours:
                hour_strs = []
                for h in busy_hours:
                    hr = h["hour"]
                    ampm = "AM" if hr < 12 else "PM"
                    display_hr = hr % 12 or 12
                    hour_strs.append(f"{display_hr}{ampm} ({h['count']})")
                lines.append(f"  Peak hours: {', '.join(hour_strs)}")

            if act.get("active_days"):
                lines.append(f"  Active days: {act['active_days']}")
            if act.get("max_streak") and act["max_streak"] > 1:
                lines.append(f"  Best streak: {act['max_streak']} consecutive days")
            lines.append("")

        # Notable sessions
        if report.get("top_sessions"):
            lines.append("  🏆 Notable Sessions")
            lines.append("  " + "─" * 56)
            for ts in report["top_sessions"]:
                lines.append(f"  {ts['label']:<20} {ts['value']:<18} ({ts['date']}, {ts['session_id']})")
            lines.append("")

        return "\n".join(lines)

    def format_gateway(self, report: Dict) -> str:
        """Format the insights report for gateway/messaging (shorter)."""
        if report.get("empty"):
            days = report.get("days", 30)
            return f"No sessions found in the last {days} days."

        lines = []
        o = report["overview"]
        days = report["days"]

        lines.append(f"📊 **Marlow Insights** — Last {days} days\n")

        # Overview
        lines.append(f"**Sessions:** {o['total_sessions']} | **Messages:** {o['total_messages']:,} | **Tool calls:** {o['total_tool_calls']:,}")
        lines.append(f"**Tokens:** {o['total_tokens']:,} (in: {o['total_input_tokens']:,} / out: {o['total_output_tokens']:,})")
        if o["total_hours"] > 0:
            lines.append(f"**Active time:** ~{_format_duration(o['total_hours'] * 3600)} | **Avg session:** ~{_format_duration(o['avg_session_duration'])}")
        lines.append("")

        # Models (top 5)
        if report["models"]:
            lines.append("**🤖 Models:**")
            for m in report["models"][:5]:
                lines.append(f"  {m['model'][:25]} — {m['sessions']} sessions, {m['total_tokens']:,} tokens")
            lines.append("")

        # Platforms (if multi-platform)
        if len(report["platforms"]) > 1:
            lines.append("**📱 Platforms:**")
            for p in report["platforms"]:
                lines.append(f"  {p['platform']} — {p['sessions']} sessions, {p['messages']:,} msgs")
            lines.append("")

        # Tools (top 8)
        if report["tools"]:
            lines.append("**🔧 Top Tools:**")
            for t in report["tools"][:8]:
                lines.append(f"  {t['tool']} — {t['count']:,} calls ({t['percentage']:.1f}%)")
            lines.append("")

        mcp = report.get("mcp", {})
        if mcp.get("summary", {}).get("total_calls"):
            ms = mcp["summary"]
            lines.append(
                f"**🔌 MCP:** {ms['total_calls']:,} calls across "
                f"{ms['distinct_servers']} servers / {ms['distinct_tools']} tools"
            )
            for row in mcp.get("top_servers", [])[:3]:
                lines.append(f"  {row['server']} — {row['count']:,} calls")
            lines.append("")

        skills = report.get("skills", {})
        if skills.get("top_skills"):
            lines.append("**🧠 Top Skills:**")
            for skill in skills["top_skills"][:5]:
                suffix = ""
                if skill.get("last_used_at"):
                    suffix = f", last used {datetime.fromtimestamp(skill['last_used_at']).strftime('%b %d')}"
                lines.append(
                    f"  {skill['skill']} — {skill['view_count']:,} loads, {skill['manage_count']:,} edits{suffix}"
                )
            lines.append("")

        memory = report.get("memory", {})
        if any(memory.get("summary", {}).values()):
            ms = memory["summary"]
            lines.append(
                f"**🧠 Memory:** {ms['recall_hits']:,}/{ms['recall_attempts']:,} "
                f"recall hits ({ms['hit_rate']:.1f}%), {ms['context_injections']:,} injections"
            )
            lines.append("")

        experience = report.get("experience", {})
        if any(experience.get("summary", {}).values()):
            es = experience["summary"]
            lines.append(
                f"**🧭 Work Experience:** {es['retrieval_attempts']:,} retrievals, "
                f"{es['lessons_returned']:,} lessons returned, "
                f"{es['context_injections']:,} injections"
            )
            lines.append("")

        # Activity summary
        act = report.get("activity", {})
        if act.get("busiest_day") and act.get("busiest_hour"):
            hr = act["busiest_hour"]["hour"]
            ampm = "AM" if hr < 12 else "PM"
            display_hr = hr % 12 or 12
            lines.append(f"**📅 Busiest:** {act['busiest_day']['day']}s ({act['busiest_day']['count']} sessions), {display_hr}{ampm} ({act['busiest_hour']['count']} sessions)")
            if act.get("active_days"):
                lines.append(f"**Active days:** {act['active_days']}", )
            if act.get("max_streak", 0) > 1:
                lines.append(f"**Best streak:** {act['max_streak']} consecutive days")

        return "\n".join(lines)

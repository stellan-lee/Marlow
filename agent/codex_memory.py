"""Preserved Marlow memory lifecycle; no inference loop."""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MemoryLifecycle:
    def _build_memory_write_metadata(
        self,
        *,
        write_origin: Optional[str] = None,
        execution_context: Optional[str] = None,
        task_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Forwarder — see ``agent.background_review.build_memory_write_metadata``."""
        from agent.background_review import build_memory_write_metadata

        return build_memory_write_metadata(
            self,
            write_origin=write_origin,
            execution_context=execution_context,
            task_id=task_id,
            tool_call_id=tool_call_id,
        )

    def shutdown_memory_provider(self, messages: list = None) -> None:
        """Shut down the memory providers — call at actual session boundaries.

        This calls on_session_end() then shutdown_all() on the memory
        manager.
        NOT called per-turn — only at CLI exit, /reset, gateway
        session expiry, etc.
        """
        if self._memory_manager:
            try:
                self._memory_manager.on_session_end(messages or [])
            except Exception:
                pass
            try:
                self._memory_manager.shutdown_all()
            except Exception:
                pass
        # Notify context engine of session end (flush DAG, close DBs, etc.)

    def commit_memory_session(self, messages: list = None) -> None:
        """Trigger end-of-session extraction without tearing providers down.
        Called when session_id rotates (e.g. /new, context compression);
        providers keep their state and continue running under the old
        session_id — they just flush pending extraction now."""
        if self._memory_manager:
            try:
                self._memory_manager.on_session_end(messages or [])
            except Exception:
                pass
        # Notify context engine of session end too — same lifecycle moment as
        # the memory manager's on_session_end. Without this, engines that
        # accumulate per-session state (DAGs, summaries) leak that state from
        # the rotated-out session into whatever comes next under the same
        # compressor instance. Mirrors the call in shutdown_memory_provider().
        # See issue #22394.

    def _sync_external_memory_for_turn(
        self,
        *,
        original_user_message: Any,
        final_response: Any,
        interrupted: bool,
        messages: list | None = None,
    ) -> None:
        """Mirror a completed turn into external memory providers.

        Called at the end of ``run_conversation`` with the cleaned user
        message (``original_user_message``) and the finalised assistant
        response.  The external memory backend gets ``sync_all`` to
        persist the exchange.  The legacy post-turn ``queue_prefetch_all``
        warmer is disabled by default because it can only warm the query
        that just finished, not the user's next unknown query.

        Uses ``original_user_message`` rather than ``user_message``
        because the latter may carry injected skill content that bloats
        or breaks provider queries.

        Interrupted turns are skipped entirely (#15218).  A partial
        assistant output, an aborted tool chain, or a mid-stream reset
        is not durable conversational truth — mirroring it into an
        external memory backend pollutes future recall with state the
        user never saw completed.  The prefetch is gated on the same
        flag: the user's next message is almost certainly a retry of
        the same intent, and a prefetch keyed on the interrupted turn
        would fire against stale context.

        Normal completed turns still sync as before.  The whole body is
        wrapped in ``try/except Exception`` because external memory
        providers are strictly best-effort — a misconfigured or offline
        backend must not block the user from seeing their response.
        """
        if (
            interrupted
            or getattr(self, "_conversation_persistence_policy", "session") == "none"
        ):
            return
        # The consolidation evidence ledger is independent of an external
        # memory provider.  When explicitly enabled, append only completed
        # user/assistant turns with a deterministic source key; failures remain
        # best-effort and never block the response path.
        try:
            from marlow_cli.config import load_config as _load_memory_config

            _memory_cfg = _load_memory_config() or {}
            _consolidation_cfg = (
                _memory_cfg.get("memory", {}).get("consolidation", {})
                if isinstance(_memory_cfg, dict)
                else {}
            )
            if (
                _consolidation_cfg.get("enabled")
                and final_response
                and original_user_message
            ):
                from agent.memory_consolidation_runner import (
                    append_conversation_evidence,
                )

                _scope_type = "user" if getattr(self, "_user_id", "") else "profile"
                _scope_id = getattr(self, "_user_id", "") or "local-owner"
                append_conversation_evidence(
                    scope_id=str(_scope_id),
                    scope_type=_scope_type,
                    session_id=str(getattr(self, "session_id", "") or ""),
                    user_content=original_user_message,
                    assistant_content=final_response,
                    turn_id=getattr(self, "_user_turn_count", None),
                    tool_events=messages,
                )
        except Exception:
            pass
        if not (self._memory_manager and final_response and original_user_message):
            return
        try:
            sync_kwargs = {"session_id": self.session_id or ""}
            if messages is not None:
                sync_kwargs["messages"] = messages
            self._memory_manager.sync_all(
                original_user_message,
                final_response,
                **sync_kwargs,
            )
            if getattr(self, "_memory_post_turn_prefetch_enabled", False):
                self._memory_manager.queue_prefetch_all(
                    original_user_message,
                    session_id=self.session_id or "",
                )
        except Exception:
            pass

        # Structured memory cards (PR4): after the normal turn sync (which has
        # already run/failed-open above), distil a few deterministic cards from
        # the completed turn and write them for FUTURE recall. Default off.
        # Wrapped in its own guard so card extraction/sync can never undo the
        # sync_all above or block the user. Recall-only: this does NOT queue a
        # prefetch, touch the current model call, or modify conversation
        # history.
        if getattr(self, "_memory_structured_cards_enabled", False):
            try:
                from agent.memory_cards import extract_memory_cards

                cards = extract_memory_cards(
                    original_user_message,
                    final_response,
                    session_id=self.session_id or "",
                    max_cards=getattr(self, "_memory_structured_cards_max_per_turn", 5),
                    max_chars=getattr(self, "_memory_structured_cards_max_chars", 2500),
                )
                # PR5: append-only supersession/conflict resolution. Runs AFTER
                # extraction but BEFORE writing the new cards, so candidate
                # lookup can't match the new cards against themselves. Fail-open:
                # any error returns the original cards (still written, no
                # supersession metadata). Never queues prefetch, never injects
                # into the current turn, never deletes old provider memories.
                if cards and getattr(
                    self, "_memory_structured_conflict_resolution_enabled", False
                ):
                    cards = self._resolve_structured_card_conflicts(cards)
                if cards:
                    self._memory_manager.sync_structured_cards_all(
                        cards,
                        session_id=self.session_id or "",
                        fallback_sync_turn_enabled=getattr(
                            self,
                            "_memory_structured_cards_fallback_sync_turn_enabled",
                            True,
                        ),
                    )
            except Exception:
                pass

    def _resolve_structured_card_conflicts(self, cards: list) -> list:
        """PR5 — detect deterministic supersessions and append marker cards.

        Returns ``updated_new_cards + superseded_marker_cards`` (append-only;
        old provider memories are never touched). Fail-open: on any error the
        original ``cards`` are returned unchanged so they are still written.
        Logs only safe metadata (counts), never raw card/candidate text.
        """
        try:
            from agent.memory_card_conflicts import (
                build_candidate_query,
                resolve_card_conflicts,
            )
            from agent.memory_cards import MemoryCardType

            sid = self.session_id or ""
            supersedable = {
                MemoryCardType.DECISION,
                MemoryCardType.PREFERENCE,
                MemoryCardType.CONSTRAINT,
                MemoryCardType.TODO,
            }
            candidate_texts: list = []
            seen_queries: set = set()
            for card in cards:
                if card.type not in supersedable:
                    continue
                try:
                    query = build_candidate_query(card)
                    # Cards on the same topic produce identical queries — look
                    # each unique query up only once to bound post-turn latency.
                    if query in seen_queries:
                        continue
                    seen_queries.add(query)
                    text = self._memory_manager.lookup_structured_card_candidates(
                        query, session_id=sid
                    )
                    if text:
                        candidate_texts.append(text)
                except Exception:
                    continue  # per-card fail-open
            if not candidate_texts:
                return cards

            result = resolve_card_conflicts(
                cards,
                candidate_texts,
                require_explicit_override=getattr(
                    self,
                    "_memory_structured_conflict_require_explicit_override",
                    True,
                ),
                min_entity_overlap=getattr(
                    self, "_memory_structured_conflict_min_entity_overlap", 1
                ),
                max_candidates=getattr(
                    self, "_memory_structured_conflict_max_candidates", 8
                ),
            )
            logger.debug(
                "structured card conflict resolution: new_cards=%d "
                "candidates_parsed=%d superseded=%d markers=%d",
                len(cards),
                result.candidates_parsed,
                result.superseded_count,
                result.marker_count,
            )
            return result.updated_new_cards + result.superseded_marker_cards
        except Exception as e:
            logger.debug(
                "structured card conflict resolution failed (non-fatal): %s",
                type(e).__name__,
            )
            return cards

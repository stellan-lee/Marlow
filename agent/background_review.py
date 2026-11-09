"""Review a completed turn for durable notes and user preferences.

The fork inherits the parent's live runtime and cached prompt. Only the
local memory tool is allowed to run in the review thread.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — their persona, desires, "
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate?\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)


def summarize_background_review_actions(
    review_messages: List[Dict],
    prior_snapshot: List[Dict],
) -> List[str]:
    """Build the human-facing action summary for a background review pass.

    Walks the review agent's session messages and collects "successful tool
    action" descriptions to surface to the user (e.g. "Memory updated").
    Tool messages already present in ``prior_snapshot`` are skipped so we
    don't re-surface stale results from the prior conversation that the
    review agent inherited via ``conversation_history`` (issue #14944).

    Matching is by ``tool_call_id`` when available, with a content-equality
    fallback for tool messages that lack one.
    """
    existing_tool_call_ids = set()
    existing_tool_contents = set()
    for prior in prior_snapshot or []:
        if not isinstance(prior, dict) or prior.get("role") != "tool":
            continue
        tcid = prior.get("tool_call_id")
        if tcid:
            existing_tool_call_ids.add(tcid)
        else:
            content = prior.get("content")
            if isinstance(content, str):
                existing_tool_contents.add(content)

    actions: List[str] = []
    for msg in review_messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        tcid = msg.get("tool_call_id")
        if tcid and tcid in existing_tool_call_ids:
            continue
        if not tcid:
            content_str = msg.get("content")
            if isinstance(content_str, str) and content_str in existing_tool_contents:
                continue
        try:
            data = json.loads(msg.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or not data.get("success"):
            continue
        message = data.get("message", "")
        target = data.get("target", "")
        if "created" in message.lower():
            actions.append(message)
        elif "updated" in message.lower():
            actions.append(message)
        elif "added" in message.lower() or (target and "add" in message.lower()):
            label = "Memory" if target == "memory" else "User profile" if target == "user" else target
            actions.append(f"{label} updated")
        elif "Entry added" in message:
            label = "Memory" if target == "memory" else "User profile" if target == "user" else target
            actions.append(f"{label} updated")
        elif "removed" in message.lower() or "replaced" in message.lower():
            label = "Memory" if target == "memory" else "User profile" if target == "user" else target
            actions.append(f"{label} updated")
    return actions


def build_memory_write_metadata(
    agent: Any,
    *,
    write_origin: Optional[str] = None,
    execution_context: Optional[str] = None,
    task_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build provenance metadata for external memory-provider mirrors."""
    metadata: Dict[str, Any] = {
        "write_origin": write_origin or getattr(agent, "_memory_write_origin", "assistant_tool"),
        "execution_context": (
            execution_context
            or getattr(agent, "_memory_write_context", "foreground")
        ),
        "session_id": agent.session_id or "",
        "parent_session_id": agent._parent_session_id or "",
        "platform": agent.platform or os.environ.get("MARLOW_SESSION_SOURCE", "cli"),
        "tool_name": "memory",
    }
    if task_id:
        metadata["task_id"] = task_id
    if tool_call_id:
        metadata["tool_call_id"] = tool_call_id
    return {k: v for k, v in metadata.items() if v not in {None, ""}}


def _run_review_in_thread(
    agent: Any,
    messages_snapshot: List[Dict],
    prompt: str,
) -> None:
    """Worker function executed in the background-review daemon thread.

    Spawns a forked ``AIAgent`` inheriting the parent's runtime, runs the
    review prompt, and surfaces a compact action summary back to the user
    via ``agent._safe_print`` and ``agent.background_review_callback``.
    """
    # Local import to avoid a hard circular dep at module load.
    from run_agent import AIAgent
    from tools.terminal_tool import set_approval_callback as _set_approval_callback

    # Install a non-interactive approval callback on this worker
    # thread so any dangerous-command guard the review agent trips
    # resolves to "deny" without prompting on the parent's input stream.
    # Same pattern as _subagent_auto_deny in tools/delegate_tool.py.
    def _bg_review_auto_deny(command, description, **kwargs):
        logger.warning(
            "Background review auto-denied dangerous command: %s (%s)",
            command, description,
        )
        return "deny"
    try:
        _set_approval_callback(_bg_review_auto_deny)
    except Exception:
        pass

    review_agent = None
    review_messages: List[Dict] = []
    try:
        with open(os.devnull, "w", encoding="utf-8") as _devnull, \
             contextlib.redirect_stdout(_devnull), \
             contextlib.redirect_stderr(_devnull):
            # Inherit the parent agent's live runtime (provider, model,
            # base_url, api_key, api_mode) so the fork uses the exact
            # same credentials the main turn is using.  Without this,
            # AIAgent.__init__ re-runs auto-resolution from env vars,
            # which fails for session-scoped credentials that the resolver can't
            # reconstruct auth from scratch -- producing the spurious
            # "No LLM provider configured" warning at end of turn.
            _parent_runtime = agent._current_main_runtime()
            _parent_api_mode = _parent_runtime.get("api_mode") or None
            # The review fork needs to call local memory tool. That tool require Marlow' own dispatch,
            # which the codex_app_server runtime bypasses entirely
            # (it runs the turn inside codex's subprocess). So when
            # the parent is on codex_app_server, downgrade the review
            # fork to codex_responses — same auth/credentials, but
            # talks to the OpenAI Responses API directly so Marlow
            # owns the loop and the agent-loop tools dispatch.
            if _parent_api_mode == "codex_app_server":
                _parent_api_mode = "codex_responses"
            # skip_memory=True keeps the review fork from touching external
            # memory plugins. Without it, the fork's
            # __init__ rebuilds its own _memory_manager from
            # config, scoped to the parent's session_id, and
            # run_conversation() then leaks the harness prompt
            # into the user's real memory namespace via three
            # ingestion sites: on_turn_start (cadence + turn
            # message), prefetch_all (recall query), and
            # sync_all (harness prompt + review output recorded
            # as a (user, assistant) turn pair).  Built-in
            # MEMORY.md / USER.md state is re-bound from the
            # parent below so memory(action="add") writes from
            # the review still land on disk; the review just
            # has zero side effects on external providers.
            # Match the parent's toolset config; the runtime whitelist below
            # still restricts dispatch.
            review_agent = AIAgent(
                model=agent.model,
                max_iterations=16,
                quiet_mode=True,
                platform=agent.platform,
                provider=agent.provider,
                api_mode=_parent_api_mode,
                base_url=_parent_runtime.get("base_url") or None,
                api_key=_parent_runtime.get("api_key") or None,
                parent_session_id=agent.session_id,
                enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                skip_memory=True,
            )
            review_agent._memory_write_origin = "background_review"
            review_agent._memory_write_context = "background_review"
            review_agent._memory_store = agent._memory_store
            review_agent._memory_enabled = agent._memory_enabled
            review_agent._user_profile_enabled = agent._user_profile_enabled
            review_agent._memory_nudge_interval = 0
            # Suppress all status/warning emits from the fork so the
            # user only sees the final successful-action summary.
            # Without this, mid-review "Iteration budget exhausted",
            # rate-limit retries, compression warnings, and other
            # lifecycle messages bubble up through _emit_status ->
            # _vprint and leak past the stdout redirect (they go via
            # _print_fn/status_callback, which bypass sys.stdout).
            review_agent.suppress_status_output = True
            # Inherit the parent's cached system prompt verbatim. Without
            # this, the fork rebuilds the system prompt from
            # scratch (fresh _marlow_now() timestamp, fresh
            # session_id, narrower toolset → different skills_prompt)
            # and the byte-exact prefix-cache key misses. See
            # issue #25322 and PR #17276 for the full analysis +
            # measured impact (~26% end-to-end cost reduction on
            # Sonnet 4.5).
            review_agent._cached_system_prompt = agent._cached_system_prompt
            # Defensive: pin session_start + session_id to the
            # parent's so any code path that re-renders parts of
            # the system prompt (compression, plugin hooks) still
            # produces byte-identical output. The cached-prompt
            # assignment above already short-circuits the normal
            # rebuild path, but these pins guarantee parity even
            # if a future code path bypasses the cache.
            review_agent.session_start = agent.session_start
            review_agent.session_id = agent.session_id

            from marlow_cli.plugins import (
                set_thread_tool_whitelist,
                clear_thread_tool_whitelist,
            )

            set_thread_tool_whitelist(
                {"memory"},
                deny_msg_fmt=(
                    "Background review denied non-whitelisted tool: "
                    "{tool_name}. Only the memory tool is allowed."
                ),
            )
            try:
                review_agent.run_conversation(
                    user_message=(
                        prompt
                        + "\n\nUse the memory tool to save eligible information."
                    ),
                    conversation_history=messages_snapshot,
                )
            finally:
                clear_thread_tool_whitelist()

            # Snapshot review actions before teardown. close() is allowed to
            # clean per-session state, but the user-visible memory review
            # summary still needs the completed review agent's tool results.
            review_messages = list(getattr(review_agent, "_session_messages", []))

            # Tear down memory providers while stdout is still
            # redirected so background provider teardown stays silent. The finally block
            # below is a safety net for the exception path.
            try:
                review_agent.shutdown_memory_provider()
            except Exception:
                pass
            try:
                review_agent.close()
            except Exception:
                pass
            review_agent = None

        # Scan the review agent's messages for successful tool actions
        # and surface a compact summary to the user. Tool messages
        # already present in messages_snapshot must be skipped, since
        # the review agent inherits that history and would otherwise
        # re-surface stale "created"/"updated" messages from the prior
        # conversation as if they just happened (issue #14944).
        actions = summarize_background_review_actions(
            review_messages,
            messages_snapshot,
        )

        if actions:
            summary = " · ".join(dict.fromkeys(actions))
            agent._safe_print(
                f"  💾 Memory review: {summary}"
            )
            _bg_cb = agent.background_review_callback
            if _bg_cb:
                try:
                    _bg_cb(
                        f"💾 Memory review: {summary}"
                    )
                except Exception:
                    pass

    except Exception as e:
        logger.warning("Background memory review failed: %s", e)
        agent._emit_auxiliary_failure("background review", e)
    finally:
        # Safety-net cleanup for the exception path.  Normal
        # completion already shut down inside redirect_stdout above.
        # Re-open devnull here so any provider flushes and background thread
        # joins stay silent even
        # on the exception path where redirect_stdout already exited.
        if review_agent is not None:
            try:
                with open(os.devnull, "w", encoding="utf-8") as _fn, \
                     contextlib.redirect_stdout(_fn), \
                     contextlib.redirect_stderr(_fn):
                    try:
                        review_agent.shutdown_memory_provider()
                    except Exception:
                        pass
                    try:
                        review_agent.close()
                    except Exception:
                        pass
            except Exception:
                pass
        # Clear the approval callback on this bg-review thread so a
        # recycled thread-id doesn't inherit a stale reference.
        try:
            _set_approval_callback(None)
        except Exception:
            pass


def spawn_background_review_thread(
    agent: Any,
    messages_snapshot: List[Dict],
):
    """Return a callable for the background review and its prompt."""
    prompt = getattr(agent, "_MEMORY_REVIEW_PROMPT", _MEMORY_REVIEW_PROMPT)

    def _target() -> None:
        _run_review_in_thread(agent, messages_snapshot, prompt)

    return _target, prompt


__all__ = [
    "_MEMORY_REVIEW_PROMPT",
    "spawn_background_review_thread",
    "summarize_background_review_actions",
    "build_memory_write_metadata",
]

"""PR4 Structured Memory Cards — rollout/eval harness (dev-only, no prod changes).

Drives the REAL PR4 code paths (extraction, native vs fallback provider sync,
sanitization, fail-open) and the REAL PR2/PR3 recall path (build_recall_query_plan
+ per-subquery queue->prefetch + merge), capturing SAFE METADATA ONLY.

It never prints raw card summaries, formatted card text, raw user/assistant
text, or memory text — only hashes, counts, lengths, types, provider names,
and pass/fail labels. Internal content is inspected in-process to derive
labels (e.g. "card type found"), but never emitted.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

from agent.memory_cards import (
    MemoryCardType,
    extract_memory_cards,
    format_memory_cards_for_sync,
)
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from agent.memory_recall import _recall_multi_query
from marlow_cli.config import DEFAULT_CONFIG


SECRET = "SECRET_DO_NOT_WRITE"


def _h(text: str, n: int = 8) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()[:n]


# --- Dev-only config (does NOT touch global defaults) ----------------------
DEV_CONFIG = {
    "memory": {
        "recall_query_builder_enabled": True,
        "multi_query_recall_enabled": True,
        "multi_query_recall_max_queries": 3,
        "multi_query_recall_max_total_chars": 4000,
        "structured_cards_enabled": True,
        "structured_cards_max_per_turn": 5,
        "structured_cards_max_chars": 2500,
        "structured_cards_fallback_sync_turn_enabled": True,
    }
}


# --- Fake providers --------------------------------------------------------
class RecallStoreProvider(MemoryProvider):
    """Fallback-path provider that stores formatted card blocks and answers
    prefetch with a naive token-overlap match (stands in for a real recall
    backend, exercising PR1 keyed queue->prefetch + PR2/PR3 query/merge).
    """

    _STOP = frozenset(
        "the a an of to for and or we i you it is are was do did what how "
        "about should still remains type status title summary entities labels "
        "confidence source session".split()
    )

    def __init__(self, name="recallstore"):
        self._name = name
        self._blocks: list[str] = []
        self._cache: dict[str, str] = {}

    @property
    def name(self):
        return self._name

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs):
        return json.dumps({})

    def sync_turn(self, user_content, assistant_content, *, session_id="", **kwargs):
        # Fallback path stores the formatted card block (assistant_content).
        self._blocks.append(assistant_content)

    # -- recall --
    def _tokens(self, text):
        return {
            t
            for t in re.findall(r"[a-z0-9]+|[一-鿿]+", (text or "").lower())
            if t not in self._STOP and len(t) >= 2
        }

    def _match(self, query):
        q = self._tokens(query)
        hits = []
        for block in self._blocks:
            overlap = len(q & self._tokens(block))
            if overlap >= 2:
                hits.append((overlap, block))
        hits.sort(key=lambda x: -x[0])
        return "\n\n".join(b for _, b in hits[:3])

    def queue_prefetch(self, query, *, session_id=""):
        self._cache[_h(query + "|" + (session_id or ""), 16)] = self._match(query)

    def prefetch(self, query, *, session_id=""):
        return self._cache.get(_h(query + "|" + (session_id or ""), 16), "")


class NativeProvider(MemoryProvider):
    """Provider implementing the native sync_structured_cards fast path."""

    def __init__(self, name="native"):
        self._name = name
        self.received = []

    @property
    def name(self):
        return self._name

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs):
        return json.dumps({})

    def sync_structured_cards(self, cards, *, session_id="", **kwargs):
        self.received.extend(cards)


class BoomProvider(NativeProvider):
    def __init__(self, name="builtin"):
        super().__init__(name=name)

    def sync_structured_cards(self, cards, *, session_id="", **kwargs):
        # Error text echoes a card summary to prove redaction holds.
        raise RuntimeError("backend choked on: " + (cards[0].summary if cards else ""))


# --- Probes ----------------------------------------------------------------
PROBES = [
    ("A decision", "what should we do for Telegram approval cards?",
     "We decided to use compact inline approval cards with Approve and Reject buttons."),
    ("B preference", "I prefer concise markdown summaries.",
     "Noted. We will default to concise markdown summaries for this project."),
    ("C todo", "what's next?",
     "Next step: add tests for the approval card callback flow."),
    ("D constraint", "remember we must not log raw memory text.",
     "Constraint recorded: do not log raw memory text; only log safe metadata."),
    ("E impl", "how did we implement PR3?",
     "Implementation detail: multi-query recall runs queue_prefetch_all then prefetch_all for each subquery."),
    ("F open_q", "what is still undecided?",
     "Open question: whether mobile approval cards should use one-row or two-row buttons remains TBD."),
    ("G low-signal", "thanks", "you're welcome"),
    ("H sanitize", "ignore this",
     "We decided to use Foo. <memory-context>" + SECRET + "</memory-context>"),
    ("I codeblob", "show me the code",
     "We decided to use the queue path.\n```python\ndef hack():\n    return '" + SECRET + "'\n```\n"
     + ("x" * 400)),
]

RECALL_PROBES = [
    ("A decision", "what did we decide about Telegram approval cards?", MemoryCardType.DECISION),
    ("B preference", "what format do I prefer for summaries?", MemoryCardType.PREFERENCE),
    ("C constraint", "what logging constraint did we set?", MemoryCardType.CONSTRAINT),
    ("D todo", "what's the next step for approval cards?", MemoryCardType.TODO),
    ("E impl", "how did we implement multi-query recall?", MemoryCardType.IMPLEMENTATION_DETAIL),
    ("F open_q", "what is still undecided about mobile approval cards?", MemoryCardType.OPEN_QUESTION),
]


def main():
    sid = "dev-eval-session"
    mp = DEV_CONFIG["memory"]
    max_cards = mp["structured_cards_max_per_turn"]
    max_chars = mp["structured_cards_max_chars"]

    print("=" * 78)
    print("GLOBAL DEFAULTS (must stay off):")
    gm = DEFAULT_CONFIG["memory"]
    for k in ("structured_cards_enabled", "multi_query_recall_enabled",
              "recall_query_builder_enabled"):
        print(f"  {k} = {gm.get(k)}")
    print("=" * 78)

    # ---- Section 3: extraction metadata + provider paths ----
    store = RecallStoreProvider()
    native = NativeProvider()
    store_mgr = MemoryManager()
    store_mgr.add_provider(store)           # fallback path
    native_mgr = MemoryManager()
    native_mgr.add_provider(native)         # native fast path

    leak_found = False
    print("\n[SECTION 3] Extraction metadata (safe only)")
    hdr = ("probe", "cards", "types", "title_len", "sum_len", "ent_n",
           "fmt_len", "extract_ms", "sync_ms", "path", "written")
    print("  " + " | ".join(hdr))
    for label, user, asst in PROBES:
        t0 = time.monotonic()
        cards = extract_memory_cards(user, asst, session_id=sid,
                                     max_cards=max_cards, max_chars=max_chars)
        extract_ms = (time.monotonic() - t0) * 1000
        types = ",".join(sorted({c.type for c in cards})) or "-"
        title_len = max((len(c.title) for c in cards), default=0)
        sum_len = max((len(c.summary) for c in cards), default=0)
        ent_n = sum(len(c.entities) for c in cards)
        formatted = format_memory_cards_for_sync(cards, max_chars=max_chars)
        # leak check (internal only — never printed)
        blob = formatted + "".join(
            c.title + c.summary + " ".join(c.entities) for c in cards)
        if SECRET in blob or "def hack" in blob or "```" in blob:
            leak_found = True
        # sync via fallback store (so recall can find later)
        t1 = time.monotonic()
        store_mgr.sync_structured_cards_all(
            cards, session_id=sid,
            fallback_sync_turn_enabled=mp["structured_cards_fallback_sync_turn_enabled"])
        sync_ms = (time.monotonic() - t1) * 1000
        path = "fallback_sync_turn" if cards else "skipped(no cards)"
        written = bool(cards)
        print("  " + " | ".join(str(x) for x in (
            label, len(cards), types, title_len, sum_len, ent_n,
            len(formatted), f"{extract_ms:.2f}", f"{sync_ms:.2f}", path, written)))

    print(f"\n  leak_detected(in card/fmt text)={leak_found}  (expect False)")

    # ---- provider path coverage ----
    print("\n[SECTION 3b] Provider path coverage")
    demo_cards = extract_memory_cards(*PROBES[0][1:], session_id=sid)
    native_mgr.sync_structured_cards_all(demo_cards, session_id=sid)
    print(f"  native sync_structured_cards: received={len(native.received)} "
          f"(path=native_fast_path)")
    # fail-open: builtin boom + healthy native
    boom = BoomProvider(name="builtin")
    healthy = NativeProvider(name="ext")
    failmgr = MemoryManager()
    failmgr.add_provider(boom)
    failmgr.add_provider(healthy)
    raised = False
    try:
        failmgr.sync_structured_cards_all(demo_cards, session_id=sid)
    except Exception:
        raised = True
    print(f"  fail_open: raised={raised} (expect False), "
          f"healthy_received={len(healthy.received)} (expect >0)")
    # fallback disabled skip
    skipstore = RecallStoreProvider(name="skipme")
    skipmgr = MemoryManager()
    skipmgr.add_provider(skipstore)
    skipmgr.sync_structured_cards_all(demo_cards, session_id=sid,
                                      fallback_sync_turn_enabled=False)
    print(f"  fallback_disabled_skip: stored_blocks={len(skipstore._blocks)} (expect 0)")

    # ---- Section 4: future recall via real PR2/PR3 path ----
    print("\n[SECTION 4] Future recall (PR2/PR3 path, safe metadata)")

    class _Agent:
        pass
    ag = _Agent()
    ag._memory_manager = store_mgr
    ag.session_id = sid
    ag._memory_recall_query_recent_turns = 6
    ag._memory_recall_query_max_recent_chars = 1200
    ag._memory_recall_query_max_chars = 1800
    ag._memory_multi_query_recall_enabled = True
    ag._memory_multi_query_recall_max_queries = mp["multi_query_recall_max_queries"]
    ag._memory_multi_query_recall_max_total_chars = mp["multi_query_recall_max_total_chars"]
    ag._memory_multi_query_recall_per_query_timeout_ms = 3000

    from agent.memory_recall_query import build_recall_query_plan
    rhdr = ("probe", "q_hash", "subq_n", "result_len", "type_found", "label")
    print("  " + " | ".join(rhdr))
    all_pass = True
    for label, query, want_type in RECALL_PROBES:
        plan = build_recall_query_plan(query, max_queries=ag._memory_multi_query_recall_max_queries)
        merged = _recall_multi_query(ag, query, sid, None)
        # internal-only type detection (never printed raw)
        type_found = ("type: " + want_type) in merged
        ok = bool(merged) and type_found
        all_pass = all_pass and ok
        print("  " + " | ".join(str(x) for x in (
            label, _h(query), len(plan.subqueries), len(merged),
            want_type if type_found else "-", "PASS" if ok else "FAIL")))

    print(f"\n  recall_all_pass={all_pass}")
    print("=" * 78)
    print(f"SUMMARY: leak_detected={leak_found} recall_all_pass={all_pass} "
          f"fail_open_ok={not raised}")


if __name__ == "__main__":
    main()

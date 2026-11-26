"""Redact legacy internal context in saved history and resumed Codex Threads."""

import re


_CONTEXT_TAG_NAMES = (
    "memory-context",
    "profile-memory-context",
    "active-decision-context",
    "work-experience-context",
    "external-memory-context",
)
_CONTEXT_TAG_PATTERN = (
    r"(?:memory-context|profile-memory-context|active-decision-context|"
    r"work-experience-context|external-memory-context)"
)
_FENCE_TAG_RE = re.compile(
    rf"</?\s*{_CONTEXT_TAG_PATTERN}\b[^>]*>",
    re.IGNORECASE,
)
_INTERNAL_CONTEXT_RE = re.compile(
    rf"<\s*(?P<context_tag>{_CONTEXT_TAG_PATTERN})\b[^>]*>"
    rf"[\s\S]*?</\s*(?P=context_tag)\s*>",
    re.IGNORECASE,
)
_UNCLOSED_INTERNAL_CONTEXT_RE = re.compile(
    rf"<\s*{_CONTEXT_TAG_PATTERN}\b[^>]*>[\s\S]*\Z",
    re.IGNORECASE,
)
_INTERNAL_NOTE_RE = re.compile(
    r"\[System note:\s*The following is recalled memory context,\s*NOT new user input\.\s*Treat as (?:informational background data|advisory(?:, fallible)? recollection[^\]]*|authoritative reference data[^\]]*)\.\]\s*",
    re.IGNORECASE,
)


def sanitize_context(text: str) -> str:
    """Strip fence tags, injected context blocks, and system notes from provider output."""
    text = _INTERNAL_CONTEXT_RE.sub("", text)
    # Fail closed when a model starts echoing an internal block but omits the
    # closing tag. Streaming output already behaves this way; persistence must
    # enforce the same boundary.
    text = _UNCLOSED_INTERNAL_CONTEXT_RE.sub("", text)
    text = _INTERNAL_NOTE_RE.sub("", text)
    text = _FENCE_TAG_RE.sub("", text)
    return text


class StreamingContextScrubber:
    """Stateful scrubber for streaming text containing internal context spans.

    The one-shot ``sanitize_context`` regex cannot survive chunk boundaries:
    a ``<memory-context>`` opened in one delta and closed in a later delta
    leaks its payload to the UI because the non-greedy block regex needs
    both tags in one string.  This scrubber runs a small state machine
    across deltas, holding back partial-tag tails and discarding
    everything inside a span (including the system-note line).

    Usage::

        scrubber = StreamingContextScrubber()
        for delta in stream:
            visible = scrubber.feed(delta)
            if visible:
                emit(visible)
        trailing = scrubber.flush()  # at end of stream
        if trailing:
            emit(trailing)

    The scrubber is re-entrant per agent instance.  Callers building new
    top-level responses (new turn) should create a fresh scrubber or call
    ``reset()``.
    """

    _TAG_PAIRS = tuple((f"<{name}>", f"</{name}>") for name in _CONTEXT_TAG_NAMES)

    def __init__(self) -> None:
        self._in_span: bool = False
        self._active_close_tag: str | None = None
        self._buf: str = ""
        self._at_block_boundary: bool = True

    def reset(self) -> None:
        self._in_span = False
        self._active_close_tag = None
        self._buf = ""
        self._at_block_boundary = True

    def feed(self, text: str) -> str:
        """Return the visible portion of ``text`` after scrubbing.

        Any trailing fragment that could be the start of an open/close tag
        is held back in the internal buffer and surfaced on the next
        ``feed()`` call or discarded/emitted by ``flush()``.
        """
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []

        while buf:
            if self._in_span:
                close_tag = self._active_close_tag
                if close_tag is None:
                    # Defensive fail closed: an internal state mismatch must
                    # not turn a protected span into visible output.
                    self._buf = ""
                    return "".join(out)
                idx = buf.lower().find(close_tag)
                if idx == -1:
                    # Hold back a potential partial close tag; drop the rest
                    held = self._max_partial_suffix(buf, close_tag)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                # Found close — skip span content + tag, continue
                buf = buf[idx + len(close_tag) :]
                self._in_span = False
                self._active_close_tag = None
            else:
                found = self._find_boundary_open_tag(buf)
                if found is None:
                    # No open tag — hold back a potential partial open tag
                    held = self._max_pending_open_suffix(buf)
                    if not held:
                        held = max(
                            self._max_partial_suffix(buf, open_tag)
                            for open_tag, _ in self._TAG_PAIRS
                        )
                    if held:
                        self._append_visible(out, buf[:-held])
                        self._buf = buf[-held:]
                    else:
                        self._append_visible(out, buf)
                    return "".join(out)
                # Emit text before the tag, enter span
                idx, open_tag, close_tag = found
                if idx > 0:
                    self._append_visible(out, buf[:idx])
                buf = buf[idx + len(open_tag) :]
                self._in_span = True
                self._active_close_tag = close_tag

        return "".join(out)

    def flush(self) -> str:
        """Emit any held-back buffer at end-of-stream.

        If we're still inside an unterminated span the remaining content is
        discarded (safer: leaking partial memory context is worse than a
        truncated answer).  Otherwise the held-back partial-tag tail is
        emitted verbatim (it turned out not to be a real tag).
        """
        if self._in_span:
            self._buf = ""
            self._in_span = False
            self._active_close_tag = None
            return ""
        tail = self._buf
        self._buf = ""
        return tail

    @staticmethod
    def _max_partial_suffix(buf: str, tag: str) -> int:
        """Return the length of the longest buf-suffix that is a tag-prefix.

        Case-insensitive.  Returns 0 if no suffix could start the tag.
        """
        tag_lower = tag.lower()
        buf_lower = buf.lower()
        max_check = min(len(buf_lower), len(tag_lower) - 1)
        for i in range(max_check, 0, -1):
            if tag_lower.startswith(buf_lower[-i:]):
                return i
        return 0

    def _find_boundary_open_tag(self, buf: str) -> tuple[int, str, str] | None:
        """Find an opening fence only when it starts a block-like span."""
        buf_lower = buf.lower()
        candidates: list[tuple[int, str, str]] = []
        for open_tag, close_tag in self._TAG_PAIRS:
            search_start = 0
            while True:
                idx = buf_lower.find(open_tag, search_start)
                if idx == -1:
                    break
                if self._is_block_boundary(buf, idx) and self._has_block_opener_suffix(
                    buf, idx, open_tag
                ):
                    candidates.append((idx, open_tag, close_tag))
                    break
                search_start = idx + 1
        if not candidates:
            return None
        return min(candidates, key=lambda candidate: candidate[0])

    def _max_pending_open_suffix(self, buf: str) -> int:
        """Hold a complete boundary tag until the following char confirms it."""
        held = 0
        for open_tag, _ in self._TAG_PAIRS:
            if not buf.lower().endswith(open_tag):
                continue
            idx = len(buf) - len(open_tag)
            if self._is_block_boundary(buf, idx):
                held = max(held, len(open_tag))
        return held

    def _has_block_opener_suffix(self, buf: str, idx: int, open_tag: str) -> bool:
        after_idx = idx + len(open_tag)
        if after_idx >= len(buf):
            return False
        return buf[after_idx] in "\r\n"

    def _is_block_boundary(self, buf: str, idx: int) -> bool:
        if idx == 0:
            return self._at_block_boundary
        preceding = buf[:idx]
        last_newline = preceding.rfind("\n")
        if last_newline == -1:
            return self._at_block_boundary and preceding.strip() == ""
        return preceding[last_newline + 1 :].strip() == ""

    def _append_visible(self, out: list[str], text: str) -> None:
        if not text:
            return
        out.append(text)
        self._update_block_boundary(text)

    def _update_block_boundary(self, text: str) -> None:
        last_newline = text.rfind("\n")
        if last_newline != -1:
            self._at_block_boundary = text[last_newline + 1 :].strip() == ""
        else:
            self._at_block_boundary = self._at_block_boundary and text.strip() == ""

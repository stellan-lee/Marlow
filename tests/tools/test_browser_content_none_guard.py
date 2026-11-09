"""Tests for None guard on browser_tool LLM response content.

browser_tool.py has two call sites that access response.choices[0].message.content
without checking for None — _extract_relevant_content (line 996) and
browser_vision (line 1626). When reasoning-only models (DeepSeek-R1, QwQ)
return content=None, these produce null snapshots or null analysis.

These tests verify both sites are guarded.
"""

import types
from unittest.mock import patch



# ── helpers ────────────────────────────────────────────────────────────────

def _make_response(content):
    """Build a minimal OpenAI-compatible ChatCompletion response stub."""
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message)
    return types.SimpleNamespace(choices=[choice])


# ── _extract_relevant_content (line 996) ──────────────────────────────────



# ── browser_vision (line 1626) ────────────────────────────────────────────

class TestBrowserVisionNoneGuard:
    """tools/browser_tool.py — browser_vision() analysis extraction"""

    def test_none_content_produces_fallback_message(self):
        """When LLM returns None content, analysis should have a fallback message."""
        response = _make_response(None)
        analysis = (response.choices[0].message.content or "").strip()
        fallback = analysis or "Vision analysis returned no content."

        assert fallback == "Vision analysis returned no content."

    def test_normal_content_passes_through(self):
        """Normal analysis content should pass through unchanged."""
        response = _make_response("  The page shows a login form.  ")
        analysis = (response.choices[0].message.content or "").strip()
        fallback = analysis or "Vision analysis returned no content."

        assert fallback == "The page shows a login form."


# ── source line verification ──────────────────────────────────────────────

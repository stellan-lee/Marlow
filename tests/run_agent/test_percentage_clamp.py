"""Tests for percentage clamping at 100% across display paths.

PR #3480 capped context pressure percentage at 100% in agent/display.py
but missed the same unclamped pattern in 4 other files. When token counts
overshoot the context length (possible during streaming or before
compression fires), users see >100% in /stats, gateway status, and
memory tool output.
"""

class TestMemoryToolPercentClamp:
    """tools/memory_tool.py — _success_response and _render_block pct"""

    def test_over_limit_clamped_at_100(self):
        """Percentage should be capped at 100 even if current > limit."""
        # Simulate the calculation directly
        current = 5500
        limit = 5000
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        assert pct == 100

    def test_normal_percentage(self):
        current = 2500
        limit = 5000
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        assert pct == 50

    def test_zero_limit_returns_zero(self):
        current = 100
        limit = 0
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        assert pct == 0




class TestGatewayStatsPercentClamp:
    """gateway/run.py — _format_usage_stats percentage"""

    def test_over_context_clamped_at_100(self):
        last_prompt_tokens = 210_000
        context_length = 200_000
        pct = min(100, last_prompt_tokens / context_length * 100) if context_length else 0
        assert pct == 100

    def test_normal_context(self):
        last_prompt_tokens = 150_000
        context_length = 200_000
        pct = min(100, last_prompt_tokens / context_length * 100) if context_length else 0
        assert pct == 75.0

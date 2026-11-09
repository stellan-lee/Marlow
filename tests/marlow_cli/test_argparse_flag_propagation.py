"""Tests for parent→subparser flag propagation.

When flags like --yolo and -w exist on both the parent parser and the 'chat'
subparser, placing the flag BEFORE the subcommand (e.g. 'marlow --yolo chat')
must not silently drop the flag value.

Regression test for: argparse subparser default=False overwriting parent's
parsed True when the same argument is defined on both parsers.

Fix: chat subparser uses default=argparse.SUPPRESS for all duplicated flags,
so the subparser only sets the attribute when the user explicitly provides it.
"""

import sys

import pytest


class TestChatVerboseArg:
    """Verify chat --verbose preserves config fallback when absent."""

    def test_chat_without_verbose_leaves_attribute_unset(self):
        from marlow_cli._parser import build_top_level_parser

        parser, _subparsers, _chat_parser = build_top_level_parser()
        args = parser.parse_args(["chat"])

        assert not hasattr(args, "verbose")

    def test_chat_verbose_sets_attribute_true(self):
        from marlow_cli._parser import build_top_level_parser

        parser, _subparsers, _chat_parser = build_top_level_parser()
        args = parser.parse_args(["chat", "--verbose"])

        assert args.verbose is True

class TestAcceptHooksOnAgentSubparsers:
    """Verify --accept-hooks is accepted at every agent-subcommand
    position (before the subcommand, between group/subcommand, and
    after the leaf subcommand) for gateway/cron/mcp.  Regression
    against prior behaviour where the flag only worked on the root
    parser and `chat`, so `marlow gateway run --accept-hooks` failed
    with `unrecognized arguments`."""

    @pytest.mark.parametrize("argv", [
        ["--accept-hooks", "gateway", "run", "--help"],
        ["gateway", "--accept-hooks", "run", "--help"],
        ["gateway", "run", "--accept-hooks", "--help"],
        ["--accept-hooks", "cron", "tick", "--help"],
        ["cron", "--accept-hooks", "tick", "--help"],
        ["cron", "tick", "--accept-hooks", "--help"],
        ["cron", "run", "--accept-hooks", "dummy-id", "--help"],
        ["--accept-hooks", "mcp", "serve", "--help"],
        ["mcp", "--accept-hooks", "serve", "--help"],
        ["mcp", "serve", "--accept-hooks", "--help"],
    ])
    def test_accepted_at_every_position(self, argv):
        """Invoking `marlow <argv>` must exit 0 (help) rather than
        failing with `unrecognized arguments`."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "marlow_cli.main", *argv],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"argv={argv!r} returned {result.returncode}\n"
            f"stdout: {result.stdout[:300]}\n"
            f"stderr: {result.stderr[:300]}"
        )
        assert "unrecognized arguments" not in result.stderr

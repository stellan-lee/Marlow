"""
Top-level argparse construction for the marlow CLI.

Only the top-level parser and the ``chat`` subparser live here. Every other
subparser (model, gateway, sessions, …) is built inline in ``main.py``
because its dispatch is tightly coupled to module-level ``cmd_*`` functions.
"""

import argparse


_EPILOGUE = """
Examples:
    marlow                                 Show available commands
    marlow chat -q "Hello"                 Run one query
    marlow -z "Hello"                      Print only the final answer
    marlow chat -c -q "Continue"           Resume the most recent session
    marlow chat -r SESSION -q "Continue"   Resume a session by ID or title
    marlow setup                  Run setup wizard
    marlow logout                 Clear stored authentication
    marlow model                  Select default model
    marlow fallback [list]        Show fallback provider chain
    marlow fallback add           Add a fallback provider (same picker as `marlow model`)
    marlow fallback remove        Remove a fallback provider from the chain
    marlow config                 View configuration
    marlow config edit            Edit config in $EDITOR
    marlow config set model gpt-4 Set a config value
    marlow gateway                Run messaging gateway
    marlow chat -w -q "Fix issue" Run one query in an isolated Git worktree
    marlow gateway install        Install gateway background service
    marlow sessions list          List past sessions
    marlow sessions browse        Interactive session picker
    marlow sessions rename ID T   Rename/title a session
    marlow logs                   View agent.log (last 50 lines)
    marlow logs -f                Follow agent.log in real time
    marlow logs errors            View errors.log
    marlow logs --since 1h        Lines from the last hour
    marlow debug share             Upload debug report for support
    marlow update                 Update to latest version

For more help on a command:
    marlow <command> --help
"""


def build_top_level_parser():
    """Build the top-level parser, the subparsers action, and the ``chat`` subparser.

    Returns ``(parser, subparsers, chat_parser)``. The caller wires
    ``chat_parser.set_defaults(func=cmd_chat)`` and continues registering
    other subparsers via ``subparsers.add_parser(...)``.
    """
    parser = argparse.ArgumentParser(
        prog="marlow",
        description="Marlow Agent - AI assistant with tool-calling capabilities",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOGUE,
    )

    parser.add_argument(
        "--version", "-V", action="store_true", help="Show version and exit"
    )
    parser.add_argument(
        "-z",
        "--oneshot",
        metavar="PROMPT",
        default=None,
        help=(
            "One-shot mode: send a single prompt and print ONLY the final "
            "response text to stdout. No banner, no spinner, no tool "
            "previews, no session_id line. Tools, memory, rules, and "
            "AGENTS.md in the CWD are loaded as normal. Actions requiring an "
            "interactive approval are blocked. Intended for scripts / pipes."
        ),
    )
    # --model / --provider are accepted at the top level so they can pair
    # with -z without needing the `chat` subcommand.  If neither -z nor a
    # subcommand consumes them, they fall through harmlessly as None.
    # Mirrors `marlow chat --model ... --provider ...` semantics.
    parser.add_argument(
        "-m",
        "--model",
        default=None,
        help=(
            "Model override for this invocation (e.g. gpt-5.4 or a local model id). "
            "Applies to -z/--oneshot. Also settable via MARLOW_INFERENCE_MODEL env var."
        ),
    )
    parser.add_argument(
        "--provider",
        default=None,
        help=(
            "Provider override for this invocation (openai-codex or custom). "
            "Applies to -z/--oneshot. The persistent provider lives in config.yaml "
            "under model.provider — use `marlow setup` or edit the file to change it."
        ),
    )
    parser.add_argument(
        "-t",
        "--toolsets",
        default=None,
        help="Comma-separated toolsets to enable for this invocation. Applies to -z/--oneshot.",
    )
    parser.add_argument(
        "--resume",
        "-r",
        metavar="SESSION",
        default=None,
        help="Resume a previous session by ID or title",
    )
    parser.add_argument(
        "--no-restore-cwd",
        action="store_true",
        default=False,
        help="Don't cd into a resumed session's recorded working directory.",
    )
    parser.add_argument(
        "--continue",
        "-c",
        dest="continue_last",
        nargs="?",
        const=True,
        default=None,
        metavar="SESSION_NAME",
        help="Resume a session by name, or the most recent if no name given",
    )
    parser.add_argument(
        "--worktree",
        "-w",
        action="store_true",
        default=False,
        help="Run in an isolated git worktree (for parallel agents)",
    )
    parser.add_argument(
        "--accept-hooks",
        action="store_true",
        default=False,
        help=(
            "Auto-approve any unseen shell hooks declared in config.yaml "
            "without a TTY prompt.  Equivalent to MARLOW_ACCEPT_HOOKS=1 or "
            "hooks_auto_accept: true in config.yaml.  Use on CI / headless "
            "runs that can't prompt."
        ),
    )
    parser.add_argument(
        "--yolo",
        action="store_true",
        default=False,
        help="Bypass all dangerous command approval prompts (use at your own risk)",
    )
    parser.add_argument(
        "--pass-session-id",
        action="store_true",
        default=False,
        help="Include the session ID in the agent's system prompt",
    )
    parser.add_argument(
        "--ignore-user-config",
        action="store_true",
        default=False,
        help="Ignore ~/.marlow/config.yaml and fall back to built-in defaults (credentials in .env are still loaded)",
    )
    parser.add_argument(
        "--ignore-rules",
        action="store_true",
        default=False,
        help="Skip auto-injection of AGENTS.md, .cursorrules, and memory",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # =========================================================================
    # chat command
    # =========================================================================
    chat_parser = subparsers.add_parser(
        "chat",
        help="Run a single query with the agent",
        description="Run one query with Marlow Agent, then exit",
    )
    chat_parser.add_argument(
        "-q", "--query", help="Single query (non-interactive mode)"
    )
    chat_parser.add_argument(
        "--image", help="Optional local image path to attach to a single query"
    )
    chat_parser.add_argument(
        "-m", "--model", help="Model to use (e.g. gpt-5.4 or a local model id)",
    )
    chat_parser.add_argument(
        "-t", "--toolsets", help="Comma-separated toolsets to enable"
    )
    chat_parser.add_argument(
        "--provider",
        # No `choices=` here: user-defined providers from config.yaml `providers:`
        # are also valid values, and runtime resolution (resolve_runtime_provider)
        # handles validation/error reporting consistently with the top-level
        # `--provider` flag.
        default=None,
        help="Inference provider (default: auto). Built-in or a user-defined name from `providers:` in config.yaml.",
    )
    chat_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Verbose output",
    )
    chat_parser.add_argument(
        "-Q",
        "--quiet",
        action="store_true",
        help="Quiet mode for programmatic use: suppress banner, spinner, and tool previews. Only output the final response and session info.",
    )
    chat_parser.add_argument(
        "--resume",
        "-r",
        metavar="SESSION_ID",
        default=argparse.SUPPRESS,
        help="Resume a previous session by ID (shown on exit)",
    )
    chat_parser.add_argument(
        "--no-restore-cwd",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Don't cd into a resumed session's recorded working directory.",
    )
    chat_parser.add_argument(
        "--continue",
        "-c",
        dest="continue_last",
        nargs="?",
        const=True,
        default=argparse.SUPPRESS,
        metavar="SESSION_NAME",
        help="Resume a session by name, or the most recent if no name given",
    )
    chat_parser.add_argument(
        "--worktree",
        "-w",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Run in an isolated git worktree (for parallel agents on the same repo)",
    )
    chat_parser.add_argument(
        "--accept-hooks",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "Auto-approve any unseen shell hooks declared in config.yaml "
            "without a TTY prompt (see also MARLOW_ACCEPT_HOOKS env var and "
            "hooks_auto_accept: in config.yaml)."
        ),
    )
    chat_parser.add_argument(
        "--checkpoints",
        action="store_true",
        default=False,
        help="Enable filesystem checkpoints before destructive file operations (use /rollback to restore)",
    )
    chat_parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        metavar="N",
        help="Maximum tool-calling iterations per conversation turn (default: 90, or agent.max_turns in config)",
    )
    chat_parser.add_argument(
        "--yolo",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Bypass all dangerous command approval prompts (use at your own risk)",
    )
    chat_parser.add_argument(
        "--pass-session-id",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Include the session ID in the agent's system prompt",
    )
    chat_parser.add_argument(
        "--ignore-user-config",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Ignore ~/.marlow/config.yaml and fall back to built-in defaults (credentials in .env are still loaded). Useful for isolated CI runs, reproduction, and third-party integrations.",
    )
    chat_parser.add_argument(
        "--ignore-rules",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Skip auto-injection of AGENTS.md, .cursorrules, and memory. Combine with --ignore-user-config for a fully isolated run.",
    )
    chat_parser.add_argument(
        "--source",
        default=None,
        help="Session source tag for filtering (default: cli). Use 'tool' for third-party integrations that should not appear in user session lists.",
    )
    return parser, subparsers, chat_parser

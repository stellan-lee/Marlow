#!/usr/bin/env python3
"""
Marlow CLI - Main entry point.

Usage:
    marlow                     # Show command help
    marlow chat -q "Hello"     # Run a single query
    marlow gateway             # Run gateway in foreground
    marlow gateway start       # Start gateway as service
    marlow gateway stop        # Stop gateway service
    marlow gateway status      # Show gateway status
    marlow gateway install     # Install gateway service
    marlow gateway uninstall   # Uninstall gateway service
    marlow setup               # Interactive setup wizard
    marlow logout              # Clear stored authentication
    marlow status              # Show status of all components
    marlow cron                # Manage cron jobs
    marlow cron list           # List cron jobs
    marlow cron status         # Check if cron scheduler is running
    marlow doctor              # Check configuration and dependencies
    marlow honcho setup                    # Configure Honcho AI memory integration
    marlow honcho status                   # Show Honcho config and connection status
    marlow honcho sessions                 # List directory → session name mappings
    marlow honcho map <name>               # Map current directory to a session name
    marlow honcho peer                     # Show peer names and dialectic settings
    marlow honcho peer --user NAME         # Set user peer name
    marlow honcho peer --ai NAME           # Set AI peer name
    marlow honcho peer --reasoning LEVEL   # Set dialectic reasoning level
    marlow honcho mode                     # Show current memory mode
    marlow honcho mode [hybrid|honcho|local]  # Set memory mode
    marlow honcho tokens                   # Show token budget settings
    marlow honcho tokens --context N       # Set session.context() token cap
    marlow honcho tokens --dialectic N     # Set dialectic result char cap
    marlow honcho identity                 # Show AI peer identity representation
    marlow honcho identity <file>          # Seed AI peer identity from a file (SOUL.md etc.)
    marlow version             Show version
    marlow update              Update to latest version
    marlow uninstall           Uninstall Marlow Agent
    marlow sessions browse     Interactive session picker with search
"""

import os
import sys


def _set_process_title() -> None:
    """Set the process title to 'marlow' so tools like 'ps', 'top', and
    'htop' show the app name instead of 'python3.xx'.

    Purely cosmetic — non-fatal on any platform.

    Strategy (try in order):
      1. ``setproctitle`` (opt-in dep — installed via ``marlow tools`` or
         ``pip install setproctitle``, or bundled in a future release).
      2. ctypes ``prctl(PR_SET_NAME)`` (Linux only, 15-char limit).
      3. ctypes ``pthread_setname_np`` (macOS only, kernel thread name —
         changes lldb/top but not ``ps aux``).
    """
    # Strategy 1: setproctitle (best — works on macOS, Linux, BSD)
    try:
        import setproctitle  # type: ignore[import-untyped]

        setproctitle.setproctitle("marlow")
        return
    except ImportError:
        pass

    # Strategy 2/3: platform-specific ctypes fallback
    import ctypes
    import platform

    try:
        system = platform.system()
        if system == "Linux":
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl(15, b"marlow", 0, 0, 0)  # PR_SET_NAME = 15
        elif system == "Darwin":
            libc = ctypes.CDLL("libc.dylib", use_errno=True)
            libc.pthread_setname_np(b"marlow")
    except Exception:
        pass


import argparse
from contextlib import redirect_stdout
import json
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Optional


def _add_accept_hooks_flag(parser) -> None:
    """Attach the ``--accept-hooks`` flag.  Shared across every agent
    subparser so the flag works regardless of CLI position."""
    parser.add_argument(
        "--accept-hooks",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "Auto-approve unseen shell hooks without a TTY prompt "
            "(equivalent to MARLOW_ACCEPT_HOOKS=1 / hooks_auto_accept: true)."
        ),
    )


def _require_tty(command_name: str) -> None:
    """Exit with a clear error if stdin is not a terminal.

    Interactive TUI commands (marlow tools, marlow setup, marlow model) use
    curses or input() prompts that spin at 100% CPU when stdin is a pipe.
    This guard prevents accidental non-interactive invocation.
    """
    if not sys.stdin.isatty():
        print(
            f"Error: 'marlow {command_name}' requires an interactive terminal.\n"
            f"It cannot be run through a pipe or non-interactive subprocess.\n"
            f"Run it directly in your terminal instead.",
            file=sys.stderr,
        )
        sys.exit(1)


# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from marlow_constants import MARLOW_INSTALL_SCRIPT_URL  # noqa: E402


# ---------------------------------------------------------------------------
# Profile override — MUST happen before any marlow module import.
#
# Many modules cache MARLOW_HOME at import time (module-level constants).
# We intercept --profile/-p from sys.argv here and set the env var so that
# every subsequent ``os.getenv("MARLOW_HOME", ...)`` resolves correctly.
# The flag is stripped from sys.argv so argparse never sees it.
# Falls back to ~/.marlow/active_profile for sticky default.
# ---------------------------------------------------------------------------
def _apply_profile_override() -> None:
    """Pre-parse --profile/-p and set MARLOW_HOME before module imports."""
    argv = sys.argv[1:]
    profile_name = None
    consume = 0

    # 1. Check for explicit -p / --profile flag
    for i, arg in enumerate(argv):
        if arg in {"--profile", "-p"} and i + 1 < len(argv):
            profile_name = argv[i + 1]
            consume = 2
            break
        elif arg.startswith("--profile="):
            profile_name = arg.split("=", 1)[1]
            consume = 1
            break

    # 1b. Reject values that can't be valid profile names (e.g. pytest's
    # "-p no:xdist" would be misread as profile "no:xdist" otherwise).
    # Mirrors marlow_cli.profiles._PROFILE_ID_RE so we never call
    # resolve_profile_env() with a value it must reject + sys.exit on.
    if profile_name is not None and consume == 2:
        import re as _re

        if not _re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", profile_name):
            profile_name = None
            consume = 0

    # 1.5 If MARLOW_HOME is already set and no explicit flag was given, trust it
    # only when it already points to a specific profile directory.  The
    # distinguishing heuristic: a profile path has "profiles" as its immediate
    # parent directory name (e.g. ~/.marlow/profiles/coder or
    # /opt/data/profiles/coder).  If MARLOW_HOME points to the marlow root
    # instead (e.g. systemd hardcodes MARLOW_HOME=/root/.marlow), we must
    # still read active_profile — the user may have switched profiles via
    # `marlow profile use` and the gateway should honour that choice.
    # See issue #22502.
    marlow_home_env = os.environ.get("MARLOW_HOME", "")
    if profile_name is None and marlow_home_env:
        if Path(marlow_home_env).parent.name == "profiles":
            return

    # 2. If no flag, check active_profile in the marlow root
    if profile_name is None:
        try:
            from marlow_constants import get_default_marlow_root

            active_path = get_default_marlow_root() / "active_profile"
            if active_path.exists():
                name = active_path.read_text().strip()
                if name and name != "default":
                    profile_name = name
                    consume = 0  # don't strip anything from argv
        except (UnicodeDecodeError, OSError):
            pass  # corrupted file, skip

    # 3. If we found a profile, resolve and set MARLOW_HOME
    if profile_name is not None:
        try:
            from marlow_cli.profiles import resolve_profile_env

            marlow_home = resolve_profile_env(profile_name)
        except (ValueError, FileNotFoundError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            # A bug in profiles.py must NEVER prevent marlow from starting
            print(
                f"Warning: profile override failed ({exc}), using default",
                file=sys.stderr,
            )
            return
        os.environ["MARLOW_HOME"] = marlow_home
        # Strip the flag from argv so argparse doesn't choke
        if consume > 0:
            for i, arg in enumerate(argv):
                if arg in {"--profile", "-p"}:
                    start = i + 1  # +1 because argv is sys.argv[1:]
                    sys.argv = sys.argv[:start] + sys.argv[start + consume :]
                    break
                elif arg.startswith("--profile="):
                    start = i + 1
                    sys.argv = sys.argv[:start] + sys.argv[start + 1 :]
                    break


_apply_profile_override()

# Load .env from ~/.marlow/.env first, then project root as dev fallback.
# User-managed env files should override stale shell exports on restart.
from marlow_cli.config import get_marlow_home
from marlow_cli.env_loader import load_marlow_dotenv

load_marlow_dotenv(project_env=PROJECT_ROOT / ".env")

# Bridge security.redact_secrets from config.yaml → MARLOW_REDACT_SECRETS env
# var BEFORE marlow_logging imports agent.redact (which snapshots the flag at
# module-import time). Without this, config.yaml's toggle is ignored because
# the setup_logging() call below imports agent.redact, which reads the env var
# exactly once. Env var in .env still wins — this is config.yaml fallback only.
#
# We also read network.force_ipv4 from the same yaml load to avoid two
# separate config.yaml reads (saves ~17ms on every CLI startup — the second
# `load_config()` was doing a full deep-merge for one boolean lookup).
_FORCE_IPV4_EARLY = False
try:
    import yaml as _yaml_early

    _cfg_path = get_marlow_home() / "config.yaml"
    _skip_early_config = (
        os.environ.get("MARLOW_IGNORE_USER_CONFIG") == "1"
        or "--ignore-user-config" in sys.argv[1:]
    )
    if not _skip_early_config and _cfg_path.exists():
        with open(_cfg_path, encoding="utf-8") as _f:
            _early_cfg_raw = _yaml_early.safe_load(_f) or {}
        if "MARLOW_REDACT_SECRETS" not in os.environ:
            _early_sec_cfg = _early_cfg_raw.get("security", {})
            if isinstance(_early_sec_cfg, dict):
                _early_redact = _early_sec_cfg.get("redact_secrets")
                if _early_redact is not None:
                    os.environ["MARLOW_REDACT_SECRETS"] = str(_early_redact).lower()
        _early_net_cfg = _early_cfg_raw.get("network", {})
        if isinstance(_early_net_cfg, dict) and _early_net_cfg.get("force_ipv4"):
            _FORCE_IPV4_EARLY = True
        del _early_cfg_raw
    del _cfg_path
except Exception:
    pass  # best-effort — redaction stays at default (enabled) on config errors

# Initialize centralized file logging early — all `marlow` subcommands
# (chat, setup, gateway, config, etc.) write to agent.log + errors.log.
try:
    from marlow_logging import setup_logging as _setup_logging

    _setup_logging(mode="cli")
except Exception:
    pass  # best-effort — don't crash the CLI if logging setup fails

# Apply IPv4 preference early, before any HTTP clients are created.
# We already determined whether to force IPv4 from the raw yaml read above —
# this just calls the toggle without a redundant load_config() round trip.
if _FORCE_IPV4_EARLY:
    try:
        from marlow_constants import apply_ipv4_preference as _apply_ipv4

        _apply_ipv4(force=True)
    except Exception:
        pass  # best-effort — don't crash if marlow_constants not importable yet

import logging
import threading
import time as _time
from datetime import datetime

from marlow_cli import __version__, __release_date__
logger = logging.getLogger(__name__)


def _relative_time(ts) -> str:
    """Format a timestamp as relative time (e.g., '2h ago', 'yesterday')."""
    if not ts:
        return "?"
    delta = _time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    if delta < 172800:
        return "yesterday"
    if delta < 604800:
        return f"{int(delta / 86400)}d ago"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _has_any_provider_configured() -> bool:
    """Return whether Codex or a retained custom/local runtime is configured."""
    from marlow_cli.auth import get_codex_auth_status
    from marlow_cli.config import get_env_value, load_config

    try:
        if get_codex_auth_status().get("logged_in"):
            return True
    except Exception:
        pass
    from agent.codex_environment import managed_environment
    return bool(managed_environment().config.get('model_provider'))





def _session_browse_picker(sessions: list) -> Optional[str]:
    """Interactive curses-based session browser with live search filtering.

    Returns the selected session ID, or None if cancelled.
    Uses curses (not simple_term_menu) to avoid the ghost-duplication rendering
    bug in tmux/iTerm when arrow keys are used.
    """
    if not sessions:
        print("No sessions found.")
        return None

    # Try curses-based picker first
    try:
        import curses

        result_holder = [None]

        def _format_row(s, max_x):
            """Format a session row for display."""
            title = (s.get("title") or "").strip()
            preview = (s.get("preview") or "").strip()
            source = s.get("source", "")[:6]
            last_active = _relative_time(s.get("last_active"))
            sid = s["id"][:18]

            # Adaptive column widths based on terminal width
            # Layout: [arrow 3] [title/preview flexible] [active 12] [src 6] [id 18]
            fixed_cols = 3 + 12 + 6 + 18 + 6  # arrow + active + src + id + padding
            name_width = max(20, max_x - fixed_cols)

            if title:
                name = title[:name_width]
            elif preview:
                name = preview[:name_width]
            else:
                name = sid

            return f"{name:<{name_width}}  {last_active:<10}  {source:<5} {sid}"

        def _match(s, query):
            """Check if a session matches the search query (case-insensitive)."""
            q = query.lower()
            return (
                q in (s.get("title") or "").lower()
                or q in (s.get("preview") or "").lower()
                or q in s.get("id", "").lower()
                or q in (s.get("source") or "").lower()
            )

        def _curses_browse(stdscr):
            curses.curs_set(0)
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_GREEN, -1)  # selected
                curses.init_pair(2, curses.COLOR_YELLOW, -1)  # header
                curses.init_pair(3, curses.COLOR_CYAN, -1)  # search
                curses.init_pair(4, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)  # dim

            cursor = 0
            scroll_offset = 0
            search_text = ""
            filtered = list(sessions)

            while True:
                stdscr.clear()
                max_y, max_x = stdscr.getmaxyx()
                if max_y < 5 or max_x < 40:
                    # Terminal too small
                    try:
                        stdscr.addstr(0, 0, "Terminal too small")
                    except curses.error:
                        pass
                    stdscr.refresh()
                    stdscr.getch()
                    return

                # Header line
                if search_text:
                    header = f"  Browse sessions — filter: {search_text}█"
                    header_attr = curses.A_BOLD
                    if curses.has_colors():
                        header_attr |= curses.color_pair(3)
                else:
                    header = "  Browse sessions — ↑↓ navigate  Enter select  Type to filter  Esc quit"
                    header_attr = curses.A_BOLD
                    if curses.has_colors():
                        header_attr |= curses.color_pair(2)
                try:
                    stdscr.addnstr(0, 0, header, max_x - 1, header_attr)
                except curses.error:
                    pass

                # Column header line
                fixed_cols = 3 + 12 + 6 + 18 + 6
                name_width = max(20, max_x - fixed_cols)
                col_header = f"   {'Title / Preview':<{name_width}}  {'Active':<10}  {'Src':<5} {'ID'}"
                try:
                    dim_attr = (
                        curses.color_pair(4) if curses.has_colors() else curses.A_DIM
                    )
                    stdscr.addnstr(1, 0, col_header, max_x - 1, dim_attr)
                except curses.error:
                    pass

                # Compute visible area
                visible_rows = max_y - 4  # header + col header + blank + footer
                visible_rows = max(visible_rows, 1)

                # Clamp cursor and scroll
                if not filtered:
                    try:
                        msg = "  No sessions match the filter."
                        stdscr.addnstr(3, 0, msg, max_x - 1, curses.A_DIM)
                    except curses.error:
                        pass
                else:
                    if cursor >= len(filtered):
                        cursor = len(filtered) - 1
                    cursor = max(cursor, 0)
                    if cursor < scroll_offset:
                        scroll_offset = cursor
                    elif cursor >= scroll_offset + visible_rows:
                        scroll_offset = cursor - visible_rows + 1

                    for draw_i, i in enumerate(
                        range(
                            scroll_offset,
                            min(len(filtered), scroll_offset + visible_rows),
                        )
                    ):
                        y = draw_i + 3
                        if y >= max_y - 1:
                            break
                        s = filtered[i]
                        arrow = " → " if i == cursor else "   "
                        row = arrow + _format_row(s, max_x - 3)
                        attr = curses.A_NORMAL
                        if i == cursor:
                            attr = curses.A_BOLD
                            if curses.has_colors():
                                attr |= curses.color_pair(1)
                        try:
                            stdscr.addnstr(y, 0, row, max_x - 1, attr)
                        except curses.error:
                            pass

                # Footer
                footer_y = max_y - 1
                if filtered:
                    footer = f"  {cursor + 1}/{len(filtered)} sessions"
                    if len(filtered) < len(sessions):
                        footer += f" (filtered from {len(sessions)})"
                else:
                    footer = f"  0/{len(sessions)} sessions"
                try:
                    stdscr.addnstr(
                        footer_y,
                        0,
                        footer,
                        max_x - 1,
                        curses.color_pair(4) if curses.has_colors() else curses.A_DIM,
                    )
                except curses.error:
                    pass

                stdscr.refresh()
                key = stdscr.getch()

                if key in {curses.KEY_UP,}:
                    if filtered:
                        cursor = (cursor - 1) % len(filtered)
                elif key in {curses.KEY_DOWN,}:
                    if filtered:
                        cursor = (cursor + 1) % len(filtered)
                elif key in {curses.KEY_ENTER, 10, 13}:
                    if filtered:
                        result_holder[0] = filtered[cursor]["id"]
                    return
                elif key == 27:  # Esc
                    if search_text:
                        # First Esc clears the search
                        search_text = ""
                        filtered = list(sessions)
                        cursor = 0
                        scroll_offset = 0
                    else:
                        # Second Esc exits
                        return
                elif key in {curses.KEY_BACKSPACE, 127, 8}:
                    if search_text:
                        search_text = search_text[:-1]
                        if search_text:
                            filtered = [s for s in sessions if _match(s, search_text)]
                        else:
                            filtered = list(sessions)
                        cursor = 0
                        scroll_offset = 0
                elif key == ord("q") and not search_text:
                    return
                elif 32 <= key <= 126:
                    # Printable character → add to search filter
                    search_text += chr(key)
                    filtered = [s for s in sessions if _match(s, search_text)]
                    cursor = 0
                    scroll_offset = 0

        curses.wrapper(_curses_browse)
        return result_holder[0]

    except Exception:
        pass

    # Fallback: numbered list when curses is unavailable.
    print("\n  Browse sessions  (enter number to resume, q to cancel)\n")
    for i, s in enumerate(sessions):
        title = (s.get("title") or "").strip()
        preview = (s.get("preview") or "").strip()
        label = title or preview or s["id"]
        if len(label) > 50:
            label = label[:47] + "..."
        last_active = _relative_time(s.get("last_active"))
        src = s.get("source", "")[:6]
        print(f"  {i + 1:>3}. {label:<50}  {last_active:<10}  {src}")

    while True:
        try:
            val = input(f"\n  Select [1-{len(sessions)}]: ").strip()
            if not val or val.lower() in {"q", "quit", "exit"}:
                return None
            idx = int(val) - 1
            if 0 <= idx < len(sessions):
                return sessions[idx]["id"]
            print(f"  Invalid selection. Enter 1-{len(sessions)} or q to cancel.")
        except ValueError:
            print("  Invalid input. Enter a number or q to cancel.")
        except (KeyboardInterrupt, EOFError):
            print()
            return None


def cmd_chat(args):
    """Run one query without starting an interactive chat interface."""
    from marlow_cli.query import run_query

    sys.exit(run_query(args))


def cmd_gateway(args):
    """Gateway management commands."""
    from marlow_cli.gateway import gateway_command

    gateway_command(args)




def cmd_setup(args):
    """Interactive setup wizard."""
    from marlow_cli.setup import run_setup_wizard

    run_setup_wizard(args)


def cmd_postinstall(args):
    """One-shot bootstrap for pip users: install non-Python deps + run setup."""
    from marlow_cli.config import stamp_install_method
    from marlow_cli.dep_ensure import ensure_dependency

    stamp_install_method("pip")

    print("⚕ Marlow post-install bootstrap")
    print()

    for dep in ("node", "browser", "ripgrep"):
        ensure_dependency(dep)

    if not _has_any_provider_configured():
        print()
        cmd_setup(args)
    else:
        print()
        print("✓ Post-install complete.")


def cmd_model(args):
    """Select default model — starts with provider selection, then model picker."""
    _require_tty("model")
    if getattr(args, "refresh", False):
        try:
            from marlow_cli.models import clear_provider_models_cache
            clear_provider_models_cache()
            print("  Cleared model picker cache.")
        except Exception:
            pass
    select_provider_and_model(args=args)




def select_provider_and_model(args=None):
    from agent.codex_environment import CodexEnvironment
    from marlow_cli.codex_config import set_model
    e = CodexEnvironment.build()
    print(f"Codex model configuration: {e.codex_home / 'config.toml'}")
    print(f"Current model: {e.config.get('model', '(Codex default)')}")
    if sys.stdin.isatty():
        model = input("New model identifier (blank keeps current): ").strip()
        if model:
            set_model(model)
    print("Custom providers require the Responses API with tools; edit model_providers in this file.")



def _current_reasoning_effort(config) -> str:
    agent_cfg = config.get("agent")
    if isinstance(agent_cfg, dict):
        return str(agent_cfg.get("reasoning_effort") or "").strip().lower()
    return ""


def _set_reasoning_effort(config, effort: str) -> None:
    agent_cfg = config.get("agent")
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
        config["agent"] = agent_cfg
    agent_cfg["reasoning_effort"] = effort


def _prompt_reasoning_effort_selection(efforts, current_effort=""):
    """Prompt for a reasoning effort. Returns effort, 'none', or None to keep current."""
    deduped = list(
        dict.fromkeys(
            str(effort).strip().lower() for effort in efforts if str(effort).strip()
        )
    )
    canonical_order = ("minimal", "low", "medium", "high", "xhigh")
    ordered = [effort for effort in canonical_order if effort in deduped]
    ordered.extend(effort for effort in deduped if effort not in canonical_order)
    if not ordered:
        return None

    def _label(effort):
        if effort == current_effort:
            return f"{effort}  ← currently in use"
        return effort

    disable_label = "Disable reasoning"
    skip_label = "Skip (keep current)"

    if current_effort == "none":
        default_idx = len(ordered)
    elif current_effort in ordered:
        default_idx = ordered.index(current_effort)
    elif "medium" in ordered:
        default_idx = ordered.index("medium")
    else:
        default_idx = 0

    try:
        from marlow_cli.curses_ui import curses_radiolist

        choices = [_label(effort) for effort in ordered]
        choices.append(disable_label)
        choices.append(skip_label)
        idx = curses_radiolist(
            "Select reasoning effort:",
            choices,
            selected=default_idx,
            cancel_returns=-1,
        )
        if idx < 0:
            return None
        print()
        if idx < len(ordered):
            return ordered[idx]
        if idx == len(ordered):
            return "none"
        return None
    except (ImportError, NotImplementedError, OSError, subprocess.SubprocessError):
        pass

    print("Select reasoning effort:")
    for i, effort in enumerate(ordered, 1):
        print(f"  {i}. {_label(effort)}")
    n = len(ordered)
    print(f"  {n + 1}. {disable_label}")
    print(f"  {n + 2}. {skip_label}")
    print()

    while True:
        try:
            choice = input(f"Choice [1-{n + 2}] (default: keep current): ").strip()
            if not choice:
                return None
            idx = int(choice)
            if 1 <= idx <= n:
                return ordered[idx - 1]
            if idx == n + 1:
                return "none"
            if idx == n + 2:
                return None
            print(f"Please enter 1-{n + 2}")
        except ValueError:
            print("Please enter a number")
        except (KeyboardInterrupt, EOFError):
            return None




























def cmd_login(args):
    """Authenticate Marlow CLI with a provider."""
    from marlow_cli.auth import login_command

    login_command(args)


def cmd_logout(args):
    """Clear provider authentication."""
    from marlow_cli.auth import logout_command

    logout_command(args)


def cmd_status(args):
    """Show status of all components."""
    from marlow_cli.status import show_status

    show_status(args)


def cmd_cron(args):
    """Cron job management."""
    from marlow_cli.cron import cron_command

    cron_command(args)


def cmd_webhook(args):
    """Webhook subscription management."""
    from marlow_cli.webhook import webhook_command

    webhook_command(args)


def cmd_slack(args):
    """Slack integration helpers.

    Dispatches ``marlow slack <subcommand>``. Currently supports:
      manifest — print or write a Slack app manifest with every gateway
                 command registered as a first-class slash.
    """
    sub = getattr(args, "slack_command", None)
    if sub in {None, ""}:
        # No subcommand — print usage hint.
        print(
            "usage: marlow slack <subcommand>\n"
            "\n"
            "subcommands:\n"
            "  manifest   Generate a Slack app manifest with every gateway\n"
            "             command registered as a native slash\n"
            "\n"
            "Run `marlow slack manifest -h` for details.",
            file=sys.stderr,
        )
        return 1

    if sub == "manifest":
        from marlow_cli.slack_cli import slack_manifest_command

        return slack_manifest_command(args)

    print(f"Unknown slack subcommand: {sub}", file=sys.stderr)
    return 1


def cmd_hooks(args):
    """Shell-hook inspection and management."""
    from marlow_cli.hooks import hooks_command

    hooks_command(args)


def cmd_doctor(args):
    """Check configuration and dependencies."""
    from marlow_cli.doctor import run_doctor

    run_doctor(args)


def cmd_security(args):
    """Dispatch `marlow security <subcmd>`."""
    sub = getattr(args, "security_command", None)
    if sub in ("audit", None):
        from marlow_cli.security_audit import cmd_security_audit

        # Default subcommand is `audit` when no subcmd is given.
        code = cmd_security_audit(args)
        sys.exit(int(code or 0))
    print(f"unknown security subcommand: {sub}", file=sys.stderr)
    sys.exit(2)


def cmd_dump(args):
    """Dump setup summary for support/debugging."""
    from marlow_cli.dump import run_dump

    run_dump(args)


def cmd_debug(args):
    """Debug tools (share report, etc.)."""
    from marlow_cli.debug import run_debug

    run_debug(args)


def cmd_config(args):
    """Configuration management."""
    from marlow_cli.config import config_command

    config_command(args)






def _print_version_info(*, check_updates: bool = True) -> None:
    print(f"Marlow Agent v{__version__} ({__release_date__})")
    print(f"Project: {PROJECT_ROOT}")

    # Show Python version
    print(f"Python: {sys.version.split()[0]}")

    print("Agent runtime: Codex app-server (supported CLI: 0.159.2)")

    if not check_updates:
        return

    # Show update status (synchronous — acceptable since user asked for version info)
    try:
        from marlow_cli.banner import check_for_updates
        from marlow_cli.config import recommended_update_command

        behind = check_for_updates()
        if behind and behind > 0:
            commits_word = "commit" if behind == 1 else "commits"
            print(
                f"Update available: {behind} {commits_word} behind — "
                f"run '{recommended_update_command()}'"
            )
        elif behind == 0:
            print("Up to date")
    except Exception:
        pass


def cmd_version(args):
    """Show version."""
    _print_version_info(check_updates=True)


def cmd_uninstall(args):
    """Uninstall Marlow Agent."""
    _require_tty("uninstall")
    from marlow_cli.uninstall import run_uninstall

    run_uninstall(args)


def _clear_bytecode_cache(root: Path) -> int:
    """Remove all __pycache__ directories under *root*.

    Stale .pyc files can cause ImportError after code updates when Python
    loads a cached bytecode file that references names that no longer exist
    (or don't yet exist) in the updated source.  Clearing them forces Python
    to recompile from the .py source on next import.

    Returns the number of directories removed.
    """
    removed = 0
    for dirpath, dirnames, _ in os.walk(root):
        # Skip venv / node_modules / .git entirely
        dirnames[:] = [
            d
            for d in dirnames
            if d not in {"venv", ".venv", "node_modules", ".git", ".worktrees"}
        ]
        if os.path.basename(dirpath) == "__pycache__":
            try:
                shutil.rmtree(dirpath)
                removed += 1
            except OSError:
                pass
            dirnames.clear()  # nothing left to recurse into
    return removed


# Critical files that every ``marlow`` invocation imports at startup. If any
# of these fail to parse after a pull, the CLI is bricked — the user can't
# even run ``marlow update`` again to roll forward. The post-pull syntax
# guard validates these and auto-rolls-back on failure.
_UPDATE_CRITICAL_FILES = (
    "marlow_cli/main.py",
    "marlow_cli/config.py",
    "marlow_cli/__init__.py",
    "marlow_cli/_parser.py",
    "marlow_cli/query.py",
    "marlow_cli/oneshot.py",
    "run_agent.py",
    "model_tools.py",
    "toolsets.py",
    "marlow_constants.py",
)


def _capture_head_sha(git_cmd, cwd) -> str | None:
    """Return the current HEAD SHA, or None if it can't be resolved."""
    try:
        result = subprocess.run(
            git_cmd + ["rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, OSError):
        return None


def _validate_critical_files_syntax(root) -> tuple[bool, str | None, str | None]:
    """Compile each file in ``_UPDATE_CRITICAL_FILES`` to catch SyntaxErrors.

    These are the files imported on every ``marlow`` startup; if any of them
    has a syntax error (orphan merge-conflict markers, bad ref to a name
    that no longer exists, etc.) the CLI can't bootstrap at all. We validate
    them after a successful ``git pull`` so we can auto-roll-back instead of
    leaving the user with a bricked install.

    The compiled ``.pyc`` is written to a temp directory rather than the
    source tree's ``__pycache__/`` so we don't race with concurrent test
    workers that walk the same dir, and so we don't leave a stale pyc
    behind in production if the next interpreter run picks a different
    Python version. The pyc is discarded on function return either way —
    we only care about the compile-or-not signal.

    Returns ``(ok, failing_path, error_message)``. ``ok=True`` means every
    file parsed cleanly.
    """
    import py_compile
    import tempfile

    root = Path(root)
    with tempfile.TemporaryDirectory(prefix="marlow-syntax-check-") as tmpdir:
        for relpath in _UPDATE_CRITICAL_FILES:
            path = root / relpath
            if not path.exists():
                # Missing file is suspicious but not necessarily fatal — a future
                # refactor may legitimately remove one of these. Skip and move on.
                continue
            # Mirror the relative path under the tmpdir so two different
            # files with the same basename don't collide on the cfile name.
            cfile = Path(tmpdir) / (relpath.replace("/", "__") + "c")
            try:
                py_compile.compile(str(path), cfile=str(cfile), doraise=True)
            except py_compile.PyCompileError as exc:
                return False, str(path), str(exc)
            except OSError as exc:
                return False, str(path), f"could not read: {exc}"
    return True, None, None


def _gateway_prompt(prompt_text: str, default: str = "", timeout: float = 300.0) -> str:
    """File-based IPC prompt for gateway mode.

    Writes a prompt marker file so the gateway can forward the question to the
    user, then polls for a response file.  Falls back to *default* on timeout.

    Used by ``marlow update --gateway`` so interactive prompts (stash restore,
    config migration) are forwarded to the messenger instead of being silently
    skipped.
    """
    import json as _json
    import uuid as _uuid
    from marlow_constants import get_marlow_home

    home = get_marlow_home()
    prompt_path = home / ".update_prompt.json"
    response_path = home / ".update_response"

    # Clean any stale response file
    response_path.unlink(missing_ok=True)

    payload = {
        "prompt": prompt_text,
        "default": default,
        "id": str(_uuid.uuid4()),
    }
    tmp = prompt_path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(payload))
    tmp.replace(prompt_path)

    # Poll for response
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if response_path.exists():
            try:
                answer = response_path.read_text().strip()
                response_path.unlink(missing_ok=True)
                prompt_path.unlink(missing_ok=True)
                return answer if answer else default
            except (OSError, ValueError):
                pass
        _time.sleep(0.5)

    # Timeout — clean up and use default
    prompt_path.unlink(missing_ok=True)
    response_path.unlink(missing_ok=True)
    print(f"  (no response after {int(timeout)}s, using default: {default!r})")
    return default




def _run_with_idle_timeout(
    cmd: list[str],
    cwd: Path,
    *,
    idle_timeout_seconds: int = 180,
    indent: str = "    ",
) -> subprocess.CompletedProcess:
    """Run a subprocess that streams output, with an idle-output timeout.

    Issue #33788: ``npm run build`` (Vite) was invoked with
    ``capture_output=True`` and no timeout. On low-memory hosts the build can stall or sit silent for
    minutes; users see a frozen terminal, assume the update is hung, and
    reboot — leaving the editable install in a half-state with the
    ``marlow`` launcher present but ``marlow_cli`` not importable.

    This helper fixes both halves: stdout is streamed (so the user sees
    progress), and if no bytes have appeared on stdout/stderr for
    ``idle_timeout_seconds``, the process is terminated and the call
    returns with a non-zero ``returncode``. The caller's existing
    stale-dist fallback (#23817) takes over from there.

    Returns a ``CompletedProcess`` with merged stdout (text), empty
    stderr, and an integer returncode. Never raises on idle timeout —
    propagation of failure is via the returncode.
    """
    merged_chunks: list[str] = []
    last_output_ts = _time.monotonic()
    lock = threading.Lock()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        # E.g. npm not on PATH between the which() check and now.
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(exc))

    def _reader() -> None:
        nonlocal last_output_ts
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                print(f"{indent}{line.rstrip()}", flush=True)
            except UnicodeEncodeError:
                enc = getattr(sys.stdout, "encoding", None) or "ascii"
                safe = line.rstrip().encode(enc, errors="replace").decode(enc, errors="replace")
                print(f"{indent}{safe}", flush=True)
            with lock:
                merged_chunks.append(line)
                last_output_ts = _time.monotonic()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    idle_killed = False
    while True:
        try:
            rc = proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            with lock:
                idle = _time.monotonic() - last_output_ts
            if idle > idle_timeout_seconds:
                idle_killed = True
                proc.terminate()
                try:
                    rc = proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    rc = proc.wait()
                break

    # Drain reader so we don't leak the stdout file descriptor.
    reader_thread.join(timeout=2)

    combined = "".join(merged_chunks)
    if idle_killed:
        msg = (
            f"\n  ⚠ Build produced no output for {idle_timeout_seconds}s — terminated.\n"
            "    Common causes: out-of-memory on a low-RAM host or container,\n"
            "    a stuck Node process, or an antivirus scan stalling I/O.\n"
        )
        combined += msg
        # Force a non-zero rc even if terminate() raced with a clean exit.
        if rc == 0:
            rc = 124  # GNU `timeout` convention
    return subprocess.CompletedProcess(cmd, rc, stdout=combined, stderr="")


def _run_npm_install_deterministic(
    npm: str,
    cwd: Path,
    *,
    extra_args: tuple[str, ...] = (),
    capture_output: bool = True,
) -> subprocess.CompletedProcess:
    """Run a deterministic npm install that does not mutate ``package-lock.json``.

    Prefers ``npm ci`` (strict, lockfile-preserving) when a lockfile is present;
    falls back to ``npm install`` only if ``npm ci`` fails (e.g. lockfile out of
    sync on a WIP checkout).  Without this, ``npm install`` on npm ≥ 10 silently
    rewrites committed lockfiles (stripping ``"peer": true`` etc.), which leaves
    the working tree dirty and causes the next ``marlow update`` to stash the
    lockfile — repeatedly.
    """
    lockfile = cwd / "package-lock.json"
    if lockfile.exists():
        ci_cmd = [npm, "ci", *extra_args]
        ci_result = subprocess.run(
            ci_cmd,
            cwd=cwd,
            capture_output=capture_output,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if ci_result.returncode == 0:
            return ci_result
        # Fall through to `npm install` — lockfile may be out of sync on a
        # WIP fork/branch, or `npm ci` may not be available on very old npm.
    install_cmd = [npm, "install", *extra_args]
    return subprocess.run(
        install_cmd,
        cwd=cwd,
        capture_output=capture_output,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )






def _stash_local_changes_if_needed(git_cmd: list[str], cwd: Path) -> Optional[str]:
    status = subprocess.run(
        git_cmd + ["status", "--porcelain"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    if not status.stdout.strip():
        return None

    # If the index has unmerged entries (e.g. from an interrupted merge/rebase),
    # git stash will fail with "needs merge / could not write index".  Clear the
    # conflict state with `git reset` so the stash can proceed.  Working-tree
    # changes are preserved; only the index conflict markers are dropped.
    unmerged = subprocess.run(
        git_cmd + ["ls-files", "--unmerged"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if unmerged.stdout.strip():
        print("→ Clearing unmerged index entries from a previous conflict...")
        subprocess.run(git_cmd + ["reset"], cwd=cwd, capture_output=True)

    from datetime import datetime, timezone

    stash_name = datetime.now(timezone.utc).strftime(
        "marlow-update-autostash-%Y%m%d-%H%M%S"
    )
    print("→ Local changes detected — stashing before update...")
    subprocess.run(
        git_cmd + ["stash", "push", "--include-untracked", "-m", stash_name],
        cwd=cwd,
        check=True,
    )
    stash_ref = subprocess.run(
        git_cmd + ["rev-parse", "--verify", "refs/stash"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return stash_ref


def _resolve_stash_selector(
    git_cmd: list[str], cwd: Path, stash_ref: str
) -> Optional[str]:
    stash_list = subprocess.run(
        git_cmd + ["stash", "list", "--format=%gd %H"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    for line in stash_list.stdout.splitlines():
        selector, _, commit = line.partition(" ")
        if commit.strip() == stash_ref:
            return selector.strip()
    return None


def _print_stash_cleanup_guidance(
    stash_ref: str, stash_selector: Optional[str] = None
) -> None:
    print(
        "  Check `git status` first so you don't accidentally reapply the same change twice."
    )
    print("  Find the saved entry with: git stash list --format='%gd %H %s'")
    if stash_selector:
        print(f"  Remove it with: git stash drop {stash_selector}")
    else:
        print(
            f"  Look for commit {stash_ref}, then drop its selector with: git stash drop stash@{{N}}"
        )


def _restore_stashed_changes(
    git_cmd: list[str],
    cwd: Path,
    stash_ref: str,
    prompt_user: bool = False,
    input_fn=None,
) -> bool:
    if prompt_user:
        print()
        print("⚠ Local changes were stashed before updating.")
        print(
            "  Restoring them may reapply local customizations onto the updated codebase."
        )
        print("  Review the result afterward if Marlow behaves unexpectedly.")
        print("Restore local changes now? [Y/n]")
        if input_fn is not None:
            response = input_fn("Restore local changes now? [Y/n]", "y")
        else:
            response = input().strip().lower()
        if response not in {"", "y", "yes"}:
            print("Skipped restoring local changes.")
            print("Your changes are still preserved in git stash.")
            print(f"Restore manually with: git stash apply {stash_ref}")
            return False

    print("→ Restoring local changes...")
    restore = subprocess.run(
        git_cmd + ["stash", "apply", stash_ref],
        cwd=cwd,
        capture_output=True,
        text=True,
    )

    # Check for unmerged (conflicted) files — can happen even when returncode is 0
    unmerged = subprocess.run(
        git_cmd + ["diff", "--name-only", "--diff-filter=U"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    has_conflicts = bool(unmerged.stdout.strip())

    if restore.returncode != 0 or has_conflicts:
        print("✗ Update pulled new code, but restoring local changes hit conflicts.")
        if restore.stdout.strip():
            print(restore.stdout.strip())
        if restore.stderr.strip():
            print(restore.stderr.strip())

        # Show which files conflicted
        conflicted_files = unmerged.stdout.strip()
        if conflicted_files:
            print("\nConflicted files:")
            for f in conflicted_files.splitlines():
                print(f"  • {f}")

        print("\nYour stashed changes are preserved — nothing is lost.")
        print(f"  Stash ref: {stash_ref}")

        # Always reset to clean state — leaving conflict markers in source
        # files makes marlow completely unrunnable (SyntaxError on import).
        # The user's changes are safe in the stash for manual recovery.
        subprocess.run(
            git_cmd + ["reset", "--hard", "HEAD"],
            cwd=cwd,
            capture_output=True,
        )
        print("Working tree reset to clean state.")
        print(f"Restore your changes later with: git stash apply {stash_ref}")
        # Don't sys.exit — the code update itself succeeded, only the stash
        # restore had conflicts. Let cmd_update continue with pip install
        # and gateway restart.
        return False

    stash_selector = _resolve_stash_selector(git_cmd, cwd, stash_ref)
    if stash_selector is None:
        print(
            "⚠ Local changes were restored, but Marlow couldn't find the stash entry to drop."
        )
        print(
            "  The stash was left in place. You can remove it manually after checking the result."
        )
        _print_stash_cleanup_guidance(stash_ref)
    else:
        drop = subprocess.run(
            git_cmd + ["stash", "drop", stash_selector],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if drop.returncode != 0:
            print(
                "⚠ Local changes were restored, but Marlow couldn't drop the saved stash entry."
            )
            if drop.stdout.strip():
                print(drop.stdout.strip())
            if drop.stderr.strip():
                print(drop.stderr.strip())
            print(
                "  The stash was left in place. You can remove it manually after checking the result."
            )
            _print_stash_cleanup_guidance(stash_ref, stash_selector)

    print("⚠ Local changes were restored on top of the updated codebase.")
    print("  Review `git diff` / `git status` if Marlow behaves unexpectedly.")
    return True


def _invalidate_update_cache():
    """Delete the update-check cache for ALL profiles so no banner
    reports a stale "commits behind" count after a successful update.

    The git repo is shared across profiles — when one profile runs
    ``marlow update``, every profile is now current.
    """
    homes = []
    # Default profile home (Docker-aware — uses /opt/data in Docker)
    from marlow_constants import get_default_marlow_root

    default_home = get_default_marlow_root()
    homes.append(default_home)
    # Named profiles under <root>/profiles/
    profiles_root = default_home / "profiles"
    if profiles_root.is_dir():
        for entry in profiles_root.iterdir():
            if entry.is_dir():
                homes.append(entry)
    for home in homes:
        try:
            cache_file = home / ".update_check"
            if cache_file.exists():
                cache_file.unlink()
        except Exception:
            pass


def _load_installable_optional_extras(group: str = "all") -> list[str]:
    """Return optional extras referenced by a dependency group.

    ``group`` is normally ``all`` for the broad install.
    """
    try:
        import tomllib

        with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
    except Exception:
        return []

    optional_deps = project.get("optional-dependencies", {})
    if not isinstance(optional_deps, dict):
        return []

    refs = optional_deps.get(group, [])
    referenced: list[str] = []
    for ref in refs:
        if "[" in ref and "]" in ref:
            name = ref.split("[", 1)[1].split("]", 1)[0]
            if name in optional_deps:
                referenced.append(name)

    return referenced


def _run_install_with_heartbeat(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    heartbeat_interval_seconds: int = 30,
) -> None:
    """Run dependency install command with periodic heartbeat output.

    Some resolvers/build backends (especially when compiling Rust/C extensions)
    can stay quiet for minutes. Emit a simple elapsed-time heartbeat so users
    know ``marlow update`` is still progressing even if pip/uv itself is silent.
    """
    done = threading.Event()
    start = _time.time()

    def _heartbeat() -> None:
        # Wait first, then print, so short installs don't emit noise.
        while not done.wait(heartbeat_interval_seconds):
            elapsed = int(_time.time() - start)
            print(
                f"  … still installing dependencies ({elapsed}s elapsed)"
                " — compiling Rust/C extensions can take several minutes",
                flush=True,
            )

    t = threading.Thread(target=_heartbeat, daemon=True)
    t.start()
    try:
        subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            check=True,
            env=env,
        )
    finally:
        done.set()
        t.join(timeout=0.2)


def _refresh_active_lazy_features() -> None:
    """Refresh lazy-installed backends after a code update.

    When pyproject.toml's ``[all]`` extra was slimmed down (May 2026), most
    optional backends moved to ``tools/lazy_deps.py`` and only install on
    first use. ``marlow update`` runs ``uv pip install -e .[all]`` which
    leaves those packages untouched — so if we bump a pin in
    :data:`LAZY_DEPS` (CVE response, transitive bug fix), users who already
    activated the backend keep the stale version forever.

    This function asks lazy_deps which features the user has previously
    activated and reinstalls them under the current pins. Features the
    user never enabled stay quiet — no churn for cold backends.

    Never raises. A failure here must not block the rest of the update.
    """
    try:
        from tools import lazy_deps
    except Exception as exc:
        logger.debug("Lazy refresh skipped (import failed): %s", exc)
        return

    try:
        active = lazy_deps.active_features()
    except Exception as exc:
        logger.debug("Lazy refresh skipped (active_features failed): %s", exc)
        return

    if not active:
        return

    print()
    print(f"→ Refreshing {len(active)} active lazy backend(s)...")

    try:
        results = lazy_deps.refresh_active_features(prompt=False)
    except Exception as exc:
        # refresh_active_features is documented as never-raise, but defend
        # the update flow against future regressions.
        print(f"  ⚠ Lazy refresh failed unexpectedly: {exc}")
        return

    refreshed = [f for f, s in results.items() if s == "refreshed"]
    current = [f for f, s in results.items() if s == "current"]
    failed = [(f, s) for f, s in results.items() if s.startswith("failed:")]
    skipped = [(f, s) for f, s in results.items() if s.startswith("skipped:")]

    if refreshed:
        print(f"  ↑ {len(refreshed)} refreshed: {', '.join(refreshed)}")
    if current:
        print(f"  ✓ {len(current)} already current")
    if skipped:
        # Most common reason: security.allow_lazy_installs=false. Show one
        # line so the user knows why; not an error.
        names = ", ".join(f for f, _ in skipped)
        reason = skipped[0][1].split(": ", 1)[-1]
        print(f"  · {len(skipped)} skipped ({reason}): {names}")
    if failed:
        for feature, status in failed:
            reason = status.split(": ", 1)[-1]
            # Clip noisy pip stderr to keep update output legible.
            if len(reason) > 200:
                reason = reason[:200] + "..."
            print(f"  ⚠ {feature} failed to refresh: {reason}")
        print("  Backends keep their previously-installed version; rerun")
        print("  `marlow update` once the upstream issue is resolved.")


def _install_python_dependencies_with_optional_fallback(
    install_cmd_prefix: list[str],
    *,
    env: dict[str, str] | None = None,
    group: str = "all",
) -> None:
    """Install base deps plus as many optional extras as the environment supports.

    By default this targets ``.[all]``.
    """
    def _install(args: list[str]) -> None:
        _run_install_with_heartbeat(install_cmd_prefix + args, env=env)

    try:
        _install(["install", "-e", f".[{group}]"])
        return
    except subprocess.CalledProcessError:
        print(
            "  ⚠ Optional extras failed, reinstalling base dependencies and retrying extras individually..."
        )

    _install(["install", "-e", "."])

    failed_extras: list[str] = []
    installed_extras: list[str] = []
    for extra in _load_installable_optional_extras(group=group):
        try:
            _install(["install", "-e", f".[{extra}]"])
            installed_extras.append(extra)
        except subprocess.CalledProcessError:
            failed_extras.append(extra)

    if installed_extras:
        print(
            f"  ✓ Reinstalled optional extras individually: {', '.join(installed_extras)}"
        )
    if failed_extras:
        print(
            f"  ⚠ Skipped optional extras that still failed: {', '.join(failed_extras)}"
        )


def _update_node_dependencies() -> None:
    npm = shutil.which("npm")
    if not npm:
        return

    if not (PROJECT_ROOT / "package.json").exists():
        return

    print("→ Updating browser-tool dependencies...")
    result = _run_npm_install_deterministic(
        npm,
        PROJECT_ROOT,
        extra_args=("--no-fund", "--no-audit", "--progress=false"),
        capture_output=False,
    )
    if result.returncode != 0:
        print("  ⚠ npm install failed in repo root")


class _UpdateOutputStream:
    """Stream wrapper used during ``marlow update`` to survive terminal loss.

    Wraps the process's original stdout/stderr so that:

    * Every write is also mirrored to an append-only log file
      (``~/.marlow/logs/update.log``) that users can inspect after the
      terminal disconnects.
    * Writes to the original stream that fail with ``BrokenPipeError`` /
      ``OSError`` / ``ValueError`` (closed file) no longer cascade into
      process exit — the update keeps going, only the on-screen output
      stops.

    Combined with ``SIGHUP -> SIG_IGN`` installed by
    ``_install_hangup_protection``, this makes ``marlow update`` safe to
    run in a plain SSH session that might disconnect mid-install.
    """

    def __init__(self, original, log_file):
        self._original = original
        self._log = log_file
        self._original_broken = False

    def write(self, data):
        # Mirror to the log file first — it's the most reliable destination.
        if self._log is not None:
            try:
                self._log.write(data)
            except Exception:
                # Log errors should never abort the update.
                pass

        if self._original_broken:
            return len(data) if isinstance(data, (str, bytes)) else 0

        try:
            return self._original.write(data)
        except (BrokenPipeError, OSError, ValueError):
            # Terminal vanished (SSH disconnect, shell close).  Stop trying
            # to write to it, but keep the update running.
            self._original_broken = True
            return len(data) if isinstance(data, (str, bytes)) else 0

    def flush(self):
        if self._log is not None:
            try:
                self._log.flush()
            except Exception:
                pass
        if self._original_broken:
            return
        try:
            self._original.flush()
        except (BrokenPipeError, OSError, ValueError):
            self._original_broken = True

    def isatty(self):
        if self._original_broken:
            return False
        try:
            return self._original.isatty()
        except Exception:
            return False

    def fileno(self):
        # Some tools probe fileno(); defer to the underlying stream and let
        # callers handle failures (same behaviour as the unwrapped stream).
        return self._original.fileno()

    def __getattr__(self, name):
        return getattr(self._original, name)


def _install_hangup_protection(gateway_mode: bool = False):
    """Protect ``cmd_update`` from SIGHUP and broken terminal pipes.

    Users commonly run ``marlow update`` in an SSH session or a terminal
    that may close mid-install.  Without protection, ``SIGHUP`` from the
    terminal kills the Python process during ``pip install`` and leaves
    the venv half-installed; the documented workaround ("use screen /
    tmux") shouldn't be required for something as routine as an update.

    Protections installed:

    1. ``SIGHUP`` is set to ``SIG_IGN``.  POSIX preserves ``SIG_IGN``
       across ``exec()``, so pip and git subprocesses also stop dying on
       hangup.
    2. ``sys.stdout`` / ``sys.stderr`` are wrapped to mirror output to
       ``~/.marlow/logs/update.log`` and to silently absorb
       ``BrokenPipeError`` when the terminal vanishes.

    ``SIGINT`` (Ctrl-C) and ``SIGTERM`` (systemd shutdown) are
    **intentionally left alone** — those are legitimate cancellation
    signals the user or OS sent on purpose.

    In gateway mode (``marlow update --gateway``) the update is already
    spawned detached from a terminal, so this function is a no-op.

    Returns a dict that ``cmd_update`` can pass to
    ``_finalize_update_output`` on exit.  Returning a dict rather than a
    tuple keeps the call site forward-compatible with future additions.
    """
    state = {
        "prev_stdout": sys.stdout,
        "prev_stderr": sys.stderr,
        "log_file": None,
        "installed": False,
    }

    if gateway_mode:
        return state

    import signal as _signal

    # (1) Ignore SIGHUP for the remainder of this process.
    if hasattr(_signal, "SIGHUP"):
        try:
            _signal.signal(_signal.SIGHUP, _signal.SIG_IGN)
        except (ValueError, OSError):
            # Called from a non-main thread — not fatal.  The update still
            # runs, just without hangup protection.
            pass

    # (2) Mirror output to update.log and wrap stdio for broken-pipe
    # tolerance.  Any failure here is non-fatal; we just skip the wrap.
    try:
        # Late-bound import so tests can monkeypatch
        # marlow_cli.config.get_marlow_home to simulate setup failure.
        from marlow_cli.config import get_marlow_home as _get_marlow_home

        logs_dir = _get_marlow_home() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "update.log"
        log_file = open(log_path, "a", buffering=1, encoding="utf-8")

        import datetime as _dt

        log_file.write(
            f"\n=== marlow update started "
            f"{_dt.datetime.now().isoformat(timespec='seconds')} ===\n"
        )

        state["log_file"] = log_file
        sys.stdout = _UpdateOutputStream(state["prev_stdout"], log_file)
        sys.stderr = _UpdateOutputStream(state["prev_stderr"], log_file)
        state["installed"] = True
    except Exception:
        # Leave stdio untouched on any setup failure.  Update continues
        # without mirroring.
        state["log_file"] = None

    return state


def _finalize_update_output(state):
    """Restore stdio and close the update.log handle opened by ``_install_hangup_protection``."""
    if not state:
        return
    if state.get("installed"):
        try:
            sys.stdout = state.get("prev_stdout", sys.stdout)
        except Exception:
            pass
        try:
            sys.stderr = state.get("prev_stderr", sys.stderr)
        except Exception:
            pass
    log_file = state.get("log_file")
    if log_file is not None:
        try:
            log_file.flush()
            log_file.close()
        except Exception:
            pass


def _resolve_update_branch(args) -> str:
    """Normalize ``args.branch`` into a non-empty branch name.

    Centralizes the "default to main, accept --branch override, treat empty
    or whitespace-only values as the default" parsing so every consumer of
    ``--branch`` (check path, git-update path, ZIP-fallback path) agrees on
    the same answer.
    """
    return (getattr(args, "branch", None) or "main").strip() or "main"


def _cmd_update_check(branch: str = "main", *, branch_explicit: bool = False):
    """Implement ``marlow update --check``: fetch and report without installing.

    ``branch`` selects which branch the check compares against. Default is
    "main"; callers can pass another branch to ask "are there new commits
    on origin/<branch>?" without performing the update.

    ``branch_explicit`` is True iff the caller passed --branch on the CLI.
    PyPI installs can't honor non-default branches, so when this is True
    on a PyPI install we surface a one-line notice instead of silently
    dropping the flag.
    """
    from marlow_cli.config import detect_install_method
    method = detect_install_method(PROJECT_ROOT)
    if method == "docker":
        # Docker can't ``git fetch`` from within the container.  Surface the
        # same long-form source-rebuild guidance ``marlow update`` (apply
        # path) uses — telling the user to "reinstall via curl" or that
        # ".git is missing" would point them at the wrong remediation.
        from marlow_cli.config import format_docker_update_message
        print(format_docker_update_message())
        sys.exit(1)
    if method == "pip":
        from marlow_cli.config import recommended_update_command
        from marlow_cli.banner import check_via_pypi
        if branch_explicit and branch != "main":
            print(f"⚠ --branch is ignored for PyPI installs (would have checked '{branch}').")
        result = check_via_pypi()
        if result is None:
            print("✗ Could not reach PyPI to check for updates.")
            sys.exit(1)
        elif result == 0:
            print("✓ Already up to date.")
        else:
            print("⚕ Update available on PyPI.")
            print(f"  Run '{recommended_update_command()}' to install.")
        return

    git_dir = PROJECT_ROOT / ".git"
    if not git_dir.exists():
        print("✗ Not a git repository — cannot check for updates.")
        sys.exit(1)

    git_cmd = ["git"]

    print("→ Fetching from origin...")
    fetch_result = subprocess.run(
        git_cmd + ["fetch", "origin"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    compare_branch = f"origin/{branch}"

    if fetch_result.returncode != 0:
        stderr = fetch_result.stderr.strip()
        if "Could not resolve host" in stderr or "unable to access" in stderr:
            print("✗ Network error — cannot reach the remote repository.")
        elif "Authentication failed" in stderr or "could not read Username" in stderr:
            print("✗ Authentication failed — check your git credentials or SSH key.")
        else:
            print("✗ Failed to fetch.")
            if stderr:
                print(f"  {stderr.splitlines()[0]}")
        sys.exit(1)

    # Verify the compare ref actually exists before asking rev-list about it.
    # Without this, `git rev-list HEAD..origin/<bogus> --count` exits 128 and
    # (with check=True) raises CalledProcessError, surfacing a Python
    # traceback. Friendlier to detect-and-report.
    verify_result = subprocess.run(
        git_cmd + ["rev-parse", "--verify", "--quiet", compare_branch],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if verify_result.returncode != 0:
        print(f"✗ Branch '{branch}' not found on {compare_branch.split('/', 1)[0]}.")
        sys.exit(1)

    rev_result = subprocess.run(
        git_cmd + ["rev-list", f"HEAD..{compare_branch}", "--count"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    behind = int(rev_result.stdout.strip())

    if behind == 0:
        print("✓ Already up to date.")
    else:
        commits_word = "commit" if behind == 1 else "commits"
        print(f"⚕ Update available: {behind} {commits_word} behind {compare_branch}.")
        from marlow_cli.config import recommended_update_command

        print(f"  Run '{recommended_update_command()}' to install.")


def _ensure_fhs_path_guard() -> None:
    """Ensure /usr/local/bin is on PATH for RHEL-family root non-login shells.

    Mirrors the post-symlink probe added to ``scripts/install.sh`` so that
    existing FHS-layout root installs on RHEL/CentOS/Rocky/Alma 8+ get
    repaired on ``marlow update`` without requiring a reinstall.  The
    installer's assumption that ``/usr/local/bin`` is on PATH for every
    standard shell breaks on those distros in non-login interactive shells
    (su, sudo -s, tmux panes, some web terminals): /etc/bashrc doesn't
    add /usr/local/bin and /root/.bash_profile doesn't either.  Symptom:
    ``marlow`` prints ``command not found`` even though the symlink lives
    at /usr/local/bin/marlow.

    Silent no-op on: non-Linux, non-root, non-FHS installs, and any system
    where ``bash -i -c 'command -v marlow'`` already resolves.  Idempotent.
    """
    if sys.platform != "linux":
        return
    try:
        if os.geteuid() != 0:
            return
    except AttributeError:
        return
    # Only act when this is actually an FHS-layout install (command link at
    # /usr/local/bin/marlow, code at /usr/local/lib/marlow-agent).
    fhs_link = Path("/usr/local/bin/marlow")
    if not fhs_link.is_symlink() and not fhs_link.exists():
        return

    # Probe a fresh non-login interactive bash the way the user will use it.
    # ``bash -i -c`` sources ~/.bashrc but NOT ~/.bash_profile or /etc/profile,
    # which is the exact scenario where RHEL root loses /usr/local/bin.
    home = os.environ.get("HOME") or "/root"
    try:
        probe = subprocess.run(
            [
                "env",
                "-i",
                f"HOME={home}",
                f"TERM={os.environ.get('TERM', 'dumb')}",
                "bash",
                "-i",
                "-c",
                "command -v marlow",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return  # no bash or probe hung — don't block update on this
    if probe.returncode == 0:
        return  # already on PATH, nothing to do

    path_line = 'export PATH="/usr/local/bin:$PATH"'
    path_comment = (
        "# Marlow Agent — ensure /usr/local/bin is on PATH " "(RHEL non-login shells)"
    )
    wrote_any = False
    for candidate in (".bashrc", ".bash_profile"):
        cfg = Path(home) / candidate
        if not cfg.is_file():
            continue
        try:
            existing = cfg.read_text(errors="replace")
        except OSError:
            continue
        # Idempotency: skip if any uncommented PATH= line already references
        # /usr/local/bin.  Mirrors the grep pattern used by install.sh.
        already_guarded = any(
            "/usr/local/bin" in line
            and "PATH" in line
            and not line.lstrip().startswith("#")
            for line in existing.splitlines()
        )
        if already_guarded:
            continue
        try:
            with cfg.open("a", encoding="utf-8") as f:
                f.write("\n" + path_comment + "\n" + path_line + "\n")
        except OSError as e:
            print(f"  ⚠ Could not update {cfg}: {e}")
            continue
        print(f"  ✓ Added /usr/local/bin to PATH in {cfg}")
        wrote_any = True
    if wrote_any:
        print("    (reload your shell or run 'source ~/.bashrc' to pick it up)")




def _discard_lockfile_churn(git_cmd, repo_root):
    """Restore tracked ``package-lock.json`` files that npm dirtied locally.

    npm rewrites lockfiles non-deterministically at install/build time. On a
    managed install those diffs are never intentional, so we discard them so
    ``marlow update`` sees a clean tree instead of autostashing every run.
    Best-effort; only ever touches files named ``package-lock.json``.
    """
    try:
        diff = subprocess.run(
            git_cmd + ["diff", "--name-only"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if diff.returncode != 0:
            return
        dirty = [
            line.strip()
            for line in diff.stdout.splitlines()
            if line.strip().endswith("package-lock.json")
        ]
        if not dirty:
            return
        subprocess.run(
            git_cmd + ["checkout", "--", *dirty],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        print(f"→ Discarded npm lockfile churn ({len(dirty)} file(s))")
    except Exception:
        # Never let lockfile cleanup block an update.
        pass


def cmd_update(args):
    """Update Marlow Agent to the latest version.

    Thin wrapper around ``_cmd_update_impl``: installs hangup protection,
    runs the update, then restores stdio on the way out (even on
    ``sys.exit`` or unhandled exceptions).
    """
    from marlow_cli.config import (
        detect_install_method,
        format_docker_update_message,
        is_managed,
        managed_error,
    )

    if is_managed():
        managed_error("update Marlow Agent")
        return

    # Docker users can't ``git pull`` — the image excludes ``.git`` from
    # the build context. Bail with a friendly explanation pointing at a
    # host-side source rebuild BEFORE any apply/check branches
    # below get a chance to error out with misleading "Not a git
    # repository" text.  See format_docker_update_message() for the full
    # rationale and config-persistence notes.
    if detect_install_method(PROJECT_ROOT) == "docker":
        print(format_docker_update_message())
        sys.exit(1)

    if getattr(args, "check", False):
        # --check honors --branch so the "any new commits?" answer matches
        # what a subsequent `marlow update --branch=<x>` would actually pull.
        branch = _resolve_update_branch(args)
        _cmd_update_check(
            branch=branch,
            branch_explicit=bool(getattr(args, "branch", None)),
        )
        return

    gateway_mode = getattr(args, "gateway", False)

    # Protect against mid-update terminal disconnects (SIGHUP) and tolerate
    # writes to a closed stdout.  No-op in gateway mode.  See
    # _install_hangup_protection for rationale.
    _update_io_state = _install_hangup_protection(gateway_mode=gateway_mode)
    try:
        _cmd_update_impl(args, gateway_mode=gateway_mode)
    finally:
        _finalize_update_output(_update_io_state)


def _cmd_update_pip(args):
    """Update Marlow via pip (for PyPI installs)."""
    from marlow_cli import __version__
    from marlow_cli.config import is_uv_tool_install

    print(f"→ Current version: {__version__}")
    print("→ Checking PyPI for updates...")

    from marlow_cli.managed_uv import ensure_uv, update_managed_uv

    # Keep managed uv current before using it.
    update_managed_uv()

    uv, _fresh_bootstrap = ensure_uv()
    in_venv = sys.prefix != sys.base_prefix
    # pipx-managed installs live under .../pipx/venvs/<name>/...
    pipx_managed = "pipx" in sys.prefix.split(os.sep)
    pipx = shutil.which("pipx") if pipx_managed else None

    # Only the ``uv pip install`` path inside a venv needs VIRTUAL_ENV
    # exported (uv refuses to install without it when the launcher shim
    # didn't activate the venv). ``uv tool upgrade`` / ``pipx upgrade``
    # operate on a named environment and ignore VIRTUAL_ENV, so we don't
    # set it for them.
    export_virtualenv = False

    if is_uv_tool_install():
        if not uv:
            print("✗ Detected a uv-tool install but managed uv install failed.")
            print("  Install uv manually: https://docs.astral.sh/uv/getting-started/installation/")
            sys.exit(1)
        cmd = [uv, "tool", "upgrade", "marlow-agent"]
    elif pipx_managed and pipx:
        # pipx owns its own venv; ``pipx upgrade`` is the only correct path.
        # Matches scripts/auto-update.sh, which already uses pipx upgrade.
        cmd = [pipx, "upgrade", "marlow-agent"]
    elif uv:
        cmd = [uv, "pip", "install", "--upgrade", "marlow-agent"]
        if in_venv:
            # Launcher shim runs the venv interpreter but doesn't export
            # VIRTUAL_ENV; without it uv errors "No virtual environment found".
            export_virtualenv = True
        else:
            # Outside any venv, ``--system`` lets uv target the active
            # interpreter, matching pip's default behaviour.
            cmd.insert(3, "--system")
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "marlow-agent"]

    print(f"→ Running: {' '.join(cmd)}")
    run_kwargs = {}
    if export_virtualenv:
        run_kwargs["env"] = {**os.environ, "VIRTUAL_ENV": sys.prefix}
    result = subprocess.run(cmd, **run_kwargs)
    if result.returncode != 0:
        print("✗ Update failed")
        sys.exit(1)

    print("✓ Update complete! Restart marlow to use the new version.")


def _cmd_update_impl(args, gateway_mode: bool):
    """Body of ``cmd_update`` — kept separate so the wrapper can always
    restore stdio even on ``sys.exit``."""
    # In gateway mode, use file-based IPC for prompts instead of stdin
    gw_input_fn = (
        (lambda prompt, default="": _gateway_prompt(prompt, default))
        if gateway_mode
        else None
    )
    assume_yes = bool(getattr(args, "yes", False))

    print("⚕ Updating Marlow Agent...")
    print()

    git_dir = PROJECT_ROOT / ".git"

    if not git_dir.exists():
        from marlow_cli.config import detect_install_method
        method = detect_install_method(PROJECT_ROOT)
        if method == "pip":
            _cmd_update_pip(args)
            return
        print("✗ Not a git repository. Please reinstall:")
        print(f"  curl -fsSL {MARLOW_INSTALL_SCRIPT_URL} | bash")
        sys.exit(1)

    # Build the git command once for the update pipeline.
    git_cmd = ["git"]

    # Discard npm lockfile churn before any stash/branch logic. npm rewrites
    # tracked package-lock.json files non-deterministically at install/build
    # time (platform-specific optional deps, ideallyInert annotations, etc.),
    # which is never an intentional edit on a managed install but leaves the
    # tree dirty — forcing an autostash on every update and making branch
    # switches fragile. Restoring them first lets the common case (only
    # lockfile churn) update with a clean tree.
    _discard_lockfile_churn(git_cmd, PROJECT_ROOT)

    # Fetch and pull
    try:

        print("→ Fetching updates...")
        fetch_result = subprocess.run(
            git_cmd + ["fetch", "origin"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        if fetch_result.returncode != 0:
            stderr = fetch_result.stderr.strip()
            if "Could not resolve host" in stderr or "unable to access" in stderr:
                print("✗ Network error — cannot reach the remote repository.")
                print(f"  {stderr.splitlines()[0]}" if stderr else "")
            elif (
                "Authentication failed" in stderr or "could not read Username" in stderr
            ):
                print(
                    "✗ Authentication failed — check your git credentials or SSH key."
                )
            else:
                print(f"✗ Failed to fetch updates from origin.")
                if stderr:
                    print(f"  {stderr.splitlines()[0]}")
            sys.exit(1)

        # Get current branch (returns literal "HEAD" when detached)
        result = subprocess.run(
            git_cmd + ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        current_branch = result.stdout.strip()

        # Determine the target branch. Default is "main" (the long-standing
        # CLI behavior); --branch overrides for callers that want to update
        # against a non-default channel.
        branch = _resolve_update_branch(args)

        # If user is on a different branch than the update target, switch
        # to the target. When the target is "main" this is the historical
        # "always update against main" behavior; for any other target it's
        # the same thing — get HEAD onto the requested branch first, then
        # fast-forward.
        if current_branch != branch:
            label = (
                "detached HEAD"
                if current_branch == "HEAD"
                else f"branch '{current_branch}'"
            )
            print(f"  ⚠ Currently on {label} — switching to {branch} for update...")
            # Stash before checkout so uncommitted work isn't lost
            auto_stash_ref = _stash_local_changes_if_needed(git_cmd, PROJECT_ROOT)
            checkout_result = subprocess.run(
                git_cmd + ["checkout", branch],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
            )
            if checkout_result.returncode != 0:
                # Local checkout doesn't have this branch yet. Try to set
                # it up as a tracking branch of origin/<branch>. This is
                # the common case when the requested branch exists upstream
                # but was never checked out locally.
                track_result = subprocess.run(
                    git_cmd + ["checkout", "-B", branch, f"origin/{branch}"],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                )
                if track_result.returncode != 0:
                    # Restore the user's prior branch + stash before bailing
                    # so we don't leave them stranded in a weird state.
                    if auto_stash_ref is not None:
                        _restore_stashed_changes(
                            git_cmd,
                            PROJECT_ROOT,
                            auto_stash_ref,
                            prompt_user=False,
                            input_fn=gw_input_fn,
                        )
                    print(f"✗ Branch '{branch}' does not exist locally or on origin.")
                    if track_result.stderr.strip():
                        print(f"  {track_result.stderr.strip().splitlines()[0]}")
                    sys.exit(1)
        else:
            auto_stash_ref = _stash_local_changes_if_needed(git_cmd, PROJECT_ROOT)

        prompt_for_restore = (
            auto_stash_ref is not None
            and not assume_yes
            and (gateway_mode or (sys.stdin.isatty() and sys.stdout.isatty()))
        )

        # Check if there are updates
        result = subprocess.run(
            git_cmd + ["rev-list", f"HEAD..origin/{branch}", "--count"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        commit_count = int(result.stdout.strip())

        if commit_count == 0:
            _invalidate_update_cache()

            # Restore stash and switch back to original branch if we moved
            if auto_stash_ref is not None:
                _restore_stashed_changes(
                    git_cmd,
                    PROJECT_ROOT,
                    auto_stash_ref,
                    prompt_user=prompt_for_restore,
                    input_fn=gw_input_fn,
                )
            if current_branch not in {branch, "HEAD"}:
                subprocess.run(
                    git_cmd + ["checkout", current_branch],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            print("✓ Already up to date!")
            return

        print(f"→ Found {commit_count} new commit(s)")

        print("→ Pulling updates...")
        update_succeeded = False
        # Capture the pre-pull SHA so we can auto-roll-back if the new code
        # has a syntax error in a critical-path file (PR #28452 incident:
        # orphan merge-conflict markers in marlow_cli/config.py bricked
        # every user who ran ``marlow update`` for the 7 minutes between
        # the bad commit and the fix landing).
        pre_pull_sha = _capture_head_sha(git_cmd, PROJECT_ROOT)
        try:
            pull_result = subprocess.run(
                git_cmd + ["pull", "--ff-only", "origin", branch],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
            )
            if pull_result.returncode != 0:
                # ff-only failed — local and remote have diverged (e.g. upstream
                # force-pushed or rebase).  Since local changes are already
                # stashed, reset to match the remote exactly.
                print(
                    "  ⚠ Fast-forward not possible (history diverged), resetting to match remote..."
                )
                reset_result = subprocess.run(
                    git_cmd + ["reset", "--hard", f"origin/{branch}"],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                )
                if reset_result.returncode != 0:
                    print(f"✗ Failed to reset to origin/{branch}.")
                    if reset_result.stderr.strip():
                        print(f"  {reset_result.stderr.strip()}")
                    print(
                        f"  Try manually: git fetch origin && git reset --hard origin/{branch}"
                    )
                    sys.exit(1)

            # Post-pull syntax guard: validate critical-path files actually
            # parse before declaring the update successful. If a bad commit
            # made it through CI (e.g. admin-merge bypass of a failing
            # ruff check), this catches it on the user side and rolls back
            # so the CLI stays bootable. The user can then retry ``marlow
            # update`` later once a fix lands upstream.
            syntax_ok, failing_path, syntax_error = _validate_critical_files_syntax(
                PROJECT_ROOT
            )
            if not syntax_ok:
                print()
                print("✗ Pulled code has a syntax error in a critical file:")
                print(f"  {failing_path}")
                if syntax_error:
                    # py_compile errors can be multi-line; show the first
                    # ~6 lines so the user sees the actual SyntaxError text.
                    for line in str(syntax_error).splitlines()[:6]:
                        print(f"    {line}")
                if pre_pull_sha:
                    print()
                    print(f"→ Rolling back to {pre_pull_sha[:10]}...")
                    rollback_result = subprocess.run(
                        git_cmd + ["reset", "--hard", pre_pull_sha],
                        cwd=PROJECT_ROOT,
                        capture_output=True,
                        text=True,
                    )
                    if rollback_result.returncode == 0:
                        print("  ✓ Rollback complete — your install is unchanged.")
                        print("  Try ``marlow update`` again later once a fix lands.")
                    else:
                        print("  ✗ Rollback failed. Recover manually with:")
                        print(f"    cd {PROJECT_ROOT} && git reset --hard {pre_pull_sha}")
                        if rollback_result.stderr.strip():
                            print(f"    ({rollback_result.stderr.strip().splitlines()[0]})")
                else:
                    print()
                    print("  Could not capture pre-pull SHA — recover manually with:")
                    print(f"    cd {PROJECT_ROOT} && git reflog && git reset --hard <prev-sha>")
                sys.exit(1)

            update_succeeded = True
        finally:
            if auto_stash_ref is not None:
                # Don't attempt stash restore if the code update itself failed —
                # working tree is in an unknown state.
                if not update_succeeded:
                    print(
                        f"  ℹ️  Local changes preserved in stash (ref: {auto_stash_ref})"
                    )
                    print(f"  Restore manually with: git stash apply")
                else:
                    _restore_stashed_changes(
                        git_cmd,
                        PROJECT_ROOT,
                        auto_stash_ref,
                        prompt_user=prompt_for_restore,
                        input_fn=gw_input_fn,
                    )

        _invalidate_update_cache()

        # Clear stale .pyc bytecode cache — prevents ImportError on gateway
        # restart when updated source references names that didn't exist in
        # the old bytecode (e.g. get_marlow_home added to marlow_constants).
        removed = _clear_bytecode_cache(PROJECT_ROOT)
        if removed:
            print(
                f"  ✓ Cleared {removed} stale __pycache__ director{'y' if removed == 1 else 'ies'}"
            )

        # Reinstall Python dependencies. Prefer .[all], but if one optional extra
        # breaks on this machine, keep base deps and reinstall the remaining extras
        # individually so update does not silently strip working capabilities.
        print("→ Updating Python dependencies...")
        from marlow_cli.managed_uv import ensure_uv, rebuild_venv, update_managed_uv

        # Keep managed uv current — runs `uv self update` if we already have one.
        update_managed_uv()

        uv_bin, fresh_bootstrap = ensure_uv()
        # First-time managed uv install on an existing checkout: the old venv
        # may point to a Python without FTS5.  Rebuild it so the new managed
        # uv provides a fresh interpreter with FTS5 guaranteed.
        if fresh_bootstrap and uv_bin:
            rebuild_venv(uv_bin, PROJECT_ROOT / "venv")

        pip_cmd = [sys.executable, "-m", "pip"]
        install_group = "all"

        if uv_bin:
            uv_env = {**os.environ, "VIRTUAL_ENV": str(PROJECT_ROOT / "venv")}
            _install_python_dependencies_with_optional_fallback(
                [uv_bin, "pip"], env=uv_env, group=install_group
            )
        else:
            # Use sys.executable to explicitly call the venv's pip module,
            # avoiding PEP 668 'externally-managed-environment' errors on Debian/Ubuntu.
            # Some environments lose pip inside the venv; bootstrap it back with
            # ensurepip before trying the editable install.
            pip_cmd = [sys.executable, "-m", "pip"]
            try:
                subprocess.run(
                    pip_cmd + ["--version"],
                    cwd=PROJECT_ROOT,
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                subprocess.run(
                    [sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
                    cwd=PROJECT_ROOT,
                    check=True,
                )
            _install_python_dependencies_with_optional_fallback(pip_cmd, group=install_group)

        _refresh_active_lazy_features()

        _update_node_dependencies()

        print()
        print("✓ Code updated!")

        # After git pull, source files on disk are newer than cached Python
        # modules in this process.  Reload marlow_constants so that any lazy
        # import executed below (gateway restart) sees new
        # attributes like display_marlow_home() added since the last release.
        try:
            import importlib
            import marlow_constants as _hc

            importlib.reload(_hc)
        except Exception:
            pass  # non-fatal — worst case a lazy import fails gracefully

        # Sync Honcho host blocks to all profiles
        try:
            from plugins.memory.honcho.cli import sync_honcho_profiles_quiet

            synced = sync_honcho_profiles_quiet()
            if synced:
                print(f"\n-> Honcho: synced {synced} profile(s)")
        except Exception:
            pass  # honcho plugin not installed or not configured

        # Check for config migrations
        print()
        print("→ Checking configuration for new options...")

        from marlow_cli.config import (
            get_missing_env_vars,
            get_missing_config_fields,
            check_config_version,
            migrate_config,
        )

        missing_env = get_missing_env_vars(required_only=True)
        missing_config = get_missing_config_fields()
        current_ver, latest_ver = check_config_version()

        has_new_options = bool(missing_env or missing_config)
        version_bump_only = (
            not has_new_options and current_ver < latest_ver
        )
        needs_migration = has_new_options or current_ver < latest_ver

        if version_bump_only:
            # Nothing for the user to fill in — only the config format version
            # changed (new defaults already merge in transparently). Asking
            # "configure new options now?" here is misleading: saying yes just
            # bumps the version and looks like a no-op (issue: ScottFive /
            # Tt2021). Apply it silently and say what actually happened.
            print()
            print(
                f"  ℹ Updating config format (v{current_ver} → v{latest_ver})…"
            )
            try:
                migrate_config(interactive=False, quiet=True)
                print("  ✓ Config format updated (no new settings to configure)")
            except Exception as _mig_err:
                print(f"  ⚠️  Config format update failed: {_mig_err}")
                print("     Run 'marlow config migrate' to retry.")
        elif needs_migration:
            print()
            # Show WHAT changed, not just a count, so the user can make an
            # informed yes/no decision (previously the prompt named nothing).
            def _print_items(items, label, key, fallback_key=None):
                if not items:
                    return
                print(f"  {label}:")
                shown = items[:8]
                for it in shown:
                    if isinstance(it, dict):
                        name = it.get(key) or (fallback_key and it.get(fallback_key)) or "?"
                        desc = (it.get("description") or "").strip()
                    else:
                        # Defensive: some callers/mocks pass bare name strings.
                        name = str(it)
                        desc = ""
                    if desc:
                        print(f"      • {name} — {desc}")
                    else:
                        print(f"      • {name}")
                extra = len(items) - len(shown)
                if extra > 0:
                    print(f"      … and {extra} more")

            if missing_env:
                print(
                    f"  ⚠️  {len(missing_env)} new required setting(s) need configuration"
                )
                _print_items(missing_env, "New settings", "name")
            if missing_config:
                print(f"  ℹ️  {len(missing_config)} new config option(s) available")
                _print_items(missing_config, "New options", "key")

            print()
            if assume_yes:
                print(
                    "  ℹ --yes: auto-applying config migration (skipping API-key prompts)."
                )
                response = "y"
            elif gateway_mode:
                response = (
                    _gateway_prompt(
                        "Would you like to configure new options now? [Y/n]", "n"
                    )
                    .strip()
                    .lower()
                )
            elif not (sys.stdin.isatty() and sys.stdout.isatty()):
                print("  ℹ Non-interactive session — applying safe config migrations.")
                response = "auto"
            else:
                try:
                    response = (
                        input("Would you like to configure them now? [Y/n]: ")
                        .strip()
                        .lower()
                    )
                except EOFError:
                    response = "n"

            if response in {"", "y", "yes", "auto"}:
                print()
                # Gateway mode, --yes, and non-interactive update contexts
                # cannot prompt for API keys.
                # Still run the non-interactive migration pass before restarting
                # so new default config fields and version bumps are written
                # before the freshly updated gateway validates config at startup.
                interactive_migration = not (
                    gateway_mode or assume_yes or response == "auto"
                )
                results = migrate_config(interactive=interactive_migration, quiet=False)

                if results["env_added"] or results["config_added"]:
                    print()
                    print("✓ Configuration updated!")
                if (gateway_mode or assume_yes or response == "auto") and missing_env:
                    print("  ℹ API keys require manual entry: marlow config migrate")
            else:
                print()
                print("Skipped. Run 'marlow config migrate' later to configure.")
        else:
            print("  ✓ Configuration is up to date")

        print()
        print("✓ Update complete!")

        # Repair RHEL-family root installs where /usr/local/bin isn't on PATH
        # for non-login interactive shells.  No-op on every other platform.
        try:
            _ensure_fhs_path_guard()
        except Exception as e:
            logger.debug("FHS PATH guard check failed: %s", e)

        # Write exit code *before* the gateway restart attempt.
        # When running as ``marlow update --gateway`` (spawned by the gateway's
        # /update command), this process lives inside the gateway's systemd
        # cgroup.  A graceful SIGUSR1 restart keeps the drain loop alive long
        # enough for the exit-code marker to be written below, but the
        # fallback ``systemctl restart`` path (see below) kills everything in
        # the cgroup (KillMode=mixed → SIGKILL to remaining processes),
        # including us and the wrapping bash shell.  The shell never reaches
        # its ``printf $status > .update_exit_code`` epilogue, so the
        # exit-code marker file would never be created.  The new gateway's
        # update watcher would then poll for 30 minutes and send a spurious
        # timeout message.
        #
        # Writing the marker here — after git pull + pip install succeed but
        # before we attempt the restart — ensures the new gateway sees it
        # regardless of how we die.
        if gateway_mode:
            _exit_code_path = get_marlow_home() / ".update_exit_code"
            try:
                _exit_code_path.write_text("0")
            except OSError:
                pass

        # Auto-restart ALL gateways after update.
        # The code update (git pull) is shared across all profiles, so every
        # running gateway needs restarting to pick up the new code.
        try:
            from marlow_cli.gateway import (
                is_macos,
                supports_systemd_services,
                _ensure_user_systemd_env,
                find_gateway_pids,
                find_profile_gateway_processes,
                launch_detached_profile_gateway_restart,
                _get_service_pids,
                _graceful_restart_via_sigusr1,
                _wait_for_gateway_exit,
            )
            import signal as _signal

            def _wait_for_service_active(
                scope_cmd_: list,
                svc_name_: str,
                timeout: float = 10.0,
            ) -> bool:
                """Poll ``systemctl is-active`` until the unit reports active.

                systemd's Stopped -> Started transition after a graceful exit
                (or a hard restart) is not instantaneous; a one-shot check
                races that window and falsely reports the unit as down.
                Poll every 0.5s up to ``timeout`` seconds before giving up.
                """
                deadline = _time.monotonic() + max(timeout, 0.5)
                while True:
                    try:
                        _verify = subprocess.run(
                            scope_cmd_ + ["is-active", svc_name_],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        if _verify.stdout.strip() == "active":
                            return True
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        pass
                    if _time.monotonic() >= deadline:
                        return False
                    _time.sleep(0.5)

            def _service_restart_sec(
                scope_cmd_: list,
                svc_name_: str,
                default: float = 0.0,
            ) -> float:
                """Read the unit's ``RestartUSec`` (RestartSec) in seconds.

                After a graceful exit-75, systemd waits ``RestartSec`` before
                respawning the unit.  Callers that poll for ``is-active``
                must use a timeout >= ``RestartSec`` + transition slack, or
                they'll give up *during* the cooldown window and wrongly
                conclude the unit didn't relaunch.
                """
                try:
                    _show = subprocess.run(
                        scope_cmd_
                        + [
                            "show",
                            svc_name_,
                            "--property=RestartUSec",
                            "--value",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    return default
                raw = (_show.stdout or "").strip()
                # systemd emits values like "30s", "100ms", "1min 30s", or
                # "infinity".  Parse conservatively; on any miss return default.
                if not raw or raw == "infinity":
                    return default
                total = 0.0
                matched = False
                for part in raw.split():
                    for _suf, _mult in (
                        ("ms", 0.001),
                        ("us", 0.000001),
                        ("min", 60.0),
                        ("s", 1.0),
                    ):
                        if part.endswith(_suf):
                            try:
                                total += float(part[: -len(_suf)]) * _mult
                                matched = True
                            except ValueError:
                                pass
                            break
                return total if matched else default

            # Drain budget for graceful SIGUSR1 restarts.  The gateway drains
            # for up to ``agent.restart_drain_timeout`` (default 60s) before
            # exiting with code 75; we wait slightly longer so the drain
            # completes before we fall back to a hard restart.  On older
            # systemd units without SIGUSR1 wiring this wait just times out
            # and we fall back to ``systemctl restart`` (the old behaviour).
            try:
                from marlow_constants import (
                    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT as _DEFAULT_DRAIN,
                )
            except Exception:
                _DEFAULT_DRAIN = 60.0
            _cfg_drain = None
            try:
                from marlow_cli.config import load_config

                _cfg_agent = load_config().get("agent") or {}
                _cfg_drain = _cfg_agent.get("restart_drain_timeout")
            except Exception:
                pass
            try:
                _drain_budget = (
                    float(_cfg_drain)
                    if _cfg_drain is not None
                    else float(_DEFAULT_DRAIN)
                )
            except (TypeError, ValueError):
                _drain_budget = float(_DEFAULT_DRAIN)
            # Add a 15s margin so the drain loop + final exit finish before
            # we escalate to ``systemctl restart`` / SIGTERM.
            _drain_budget = max(_drain_budget, 30.0) + 15.0

            restarted_services = []
            killed_pids = set()
            relaunched_profiles = []

            # --- Systemd services (Linux) ---
            # Discover all marlow-gateway* units (default + profiles)
            if supports_systemd_services():
                try:
                    _ensure_user_systemd_env()
                except Exception:
                    pass

                for scope, scope_cmd in [
                    ("user", ["systemctl", "--user"]),
                    ("system", ["systemctl"]),
                ]:
                    try:
                        result = subprocess.run(
                            scope_cmd
                            + [
                                "list-units",
                                "marlow-gateway*",
                                "--plain",
                                "--no-legend",
                                "--no-pager",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        for line in result.stdout.strip().splitlines():
                            parts = line.split()
                            if not parts:
                                continue
                            unit = parts[
                                0
                            ]  # e.g. marlow-gateway.service or marlow-gateway-coder.service
                            if not unit.endswith(".service"):
                                continue
                            svc_name = unit.removesuffix(".service")
                            # Check if active
                            check = subprocess.run(
                                scope_cmd + ["is-active", svc_name],
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            if check.stdout.strip() != "active":
                                continue

                            # Prefer a graceful SIGUSR1 restart so in-flight
                            # agent runs drain instead of being SIGKILLed.
                            # The gateway's SIGUSR1 handler calls
                            # request_restart(via_service=True) → drain →
                            # exit; systemd's Restart=always respawns the unit.
                            _main_pid = 0
                            try:
                                _show = subprocess.run(
                                    scope_cmd
                                    + [
                                        "show",
                                        svc_name,
                                        "--property=MainPID",
                                        "--value",
                                    ],
                                    capture_output=True,
                                    text=True,
                                    timeout=5,
                                )
                                _main_pid = int((_show.stdout or "").strip() or 0)
                            except (
                                ValueError,
                                subprocess.TimeoutExpired,
                                FileNotFoundError,
                            ):
                                _main_pid = 0

                            _graceful_ok = False
                            if _main_pid > 0:
                                print(
                                    f"  → {svc_name}: draining (up to {int(_drain_budget)}s)..."
                                )
                                _graceful_ok = _graceful_restart_via_sigusr1(
                                    _main_pid,
                                    drain_timeout=_drain_budget,
                                )

                            if _graceful_ok:
                                # Gateway exited after a planned restart.
                                # ``Restart=always`` means systemd WILL respawn
                                # the unit — but only after
                                # ``RestartSec`` (default 60s on our unit
                                # file). That 60s wait is a crash-loop guard,
                                # and is the right default when the gateway
                                # dies unexpectedly. For a voluntary restart
                                # on update, it's dead time the user watches.
                                #
                                # Shortcut it: ``reset-failed`` + ``start``
                                # skips RestartSec entirely (we're manually
                                # initiating the unit, not waiting for
                                # systemd's auto-restart logic). Takes about
                                # as long as the process takes to come up
                                # (~1-3s on a warm box).
                                #
                                # If the unit is already active because
                                # RestartSec elapsed while we were draining,
                                # ``start`` is a no-op and we fall through to
                                # the poll below. Either way we collapse the
                                # 60s+ delay to a ~5s one.
                                subprocess.run(
                                    scope_cmd + ["reset-failed", svc_name],
                                    capture_output=True,
                                    text=True,
                                    timeout=10,
                                )
                                subprocess.run(
                                    scope_cmd + ["start", svc_name],
                                    capture_output=True,
                                    text=True,
                                    timeout=15,
                                )
                                # Short poll: the gateway should be up within
                                # a few seconds now that we bypassed
                                # RestartSec. Fall back to the longer
                                # RestartSec + slack budget ONLY if the
                                # explicit start failed and we need to rely
                                # on systemd's auto-restart.
                                if _wait_for_service_active(
                                    scope_cmd,
                                    svc_name,
                                    timeout=10.0,
                                ):
                                    restarted_services.append(svc_name)
                                    continue
                                # Explicit start didn't take. Fall back to
                                # the original passive poll (systemd's
                                # auto-restart WILL fire after RestartSec
                                # regardless).
                                _restart_sec = _service_restart_sec(
                                    scope_cmd,
                                    svc_name,
                                    default=0.0,
                                )
                                _post_drain_timeout = max(
                                    10.0,
                                    _restart_sec + 10.0,
                                )
                                if _wait_for_service_active(
                                    scope_cmd,
                                    svc_name,
                                    timeout=_post_drain_timeout,
                                ):
                                    restarted_services.append(svc_name)
                                    continue
                                # Process exited but wasn't respawned (older
                                # unit without Restart=on-failure or
                                # RestartForceExitStatus=75).  Fall through
                                # to systemctl start/restart.
                                print(
                                    f"  ⚠ {svc_name} drained but didn't relaunch — forcing restart"
                                )

                            # Fallback: blunt systemctl restart.  This is
                            # what the old code always did; we get here only
                            # when the graceful path failed (unit missing
                            # SIGUSR1 wiring, drain exceeded the budget,
                            # restart-policy mismatch).
                            #
                            # Always `reset-failed` first.  If systemd's own
                            # auto-restart attempts already parked the unit
                            # in a failed state (transient CHDIR / OOM /
                            # filesystem race after our drain + exit-75),
                            # a plain `systemctl restart` can wedge against
                            # the RestartSec backoff and leave the unit
                            # dead.  Clearing the failed state first makes
                            # the restart idempotent.  Mirrors the recovery
                            # path in `marlow gateway restart`
                            # (`systemd_restart()`) as of PR #20949.
                            subprocess.run(
                                scope_cmd + ["reset-failed", svc_name],
                                capture_output=True,
                                text=True,
                                timeout=10,
                            )
                            restart = subprocess.run(
                                scope_cmd + ["restart", svc_name],
                                capture_output=True,
                                text=True,
                                timeout=15,
                            )
                            if restart.returncode == 0:
                                # Verify the service actually survived the
                                # restart.  systemctl restart returns 0 even
                                # if the new process crashes immediately.
                                if _wait_for_service_active(
                                    scope_cmd,
                                    svc_name,
                                    timeout=10.0,
                                ):
                                    restarted_services.append(svc_name)
                                else:
                                    # Retry once — transient startup failures
                                    # (stale module cache, import race) often
                                    # resolve on the second attempt.  Again
                                    # clear any failed state first so the
                                    # retry isn't blocked by the previous
                                    # crash.
                                    print(
                                        f"  ⚠ {svc_name} died after restart, retrying..."
                                    )
                                    subprocess.run(
                                        scope_cmd + ["reset-failed", svc_name],
                                        capture_output=True,
                                        text=True,
                                        timeout=10,
                                    )
                                    subprocess.run(
                                        scope_cmd + ["restart", svc_name],
                                        capture_output=True,
                                        text=True,
                                        timeout=15,
                                    )
                                    if _wait_for_service_active(
                                        scope_cmd,
                                        svc_name,
                                        timeout=10.0,
                                    ):
                                        restarted_services.append(svc_name)
                                        print(f"  ✓ {svc_name} recovered on retry")
                                    else:
                                        _scope_flag = "--user " if scope == "user" else ""
                                        print(
                                            f"  ✗ {svc_name} failed to stay running after restart.\n"
                                            f"    Check logs: journalctl {_scope_flag}-u {svc_name} --since '2 min ago'\n"
                                            f"    Recover manually:\n"
                                            f"      systemctl {_scope_flag}reset-failed {svc_name}\n"
                                            f"      systemctl {_scope_flag}restart {svc_name}"
                                        )
                            else:
                                print(
                                    f"  ⚠ Failed to restart {svc_name}: {restart.stderr.strip()}"
                                )
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        pass

            # --- Launchd services (macOS) ---
            if is_macos():
                try:
                    from marlow_cli.gateway import (
                        launchd_restart,
                        get_launchd_label,
                        get_launchd_plist_path,
                    )

                    plist_path = get_launchd_plist_path()
                    if plist_path.exists():
                        check = subprocess.run(
                            ["launchctl", "list", get_launchd_label()],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        if check.returncode == 0:
                            try:
                                launchd_restart()
                                restarted_services.append(get_launchd_label())
                            except subprocess.CalledProcessError as e:
                                stderr = (getattr(e, "stderr", "") or "").strip()
                                print(f"  ⚠ Gateway restart failed: {stderr}")
                except (FileNotFoundError, subprocess.TimeoutExpired, ImportError):
                    pass

            # --- Manual (non-service) gateways ---
            # Kill any remaining gateway processes not managed by a service.
            # Exclude PIDs that belong to just-restarted services so we don't
            # immediately kill the process that systemd/launchd just spawned.
            service_pids = _get_service_pids()
            manual_pids = find_gateway_pids(
                exclude_pids=service_pids, all_profiles=True
            )
            profile_processes = {
                proc.pid: proc
                for proc in find_profile_gateway_processes(exclude_pids=service_pids)
                if proc.pid in manual_pids
            }
            for pid, proc in profile_processes.items():
                if not launch_detached_profile_gateway_restart(proc.profile, pid):
                    continue
                # Prefer a graceful SIGUSR1 drain so in-flight agent runs
                # finish before the watcher respawns the gateway.  If the
                # gateway doesn't support SIGUSR1 or doesn't exit within
                # the drain budget, fall back to SIGTERM — the watcher
                # still sees the exit and relaunches either way.
                drained = _graceful_restart_via_sigusr1(
                    pid,
                    drain_timeout=_drain_budget,
                )
                if not drained:
                    try:
                        os.kill(pid, _signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
                # Wait for the old process to fully exit before the watcher
                # spawns the new gateway.  Telegram holds the previous
                # getUpdates long-poll session open on its servers for up to
                # ~30s after the client disconnects.  If the new gateway
                # connects before that window expires it receives a 409
                # Conflict, which _handle_polling_conflict() recovers from
                # via back-off retries — but a brief wait here reduces the
                # chance of hitting that path at all, especially on fast
                # machines where the watcher loop restarts in < 1s.
                # We wait up to 5s for the process to exit (the OS-level
                # close, not the Telegram server-side expiry), then let the
                # watcher take over.  The Telegram adapter's retry logic
                # handles any remaining 409s if the server session is still
                # live when the new gateway polls.
                _wait_for_gateway_exit(timeout=5.0, force_after=None)
                killed_pids.add(pid)
                relaunched_profiles.append(proc.profile)

            for pid in manual_pids:
                if pid in profile_processes:
                    continue
                try:
                    os.kill(pid, _signal.SIGTERM)
                    killed_pids.add(pid)
                except (ProcessLookupError, PermissionError):
                    pass

            if restarted_services or killed_pids:
                print()
                for svc in restarted_services:
                    print(f"  ✓ Restarted {svc}")
                if relaunched_profiles:
                    names = ", ".join(relaunched_profiles)
                    print(f"  ✓ Restarting manual gateway profile(s): {names}")
                unmapped_count = len(killed_pids) - len(relaunched_profiles)
                if unmapped_count:
                    print(f"  → Stopped {unmapped_count} manual gateway process(es)")
                    print("    Restart manually: marlow gateway run")
                    if unmapped_count > 1:
                        print(
                            "    (or: marlow -p <profile> gateway run  for each profile)"
                        )

            if not restarted_services and not killed_pids:
                # No gateways were running — nothing to do
                pass

            # --- Post-restart survivor sweep -----------------------------
            # Issue #17648: some gateways ignore SIGTERM (stuck drain,
            # blocked I/O, PID dead but zombie).  The detached profile
            # watchers wait 120s for the old PID to exit — if it never
            # does, no respawn happens and the user keeps hitting
            # ImportError against a stale sys.modules.  Give the
            # graceful paths a brief window to complete, then SIGKILL
            # any remaining pre-update PIDs so the watcher / service
            # manager can relaunch with fresh code.
            try:
                _time.sleep(3.0)
                _service_pids_after = _get_service_pids()
                _surviving = find_gateway_pids(
                    exclude_pids=_service_pids_after,
                    all_profiles=True,
                )
                # Scope to PIDs we already tried to kill during this
                # update (killed_pids).  Anything new is a gateway that
                # started AFTER our restart attempt — respecting user
                # intent, we don't kill those.
                _stuck = [pid for pid in _surviving if pid in killed_pids]
                if _stuck:
                    print()
                    print(
                        f"  ⚠ {len(_stuck)} gateway process(es) ignored SIGTERM — force-killing"
                    )
                    from gateway.status import terminate_pid as _terminate_pid
                    for pid in _stuck:
                        try:
                            _terminate_pid(pid, force=True)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                    # Give the OS a beat to reap the processes so the
                    # watchers see them exit and respawn.
                    _time.sleep(1.5)
            except Exception as _sweep_exc:
                logger.debug("Post-restart survivor sweep failed: %s", _sweep_exc)

        except Exception as e:
            logger.debug("Gateway restart during update failed: %s", e)

        # Warn if legacy Marlow gateway unit files are still installed.
        # When both marlow.service (from a pre-rename install) and the
        # current marlow-gateway.service are enabled, they SIGTERM-fight
        # for the same bot token (see PR #11909). Flagging here means
        # every `marlow update` surfaces the issue until the user migrates.
        try:
            from marlow_cli.gateway import (
                has_legacy_marlow_units,
                _find_legacy_marlow_units,
                supports_systemd_services,
            )

            if supports_systemd_services() and has_legacy_marlow_units():
                print()
                print("⚠ Legacy Marlow gateway unit(s) detected:")
                for name, path, is_sys in _find_legacy_marlow_units():
                    scope = "system" if is_sys else "user"
                    print(f"    {path}  ({scope} scope)")
                print()
                print("  These pre-rename units (marlow.service) fight the current")
                print("  marlow-gateway.service for the bot token and cause SIGTERM")
                print("  flap loops. Remove them with:")
                print()
                print("    marlow gateway migrate-legacy")
                print()
                print("  (add `sudo` if any are in system scope)")
        except Exception as e:
            logger.debug("Legacy unit check during update failed: %s", e)

        print()
        print("Tip: You can now select a provider and model:")
        print("  marlow model              # Select provider and model")

    except subprocess.CalledProcessError as e:
        print(f"✗ Update failed: {e}")
        sys.exit(1)


def _coalesce_session_name_args(argv: list) -> list:
    """Join unquoted multi-word session names after -c/--continue and -r/--resume.

    When a user types ``marlow -c Pokemon Agent Dev`` without quoting the
    session name, argparse sees three separate tokens.  This function merges
    them into a single argument so argparse receives
    ``['-c', 'Pokemon Agent Dev']`` instead.

    Tokens are collected after the flag until we hit another flag (``-*``)
    or a known top-level subcommand.
    """
    _SUBCOMMANDS = {
        "chat",
        "model",
        "gateway",
        "setup",
        "login",
        "logout",
        "status",
        "cron",
        "doctor",
        "config",
        "pairing",
        "skills",
        "tools",
        "mcp",
        "sessions",
        "insights",
        "version",
        "update",
        "uninstall",
        "profile",
        "honcho",
        "plugins",
        "security",
        "webhook",
        "memory",
        "experience",
        "dump",
        "debug",
        "completion",
        "logs",
    }
    _SESSION_FLAGS = {"-c", "--continue", "-r", "--resume"}

    result = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _SESSION_FLAGS:
            result.append(token)
            i += 1
            # Collect subsequent non-flag, non-subcommand tokens as one name
            parts: list = []
            while (
                i < len(argv)
                and not argv[i].startswith("-")
                and argv[i] not in _SUBCOMMANDS
            ):
                parts.append(argv[i])
                i += 1
            if parts:
                result.append(" ".join(parts))
        else:
            result.append(token)
            i += 1
    return result


def cmd_profile(args):
    """Profile management — create, delete, list, switch, alias."""
    from marlow_cli.profiles import (
        list_profiles,
        create_profile,
        delete_profile,
        set_active_profile,
        get_active_profile_name,
        check_alias_collision,
        create_wrapper_script,
        remove_wrapper_script,
        _is_wrapper_dir_in_path,
        _get_wrapper_dir,
    )
    from marlow_constants import display_marlow_home

    action = getattr(args, "profile_action", None)

    if action is None:
        # Bare `marlow profile` — show current profile status
        profile_name = get_active_profile_name()
        dhh = display_marlow_home()
        print(f"\nActive profile: {profile_name}")
        print(f"Path:           {dhh}")

        profiles = list_profiles()
        for p in profiles:
            if p.name == profile_name or (profile_name == "default" and p.is_default):
                if p.model:
                    print(
                        f"Model:          {p.model}"
                        + (f" ({p.provider})" if p.provider else "")
                    )
                print(
                    f"Gateway:        {'running' if p.gateway_running else 'stopped'}"
                )
                print(f"Skills:         {p.skill_count} installed")
                if p.alias_path:
                    print(f"Alias:          {p.name} → marlow -p {p.name}")
                break
        print()
        return

    if action == "list":
        profiles = list_profiles()
        active = get_active_profile_name()

        if not profiles:
            print("No profiles found.")
            return

        # Header
        print(
            f"\n {'Profile':<16} {'Model':<28} {'Gateway':<12} "
            f"{'Alias':<12} {'Distribution'}"
        )
        print(
            f" {'─' * 15}    {'─' * 27}    {'─' * 11}    "
            f"{'─' * 11}    {'─' * 20}"
        )

        for p in profiles:
            marker = (
                " ◆"
                if (p.name == active or (active == "default" and p.is_default))
                else "  "
            )
            name = p.name
            model = (p.model or "—")[:26]
            gw = "running" if p.gateway_running else "stopped"
            alias = p.name if p.alias_path else "—"
            if p.is_default:
                alias = "—"
            if p.distribution_name:
                dist = f"{p.distribution_name}@{p.distribution_version or '?'}"
                dist = dist[:30]
            else:
                dist = "—"
            print(f"{marker}{name:<15} {model:<28} {gw:<12} {alias:<12} {dist}")
        print()

    elif action == "use":
        name = args.profile_name
        try:
            set_active_profile(name)
            if name == "default":
                print(f"Switched to: default (~/.marlow)")
            else:
                print(f"Switched to: {name}")
        except (ValueError, FileNotFoundError) as e:
            print(f"Error: {e}")
            sys.exit(1)

    elif action == "create":
        name = args.profile_name
        clone = getattr(args, "clone", False)
        clone_all = getattr(args, "clone_all", False)
        no_alias = getattr(args, "no_alias", False)
        no_skills = getattr(args, "no_skills", False)

        try:
            clone_from = getattr(args, "clone_from", None)

            profile_dir = create_profile(
                name=name,
                clone_from=clone_from,
                clone_all=clone_all,
                clone_config=clone,
                no_alias=no_alias,
                no_skills=no_skills,
                description=getattr(args, "description", None),
            )
            print(f"\nProfile '{name}' created at {profile_dir}")

            if clone or clone_all:
                source_label = (
                    getattr(args, "clone_from", None) or get_active_profile_name()
                )
                if clone_all:
                    print(f"Full copy from {source_label}.")
                else:
                    print(
                        f"Cloned config, .env, SOUL.md, and skills from {source_label}."
                    )

            # Auto-clone Honcho config for the new profile (only with --clone/--clone-all)
            if clone or clone_all:
                try:
                    from plugins.memory.honcho.cli import clone_honcho_for_profile

                    if clone_honcho_for_profile(name):
                        print(f"Honcho config cloned (peer: {name})")
                except Exception:
                    pass  # Honcho plugin not installed or not configured

            if no_skills:
                print("Bundled on-demand skills disabled (--no-skills).")

            # Create wrapper alias
            if not no_alias:
                collision = check_alias_collision(name)
                if collision:
                    print(f"\n⚠ Cannot create alias '{name}' — {collision}")
                    print(
                        f"  Choose a custom alias:  marlow profile alias {name} --name <custom>"
                    )
                    print(f"  Or access via flag:     marlow -p {name} chat")
                else:
                    wrapper_path = create_wrapper_script(name)
                    if wrapper_path:
                        print(f"Wrapper created: {wrapper_path}")
                        if not _is_wrapper_dir_in_path():
                            print(f"\n⚠ {_get_wrapper_dir()} is not in your PATH.")
                            print(
                                f"  Add to your shell config (~/.bashrc or ~/.zshrc):"
                            )
                            print(f'    export PATH="$HOME/.local/bin:$PATH"')

            # Profile dir for display
            try:
                profile_dir_display = "~/" + str(profile_dir.relative_to(Path.home()))
            except ValueError:
                profile_dir_display = str(profile_dir)

            # Next steps
            print(f"\nNext steps:")
            print(f"  {name} setup              Configure API keys and model")
            print(f"  {name} chat               Start chatting")
            print(f"  {name} gateway start      Start the messaging gateway")
            if clone or clone_all:
                print(f"\n  Edit {profile_dir_display}/.env for different API keys")
                print(f"  Set agent.system_prompt in {profile_dir_display}/config.yaml to customize instructions")
            else:
                print(
                    f"\n  ⚠ This profile has no API keys yet. Run '{name} setup' first,"
                )
                print(f"    or it will inherit keys from your shell environment.")
                print(f"  Set agent.system_prompt in {profile_dir_display}/config.yaml to customize instructions")
            print()

        except (ValueError, FileExistsError, FileNotFoundError) as e:
            print(f"Error: {e}")
            sys.exit(1)

    elif action == "delete":
        name = args.profile_name
        yes = getattr(args, "yes", False)
        try:
            delete_profile(name, yes=yes)
        except (ValueError, FileNotFoundError) as e:
            print(f"Error: {e}")
            sys.exit(1)

    elif action == "describe":
        # Read or write a profile's description. The description is
        # used by profile-aware workflows.
        from marlow_cli import profiles as _profiles_mod

        all_flag = bool(getattr(args, "all_missing", False))
        auto_flag = bool(getattr(args, "auto", False))
        overwrite_flag = bool(getattr(args, "overwrite", False))
        text_value = getattr(args, "text", None)
        name = getattr(args, "profile_name", None)

        if all_flag and not auto_flag:
            print("profile describe: --all requires --auto", file=sys.stderr)
            sys.exit(2)
        if all_flag and (text_value or name):
            print(
                "profile describe: --all is mutually exclusive with a profile name / --text",
                file=sys.stderr,
            )
            sys.exit(2)
        if not all_flag and not name:
            print("profile describe: profile name is required (or --all --auto)", file=sys.stderr)
            sys.exit(2)
        if text_value and auto_flag:
            print(
                "profile describe: --text is mutually exclusive with --auto",
                file=sys.stderr,
            )
            sys.exit(2)

        # Show current description if no operation requested.
        if name and not text_value and not auto_flag:
            try:
                if _profiles_mod.normalize_profile_name(name) == "default":
                    from marlow_constants import get_marlow_home as _hh
                    profile_dir = Path(_hh())
                else:
                    profile_dir = _profiles_mod.get_profile_dir(name)
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
            if not profile_dir.is_dir():
                print(f"Error: profile '{name}' not found", file=sys.stderr)
                sys.exit(1)
            meta = _profiles_mod.read_profile_meta(profile_dir)
            desc = meta.get("description") or ""
            if not desc:
                print(f"(no description set for '{name}')")
            else:
                tag = "[auto] " if meta.get("description_auto") else ""
                print(f"{tag}{desc}")
            sys.exit(0)

        # --text path: just write the user-authored description.
        if text_value:
            try:
                if _profiles_mod.normalize_profile_name(name) == "default":
                    from marlow_constants import get_marlow_home as _hh
                    profile_dir = Path(_hh())
                else:
                    profile_dir = _profiles_mod.get_profile_dir(name)
                _profiles_mod.write_profile_meta(
                    profile_dir,
                    description=text_value,
                    description_auto=False,
                )
                print(f"Description updated for '{name}'.")
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
            sys.exit(0)

        # --auto path: invoke the LLM describer.
        from marlow_cli import profile_describer as _pd

        if all_flag:
            targets = _pd.list_describable_profiles(missing_only=True)
            if not targets:
                print("All profiles already have descriptions.")
                sys.exit(0)
        else:
            targets = [name]

        ok_count = 0
        fail_count = 0
        for tgt in targets:
            outcome = _pd.describe_profile(tgt, overwrite=overwrite_flag)
            if outcome.ok:
                ok_count += 1
                print(f"Described '{outcome.profile_name}': {outcome.description}")
            else:
                fail_count += 1
                print(
                    f"profile describe {outcome.profile_name}: {outcome.reason}",
                    file=sys.stderr,
                )
        if not all_flag:
            sys.exit(0 if ok_count == 1 else 1)
        sys.exit(0 if ok_count > 0 else 1)

    elif action == "show":
        name = args.profile_name
        from marlow_cli.profiles import (
            get_profile_dir,
            profile_exists,
            _read_config_model,
            _check_gateway_running,
            _count_skills,
            _read_distribution_meta,
        )

        if not profile_exists(name):
            print(f"Error: Profile '{name}' does not exist.")
            sys.exit(1)
        profile_dir = get_profile_dir(name)
        model, provider = _read_config_model(profile_dir)
        gw = _check_gateway_running(profile_dir)
        skills = _count_skills(profile_dir)
        dist_name, dist_version, dist_source = _read_distribution_meta(profile_dir)
        wrapper = _get_wrapper_dir() / name

        print(f"\nProfile: {name}")
        print(f"Path:    {profile_dir}")
        if model:
            print(f"Model:   {model}" + (f" ({provider})" if provider else ""))
        print(f"Gateway: {'running' if gw else 'stopped'}")
        print(f"Skills:  {skills}")
        print(
            f".env:    {'exists' if (profile_dir / '.env').exists() else 'not configured'}"
        )
        print(
            f"SOUL.md: {'exists' if (profile_dir / 'SOUL.md').exists() else 'not configured'}"
        )
        if dist_name:
            print(f"Distribution: {dist_name}@{dist_version or '?'}")
            if dist_source:
                print(f"Installed from: {dist_source}")
            print(f"  (run `marlow profile info {name}` for full manifest)")
        if wrapper.exists():
            print(f"Alias:   {wrapper}")
        print()

    elif action == "alias":
        name = args.profile_name
        remove = getattr(args, "remove", False)
        custom_name = getattr(args, "alias_name", None)

        from marlow_cli.profiles import profile_exists

        if not profile_exists(name):
            print(f"Error: Profile '{name}' does not exist.")
            sys.exit(1)

        alias_name = custom_name or name

        if remove:
            if remove_wrapper_script(alias_name):
                print(f"✓ Removed alias '{alias_name}'")
            else:
                print(f"No alias '{alias_name}' found to remove.")
        else:
            collision = check_alias_collision(alias_name)
            if collision:
                print(f"Error: {collision}")
                sys.exit(1)
            wrapper_path = create_wrapper_script(
                alias_name, target=name if custom_name else None
            )
            if wrapper_path:
                print(f"✓ Alias created: {wrapper_path}")
                if not _is_wrapper_dir_in_path():
                    print(f"⚠ {_get_wrapper_dir()} is not in your PATH.")

    elif action == "rename":
        from marlow_cli.profiles import rename_profile

        try:
            new_dir = rename_profile(args.old_name, args.new_name)
            print(f"\nProfile renamed: {args.old_name} → {args.new_name}")
            print(f"Path: {new_dir}\n")
        except (ValueError, FileExistsError, FileNotFoundError) as e:
            print(f"Error: {e}")
            sys.exit(1)

    elif action == "export":
        from marlow_cli.profiles import export_profile

        name = args.profile_name
        output = args.output or f"{name}.tar.gz"
        try:
            result_path = export_profile(name, output)
            print(f"✓ Exported '{name}' to {result_path}")
        except (ValueError, FileNotFoundError) as e:
            print(f"Error: {e}")
            sys.exit(1)

    elif action == "import":
        from marlow_cli.profiles import import_profile

        try:
            profile_dir = import_profile(
                args.archive, name=getattr(args, "import_name", None)
            )
            name = profile_dir.name
            print(f"✓ Imported profile '{name}' at {profile_dir}")

            # Offer to create alias
            collision = check_alias_collision(name)
            if not collision:
                wrapper_path = create_wrapper_script(name)
                if wrapper_path:
                    print(f"  Wrapper created: {wrapper_path}")
            print()
        except (ValueError, FileExistsError, FileNotFoundError) as e:
            print(f"Error: {e}")
            sys.exit(1)

    elif action == "install":
        import tempfile
        from marlow_cli.profile_distribution import (
            plan_install,
            install_distribution,
            DistributionError,
        )

        try:
            # Preview: stage the distribution into a scratch dir, show the
            # manifest, then do the real install.  The double-stage avoids
            # any side-effects if the user declines.
            with tempfile.TemporaryDirectory(prefix="marlow_dist_preview_") as tmp:
                plan = plan_install(
                    args.source,
                    Path(tmp),
                    override_name=getattr(args, "install_name", None),
                )
                _render_distribution_plan(plan)

                if not getattr(args, "yes", False):
                    try:
                        answer = input("\nProceed with install? [y/N] ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        answer = ""
                    if answer not in {"y", "yes"}:
                        print("Install cancelled.")
                        return

            plan = install_distribution(
                args.source,
                name=getattr(args, "install_name", None),
                force=getattr(args, "force", False),
                create_alias=getattr(args, "alias", False),
            )
            print(f"\n✓ Installed '{plan.manifest.name}' v{plan.manifest.version}")
            print(f"  Profile path: {plan.target_dir}")
            if plan.manifest.env_requires:
                print(
                    f"  Next: copy .env.EXAMPLE to .env and fill in required keys:\n"
                    f"    {plan.target_dir}/.env.EXAMPLE"
                )
            if plan.has_cron:
                print(
                    "  Cron jobs were included but are NOT scheduled automatically.\n"
                    f"  Review them with:  marlow -p {plan.manifest.name} cron list"
                )
            print(f"\n  Use with:      marlow -p {plan.manifest.name} chat")
        except (DistributionError, ValueError) as e:
            print(f"Error: {e}")
            sys.exit(1)

    elif action == "update":
        from marlow_cli.profile_distribution import (
            update_distribution,
            read_manifest,
            DistributionError,
        )
        from marlow_cli.profiles import get_profile_dir, normalize_profile_name

        name = args.profile_name
        try:
            canon = normalize_profile_name(name)
            current = read_manifest(get_profile_dir(canon))
            if current is None:
                print(
                    f"Error: Profile '{canon}' is not a distribution (no distribution.yaml). "
                    "Only profiles installed via `marlow profile install` can be updated."
                )
                sys.exit(1)

            force_config = getattr(args, "force_config", False)
            if not getattr(args, "yes", False):
                print(f"\nUpdate '{canon}' from: {current.source or '(no source)'}")
                print(f"  Currently at version {current.version}")
                if force_config:
                    print("  --force-config set: config.yaml WILL be overwritten.")
                else:
                    print("  config.yaml will be preserved (pass --force-config to overwrite).")
                print("  User data (memories, sessions, auth, .env) will NOT be touched.")
                try:
                    answer = input("\nProceed? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                if answer not in {"y", "yes"}:
                    print("Update cancelled.")
                    return

            plan = update_distribution(canon, force_config=force_config)
            print(f"\n✓ Updated '{plan.manifest.name}' → v{plan.manifest.version}")
            if plan.has_cron:
                print(
                    "  Cron files were refreshed.  Review with:  "
                    f"marlow -p {plan.manifest.name} cron list"
                )
        except (DistributionError, ValueError) as e:
            print(f"Error: {e}")
            sys.exit(1)

    elif action == "info":
        from marlow_cli.profile_distribution import describe_distribution, DistributionError

        try:
            data = describe_distribution(args.profile_name)
        except (DistributionError, ValueError) as e:
            print(f"Error: {e}")
            sys.exit(1)
        if not data:
            print(
                f"Profile '{args.profile_name}' is not a distribution "
                "(no distribution.yaml)."
            )
            return
        print(f"\nDistribution: {data.get('name')}")
        print(f"Version:      {data.get('version', '?')}")
        if data.get("description"):
            print(f"Description:  {data['description']}")
        if data.get("author"):
            print(f"Author:       {data['author']}")
        if data.get("license"):
            print(f"License:      {data['license']}")
        if data.get("marlow_requires"):
            print(f"Requires:     Marlow {data['marlow_requires']}")
        if data.get("source"):
            print(f"Source:       {data['source']}")
        if data.get("installed_at"):
            print(f"Installed:    {data['installed_at']}")
        env_reqs = data.get("env_requires") or []
        if env_reqs:
            print("\nEnvironment variables:")
            for er in env_reqs:
                tag = "required" if er.get("required", True) else "optional"
                line = f"  {er['name']} ({tag})"
                if er.get("description"):
                    line += f" — {er['description']}"
                print(line)
                if er.get("default") is not None:
                    print(f"      default: {er['default']}")
        print()


def _render_distribution_plan(plan) -> None:
    """Print a human-readable summary of a pending distribution install."""
    from marlow_cli.profile_distribution import MANIFEST_FILENAME
    mf = plan.manifest
    print(f"\nDistribution: {mf.name} v{mf.version}")
    if mf.description:
        print(f"  {mf.description}")
    if mf.author:
        print(f"  Author:   {mf.author}")
    if mf.marlow_requires:
        print(f"  Requires: Marlow {mf.marlow_requires}")
    print(f"  Source:   {plan.provenance}")
    print(f"  Target:   {plan.target_dir}")
    if plan.existing:
        # Distinguish "updating an existing distribution" (well-understood
        # semantics — dist-owned overwritten, config preserved, user data
        # untouched) from "overwriting a hand-built plain profile" (same
        # mechanics but the user didn't sign up for this when they created
        # the profile manually).
        existing_is_distribution = (plan.target_dir / MANIFEST_FILENAME).is_file()
        if existing_is_distribution:
            print("  (profile exists — will overwrite distribution-owned files only)")
        else:
            print(
                "  ⚠ Profile exists but is NOT a distribution.  Installing here will\n"
                "    overwrite its SOUL.md, skills/, cron/, and mcp.json.\n"
                "    Your memories, sessions, auth.json, and .env will be preserved,\n"
                "    but any hand-edits to distribution-owned files will be lost."
            )
    if mf.env_requires:
        print("\n  Env vars:")
        for er in mf.env_requires:
            tag = "required" if er.required else "optional"
            # Check both the current shell environment and the target profile's
            # .env file so we don't nag about keys the user already has set up.
            already = os.environ.get(er.name) is not None
            if not already and plan.target_dir.is_dir():
                env_path = plan.target_dir / ".env"
                if env_path.is_file():
                    try:
                        for raw in env_path.read_text().splitlines():
                            line = raw.strip()
                            if not line or line.startswith("#"):
                                continue
                            key = line.split("=", 1)[0].strip()
                            if key == er.name:
                                already = True
                                break
                    except OSError:
                        pass
            status = "✓ set" if already else ("needs setting" if er.required else "—")
            line = f"    • {er.name} ({tag}, {status})"
            if er.description:
                line += f" — {er.description}"
            print(line)
    if plan.has_cron:
        print(
            "\n  ⚠ This distribution ships cron jobs.  They will NOT run "
            "automatically — review and enable manually."
        )






def cmd_completion(args, parser=None):
    """Print shell completion script."""
    from marlow_cli.completion import generate_bash, generate_zsh, generate_fish

    shell = getattr(args, "shell", "bash")
    if shell == "zsh":
        print(generate_zsh(parser))
    elif shell == "fish":
        print(generate_fish(parser))
    else:
        print(generate_bash(parser))


def cmd_prompt_size(args):
    """Show a byte/char breakdown of the system prompt + tool schemas."""
    from marlow_cli.prompt_size import cmd_prompt_size as _impl

    _impl(args)


def cmd_logs(args):
    """View and filter Marlow log files."""
    from marlow_cli.logs import tail_log, list_logs

    log_name = getattr(args, "log_name", "agent") or "agent"

    if log_name == "list":
        list_logs()
        return

    tail_log(
        log_name,
        num_lines=getattr(args, "lines", 50),
        follow=getattr(args, "follow", False),
        level=getattr(args, "level", None),
        session=getattr(args, "session", None),
        since=getattr(args, "since", None),
        component=getattr(args, "component", None),
    )
# Top-level subcommands that argparse knows about WITHOUT running plugin
# discovery.  Used to short-circuit eager plugin imports (which can take
# 500ms+ pulling in google.cloud.pubsub_v1, aiohttp, grpc, etc.) when the
# user's invocation clearly doesn't need any plugin-registered subcommand.
#
# Keep this in sync with the ``subparsers.add_parser("NAME", ...)`` calls
# below in ``main()``. Missing an entry here only costs a one-time
# discovery; extra entries here would let a plugin command silently fail
# to parse.
_BUILTIN_SUBCOMMANDS = frozenset(
    {
        "checkpoints", "completion",
        "config", "cron", "debug", "doctor",
        "dump", "runtime", "gateway", "hooks", "insights",
        "experience",
        "login", "logout", "logs", "mcp", "memory",
        "model", "pairing", "plugins", "postinstall", "profile",
        "prompt-size",
        "send", "sessions", "setup",
        "skills", "slack", "status", "tools", "uninstall", "update",
        "version", "webhook", "chat", "security",
        # Help-ish invocations — plugin commands not being listed in
        # top-level --help is an acceptable trade-off for skipping an
        # expensive eager import of every bundled plugin module.
        "help",
    }
)


# Top-level flags that take a value. Needed by ``_first_positional_argv``
# so that in ``marlow -m gpt5 chat``, ``gpt5`` is correctly skipped as a
# flag value rather than misclassified as a subcommand. Kept in sync with
# the top-level flags declared in ``marlow_cli/_parser.py``.
#
# Correctness-safe either way: missing an entry here only makes the
# fast-path bail out too eagerly (we run plugin discovery when we didn't
# need to); extra entries would make us skip a real positional.
_TOP_LEVEL_VALUE_FLAGS = frozenset(
    {
        "-z", "--oneshot",
        "-m", "--model",
        "--provider",
        "-t", "--toolsets",
        "-r", "--resume",
        # ``-c / --continue`` is nargs='?' (optional value). Treat it as
        # value-taking: if the next token is a subcommand-looking word
        # the user almost certainly meant it as the session name, and
        # either interpretation keeps us on the safe side.
        "-c", "--continue",
    }
)


def _first_positional_argv() -> str | None:
    """Return the first non-flag, non-flag-value token in ``sys.argv[1:]``.

    Used by ``main()`` to decide whether plugin discovery has to run at
    argparse-setup time. Handles common invocations like
    ``marlow -m gpt5 --provider openai chat "msg"`` by skipping the
    values attached to known top-level flags.

    Does NOT fully simulate argparse — unknown ``--foo=bar`` / ``--foo
    bar`` flags degrade gracefully (``bar`` may be wrongly classified as
    a positional, which at worst forces a one-time plugin discovery).
    """
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            # Everything after ``--`` is positional.
            if i + 1 < len(argv):
                return argv[i + 1]
            return None
        if tok.startswith("-"):
            # ``--flag=value`` carries its value inline — single token.
            if "=" in tok:
                i += 1
                continue
            if tok in _TOP_LEVEL_VALUE_FLAGS and i + 1 < len(argv):
                i += 2
                continue
            i += 1
            continue
        return tok
    return None


def _plugin_cli_discovery_needed() -> bool:
    """True when the CLI might be invoking a plugin-registered subcommand.

    Returning False lets ``main()`` skip plugin discovery entirely during
    argparse setup, saving ~500-650ms per invocation for users whose
    enabled plugins don't contribute any CLI command.
    """
    first = _first_positional_argv()
    if first is None:
        return False
    if first in _BUILTIN_SUBCOMMANDS:
        return False
    # Unknown token — could be a plugin subcommand, OR a chat prompt
    # starting with a non-flag word. Either way we need discovery: if it
    # IS a plugin command, argparse needs the subparser; if it's a chat
    # prompt, argparse will route it via positional handling and the
    # extra discovery cost is amortized over a full agent run anyway.
    return True


_AGENT_COMMANDS = {None, "chat", "rl"}
_AGENT_SUBCOMMANDS = {
    "cron": ("cron_command", {"run", "tick"}),
    "gateway": ("gateway_command", {"run"}),
    "mcp": ("mcp_action", {"serve"}),
}


def _command_has_dedicated_mcp_startup(args) -> bool:
    if args.command == "gateway" and getattr(args, "gateway_command", None) == "run":
        return True
    if args.command == "cron" and getattr(args, "cron_command", None) in {"run", "tick"}:
        return True
    return False


def _prepare_agent_startup(args) -> None:
    """Discover plugins/MCP/hooks for commands that can run an agent turn."""
    _sub_attr, _sub_set = _AGENT_SUBCOMMANDS.get(args.command, (None, None))
    if not (
        args.command in _AGENT_COMMANDS
        or (_sub_attr and getattr(args, _sub_attr, None) in _sub_set)
    ):
        return

    _accept_hooks = bool(getattr(args, "accept_hooks", False))
    try:
        from marlow_cli.plugins import discover_plugins

        discover_plugins()
    except Exception:
        logger.warning(
            "plugin discovery failed at CLI startup",
            exc_info=True,
        )
    if getattr(args, "oneshot", None) is not None or args.command == "chat":
        from marlow_cli.mcp_startup import start_background_mcp_discovery

        start_background_mcp_discovery(logger=logger, thread_name="cli-mcp-discovery")
    elif not _command_has_dedicated_mcp_startup(args):
        try:
            from tools.mcp_tool import discover_mcp_tools

            discover_mcp_tools()
        except Exception:
            logger.debug("MCP tool discovery failed at CLI startup", exc_info=True)
    try:
        from marlow_cli.config import load_config
        from agent.shell_hooks import register_from_config

        register_from_config(
            load_config(),
            accept_hooks=_accept_hooks,
            allow_prompt=getattr(args, "oneshot", None) is None,
        )
    except Exception:
        logger.debug(
            "shell-hook registration failed at CLI startup",
            exc_info=True,
        )


def main():
    """Main entry point for marlow CLI."""
    # Cosmetic: make the process show up as 'marlow' instead of 'python3.11'
    # in ps/top/htop.  Non-fatal — just a nicer UX.
    _set_process_title()

    from marlow_cli._parser import build_top_level_parser

    parser, subparsers, chat_parser = build_top_level_parser()
    chat_parser.set_defaults(func=cmd_chat)

    from marlow_cli.runtime import command as runtime_command
    runtime_parser = subparsers.add_parser('runtime', help='Codex execution status and recovery')
    runtime_actions = runtime_parser.add_subparsers(dest='runtime_action', required=True)
    runtime_actions.add_parser('status')
    acknowledge = runtime_actions.add_parser('acknowledge')
    for identity in ('session', 'thread', 'turn'):
        acknowledge.add_argument('--' + identity, required=True)
    acknowledge.add_argument('--checked', action='store_true', help='Actual side effects have been verified')
    runtime_parser.set_defaults(func=runtime_command)

    # =========================================================================
    # model command
    # =========================================================================
    model_parser = subparsers.add_parser(
        "model",
        help="Select default model and provider",
        description="Interactively select your inference provider and default model",
    )
    model_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Wipe the model picker disk cache and re-fetch every provider's live /v1/models list.",
    )
    model_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the browser automatically during OAuth login",
    )
    model_parser.add_argument(
        "--manual-paste",
        action="store_true",
        help="Paste a Codex OAuth callback URL when loopback login is unavailable.",
    )
    model_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP request timeout in seconds (default: 15)",
    )
    model_parser.add_argument(
        "--ca-bundle", help="Path to CA bundle PEM file for TLS verification"
    )
    model_parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification (testing only)",
    )
    model_parser.set_defaults(func=cmd_model)

    # =========================================================================
    gateway_parser = subparsers.add_parser(
        "gateway",
        help="Messaging gateway management",
        description="Manage the Telegram, Discord, Slack, Feishu, email, and webhook gateway",
    )
    gateway_subparsers = gateway_parser.add_subparsers(dest="gateway_command")

    # gateway run (default)
    gateway_run = gateway_subparsers.add_parser(
        "run", help="Run gateway in foreground (recommended for Docker)"
    )
    gateway_run.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase stderr log verbosity (-v=INFO, -vv=DEBUG)",
    )
    gateway_run.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress all stderr log output"
    )
    gateway_run.add_argument(
        "--replace",
        action="store_true",
        help="Replace any existing gateway instance (useful for systemd)",
    )
    gateway_run.add_argument(
        "--no-supervise",
        action="store_true",
        help=(
            "Inside the s6-overlay Docker image, normally `gateway run` is "
            "automatically redirected to the supervised s6 service. Pass "
            "--no-supervise to opt out and "
            "get the historical pre-s6 foreground behavior: the gateway is "
            "the container's main process and the container exits with the "
            "gateway's exit code. No effect outside an s6 container."
        ),
    )
    _add_accept_hooks_flag(gateway_run)
    _add_accept_hooks_flag(gateway_parser)

    # gateway start
    gateway_start = gateway_subparsers.add_parser(
        "start", help="Start the installed systemd/launchd background service"
    )
    gateway_start.add_argument(
        "--system",
        action="store_true",
        help="Target the Linux system-level gateway service",
    )
    gateway_start.add_argument(
        "--all",
        action="store_true",
        help="Kill ALL stale gateway processes across all profiles before starting",
    )

    # gateway stop
    gateway_stop = gateway_subparsers.add_parser("stop", help="Stop gateway service")
    gateway_stop.add_argument(
        "--system",
        action="store_true",
        help="Target the Linux system-level gateway service",
    )
    gateway_stop.add_argument(
        "--all",
        action="store_true",
        help="Stop ALL gateway processes across all profiles",
    )

    # gateway restart
    gateway_restart = gateway_subparsers.add_parser(
        "restart", help="Restart gateway service"
    )
    gateway_restart.add_argument(
        "--system",
        action="store_true",
        help="Target the Linux system-level gateway service",
    )
    gateway_restart.add_argument(
        "--all",
        action="store_true",
        help="Kill ALL gateway processes across all profiles before restarting",
    )

    # gateway status
    gateway_status = gateway_subparsers.add_parser("status", help="Show gateway status")
    gateway_status.add_argument("--deep", action="store_true", help="Deep status check")
    gateway_status.add_argument(
        "-l",
        "--full",
        action="store_true",
        help="Show full, untruncated service/log output where supported",
    )
    gateway_status.add_argument(
        "--system",
        action="store_true",
        help="Target the Linux system-level gateway service",
    )

    # gateway install
    gateway_install = gateway_subparsers.add_parser(
        "install", help="Install gateway as a systemd/launchd background service"
    )
    gateway_install.add_argument("--force", action="store_true", help="Force reinstall")
    gateway_install.add_argument(
        "--system",
        action="store_true",
        help="Install as a Linux system-level service (starts at boot)",
    )
    gateway_install.add_argument(
        "--run-as-user",
        dest="run_as_user",
        help="User account the Linux system service should run as",
    )
    gateway_install.add_argument(
        "--start-now",
        dest="start_now",
        action="store_true",
        default=None,
        help=argparse.SUPPRESS,
    )
    gateway_install.add_argument(
        "--no-start-now",
        dest="start_now",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    gateway_install.add_argument(
        "--start-on-login",
        dest="start_on_login",
        action="store_true",
        default=None,
        help=argparse.SUPPRESS,
    )
    gateway_install.add_argument(
        "--no-start-on-login",
        dest="start_on_login",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    gateway_install.add_argument(
        "--elevated-handoff",
        dest="elevated_handoff",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    # gateway uninstall
    gateway_uninstall = gateway_subparsers.add_parser(
        "uninstall", help="Uninstall gateway service"
    )
    gateway_uninstall.add_argument(
        "--system",
        action="store_true",
        help="Target the Linux system-level gateway service",
    )

    # gateway list
    gateway_subparsers.add_parser("list", help="List all profiles and their gateway status")

    # gateway setup
    gateway_subparsers.add_parser("setup", help="Configure messaging platforms")

    # gateway migrate-legacy
    gateway_migrate_legacy = gateway_subparsers.add_parser(
        "migrate-legacy",
        help="Remove legacy marlow.service units from pre-rename installs",
        description=(
            "Stop, disable, and remove legacy Marlow gateway unit files "
            "(e.g. marlow.service) left over from older installs. Profile "
            "units (marlow-gateway-<profile>.service) and unrelated "
            "third-party services are never touched."
        ),
    )
    gateway_migrate_legacy.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="List what would be removed without doing it",
    )
    gateway_migrate_legacy.add_argument(
        "-y",
        "--yes",
        dest="yes",
        action="store_true",
        help="Skip the confirmation prompt",
    )

    gateway_parser.set_defaults(func=cmd_gateway)

    # =========================================================================
    # setup command
    # =========================================================================
    setup_parser = subparsers.add_parser(
        "setup",
        help="Interactive setup wizard",
        description="Configure Marlow Agent with an interactive wizard. "
        "Run a specific section: marlow setup model|terminal|gateway|tools|agent",
    )
    setup_parser.add_argument(
        "section",
        nargs="?",
        choices=["model", "terminal", "gateway", "tools", "agent"],
        default=None,
        help="Run a specific setup section instead of the full wizard",
    )
    setup_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Non-interactive mode (use defaults/env vars)",
    )
    setup_parser.add_argument(
        "--reset", action="store_true", help="Reset configuration to defaults"
    )
    setup_parser.add_argument(
        "--reconfigure",
        action="store_true",
        help="(Default on existing installs.) Re-run the full wizard, "
        "showing current values as defaults. Kept for backwards "
        "compatibility — a bare 'marlow setup' now does this.",
    )
    setup_parser.add_argument(
        "--quick",
        action="store_true",
        help="On existing installs: only prompt for items that are missing "
        "or unset, instead of running the full reconfigure wizard.",
    )
    setup_parser.set_defaults(func=cmd_setup)

    # =========================================================================
    # postinstall command
    # =========================================================================
    postinstall_parser = subparsers.add_parser(
        "postinstall",
        help="Bootstrap non-Python deps for pip installs (node, browser, ripgrep)",
        description="One-shot post-install for pip users. Installs system "
        "dependencies that pip cannot provide, then runs setup if needed.",
    )
    postinstall_parser.set_defaults(func=cmd_postinstall)

    # =========================================================================
    # slack command
    # =========================================================================
    slack_parser = subparsers.add_parser(
        "slack",
        help="Slack integration helpers (manifest generation, etc.)",
        description="Slack integration helpers for Marlow.",
    )
    slack_sub = slack_parser.add_subparsers(dest="slack_command")
    slack_manifest = slack_sub.add_parser(
        "manifest",
        help="Print or write a Slack app manifest with every gateway command "
        "registered as a native slash (/btw, /stop, /model, ...)",
        description=(
            "Generate a Slack app manifest that registers every gateway "
            "command in COMMAND_REGISTRY as a first-class Slack slash "
            "command (matching Discord and Telegram parity). Paste the "
            "output into Slack app config → Features → App Manifest → "
            "Edit, then Save. Reinstall the app if Slack prompts for it."
        ),
    )
    slack_manifest.add_argument(
        "--write",
        nargs="?",
        const=True,
        default=None,
        metavar="PATH",
        help="Write manifest to a file instead of stdout. With no PATH "
        "writes to $MARLOW_HOME/slack-manifest.json.",
    )
    slack_manifest.add_argument(
        "--name",
        default=None,
        help='Bot display name (default: "Marlow")',
    )
    slack_manifest.add_argument(
        "--description",
        default=None,
        help="Bot description shown in Slack's app directory.",
    )
    slack_manifest.add_argument(
        "--slashes-only",
        action="store_true",
        help="Emit only the features.slash_commands array (for merging "
        "into an existing manifest manually).",
    )
    slack_parser.set_defaults(func=cmd_slack)

    # =========================================================================
    # send command — pipe shell-script output to any configured platform
    # =========================================================================
    from marlow_cli.send_cmd import register_send_subparser
    register_send_subparser(subparsers)

    # =========================================================================
    # login command
    # =========================================================================
    login_parser = subparsers.add_parser(
        "login",
        help="Authenticate with an inference provider",
        description="Run OAuth device authorization flow for Marlow CLI",
    )
    login_parser.add_argument(
        "--provider",
        choices=["openai-codex"],
        default="openai-codex",
        help="Provider to authenticate with",
    )
    login_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not attempt to open the browser automatically",
    )
    login_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP request timeout in seconds (default: 15)",
    )
    login_parser.add_argument(
        "--ca-bundle", help="Path to CA bundle PEM file for TLS verification"
    )
    login_parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification (testing only)",
    )
    login_parser.set_defaults(func=cmd_login)

    # =========================================================================
    # logout command
    # =========================================================================
    logout_parser = subparsers.add_parser(
        "logout",
        help="Clear authentication for an inference provider",
        description="Remove stored credentials and reset provider config",
    )
    logout_parser.add_argument(
        "--provider",
        choices=["openai-codex"],
        default="openai-codex",
        help="Provider to log out from",
    )
    logout_parser.set_defaults(func=cmd_logout)

    # =========================================================================
    # status command
    # =========================================================================
    status_parser = subparsers.add_parser(
        "status",
        help="Show status of all components",
        description="Display status of Marlow Agent components",
    )
    status_parser.add_argument(
        "--all", action="store_true", help="Show all details (redacted for sharing)"
    )
    status_parser.add_argument(
        "--deep", action="store_true", help="Run deep checks (may take longer)"
    )
    status_parser.set_defaults(func=cmd_status)

    # =========================================================================
    # cron command
    # =========================================================================
    cron_parser = subparsers.add_parser(
        "cron", help="Cron job management", description="Manage scheduled tasks"
    )
    cron_subparsers = cron_parser.add_subparsers(dest="cron_command")

    # cron list
    cron_list = cron_subparsers.add_parser("list", help="List scheduled jobs")
    cron_list.add_argument("--all", action="store_true", help="Include disabled jobs")

    # cron create/add
    cron_create = cron_subparsers.add_parser(
        "create", aliases=["add"], help="Create a scheduled job"
    )
    cron_create.add_argument(
        "schedule", help="Schedule like '30m', 'every 2h', or '0 9 * * *'"
    )
    cron_create.add_argument(
        "prompt", nargs="?", help="Optional self-contained prompt or task instruction"
    )
    cron_create.add_argument("--name", help="Optional human-friendly job name")
    cron_create.add_argument(
        "--deliver",
        help="Delivery target: origin, local, a retained platform, or platform:chat_id",
    )
    cron_create.add_argument("--repeat", type=int, help="Optional repeat count")
    cron_create.add_argument(
        "--script",
        help=(
            "Path to a script under ~/.marlow/scripts/. Default mode: "
            "script stdout is injected into the agent's prompt each run. "
            "With --no-agent: the script IS the job and its stdout is "
            "delivered verbatim. .sh/.bash files run via bash, everything "
            "else via Python."
        ),
    )
    cron_create.add_argument(
        "--no-agent",
        dest="no_agent",
        action="store_true",
        default=False,
        help=(
            "Skip the LLM entirely — run --script on schedule and deliver "
            "its stdout directly. Empty stdout = silent. Classic watchdog "
            "pattern (memory alerts, disk alerts, CI pings)."
        ),
    )
    cron_create.add_argument(
        "--workdir",
        help="Absolute path for the job to run from. Injects AGENTS.md / CLAUDE.md / .cursorrules from that directory and uses it as the cwd for terminal/file/code_exec tools. Omit to preserve old behaviour (no project context files).",
    )
    cron_create.add_argument(
        "--profile",
        help="Marlow profile name to run the job under. Use 'default' for the root profile. Named profiles must already exist. Omit to preserve the scheduler's existing profile.",
    )

    # cron edit
    cron_edit = cron_subparsers.add_parser(
        "edit", help="Edit an existing scheduled job"
    )
    cron_edit.add_argument("job_id", help="Job ID to edit")
    cron_edit.add_argument("--schedule", help="New schedule")
    cron_edit.add_argument("--prompt", help="New prompt/task instruction")
    cron_edit.add_argument("--name", help="New job name")
    cron_edit.add_argument("--deliver", help="New delivery target")
    cron_edit.add_argument("--repeat", type=int, help="New repeat count")
    cron_edit.add_argument(
        "--script",
        help=(
            "Path to a script under ~/.marlow/scripts/. Pass empty string to clear. "
            "With --no-agent the script IS the job; otherwise its stdout is "
            "injected into the agent's prompt each run."
        ),
    )
    cron_edit.add_argument(
        "--no-agent",
        dest="no_agent",
        action="store_const",
        const=True,
        default=None,
        help=(
            "Enable no-agent mode on this job (requires --script or an "
            "existing script on the job)."
        ),
    )
    cron_edit.add_argument(
        "--agent",
        dest="no_agent",
        action="store_const",
        const=False,
        help="Disable no-agent mode on this job (reverts to LLM-driven execution).",
    )
    cron_edit.add_argument(
        "--workdir",
        help="Absolute path for the job to run from (injects AGENTS.md etc. and sets terminal cwd). Pass empty string to clear.",
    )
    cron_edit.add_argument(
        "--profile",
        help="Marlow profile name to run the job under. Use 'default' for the root profile. Pass empty string to clear.",
    )

    # lifecycle actions
    cron_pause = cron_subparsers.add_parser("pause", help="Pause a scheduled job")
    cron_pause.add_argument("job_id", help="Job ID to pause")

    cron_resume = cron_subparsers.add_parser("resume", help="Resume a paused job")
    cron_resume.add_argument("job_id", help="Job ID to resume")

    cron_run = cron_subparsers.add_parser(
        "run", help="Run a job on the next scheduler tick"
    )
    cron_run.add_argument("job_id", help="Job ID to trigger")
    _add_accept_hooks_flag(cron_run)

    cron_remove = cron_subparsers.add_parser(
        "remove", aliases=["rm", "delete"], help="Remove a scheduled job"
    )
    cron_remove.add_argument("job_id", help="Job ID to remove")

    # cron status
    cron_subparsers.add_parser("status", help="Check if cron scheduler is running")

    # cron tick (mostly for debugging)
    cron_tick = cron_subparsers.add_parser("tick", help="Run due jobs once and exit")
    _add_accept_hooks_flag(cron_tick)
    _add_accept_hooks_flag(cron_parser)
    cron_parser.set_defaults(func=cmd_cron)

    # =========================================================================
    # webhook command
    # =========================================================================
    webhook_parser = subparsers.add_parser(
        "webhook",
        help="Manage dynamic webhook subscriptions",
        description="Create, list, and remove webhook subscriptions for event-driven agent activation",
    )
    webhook_subparsers = webhook_parser.add_subparsers(dest="webhook_action")

    wh_sub = webhook_subparsers.add_parser(
        "subscribe", aliases=["add"], help="Create a webhook subscription"
    )
    wh_sub.add_argument("name", help="Route name (used in URL: /webhooks/<name>)")
    wh_sub.add_argument(
        "--prompt", default="", help="Prompt template with {dot.notation} payload refs"
    )
    wh_sub.add_argument(
        "--events", default="", help="Comma-separated event types to accept"
    )
    wh_sub.add_argument("--description", default="", help="What this subscription does")
    wh_sub.add_argument(
        "--deliver",
        default="log",
        help="Delivery target: log, telegram, discord, slack, etc.",
    )
    wh_sub.add_argument(
        "--deliver-chat-id",
        default="",
        help="Target chat ID for cross-platform delivery",
    )
    wh_sub.add_argument(
        "--secret", default="", help="HMAC secret (auto-generated if omitted)"
    )
    wh_sub.add_argument(
        "--deliver-only",
        action="store_true",
        help="Skip the agent — deliver the rendered prompt directly as the "
        "message. Zero LLM cost. Requires --deliver to be a real target "
        "(not 'log').",
    )

    webhook_subparsers.add_parser(
        "list", aliases=["ls"], help="List all dynamic subscriptions"
    )

    wh_rm = webhook_subparsers.add_parser(
        "remove", aliases=["rm"], help="Remove a subscription"
    )
    wh_rm.add_argument("name", help="Subscription name to remove")

    wh_test = webhook_subparsers.add_parser(
        "test", help="Send a test POST to a webhook route"
    )
    wh_test.add_argument("name", help="Subscription name to test")
    wh_test.add_argument(
        "--payload", default="", help="JSON payload to send (default: test payload)"
    )

    webhook_parser.set_defaults(func=cmd_webhook)

    # =========================================================================
    # hooks command — shell-hook inspection and management
    # =========================================================================
    hooks_parser = subparsers.add_parser(
        "hooks",
        help="Inspect and manage shell-script hooks",
        description=(
            "Inspect shell-script hooks declared in ~/.marlow/config.yaml, "
            "test them against synthetic payloads, and manage the first-use "
            "consent allowlist at ~/.marlow/shell-hooks-allowlist.json."
        ),
    )
    hooks_subparsers = hooks_parser.add_subparsers(dest="hooks_action")

    hooks_subparsers.add_parser(
        "list",
        aliases=["ls"],
        help="List configured hooks with matcher, timeout, and consent status",
    )

    _hk_test = hooks_subparsers.add_parser(
        "test",
        help="Fire every hook matching <event> against a synthetic payload",
    )
    _hk_test.add_argument(
        "event",
        help="Hook event name (e.g. pre_tool_call, pre_llm_call, subagent_stop)",
    )
    _hk_test.add_argument(
        "--for-tool",
        dest="for_tool",
        default=None,
        help=(
            "Only fire hooks whose matcher matches this tool name "
            "(used for pre_tool_call / post_tool_call)"
        ),
    )
    _hk_test.add_argument(
        "--payload-file",
        dest="payload_file",
        default=None,
        help=(
            "Path to a JSON file whose contents are merged into the "
            "synthetic payload before execution"
        ),
    )

    _hk_revoke = hooks_subparsers.add_parser(
        "revoke",
        aliases=["remove", "rm"],
        help="Remove a command's allowlist entries (takes effect on next restart)",
    )
    _hk_revoke.add_argument(
        "command",
        help="The exact command string to revoke (as declared in config.yaml)",
    )

    hooks_subparsers.add_parser(
        "doctor",
        help=(
            "Check each configured hook: exec bit, allowlist, mtime drift, "
            "JSON validity, and synthetic run timing"
        ),
    )

    hooks_parser.set_defaults(func=cmd_hooks)

    # =========================================================================
    # doctor command
    # =========================================================================
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check configuration and dependencies",
        description="Diagnose issues with Marlow Agent setup",
    )
    doctor_parser.add_argument(
        "--fix", action="store_true", help="Attempt to fix issues automatically"
    )
    doctor_parser.add_argument(
        "--ack",
        metavar="ADVISORY_ID",
        default=None,
        help=(
            "Acknowledge a security advisory by ID and exit. After ack, the "
            "advisory will no longer trigger startup banners. Run `marlow "
            "doctor` first to see active advisories and their IDs."
        ),
    )
    doctor_parser.set_defaults(func=cmd_doctor)

    # =========================================================================
    # security command — on-demand supply-chain audit
    # =========================================================================
    security_parser = subparsers.add_parser(
        "security",
        help="Supply-chain audit (OSV.dev) for venv, plugins, and MCP servers",
        description=(
            "On-demand vulnerability scan against OSV.dev. Covers the Marlow "
            "venv (installed PyPI dists), Python deps declared by plugins under "
            "~/.marlow/plugins/, and pinned npx/uvx MCP servers in config.yaml. "
            "Does NOT scan globally-installed packages or editor/browser extensions."
        ),
    )
    security_subparsers = security_parser.add_subparsers(
        dest="security_command",
        metavar="<subcommand>",
    )

    audit_parser = security_subparsers.add_parser(
        "audit",
        help="Run a one-shot supply-chain audit",
        description="Query OSV.dev for known vulnerabilities in installed components.",
    )
    audit_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text",
    )
    audit_parser.add_argument(
        "--fail-on",
        default="critical",
        choices=["low", "moderate", "high", "critical"],
        help="Exit non-zero when any finding meets this severity (default: critical)",
    )
    audit_parser.add_argument(
        "--skip-venv",
        action="store_true",
        help="Skip scanning the Marlow Python venv",
    )
    audit_parser.add_argument(
        "--skip-plugins",
        action="store_true",
        help="Skip scanning plugin requirements files",
    )
    audit_parser.add_argument(
        "--skip-mcp",
        action="store_true",
        help="Skip scanning pinned MCP servers in config.yaml",
    )
    audit_parser.set_defaults(func=cmd_security)
    security_parser.set_defaults(func=cmd_security)

    # =========================================================================
    # dump command
    # =========================================================================
    dump_parser = subparsers.add_parser(
        "dump",
        help="Dump setup summary for support/debugging",
        description="Output a compact, plain-text summary of your Marlow setup "
        "that can be copy-pasted into Discord/GitHub for support context",
    )
    dump_parser.add_argument(
        "--show-keys",
        action="store_true",
        help="Show redacted API key prefixes (first/last 4 chars) instead of just set/not set",
    )
    dump_parser.set_defaults(func=cmd_dump)

    # =========================================================================
    # debug command
    # =========================================================================
    debug_parser = subparsers.add_parser(
        "debug",
        help="Debug tools — upload logs and system info for support",
        description="Debug utilities for Marlow Agent. Use 'marlow debug share' to "
        "upload a debug report (system info + recent logs) to a paste "
        "service and get a shareable URL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    marlow debug share              Upload debug report and print URL
    marlow debug share --lines 500  Include more log lines
    marlow debug share --expire 30  Keep paste for 30 days
    marlow debug share --local      Print report locally (no upload)
    marlow debug share --no-redact  Disable upload-time secret redaction
    marlow debug delete <url>       Delete a previously uploaded paste
""",
    )
    debug_sub = debug_parser.add_subparsers(dest="debug_command")
    share_parser = debug_sub.add_parser(
        "share",
        help="Upload debug report to a paste service and print a shareable URL",
    )
    share_parser.add_argument(
        "--lines",
        type=int,
        default=200,
        help="Number of log lines to include per log file (default: 200)",
    )
    share_parser.add_argument(
        "--expire",
        type=int,
        default=7,
        help="Paste expiry in days (default: 7)",
    )
    share_parser.add_argument(
        "--local",
        action="store_true",
        help="Print the report locally instead of uploading",
    )
    share_parser.add_argument(
        "--no-redact",
        action="store_true",
        help=(
            "Disable upload-time secret redaction (default: redact). Logs "
            "are normally run through agent.redact.redact_sensitive_text "
            "with force=True before upload so credentials are not leaked "
            "into the public paste service."
        ),
    )
    delete_parser = debug_sub.add_parser(
        "delete",
        help="Delete a paste uploaded by 'marlow debug share'",
    )
    delete_parser.add_argument(
        "urls",
        nargs="*",
        default=[],
        help="One or more paste URLs to delete (e.g. https://paste.rs/abc123)",
    )
    debug_parser.set_defaults(func=cmd_debug)

    # =========================================================================
    # checkpoints command
    # =========================================================================
    checkpoints_parser = subparsers.add_parser(
        "checkpoints",
        help="Inspect / prune / clear ~/.marlow/checkpoints/",
        description="Manage the filesystem checkpoint store — the shadow git "
        "repo marlow uses to snapshot working directories before "
        "write_file/patch/terminal calls. Lets you see how much "
        "space checkpoints occupy, force a prune, or wipe the base.",
    )
    from marlow_cli.checkpoints import register_cli as _register_checkpoints_cli
    _register_checkpoints_cli(checkpoints_parser)

    # =========================================================================
    # config command
    # =========================================================================
    config_parser = subparsers.add_parser(
        "config",
        help="View and edit configuration",
        description="Manage Marlow Agent configuration",
    )
    config_subparsers = config_parser.add_subparsers(dest="config_command")

    # config show (default)
    config_subparsers.add_parser("show", help="Show current configuration")

    # config edit
    config_subparsers.add_parser("edit", help="Open config file in editor")

    # config set
    config_set = config_subparsers.add_parser("set", help="Set a configuration value")
    config_set.add_argument(
        "key", nargs="?", help="Configuration key (e.g., model, terminal.backend)"
    )
    config_set.add_argument("value", nargs="?", help="Value to set")

    # config path
    config_subparsers.add_parser("path", help="Print config file path")

    # config env-path
    config_subparsers.add_parser("env-path", help="Print .env file path")

    # config check
    config_subparsers.add_parser("check", help="Check for missing/outdated config")

    # config migrate
    config_subparsers.add_parser("migrate", help="Update config with new options")

    config_parser.set_defaults(func=cmd_config)

    # =========================================================================
    # pairing command
    # =========================================================================
    pairing_parser = subparsers.add_parser(
        "pairing",
        help="Manage DM pairing codes for user authorization",
        description="Approve or revoke user access via pairing codes",
    )
    pairing_sub = pairing_parser.add_subparsers(dest="pairing_action")

    pairing_sub.add_parser("list", help="Show pending + approved users")

    pairing_approve_parser = pairing_sub.add_parser(
        "approve", help="Approve a pairing code"
    )
    pairing_approve_parser.add_argument(
        "platform", help="Platform name (telegram, discord, slack, email, feishu)"
    )
    pairing_approve_parser.add_argument("code", help="Pairing code to approve")

    pairing_revoke_parser = pairing_sub.add_parser("revoke", help="Revoke user access")
    pairing_revoke_parser.add_argument("platform", help="Platform name")
    pairing_revoke_parser.add_argument("user_id", help="User ID to revoke")

    pairing_sub.add_parser("clear-pending", help="Clear all pending codes")

    def cmd_pairing(args):
        from marlow_cli.pairing import pairing_command

        pairing_command(args)

    pairing_parser.set_defaults(func=cmd_pairing)

    # =========================================================================
    # skills command
    # =========================================================================
    skills_parser = subparsers.add_parser(
        "skills",
        help="Configure locally installed skills",
        description="Enable or disable local and bundled SKILL.md content.",
    )
    skills_parser.set_defaults(
        func=lambda args: __import__(
            "marlow_cli.skills_config", fromlist=["skills_command"]
        ).skills_command(args)
    )

    # =========================================================================
    # plugins command
    # =========================================================================
    plugins_parser = subparsers.add_parser(
        "plugins",
        help="Manage plugins — install, update, remove, list",
        description="Install plugins from Git repositories, update, remove, or list them.",
    )
    plugins_subparsers = plugins_parser.add_subparsers(dest="plugins_action")

    plugins_install = plugins_subparsers.add_parser(
        "install", help="Install a plugin from a Git URL or owner/repo"
    )
    plugins_install.add_argument(
        "identifier",
        help="Git URL or owner/repo shorthand (e.g. anpicasso/marlow-plugin-chrome-profiles)",
    )
    plugins_install.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Remove existing plugin and reinstall",
    )
    _install_enable_group = plugins_install.add_mutually_exclusive_group()
    _install_enable_group.add_argument(
        "--enable",
        action="store_true",
        help="Auto-enable the plugin after install (skip confirmation prompt)",
    )
    _install_enable_group.add_argument(
        "--no-enable",
        action="store_true",
        help="Install disabled (skip confirmation prompt); enable later with `marlow plugins enable <name>`",
    )

    plugins_update = plugins_subparsers.add_parser(
        "update", help="Pull latest changes for an installed plugin"
    )
    plugins_update.add_argument("name", help="Plugin name to update")

    plugins_remove = plugins_subparsers.add_parser(
        "remove", aliases=["rm", "uninstall"], help="Remove an installed plugin"
    )
    plugins_remove.add_argument("name", help="Plugin directory name to remove")

    plugins_list = plugins_subparsers.add_parser(
        "list", aliases=["ls"], help="List installed plugins"
    )
    plugins_list.add_argument(
        "--enabled",
        action="store_true",
        help="Show only enabled plugins",
    )
    plugins_list.add_argument(
        "--user",
        action="store_true",
        help="Show only user-installed plugins (including git plugins)",
    )
    plugins_list.add_argument(
        "--no-bundled",
        action="store_true",
        help="Hide bundled plugins",
    )
    plugins_list.add_argument(
        "--plain",
        action="store_true",
        help="Print compact plain-text output instead of a Rich table",
    )
    plugins_list.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )

    plugins_enable = plugins_subparsers.add_parser(
        "enable", help="Enable a disabled plugin"
    )
    plugins_enable.add_argument("name", help="Plugin name to enable")

    plugins_disable = plugins_subparsers.add_parser(
        "disable", help="Disable a plugin without removing it"
    )
    plugins_disable.add_argument("name", help="Plugin name to disable")

    def cmd_plugins(args):
        from marlow_cli.plugins_cmd import plugins_command

        plugins_command(args)

    plugins_parser.set_defaults(func=cmd_plugins)

    # =========================================================================
    # Plugin CLI commands — dynamically registered by memory/general plugins.
    # Plugins provide a register_cli(subparser) function that builds their
    # own argparse tree.  No hardcoded plugin commands in main.py.
    #
    # Skipped when the invocation is already targeting a known built-in
    # subcommand — ``marlow --help``, ``marlow version``, ``marlow logs``,
    # etc.  This avoids eagerly importing every bundled plugin module
    # (google.cloud.pubsub_v1, aiohttp, grpc, PIL …) which costs
    # 500-650ms on typical installs.
    # =========================================================================
    if _plugin_cli_discovery_needed():
        try:
            from plugins.memory import discover_plugin_cli_commands
            from marlow_cli.plugins import discover_plugins, get_plugin_manager

            seen_plugin_commands = set()
            for cmd_info in discover_plugin_cli_commands():
                plugin_parser = subparsers.add_parser(
                    cmd_info["name"],
                    help=cmd_info["help"],
                    description=cmd_info.get("description", ""),
                    formatter_class=__import__("argparse").RawDescriptionHelpFormatter,
                )
                cmd_info["setup_fn"](plugin_parser)
                if cmd_info.get("handler_fn") is not None:
                    plugin_parser.set_defaults(func=cmd_info["handler_fn"])
                seen_plugin_commands.add(cmd_info["name"])

            discover_plugins()
            for cmd_info in get_plugin_manager()._cli_commands.values():
                if cmd_info["name"] in seen_plugin_commands:
                    continue
                plugin_parser = subparsers.add_parser(
                    cmd_info["name"],
                    help=cmd_info["help"],
                    description=cmd_info.get("description", ""),
                    formatter_class=__import__("argparse").RawDescriptionHelpFormatter,
                )
                cmd_info["setup_fn"](plugin_parser)
                if cmd_info.get("handler_fn") is not None:
                    plugin_parser.set_defaults(func=cmd_info["handler_fn"])
        except Exception as _exc:
            logging.getLogger(__name__).debug("Plugin CLI discovery failed: %s", _exc)

    # =========================================================================
    # experience command — user governance for durable work experience
    # =========================================================================
    experience_parser = subparsers.add_parser(
        "experience",
        help="Inspect and govern durable work experience",
        description=(
            "Manage project-scoped Work Experience lessons and recall policy. "
            "Policies are stored in the active profile's state.db."
        ),
    )
    from marlow_cli.experience import register_cli as _register_experience_cli

    _register_experience_cli(experience_parser)

    # =========================================================================
    # memory command
    # =========================================================================
    memory_parser = subparsers.add_parser(
        "memory",
        help="Configure external memory provider",
        description=(
            "Set up and manage external memory provider plugins.\n\n"
            "Bundled external memory provider: honcho.\n\n"
            "Only one external provider can be active at a time.\n"
            "Built-in memory (MEMORY.md/USER.md) is always active."
        ),
    )
    memory_sub = memory_parser.add_subparsers(dest="memory_command")
    _setup_parser = memory_sub.add_parser(
        "setup", help="Interactive provider selection and configuration"
    )
    _setup_parser.add_argument(
        "provider",
        nargs="?",
        default=None,
        help="Provider to configure directly (e.g. honcho), skipping the picker",
    )
    memory_sub.add_parser("status", help="Show current memory provider config")
    memory_sub.add_parser("off", help="Disable external provider (built-in only)")
    _reset_parser = memory_sub.add_parser(
        "reset",
        help="Erase all built-in memory (MEMORY.md and USER.md)",
    )
    _reset_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    _reset_parser.add_argument(
        "--target",
        choices=["all", "memory", "user"],
        default="all",
        help="Which store to reset: 'all' (default), 'memory', or 'user'",
    )

    def cmd_memory(args):
        sub = getattr(args, "memory_command", None)
        if sub == "off":
            from marlow_cli.config import load_config, save_config

            config = load_config()
            if not isinstance(config.get("memory"), dict):
                config["memory"] = {}
            config["memory"]["provider"] = ""
            save_config(config)
            print("\n  ✓ Memory provider: built-in only")
            print("  Saved to config.yaml\n")
        elif sub == "reset":
            from marlow_constants import get_marlow_home, display_marlow_home

            mem_dir = get_marlow_home() / "memories"
            target = getattr(args, "target", "all")
            files_to_reset = []
            if target in {"all", "memory"}:
                files_to_reset.append(("MEMORY.md", "agent notes"))
            if target in {"all", "user"}:
                files_to_reset.append(("USER.md", "user profile"))

            # Check what exists
            existing = [
                (f, desc) for f, desc in files_to_reset if (mem_dir / f).exists()
            ]
            if not existing:
                print(
                    f"\n  Nothing to reset — no memory files found in {display_marlow_home()}/memories/\n"
                )
                return

            print(f"\n  This will permanently erase the following memory files:")
            for f, desc in existing:
                path = mem_dir / f
                size = path.stat().st_size
                print(f"    ◆ {f} ({desc}) — {size:,} bytes")

            if not getattr(args, "yes", False):
                try:
                    answer = input("\n  Type 'yes' to confirm: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\n  Cancelled.\n")
                    return
                if answer != "yes":
                    print("  Cancelled.\n")
                    return

            for f, desc in existing:
                (mem_dir / f).unlink()
                print(f"  ✓ Deleted {f} ({desc})")

            print(
                f"\n  Memory reset complete. New sessions will start with a blank slate."
            )
            print(f"  Files were in: {display_marlow_home()}/memories/\n")
        else:
            from marlow_cli.memory_setup import memory_command

            memory_command(args)

    memory_parser.set_defaults(func=cmd_memory)

    # =========================================================================
    # tools command
    # =========================================================================
    tools_parser = subparsers.add_parser(
        "tools",
        help="Configure which tools are enabled per platform",
        description=(
            "Enable, disable, or list tools for CLI, Telegram, Discord, etc.\n\n"
            "Built-in toolsets use plain names (e.g. web, memory).\n"
            "MCP tools use server:tool notation (e.g. github:create_issue).\n\n"
            "Run 'marlow tools' with no subcommand for the interactive configuration UI."
        ),
    )
    tools_parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a summary of enabled tools per platform and exit",
    )
    tools_sub = tools_parser.add_subparsers(dest="tools_action")

    # marlow tools list [--platform cli]
    tools_list_p = tools_sub.add_parser(
        "list",
        help="Show all tools and their enabled/disabled status",
    )
    tools_list_p.add_argument(
        "--platform",
        default="cli",
        help="Platform to show (default: cli)",
    )

    # marlow tools disable <name...> [--platform cli]
    tools_disable_p = tools_sub.add_parser(
        "disable",
        help="Disable toolsets or MCP tools",
    )
    tools_disable_p.add_argument(
        "names",
        nargs="+",
        metavar="NAME",
        help="Toolset name (e.g. web) or MCP tool in server:tool form",
    )
    tools_disable_p.add_argument(
        "--platform",
        default="cli",
        help="Platform to apply to (default: cli)",
    )

    # marlow tools enable <name...> [--platform cli]
    tools_enable_p = tools_sub.add_parser(
        "enable",
        help="Enable toolsets or MCP tools",
    )
    tools_enable_p.add_argument(
        "names",
        nargs="+",
        metavar="NAME",
        help="Toolset name or MCP tool in server:tool form",
    )
    tools_enable_p.add_argument(
        "--platform",
        default="cli",
        help="Platform to apply to (default: cli)",
    )

    def cmd_tools(args):
        action = getattr(args, "tools_action", None)
        if action in {"list", "disable", "enable"}:
            from marlow_cli.tools_config import tools_disable_enable_command

            tools_disable_enable_command(args)
        else:
            _require_tty("tools")
            from marlow_cli.tools_config import tools_command

            tools_command(args)

    tools_parser.set_defaults(func=cmd_tools)

    # =========================================================================
    # mcp command — manage MCP server connections
    # =========================================================================
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Manage MCP servers and run Marlow as an MCP server",
        description=(
            "Manage MCP server connections and run Marlow as an MCP server.\n\n"
            "MCP servers provide additional tools via the Model Context Protocol.\n"
            "Use 'marlow mcp add' to connect to a new server, or\n"
            "'marlow mcp serve' to expose Marlow conversations over MCP."
        ),
    )
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_action")

    mcp_serve_p = mcp_sub.add_parser(
        "serve",
        help="Run Marlow as an MCP server (expose conversations to other agents)",
    )
    mcp_serve_p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging on stderr",
    )
    _add_accept_hooks_flag(mcp_serve_p)

    mcp_add_p = mcp_sub.add_parser(
        "add", help="Add an MCP server (discovery-first install)"
    )
    mcp_add_p.add_argument("name", help="Server name (used as config key)")
    mcp_add_p.add_argument("--url", help="HTTP/SSE endpoint URL")
    # dest="mcp_command" so this flag does not clobber the top-level
    # subparser's args.command attribute, which the dispatcher reads to
    # route to cmd_mcp.  Without an explicit dest, argparse derives
    # dest="command" from the flag name and sets it to None when the
    # flag is omitted, causing `marlow mcp add ...` to fall through to
    # interactive chat.
    mcp_add_p.add_argument(
        "--command", dest="mcp_command", help="Stdio command (e.g. npx)"
    )
    mcp_add_p.add_argument(
        "--args", nargs="*", default=[], help="Arguments for stdio command"
    )
    mcp_add_p.add_argument("--auth", choices=["oauth", "header"], help="Auth method")
    mcp_add_p.add_argument("--preset", help="Known MCP preset name")
    mcp_add_p.add_argument(
        "--env",
        nargs="*",
        default=[],
        help="Environment variables for stdio servers (KEY=VALUE)",
    )

    mcp_rm_p = mcp_sub.add_parser("remove", aliases=["rm"], help="Remove an MCP server")
    mcp_rm_p.add_argument("name", help="Server name to remove")

    mcp_sub.add_parser("list", aliases=["ls"], help="List configured MCP servers")

    mcp_test_p = mcp_sub.add_parser("test", help="Test MCP server connection")
    mcp_test_p.add_argument("name", help="Server name to test")

    mcp_cfg_p = mcp_sub.add_parser(
        "configure", aliases=["config"], help="Toggle tool selection"
    )
    mcp_cfg_p.add_argument("name", help="Server name to configure")

    mcp_login_p = mcp_sub.add_parser(
        "login",
        help="Force re-authentication for an OAuth-based MCP server",
    )
    mcp_login_p.add_argument("name", help="Server name to re-authenticate")

    # ── Catalog (bundled MCP manifests) ─────────────────────────────────────
    mcp_sub.add_parser(
        "picker",
        help="Interactive catalog picker (also the default for `marlow mcp`)",
    )
    mcp_sub.add_parser(
        "catalog",
        help="List bundled MCPs available for one-click install",
    )
    mcp_install_p = mcp_sub.add_parser(
        "install",
        help="Install a catalog MCP by name (e.g. `marlow mcp install n8n`)",
    )
    mcp_install_p.add_argument(
        "identifier",
        help="Catalog entry name (or `official/<name>`)",
    )

    _add_accept_hooks_flag(mcp_parser)

    def cmd_mcp(args):
        from marlow_cli.mcp_config import mcp_command

        mcp_command(args)

    mcp_parser.set_defaults(func=cmd_mcp)

    # =========================================================================
    # sessions command
    # =========================================================================
    sessions_parser = subparsers.add_parser(
        "sessions",
        help="Manage session history (list, rename, export, prune, delete)",
        description="View and manage the SQLite session store",
    )
    sessions_subparsers = sessions_parser.add_subparsers(dest="sessions_action")

    sessions_list = sessions_subparsers.add_parser("list", help="List recent sessions")
    sessions_list.add_argument(
        "--source", help="Filter by source (cli, telegram, discord, etc.)"
    )
    sessions_list.add_argument(
        "--limit", type=int, default=20, help="Max sessions to show"
    )
    sessions_list.add_argument(
        "--workspace",
        metavar="NEEDLE",
        help="Filter by git repo root or project directory",
    )

    sessions_export = sessions_subparsers.add_parser(
        "export", help="Export sessions to a JSONL file"
    )
    sessions_export.add_argument(
        "output", help="Output JSONL file path (use - for stdout)"
    )
    sessions_export.add_argument("--source", help="Filter by source")
    sessions_export.add_argument("--session-id", help="Export a specific session")

    sessions_delete = sessions_subparsers.add_parser(
        "delete", help="Delete a specific session"
    )
    sessions_delete.add_argument("session_id", help="Session ID to delete")
    sessions_delete.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation"
    )

    sessions_prune = sessions_subparsers.add_parser("prune", help="Delete old sessions")
    sessions_prune.add_argument(
        "--older-than",
        type=int,
        default=90,
        help="Delete sessions older than N days (default: 90)",
    )
    sessions_prune.add_argument("--source", help="Only prune sessions from this source")
    sessions_prune.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation"
    )

    sessions_subparsers.add_parser(
        "optimize",
        help="Reclaim disk space: merge FTS5 segments + VACUUM (no data change)",
    )

    sessions_subparsers.add_parser("stats", help="Show session store statistics")

    sessions_rename = sessions_subparsers.add_parser(
        "rename", help="Set or change a session's title"
    )
    sessions_rename.add_argument("session_id", help="Session ID to rename")
    sessions_rename.add_argument("title", nargs="+", help="New title for the session")

    sessions_browse = sessions_subparsers.add_parser(
        "browse",
        help="Interactive session picker — browse, search, and resume sessions",
    )
    sessions_browse.add_argument(
        "--source", help="Filter by source (cli, telegram, discord, etc.)"
    )
    sessions_browse.add_argument(
        "--limit", type=int, default=500, help="Max sessions to load (default: 500)"
    )

    def _confirm_prompt(prompt: str) -> bool:
        """Prompt for y/N confirmation, safe against non-TTY environments."""
        try:
            return input(prompt).strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            return False

    def cmd_sessions(args):
        import json as _json

        try:
            from marlow_state import SessionDB

            db = SessionDB()
        except Exception as e:
            print(f"Error: Could not open session database: {e}")
            return

        action = args.sessions_action

        # Hide third-party tool sessions by default, but honour explicit --source
        _source = getattr(args, "source", None)
        _exclude = None if _source else ["tool"]

        if action == "list":
            from marlow_state import workspace_key as _workspace_key

            sessions = db.list_sessions_rich(
                source=args.source, exclude_sources=_exclude, limit=args.limit
            )
            workspace_filter = (getattr(args, "workspace", None) or "").strip()
            if workspace_filter:
                needle = workspace_filter.lower()
                sessions = [
                    session
                    for session in sessions
                    if (key := (_workspace_key(session) or "").lower())
                    and (
                        needle in key
                        or needle == os.path.basename(key.rstrip("/\\"))
                    )
                ]
            if not sessions:
                print("No sessions found.")
                return
            has_workspaces = bool(workspace_filter) or any(
                _workspace_key(session) for session in sessions
            )
            has_titles = any(s.get("title") for s in sessions)
            if has_workspaces:
                if has_titles:
                    print(f"{'Title':<28} {'Workspace':<18} {'Last Active':<13} {'ID'}")
                    print("─" * 110)
                else:
                    print(f"{'Preview':<38} {'Workspace':<18} {'Last Active':<13} {'Src':<6} {'ID'}")
                    print("─" * 100)
                for session in sessions:
                    key = _workspace_key(session)
                    workspace = (
                        os.path.basename(key.rstrip("/\\")) or key if key else "—"
                    )[:16]
                    last_active = _relative_time(session.get("last_active"))
                    if has_titles:
                        title = (session.get("title") or "—")[:26]
                        print(f"{title:<28} {workspace:<18} {last_active:<13} {session['id']}")
                    else:
                        preview = session.get("preview", "")[:36]
                        print(f"{preview:<38} {workspace:<18} {last_active:<13} {session['source']:<6} {session['id']}")
                return
            if has_titles:
                print(f"{'Title':<32} {'Preview':<40} {'Last Active':<13} {'ID'}")
                print("─" * 110)
            else:
                print(f"{'Preview':<50} {'Last Active':<13} {'Src':<6} {'ID'}")
                print("─" * 95)
            for s in sessions:
                last_active = _relative_time(s.get("last_active"))
                preview = (
                    s.get("preview", "")[:38]
                    if has_titles
                    else s.get("preview", "")[:48]
                )
                if has_titles:
                    title = (s.get("title") or "—")[:30]
                    sid = s["id"]
                    print(f"{title:<32} {preview:<40} {last_active:<13} {sid}")
                else:
                    sid = s["id"]
                    print(f"{preview:<50} {last_active:<13} {s['source']:<6} {sid}")

        elif action == "export":
            if args.session_id:
                resolved_session_id = db.resolve_session_id(args.session_id)
                if not resolved_session_id:
                    print(f"Session '{args.session_id}' not found.")
                    return
                data = db.export_session(resolved_session_id)
                if not data:
                    print(f"Session '{args.session_id}' not found.")
                    return
                line = _json.dumps(data, ensure_ascii=False) + "\n"
                if args.output == "-":

                    sys.stdout.write(line)
                else:
                    with open(args.output, "w", encoding="utf-8") as f:
                        f.write(line)
                    print(f"Exported 1 session to {args.output}")
            else:
                sessions = db.export_all(source=args.source)
                if args.output == "-":

                    for s in sessions:
                        sys.stdout.write(_json.dumps(s, ensure_ascii=False) + "\n")
                else:
                    with open(args.output, "w", encoding="utf-8") as f:
                        for s in sessions:
                            f.write(_json.dumps(s, ensure_ascii=False) + "\n")
                    print(f"Exported {len(sessions)} sessions to {args.output}")

        elif action == "delete":
            resolved_session_id = db.resolve_session_id(args.session_id)
            if not resolved_session_id:
                print(f"Session '{args.session_id}' not found.")
                return
            if not args.yes:
                if not _confirm_prompt(
                    f"Delete session '{resolved_session_id}' and all its messages? [y/N] "
                ):
                    print("Cancelled.")
                    return
            sessions_dir = get_marlow_home() / "sessions"
            if db.delete_session(resolved_session_id, sessions_dir=sessions_dir):
                print(f"Deleted session '{resolved_session_id}'.")
            else:
                print(f"Session '{args.session_id}' not found.")

        elif action == "prune":
            days = args.older_than
            source_msg = f" from '{args.source}'" if args.source else ""
            if not args.yes:
                if not _confirm_prompt(
                    f"Delete all ended sessions older than {days} days{source_msg}? [y/N] "
                ):
                    print("Cancelled.")
                    return
            sessions_dir = get_marlow_home() / "sessions"
            count = db.prune_sessions(
                older_than_days=days, source=args.source, sessions_dir=sessions_dir
            )
            print(f"Pruned {count} session(s).")

        elif action == "rename":
            resolved_session_id = db.resolve_session_id(args.session_id)
            if not resolved_session_id:
                print(f"Session '{args.session_id}' not found.")
                return
            title = " ".join(args.title)
            try:
                if db.set_session_title(resolved_session_id, title):
                    print(f"Session '{resolved_session_id}' renamed to: {title}")
                else:
                    print(f"Session '{args.session_id}' not found.")
            except ValueError as e:
                print(f"Error: {e}")

        elif action == "browse":
            limit = getattr(args, "limit", 500) or 500
            source = getattr(args, "source", None)
            _browse_exclude = None if source else ["tool"]
            sessions = db.list_sessions_rich(
                source=source, exclude_sources=_browse_exclude, limit=limit
            )
            db.close()
            if not sessions:
                print("No sessions found.")
                return

            selected_id = _session_browse_picker(sessions)
            if not selected_id:
                print("Cancelled.")
                return

            print(f"Session: {selected_id}")
            print(f"Resume with: marlow chat --resume {selected_id} -q 'Your next message'")
            return

        elif action == "optimize":
            db_path = db.db_path
            before_mb = (
                os.path.getsize(db_path) / (1024 * 1024)
                if db_path.exists()
                else 0.0
            )
            print("Optimizing session store (FTS merge + VACUUM)…")
            try:
                # vacuum() merges FTS5 segments (optimize_fts) then VACUUMs,
                # and returns the number of indexes it merged.
                n = db.vacuum()
            except Exception as e:
                print(f"Error: optimization failed: {e}")
                db.close()
                return
            after_mb = (
                os.path.getsize(db_path) / (1024 * 1024)
                if db_path.exists()
                else 0.0
            )
            saved = before_mb - after_mb
            print(f"Optimized {n} FTS index(es).")
            print(
                f"Database size: {before_mb:.1f} MB -> {after_mb:.1f} MB "
                f"(reclaimed {saved:.1f} MB)"
            )

        elif action == "stats":
            total = db.session_count()
            msgs = db.message_count()
            print(f"Total sessions: {total}")
            print(f"Total messages: {msgs}")
            for src in ["cli", "telegram", "discord", "slack", "email", "feishu", "webhook"]:
                c = db.session_count(source=src)
                if c > 0:
                    print(f"  {src}: {c} sessions")
            db_path = db.db_path
            if db_path.exists():
                size_mb = os.path.getsize(db_path) / (1024 * 1024)
                print(f"Database size: {size_mb:.1f} MB")

        else:
            sessions_parser.print_help()

        db.close()

    sessions_parser.set_defaults(func=cmd_sessions)

    # =========================================================================
    # insights command
    # =========================================================================
    insights_parser = subparsers.add_parser(
        "insights",
        help="Show usage insights and analytics",
        description="Analyze session history to show token usage, costs, tool patterns, and activity trends",
    )
    insights_parser.add_argument(
        "--days", type=int, default=30, help="Number of days to analyze (default: 30)"
    )
    insights_parser.add_argument(
        "--source", help="Filter by platform (cli, telegram, discord, etc.)"
    )

    def cmd_insights(args):
        try:
            from marlow_state import SessionDB
            from agent.insights import InsightsEngine

            db = SessionDB()
            engine = InsightsEngine(db)
            report = engine.generate(days=args.days, source=args.source)
            print(engine.format_terminal(report))
            db.close()
        except Exception as e:
            print(f"Error generating insights: {e}")

    insights_parser.set_defaults(func=cmd_insights)

    # =========================================================================
    # version command
    # =========================================================================
    version_parser = subparsers.add_parser("version", help="Show version information")
    version_parser.set_defaults(func=cmd_version)

    # =========================================================================
    # update command
    # =========================================================================
    update_parser = subparsers.add_parser(
        "update",
        help="Update Marlow Agent to the latest version",
        description="Pull the latest changes from git and reinstall dependencies",
    )
    update_parser.add_argument(
        "--gateway",
        action="store_true",
        default=False,
        help="Gateway mode: use file-based IPC for prompts instead of stdin (used internally by /update)",
    )
    update_parser.add_argument(
        "--check",
        action="store_true",
        default=False,
        help="Check whether an update is available without installing anything",
    )
    update_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        help="Assume yes for interactive prompts (config migration, stash restore). API-key entry is skipped; run 'marlow config migrate' separately for those.",
    )
    update_parser.add_argument(
        "--branch",
        default=None,
        metavar="NAME",
        help=(
            "Update against this branch instead of the default (main). "
            "If the local checkout is on a different branch, marlow will "
            "switch to the requested branch first (auto-stashing any "
            "uncommitted changes)."
        ),
    )
    update_parser.set_defaults(func=cmd_update)

    # =========================================================================
    # uninstall command
    # =========================================================================
    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Uninstall Marlow Agent",
        description="Remove Marlow Agent from your system. Can keep configs/data for reinstall.",
    )
    uninstall_parser.add_argument(
        "--full",
        action="store_true",
        help="Full uninstall - remove everything including configs and data",
    )
    uninstall_parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompts"
    )
    uninstall_parser.set_defaults(func=cmd_uninstall)

    # =========================================================================
    # profile command
    # =========================================================================
    profile_parser = subparsers.add_parser(
        "profile",
        help="Manage profiles — multiple isolated Marlow instances",
    )
    profile_subparsers = profile_parser.add_subparsers(dest="profile_action")

    profile_subparsers.add_parser("list", help="List all profiles")
    profile_use = profile_subparsers.add_parser(
        "use", help="Set sticky default profile"
    )
    profile_use.add_argument("profile_name", help="Profile name (or 'default')")

    profile_create = profile_subparsers.add_parser(
        "create", help="Create a new profile"
    )
    profile_create.add_argument(
        "profile_name", help="Profile name (lowercase, alphanumeric)"
    )
    profile_create.add_argument(
        "--clone",
        action="store_true",
        help="Copy config.yaml, .env, SOUL.md from active profile",
    )
    profile_create.add_argument(
        "--clone-all",
        action="store_true",
        help="Full copy of active profile (all state)",
    )
    profile_create.add_argument(
        "--clone-from",
        metavar="SOURCE",
        help="Source profile to clone from (default: active)",
    )
    profile_create.add_argument(
        "--no-alias", action="store_true", help="Skip wrapper script creation"
    )
    profile_create.add_argument(
        "--no-skills",
        action="store_true",
        help="Opt out of bundled on-demand skills for this profile",
    )
    profile_create.add_argument(
        "--description",
        default=None,
        help="One- or two-sentence description of what this profile is good at. "
             "Skip and add later via `marlow profile describe`.",
    )

    profile_delete = profile_subparsers.add_parser("delete", help="Delete a profile")
    profile_delete.add_argument("profile_name", help="Profile to delete")
    profile_delete.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation prompt"
    )

    profile_describe = profile_subparsers.add_parser(
        "describe",
        help="Read or set a profile's description",
    )
    profile_describe.add_argument(
        "profile_name",
        nargs="?",
        default=None,
        help="Profile to describe (omit + use --all --auto to sweep)",
    )
    profile_describe.add_argument(
        "--text",
        default=None,
        help="Set description to this exact text (overwrites any existing description)",
    )
    profile_describe.add_argument(
        "--auto",
        action="store_true",
        help="Auto-generate description via the auxiliary LLM "
             "(uses auxiliary.profile_describer)",
    )
    profile_describe.add_argument(
        "--overwrite",
        action="store_true",
        help="With --auto, replace user-authored descriptions too (default: only "
             "fill in missing or previously-auto descriptions)",
    )
    profile_describe.add_argument(
        "--all",
        dest="all_missing",
        action="store_true",
        help="With --auto, run on every profile missing a description",
    )

    profile_show = profile_subparsers.add_parser("show", help="Show profile details")
    profile_show.add_argument("profile_name", help="Profile to show")

    profile_alias = profile_subparsers.add_parser(
        "alias", help="Manage wrapper scripts"
    )
    profile_alias.add_argument("profile_name", help="Profile name")
    profile_alias.add_argument(
        "--remove", action="store_true", help="Remove the wrapper script"
    )
    profile_alias.add_argument(
        "--name",
        dest="alias_name",
        metavar="NAME",
        help="Custom alias name (default: profile name)",
    )

    profile_rename = profile_subparsers.add_parser("rename", help="Rename a profile")
    profile_rename.add_argument("old_name", help="Current profile name")
    profile_rename.add_argument("new_name", help="New profile name")

    profile_export = profile_subparsers.add_parser(
        "export", help="Export a profile to archive"
    )
    profile_export.add_argument("profile_name", help="Profile to export")
    profile_export.add_argument(
        "-o", "--output", default=None, help="Output file (default: <name>.tar.gz)"
    )

    profile_import = profile_subparsers.add_parser(
        "import", help="Import a profile from archive"
    )
    profile_import.add_argument("archive", help="Path to .tar.gz archive")
    profile_import.add_argument(
        "--name",
        dest="import_name",
        metavar="NAME",
        help="Profile name (default: inferred from archive)",
    )

    # ---------- Distribution subcommands (issue #20456) ----------
    profile_install = profile_subparsers.add_parser(
        "install",
        help="Install a profile distribution from a git URL or local directory",
        description=(
            "Install a Marlow profile distribution. SOURCE can be a git URL "
            "(github.com/user/repo, https://..., git@...) or a local "
            "directory containing distribution.yaml at its root."
        ),
    )
    profile_install.add_argument(
        "source",
        help="Distribution source (git URL or local directory)",
    )
    profile_install.add_argument(
        "--name", dest="install_name", metavar="NAME",
        help="Override profile name (default: read from manifest)",
    )
    profile_install.add_argument(
        "--alias", action="store_true",
        help="Create a shell wrapper alias for the installed profile",
    )
    profile_install.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing profile of the same name (user data preserved)",
    )
    profile_install.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip manifest preview confirmation",
    )

    profile_update = profile_subparsers.add_parser(
        "update",
        help="Re-pull a distribution and apply updates (user data preserved)",
        description=(
            "Fetch the distribution from its recorded source and overwrite "
            "distribution-owned files (SOUL.md, skills/, cron/, mcp.json). "
            "User data (memories, sessions, auth, .env) is never touched. "
            "config.yaml is preserved unless --force-config is passed."
        ),
    )
    profile_update.add_argument("profile_name", help="Profile to update")
    profile_update.add_argument(
        "--force-config", action="store_true",
        help="Also overwrite config.yaml (normally preserved to keep user overrides)",
    )
    profile_update.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip confirmation",
    )

    profile_info = profile_subparsers.add_parser(
        "info",
        help="Show a profile's distribution manifest (version, requirements, source)",
    )
    profile_info.add_argument("profile_name", help="Profile to inspect")

    profile_parser.set_defaults(func=cmd_profile)

    # =========================================================================
    # completion command
    # =========================================================================
    completion_parser = subparsers.add_parser(
        "completion",
        help="Print shell completion script (bash, zsh, or fish)",
    )
    completion_parser.add_argument(
        "shell",
        nargs="?",
        default="bash",
        choices=["bash", "zsh", "fish"],
        help="Shell type (default: bash)",
    )
    completion_parser.set_defaults(func=lambda args: cmd_completion(args, parser))

    # =========================================================================
    # logs command
    # =========================================================================
    logs_parser = subparsers.add_parser(
        "logs",
        help="View and filter Marlow log files",
        description="View, tail, and filter agent.log / errors.log / gateway.log",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    marlow logs                    Show last 50 lines of agent.log
    marlow logs -f                 Follow agent.log in real time
    marlow logs errors             Show last 50 lines of errors.log
    marlow logs gateway -n 100     Show last 100 lines of gateway.log
    marlow logs --level WARNING    Only show WARNING and above
    marlow logs --session abc123   Filter by session ID
    marlow logs --component tools  Only show tool-related lines
    marlow logs --since 1h         Lines from the last hour
    marlow logs --since 30m -f     Follow, starting from 30 min ago
    marlow logs list               List available log files with sizes
""",
    )
    logs_parser.add_argument(
        "log_name",
        nargs="?",
        default="agent",
        help="Log to view: agent (default), errors, gateway, or 'list' to show available files",
    )
    logs_parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=50,
        help="Number of lines to show (default: 50)",
    )
    logs_parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Follow the log in real time (like tail -f)",
    )
    logs_parser.add_argument(
        "--level",
        metavar="LEVEL",
        help="Minimum log level to show (DEBUG, INFO, WARNING, ERROR)",
    )
    logs_parser.add_argument(
        "--session",
        metavar="ID",
        help="Filter lines containing this session ID substring",
    )
    logs_parser.add_argument(
        "--since",
        metavar="TIME",
        help="Show lines since TIME ago (e.g. 1h, 30m, 2d)",
    )
    logs_parser.add_argument(
        "--component",
        metavar="NAME",
        help="Filter by component: gateway, agent, tools, cli, cron",
    )
    logs_parser.set_defaults(func=cmd_logs)

    # =========================================================================
    # prompt-size command
    # =========================================================================
    prompt_size_parser = subparsers.add_parser(
        "prompt-size",
        help="Show a byte breakdown of the system prompt + tool schemas",
        description=(
            "Report the fixed prompt budget for a fresh session: system "
            "prompt total, skills index, memory, user profile, and tool-schema "
            "JSON. Runs offline (no API call)."
        ),
    )
    prompt_size_parser.add_argument(
        "--platform",
        default="cli",
        help="Platform to simulate (cli, telegram, discord, ...). Default: cli",
    )
    prompt_size_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the breakdown as JSON",
    )
    prompt_size_parser.set_defaults(func=cmd_prompt_size)

    # =========================================================================
    # Parse and execute
    # =========================================================================
    # Pre-process argv so unquoted multi-word session names after -c / -r
    # are merged into a single token before argparse sees them.
    # e.g. ``marlow -c Pokemon Agent Dev`` → ``marlow -c 'Pokemon Agent Dev'``
    _processed_argv = _coalesce_session_name_args(sys.argv[1:])

    # ── Defensive subparser routing (bpo-9338 workaround) ───────────
    # On some Python versions (notably <3.11), argparse fails to route
    # subcommand tokens when the parent parser has nargs='?' optional
    # arguments (--continue).  The symptom: "unrecognized arguments: model"
    # even though 'model' is a registered subcommand.
    #
    # Fix: when argv contains a token matching a known subcommand, set
    # subparsers.required=True to force deterministic routing.  If that
    # fails (e.g. 'marlow -c model' where 'model' is consumed as the
    # session name for --continue), fall back to the default behaviour.
    import io as _io

    _known_cmds = (
        set(subparsers.choices.keys()) if hasattr(subparsers, "choices") else set()
    )
    _has_cmd_token = any(
        t in _known_cmds for t in _processed_argv if not t.startswith("-")
    )

    if _has_cmd_token:
        subparsers.required = True
        _saved_stderr = sys.stderr
        try:
            sys.stderr = _io.StringIO()
            args = parser.parse_args(_processed_argv)
            sys.stderr = _saved_stderr
        except SystemExit as exc:
            sys.stderr = _saved_stderr
            # Help/version flags (exit code 0) already printed output —
            # re-raise immediately to avoid a second parse_args printing
            # the same help text again (#10230).
            if exc.code == 0:
                raise
            # Subcommand name was consumed as a flag value (e.g. -c model).
            # Fall back to optional subparsers so argparse handles it normally.
            subparsers.required = False
            args = parser.parse_args(_processed_argv)
    else:
        subparsers.required = False
        args = parser.parse_args(_processed_argv)

    # Handle --version flag
    if args.version:
        cmd_version(args)
        return

    if args.command is None and args.oneshot is None:
        parser.print_help()
        return

    if args.command == "chat" and not (args.query or args.image):
        chat_parser.error("provide a query with --query or an image with --image")

    from marlow_cli.query import _invocation_environment

    overrides = {}
    if args.command == "chat" or args.oneshot is not None:
        if args.ignore_user_config:
            overrides["MARLOW_IGNORE_USER_CONFIG"] = "1"
        if args.ignore_rules:
            overrides["MARLOW_IGNORE_RULES"] = "1"

    with _invocation_environment(overrides):
        # Discover Python plugins and register shell hooks once, before any
        # command that can fire lifecycle hooks.  Both are idempotent; gated
        # so introspection/management commands (marlow hooks list, cron
        # list, gateway status, mcp add, ...) don't pay discovery cost or
        # trigger consent prompts for hooks the user is still inspecting.
        if args.oneshot is not None:
            # Reserve stdout for the final answer even when plugins emit output
            # during startup.
            with redirect_stdout(sys.stderr):
                _prepare_agent_startup(args)

            # The script interface prints only the final response.
            from marlow_cli.oneshot import run_oneshot

            sys.exit(
                run_oneshot(
                    args.oneshot,
                    model=getattr(args, "model", None),
                    provider=getattr(args, "provider", None),
                    toolsets=getattr(args, "toolsets", None),
                    yolo=args.yolo,
                )
            )

        _prepare_agent_startup(args)

        # Execute the command
        if hasattr(args, "func"):
            args.func(args)
        else:
            parser.print_help()


if __name__ == "__main__":
    main()

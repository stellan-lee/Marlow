"""Dangerous command approval -- detection, prompting, and per-session state.

This module is the single source of truth for the dangerous command system:
- Pattern detection (DANGEROUS_PATTERNS, detect_dangerous_command)
- Per-session approval state (thread-safe, keyed by session_key)
- Approval prompting (CLI interactive + gateway async)
- Smart approval via auxiliary LLM (auto-approve low-risk commands)
- Permanent allowlist persistence (config.yaml)
"""

import contextvars
import html
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import uuid
from typing import Optional
from marlow_cli.config import cfg_get

from utils import env_var_enabled, is_truthy_value

logger = logging.getLogger(__name__)

# Freeze YOLO mode at module import time. Reading os.environ on every call
# would allow any skill running inside the process to set this variable and
# instantly bypass all approval checks — a prompt-injection escalation path.
_YOLO_MODE_FROZEN: bool = is_truthy_value(os.getenv("MARLOW_YOLO_MODE", ""))

# Per-thread/per-task gateway session identity.
# Gateway runs agent turns concurrently in executor threads, so reading a
# process-global env var for session identity is racy. Keep env fallback for
# legacy single-threaded callers, but prefer the context-local value when set.
_approval_session_key: contextvars.ContextVar[str] = contextvars.ContextVar(
    "approval_session_key",
    default="",
)
_super_admin_context: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "super_admin_approval_context",
    default=False,
)


def _fire_approval_hook(hook_name: str, **kwargs) -> None:
    """Invoke a plugin lifecycle hook for the approval system.

    Lazy-imports the plugin manager to avoid circular imports (approval.py is
    imported very early, long before plugins are discovered). Never raises --
    plugin errors are logged and swallowed.

    Only fires for the two approval-specific hooks in VALID_HOOKS:
    pre_approval_request, post_approval_response.
    """
    try:
        from marlow_cli.plugins import invoke_hook
    except Exception:
        # Plugin system not available in this execution context
        # (e.g. bare tool-only imports, minimal test environments).
        return
    try:
        invoke_hook(hook_name, **kwargs)
    except Exception as exc:
        # invoke_hook() already swallows per-callback errors, so reaching here
        # means the dispatch layer itself failed. Log and move on -- approval
        # flow is safety-critical, plugin observability is not.
        logger.debug("Approval hook %s dispatch failed: %s", hook_name, exc)



def set_current_session_key(session_key: str) -> contextvars.Token[str]:
    """Bind the active approval session key to the current context."""
    return _approval_session_key.set(session_key or "")


def reset_current_session_key(token: contextvars.Token[str]) -> None:
    """Restore the prior approval session key context."""
    _approval_session_key.reset(token)


def get_current_session_key(default: str = "default") -> str:
    """Return the active session key, preferring context-local state.

    Resolution order:
    1. approval-specific contextvars (set by gateway before agent.run)
    2. session_context contextvars (set by _set_session_env)
    3. os.environ fallback (CLI, cron, tests)
    """
    session_key = _approval_session_key.get()
    if session_key:
        return session_key
    from gateway.session_context import get_session_env
    return get_session_env("MARLOW_SESSION_KEY", default)


def set_super_admin_context(enabled: bool) -> contextvars.Token[bool]:
    """Bind exact-source super-admin authority to the current agent turn."""
    return _super_admin_context.set(bool(enabled))


def reset_super_admin_context(token: contextvars.Token[bool]) -> None:
    """Restore the prior super-admin authority context."""
    _super_admin_context.reset(token)


def is_super_admin_context() -> bool:
    """Return whether approvable actions in this turn are pre-authorized."""
    return _super_admin_context.get()


def _get_session_platform() -> str:
    """Return the current gateway platform from contextvars/env fallback."""
    try:
        from gateway.session_context import get_session_env

        return get_session_env("MARLOW_SESSION_PLATFORM", "") or ""
    except Exception:
        return os.getenv("MARLOW_SESSION_PLATFORM", "") or ""


def _is_gateway_approval_context() -> bool:
    """True when this call is inside a gateway/API session.

    Legacy gateway integrations set MARLOW_GATEWAY_SESSION in process env.
    Newer concurrent gateway paths bind MARLOW_SESSION_PLATFORM via
    contextvars so approval mode does not depend on process-global flags.

    Cron jobs are NEVER gateway-approval contexts even when they originate
    from a gateway platform (cron binds MARLOW_SESSION_PLATFORM via
    contextvars for delivery routing). Cron approvals are governed by
    ``approvals.cron_mode`` config, not interactive resolve — letting cron
    fall through to the gateway branch would submit a pending approval
    with no listener and block the job indefinitely.
    """
    if env_var_enabled("MARLOW_CRON_SESSION"):
        return False
    if env_var_enabled("MARLOW_GATEWAY_SESSION"):
        return True
    return bool(_get_session_platform())

# Sensitive write targets that should trigger approval even when referenced
# via shell expansions like $HOME or $MARLOW_HOME.
_SSH_SENSITIVE_PATH = r'(?:~|\$home|\$\{home\})/\.ssh(?:/|$)'
_MARLOW_ENV_PATH = (
    r'(?:~\/\.marlow/|'
    r'(?:\$home|\$\{home\})/\.marlow/|'
    r'(?:\$marlow_home|\$\{marlow_home\})/)'
    r'\.env\b'
)
# ~/.marlow/config.yaml IS the security policy: approvals.mode, yolo, and the
# permanent-approval allowlist live here, and the config cache is mtime-keyed
# so a write takes effect mid-session (the agent could flip approvals.mode=off
# and immediately bypass the gate). Pair the write_file/patch deny (file_tools
# _check_sensitive_path) with terminal-side coverage so `sed -i`, `tee`, `>`,
# `cp`, etc. targeting it are gated too — otherwise the deny is unpaired
# theater. Mirrors _MARLOW_ENV_PATH; matches the MARLOW_HOME override form as
# well as ~/.marlow/.
_MARLOW_CONFIG_PATH = (
    r'(?:~\/\.marlow/|'
    r'(?:\$home|\$\{home\})/\.marlow/|'
    r'(?:\$marlow_home|\$\{marlow_home\})/)'
    r'config\.yaml\b'
)
_PROJECT_ENV_PATH = r'(?:(?:/|\.{1,2}/)?(?:[^\s/"\'`]+/)*\.env(?:\.[^/\s"\'`]+)*)'
_PROJECT_CONFIG_PATH = r'(?:(?:/|\.{1,2}/)?(?:[^\s/"\'`]+/)*config\.yaml)'
_SHELL_RC_FILES = (
    r'(?:~|\$home|\$\{home\})/\.'
    r'(?:bashrc|zshrc|profile|bash_profile|zprofile)\b'
)
_CREDENTIAL_FILES = (
    r'(?:~|\$home|\$\{home\})/\.'
    r'(?:netrc|pgpass|npmrc|pypirc)\b'
)
# macOS: /etc, /var, /tmp, /home are symlinks to /private/{etc,var,tmp,home}.
# A command written to target /private/etc/sudoers works identically to
# /etc/sudoers on macOS but bypasses a plain "/etc/" pattern check. Match
# both forms. Inspired by Claude Code 2.1.113's "dangerous path protection".
_MACOS_PRIVATE_SYSTEM_PATH = r'/private/(?:etc|var|tmp|home)/'
# System-config paths that should trigger approval for any write/edit,
# collapsing /etc, its macOS /private/etc mirror, and /etc/sudoers.d/ into
# one shared fragment so new DANGEROUS_PATTERNS stay consistent.
_SYSTEM_CONFIG_PATH = (
    rf'(?:/etc/|{_MACOS_PRIVATE_SYSTEM_PATH})'
)
_SENSITIVE_WRITE_TARGET = (
    rf'(?:{_SYSTEM_CONFIG_PATH}|/dev/sd|'
    rf'{_SSH_SENSITIVE_PATH}|'
    rf'{_MARLOW_ENV_PATH}|'
    rf'{_MARLOW_CONFIG_PATH}|'
    rf'{_SHELL_RC_FILES}|'
    rf'{_CREDENTIAL_FILES})'
)
_PROJECT_SENSITIVE_WRITE_TARGET = rf'(?:{_PROJECT_ENV_PATH}|{_PROJECT_CONFIG_PATH})'
_COMMAND_TAIL = r'(?:\s*(?:&&|\|\||;).*)?$'

# =========================================================================
# Hardline (unconditional) blocklist
# =========================================================================
#
# Commands so catastrophic they should NEVER run via the agent, regardless
# of --yolo, /yolo, approvals.mode=off, or cron approve mode.  This is a
# floor below yolo: opting into yolo is the user trusting the agent with
# their files and services, not trusting it to wipe the disk or power the
# box off.
#
# Hardline only applies to environments that can actually damage the host
# (local, ssh, container-host cron). The Docker backend bypasses the layer
# because nothing they do can touch the host, so we leave that behavior
# alone.
#
# The list is deliberately tiny — only things with no recovery path:
# filesystem destruction rooted at /, raw block device overwrites, kernel
# shutdown/reboot, and denial-of-service commands that take the host down.
# Recoverable-but-costly operations (git reset --hard, rm -rf /tmp/x,
# chmod -R 777, curl|sh) stay in DANGEROUS_PATTERNS where yolo can pass
# them through — that's what yolo is for.
#
# Inspired by Mercury Agent's permission-hardened blocklist
# (https://github.com/cosmicstack-labs/mercury-agent).

# Regex fragment matching the *start* of a command (i.e. positions where
# a shell would begin parsing a new command).  Used by shutdown/reboot
# patterns so they don't fire on "echo reboot" or "grep 'shutdown' log".
# Matches: start of string, after command separators (; && || | newline),
# after subshell openers ( `$(` or backtick ), optionally consuming
# leading wrapper commands (sudo, env VAR=VAL, exec, nohup, setsid).
_CMDPOS = (
    r'(?:^|[;&|\n`]|\$\()'         # start position
    r'\s*'                          # optional whitespace
    r'(?:sudo\s+(?:-[^\s]+\s+)*)?'  # optional sudo with flags
    r'(?:env\s+(?:\w+=\S*\s+)*)?'   # optional env with VAR=VAL pairs
    r'(?:(?:exec|nohup|setsid|time)\s+)*'  # optional wrapper commands
    r'\s*'
)

HARDLINE_PATTERNS = [
    # rm recursive targeting the root filesystem or protected roots
    (r'\brm\s+(-[^\s]*\s+)*(/|/\*|/ \*)(\s|$)', "recursive delete of root filesystem"),
    (r'\brm\s+(-[^\s]*\s+)*(/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|/var|/var/\*|/bin|/bin/\*|/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*)(\s|$)', "recursive delete of system directory"),
    (r'\brm\s+(-[^\s]*\s+)*(~|\$HOME)(/?|/\*)?(\s|$)', "recursive delete of home directory"),
    # Filesystem format
    (r'\bmkfs(\.[a-z0-9]+)?\b', "format filesystem (mkfs)"),
    # Raw block device overwrites (dd + redirection)
    (r'\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*', "dd to raw block device"),
    (r'>\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b', "redirect to raw block device"),
    # Fork bomb (classic shell form)
    (r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:', "fork bomb"),
    # Kill every process on the system
    (r'\bkill\s+(-[^\s]+\s+)*-1\b', "kill all processes"),
    # System shutdown / reboot — anchor to command position (start of line,
    # after a command separator, or after sudo/env wrappers) so we don't
    # false-positive on "echo reboot" or "grep 'shutdown' logs".
    # _CMDPOS matches start-of-command positions.
    (_CMDPOS + r'(shutdown|reboot|halt|poweroff)\b', "system shutdown/reboot"),
    (_CMDPOS + r'init\s+[06]\b', "init 0/6 (shutdown/reboot)"),
    (_CMDPOS + r'systemctl\s+(poweroff|reboot|halt|kexec)\b', "systemctl poweroff/reboot"),
    (_CMDPOS + r'telinit\s+[06]\b', "telinit 0/6 (shutdown/reboot)"),
]

# Pre-compiled variant used by the hot-path matcher. Building these at module
# load eliminates the ~2.6 ms cold-cache re.compile fan-out on the first
# terminal() call per process (12 HARDLINE + 47 DANGEROUS patterns, each
# potentially evicted from Python's 512-entry ``re._cache`` by unrelated
# regex work elsewhere in the agent). DANGEROUS_PATTERNS_COMPILED is built
# at the end of this module after DANGEROUS_PATTERNS is defined.
_RE_FLAGS = re.IGNORECASE | re.DOTALL
HARDLINE_PATTERNS_COMPILED = [
    (re.compile(pattern, _RE_FLAGS), description)
    for pattern, description in HARDLINE_PATTERNS
]


# =========================================================================
# Sudo stdin guard — block password guessing via "sudo -S"
# =========================================================================
# When SUDO_PASSWORD is not configured, any explicit "sudo -S" in the
# command is the LLM piping a guessed password via stdin.  This is a
# brute-force attack vector: the model iterates through candidate
# passwords, inspects sudo's "Sorry, try again" output, and refines.
# Treat this as an unconditional block — there is never a legitimate
# reason for the agent to pipe passwords to sudo -S when no password
# has been configured.
_SUDO_STDIN_RE = re.compile(
    r'(?:^|[;&|`\n]|&&|\|\||\$\()\s*sudo\s+-S\b',
    re.IGNORECASE)


def _check_sudo_stdin_guard(command: str) -> tuple:
    """Detect ``sudo -S`` (stdin password) without configured SUDO_PASSWORD.

    When SUDO_PASSWORD is set, ``_transform_sudo_command`` injects ``-S``
    internally — that path is legitimate and handled elsewhere.  This guard
    only fires when SUDO_PASSWORD is *not* set, meaning the LLM explicitly
    wrote ``sudo -S`` to pipe a guessed password.

    Returns:
        (is_blocked: bool, description: str | None)
    """
    if "SUDO_PASSWORD" in os.environ:
        return (False, None)
    normalized = _normalize_command_for_detection(command).lower()
    if _SUDO_STDIN_RE.search(normalized):
        return (True, "sudo password guessing via stdin (sudo -S)")
    return (False, None)


def detect_hardline_command(command: str) -> tuple:
    """Check if a command matches the unconditional hardline blocklist.

    Returns:
        (is_hardline, description) or (False, None)
    """
    normalized = _normalize_command_for_detection(command).lower()
    for pattern_re, description in HARDLINE_PATTERNS_COMPILED:
        if pattern_re.search(normalized):
            return (True, description)
    return (False, None)


def _hardline_block_result(description: str) -> dict:
    """Build the standard block result for a hardline match."""
    return {
        "approved": False,
        "hardline": True,
        "message": (
            f"BLOCKED (hardline): {description}. "
            "This command is on the unconditional blocklist and cannot "
            "be executed via the agent — not even with --yolo, /yolo, "
            "approvals.mode=off, or cron approve mode. If you genuinely "
            "need to run it, run it yourself in a terminal outside the "
            "agent."
        ),
    }


def _sudo_stdin_block_result(description: str) -> dict:
    """Build the standard block result for sudo stdin guard."""
    return {
        "approved": False,
        "message": (
            f"BLOCKED: {description}. "
            "Do not pipe passwords to 'sudo -S' — this is a brute-force "
            "attack vector. Set SUDO_PASSWORD in your .env file if the "
            "agent needs passwordless sudo, or run the sudo command "
            "manually in your own terminal."
        ),
    }


# =========================================================================
# Dangerous command patterns
# =========================================================================

DANGEROUS_PATTERNS = [
    (r'\brm\s+(-[^\s]*\s+)*/', "delete in root path"),
    (r'\brm\s+-[^\s]*r', "recursive delete"),
    (r'\brm\s+--recursive\b', "recursive delete (long flag)"),
    (r'\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b', "world/other-writable permissions"),
    (r'\bchmod\s+--recursive\b.*(777|666|o\+[rwx]*w|a\+[rwx]*w)', "recursive world/other-writable (long flag)"),
    (r'\bchown\s+(-[^\s]*)?R\s+root', "recursive chown to root"),
    (r'\bchown\s+--recursive\b.*root', "recursive chown to root (long flag)"),
    (r'\bmkfs\b', "format filesystem"),
    (r'\bdd\s+.*if=', "disk copy"),
    (r'>\s*/dev/sd', "write to block device"),
    (r'\bDROP\s+(TABLE|DATABASE)\b', "SQL DROP"),
    # Use [^\n]* instead of .* so DOTALL mode does not cause a WHERE clause on the
    # *next* line to satisfy the negative lookahead, silently allowing DELETE without WHERE.
    (r'\bDELETE\s+FROM\b(?![^\n]*\bWHERE\b)', "SQL DELETE without WHERE"),
    (r'\bTRUNCATE\s+(TABLE)?\s*\w', "SQL TRUNCATE"),
    (rf'>\s*{_SYSTEM_CONFIG_PATH}', "overwrite system config"),
    (r'\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b', "stop/restart system service"),
    (r'\bkill\s+-9\s+-1\b', "kill all processes"),
    (r'\bpkill\s+-9\b', "force kill processes"),
    # killall with SIGKILL (parallel to pkill -9). Catches -9 / -KILL /
    # -s KILL / -SIGKILL forms, and also `killall -r <regex>` broad sweeps
    # that can wipe out unrelated processes by accident.
    # Inspired by Claude Code 2.1.113 expanded deny rules.
    (r'\bkillall\s+(-[^\s]*\s+)*-(9|KILL|SIGKILL)\b', "force kill processes (killall -KILL)"),
    (r'\bkillall\s+(-[^\s]*\s+)*-s\s+(KILL|SIGKILL|9)\b', "force kill processes (killall -s KILL)"),
    (r'\bkillall\s+(-[^\s]*\s+)*-r\b', "kill processes by regex (killall -r)"),
    (r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:', "fork bomb"),
    # Any shell invocation via -c or combined flags like -lc, -ic, etc.
    (r'\b(bash|sh|zsh|ksh)\s+-[^\s]*c(\s+|$)', "shell command via -c/-lc flag"),
    (r'\b(python[23]?|perl|ruby|node)\s+-[ec]\s+', "script execution via -e/-c flag"),
    (r'\b(curl|wget)\b.*\|\s*(?:[/\w]*/)?(?:ba)?sh(?:\s|$|-c)', "pipe remote content to shell"),
    (r'\b(bash|sh|zsh|ksh)\s+<\s*<?\s*\(\s*(curl|wget)\b', "execute remote script via process substitution"),
    (rf'\btee\b.*["\']?{_SENSITIVE_WRITE_TARGET}', "overwrite system file via tee"),
    (rf'>>?\s*["\']?{_SENSITIVE_WRITE_TARGET}', "overwrite system file via redirection"),
    (rf'\btee\b.*["\']?{_PROJECT_SENSITIVE_WRITE_TARGET}["\']?{_COMMAND_TAIL}', "overwrite project env/config via tee"),
    (rf'>>?\s*["\']?{_PROJECT_SENSITIVE_WRITE_TARGET}["\']?{_COMMAND_TAIL}', "overwrite project env/config via redirection"),
    (r'\bxargs\s+.*\brm\b', "xargs with rm"),
    # find -exec rm / -execdir rm — the -execdir variant (same semantics,
    # runs in the directory of each match) was previously missed. Claude
    # Code 2.1.113 tightened their equivalent find rule to stop auto-
    # approving -exec / -delete flags.
    (r'\bfind\b.*-exec(?:dir)?\s+(/\S*/)?rm\b', "find -exec/-execdir rm"),
    (r'\bfind\b.*-delete\b', "find -delete"),
    # Gateway lifecycle protection: prevent the agent from killing its own
    # gateway process.  These commands trigger a gateway restart/stop that
    # terminates all running agents mid-work.
    (r'\bmarlow\s+gateway\s+(stop|restart)\b', "stop/restart marlow gateway (kills running agents)"),
    (r'\bmarlow\s+update\b', "marlow update (restarts gateway, kills running agents)"),
    # Docker container lifecycle — any user with docker.sock mounted (a common
    # Docker Compose pattern) gives the agent the ability to restart/stop/kill
    # containers without approval.  These are agent-initiated lifecycle operations
    # that should always require user consent, just like `marlow gateway restart`
    # already does for the gateway process.
    (r'\bdocker\s+compose\s+(restart|stop|kill|down)\b', "docker compose restart/stop/kill/down (container lifecycle)"),
    (r'\bdocker\s+(restart|stop|kill)\b', "docker restart/stop/kill (container lifecycle)"),
    # Gateway protection: never start gateway outside systemd management
    (r'gateway\s+run\b.*(&\s*$|&\s*;|\bdisown\b|\bsetsid\b)', "start gateway outside systemd (use 'systemctl --user restart marlow-gateway')"),
    (r'\bnohup\b.*gateway\s+run\b', "start gateway outside systemd (use 'systemctl --user restart marlow-gateway')"),
    # Self-termination protection: prevent agent from killing its own process
    (r'\b(pkill|killall)\b.*\b(marlow|gateway|cli\.py)\b', "kill marlow/gateway process (self-termination)"),
    # Self-termination via kill + command substitution (pgrep/pidof).
    # The name-based pattern above catches `pkill marlow` but not
    # `kill -9 $(pgrep -f marlow)` because the substitution is opaque
    # to regex at detection time. Catch the structural pattern instead.
    (r'\bkill\b.*\$\(\s*pgrep\b', "kill process via pgrep expansion (self-termination)"),
    (r'\bkill\b.*`\s*pgrep\b', "kill process via backtick pgrep expansion (self-termination)"),
    # File copy/move/edit into sensitive system paths (/etc/ and macOS
    # /private/etc/ mirror).
    (rf'\b(cp|mv|install)\b.*\s{_SYSTEM_CONFIG_PATH}', "copy/move file into system config path"),
    (rf'\b(cp|mv|install)\b.*\s["\']?{_PROJECT_SENSITIVE_WRITE_TARGET}["\']?{_COMMAND_TAIL}', "overwrite project env/config file"),
    (rf'\bsed\s+-[^\s]*i.*\s{_SYSTEM_CONFIG_PATH}', "in-place edit of system config"),
    (rf'\bsed\s+--in-place\b.*\s{_SYSTEM_CONFIG_PATH}', "in-place edit of system config (long flag)"),
    # In-place edit of a Marlow-managed security file (~/.marlow/config.yaml or
    # .env). sed -i bypasses the redirection/tee patterns above because it
    # mutates the file directly. Pairs the file_tools write_file/patch deny so
    # the terminal side is not an open door. See #14639.
    (rf'\bsed\s+-[^\s]*i.*(?:{_MARLOW_CONFIG_PATH}|{_MARLOW_ENV_PATH})', "in-place edit of Marlow config/env"),
    (rf'\bsed\s+--in-place\b.*(?:{_MARLOW_CONFIG_PATH}|{_MARLOW_ENV_PATH})', "in-place edit of Marlow config/env (long flag)"),
    # Script execution via heredoc — bypasses the -e/-c flag patterns above.
    # `python3 << 'EOF'` feeds arbitrary code via stdin without -c/-e flags.
    (r'\b(python[23]?|perl|ruby|node)\s+<<', "script execution via heredoc"),
    # Git destructive operations that can lose uncommitted work or rewrite
    # shared history. Not captured by rm/chmod/etc patterns.
    (r'\bgit\s+reset\s+--hard\b', "git reset --hard (destroys uncommitted changes)"),
    (r'\bgit\s+push\b.*--force\b', "git force push (rewrites remote history)"),
    (r'\bgit\s+push\b.*-f\b', "git force push short flag (rewrites remote history)"),
    (r'\bgit\s+clean\s+-[^\s]*f', "git clean with force (deletes untracked files)"),
    (r'\bgit\s+branch\s+-D\b', "git branch force delete"),
    # Script execution after chmod +x — catches the two-step pattern where
    # a script is first made executable then immediately run. The script
    # content may contain dangerous commands that individual patterns miss.
    (r'\bchmod\s+\+x\b.*[;&|]+\s*\./', "chmod +x followed by immediate execution"),
    # Sudo with stdin / askpass / shell / list-privs flags. An LLM-driven
    # agent has no TTY, so sudo invocations that succeed without human
    # interaction are those reading the password from stdin (-S/--stdin)
    # or via an askpass helper (-A/--askpass). The shell-launch (-s) and
    # list-privileges (-a) flags are also gated since they are
    # privilege-relevant invocations the agent can chain after acquiring
    # the password (e.g. read SUDO_PASSWORD from .env -> sudo -S -s ->
    # root shell). Plain `sudo cmd` (no flag) is TTY-bound and excluded.
    # `_normalize_command_for_detection` lowercases input before pattern
    # matching, so case variants of S/s and A/a collapse — both forms
    # are gated below. Lazy `[^;|&\n]*?` allows flag arguments (e.g.
    # `sudo -u root -S whoami`) without spanning command separators. See
    # #17873 category 4.
    (r'\bsudo\b[^;|&\n]*?\s+(?:-s\b|--stdin\b|-a\b|--askpass\b)',
     "sudo with privilege flag (stdin/askpass/shell/list)"),
    # Combined short-flag form: -nS, -ns, -sa, -las — sudo flags packed
    # into a single -X token. Catches the same threat class.
    (r'\bsudo\b[^;|&\n]*?\s+-[a-z]*[sa][a-z]*\b',
     "sudo with combined-flag privilege escalation"),
]


# Pre-compiled variant (same rationale as HARDLINE_PATTERNS_COMPILED above).
DANGEROUS_PATTERNS_COMPILED = [
    (re.compile(pattern, _RE_FLAGS), description)
    for pattern, description in DANGEROUS_PATTERNS
]


def _legacy_pattern_key(pattern: str) -> str:
    """Reproduce the old regex-derived approval key for backwards compatibility."""
    return pattern.split(r'\b')[1] if r'\b' in pattern else pattern[:20]


_PATTERN_KEY_ALIASES: dict[str, set[str]] = {}
for _pattern, _description in DANGEROUS_PATTERNS:
    _legacy_key = _legacy_pattern_key(_pattern)
    _canonical_key = _description
    _PATTERN_KEY_ALIASES.setdefault(_canonical_key, set()).update({_canonical_key, _legacy_key})
    _PATTERN_KEY_ALIASES.setdefault(_legacy_key, set()).update({_legacy_key, _canonical_key})


def _approval_key_aliases(pattern_key: str) -> set[str]:
    """Return all approval keys that should match this pattern.

    New approvals use the human-readable description string, but older
    command_allowlist entries and session approvals may still contain the
    historical regex-derived key.
    """
    return _PATTERN_KEY_ALIASES.get(pattern_key, {pattern_key})


# =========================================================================
# Detection
# =========================================================================

def _normalize_command_for_detection(command: str) -> str:
    """Normalize a command string before dangerous-pattern matching.

    Strips ANSI escape sequences (full ECMA-48 via tools.ansi_strip),
    null bytes, and normalizes Unicode fullwidth characters so that
    obfuscation techniques cannot bypass the pattern-based detection.
    """
    from tools.ansi_strip import strip_ansi

    # Strip all ANSI escape sequences (CSI, OSC, DCS, 8-bit C1, etc.)
    command = strip_ansi(command)
    # Strip null bytes
    command = command.replace('\x00', '')
    # Normalize Unicode (fullwidth Latin, halfwidth Katakana, etc.)
    command = unicodedata.normalize('NFKC', command)
    return command


def detect_dangerous_command(command: str) -> tuple:
    """Check if a command matches any dangerous patterns.

    Returns:
        (is_dangerous, pattern_key, description) or (False, None, None)
    """
    command_lower = _normalize_command_for_detection(command).lower()
    for pattern_re, description in DANGEROUS_PATTERNS_COMPILED:
        if pattern_re.search(command_lower):
            pattern_key = description
            return (True, pattern_key, description)
    return (False, None, None)


# =========================================================================
# Per-session approval state (thread-safe)
# =========================================================================

_lock = threading.Lock()
_pending: dict[str, dict] = {}
_session_approved: dict[str, set] = {}
_session_yolo: set[str] = set()
_permanent_approved: set = set()

# Administrator-approved terminal plans are deliberately narrower than the
# legacy pattern grants above.  A grant is process-local, session-bound,
# short-lived, and contains a consumable multiset of exact command digests.
# Restarting the gateway therefore invalidates every outstanding grant.
_INTENT_GRANT_TTL_SECONDS = 15 * 60
_MAX_INTENT_PLAN_COMMANDS = 12
_MAX_INTENT_PLAN_COMMAND_CHARS = 2400
_intent_grants: dict[str, dict] = {}


def _terminal_grant_digest(command: str, env_type: str, workdir: str) -> str:
    """Return the exact execution digest used by bounded intent grants."""
    from tools.action_intent import argument_digest

    return argument_digest(
        "terminal.intent-command",
        {
            "command": command,
            "environment": env_type,
            "workdir": workdir,
        },
    )


def _purpose_grant_digest(purpose: str) -> str:
    """Hash the raw purpose so grants bind intent without retaining secrets."""
    from tools.action_intent import argument_digest

    return argument_digest("terminal.intent-purpose", {"purpose": purpose})


def _normalize_terminal_plan(
    approval_plan,
    *,
    command: str,
    env_type: str,
    workdir: str,
) -> tuple[Optional[list[dict[str, str]]], Optional[str]]:
    """Validate a model-supplied exact command plan and include this command."""
    if approval_plan is None:
        return None, None
    if not isinstance(approval_plan, list) or not approval_plan:
        return None, "approval_plan must be a non-empty list of command objects"

    normalized: list[dict[str, str]] = []
    for index, item in enumerate(approval_plan, start=1):
        if not isinstance(item, dict):
            return None, f"approval_plan item {index} must be an object"
        planned_command = item.get("command")
        if not isinstance(planned_command, str) or not planned_command.strip():
            return None, f"approval_plan item {index} requires a non-empty command"
        planned_workdir = item.get("workdir", workdir)
        if planned_workdir is None:
            planned_workdir = workdir
        if not isinstance(planned_workdir, str):
            return None, f"approval_plan item {index} workdir must be a string"
        normalized.append({
            "command": planned_command,
            "environment": env_type,
            "workdir": planned_workdir,
        })

    current = {"command": command, "environment": env_type, "workdir": workdir}
    try:
        current_index = normalized.index(current)
    except ValueError:
        normalized.insert(0, current)
    else:
        normalized.insert(0, normalized.pop(current_index))

    if len(normalized) > _MAX_INTENT_PLAN_COMMANDS:
        return None, (
            f"approval_plan may contain at most {_MAX_INTENT_PLAN_COMMANDS} commands"
        )
    rendered_plan_chars = sum(
        len(html.escape(item["command"])) + len(html.escape(item["workdir"]))
        for item in normalized
    )
    if rendered_plan_chars > _MAX_INTENT_PLAN_COMMAND_CHARS:
        return None, (
            "approval_plan commands are too long for complete administrator review; "
            "split the workflow into smaller plans"
        )

    for index, item in enumerate(normalized, start=1):
        blocked, description = detect_hardline_command(item["command"])
        if blocked:
            return None, f"approval_plan item {index} is unconditionally blocked: {description}"
        sudo_blocked, sudo_description = _check_sudo_stdin_guard(item["command"])
        if sudo_blocked:
            return None, f"approval_plan item {index} is blocked: {sudo_description}"
    return normalized, None


def _create_terminal_intent_grant(
    *,
    session_key: str,
    purpose: str,
    plan: list[dict[str, str]],
    current_command: str,
    env_type: str,
    workdir: str,
) -> tuple[Optional[str], int]:
    """Create a grant for future plan entries, excluding the current call."""
    remaining: dict[str, int] = {}
    current_digest = _terminal_grant_digest(current_command, env_type, workdir)
    current_removed = False
    for item in plan:
        digest = _terminal_grant_digest(
            item["command"], item["environment"], item["workdir"]
        )
        if digest == current_digest and not current_removed:
            current_removed = True
            continue
        remaining[digest] = remaining.get(digest, 0) + 1

    count = sum(remaining.values())
    if not count:
        return None, 0

    grant_id = uuid.uuid4().hex
    purpose_digest = _purpose_grant_digest(purpose)
    with _lock:
        now = time.monotonic()
        for expired_id, grant in list(_intent_grants.items()):
            if grant["expires_at"] <= now:
                _intent_grants.pop(expired_id, None)
        _intent_grants[grant_id] = {
            "session_key": session_key,
            "purpose_digest": purpose_digest,
            "expires_at": now + _INTENT_GRANT_TTL_SECONDS,
            "remaining": remaining,
        }
    return grant_id, count


def _consume_terminal_intent_grant(
    *,
    grant_id: str,
    session_key: str,
    purpose: str,
    command: str,
    env_type: str,
    workdir: str,
) -> Optional[int]:
    """Atomically consume one exact planned command; return remaining count."""
    if not grant_id:
        return None
    now = time.monotonic()
    command_digest = _terminal_grant_digest(command, env_type, workdir)
    purpose_digest = _purpose_grant_digest(purpose)
    with _lock:
        grant = _intent_grants.get(grant_id)
        if grant is None:
            return None
        if grant["expires_at"] <= now:
            _intent_grants.pop(grant_id, None)
            return None
        if (
            grant["session_key"] != session_key
            or grant["purpose_digest"] != purpose_digest
        ):
            return None
        uses = grant["remaining"].get(command_digest, 0)
        if uses <= 0:
            return None
        if uses == 1:
            grant["remaining"].pop(command_digest, None)
        else:
            grant["remaining"][command_digest] = uses - 1
        remaining = sum(grant["remaining"].values())
        if not remaining:
            _intent_grants.pop(grant_id, None)
        return remaining

# =========================================================================
# Blocking gateway approval (mirrors CLI's synchronous input() flow)
# =========================================================================
# Per-session QUEUE of pending approvals.  Multiple threads (parallel
# subagents, execute_code RPC handlers) can block concurrently — each gets
# its own threading.Event.  /approve resolves the oldest, /approve all
# resolves every pending approval in the session.


class _ApprovalEntry:
    """One pending dangerous-command approval inside a gateway session."""
    __slots__ = ("event", "data", "request_id", "result")

    def __init__(self, data: dict):
        self.event = threading.Event()
        self.data = data          # command, description, pattern_keys, …
        self.request_id = str(data.get("request_id") or uuid.uuid4().hex)
        self.data["request_id"] = self.request_id
        self.result: Optional[str] = None  # "once"|"session"|"always"|"deny"


_gateway_queues: dict[str, list] = {}        # session_key → [_ApprovalEntry, …]
_gateway_notify_cbs: dict[str, object] = {}  # session_key → callable(approval_data)


def register_gateway_notify(session_key: str, cb) -> None:
    """Register a per-session callback for sending approval requests to the user.

    The callback signature is ``cb(approval_data: dict) -> None`` where
    *approval_data* contains ``command``, ``description``, and
    ``pattern_keys``.  The callback bridges sync→async (runs in the agent
    thread, must schedule the actual send on the event loop).
    """
    with _lock:
        _gateway_notify_cbs[session_key] = cb


def unregister_gateway_notify(session_key: str) -> None:
    """Unregister the per-session gateway approval callback.

    Signals ALL blocked threads for this session so they don't hang forever
    (e.g. when the agent run finishes or is interrupted).
    """
    with _lock:
        _gateway_notify_cbs.pop(session_key, None)
        entries = _gateway_queues.pop(session_key, [])
    for entry in entries:
        entry.event.set()


def resolve_gateway_approval(session_key: str, choice: str,
                             resolve_all: bool = False,
                             request_id: Optional[str] = None) -> int:
    """Called by the gateway's /approve or /deny handler to unblock
    waiting agent thread(s).

    When *request_id* is provided, only that exact approval is resolved. This
    is the safe path for interactive cards because multiple tool threads may
    ask for approval concurrently and cards can be clicked out of order.
    Otherwise, *resolve_all* resolves every pending approval in the session,
    while the default preserves the legacy FIFO ``/approve`` behavior.

    Returns the number of approvals resolved (0 means nothing was pending).
    """
    choice = str(choice or "").strip().lower()
    if choice not in {"once", "session", "always", "deny"}:
        logger.warning("Ignoring invalid gateway approval choice %r", choice)
        return 0

    with _lock:
        queue = _gateway_queues.get(session_key)
        if not queue:
            return 0

        def _accepts(entry: _ApprovalEntry) -> bool:
            # Admin-routed entries may only be resolved by the correlated
            # interactive control. This blocks stale legacy buttons or direct
            # FIFO calls from approving a newer privileged request.
            if not request_id and entry.data.get("request_scoped_only"):
                return False
            allowed = entry.data.get("allowed_choices")
            return not allowed or choice in {
                str(value).strip().lower()
                for value in allowed
                if str(value).strip()
            }

        if request_id:
            target = next(
                (entry for entry in queue if entry.request_id == request_id),
                None,
            )
            if target is None or not _accepts(target):
                return 0
            targets = [target]
            queue.remove(target)
        elif resolve_all:
            targets = [entry for entry in queue if _accepts(entry)]
            if not targets:
                return 0
            for target in targets:
                queue.remove(target)
        else:
            target = queue[0]
            if not _accepts(target):
                return 0
            targets = [queue.pop(0)]
        if not queue:
            _gateway_queues.pop(session_key, None)

    for entry in targets:
        entry.result = choice
        entry.event.set()
    return len(targets)


def has_blocking_approval(session_key: str) -> bool:
    """Check if a session has one or more blocking gateway approvals waiting."""
    with _lock:
        return bool(_gateway_queues.get(session_key))


def submit_pending(session_key: str, approval: dict):
    """Store a pending approval request for a session."""
    with _lock:
        _pending[session_key] = approval


def approve_session(session_key: str, pattern_key: str):
    """Approve a pattern for this session only."""
    with _lock:
        _session_approved.setdefault(session_key, set()).add(pattern_key)


def enable_session_yolo(session_key: str) -> None:
    """Enable YOLO bypass for a single session key."""
    if not session_key:
        return
    with _lock:
        _session_yolo.add(session_key)


def disable_session_yolo(session_key: str) -> None:
    """Disable YOLO bypass for a single session key."""
    if not session_key:
        return
    with _lock:
        _session_yolo.discard(session_key)


def clear_session(session_key: str) -> None:
    """Remove all approval and yolo state for a given session."""
    if not session_key:
        return
    with _lock:
        _session_approved.pop(session_key, None)
        _session_yolo.discard(session_key)
        _pending.pop(session_key, None)
        for grant_id, grant in list(_intent_grants.items()):
            if grant["session_key"] == session_key:
                _intent_grants.pop(grant_id, None)
        entries = _gateway_queues.pop(session_key, [])
    for entry in entries:
        # Session-boundary cleanup should cancel any blocked approval waits
        # immediately so the old run can unwind instead of idling until timeout.
        entry.result = "deny"
        entry.event.set()


def is_session_yolo_enabled(session_key: str) -> bool:
    """Return True when YOLO bypass is enabled for a specific session."""
    if not session_key:
        return False
    with _lock:
        return session_key in _session_yolo


def is_current_session_yolo_enabled() -> bool:
    """Return True when the active approval session has YOLO bypass enabled."""
    return is_session_yolo_enabled(get_current_session_key(default=""))


def is_approved(session_key: str, pattern_key: str) -> bool:
    """Check if a pattern is approved (session-scoped or permanent).

    Accept both the current canonical key and the legacy regex-derived key so
    existing command_allowlist entries continue to work after key migrations.
    """
    aliases = _approval_key_aliases(pattern_key)
    with _lock:
        if any(alias in _permanent_approved for alias in aliases):
            return True
        session_approvals = _session_approved.get(session_key, set())
        return any(alias in session_approvals for alias in aliases)


def approve_permanent(pattern_key: str):
    """Add a pattern to the permanent allowlist."""
    with _lock:
        _permanent_approved.add(pattern_key)


def load_permanent(patterns: set):
    """Bulk-load permanent allowlist entries from config."""
    with _lock:
        _permanent_approved.update(patterns)



# =========================================================================
# Config persistence for permanent allowlist
# =========================================================================

def load_permanent_allowlist() -> set:
    """Load permanently allowed command patterns from config.

    Also syncs them into the approval module so is_approved() works for
    patterns added via 'always' in a previous session.
    """
    try:
        from marlow_cli.config import load_config
        config = load_config()
        patterns = set(config.get("command_allowlist", []) or [])
        if patterns:
            load_permanent(patterns)
        return patterns
    except Exception as e:
        logger.warning("Failed to load permanent allowlist: %s", e)
        return set()


def save_permanent_allowlist(patterns: set):
    """Save permanently allowed command patterns to config."""
    try:
        from marlow_cli.config import load_config, save_config
        config = load_config()
        config["command_allowlist"] = list(patterns)
        save_config(config)
    except Exception as e:
        logger.warning("Could not save allowlist: %s", e)


# =========================================================================
# Approval prompting + orchestration
# =========================================================================

def prompt_dangerous_approval(command: str, description: str,
                              timeout_seconds: int | None = None,
                              allow_permanent: bool = True,
                              approval_callback=None) -> str:
    """Prompt the user to approve a dangerous command (CLI only).

    Args:
        allow_permanent: When False, hide the [a]lways option (used when
            tirith warnings are present, since broad permanent allowlisting
            is inappropriate for content-level security findings).
        approval_callback: Optional callback registered by the CLI for
            prompt_toolkit integration. Signature:
            (command, description, *, allow_permanent=True) -> str.

    Returns: 'once', 'session', 'always', or 'deny'
    """
    if timeout_seconds is None:
        timeout_seconds = _get_approval_timeout()

    if approval_callback is not None:
        try:
            return approval_callback(command, description,
                                     allow_permanent=allow_permanent)
        except Exception as e:
            logger.error("Approval callback failed: %s", e, exc_info=True)
            return "deny"

    # Fail-closed guard: if prompt_toolkit owns the terminal (interactive
    # CLI session) and no approval callback is registered on this thread,
    # the input() fallback below would spawn a daemon thread whose read
    # can never see Enter -- the user's keystrokes go to prompt_toolkit,
    # not input(), producing an invisible 60s deadlock (issue #15216).
    # Deny fast and log loudly instead so the caller can surface a real
    # error to the agent. Any thread that needs interactive approval must
    # install a callback via tools.terminal_tool.set_approval_callback()
    # before reaching this point (see delegate_tool.py, run_agent.py
    # _execute_tool_calls_concurrent / _spawn_background_review for the
    # established pattern).
    try:
        from prompt_toolkit.application.current import get_app_or_none
        if get_app_or_none() is not None:
            logger.warning(
                "Dangerous-command approval requested on a thread with no "
                "approval callback while prompt_toolkit is active; denying "
                "to avoid stdin deadlock. command=%r description=%r",
                command, description,
            )
            return "deny"
    except Exception:
        # prompt_toolkit not installed, or detection failed -- fall through
        # to the legacy input() path (safe in non-TUI contexts: scripts,
        # tests, sshd, etc.).
        pass

    os.environ["MARLOW_SPINNER_PAUSE"] = "1"
    try:
        # Resolve the active UI language once per prompt so we don't re-read
        # config/YAML inside the retry loop below.
        from agent.i18n import t
        while True:
            print()
            print(f"  {t('approval.dangerous_header', description=description)}")
            print(f"      {command}")
            print()
            if allow_permanent:
                print(t("approval.choose_long"))
            else:
                print(t("approval.choose_short"))
            print()
            sys.stdout.flush()

            result = {"choice": ""}

            def get_input():
                try:
                    prompt = t("approval.prompt_long") if allow_permanent else t("approval.prompt_short")
                    result["choice"] = input(prompt).strip().lower()
                except (EOFError, OSError):
                    result["choice"] = ""

            thread = threading.Thread(target=get_input, daemon=True)
            thread.start()
            thread.join(timeout=timeout_seconds)

            if thread.is_alive():
                print("\n" + t("approval.timeout"))
                return "deny"

            choice = result["choice"]
            if choice in {'o', 'once'}:
                print(t("approval.allowed_once"))
                return "once"
            elif choice in {'s', 'session'}:
                print(t("approval.allowed_session"))
                return "session"
            elif choice in {'a', 'always'}:
                if not allow_permanent:
                    print(t("approval.allowed_session"))
                    return "session"
                print(t("approval.allowed_always"))
                return "always"
            else:
                print(t("approval.denied"))
                return "deny"

    except (EOFError, KeyboardInterrupt):
        print("\n" + t("approval.cancelled"))
        return "deny"
    finally:
        if "MARLOW_SPINNER_PAUSE" in os.environ:
            del os.environ["MARLOW_SPINNER_PAUSE"]
        print()
        sys.stdout.flush()


def _normalize_approval_mode(mode) -> str:
    """Normalize approval mode values loaded from YAML/config.

    YAML 1.1 treats bare words like `off` as booleans, so a config entry like
    `approvals:\n  mode: off` is parsed as False unless quoted. Treat that as the
    intended string mode instead of falling back to manual approvals.
    """
    if isinstance(mode, bool):
        return "off" if mode is False else "manual"
    if isinstance(mode, str):
        normalized = mode.strip().lower()
        return normalized or "manual"
    return "manual"


def _get_approval_config() -> dict:
    """Read the approvals config block. Returns a dict with 'mode', 'timeout', etc."""
    try:
        from marlow_cli.config import load_config
        config = load_config()
        return config.get("approvals", {}) or {}
    except Exception as e:
        logger.warning("Failed to load approval config: %s", e)
        return {}


def _get_approval_mode() -> str:
    """Read the approval mode from config. Returns 'manual', 'smart', or 'off'."""
    mode = _get_approval_config().get("mode", "manual")
    return _normalize_approval_mode(mode)


def _is_admin_approval_enforced(approval_config: Optional[dict] = None) -> bool:
    """Return whether gateway approvals must use the configured admin route.

    Admin routing is a stricter security mode. In an active gateway session it
    takes precedence over YOLO, ``approvals.mode: off``, smart auto-approval,
    and previously persisted/session-scoped command grants; otherwise those
    settings could silently bypass the exact administrator configured to make
    the decision.
    """
    if not _is_gateway_approval_context():
        return False
    config = approval_config if isinstance(approval_config, dict) else _get_approval_config()
    admin = config.get("admin")
    return bool(isinstance(admin, dict) and is_truthy_value(admin.get("enabled")))


def is_admin_action_approval_enabled() -> bool:
    """Return whether the current dispatch must enforce action intents.

    This public wrapper keeps registry/dispatcher code out of approval's
    private configuration details while preserving the gateway-only boundary.
    """
    return is_super_admin_context() or _is_admin_approval_enforced()


def _get_approval_timeout() -> int:
    """Read the approval timeout from config. Defaults to 60 seconds."""
    try:
        return int(_get_approval_config().get("timeout", 60))
    except (ValueError, TypeError):
        return 60


def _get_cron_approval_mode() -> str:
    """Read the cron approval mode from config. Returns 'deny' or 'approve'."""
    try:
        from marlow_cli.config import load_config
        config = load_config()
        mode = str(cfg_get(config, "approvals", "cron_mode", default="deny")).lower().strip()
        if mode in {"approve", "off", "allow", "yes"}:
            return "approve"
        return "deny"
    except Exception:
        return "deny"


def _smart_approve(command: str, description: str) -> str:
    """Use the auxiliary LLM to assess risk and decide approval.

    Returns 'approve' if the LLM determines the command is safe,
    'deny' if genuinely dangerous, or 'escalate' if uncertain.

    Inspired by OpenAI Codex's Smart Approvals guardian subagent
    (openai/codex#13860).
    """
    try:
        from agent.auxiliary_client import call_llm

        prompt = f"""You are a security reviewer for an AI coding agent. A terminal command was flagged by pattern matching as potentially dangerous.

Command: {command}
Flagged reason: {description}

Assess the ACTUAL risk of this command. Many flagged commands are false positives — for example, `python -c "print('hello')"` is flagged as "script execution via -c flag" but is completely harmless.

Rules:
- APPROVE if the command is clearly safe (benign script execution, safe file operations, development tools, package installs, git operations, etc.)
- DENY if the command could genuinely damage the system (recursive delete of important paths, overwriting system files, fork bombs, wiping disks, dropping databases, etc.)
- ESCALATE if you're uncertain

Respond with exactly one word: APPROVE, DENY, or ESCALATE"""

        response = call_llm(
            task="approval",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=16,
        )

        answer = (response.choices[0].message.content or "").strip().upper()

        if answer == "APPROVE":
            return "approve"
        elif answer == "DENY":
            return "deny"
        else:
            return "escalate"

    except Exception as e:
        logger.debug("Smart approvals: LLM call failed (%s), escalating", e)
        return "escalate"


def check_dangerous_command(command: str, env_type: str,
                            approval_callback=None) -> dict:
    """Check if a command is dangerous and handle approval.

    This is the main entry point called by terminal_tool before executing
    any command. It orchestrates detection, session checks, and prompting.

    Args:
        command: The shell command to check.
        env_type: Terminal backend type ('local', 'ssh', 'docker', etc.).
        approval_callback: Optional CLI callback for interactive prompts.

    Returns:
        {"approved": True/False, "message": str or None, ...}
    """
    if env_type == "docker":
        return {"approved": True, "message": None}

    # Hardline floor: commands with no recovery path (rm -rf /, mkfs, dd
    # to raw device, shutdown/reboot, fork bomb, kill -1) are blocked
    # unconditionally, BEFORE the yolo bypass.  Opting into yolo is
    # trusting the agent with your files and services, not trusting it
    # to wipe the disk or power the box off.
    is_hardline, hardline_desc = detect_hardline_command(command)
    if is_hardline:
        logger.warning("Hardline block: %s (command: %s)", hardline_desc, command[:200])
        return _hardline_block_result(hardline_desc)

    if is_super_admin_context():
        logger.info(
            "Super-admin auto-approved dangerous-command guard: %s",
            command[:200],
        )
        return {
            "approved": True,
            "message": None,
            "super_admin_approved": True,
        }

    # --yolo: bypass all approval prompts unless exact-admin routing is the
    # active gateway security boundary.
    admin_enforced = _is_admin_approval_enforced()
    if not admin_enforced and (
        _YOLO_MODE_FROZEN or is_current_session_yolo_enabled()
    ):
        return {"approved": True, "message": None}

    is_dangerous, pattern_key, description = detect_dangerous_command(command)
    if not is_dangerous:
        return {"approved": True, "message": None}

    session_key = get_current_session_key()
    if not admin_enforced and is_approved(session_key, pattern_key):
        return {"approved": True, "message": None}

    is_cli = env_var_enabled("MARLOW_INTERACTIVE")
    is_gateway = _is_gateway_approval_context()

    if not is_cli and not is_gateway:
        # Cron sessions: respect cron_mode config
        if env_var_enabled("MARLOW_CRON_SESSION"):
            if _get_cron_approval_mode() == "deny":
                return {
                    "approved": False,
                    "message": (
                        f"BLOCKED: Command flagged as dangerous ({description}) "
                        "but cron jobs run without a user present to approve it. "
                        "Find an alternative approach that avoids this command. "
                        "To allow dangerous commands in cron jobs, set "
                        "approvals.cron_mode: approve in config.yaml."
                    ),
                }
        logger.warning(
            "AUTO-APPROVED dangerous command in non-interactive non-gateway context "
            "(pattern: %s): %s — set MARLOW_INTERACTIVE or MARLOW_GATEWAY_SESSION to require approval.",
            description, command[:200],
        )
        return {"approved": True, "message": None}

    if is_gateway or env_var_enabled("MARLOW_EXEC_ASK"):
        submit_pending(session_key, {
            "command": command,
            "pattern_key": pattern_key,
            "description": description,
        })
        return {
            "approved": False,
            "pattern_key": pattern_key,
            "status": "approval_required",
            "command": command,
            "description": description,
            "message": (
                f"⚠️ This command is potentially dangerous ({description}). "
                f"Asking the user for approval.\n\n**Command:**\n```\n{command}\n```"
            ),
        }

    choice = prompt_dangerous_approval(command, description,
                                       approval_callback=approval_callback)

    if choice == "deny":
        return {
            "approved": False,
            "message": f"BLOCKED: User denied this potentially dangerous command (matched '{description}' pattern). Do NOT retry this command - the user has explicitly rejected it.",
            "pattern_key": pattern_key,
            "description": description,
        }

    if choice == "session":
        approve_session(session_key, pattern_key)
    elif choice == "always":
        approve_session(session_key, pattern_key)
        approve_permanent(pattern_key)
        save_permanent_allowlist(_permanent_approved)

    return {"approved": True, "message": None}


# =========================================================================
# Combined pre-exec guard (tirith + dangerous command detection)
# =========================================================================

def _format_tirith_description(tirith_result: dict) -> str:
    """Build a human-readable description from tirith findings.

    Includes severity, title, and description for each finding so users
    can make an informed approval decision.
    """
    findings = tirith_result.get("findings") or []
    if not findings:
        summary = tirith_result.get("summary") or "security issue detected"
        return f"Security scan: {summary}"

    parts = []
    for f in findings:
        severity = f.get("severity", "")
        title = f.get("title", "")
        desc = f.get("description", "")
        if title and desc:
            parts.append(f"[{severity}] {title}: {desc}" if severity else f"{title}: {desc}")
        elif title:
            parts.append(f"[{severity}] {title}" if severity else title)
    if not parts:
        summary = tirith_result.get("summary") or "security issue detected"
        return f"Security scan: {summary}"

    return "Security scan — " + "; ".join(parts)


def _await_gateway_decision(session_key: str, notify_cb, approval_data: dict,
                            *, surface: str = "gateway") -> dict:
    """Enqueue *approval_data*, notify the user, and block the calling agent
    thread until the request is resolved or the gateway approval timeout
    elapses — firing pre/post approval hooks and cleaning up the queue entry.

    Shared by the terminal command guard (``check_all_command_guards``) and
    the execute_code guard (``check_execute_code_guard``) so the fiddly
    heartbeat-polling wait loop lives in one place.

    Returns ``{"resolved": bool, "choice": str|None}`` on completion, or
    ``{"resolved": False, "choice": None, "notify_failed": True}`` if the
    notify callback raised.  Persistence of an approved choice and building
    the final tool-facing result dict remain the caller's responsibility.
    """
    # Work with a private copy because the request id is transport metadata;
    # callers may reuse their input dict in logs or fallback results.
    approval_data = dict(approval_data)
    command = approval_data.get("command", "")
    description = approval_data.get("description", "")
    action_intent = approval_data.get("action_intent")
    primary_key = approval_data.get("pattern_key", "")
    all_keys = approval_data.get("pattern_keys", [primary_key])

    entry = _ApprovalEntry(approval_data)
    with _lock:
        _gateway_queues.setdefault(session_key, []).append(entry)

    def _drop_entry() -> None:
        with _lock:
            queue = _gateway_queues.get(session_key, [])
            if entry in queue:
                queue.remove(entry)
            if not queue:
                _gateway_queues.pop(session_key, None)

    # Notify plugins that an approval is being requested. Fires before the
    # gateway notify callback so observers get the event in real time.
    hook_context = {
        "command": command,
        "description": description,
        "pattern_key": primary_key,
        "pattern_keys": list(all_keys),
        "session_key": session_key,
        "surface": surface,
    }
    if action_intent is not None:
        hook_context["action_intent"] = action_intent
    _fire_approval_hook("pre_approval_request", **hook_context)

    # Notify the user (bridges sync agent thread → async gateway)
    try:
        notify_cb(entry.data)
    except Exception as exc:
        logger.warning("Gateway approval notify failed: %s", exc)
        _drop_entry()
        _resume_cb = getattr(notify_cb, "on_resolved", None)
        if callable(_resume_cb):
            try:
                _resume_cb()
            except Exception:
                logger.debug("Gateway approval resume callback failed", exc_info=True)
        return {
            "resolved": False,
            "choice": None,
            "request_id": entry.request_id,
            "notify_failed": True,
        }

    # Block until the user responds or timeout (default 5 min). Poll in short
    # slices so we can fire activity heartbeats every ~10s to the agent's
    # inactivity tracker — otherwise the gateway watchdog kills the agent
    # while the user is still responding. Mirrors _wait_for_process() cadence.
    timeout = _get_approval_config().get("gateway_timeout", 300)
    try:
        timeout = int(timeout)
    except (ValueError, TypeError):
        timeout = 300

    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:  # pragma: no cover
        touch_activity_if_due = None

    _now = time.monotonic()
    _deadline = _now + max(timeout, 0)
    _activity_state = {"last_touch": _now, "start": _now}
    resolved = False
    while True:
        _remaining = _deadline - time.monotonic()
        if _remaining <= 0:
            break
        if entry.event.wait(timeout=min(1.0, _remaining)):
            resolved = True
            break
        if touch_activity_if_due is not None:
            touch_activity_if_due(_activity_state, "waiting for user approval")

    _drop_entry()

    # The prompt may have been routed to an admin on another chat/platform.
    # Resume the originating chat's typing state from the core wait path so
    # every outcome (approve, decline, timeout, cancellation) is covered.
    _resume_cb = getattr(notify_cb, "on_resolved", None)
    if callable(_resume_cb):
        try:
            _resume_cb()
        except Exception:
            logger.debug("Gateway approval resume callback failed", exc_info=True)

    choice = entry.result
    # Normalize outcome for the post hook. Unresolved (timeout) and None both
    # mean the user never responded; report that explicitly so plugins can
    # distinguish timeout from explicit deny.
    _outcome = "timeout" if not resolved else (choice if choice else "timeout")
    _fire_approval_hook(
        "post_approval_response",
        **hook_context,
        choice=_outcome,
    )
    return {
        "resolved": resolved,
        "choice": choice,
        "request_id": entry.request_id,
    }


def request_action_intent_approval(intent) -> dict:
    """Approve one structured intent and execute only in the waiting caller.

    The gateway's registered approval callback owns routing and exact admin
    authorization. This primitive only enqueues the request and blocks the
    current tool thread until the configured administrator approves, declines,
    or the request times out. Missing gateway/admin routing fails closed.
    """
    from tools.action_intent import ActionIntent

    if not isinstance(intent, ActionIntent):
        return {
            "approved": False,
            "decision": "invalid",
            "message": "A valid structured action intent is required.",
        }

    intent_data = intent.to_dict()
    action = intent.summary()
    reason = intent.reason

    if is_super_admin_context():
        session_key = get_current_session_key(default="")
        hook_context = {
            "command": action,
            "description": reason,
            "pattern_key": f"action_intent:{intent.argument_digest}",
            "pattern_keys": [f"action_intent:{intent.argument_digest}"],
            "session_key": session_key,
            "surface": "super_admin_action_intent",
            "action_intent": intent_data,
        }
        _fire_approval_hook("pre_approval_request", **hook_context)
        _fire_approval_hook(
            "post_approval_response",
            **hook_context,
            choice="super_admin",
        )
        logger.info(
            "Super-admin auto-approved action intent in session %s: %s",
            session_key or "<unknown>",
            action,
        )
        return {
            "approved": True,
            "decision": "super_admin",
            "request_id": None,
            "message": "The configured super administrator pre-authorized this action.",
            "action_intent": intent_data,
        }

    session_key = get_current_session_key(default="")
    if not session_key:
        return {
            "approved": False,
            "decision": "unavailable",
            "message": "Admin approval is not available outside an active gateway session.",
        }

    with _lock:
        notify_cb = _gateway_notify_cbs.get(session_key)
    if notify_cb is None:
        return {
            "approved": False,
            "decision": "unavailable",
            "message": "No gateway approval route is registered for this session.",
        }

    decision = _await_gateway_decision(
        session_key,
        notify_cb,
        {
            "kind": "action_intent",
            "action": action,
            # Existing platform adapters still accept a parameter named
            # command. Keep this compatibility alias internal; the title and
            # payload identify it as a semantic action intent.
            "command": action,
            "description": f"{reason}\nImpact: {intent.impact}",
            "action_intent": intent_data,
            "pattern_key": f"action_intent:{intent.argument_digest}",
            "pattern_keys": [f"action_intent:{intent.argument_digest}"],
            "allowed_choices": ["once", "deny"],
            "request_scoped_only": True,
        },
        surface="gateway_action_intent",
    )
    request_id = decision.get("request_id")
    if decision.get("notify_failed"):
        return {
            "approved": False,
            "decision": "delivery_failed",
            "request_id": request_id,
            "message": "Admin approval request could not be delivered. Do not perform the action.",
        }
    if not decision.get("resolved"):
        return {
            "approved": False,
            "decision": "timeout",
            "request_id": request_id,
            "message": "Admin approval timed out. Silence is not consent; do not perform the action.",
        }
    if decision.get("choice") != "once":
        return {
            "approved": False,
            "decision": "declined",
            "request_id": request_id,
            "message": "The administrator declined the action. Do not perform it or retry through another tool.",
        }
    return {
        "approved": True,
        "decision": "approved",
        "request_id": request_id,
        "message": "The administrator approved this action once.",
        "action_intent": intent_data,
    }


def request_admin_approval(
    action: str,
    reason: str = "",
    *,
    action_type: str = "custom.action",
    operation: str = "",
    target: str = "",
    impact: str = "",
    parameters=None,
) -> dict:
    """Compatibility entry point for a model-described privileged action.

    Registered tools should use central ``action_intent`` metadata so approval
    and execution are inseparable. This function remains for external actions
    that do not have a registered executor.
    """
    from tools.action_intent import build_manual_action_intent

    try:
        intent = build_manual_action_intent(
            action,
            reason,
            action_type=action_type,
            operation=operation,
            target=target,
            impact=impact,
            parameters=parameters,
        )
    except (TypeError, ValueError) as exc:
        return {
            "approved": False,
            "decision": "invalid",
            "message": str(exc),
        }
    return request_action_intent_approval(intent)


def check_all_command_guards(
    command: str,
    env_type: str,
    approval_callback=None,
    *,
    purpose: str = "",
    workdir: str = "",
    approval_plan=None,
    intent_grant_id: str = "",
) -> dict:
    """Run all pre-exec security checks and return a single approval decision.

    Gathers findings from tirith and dangerous-command detection, then
    presents them as a single combined approval request. This prevents
    a gateway force=True replay from bypassing one check when only the
    other was shown to the user.
    """
    # Skip containers for both checks
    if env_type == "docker":
        return {"approved": True, "message": None}

    # Hardline floor: unconditional block for catastrophic commands
    # (rm -rf /, mkfs, dd to raw device, shutdown/reboot, fork bomb,
    # kill -1). Applies BEFORE yolo / mode=off / cron approve-mode so
    # no session-level setting can bypass it.
    is_hardline, hardline_desc = detect_hardline_command(command)
    if is_hardline:
        logger.warning("Hardline block: %s (command: %s)", hardline_desc, command[:200])
        return _hardline_block_result(hardline_desc)

    # == Sudo stdin guard ==
    # Like the hardline floor above, this is unconditional: there is never a
    # legitimate reason for the agent to pipe passwords to sudo -S when no
    # SUDO_PASSWORD has been configured.  This must fire BEFORE the yolo
    # check so even yolo/smart approval/mode=off cannot bypass it.
    is_sudo_guess, sudo_guess_desc = _check_sudo_stdin_guard(command)
    if is_sudo_guess:
        logger.warning("Sudo stdin guard block: %s (command: %s)",
                       sudo_guess_desc, command[:200])
        return _sudo_stdin_block_result(sudo_guess_desc)

    if is_super_admin_context():
        logger.info(
            "Super-admin auto-approved command guards: %s",
            command[:200],
        )
        return {
            "approved": True,
            "message": None,
            "super_admin_approved": True,
        }

    # Exact-admin gateway routing is stricter than YOLO/mode=off because those
    # settings would otherwise bypass the configured decision maker entirely.
    approval_config = _get_approval_config()
    approval_mode = _normalize_approval_mode(approval_config.get("mode", "manual"))
    admin_enforced = _is_admin_approval_enforced(approval_config)
    if not admin_enforced and (
        _YOLO_MODE_FROZEN
        or is_current_session_yolo_enabled()
        or approval_mode == "off"
    ):
        return {"approved": True, "message": None}

    is_cli = env_var_enabled("MARLOW_INTERACTIVE")
    is_gateway = _is_gateway_approval_context()
    is_ask = env_var_enabled("MARLOW_EXEC_ASK")
    session_key = get_current_session_key()

    normalized_plan = None
    if admin_enforced and approval_plan is not None:
        normalized_plan, plan_error = _normalize_terminal_plan(
            approval_plan,
            command=command,
            env_type=env_type,
            workdir=workdir,
        )
        if plan_error:
            return {
                "approved": False,
                "message": f"BLOCKED: Invalid administrator approval plan: {plan_error}.",
                "description": plan_error,
                "outcome": "invalid_approval_plan",
            }

    normalized_purpose = str(purpose or "").strip()
    if normalized_plan is not None and len(html.escape(normalized_purpose)) > 500:
        return {
            "approved": False,
            "message": (
                "BLOCKED: Invalid administrator approval plan: purpose may contain "
                "at most 500 characters."
            ),
            "description": "approval plan purpose is too long",
            "outcome": "invalid_approval_plan",
        }
    if admin_enforced and (normalized_plan is not None or intent_grant_id):
        if not normalized_purpose:
            return {
                "approved": False,
                "message": (
                    "BLOCKED: Administrator approval requires a clear terminal "
                    "purpose. Retry the terminal call with a concise user-facing "
                    "outcome in the purpose field; do not merely restate the command."
                ),
                "description": "terminal purpose is required for administrator approval",
                "outcome": "missing_purpose",
            }

    # A grant bypasses only repeat prompting. The hardline and sudo guards above
    # have already run, and exact session/purpose/command/environment/workdir
    # matching is consumed atomically before execution.
    if admin_enforced and intent_grant_id:
        remaining = _consume_terminal_intent_grant(
            grant_id=intent_grant_id,
            session_key=session_key,
            purpose=normalized_purpose,
            command=command,
            env_type=env_type,
            workdir=workdir,
        )
        if remaining is not None:
            return {
                "approved": True,
                "message": None,
                "intent_grant_used": True,
                "intent_grant_id": intent_grant_id,
                "intent_grant_remaining": remaining,
            }

    # Preserve the existing non-interactive behavior: outside CLI/gateway/ask
    # flows, we do not block on approvals and we skip external guard work.
    if not is_cli and not is_gateway and not is_ask:
        # Cron sessions: respect cron_mode config
        if env_var_enabled("MARLOW_CRON_SESSION"):
            if _get_cron_approval_mode() == "deny":
                # Run detection to get a description for the block message
                is_dangerous, _pk, description = detect_dangerous_command(command)
                if is_dangerous:
                    return {
                        "approved": False,
                        "message": (
                            f"BLOCKED: Command flagged as dangerous ({description}) "
                            "but cron jobs run without a user present to approve it. "
                            "Find an alternative approach that avoids this command. "
                            "To allow dangerous commands in cron jobs, set "
                            "approvals.cron_mode: approve in config.yaml."
                        ),
                    }
        return {"approved": True, "message": None}

    # --- Phase 1: Gather findings from both checks ---

    # Tirith check — wrapper guarantees no raise for expected failures.
    # Only catch ImportError (module not installed).
    tirith_result = {"action": "allow", "findings": [], "summary": ""}
    try:
        from tools.tirith_security import check_command_security
        tirith_result = check_command_security(command)
    except ImportError:
        pass  # tirith module not installed — allow

    # Dangerous command check (detection only, no approval)
    is_dangerous, pattern_key, description = detect_dangerous_command(command)

    # --- Phase 2: Decide ---

    # Collect warnings that need approval
    warnings = []  # list of (pattern_key, description, is_tirith)

    # Tirith block/warn → approvable warning with rich findings.
    # Previously, tirith "block" was a hard block with no approval prompt.
    # Now both block and warn go through the approval flow so users can
    # inspect the explanation and approve if they understand the risk.
    if tirith_result["action"] in {"block", "warn"}:
        findings = tirith_result.get("findings") or []
        rule_id = findings[0].get("rule_id", "unknown") if findings else "unknown"
        tirith_key = f"tirith:{rule_id}"
        tirith_desc = _format_tirith_description(tirith_result)
        if admin_enforced or not is_approved(session_key, tirith_key):
            warnings.append((tirith_key, tirith_desc, True))

    if is_dangerous:
        if admin_enforced or not is_approved(session_key, pattern_key):
            warnings.append((pattern_key, description, False))

    # Nothing to warn about
    if not warnings and normalized_plan is None:
        return {"approved": True, "message": None}

    if not warnings:
        warnings.append((
            "terminal:intent-plan",
            "Pre-authorize the exact command plan for this intent",
            False,
        ))

    if admin_enforced and not normalized_purpose:
        return {
            "approved": False,
            "message": (
                "BLOCKED: Administrator approval requires a clear terminal "
                "purpose. Retry the terminal call with a concise user-facing "
                "outcome in the purpose field; do not merely restate the command."
            ),
            "description": "terminal purpose is required for administrator approval",
            "outcome": "missing_purpose",
        }

    review_purpose = normalized_purpose
    if admin_enforced:
        try:
            from agent.redact import redact_sensitive_text

            review_purpose = redact_sensitive_text(normalized_purpose, force=True)
        except Exception:
            return {
                "approved": False,
                "message": (
                    "BLOCKED: Terminal purpose could not be safely prepared for "
                    "administrator review. Do not retry with secrets in purpose."
                ),
                "description": "terminal purpose redaction failed",
                "outcome": "purpose_redaction_failed",
            }

    # --- Phase 2.5: Smart approval (auxiliary LLM risk assessment) ---
    # When approvals.mode=smart, ask the aux LLM before prompting the user.
    # Inspired by OpenAI Codex's Smart Approvals guardian subagent
    # (openai/codex#13860).
    if approval_mode == "smart" and not admin_enforced:
        combined_desc_for_llm = "; ".join(desc for _, desc, _ in warnings)
        verdict = _smart_approve(command, combined_desc_for_llm)
        if verdict == "approve":
            # Auto-approve and grant session-level approval for these patterns
            for key, _, _ in warnings:
                approve_session(session_key, key)
            logger.debug("Smart approval: auto-approved '%s' (%s)",
                         command[:60], combined_desc_for_llm)
            return {"approved": True, "message": None,
                    "smart_approved": True,
                    "description": combined_desc_for_llm}
        elif verdict == "deny":
            combined_desc_for_llm = "; ".join(desc for _, desc, _ in warnings)
            return {
                "approved": False,
                "message": f"BLOCKED by smart approval: {combined_desc_for_llm}. "
                           "The command was assessed as genuinely dangerous. Do NOT retry.",
                "smart_denied": True,
            }
        # verdict == "escalate" → fall through to manual prompt

    # --- Phase 3: Approval ---

    # Combine descriptions for a single approval prompt
    combined_desc = "; ".join(desc for _, desc, _ in warnings)
    primary_key = warnings[0][0]
    all_keys = [key for key, _, _ in warnings]
    has_tirith = any(is_t for _, _, is_t in warnings)

    # Gateway/async approval — block the agent thread until the user
    # responds with /approve or /deny, mirroring the CLI's synchronous
    # input() flow.  The agent never sees "approval_required"; it either
    # gets the command output (approved) or a definitive "BLOCKED" message.
    if is_gateway or is_ask:
        notify_cb = None
        with _lock:
            notify_cb = _gateway_notify_cbs.get(session_key)

        if notify_cb is not None:
            # --- Blocking gateway approval (queue-based) ---
            # Block the agent thread until the user responds; the notify +
            # heartbeat wait loop is shared with check_execute_code_guard via
            # _await_gateway_decision().
            approval_data: dict[str, object] = {
                "command": command,
                "pattern_key": primary_key,
                "pattern_keys": all_keys,
                "description": combined_desc,
            }
            if admin_enforced:
                from tools.action_intent import build_action_intent

                terminal_intent = build_action_intent(
                    tool_name="terminal",
                    args={
                        "purpose": normalized_purpose,
                        "command": command,
                        "environment": env_type,
                        "workdir": workdir,
                        **(
                            {"approval_plan": normalized_plan}
                            if normalized_plan is not None
                            else {}
                        ),
                    },
                    builder={
                        "action_type": "terminal.execute",
                        "operation": review_purpose,
                        "target": f"{env_type} terminal environment",
                        "reason": combined_desc,
                        "impact": (
                            "The command was flagged as capable of changing "
                            "or damaging system state."
                        ),
                        "parameters": {
                            "command": command,
                            "environment": env_type,
                            "workdir": workdir,
                            **(
                                {"command_plan": normalized_plan}
                                if normalized_plan is not None
                                else {}
                            ),
                        },
                    },
                )
                if terminal_intent is None:  # static builders never skip
                    raise RuntimeError("terminal action intent was not created")
                approval_data.update({
                    "kind": "action_intent",
                    "action": terminal_intent.summary(),
                    "command": terminal_intent.summary(),
                    "description": (
                        f"{terminal_intent.reason}\nImpact: {terminal_intent.impact}"
                    ),
                    "action_intent": terminal_intent.to_dict(),
                    "allowed_choices": ["once", "deny"],
                    "request_scoped_only": True,
                })
            decision = _await_gateway_decision(
                session_key, notify_cb, approval_data, surface="gateway"
            )
            if decision.get("notify_failed"):
                return {
                    "approved": False,
                    "message": "BLOCKED: Failed to send approval request to user. Do NOT retry.",
                    "pattern_key": primary_key,
                    "description": combined_desc,
                }
            resolved = decision["resolved"]
            choice = decision["choice"]

            if not resolved or choice is None or choice == "deny":
                # Consent contract: silence is NOT consent, and an explicit
                # deny is also a hard halt — both produce a BLOCKED outcome
                # that names the agent's most common evasion paths (retry,
                # rephrase, achieve the same outcome via a different command).
                # See issue #24912 for the original incident.
                if not resolved:
                    reason = "timed out without user response"
                    timeout_addendum = " Silence is not consent."
                    outcome = "timeout"
                else:
                    reason = "denied by user"
                    timeout_addendum = ""
                    outcome = "denied"
                return {
                    "approved": False,
                    "message": (
                        f"BLOCKED: Command {reason}. The user has NOT consented "
                        f"to this action. Do NOT retry this command, do NOT "
                        f"rephrase it, and do NOT attempt the same outcome via "
                        f"a different command. Stop the current workflow and "
                        f"wait for the user to respond before taking any "
                        f"further destructive or irreversible action."
                        f"{timeout_addendum}"
                    ),
                    "pattern_key": primary_key,
                    "description": combined_desc,
                    "outcome": outcome,
                    "user_consent": False,
                }

            # User approved — persist based on scope (same logic as CLI)
            for key, _, is_tirith in warnings:
                if choice == "session" or (choice == "always" and is_tirith):
                    approve_session(session_key, key)
                elif choice == "always":
                    approve_session(session_key, key)
                    approve_permanent(key)
                    save_permanent_allowlist(_permanent_approved)
                # choice == "once": no persistence — command allowed this
                # single time only, matching the CLI's behavior.

            result = {"approved": True, "message": None,
                      "user_approved": True, "description": combined_desc}
            if admin_enforced and normalized_plan is not None:
                grant_id, remaining = _create_terminal_intent_grant(
                    session_key=session_key,
                    purpose=normalized_purpose,
                    plan=normalized_plan,
                    current_command=command,
                    env_type=env_type,
                    workdir=workdir,
                )
                if grant_id:
                    result.update({
                        "intent_grant_id": grant_id,
                        "intent_grant_remaining": remaining,
                        "intent_grant_expires_in_seconds": _INTENT_GRANT_TTL_SECONDS,
                    })
            return result

        # Fallback: no gateway callback registered (e.g. cron, batch).
        # Return approval_required for backward compat.
        submit_pending(session_key, {
            "command": command,
            "pattern_key": primary_key,
            "pattern_keys": all_keys,
            "description": combined_desc,
        })
        return {
            "approved": False,
            "pattern_key": primary_key,
            "status": "pending_approval",
            "approval_pending": True,
            "command": command,
            "description": combined_desc,
            "message": (
                f"⚠️ {combined_desc}. Asking the user for approval.\n\n**Command:**\n```\n{command}\n```"
            ),
        }

    # CLI interactive: single combined prompt
    # Hide [a]lways when any tirith warning is present
    _fire_approval_hook(
        "pre_approval_request",
        command=command,
        description=combined_desc,
        pattern_key=primary_key,
        pattern_keys=list(all_keys),
        session_key=session_key,
        surface="cli",
    )
    choice = prompt_dangerous_approval(command, combined_desc,
                                       allow_permanent=not has_tirith,
                                       approval_callback=approval_callback)
    _fire_approval_hook(
        "post_approval_response",
        command=command,
        description=combined_desc,
        pattern_key=primary_key,
        pattern_keys=list(all_keys),
        session_key=session_key,
        surface="cli",
        choice=choice,
    )

    if choice == "deny":
        return {
            "approved": False,
            "message": (
                "BLOCKED: User denied this command. The user has NOT consented "
                "to this action. Do NOT retry this command, do NOT rephrase "
                "it, and do NOT attempt the same outcome via a different "
                "command. Stop the current workflow and wait for the user "
                "to respond before taking any further destructive or "
                "irreversible action."
            ),
            "pattern_key": primary_key,
            "description": combined_desc,
            "outcome": "denied",
            "user_consent": False,
        }

    # Persist approval for each warning individually
    for key, _, is_tirith in warnings:
        if choice == "session" or (choice == "always" and is_tirith):
            # tirith: session only (no permanent broad allowlisting)
            approve_session(session_key, key)
        elif choice == "always":
            # dangerous patterns: permanent allowed
            approve_session(session_key, key)
            approve_permanent(key)
            save_permanent_allowlist(_permanent_approved)

    return {"approved": True, "message": None,
            "user_approved": True, "description": combined_desc}


def check_execute_code_guard(code: str, env_type: str) -> dict:
    """Approve an execute_code script before its child process is spawned.

    execute_code runs arbitrary local Python — the script can call
    ``subprocess``, ``os.system``, ``ctypes``, or other process/file APIs
    directly, none of which pass through ``terminal()`` /
    ``DANGEROUS_PATTERNS``. In gateway/ask contexts we fail closed by approving
    the script as a whole before it runs (#30882). Returns the same dict
    contract as ``check_all_command_guards``.

    Scope (documented limitation, #30882): in a purely local non-interactive
    non-gateway session (no TTY, not gateway, not cron-deny) this returns
    approved — matching the existing terminal auto-approve contract. The
    hardline floor still blocks catastrophic ``terminal()`` commands the script
    issues; running arbitrary code headlessly without any approval surface is
    trusted-by-config (set a gateway/ask surface or ``approvals.cron_mode`` to
    require approval).
    """
    pattern_key = "execute_code"
    description = (
        "execute_code script execution. The script can spawn subprocesses or "
        "mutate files without passing through terminal command approval; "
        "approval is one-shot for this run."
    )

    # Isolated backends already sandbox the child — matches the container skip
    # in check_all_command_guards / check_dangerous_command.
    if env_type == "docker":
        return {"approved": True, "message": None}

    if is_super_admin_context():
        logger.info("Super-admin auto-approved execute_code guard")
        return {
            "approved": True,
            "message": None,
            "super_admin_approved": True,
        }

    # Exact-admin gateway routing takes precedence over all approval bypasses.
    approval_config = _get_approval_config()
    approval_mode = _normalize_approval_mode(approval_config.get("mode", "manual"))
    admin_enforced = _is_admin_approval_enforced(approval_config)
    if not admin_enforced and (
        _YOLO_MODE_FROZEN
        or is_current_session_yolo_enabled()
        or approval_mode == "off"
    ):
        return {"approved": True, "message": None}

    is_gateway = _is_gateway_approval_context()
    is_ask = env_var_enabled("MARLOW_EXEC_ASK")

    # Cron: no user is present to approve arbitrary code.
    if env_var_enabled("MARLOW_CRON_SESSION"):
        if _get_cron_approval_mode() == "deny":
            return {
                "approved": False,
                "message": (
                    "BLOCKED: execute_code runs arbitrary local Python "
                    "(including subprocess calls that bypass shell-string "
                    "approval checks). Cron jobs run without a user present "
                    "to approve it. Use normal tools instead, or set "
                    "approvals.cron_mode: approve only if this cron profile "
                    "is intentionally trusted."
                ),
                "pattern_key": pattern_key,
                "description": description,
                "outcome": "blocked",
                "user_consent": False,
            }
        return {"approved": True, "message": None}

    # Only gateway/ask contexts get the one-shot whole-script approval.
    #   * CLI interactive: the script's terminal() calls are guarded per-call
    #     (context now propagates into the RPC thread, #33057); a whole-script
    #     prompt would fire on every execute_code call.
    #   * Local non-interactive non-gateway: documented limitation above.
    if not is_gateway and not is_ask:
        return {"approved": True, "message": None}

    session_key = get_current_session_key()
    # Built only now (past the early-return gates) so the common non-approval
    # paths don't pay to copy a potentially-large script into this string.
    command = f"execute_code <<'PY'\n{code}\nPY"

    # Smart mode: ask the aux LLM about the whole script. An APPROVE here only
    # suppresses the redundant whole-script prompt; the per-call terminal()
    # guards (restored by context propagation) still run independently.
    if approval_mode == "smart" and not admin_enforced:
        verdict = _smart_approve(command, description)
        if verdict == "approve":
            logger.debug("Smart approval: auto-approved execute_code for session %s",
                         session_key)
            return {"approved": True, "message": None,
                    "smart_approved": True, "description": description}
        if verdict == "deny":
            return {
                "approved": False,
                "message": ("BLOCKED by smart approval: execute_code script "
                            "execution was assessed as genuinely dangerous. "
                            "Do NOT retry."),
                "smart_denied": True,
                "pattern_key": pattern_key,
                "description": description,
                "outcome": "denied",
                "user_consent": False,
            }
        # verdict == "escalate" → fall through to manual approval

    notify_cb = None
    with _lock:
        notify_cb = _gateway_notify_cbs.get(session_key)

    if notify_cb is None:
        # No gateway callback registered (e.g. ask-mode without a notifier):
        # surface a pending approval for backward compatibility.
        submit_pending(session_key, {
            "command": command,
            "pattern_key": pattern_key,
            "pattern_keys": [pattern_key],
            "description": description,
        })
        return {
            "approved": False,
            "pattern_key": pattern_key,
            "status": "pending_approval",
            "approval_pending": True,
            "command": command,
            "description": description,
            "message": (
                f"⚠️ {description}. Asking the user for approval.\n\n"
                f"**Code:**\n```python\n{code}\n```"
            ),
        }

    approval_data: dict[str, object] = {
        "command": command,
        "pattern_key": pattern_key,
        "pattern_keys": [pattern_key],
        "description": description,
    }
    if admin_enforced:
        from tools.action_intent import build_action_intent

        code_intent = build_action_intent(
            tool_name="execute_code",
            args={"code": code, "environment": env_type},
            builder={
                "action_type": "code.execute",
                "operation": "execute Python code",
                "target": f"{env_type} execution environment",
                "reason": description,
                "impact": (
                    "The script can spawn processes or mutate files and "
                    "external systems."
                ),
            },
        )
        if code_intent is None:  # static builders never skip
            raise RuntimeError("execute_code action intent was not created")
        approval_data.update({
            "kind": "action_intent",
            "action": code_intent.summary(),
            "command": code_intent.summary(),
            "description": f"{code_intent.reason}\nImpact: {code_intent.impact}",
            "action_intent": code_intent.to_dict(),
            "allowed_choices": ["once", "deny"],
            "request_scoped_only": True,
        })
    decision = _await_gateway_decision(
        session_key, notify_cb, approval_data, surface="gateway"
    )
    if decision.get("notify_failed"):
        return {
            "approved": False,
            "message": ("BLOCKED: Failed to send execute_code approval request "
                        "to user. Do NOT retry."),
            "pattern_key": pattern_key,
            "description": description,
            "outcome": "notify_failed",
            "user_consent": False,
        }

    resolved = decision["resolved"]
    choice = decision["choice"]

    if not resolved or choice is None or choice == "deny":
        reason = "timed out without user response" if not resolved else "denied by user"
        addendum = " Silence is not consent." if not resolved else ""
        return {
            "approved": False,
            "message": (
                f"BLOCKED: execute_code script {reason}. The user has NOT "
                f"consented to running this code. Do NOT retry, do NOT rephrase "
                f"the script, and do NOT attempt the same outcome via a "
                f"different tool.{addendum}"
            ),
            "pattern_key": pattern_key,
            "description": description,
            "outcome": "timeout" if not resolved else "denied",
            "user_consent": False,
        }

    # Approved — one-shot only. Deliberately NO approve_session/approve_permanent:
    # each execute_code script is distinct arbitrary code, so approval never
    # persists to future scripts.
    return {"approved": True, "message": None,
            "user_approved": True, "description": description}


# Load permanent allowlist from config on module import
load_permanent_allowlist()

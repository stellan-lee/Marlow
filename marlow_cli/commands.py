"""Slash command definitions and autocomplete for the Marlow CLI.

Central registry for gateway dispatch, Telegram BotCommands and Slack routing.

To add a command: add a ``CommandDef`` entry to ``COMMAND_REGISTRY``.
To add an alias: set ``aliases=("short",)`` on the existing ``CommandDef``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from utils import is_truthy_value

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CommandDef dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandDef:
    """Definition of a single slash command."""

    name: str                          # canonical name without slash: "background"
    description: str                   # human-readable description
    category: str                      # "Session", "Configuration", etc.
    aliases: tuple[str, ...] = ()      # alternative names: ("bg",)
    args_hint: str = ""                # argument placeholder: "<prompt>", "[name]"
    subcommands: tuple[str, ...] = ()  # tab-completable subcommands
    cli_only: bool = False             # only available in CLI
    gateway_only: bool = False         # only available in gateway/messaging
    gateway_config_gate: str | None = None  # config dotpath; when truthy, overrides cli_only for gateway
    native_menu: bool = True           # surface as a standalone Telegram/Slack command


# ---------------------------------------------------------------------------
# Central registry -- single source of truth
# ---------------------------------------------------------------------------

COMMAND_REGISTRY: list[CommandDef] = [
    # Session
    CommandDef("start", "Acknowledge platform start pings without a reply", "Session",
               gateway_only=True),
    CommandDef("new", "Start a new session (fresh session ID + history)", "Session",
               aliases=("reset",), args_hint="[name]"),
    CommandDef("topic", "Enable or inspect Telegram DM topic sessions", "Session",
               gateway_only=True, args_hint="[off|help|session-id]"),
    CommandDef("retry", "Retry the last message (resend to agent)", "Session"),
    CommandDef("undo", "Back up N user turns and re-prompt (default 1)", "Session",
               args_hint="[N]"),
    CommandDef("title", "Set a title for the current session", "Session",
               args_hint="[name]"),
    CommandDef("branch", "Branch the current session (explore a different path)", "Session",
               aliases=("fork",), args_hint="[name]"),
    CommandDef("compress", "Compress conversation context (add 'here [N]' to keep recent N turns)", "Session",
               args_hint="[here [N] | focus topic]"),
    CommandDef("rollback", "List or restore filesystem checkpoints", "Session",
               args_hint="[number]"),
    CommandDef("stop", "Kill all running background processes", "Session"),
    CommandDef("approve", "Approve a pending dangerous command", "Session",
               gateway_only=True, args_hint="[session|always]"),
    CommandDef("deny", "Deny a pending dangerous command", "Session",
               gateway_only=True),
    CommandDef("background", "Run a prompt in the background", "Session",
               aliases=("bg", "btw"), args_hint="<prompt>"),
    CommandDef("agents", "Show active agents and running tasks", "Session",
               aliases=("tasks",)),
    CommandDef("queue", "Queue a prompt for the next turn (doesn't interrupt)", "Session",
               aliases=("q",), args_hint="<prompt>"),
    CommandDef("steer", "Inject a message after the next tool call without interrupting", "Session",
               args_hint="<prompt>"),
    CommandDef("goal", "Set a standing goal Marlow works on across turns until achieved", "Session",
               args_hint="[text | pause | resume | clear | status]"),
    CommandDef("subgoal", "Add or manage extra criteria on the active goal", "Session",
               args_hint="[text | remove N | clear]"),
    CommandDef("status", "Show session info", "Session"),
    CommandDef("whoami", "Show your slash command access (admin / user)", "Info"),
    CommandDef(
        "access",
        "Manage Telegram group access",
        "Configuration",
        gateway_only=True,
        args_hint="<grant|revoke|list> [user-id]",
        subcommands=("grant", "revoke", "list"),
    ),
    CommandDef("profile", "Show active profile name and home directory", "Info"),
    CommandDef("sethome", "Set this chat as the home channel", "Session",
               gateway_only=True, aliases=("set-home",)),
    CommandDef("resume", "Resume a previously-named session", "Session",
               args_hint="[name]"),

    # Configuration
    CommandDef("sessions", "Browse and resume previous sessions", "Session"),

    # Configuration
    CommandDef("model", "Switch model for this session", "Configuration",
               args_hint="[model] [--provider name] [--global] [--refresh]"),
    CommandDef("verbose", "Cycle tool progress display: off -> new -> all -> verbose",
               "Configuration", cli_only=True,
               gateway_config_gate="display.tool_progress_command"),
    CommandDef("footer", "Toggle gateway runtime-metadata footer on final replies",
               "Configuration", args_hint="[on|off|status]",
               subcommands=("on", "off", "status")),
    CommandDef("yolo", "Toggle YOLO mode (skip all dangerous command approvals)",
               "Configuration"),
    CommandDef("reasoning", "Manage reasoning effort and display", "Configuration",
               args_hint="[level|show|hide]",
               subcommands=("none", "minimal", "low", "medium", "high", "xhigh", "show", "hide", "on", "off")),

    # Tools & Skills
    CommandDef("learn", "Learn a reusable skill from anything you describe",
               "Tools & Skills", args_hint="<what to learn from>"),
    CommandDef("reload-mcp", "Reload MCP servers from config", "Tools & Skills",
               aliases=("reload_mcp",)),
    CommandDef("reload-skills", "Re-scan ~/.marlow/skills/ for newly installed or removed skills",
               "Tools & Skills", aliases=("reload_skills",)),

    # Info
    CommandDef("commands", "Browse all commands and skills (paginated)", "Info",
               gateway_only=True, args_hint="[page]"),
    CommandDef("help", "Show available commands", "Info"),
    CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True),
    CommandDef("usage", "Show token usage and rate limits for the current session", "Info"),
    CommandDef("insights", "Show usage insights and analytics", "Info",
               args_hint="[days]"),
    CommandDef("platform", "Pause, resume, or list a failing gateway platform", "Info",
               gateway_only=True, args_hint="<pause|resume|list> [name]"),
    CommandDef("update", "Update Marlow Agent to the latest version", "Info"),
    CommandDef("debug", "Upload debug report (system info + logs) and get shareable links", "Info"),

    # Exit
]


# ---------------------------------------------------------------------------
# Derived lookups -- rebuilt once at import time, refreshed by rebuild_lookups()
# ---------------------------------------------------------------------------

def _build_command_lookup() -> dict[str, CommandDef]:
    """Map every name and alias to its CommandDef."""
    lookup: dict[str, CommandDef] = {}
    for cmd in COMMAND_REGISTRY:
        lookup[cmd.name] = cmd
        for alias in cmd.aliases:
            lookup[alias] = cmd
    return lookup


_COMMAND_LOOKUP: dict[str, CommandDef] = _build_command_lookup()


def resolve_command(name: str) -> CommandDef | None:
    """Resolve a command name or alias to its CommandDef.

    Accepts names with or without the leading slash.
    """
    return _COMMAND_LOOKUP.get(name.lower().lstrip("/"))


def _build_description(cmd: CommandDef) -> str:
    """Build a CLI-facing description string including usage hint."""
    if cmd.args_hint:
        return f"{cmd.description} (usage: /{cmd.name} {cmd.args_hint})"
    return cmd.description


# Backwards-compatible flat dict: "/command" -> description
COMMANDS: dict[str, str] = {}
for _cmd in COMMAND_REGISTRY:
    if not _cmd.gateway_only:
        COMMANDS[f"/{_cmd.name}"] = _build_description(_cmd)
        for _alias in _cmd.aliases:
            COMMANDS[f"/{_alias}"] = f"{_cmd.description} (alias for /{_cmd.name})"

# Backwards-compatible categorized dict
COMMANDS_BY_CATEGORY: dict[str, dict[str, str]] = {}
for _cmd in COMMAND_REGISTRY:
    if not _cmd.gateway_only:
        _cat = COMMANDS_BY_CATEGORY.setdefault(_cmd.category, {})
        _cat[f"/{_cmd.name}"] = COMMANDS[f"/{_cmd.name}"]
        for _alias in _cmd.aliases:
            _cat[f"/{_alias}"] = COMMANDS[f"/{_alias}"]


# Subcommands lookup: "/cmd" -> ["sub1", "sub2", ...]
SUBCOMMANDS: dict[str, list[str]] = {}
for _cmd in COMMAND_REGISTRY:
    if _cmd.subcommands:
        SUBCOMMANDS[f"/{_cmd.name}"] = list(_cmd.subcommands)

# Also extract subcommands hinted in args_hint via pipe-separated patterns
# e.g. args_hint="[on|off|status]" for commands that don't have explicit subcommands.
# NOTE: If a command already has explicit subcommands, this fallback is skipped.
# Use the `subcommands` field on CommandDef for intentional tab-completable args.
_PIPE_SUBS_RE = re.compile(r"[a-z]+(?:\|[a-z]+)+")
for _cmd in COMMAND_REGISTRY:
    key = f"/{_cmd.name}"
    if key in SUBCOMMANDS or not _cmd.args_hint:
        continue
    m = _PIPE_SUBS_RE.search(_cmd.args_hint)
    if m:
        SUBCOMMANDS[key] = m.group(0).split("|")


# ---------------------------------------------------------------------------
# Gateway helpers
# ---------------------------------------------------------------------------

# Set of all command names + aliases recognized by the gateway.
# Includes config-gated commands so the gateway can dispatch them
# (the handler checks the config gate at runtime).
GATEWAY_KNOWN_COMMANDS: frozenset[str] = frozenset(
    name
    for cmd in COMMAND_REGISTRY
    if not cmd.cli_only or cmd.gateway_config_gate
    for name in (cmd.name, *cmd.aliases)
)


def is_gateway_known_command(name: str | None) -> bool:
    """Return True if ``name`` resolves to a gateway-dispatchable slash command.

    This covers both built-in commands (``GATEWAY_KNOWN_COMMANDS`` derived
    from ``COMMAND_REGISTRY``) and plugin-registered commands, which are
    looked up lazily so importing this module never forces plugin
    discovery. Gateway code uses this to decide whether to emit
    ``command:<name>`` hooks — plugin commands get the same lifecycle
    events as built-ins.
    """
    if not name:
        return False
    if name in GATEWAY_KNOWN_COMMANDS:
        return True
    for plugin_name, _description, _args_hint in _iter_plugin_command_entries():
        if plugin_name == name:
            return True
    return False


# Commands with explicit Level-2 running-agent handlers in gateway/run.py.
# Listed here for introspection / tests; semantically a subset of
# "all resolvable commands" — which is the real bypass set (see
# should_bypass_active_session below).
ACTIVE_SESSION_BYPASS_COMMANDS: frozenset[str] = frozenset(
    {
        "agents",
        "approve",
        "background",
        "commands",
        "deny",
        "help",
        "new",
        "profile",
        "queue",
        "restart",
        "status",
        "steer",
        "stop",
        "update",
    }
)


def should_bypass_active_session(command_name: str | None) -> bool:
    """Return True for any resolvable slash command.

    Rationale: every gateway-registered slash command either has a
    specific Level-2 handler in gateway/run.py (/stop, /new, /model,
    /approve, etc.) or reaches the running-agent catch-all that returns
    a "busy — wait or /stop first" response. In both paths the command
    is dispatched, not queued.

    Queueing is always wrong for a recognized slash command because the
    safety net in gateway.run discards any command text that reaches
    the pending queue — which meant a mid-run /model (or /reasoning,
    /insights, /title, /resume, /retry, /undo, /compress,
    /usage, /reload-mcp, /sethome, /reset) would silently
    interrupt the agent AND get discarded, producing a zero-char
    response. See issue #5057 / PRs #6252, #10370, #4665.

    ACTIVE_SESSION_BYPASS_COMMANDS remains the subset of commands with
    explicit Level-2 handlers; the rest fall through to the catch-all.
    """
    return resolve_command(command_name) is not None if command_name else False


def _resolve_config_gates() -> set[str]:
    """Return canonical names of commands whose ``gateway_config_gate`` is truthy.

    Reads ``config.yaml`` and walks the dot-separated key path for each
    config-gated command.  Returns an empty set on any error so callers
    degrade gracefully.
    """
    gated = [c for c in COMMAND_REGISTRY if c.gateway_config_gate]
    if not gated:
        return set()
    try:
        from marlow_cli.config import read_raw_config
        cfg = read_raw_config()
    except Exception:
        return set()
    result: set[str] = set()
    for cmd in gated:
        val: Any = cfg
        for key in cmd.gateway_config_gate.split("."):
            if isinstance(val, dict):
                val = val.get(key)
            else:
                val = None
                break
        if is_truthy_value(val, default=False):
            result.add(cmd.name)
    return result


def _is_gateway_available(cmd: CommandDef, config_overrides: set[str] | None = None) -> bool:
    """Check if *cmd* should appear in gateway surfaces (help, menus, mappings).

    Unconditionally available when ``cli_only`` is False.  When ``cli_only``
    is True but ``gateway_config_gate`` is set, the command is available only
    when the config value is truthy.  Pass *config_overrides* (from
    ``_resolve_config_gates()``) to avoid re-reading config for every command.
    """
    if not cmd.cli_only:
        return True
    if cmd.gateway_config_gate:
        overrides = config_overrides if config_overrides is not None else _resolve_config_gates()
        return cmd.name in overrides
    return False


def _requires_argument(args_hint: str) -> bool:
    """Return True when selecting a command without text would be incomplete."""
    return args_hint.strip().startswith("<")


def gateway_help_lines() -> list[str]:
    """Generate gateway help text lines from the registry."""
    overrides = _resolve_config_gates()
    lines: list[str] = []
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        args = f" {cmd.args_hint}" if cmd.args_hint else ""
        alias_parts: list[str] = []
        for a in cmd.aliases:
            # Skip internal aliases like reload_mcp (underscore variant)
            if a.replace("-", "_") == cmd.name.replace("-", "_") and a != cmd.name:
                continue
            alias_parts.append(f"`/{a}`")
        alias_note = f" (alias: {', '.join(alias_parts)})" if alias_parts else ""
        lines.append(f"`/{cmd.name}{args}` -- {cmd.description}{alias_note}")
    return lines


def _iter_plugin_command_entries() -> list[tuple[str, str, str]]:
    """Yield (name, description, args_hint) tuples for all plugin slash commands.

    Plugin commands are registered via
    :func:`marlow_cli.plugins.PluginContext.register_command`. They behave
    like ``CommandDef`` entries for gateway surfacing: they appear in the
    Telegram command menu, in Slack's ``/marlow`` subcommand mapping, and
    (via :func:`plugins.platforms.discord.adapter._register_slash_commands`) in
    Discord's native slash command picker.

    Lookup is lazy so importing this module never forces plugin discovery
    (which can trigger filesystem scans and environment-dependent
    behavior).
    """
    try:
        from marlow_cli.plugins import get_plugin_commands
    except Exception:
        return []
    try:
        commands = get_plugin_commands() or {}
    except Exception:
        return []
    entries: list[tuple[str, str, str]] = []
    for name, meta in commands.items():
        if not isinstance(name, str) or not isinstance(meta, dict):
            continue
        description = str(meta.get("description") or f"Run /{name}")
        args_hint = str(meta.get("args_hint") or "").strip()
        entries.append((name, description, args_hint))
    return entries


def telegram_bot_commands() -> list[tuple[str, str]]:
    """Return (command_name, description) pairs for Telegram setMyCommands.

    Telegram command names cannot contain hyphens, so they are replaced with
    underscores.  Aliases are skipped -- Telegram shows one menu entry per
    canonical command.

    Built-in commands that require arguments (e.g. /queue, /steer, /background)
    are **included** because their handlers return usage text when selected
    without a payload, making them discoverable via autocomplete.

    Plugin-registered slash commands that require arguments are **excluded**
    because plugins may not provide a no-arg usage fallback.
    """
    overrides = _resolve_config_gates()
    result: list[tuple[str, str]] = []
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        if not cmd.native_menu:
            continue
        # Built-in arg-taking commands are included — their handlers show
        # usage text when invoked without arguments, and hiding them from
        # the menu hurts discoverability (issue #24312).
        tg_name = _sanitize_telegram_name(cmd.name)
        if tg_name:
            result.append((tg_name, cmd.description))
    for name, description, args_hint in _iter_plugin_command_entries():
        if _requires_argument(args_hint):
            continue
        tg_name = _sanitize_telegram_name(name)
        if tg_name:
            result.append((tg_name, description))
    return result


_TELEGRAM_MENU_PRIORITY = (
    # Most-typed everyday commands first.
    "help",
    "new",
    "stop",
    "status",
    "resume",
    "sessions",
    "model",
    # Maintenance / diagnostics — the ones that prompted this priority list.
    "debug",
    "restart",
    "update",
    "verbose",
    "commands",
    # Mid-turn session control.
    "approve",
    "deny",
    "queue",
    "steer",
    "background",
    # Lower-priority but still useful operational built-ins.
    "reasoning",
    "usage",
    "platforms",
    "platform",
    "profile",
    "whoami",
    "access",
)
"""Built-in commands that should stay visible in Telegram's capped menu.

Telegram only displays a small BotCommand menu in practice.  The full Marlow
registry is still dispatchable when typed manually, but operational commands
need to survive the visible menu cap ahead of lower-priority built-ins.
"""


def _prioritize_telegram_menu_commands(
    commands: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    priority = {
        _sanitize_telegram_name(name): index
        for index, name in enumerate(_TELEGRAM_MENU_PRIORITY)
    }
    return [
        command
        for _index, command in sorted(
            enumerate(commands),
            key=lambda item: (
                0,
                priority[item[1][0]],
                item[0],
            )
            if item[1][0] in priority
            else (
                1,
                item[0],
            ),
        )
    ]


_CMD_NAME_LIMIT = 32
"""Max command name length shared by Telegram and Discord."""

# Backward-compat alias — tests and external code may reference the old name.
_TG_NAME_LIMIT = _CMD_NAME_LIMIT

# Telegram Bot API allows only lowercase a-z, 0-9, and underscores in
# command names.  This regex strips everything else after initial conversion.
_TG_INVALID_CHARS = re.compile(r"[^a-z0-9_]")
_TG_MULTI_UNDERSCORE = re.compile(r"_{2,}")


def _sanitize_telegram_name(raw: str) -> str:
    """Convert a command/skill/plugin name to a valid Telegram command name.

    Telegram requires: 1-32 chars, lowercase a-z, digits 0-9, underscores only.
    Steps: lowercase → replace hyphens with underscores → strip all other
    invalid characters → collapse consecutive underscores → strip leading/
    trailing underscores.
    """
    name = raw.lower().replace("-", "_")
    name = _TG_INVALID_CHARS.sub("", name)
    name = _TG_MULTI_UNDERSCORE.sub("_", name)
    return name.strip("_")


def _clamp_command_names(
    entries: list[tuple[str, ...]],
    reserved: set[str],
) -> list[tuple[str, ...]]:
    """Enforce 32-char command name limit with collision avoidance.

    Both Telegram and Discord cap slash command names at 32 characters.
    Names exceeding the limit are truncated.  If truncation creates a duplicate
    (against *reserved* names or earlier entries in the same batch), the name is
    shortened to 31 chars and a digit ``0``-``9`` is appended to differentiate.
    If all 10 digit slots are taken the entry is silently dropped.

    Accepts tuples of any length >= 2.  Extra elements beyond ``(name, desc)``
    (e.g. ``cmd_key``) are passed through unchanged, so callers can attach
    metadata that survives the rename.
    """
    used: set[str] = set(reserved)
    result: list[tuple] = []
    for entry in entries:
        name, desc, *extra = entry
        if len(name) > _CMD_NAME_LIMIT:
            candidate = name[:_CMD_NAME_LIMIT]
            if candidate in used:
                prefix = name[:_CMD_NAME_LIMIT - 1]
                for digit in range(10):
                    candidate = f"{prefix}{digit}"
                    if candidate not in used:
                        break
                else:
                    # All 10 digit slots exhausted — skip entry
                    continue
            name = candidate
        if name in used:
            continue
        used.add(name)
        result.append((name, desc, *extra))
    return result


# Backward-compat alias.
_clamp_telegram_names = _clamp_command_names


# ---------------------------------------------------------------------------
# Shared skill/plugin collection for gateway platforms
# ---------------------------------------------------------------------------

def _collect_gateway_skill_entries(
    platform: str,
    max_slots: int,
    reserved_names: set[str],
    desc_limit: int = 100,
    sanitize_name: "Callable[[str], str] | None" = None,
) -> tuple[list[tuple[str, str, str]], int]:
    """Collect plugin + skill entries for a gateway platform.

    Priority order:
      1. Plugin slash commands (take precedence over skills)
      2. Built-in skill commands (fill remaining slots, alphabetical)

    Only skills are trimmed when the cap is reached.
    Per-platform disabled skills are excluded.

    Args:
        platform: Platform identifier for per-platform skill filtering
            (``"telegram"``, ``"discord"``, etc.).
        max_slots: Maximum number of entries to return (remaining slots after
            built-in/core commands).
        reserved_names: Names already taken by built-in commands.  Mutated
            in-place as new names are added.
        desc_limit: Max description length (40 for Telegram, 100 for Discord).
        sanitize_name: Optional name transform applied before clamping, e.g.
            :func:`_sanitize_telegram_name` for Telegram.  May return an
            empty string to signal "skip this entry".

    Returns:
        ``(entries, hidden_count)`` where *entries* is a list of
        ``(name, description, cmd_key)`` triples and *hidden_count* is the
        number of skill entries dropped due to the cap.  ``cmd_key`` is the
        original ``/skill-name`` key from :func:`get_skill_commands`.
    """
    all_entries: list[tuple[str, str, str]] = []

    # --- Tier 1: Plugin slash commands (never trimmed) ---------------------
    plugin_pairs: list[tuple[str, str]] = []
    try:
        from marlow_cli.plugins import get_plugin_commands
        plugin_cmds = get_plugin_commands()
        for cmd_name in sorted(plugin_cmds):
            name = sanitize_name(cmd_name) if sanitize_name else cmd_name
            if not name:
                continue
            desc = plugin_cmds[cmd_name].get("description", "Plugin command")
            if len(desc) > desc_limit:
                desc = desc[:desc_limit - 3] + "..."
            plugin_pairs.append((name, desc))
    except Exception:
        pass

    plugin_pairs = _clamp_command_names(plugin_pairs, reserved_names)
    reserved_names.update(n for n, _ in plugin_pairs)
    # Plugins have no cmd_key — use empty string as placeholder
    for n, d in plugin_pairs:
        all_entries.append((n, d, ""))

    # --- Tier 2: Built-in skill commands (trimmed at cap) -----------------
    _platform_disabled: set[str] = set()
    try:
        from agent.skill_utils import get_disabled_skill_names
        _platform_disabled = get_disabled_skill_names(platform=platform)
    except Exception:
        pass

    skill_triples: list[tuple[str, str, str]] = []
    try:
        from agent.skill_commands import get_skill_commands
        from tools.skills_tool import SKILLS_DIR
        from agent.skill_utils import get_external_skills_dirs
        _skills_dir = str(SKILLS_DIR.resolve())
        # Build set of allowed directory prefixes: local skills dir + any
        # user-configured ``skills.external_dirs``. Ensure each prefix ends
        # with ``/`` so ``/my-skills`` does not also match ``/my-skills-extra``.
        # Without this widening, external skills are visible in
        # ``marlow skills list`` and the agent's ``/skill-name`` dispatch but
        # silently excluded from gateway slash menus (#8110).
        _allowed_prefixes = [_skills_dir.rstrip("/") + "/"]
        _allowed_prefixes.extend(
            str(d).rstrip("/") + "/" for d in get_external_skills_dirs()
        )
        skill_cmds = get_skill_commands()
        for cmd_key in sorted(skill_cmds):
            info = skill_cmds[cmd_key]
            skill_path = info.get("skill_md_path", "")
            if not skill_path:
                continue
            if not any(skill_path.startswith(prefix) for prefix in _allowed_prefixes):
                continue
            skill_name = info.get("name", "")
            if skill_name in _platform_disabled:
                continue
            raw_name = cmd_key.lstrip("/")
            name = sanitize_name(raw_name) if sanitize_name else raw_name
            if not name:
                continue
            desc = info.get("description", "")
            if len(desc) > desc_limit:
                desc = desc[:desc_limit - 3] + "..."
            skill_triples.append((name, desc, cmd_key))
    except Exception:
        pass

    # Clamp names; cmd_key is passed through as extra payload so it survives
    # any clamp-induced renames.
    skill_triples = _clamp_command_names(skill_triples, reserved_names)

    # Skills fill remaining slots — only tier that gets trimmed
    remaining = max(0, max_slots - len(all_entries))
    hidden_count = max(0, len(skill_triples) - remaining)
    for n, d, k in skill_triples[:remaining]:
        all_entries.append((n, d, k))

    return all_entries[:max_slots], hidden_count


# ---------------------------------------------------------------------------
# Platform-specific wrappers
# ---------------------------------------------------------------------------

def telegram_menu_commands(max_commands: int = 100) -> tuple[list[tuple[str, str]], int]:
    """Return Telegram menu commands capped to the Bot API limit.

    Priority order (higher priority = never bumped by overflow):
      1. Core CommandDef commands (always included)
      2. Plugin slash commands (take precedence over skills)
      3. Built-in skill commands (fill remaining slots, alphabetical)

    Skills are the only tier that gets trimmed when the cap is hit.
    Skills disabled for the ``"telegram"`` platform (via ``marlow skills
    config``) are excluded from the menu entirely.

    Returns:
        (menu_commands, hidden_count) where hidden_count is the number of
        commands omitted due to the cap.
    """
    core_commands = _prioritize_telegram_menu_commands(list(telegram_bot_commands()))
    reserved_names = {n for n, _ in core_commands}
    all_commands = list(core_commands)
    hidden_core_count = max(0, len(all_commands) - max_commands)

    remaining_slots = max(0, max_commands - len(all_commands))
    entries, hidden_count = _collect_gateway_skill_entries(
        platform="telegram",
        max_slots=remaining_slots,
        reserved_names=reserved_names,
        desc_limit=40,
        sanitize_name=_sanitize_telegram_name,
    )
    # Drop the cmd_key — Telegram only needs (name, desc) pairs.
    all_commands.extend((n, d) for n, d, _k in entries)
    return all_commands[:max_commands], hidden_count + hidden_core_count


def discord_skill_commands(
    max_slots: int,
    reserved_names: set[str],
) -> tuple[list[tuple[str, str, str]], int]:
    """Return skill entries for Discord slash command registration.

    Same priority and filtering logic as :func:`telegram_menu_commands`
    (plugins > skills, per-platform disabled excluded), but
    adapted for Discord's constraints:

    - Hyphens are allowed in names (no ``-`` → ``_`` sanitization)
    - Descriptions capped at 100 chars (Discord's per-field max)

    Args:
        max_slots: Available command slots (100 minus existing built-in count).
        reserved_names: Names of already-registered built-in commands.

    Returns:
        ``(entries, hidden_count)`` where *entries* is a list of
        ``(discord_name, description, cmd_key)`` triples.  ``cmd_key`` is
        the original ``/skill-name`` key needed for the slash handler callback.
    """
    return _collect_gateway_skill_entries(
        platform="discord",
        max_slots=max_slots,
        reserved_names=set(reserved_names),  # copy — don't mutate caller's set
        desc_limit=100,
    )


def discord_skill_commands_by_category(
    reserved_names: set[str],
) -> tuple[dict[str, list[tuple[str, str, str]]], list[tuple[str, str, str]], int]:
    """Return skill entries organized by category for Discord ``/skill`` autocomplete.

    Skills whose directory is nested at least 2 levels under a scan root
    (e.g. ``creative/ascii-art/SKILL.md``) are grouped by their top-level
    category.  Root-level skills (e.g. ``dogfood/SKILL.md``) are returned as
    *uncategorized*.

    Scan roots include the local ``SKILLS_DIR`` **and** any configured
    ``skills.external_dirs`` — matching the widened filter applied to the
    flat ``discord_skill_commands()`` collector in #18741. Without this
    parity, external-dir skills are visible via ``marlow skills list`` and
    the agent's ``/skill-name`` dispatch but silently absent from Discord's
    ``/skill`` autocomplete.

    Filtering mirrors :func:`discord_skill_commands`: per-platform disabled
    skills excluded, names clamped to 32 chars, descriptions
    clamped to 100 chars.

    The legacy 25-group × 25-subcommand caps (from the old nested
    ``/skill <cat> <name>`` layout) are **not** applied — the live caller
    (``_register_skill_group`` in ``gateway/platforms/discord.py``, refactored
    in PR #11580) flattens these results and feeds them into a single
    autocomplete callback, which scales to thousands of entries without any
    per-command payload concerns. ``hidden_count`` is retained in the return
    tuple for backward compatibility and still reports skills dropped for
    other reasons (32-char clamp collision vs a reserved name).

    Returns:
        ``(categories, uncategorized, hidden_count)``

        - *categories*: ``{category_name: [(name, description, cmd_key), ...]}``
        - *uncategorized*: ``[(name, description, cmd_key), ...]``
        - *hidden_count*: skills dropped due to name clamp collisions
          against already-registered command names.
    """
    from pathlib import Path as _P

    _platform_disabled: set[str] = set()
    try:
        from agent.skill_utils import get_disabled_skill_names
        _platform_disabled = get_disabled_skill_names(platform="discord")
    except Exception:
        pass

    # Collect raw skill data --------------------------------------------------
    categories: dict[str, list[tuple[str, str, str]]] = {}
    uncategorized: list[tuple[str, str, str]] = []
    # Map clamped-32-char-name → what it came from, so we can emit an
    # actionable warning on collision. Reserved (gateway-builtin) command
    # names are marked with a sentinel so the warning distinguishes
    # "skill collided with a reserved command" from "two skills collided
    # on the 32-char clamp" — the latter is the rename-worthy case.
    _names_used: dict[str, str] = dict.fromkeys(reserved_names, "<reserved>")
    hidden = 0

    try:
        from agent.skill_commands import get_skill_commands
        from agent.skill_utils import get_external_skills_dirs
        from tools.skills_tool import SKILLS_DIR

        _skills_dir = SKILLS_DIR.resolve()
        # Build list of (resolved_root, is_local) tuples. Each external dir
        # becomes its own scan root for category derivation — a skill at
        # ``<external>/mlops/foo/SKILL.md`` is still categorized as "mlops".
        _scan_roots: list[_P] = [_skills_dir]
        try:
            for ext in get_external_skills_dirs():
                try:
                    _scan_roots.append(_P(ext).resolve())
                except Exception:
                    continue
        except Exception:
            pass
        skill_cmds = get_skill_commands()

        for cmd_key in sorted(skill_cmds):
            info = skill_cmds[cmd_key]
            skill_path = info.get("skill_md_path", "")
            if not skill_path:
                continue
            sp = _P(skill_path).resolve()
            # Accept skill if it lives under any scan root; record the
            # matching root so we can derive the category correctly.
            matched_root: _P | None = None
            for root in _scan_roots:
                try:
                    sp.relative_to(root)
                except ValueError:
                    continue
                matched_root = root
                break
            if matched_root is None:
                continue

            skill_name = info.get("name", "")
            if skill_name in _platform_disabled:
                continue

            raw_name = cmd_key.lstrip("/")
            # Clamp to 32 chars (Discord per-command name limit)
            discord_name = raw_name[:32]
            if discord_name in _names_used:
                # Two skills whose first 32 chars are identical. One wins
                # (the first one seen, which is alphabetical because the
                # caller iterates ``sorted(skill_cmds)``); the other is
                # dropped from Discord's /skill autocomplete.
                #
                # Silently counting this as ``hidden`` (the old behavior)
                # meant skill authors had no way to discover the drop —
                # their skill just didn't appear in the picker. Emit a
                # WARNING naming both sides so the author can rename the
                # losing skill's frontmatter name to something with a
                # distinct 32-char prefix.
                prior = _names_used[discord_name]
                if prior == "<reserved>":
                    logger.warning(
                        "Discord /skill: %r (from %r) collides on its 32-char "
                        "clamp with a reserved gateway command name %r — the "
                        "skill will not appear in the /skill autocomplete. "
                        "Rename the skill's frontmatter ``name:`` to differ "
                        "in its first 32 chars.",
                        discord_name, cmd_key, discord_name,
                    )
                else:
                    logger.warning(
                        "Discord /skill: %r and %r both clamp to %r on "
                        "Discord's 32-char command-name limit — only %r "
                        "will appear in the /skill autocomplete. Rename "
                        "one skill's frontmatter ``name:`` to differ in "
                        "its first 32 chars.",
                        prior, cmd_key, discord_name, prior,
                    )
                hidden += 1
                continue
            _names_used[discord_name] = cmd_key

            desc = info.get("description", "")
            if len(desc) > 100:
                desc = desc[:97] + "..."

            # Determine category from the relative path within the matched
            # scan root. e.g. creative/ascii-art/SKILL.md → ("creative", ...)
            rel = sp.parent.relative_to(matched_root)
            parts = rel.parts
            if len(parts) >= 2:
                cat = parts[0]
                categories.setdefault(cat, []).append((discord_name, desc, cmd_key))
            else:
                uncategorized.append((discord_name, desc, cmd_key))
    except Exception:
        pass

    return categories, uncategorized, hidden


# ---------------------------------------------------------------------------
# Slack native slash commands
# ---------------------------------------------------------------------------

# Slack slash command name constraints: lowercase a-z, 0-9, hyphens,
# underscores. Max 32 chars. Slack app manifest accepts up to 50 slash
# commands per app.
_SLACK_MAX_SLASH_COMMANDS = 50
_SLACK_NAME_LIMIT = 32
_SLACK_INVALID_CHARS = re.compile(r"[^a-z0-9_\-]")
_SLACK_RESERVED_COMMANDS = frozenset({
    # Built-in Slack slash commands that cannot be registered by apps.
    # https://slack.com/help/articles/201259356-Use-built-in-slash-commands
    "me", "status", "away", "dnd", "shrug", "remind", "msg", "feed",
    "who", "collapse", "expand", "leave", "join", "open", "search",
    "topic", "mute", "pro", "shortcuts",
})

# High-value aliases that must survive Slack's 50-slash cap even when the
# registry fills up. Without this, adding a new canonical command silently
# clamps off low-priority aliases (they're added in the second pass), so a
# long-standing native slash like /btw could disappear just because an
# unrelated command landed. These claim their slots right after /marlow,
# ahead of both canonical names and the rest of the aliases. Anything not
# listed here still degrades gracefully (reachable via /marlow <command>).
# Keep this list TIGHT: every pinned alias takes a slot a canonical command
# would otherwise get, and the Telegram-parity test fails when a canonical
# gets clamped ("reset" was unpinned for exactly that — /new keeps its
# native slot, the alias spelling stays reachable via /marlow reset).
_SLACK_PRIORITY_ALIASES = ("btw", "bg", "q")


def _sanitize_slack_name(raw: str) -> str:
    """Convert a command name to a valid Slack slash command name.

    Slack allows lowercase a-z, digits, hyphens, and underscores. Max 32
    chars. Uppercase is lowercased; invalid chars are stripped.
    """
    name = raw.lower()
    name = _SLACK_INVALID_CHARS.sub("", name)
    name = name.strip("-_")
    return name[:_SLACK_NAME_LIMIT]


def slack_native_slashes() -> list[tuple[str, str, str]]:
    """Return (slash_name, description, usage_hint) triples for Slack.

    Every gateway-available command in ``COMMAND_REGISTRY`` is surfaced as
    a standalone Slack slash command (e.g. ``/btw``, ``/stop``, ``/model``),
    matching Discord's and Telegram's model where every command is a
    first-class slash and not a ``/marlow <verb>`` subcommand.

    Both canonical names and aliases are included so users can type any
    documented form (e.g. ``/background``, ``/bg``, and ``/btw`` all work).
    Plugin-registered slash commands are included too.

    Commands whose sanitized name collides with a Slack built-in
    (e.g. ``/status``, ``/me``, ``/join``) are silently skipped.  Users
    can still reach them via ``/marlow <command>``.

    Results are clamped to Slack's 50-command limit with duplicate-name
    avoidance. ``/marlow`` is always reserved as the first entry so the
    legacy ``/marlow <subcommand>`` form keeps working for anything that
    gets dropped by the clamp or for free-form questions.
    """
    overrides = _resolve_config_gates()
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    # Reserve /marlow as the catch-all top-level command.
    entries.append(("marlow", "Talk to Marlow or run a subcommand", "[subcommand] [args]"))
    seen.add("marlow")

    def _add(name: str, desc: str, hint: str) -> None:
        slack_name = _sanitize_slack_name(name)
        if not slack_name or slack_name in seen:
            return
        if slack_name in _SLACK_RESERVED_COMMANDS:
            return
        if len(entries) >= _SLACK_MAX_SLASH_COMMANDS:
            return
        # Slack description cap is 2000 chars; keep it short.
        entries.append((slack_name, desc[:140], hint[:100]))
        seen.add(slack_name)

    # Priority pass: pin high-value aliases (e.g. /btw, /bg, /reset) ahead of
    # everything except /marlow, so a new canonical command can never silently
    # clamp them off the 50-slash cap. Each alias borrows its parent command's
    # description and hint.
    _alias_to_cmd = {
        alias: cmd
        for cmd in COMMAND_REGISTRY
        if _is_gateway_available(cmd, overrides) and cmd.native_menu
        for alias in cmd.aliases
    }
    for alias in _SLACK_PRIORITY_ALIASES:
        cmd = _alias_to_cmd.get(alias)
        if cmd is not None:
            _add(alias, f"Alias for /{cmd.name} — {cmd.description}", cmd.args_hint or "")

    # First pass: canonical names (so they win slots if we hit the cap).
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        if not cmd.native_menu:
            continue
        _add(cmd.name, cmd.description, cmd.args_hint or "")

    # Second pass: aliases.
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        if not cmd.native_menu:
            continue
        for alias in cmd.aliases:
            # Skip aliases that only differ from canonical by case/punctuation
            # normalization (already covered by _add dedup).
            _add(alias, f"Alias for /{cmd.name} — {cmd.description}", cmd.args_hint or "")

    # Third pass: plugin commands.
    for name, description, args_hint in _iter_plugin_command_entries():
        _add(name, description, args_hint or "")

    return entries


def slack_app_manifest(request_url: str = "https://marlow-agent.local/slack/commands") -> dict[str, Any]:
    """Generate a Slack app manifest with all gateway commands as slashes.

    ``request_url`` is required by Slack's manifest schema for every slash
    command, but in Socket Mode (which we use) Slack ignores it and routes
    the command event through the WebSocket. A placeholder URL is fine.

    The returned dict is the ``features.slash_commands`` portion only —
    callers compose it into a full manifest (or merge into an existing
    one). Keeping it narrow avoids coupling us to the rest of the manifest
    schema (display_information, oauth_config, settings, etc.) which users
    set up once in the Slack UI and rarely change.
    """
    slashes = []
    for name, desc, usage in slack_native_slashes():
        entry = {
            "command": f"/{name}",
            "description": desc or f"Run /{name}",
            "should_escape": False,
            "url": request_url,
        }
        if usage:
            entry["usage_hint"] = usage
        slashes.append(entry)
    return {"features": {"slash_commands": slashes}}


def slack_subcommand_map() -> dict[str, str]:
    """Return subcommand -> /command mapping for Slack /marlow handler.

    Maps both canonical names and aliases so /marlow bg do stuff works
    the same as /marlow background do stuff.

    Plugin-registered slash commands are included so ``/marlow <plugin-cmd>``
    routes through the plugin handler.
    """
    overrides = _resolve_config_gates()
    mapping: dict[str, str] = {}
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        mapping[cmd.name] = f"/{cmd.name}"
        for alias in cmd.aliases:
            mapping[alias] = f"/{alias}"
    for name, _description, _args_hint in _iter_plugin_command_entries():
        if name not in mapping:
            mapping[name] = f"/{name}"
    return mapping


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------

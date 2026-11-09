"""Offline sizing of explicit Marlow context; Codex owns its system prompt."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

# The skills index is wrapped in this tag pair inside the stable tier.
_SKILLS_BLOCK_RE = re.compile(r"<available_skills>.*?</available_skills>", re.DOTALL)


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def compute_prompt_breakdown(platform: str = "cli") -> Dict[str, Any]:
    """Offline Marlow context sizes. Codex's own prompt is not measurable here."""
    from agent.codex_environment import managed_environment
    from marlow_cli.config import load_config
    from tools.memory_tool import MemoryStore
    from model_tools import get_tool_definitions
    from agent.codex_bridge import BUSINESS_TOOLS

    e = managed_environment()
    cfg = load_config()
    memory_cfg = cfg.get("memory") or {}
    store = MemoryStore()
    store.load_from_disk()
    memory = (
        store.format_for_system_prompt("memory") or ""
        if memory_cfg.get("memory_enabled", True)
        else ""
    )
    user = (
        store.format_for_system_prompt("user") or ""
        if memory_cfg.get("user_profile_enabled", True)
        else ""
    )
    soul = e.profile / "SOUL.md"
    identity = (
        soul.read_text(encoding="utf-8")[:16000]
        if soul.is_file() and not soul.is_symlink()
        else ""
    )
    tools = [
        tool
        for tool in get_tool_definitions(
            quiet_mode=True, skip_tool_search_assembly=True
        )
        if tool.get("function", {}).get("name") in BUSINESS_TOOLS
    ]
    context = identity + memory + user
    measure = lambda text: {"chars": len(text), "bytes": _bytes(text)}
    return {
        "platform": platform,
        "model": e.config.get("model", ""),
        "system_prompt": measure(context),
        "skills_index": measure(""),
        "memory": measure(memory),
        "user_profile": measure(user),
        "tools": {
            "count": len(tools),
            "json_bytes": _bytes(json.dumps(tools, ensure_ascii=False)),
        },
        "sections": [
            ("Marlow identity", len(identity), _bytes(identity)),
            ("Marlow memory/profile", len(memory + user), _bytes(memory + user)),
        ],
        "codex_prompt": "unknown; Codex owns its prompt and dynamic context",
    }


def _fmt_kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def render_breakdown(data: Dict[str, Any]) -> str:
    """Render the breakdown as plain text suitable for a terminal."""
    lines: List[str] = []
    sp = data["system_prompt"]
    lines.append(
        f"Prompt-size breakdown (platform={data['platform']}, model={data['model'] or 'unset'})"
    )
    lines.append("")
    lines.append(
        f"  Marlow context total: {sp['bytes']:>8,} B  ({_fmt_kb(sp['bytes'])}, {sp['chars']:,} chars)"
    )
    lines.append("  Codex prompt: unknown (owned by Codex)")
    lines.append("")
    lines.append("  Major blocks:")
    si = data["skills_index"]
    mem = data["memory"]
    up = data["user_profile"]
    lines.append(
        f"    skills index       : {si['bytes']:>8,} B  ({_fmt_kb(si['bytes'])})"
    )
    lines.append(
        f"    memory             : {mem['bytes']:>8,} B  ({_fmt_kb(mem['bytes'])})"
    )
    lines.append(
        f"    user profile       : {up['bytes']:>8,} B  ({_fmt_kb(up['bytes'])})"
    )
    lines.append("")
    lines.append("  Prompt tiers:")
    for label, chars, byts in data["sections"]:
        lines.append(f"    {label:<36}: {byts:>8,} B  ({_fmt_kb(byts)})")
    lines.append("")
    tools = data["tools"]
    lines.append(
        f"  Tool schemas         : {tools['json_bytes']:>8,} B  ({_fmt_kb(tools['json_bytes'])}, {tools['count']} tools)"
    )
    return "\n".join(lines)


def cmd_prompt_size(args: Any) -> None:
    """Entry point for ``marlow prompt-size``."""
    platform = getattr(args, "platform", "cli") or "cli"
    as_json = getattr(args, "json", False)
    try:
        data = compute_prompt_breakdown(platform)
    except Exception as e:
        print(f"Could not compute prompt-size breakdown: {e}")
        return
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_breakdown(data))

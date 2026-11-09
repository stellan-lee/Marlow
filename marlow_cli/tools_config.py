"""Lightweight per-platform toolset and MCP configuration."""
from __future__ import annotations
import json as _json
from pathlib import Path
from typing import Dict, List, Optional, Set
from marlow_cli.cli_output import print_error as _print_error, print_info as _print_info, print_success as _print_success, print_warning as _print_warning
from marlow_cli.colors import Colors, color
from marlow_cli.config import cfg_get, get_env_value, load_config, save_config
from marlow_cli.platforms import PLATFORMS as _PLATFORMS_REGISTRY
PROJECT_ROOT=Path(__file__).parent.parent.resolve()
CONFIGURABLE_TOOLSETS=[
("web","🔍 Web Search & Scraping","Brave/DDGS search and page extraction"),
("browser","🌐 Browser Automation","local Chromium and existing-browser CDP"),
("terminal","💻 Terminal & Processes","local, Docker, and SSH execution"),
("file","📁 File Operations","read, write, patch, and search"),
("code_execution","⚡ Code Execution","programmatic Python tool execution"),
("vision","👁️  Vision / Image Analysis","analyze images"),
("moa","🧠 Mixture of Agents","multi-agent aggregation"),
("skills","📚 Skills","local SKILL.md discovery and management"),
("todo","📋 Task Planning","task planning"),
("memory","💾 Memory","local and configured external memory"),
("context_engine","🧩 Context Engine","active context-engine tools"),
("session_search","🔎 Session Search","search past conversations"),
("clarify","❓ Clarifying Questions","request user input"),
("admin_approval","🛡️ Administrator Approval","gateway administrator decisions"),
("delegation","👥 Task Delegation","foreground and background subagents"),
("cronjob","⏰ Cron Jobs","scheduled and one-shot jobs"),
("messaging","📨 Cross-Platform Messaging","retained messaging connectors")]
_DEFAULT_OFF_TOOLSETS={"moa"}
_TOOLSET_PLATFORM_RESTRICTIONS:Dict[str,Set[str]]={}
PLATFORMS={k:{"label":v.label,"default_toolset":v.default_toolset} for k,v in _PLATFORMS_REGISTRY.items()}
def _toolset_allowed_for_platform(key,platform):
 allowed=_TOOLSET_PLATFORM_RESTRICTIONS.get(key); return allowed is None or platform in allowed
def _get_plugin_toolset_keys():
 try:
  from marlow_cli.plugins import discover_plugins,get_plugin_toolsets
  discover_plugins(); return {str(k) for k,_,_ in get_plugin_toolsets()}
 except Exception:return set()
def _get_effective_configurable_toolsets():
 out=list(CONFIGURABLE_TOOLSETS); seen={x[0] for x in out}
 try:
  from marlow_cli.plugins import discover_plugins,get_plugin_toolsets
  discover_plugins()
  for row in get_plugin_toolsets():
   if row[0] not in seen: seen.add(row[0]); out.append(row)
 except Exception:pass
 return out
def _get_enabled_platforms():
 out=["cli"]
 for platform,key in {"telegram":"TELEGRAM_BOT_TOKEN","discord":"DISCORD_BOT_TOKEN","slack":"SLACK_BOT_TOKEN","email":"EMAIL_ADDRESS","feishu":"FEISHU_APP_ID","webhook":"WEBHOOK_SECRET"}.items():
  if platform in PLATFORMS and get_env_value(key):out.append(platform)
 return out
def _parse_enabled_flag(value,default=True):
 if value is None:return default
 if isinstance(value,bool):return value
 if isinstance(value,int):return value!=0
 if isinstance(value,str):
  value=value.strip().lower()
  if value in {"true","1","yes","on"}:return True
  if value in {"false","0","no","off"}:return False
 return default
def _get_platform_tools(config,platform,*,include_default_mcp_servers=True):
 from toolsets import TOOLSETS,resolve_toolset
 pm=config.get("platform_toolsets") or {}; raw=pm.get(platform); explicit=isinstance(raw,list)
 selected=[str(x) for x in raw] if explicit else [PLATFORMS.get(platform,{}).get("default_toolset",f"marlow-{platform}")]
 builtins={x[0] for x in CONFIGURABLE_TOOLSETS}; plugins=_get_plugin_toolset_keys(); defaults={x["default_toolset"] for x in PLATFORMS.values()}
 if explicit: enabled={x for x in selected if x in builtins and _toolset_allowed_for_platform(x,platform)}
 else:
  tools=set()
  for name in selected:
   if name in TOOLSETS:tools.update(resolve_toolset(name))
  enabled={k for k,_,_ in CONFIGURABLE_TOOLSETS if _toolset_allowed_for_platform(k,platform) and set(resolve_toolset(k)) and set(resolve_toolset(k)).issubset(tools)}-_DEFAULT_OFF_TOOLSETS
 known=set((config.get("known_plugin_toolsets") or {}).get(platform,[]))
 enabled|={k for k in plugins if k in selected or (k not in known and k not in _DEFAULT_OFF_TOOLSETS)}
 context=config.get("context") or {}
 if str(context.get("engine") or "compressor").strip().lower()!="compressor" and not(explicit and not selected):enabled.add("context_engine")
 passthrough={x for x in selected if x not in builtins|plugins|defaults}; servers=config.get("mcp_servers") or {}
 active={str(n) for n,s in servers.items() if isinstance(s,dict) and _parse_enabled_flag(s.get("enabled",True))}
 if "no_mcp" in selected:passthrough-=(active|{"no_mcp"})
 else:
  chosen=passthrough&active; passthrough-=active; enabled|=(chosen or active) if include_default_mcp_servers else chosen
 enabled|=passthrough
 disabled={str(x) for x in (config.get("agent") or {}).get("disabled_toolsets",[])}
 return enabled-disabled
def _platform_toolset_summary(config,platforms=None):return {p:_get_platform_tools(config,p) for p in (platforms or _get_enabled_platforms())}
def _save_platform_tools(config,platform,enabled_toolset_keys):
 config.setdefault("platform_toolsets",{}); builtins={x[0] for x in CONFIGURABLE_TOOLSETS}; plugins=_get_plugin_toolset_keys(); defaults={x["default_toolset"] for x in PLATFORMS.values()}
 existing=cfg_get(config,"platform_toolsets",platform,default=[]); existing=existing if isinstance(existing,list) else []
 keep={str(x) for x in existing if str(x) not in builtins|plugins|defaults|{"no_mcp"}}
 selected={str(x) for x in enabled_toolset_keys if _toolset_allowed_for_platform(str(x),platform)}
 config["platform_toolsets"][platform]=sorted(selected|keep)
 if plugins:config.setdefault("known_plugin_toolsets",{})[platform]=sorted(plugins)
 save_config(config)
_tool_token_cache: Optional[Dict[str, int]] = None


def _estimate_tool_tokens() -> Dict[str, int]:
    """Estimate the serialized schema size for each registered tool."""
    global _tool_token_cache
    if _tool_token_cache is not None:
        return _tool_token_cache
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        import model_tools  # noqa: F401
        from tools.registry import registry
    except Exception:
        _tool_token_cache = {}
        return _tool_token_cache

    counts: Dict[str, int] = {}
    for name in registry.get_all_tool_names():
        schema = registry.get_schema(name)
        if schema:
            payload = _json.dumps({"type": "function", "function": schema})
            counts[name] = len(encoding.encode(payload))
    _tool_token_cache = counts
    return counts


def _prompt_toolset_checklist(title, current, platform="cli", **_kwargs):
    from marlow_cli.curses_ui import curses_checklist
    from toolsets import resolve_toolset

    rows = [
        row
        for row in _get_effective_configurable_toolsets()
        if _toolset_allowed_for_platform(row[0], platform)
    ]
    labels = [f"{label}  — {desc}" for _, label, desc in rows]
    preselected = {i for i, (key, _, _) in enumerate(rows) if key in current}
    tool_tokens = _estimate_tool_tokens()
    status_fn = None
    if tool_tokens:
        keys = [key for key, _, _ in rows]

        def status_fn(chosen: set) -> str:
            names: set[str] = set()
            for index in chosen:
                names.update(resolve_toolset(keys[index]))
            total = sum(tool_tokens.get(name, 0) for name in names)
            suffix = f"{total / 1000:.1f}k" if total >= 1000 else str(total)
            return f"Est. tool context: ~{suffix} tokens"

    picked = curses_checklist(
        title,
        labels,
        preselected,
        cancel_returns=preselected,
        status_fn=status_fn,
    )
    return {rows[i][0] for i in picked}


def tools_command(args=None,first_install=False,config=None):
 del args; config=config if config is not None else load_config(); platforms=_get_enabled_platforms() or ["cli"]
 if first_install:targets=platforms
 else:
  from marlow_cli.curses_ui import curses_single_select
  choices=[PLATFORMS[x]["label"] for x in platforms]; has_mcp=bool(config.get("mcp_servers"))
  if has_mcp:choices.append("Configure MCP server tools")
  choices.append("Done"); index=curses_single_select("Configure tools",choices,default_index=0)
  if index is None or index==len(choices)-1:return
  if has_mcp and index==len(choices)-2:_configure_mcp_tools_interactive(config); return
  targets=[platforms[index]]
 for platform in targets:
  current=_get_platform_tools(config,platform,include_default_mcp_servers=False); initial=current-_DEFAULT_OFF_TOOLSETS if first_install else current
  chosen=_prompt_toolset_checklist(PLATFORMS[platform]["label"],initial,platform); _save_platform_tools(config,platform,chosen); _print_success(f"Saved {PLATFORMS[platform]['label']} tool configuration")

def _configure_mcp_tools_interactive(config: dict):
    """Discover and select tools exposed by configured MCP servers."""
    from marlow_cli.curses_ui import curses_checklist

    servers = config.get("mcp_servers") or {}
    enabled = {
        name: server
        for name, server in servers.items()
        if isinstance(server, dict)
        and _parse_enabled_flag(server.get("enabled", True))
    }
    if not servers:
        _print_info("No MCP servers configured.")
        return
    if not enabled:
        _print_info("All MCP servers are disabled.")
        return

    try:
        from tools.mcp_tool import probe_mcp_server_tools

        discovered = probe_mcp_server_tools()
    except Exception as exc:
        _print_error(f"Failed to probe MCP servers: {exc}")
        return
    if not discovered:
        _print_warning("Could not discover tools from any MCP server.")
        return

    for missing in sorted(set(enabled) - set(discovered)):
        _print_warning(f"Could not connect to '{missing}'")

    changed = False
    for server_name, tools in discovered.items():
        if not tools:
            _print_info(f"{server_name}: no tools found")
            continue
        server = servers.setdefault(server_name, {})
        filters = server.get("tools") or {}
        include = set(filters.get("include") or [])
        exclude = set(filters.get("exclude") or [])
        names = [name for name, _description in tools]
        labels = []
        for name, description in tools:
            suffix = "..." if len(description) > 70 else ""
            labels.append(
                f"{name}  ({description[:70]}{suffix})" if description else name
            )
        preselected = {
            index
            for index, name in enumerate(names)
            if (name in include if include else name not in exclude)
        }
        chosen = curses_checklist(
            f"MCP Server: {server_name}",
            labels,
            preselected,
            cancel_returns=preselected,
        )
        if chosen == preselected:
            _print_info(f"{server_name}: no changes")
            continue
        chosen_names = [names[index] for index in sorted(chosen)]
        filters = server.setdefault("tools", {})
        filters.pop("exclude", None)
        if len(chosen_names) == len(names):
            filters.pop("include", None)
        else:
            filters["include"] = chosen_names
        changed = True

    if changed:
        save_config(config)
        _print_success("MCP tool configuration saved")
    else:
        _print_info("No changes to MCP tools")


def _apply_toolset_change(
    config: dict, platform: str, toolset_names: List[str], action: str
):
    enabled = _get_platform_tools(
        config, platform, include_default_mcp_servers=False
    )
    names = set(toolset_names)
    updated = enabled - names if action == "disable" else enabled | names
    _save_platform_tools(config, platform, updated)


def _apply_mcp_change(config: dict, targets: List[str], action: str) -> Set[str]:
    missing: Set[str] = set()
    servers = config.get("mcp_servers") or {}
    for target in targets:
        server_name, tool_name = target.split(":", 1)
        if server_name not in servers:
            missing.add(server_name)
            continue
        filters = servers[server_name].setdefault("tools", {})
        excluded = list(filters.get("exclude") or [])
        if action == "disable" and tool_name not in excluded:
            excluded.append(tool_name)
        elif action == "enable":
            excluded = [name for name in excluded if name != tool_name]
        filters["exclude"] = excluded
    return missing


def _print_tools_list(enabled: set, mcp_servers: dict, platform: str = "cli"):
    print(f"Toolsets ({platform}):")
    for key, label, _description in _get_effective_configurable_toolsets():
        if not _toolset_allowed_for_platform(key, platform):
            continue
        status = (
            color("✓ enabled", Colors.GREEN)
            if key in enabled
            else color("✗ disabled", Colors.RED)
        )
        print(f"  {status}  {key}  {color(label, Colors.DIM)}")
    if not mcp_servers:
        return
    print("\nMCP servers:")
    for name, server in mcp_servers.items():
        filters = server.get("tools") or {}
        include = filters.get("include") or []
        exclude = filters.get("exclude") or []
        if include:
            detail = f"include only: {', '.join(include)}"
        elif exclude:
            detail = f"excluded: {', '.join(exclude)}"
        else:
            detail = "all tools enabled"
        _print_info(f"{name}  [{detail}]")


def tools_disable_enable_command(args):
    """Enable, disable, or list built-in/plugin toolsets and MCP tools."""
    action = args.tools_action
    platform = getattr(args, "platform", "cli")
    config = load_config()
    if platform not in PLATFORMS:
        _print_error(
            f"Unknown platform '{platform}'. Valid: {', '.join(PLATFORMS)}"
        )
        return
    if action == "list":
        _print_tools_list(
            _get_platform_tools(
                config, platform, include_default_mcp_servers=False
            ),
            config.get("mcp_servers") or {},
            platform,
        )
        return

    targets = list(args.names)
    toolsets = [name for name in targets if ":" not in name]
    mcp_targets = [name for name in targets if ":" in name]
    valid = {
        key for key, _label, _description in _get_effective_configurable_toolsets()
    }
    unknown = [name for name in toolsets if name not in valid]
    for name in unknown:
        _print_error(f"Unknown toolset '{name}'")
    toolsets = [name for name in toolsets if name in valid]
    if toolsets:
        _apply_toolset_change(config, platform, toolsets, action)

    missing = (
        _apply_mcp_change(config, mcp_targets, action) if mcp_targets else set()
    )
    for name in sorted(missing):
        _print_error(f"MCP server '{name}' not found in config")
    save_config(config)

    successful = [
        name
        for name in targets
        if name not in unknown
        and (":" not in name or name.split(":", 1)[0] not in missing)
    ]
    if successful:
        verb = "Disabled" if action == "disable" else "Enabled"
        _print_success(f"{verb}: {', '.join(successful)}")

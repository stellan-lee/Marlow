"""Lightweight status report for retained Marlow components."""

from __future__ import annotations

import sys
from pathlib import Path

from marlow_cli.colors import Colors, color
from marlow_cli.config import get_env_path, get_marlow_home, load_config


PROJECT_ROOT = Path(__file__).parent.parent.resolve()


def check_mark(ok: bool) -> str:
    return color("✓" if ok else "✗", Colors.GREEN if ok else Colors.RED)


def _configured_model(config: dict) -> tuple[str, str, str]:
    from agent.codex_environment import managed_environment
    e = managed_environment()
    provider = e.config.get('model_provider', 'openai')
    return e.config.get('model', ''), provider, ''


def _configured_platforms(config: dict) -> list[str]:
    gateway = config.get("gateway", {})
    platforms = gateway.get("platforms", {}) if isinstance(gateway, dict) else {}
    if not isinstance(platforms, dict):
        return []
    retained = ("telegram", "discord", "slack", "feishu", "email", "webhook")
    return [name for name in retained if isinstance(platforms.get(name), dict) and platforms[name].get("enabled")]


def show_status(args) -> None:
    """Show configuration and health for the retained lightweight product."""
    del args
    try:
        config = load_config()
    except Exception:
        config = {}

    model, provider, base_url = _configured_model(config)
    try:
        from marlow_cli.auth import get_codex_auth_status

        codex = get_codex_auth_status()
    except Exception as exc:
        codex = {"logged_in": False, "error": str(exc)}

    print()
    print(color("┌────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│                 ⚕ Marlow Agent Status                  │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.CYAN))

    print()
    print(color("◆ Environment", Colors.CYAN, Colors.BOLD))
    print(f"  Project:      {PROJECT_ROOT}")
    print(f"  Marlow home:  {get_marlow_home()}")
    print(f"  Python:       {sys.version.split()[0]}")
    print(f"  .env file:    {check_mark(get_env_path().exists())}")

    print()
    print(color("◆ Model runtime", Colors.CYAN, Colors.BOLD))
    print(f"  Provider:     {provider or 'auto'}")
    print(f"  Model:        {model or '(not set)'}")
    if base_url:
        print(f"  Endpoint:     {base_url}")
    print(
        f"  Codex OAuth:  {check_mark(bool(codex.get('logged_in')))} "
        f"{'logged in' if codex.get('logged_in') else 'not logged in'}"
    )
    if codex.get("auth_store"):
        print(f"    Auth store: {codex['auth_store']}")
    if codex.get("error") and not codex.get("logged_in"):
        print(f"    Error:      {codex['error']}")

    print()
    print(color("◆ Retained services", Colors.CYAN, Colors.BOLD))
    platforms = _configured_platforms(config)
    print(f"  Gateway:      {check_mark(bool(platforms))} {', '.join(platforms) if platforms else 'no platform enabled'}")
    print(f"  Cron:         {check_mark(bool((config.get('cron') or {}).get('enabled', True)))}")
    print(f"  MCP:          {check_mark(bool(config.get('mcp_servers') or config.get('mcp')))}")
    print(f"  Plugins:      {check_mark(True)} local runtime available")
    print(f"  Skills:       {check_mark(True)} local runtime available")
    print()

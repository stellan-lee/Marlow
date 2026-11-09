"""Codex-native login/logout in the active Marlow Profile's isolated HOME."""

from __future__ import annotations
from typing import Any
import shutil
import subprocess
from agent.codex_environment import CodexEnvironment


class AuthError(RuntimeError):
    def __init__(self, message, *, provider="", code=None, relogin_required=False):
        super().__init__(message)
        self.provider, self.code, self.relogin_required = (
            provider,
            code,
            relogin_required,
        )


_PLACEHOLDER_SECRET_VALUES = {
    "*",
    "**",
    "***",
    "changeme",
    "your_api_key",
    "your_api_key_here",
    "your-api-key",
    "placeholder",
    "example",
    "dummy",
    "null",
    "none",
}


def has_usable_secret(value: Any, *, min_length: int = 4) -> bool:
    """Return True when a configured secret looks usable, not empty/placeholder."""
    if not isinstance(value, str):
        return False
    cleaned = value.strip()
    if len(cleaned) < min_length:
        return False
    if cleaned.lower() in _PLACEHOLDER_SECRET_VALUES:
        return False
    return True


def environment():
    from marlow_cli.config import load_config

    return CodexEnvironment.build(
        settings=(load_config().get("runtime") or {}).get("codex") or {}
    )


def _run_auth(command):
    e = environment()
    from marlow_cli.config import load_config

    binary = ((load_config().get("runtime") or {}).get("codex") or {}).get(
        "binary"
    ) or shutil.which("codex")
    if not binary:
        raise AuthError("Install Codex CLI 0.159.2 before login", code="codex_missing")
    from agent.transports.codex_app_server import check_codex_binary

    ok, message = check_codex_binary(binary, environment=e.env)
    if not ok:
        raise AuthError(message, code="unsupported_codex")
    return subprocess.run(
        [binary, *e.arguments(), *command], env=e.env, cwd=e.workspace, check=False
    ).returncode


def login_command(args):
    # No token reads/copies, home imports, OS keyring or custom OAuth client.
    code = _run_auth(["login"])
    if code:
        raise SystemExit(code)


def logout_command(args):
    code = _run_auth(["logout"])
    if code:
        raise SystemExit(code)


def get_codex_auth_status():
    from agent.transports.codex_app_server import CodexAppServerClient

    e = environment()
    from marlow_cli.config import load_config

    binary = ((load_config().get("runtime") or {}).get("codex") or {}).get(
        "binary"
    ) or shutil.which("codex")
    if not binary:
        return {"logged_in": False, "status": "codex_missing"}
    try:
        with CodexAppServerClient(codex_bin=binary, environment=e) as client:
            client.initialize()
            result = client.request("account/read", {"refreshToken": False}, timeout=10)
        ready = bool(result.get("account")) or result.get("requiresOpenaiAuth") is False
        return {"logged_in": ready, "status": "ready" if ready else "auth_required"}
    except Exception:
        return {"logged_in": False, "status": "unavailable"}


def get_auth_status(provider_id=None):
    return get_codex_auth_status()


def get_active_provider():
    return environment().config.get("model_provider", "openai")

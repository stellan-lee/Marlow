"""Codex-native login/logout in the active Marlow Profile's isolated HOME."""

from __future__ import annotations
from typing import Any
import subprocess
from agent.codex_environment import CodexEnvironment, resolve_codex_binary


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

    try:
        binary = resolve_codex_binary((load_config().get("runtime") or {}).get("codex") or {})
    except FileNotFoundError as exc:
        raise AuthError(str(exc), code="codex_missing") from exc
    from agent.transports.codex_app_server import check_codex_binary

    ok, message = check_codex_binary(binary, environment=e.env)
    if not ok:
        raise AuthError(e.redact(message), code="invalid_codex")
    try:
        return subprocess.run(
            [binary, *e.arguments(), *command], env=e.env, cwd=e.workspace, check=False
        ).returncode
    except OSError as exc:
        raise AuthError(e.redact(str(exc)), code="invalid_codex") from exc


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
    from agent.transports.codex_app_server_session import validate_codex_runtime
    from agent.redact import redact_sensitive_text
    from marlow_cli.config import load_config

    e = None
    version = None
    try:
        settings = (load_config().get("runtime") or {}).get("codex") or {}
        try:
            binary = resolve_codex_binary(settings)
        except FileNotFoundError as exc:
            return {
                "logged_in": False, "status": "codex_missing",
                "error": redact_sensitive_text(str(exc), force=True),
            }
        e = environment()
        with CodexAppServerClient(codex_bin=binary, environment=e) as client:
            version = client.version
            validate_codex_runtime(client, e, cwd=str(e.workspace))
            result = client.request("account/read", {"refreshToken": False}, timeout=10)
        ready = bool(result.get("account")) or result.get("requiresOpenaiAuth") is False
        return {
            "logged_in": ready,
            "status": "ready" if ready else "auth_required",
            "version": version,
        }
    except Exception as exc:
        message = e.redact(str(exc)) if e is not None else redact_sensitive_text(str(exc), force=True)
        return {
            "logged_in": False,
            "status": "incompatible",
            "version": version,
            "error": message,
        }


def get_auth_status(provider_id=None):
    return get_codex_auth_status()


def get_active_provider():
    return environment().config.get("model_provider", "openai")

"""Persistent profile-local Codex environment. No ambient credential discovery.

This controls configuration/discovery, not OS-level filesystem isolation. System
requirements remain authoritative and must be reported by the runtime.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import tomllib
from dataclasses import dataclass
from typing import Mapping

from marlow_constants import get_marlow_home

SUPPORTED_CODEX_VERSION = (0, 159, 2)
# Codex owns inference configuration; executable extensions and permission
# configuration are host policy, never accepted from model configuration.
MODEL_KEYS = frozenset({
    "model",
    "model_provider",
    "model_providers",
    "model_reasoning_effort",
    "model_reasoning_summary",
    "model_verbosity",
    "model_context_window",
    "model_auto_compact_token_limit",
    "service_tier",
    "notice",
})
FORBIDDEN_ENV = frozenset({
    "HOME",
    "CODEX_HOME",
    "MARLOW_HOME",
    "SHELL",
    "ENV",
    "BASH_ENV",
    "ZDOTDIR",
    "PYTHONPATH",
    "PYTHONHOME",
    "NODE_OPTIONS",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "OPENAI_BASE_URL",
    "CODEX_CONFIG",
    "CODEX_MANAGED_CONFIG",
})


class CodexConfigurationError(ValueError):
    pass


class CodexAuthenticationError(PermissionError):
    pass


def _private_directory(path: Path) -> Path:
    # Check every existing component, including broken symlinks. Resolving a
    # path first would hide precisely the links this boundary must reject.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise CodexConfigurationError(
                f"Runtime path must not contain symlinks: {part}"
            )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@dataclass(frozen=True)
class CodexEnvironment:
    profile: Path
    home: Path
    codex_home: Path
    workspace: Path
    env: dict[str, str]
    config: dict
    projects: tuple[tuple[str, bool], ...] = ()
    execution_network: bool = False

    @classmethod
    def build(
        cls,
        profile: Path | None = None,
        *,
        settings: Mapping | None = None,
        parent: Mapping[str, str] | None = None,
        workspace_scope: str | None = None,
    ) -> "CodexEnvironment":
        parent = os.environ if parent is None else parent
        settings = settings or {}
        profile = Path(os.path.abspath(profile or get_marlow_home()))
        home = _private_directory(profile / "runtime" / "home")
        codex_home = _private_directory(profile / "runtime" / "codex")
        if workspace_scope is not None and (
            len(workspace_scope) != 32
            or any(char not in "0123456789abcdef" for char in workspace_scope)
        ):
            raise CodexConfigurationError("Invalid host workspace scope")
        workspace_base = _private_directory(profile / "workspace")
        workspace = (
            _private_directory(workspace_base / "conversations" / workspace_scope)
            if workspace_scope
            else workspace_base
        )
        # Bound project discovery before any ancestor containing personal
        # .agents/.codex instructions; never change cwd to an authorized project.
        marker = workspace / ".marlow-runtime-root"
        marker.touch(mode=0o600, exist_ok=True)
        for name in (".agents", ".codex", "AGENTS.md", "AGENTS.override.md"):
            if (workspace / name).exists() or (workspace / name).is_symlink():
                raise CodexConfigurationError(
                    f"Workspace discovery source is not authorized: {name}"
                )
        for root in (home, codex_home):
            for child in root.rglob("*"):
                if child.is_symlink():
                    relative = child.relative_to(root)
                    # Codex's own PATH helper aliases are not configuration,
                    # credentials or instruction discovery sources.
                    if len(relative.parts) > 1 and relative.parts[0] == ".tmp":
                        continue
                    raise CodexConfigurationError(
                        f"Runtime state must not contain symlinks: {relative}"
                    )
        config_path = codex_home / "config.toml"
        if config_path.is_symlink():
            raise CodexConfigurationError("Codex config must not be a symlink")
        if not config_path.exists():
            try:
                fd = os.open(config_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                # Another process may have initialized this profile first.
                if config_path.is_symlink():
                    raise CodexConfigurationError("Codex config must not be a symlink")
            else:
                os.close(fd)
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise CodexConfigurationError("Invalid profile Codex config.toml") from exc
        for key in ("model", "model_provider"):
            if key in config and (
                not isinstance(config[key], str) or "\n" in config[key]
            ):
                raise CodexConfigurationError(
                    "Native Codex " + key + " must be a single-line string"
                )
        if not isinstance(config.get("model_providers", {}), dict):
            raise CodexConfigurationError("Native model_providers must be a TOML table")
        unsupported = config.keys() - MODEL_KEYS
        if unsupported:
            raise CodexConfigurationError(
                "Profile Codex config contains host-controlled fields: "
                + ", ".join(sorted(unsupported))
            )
        for provider in config.get("model_providers", {}).values():
            if (
                not isinstance(provider, dict)
                or provider.get("wire_api", "responses") != "responses"
            ):
                raise CodexConfigurationError(
                    "Custom Codex providers require the Responses protocol with tools"
                )
            if any(k in provider for k in ("auth", "aws", "gateway_oauth")):
                raise CodexConfigurationError(
                    "Command/ambient provider authentication is not authorized; use env_key"
                )
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "MARLOW_HOME": str(profile),
            "PATH": os.defpath,
            "SHELL": "/bin/sh",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "RUST_LOG": "warn",
        }
        # Explicit operator opt-in for proxy, CA, SSH agent, CLI path and
        # provider env_key. No wildcard or implicit OpenAI/Codex overrides.
        for name in settings.get("pass_env", []):
            if (
                not isinstance(name, str)
                or not name.isidentifier()
                or name in FORBIDDEN_ENV
                or name.startswith(("CODEX_", "XDG_"))
            ):
                raise CodexConfigurationError("Disallowed runtime pass_env name")
            if name in parent:
                env[name] = parent[name]
        for provider in config.get("model_providers", {}).values():
            name = provider.get("env_key")
            if name and not env.get(name):
                raise CodexConfigurationError(
                    f"Provider credential {name} missing; explicitly allow it in runtime.codex.pass_env"
                )
        projects = []
        for entry in settings.get("projects", []):
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise CodexConfigurationError(
                    "Authorized projects need a path and explicit writable flag"
                )
            path = Path(entry["path"])
            if (
                not path.is_absolute()
                or not path.is_dir()
                or any(p.is_symlink() for p in (path, *path.parents))
            ):
                raise CodexConfigurationError(
                    "Authorized project must be an absolute directory without symlinks"
                )
            if (
                profile == path
                or profile.is_relative_to(path)
                or (
                    path.is_relative_to(profile)
                    and not path.is_relative_to(workspace_base)
                )
            ):
                raise CodexConfigurationError(
                    "A project grant cannot include runtime/profile state"
                )
            from agent.file_safety import is_write_denied

            if path in {
                Path("/"),
                Path("/etc"),
                Path("/usr"),
                Path("/var"),
                Path("/bin"),
                Path("/sbin"),
                Path("/System"),
                Path("/Library"),
                Path.home().resolve(),
            }:
                raise CodexConfigurationError(
                    "System/personal home roots cannot be project grants"
                )
            if entry.get("writable") is True and is_write_denied(str(path)):
                raise CodexConfigurationError(
                    "Project grant conflicts with Marlow file safety policy"
                )
            projects.append((str(path), entry.get("writable") is True))
        return cls(
            profile,
            home,
            codex_home,
            workspace,
            env,
            config,
            tuple(projects),
            settings.get("execution_network") is True,
        )

    def overrides(self) -> dict:
        # Provider credentials are for Codex's network client, never tool shells.
        secret_names = set()
        for provider in self.config.get("model_providers", {}).values():
            secret_names.add(provider.get("env_key"))
            secret_names.update(provider.get("env_http_headers", {}).values())
        shell_env = {
            k: v
            for k, v in self.env.items()
            if k not in secret_names
            and not any(
                x in k.upper()
                for x in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL")
            )
        }
        filesystem = {
            ":minimal": "read",
            str(self.profile): "deny",
            str(self.home): "read",
            str(self.workspace): "write",
        }
        filesystem.update({
            path: "write" if writable else "read" for path, writable in self.projects
        })
        # Preserve Marlow's mandatory sensitive-path and write-safe-root rules
        # in the OS sandbox too, including inside broader project grants.
        from agent.file_safety import (
            build_write_denied_paths,
            build_write_denied_prefixes,
        )

        personal_home = str(Path.home().resolve())
        writable_roots = [
            self.workspace,
            *(Path(path) for path, writable in self.projects if writable),
        ]
        for denied in [
            *build_write_denied_paths(personal_home),
            *build_write_denied_prefixes(personal_home),
        ]:
            protected = Path(str(denied).rstrip("/"))
            if any(
                protected == root or protected.is_relative_to(root)
                for root in writable_roots
            ):
                filesystem[str(protected)] = "read"
        safe_root = os.environ.get("MARLOW_WRITE_SAFE_ROOT")
        if safe_root:
            allowed_write_root = Path(safe_root).expanduser().resolve()
            for root in [
                self.workspace,
                *(Path(path) for path, writable in self.projects if writable),
            ]:
                if root != allowed_write_root and not root.is_relative_to(
                    allowed_write_root
                ):
                    filesystem[str(root)] = "read"

        import sys

        filesystem[str(Path(sys.prefix).resolve())] = "read"
        filesystem[str(Path(sys.base_prefix).resolve())] = "read"
        return {
            "cli_auth_credentials_store": "file",
            "mcp_oauth_credentials_store": "file",
            "allow_login_shell": False,
            "allow_symlinked_codex_home": False,
            "shell_environment_policy": {
                "inherit": "none",
                "set": shell_env,
                "experimental_use_profile": False,
            },
            "project_root_markers": [".marlow-runtime-root"],
            "project_doc_max_bytes": 0,
            "skills.bundled.enabled": False,
            "skills.include_instructions": False,
            "features.skip_host_skill_discovery": True,
            "features.shell_tool": False,
            "features.view_image": False,
            "features.shell_snapshot": False,
            "features.shell_snapshot_v2": False,
            "features.hooks": False,
            "features.codex_hooks": False,
            "features.plugin_hooks": False,
            "features.plugins": False,
            "features.remote_plugin": False,
            "features.recommended_plugins": False,
            "features.apps": False,
            "features.connectors": False,
            "features.computer_use": False,
            "features.browser_use": False,
            "features.js_repl": False,
            "features.image_generation": False,
            "features.imagegenext": False,
            "features.realtime_conversation": False,
            "features.in_app_dictation": False,
            "features.multi_agent": False,
            "features.multi_agent_v2": {"enabled": False},
            "features.responses_websockets": False,
            "features.responses_websockets_v2": False,
            "features.memories": False,
            "features.memory_tool": False,
            "features.goals": False,
            "features.telepathy": False,
            "features.send_async_message": False,
            "features.remote_control": False,
            "features.skill_mcp_dependency_install": False,
            "features.skill_env_var_dependency_prompt": False,
            "features.daemon_auto_start": False,
            "web_search": "disabled",
            "permissions": {
                "marlow": {
                    "filesystem": filesystem,
                    "network": {"enabled": self.execution_network},
                }
            },
            "default_permissions": "marlow",
            "approval_policy": "on-request",
            "approvals_reviewer": "user",
        }

    def arguments(self) -> list[str]:
        return [
            arg
            for key, value in self.overrides().items()
            for arg in ("-c", f"{key}={_toml(value)}")
        ]

    def redact(self, text):
        from agent.redact import redact_sensitive_text

        result = redact_sensitive_text(str(text), force=True)
        for provider in self.config.get("model_providers", {}).values():
            names = [
                provider.get("env_key"),
                *(provider.get("env_http_headers") or {}).values(),
            ]
            values = [self.env.get(name) for name in names if name]
            values += list((provider.get("http_headers") or {}).values())
            for value in values:
                if isinstance(value, str) and len(value) >= 6:
                    result = result.replace(value, "[REDACTED]")
        return result

    def login_hint(self) -> str:
        return "Run `marlow login` for this Marlow profile (Codex credentials are independent)."


def _toml(value) -> str:
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(f"{json.dumps(k)} = {_toml(v)}" for k, v in value.items())
            + "}"
        )
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def managed_environment():
    from marlow_cli.config import load_config

    settings = (load_config().get("runtime") or {}).get("codex") or {}
    return CodexEnvironment.build(settings=settings)


def conversation_workspace_scope(*, channel=None, user=None, chat=None, thread=None):
    """Host routing identity selects a stable private default workspace."""
    if not any(value is not None for value in (user, chat, thread)):
        return None  # local profile owner
    import hashlib

    identity = json.dumps(
        [
            str(getattr(value, "value", value)) if value is not None else None
            for value in (channel, user, chat, thread)
        ],
        sort_keys=True,
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def gateway_shared(source, config=None):
    from gateway.session import is_shared_multi_user_session

    if config is None:
        from gateway.config import load_gateway_config

        config = load_gateway_config()
    return is_shared_multi_user_session(
        source,
        group_sessions_per_user=getattr(config, "group_sessions_per_user", True),
        thread_sessions_per_user=getattr(config, "thread_sessions_per_user", False),
    )


def select_project_grants(environment, selected=None, explicit=()):
    from dataclasses import replace

    candidate = Path(selected).resolve() if selected else None
    roots = {str(Path(entry["path"]).resolve()) for entry in explicit}
    grants = tuple(
        (path, writable)
        for path, writable in environment.projects
        if path in roots
        or (
            candidate
            and (candidate == Path(path) or candidate.is_relative_to(Path(path)))
        )
    )
    return replace(environment, projects=grants)


def gateway_environment(source):
    from marlow_cli.config import load_config

    settings = (load_config().get("runtime") or {}).get("codex") or {}
    scope = conversation_workspace_scope(
        channel=source.platform,
        user=None if gateway_shared(source) else source.user_id,
        chat=source.chat_id,
        thread=source.thread_id,
    )
    environment = CodexEnvironment.build(settings=settings, workspace_scope=scope)
    return select_project_grants(environment, settings.get("project"))

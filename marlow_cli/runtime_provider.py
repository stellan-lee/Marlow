"""Codex-native configuration selection; never resolves model credentials."""

from __future__ import annotations
from agent.codex_environment import CodexEnvironment, CodexConfigurationError


def resolve_runtime_provider(
    requested=None,
    *,
    explicit_base_url=None,
    explicit_api_key=None,
    target_model=None,
    **kwargs,
):
    from marlow_cli.config import read_raw_config, load_config

    config = load_config()
    raw = read_raw_config() or {}
    legacy = [
        k
        for k in (
            "providers",
            "custom_providers",
            "fallback_providers",
            "auxiliary",
            "smart_model_routing",
            "compression",
            "context",
        )
        if raw.get(k)
    ]
    if (raw.get("delegation") or {}).get("subagent_auto_approve"):
        raise CodexConfigurationError(
            "delegation.subagent_auto_approve is retired. Remove it and use an operation-bound human approval channel; unattended dangerous operations are denied."
        )
    model = raw.get("model")
    if model:
        legacy.append("model")
    if legacy:
        raise CodexConfigurationError(
            "Retired inference settings: "
            + ", ".join(legacy)
            + ". Keep this file as a backup; move model/provider settings to runtime/codex/config.toml and remove retired keys. History is preserved."
        )
    if explicit_base_url or explicit_api_key:
        raise CodexConfigurationError(
            "Provider URL/credentials belong in the profile Codex config.toml"
        )
    settings = (config.get("runtime") or {}).get("codex") or {}
    env = CodexEnvironment.build(settings=settings)
    provider = env.config.get("model_provider", "openai")
    if requested not in (None, "", "auto", "codex", "openai-codex", provider):
        raise CodexConfigurationError(
            "Select the provider in the profile Codex config.toml"
        )
    return {
        "provider": provider,
        "api_mode": "codex_app_server",
        "model": target_model or env.config.get("model", ""),
        "api_key": None,
        "base_url": None,
        "name": provider,
    }


def resolve_requested_provider(requested=None):
    return resolve_runtime_provider(requested)["provider"]


def format_runtime_provider_error(exc):
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(str(exc), force=True)

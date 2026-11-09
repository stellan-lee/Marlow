"""Explicit model selection through canonical Codex configuration."""

from dataclasses import dataclass
import shlex


@dataclass
class ModelSwitchResult:
    """Result of a model switch attempt."""

    success: bool
    new_model: str = ""
    target_provider: str = ""
    provider_changed: bool = False
    api_key: str = ""
    base_url: str = ""
    api_mode: str = ""
    error_message: str = ""
    warning_message: str = ""
    provider_label: str = ""
    resolved_via_alias: str = ""
    is_global: bool = False


def parse_model_flags(raw_args: str) -> tuple[str, str, bool, bool]:
    """Parse --provider, --global, and --refresh flags from /model command args.

    Returns (model_input, explicit_provider, is_global, force_refresh).

    Examples::

        "sonnet"                         -> ("sonnet", "", False, False)
        "sonnet --global"                -> ("sonnet", "", True, False)
        "qwen --provider local"          -> ("qwen", "local", False, False)
        "--provider my-ollama"           -> ("", "my-ollama", False, False)
        "--refresh"                      -> ("", "", False, True)
        "qwen --provider local --global" -> ("qwen", "local", True, False)
    """
    is_global = False
    explicit_provider = ""
    force_refresh = False

    # Normalize Unicode dashes (Telegram/iOS auto-converts -- to em/en dash)
    # A single Unicode dash before a flag keyword becomes "--"
    import re as _re

    raw_args = _re.sub(
        r"[\u2012\u2013\u2014\u2015](provider|global|refresh)", r"--\1", raw_args
    )

    # Extract --global
    if "--global" in raw_args:
        is_global = True
        raw_args = raw_args.replace("--global", "").strip()

    # Extract --refresh (bust the model picker disk cache before listing)
    if "--refresh" in raw_args:
        force_refresh = True
        raw_args = raw_args.replace("--refresh", "").strip()

    # Extract --provider <name>
    parts = raw_args.split()
    i = 0
    filtered: list[str] = []
    while i < len(parts):
        if parts[i] == "--provider" and i + 1 < len(parts):
            explicit_provider = parts[i + 1]
            i += 2
        else:
            filtered.append(parts[i])
            i += 1

    model_input = " ".join(filtered).strip()
    return (model_input, explicit_provider, is_global, force_refresh)


def switch_model(
    raw_input,
    current_provider="",
    current_model="",
    current_base_url="",
    current_api_key="",
    is_global=False,
    explicit_provider="",
    **kwargs,
):
    from agent.codex_environment import managed_environment

    e = managed_environment()
    provider = e.config.get("model_provider", "openai")
    if explicit_provider and explicit_provider != provider:
        return ModelSwitchResult(
            False, error_message="Edit model_provider in the profile Codex config.toml"
        )
    if not raw_input.strip():
        return ModelSwitchResult(
            False, error_message="Specify a Codex model identifier"
        )
    if is_global:
        from marlow_cli.codex_config import set_model

        set_model(raw_input)
    return ModelSwitchResult(
        True,
        new_model=raw_input.strip(),
        target_provider=provider,
        api_mode="codex_app_server",
        provider_label=provider,
        is_global=is_global,
    )


def list_authenticated_providers(**kwargs):
    from marlow_cli.models import list_available_providers

    return [dict(p, models=[], authenticated=True) for p in list_available_providers()]


list_picker_providers = list_authenticated_providers


def resolve_display_context_length(*args, **kwargs):
    return None

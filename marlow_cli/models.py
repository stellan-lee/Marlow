"""Model metadata comes from the active profile's Codex configuration/server."""

from typing import NamedTuple
from agent.codex_environment import managed_environment


class ProviderEntry(NamedTuple):
    slug: str
    label: str
    tui_desc: str


CANONICAL_PROVIDERS = []
_PROVIDER_MODELS = {}
_PROVIDER_LABELS = {}


def normalize_provider(provider):
    return provider or managed_environment().config.get("model_provider", "openai")


def provider_label(provider):
    return provider or "Codex"


def get_default_model_for_provider(provider):
    return managed_environment().config.get("model", "")


def provider_model_ids(provider=None, **kwargs):
    from marlow_cli.codex_config import list_models

    return list_models()


curated_models_for_provider = provider_model_ids
cached_provider_model_ids = provider_model_ids


def clear_provider_models_cache(provider=None):
    pass


def detect_provider_for_model(model, current_provider=""):
    return normalize_provider(current_provider), model


detect_static_provider_for_model = detect_provider_for_model


def get_pricing_for_provider(*args, **kwargs):
    return {}


def _format_price_per_mtok(value):
    return str(value)


def list_available_providers():
    e = managed_environment()
    provider = e.config.get("model_provider", "openai")
    return [{"slug": provider, "label": provider}]

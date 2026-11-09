"""Display helpers for Codex-owned provider IDs. No provider registry/router."""


def normalize_provider(name):
    return name or "openai"


def get_label(provider_id):
    return provider_id or "Codex"


def determine_api_mode(provider=None, base_url=""):
    return "codex_app_server"

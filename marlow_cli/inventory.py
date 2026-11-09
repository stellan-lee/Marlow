"""Provider and model inventory for operator pickers.

Before this module the three call-sites each duplicated:

1. The config slice that pulls model settings and canonical ``providers:``
   entries out of ``load_config()``;
2. The call into ``list_authenticated_providers`` with the resulting kwargs;
3. A post-pass that merges authenticated rows with
   unconfigured ``CANONICAL_PROVIDERS`` rows and emits ``authenticated``/
   ``auth_type``/``key_env``/``warning`` hints for the picker UI.

The canonical merge keys on slug rather than ``is_user_defined`` to decide
  ordering. Section 3 of ``list_authenticated_providers`` sets
  ``is_user_defined=True`` even for canonical slugs that appear in the
  ``providers:`` config dict. ``_reorder_canonical`` avoids demoting them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional


# ─── Public types ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConfigContext:
    """Snapshot of the model + provider config every inventory caller
    needs. Built once via ``load_picker_context()``; the TUI overlays
    live agent state via ``with_overrides()`` before passing through.
    """

    current_provider: str
    current_model: str
    current_base_url: str
    user_providers: dict
    custom_providers: list

    def with_overrides(
        self,
        *,
        current_provider: Optional[str] = None,
        current_model: Optional[str] = None,
        current_base_url: Optional[str] = None,
    ) -> "ConfigContext":
        """Return a copy with truthy overrides applied.

        Truthy-only because the TUI reads agent attributes that may be
        empty strings before an agent is spawned — empties must NOT
        clobber the disk-config values.
        """
        kw: dict = {}
        if current_provider:
            kw["current_provider"] = current_provider
        if current_model:
            kw["current_model"] = current_model
        if current_base_url:
            kw["current_base_url"] = current_base_url
        return replace(self, **kw) if kw else self


def load_picker_context() -> ConfigContext:
    """Load the disk-config snapshot every consumer needs.

    Centralizes the config slice used by TUI model-selection handlers.
    """
    from agent.codex_environment import managed_environment
    e = managed_environment()
    provider = e.config.get('model_provider', 'openai')
    return ConfigContext(provider, e.config.get('model', ''), '', {}, [])


# ─── Public: payload builder ────────────────────────────────────────────


def build_models_payload(
    ctx: ConfigContext,
    *,
    include_unconfigured: bool = False,
    picker_hints: bool = False,
    canonical_order: bool = False,
    pricing: bool = False,
    capabilities: bool = False,
    max_models: int = 50,
) -> dict:
    """Build the ``{providers, model, provider}`` shape every consumer
    needs from a single substrate call.

    Flags:
    - ``include_unconfigured``: append ``CANONICAL_PROVIDERS`` rows that
      ``list_authenticated_providers`` didn't emit (TUI uses this to show
      the full provider universe in the picker).
    - ``picker_hints``: add ``authenticated``/``auth_type``/``key_env``/
      ``warning`` per row (TUI ``ModelPickerDialog`` shape).
    - ``canonical_order``: reorder canonical-slug rows to
      ``CANONICAL_PROVIDERS`` declaration order; truly-custom rows go
      last (TUI display order).
    - ``pricing``: enrich each row with formatted per-model pricing and,
      so interactive pickers can show $/Mtok columns. This may add a
      provider metadata request and should only be set for pickers.
    - ``capabilities``: add a per-row ``capabilities`` map
      ``{model: {reasoning}}`` so pickers can gate model options to what
      each model supports.
    """
    from marlow_cli.model_switch import list_authenticated_providers

    rows = list_authenticated_providers(
        current_provider=ctx.current_provider,
        current_base_url=ctx.current_base_url,
        current_model=ctx.current_model,
        user_providers=ctx.user_providers,
        custom_providers=ctx.custom_providers,
        max_models=max_models,
    )

    if include_unconfigured:
        rows = list(rows) + _append_unconfigured_rows(rows, ctx)
    if picker_hints:
        _apply_picker_hints(rows)
    if canonical_order:
        rows = _reorder_canonical(rows)
    if pricing:
        _apply_pricing(rows)
    if capabilities:
        _apply_capabilities(rows)

    return {
        "providers": rows,
        "model": ctx.current_model,
        "provider": ctx.current_provider,
    }


def _apply_capabilities(rows: list[dict]) -> None:
    """Attach reasoning capability hints to each provider row."""
    for row in rows:
        caps: dict[str, dict[str, bool]] = {}

        for model in row.get("models") or []:
            caps[model] = {
                "reasoning": True,
            }

        row["capabilities"] = caps


# ─── Internal: row post-processing ──────────────────────────────────────


def _append_unconfigured_rows(rows: list[dict], ctx: ConfigContext) -> list[dict]:
    """Build skeleton rows for canonical providers missing from ``rows``."""
    from marlow_cli.models import CANONICAL_PROVIDERS, _PROVIDER_LABELS

    seen = {r["slug"].lower() for r in rows}
    cur = (ctx.current_provider or "").lower()
    extras: list[dict] = []
    for entry in CANONICAL_PROVIDERS:
        if entry.slug.lower() in seen:
            continue
        extras.append(
            {
                "slug": entry.slug,
                "name": _PROVIDER_LABELS.get(entry.slug, entry.label),
                "is_current": entry.slug.lower() == cur,
                "is_user_defined": False,
                "models": [],
                "total_models": 0,
                "source": "canonical",
            }
        )
    return extras


def _apply_picker_hints(rows):
    for row in rows:
        row["auth_type"] = "codex"
        row["key_env"] = ""
        row["warning"] = "Authentication is managed by the active Marlow Profile Codex runtime."


def _reorder_canonical(rows: list[dict]) -> list[dict]:
    """Canonical slugs in ``CANONICAL_PROVIDERS`` declaration order;
    truly-custom rows last.

    Keys on slug membership, NOT ``is_user_defined`` — section 3 of
    ``list_authenticated_providers`` sets ``is_user_defined=True`` on
    rows from the ``providers:`` config dict even when the slug is
    canonical. Keying on the flag would silently demote canonical
    providers configured via the new keyed schema.
    """
    from marlow_cli.models import CANONICAL_PROVIDERS

    order = {e.slug: i for i, e in enumerate(CANONICAL_PROVIDERS)}
    canon = sorted(
        (r for r in rows if r["slug"] in order),
        key=lambda r: order[r["slug"]],
    )
    extras = [r for r in rows if r["slug"] not in order]
    return canon + extras


def _apply_pricing(rows: list[dict]) -> None:
    """Enrich provider rows with best-effort per-model pricing.

    Mutates ``rows`` in-place. A provider with available metadata receives::

        row["pricing"] = {model_id: {"input": "$3.00", "output": "$15.00",
                                     "cache": "$0.30" | None, "free": bool}}

    Prices are pre-formatted via ``_format_price_per_mtok`` so the GUI just
    renders strings. Failures are best-effort: a row simply gets no pricing.
    """
    from marlow_cli.models import (
        _format_price_per_mtok,
        get_pricing_for_provider,
    )

    for row in rows:
        slug = str(row.get("slug", "")).lower()
        models = row.get("models") or []
        if not models:
            continue
        try:
            raw_pricing = get_pricing_for_provider(slug) or {}
        except Exception:
            raw_pricing = {}
        if not raw_pricing:
            continue

        formatted: dict[str, dict] = {}
        for mid in models:
            p = raw_pricing.get(mid)
            if not p:
                continue
            inp_raw = p.get("prompt", "")
            out_raw = p.get("completion", "")
            cache_raw = p.get("input_cache_read", "")
            inp = _format_price_per_mtok(inp_raw) if inp_raw != "" else ""
            out = _format_price_per_mtok(out_raw) if out_raw != "" else ""
            cache = _format_price_per_mtok(cache_raw) if cache_raw else None
            # A model is "free" when both input and output cost nothing.
            is_free = inp == "free" and (out == "free" or out == "")
            formatted[mid] = {
                "input": inp,
                "output": out,
                "cache": cache,
                "free": is_free,
            }

        if formatted:
            row["pricing"] = formatted

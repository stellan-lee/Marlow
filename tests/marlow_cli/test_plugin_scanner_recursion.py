"""Tests for PR1 pluggable image gen: scanner recursion, kinds, path keys.

Covers ``_scan_directory`` recursion into category namespaces
(``plugins/image_gen/openai/``), ``kind`` parsing, path-derived registry
keys, and the new gate logic (bundled backends auto-load; user backends
still opt-in; exclusive kind skipped; unknown kinds → standalone warning).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from marlow_cli.plugins import PluginManager


# ── Helpers ────────────────────────────────────────────────────────────────


def _write_plugin(
    root: Path,
    segments: list[str],
    *,
    manifest_extra: Dict[str, Any] | None = None,
    register_body: str = "pass",
) -> Path:
    """Create a plugin dir at ``root/<segments...>/`` with plugin.yaml + __init__.py.

    ``segments`` lets tests build both flat (``["my-plugin"]``) and
    category-namespaced (``["image_gen", "openai"]``) layouts.
    """
    plugin_dir = root
    for seg in segments:
        plugin_dir = plugin_dir / seg
    plugin_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": segments[-1],
        "version": "0.1.0",
        "description": f"Test plugin {'/'.join(segments)}",
    }
    if manifest_extra:
        manifest.update(manifest_extra)
    (plugin_dir / "plugin.yaml").write_text(yaml.dump(manifest))
    (plugin_dir / "__init__.py").write_text(
        f"def register(ctx):\n    {register_body}\n"
    )
    return plugin_dir


def _enable(marlow_home: Path, name: str) -> None:
    """Append ``name`` to ``plugins.enabled`` in ``<marlow_home>/config.yaml``."""
    cfg_path = marlow_home / "config.yaml"
    cfg: dict = {}
    if cfg_path.exists():
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        except Exception:
            cfg = {}
    plugins_cfg = cfg.setdefault("plugins", {})
    enabled = plugins_cfg.setdefault("enabled", [])
    if isinstance(enabled, list) and name not in enabled:
        enabled.append(name)
    cfg_path.write_text(yaml.safe_dump(cfg))


# ── Scanner recursion ──────────────────────────────────────────────────────


class TestCategoryNamespaceRecursion:
    def test_category_namespace_discovered(self, tmp_path, monkeypatch):
        """``<root>/image_gen/openai/plugin.yaml`` is discovered with key
        ``image_gen/openai`` when the ``image_gen`` parent has no manifest."""
        import os
        marlow_home = Path(os.environ["MARLOW_HOME"])  # set by hermetic conftest fixture
        user_plugins = marlow_home / "plugins"

        _write_plugin(user_plugins, ["image_gen", "openai"])
        _enable(marlow_home, "image_gen/openai")

        mgr = PluginManager()
        mgr.discover_and_load()

        assert "image_gen/openai" in mgr._plugins
        loaded = mgr._plugins["image_gen/openai"]
        assert loaded.manifest.key == "image_gen/openai"
        assert loaded.manifest.name == "openai"
        assert loaded.enabled is True

    def test_flat_plugin_key_matches_name(self, tmp_path, monkeypatch):
        """Flat plugins keep their bare name as the key (back-compat)."""
        import os
        marlow_home = Path(os.environ["MARLOW_HOME"])  # set by hermetic conftest fixture
        user_plugins = marlow_home / "plugins"

        _write_plugin(user_plugins, ["my-plugin"])
        _enable(marlow_home, "my-plugin")

        mgr = PluginManager()
        mgr.discover_and_load()

        assert "my-plugin" in mgr._plugins
        assert mgr._plugins["my-plugin"].manifest.key == "my-plugin"

    def test_depth_cap_two(self, tmp_path, monkeypatch):
        """Plugins nested three levels deep are not discovered.

        ``<root>/a/b/c/plugin.yaml`` should NOT be picked up — cap is
        two segments.
        """
        import os
        marlow_home = Path(os.environ["MARLOW_HOME"])  # set by hermetic conftest fixture
        user_plugins = marlow_home / "plugins"

        _write_plugin(user_plugins, ["a", "b", "c"])

        mgr = PluginManager()
        mgr.discover_and_load()

        non_bundled = [
            k for k, p in mgr._plugins.items()
            if p.manifest.source != "bundled"
        ]
        assert non_bundled == []

    def test_category_dir_with_manifest_is_leaf(self, tmp_path, monkeypatch):
        """If ``image_gen/plugin.yaml`` exists, ``image_gen`` itself IS the
        plugin and its children are ignored."""
        import os
        marlow_home = Path(os.environ["MARLOW_HOME"])  # set by hermetic conftest fixture
        user_plugins = marlow_home / "plugins"

        # parent has a manifest → stop recursing
        _write_plugin(user_plugins, ["image_gen"])
        # child also has a manifest — should NOT be found because we stop
        # at the parent.
        _write_plugin(user_plugins, ["image_gen", "openai"])
        _enable(marlow_home, "image_gen")
        _enable(marlow_home, "image_gen/openai")

        mgr = PluginManager()
        mgr.discover_and_load()

        # The bundled plugins/image_gen/openai/ exists in the repo — filter
        # it out so we're only asserting on the user-dir layout.
        user_plugins_in_registry = {
            k for k, p in mgr._plugins.items() if p.manifest.source != "bundled"
        }
        assert "image_gen" in user_plugins_in_registry
        assert "image_gen/openai" not in user_plugins_in_registry


# ── Kind parsing ───────────────────────────────────────────────────────────


class TestKindField:
    def test_default_kind_is_standalone(self, tmp_path, monkeypatch):
        import os
        marlow_home = Path(os.environ["MARLOW_HOME"])  # set by hermetic conftest fixture
        _write_plugin(marlow_home / "plugins", ["p1"])
        _enable(marlow_home, "p1")

        mgr = PluginManager()
        mgr.discover_and_load()

        assert mgr._plugins["p1"].manifest.kind == "standalone"

    @pytest.mark.parametrize("kind", ["backend", "exclusive", "standalone"])
    def test_valid_kinds_parsed(self, kind, tmp_path, monkeypatch):
        import os
        marlow_home = Path(os.environ["MARLOW_HOME"])  # set by hermetic conftest fixture
        _write_plugin(
            marlow_home / "plugins",
            ["p1"],
            manifest_extra={"kind": kind},
        )
        # Not all kinds auto-load, but manifest should parse.
        _enable(marlow_home, "p1")

        mgr = PluginManager()
        mgr.discover_and_load()

        assert "p1" in mgr._plugins
        assert mgr._plugins["p1"].manifest.kind == kind

    def test_unknown_kind_falls_back_to_standalone(self, tmp_path, monkeypatch, caplog):
        import os
        marlow_home = Path(os.environ["MARLOW_HOME"])  # set by hermetic conftest fixture
        _write_plugin(
            marlow_home / "plugins",
            ["p1"],
            manifest_extra={"kind": "bogus"},
        )
        _enable(marlow_home, "p1")

        with caplog.at_level("WARNING"):
            mgr = PluginManager()
            mgr.discover_and_load()

        assert mgr._plugins["p1"].manifest.kind == "standalone"
        assert any(
            "unknown kind" in rec.getMessage() for rec in caplog.records
        )


# ── Gate logic ─────────────────────────────────────────────────────────────


class TestBackendGate:
    def test_user_backend_still_gated_by_enabled(self, tmp_path, monkeypatch):
        """User-installed ``kind: backend`` plugins still require opt-in —
        they're not trusted by default."""
        import os
        marlow_home = Path(os.environ["MARLOW_HOME"])  # set by hermetic conftest fixture
        user_plugins = marlow_home / "plugins"

        _write_plugin(
            user_plugins,
            ["image_gen", "fancy"],
            manifest_extra={"kind": "backend"},
        )
        # Do NOT opt in.

        mgr = PluginManager()
        mgr.discover_and_load()

        loaded = mgr._plugins["image_gen/fancy"]
        assert loaded.enabled is False
        assert "not enabled" in (loaded.error or "")

    def test_user_backend_loads_when_enabled(self, tmp_path, monkeypatch):
        import os
        marlow_home = Path(os.environ["MARLOW_HOME"])  # set by hermetic conftest fixture
        user_plugins = marlow_home / "plugins"

        _write_plugin(
            user_plugins,
            ["image_gen", "fancy"],
            manifest_extra={"kind": "backend"},
        )
        _enable(marlow_home, "image_gen/fancy")

        mgr = PluginManager()
        mgr.discover_and_load()

        assert mgr._plugins["image_gen/fancy"].enabled is True

    def test_exclusive_kind_skipped(self, tmp_path, monkeypatch):
        """``kind: exclusive`` plugins are recorded but not loaded — the
        category's own discovery system handles them (memory today)."""
        import os
        marlow_home = Path(os.environ["MARLOW_HOME"])  # set by hermetic conftest fixture
        _write_plugin(
            marlow_home / "plugins",
            ["some-backend"],
            manifest_extra={"kind": "exclusive"},
        )
        _enable(marlow_home, "some-backend")

        mgr = PluginManager()
        mgr.discover_and_load()

        loaded = mgr._plugins["some-backend"]
        assert loaded.enabled is False
        assert "exclusive" in (loaded.error or "")


# ── Bundled backend auto-load (integration with real bundled plugin) ────────


class TestBundledBackendAutoLoad:
    def test_bundled_web_backend_autoloads(self):
        """The bundled ``plugins/web/ddgs/`` plugin loads without
        any opt-in — it's ``kind: backend`` and shipped in-repo."""
        mgr = PluginManager()
        mgr.discover_and_load()

        assert "web/ddgs" in mgr._plugins
        loaded = mgr._plugins["web/ddgs"]
        assert loaded.manifest.source == "bundled"
        assert loaded.manifest.kind == "backend"
        assert loaded.enabled is True, f"error: {loaded.error}"

"""Explicit edits to the canonical profile Codex TOML; preserve other fields."""

from __future__ import annotations
import json
import os
import re
import tempfile
from pathlib import Path
from agent.codex_environment import managed_environment


def set_model(model):
    if not isinstance(model, str) or not model.strip() or "\n" in model:
        raise ValueError("A non-empty model identifier is required")
    e = managed_environment()
    path = e.codex_home / "config.toml"
    text = path.read_text(encoding="utf-8")
    # Only the root table; a nested provider/model field must be preserved.
    first_table = re.search(r"^[ \t]*\[", text, flags=re.MULTILINE)
    split_at = first_table.start() if first_table else len(text)
    root, tables = text[:split_at], text[split_at:]
    line = "model = " + json.dumps(model.strip(), ensure_ascii=False)
    root = (
        re.sub(
            r"^[ \t]*(?:model|\"model\"|'model')\s*=.*$", line, root, flags=re.MULTILINE
        )
        if re.search(r"^[ \t]*(?:model|\"model\"|'model')\s*=", root, re.MULTILINE)
        else line + "\n" + root
    )
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(root.rstrip() + "\n" + tables)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def list_models():
    import shutil
    from agent.transports.codex_app_server import CodexAppServerClient

    e = managed_environment()
    from marlow_cli.config import load_config

    binary = ((load_config().get("runtime") or {}).get("codex") or {}).get(
        "binary"
    ) or shutil.which("codex")
    if not binary:
        raise RuntimeError("Codex CLI required")
    with CodexAppServerClient(codex_bin=binary, environment=e) as client:
        client.initialize()
        result = client.request("model/list", {"limit": 100}, timeout=15)
        return [
            m.get("model") or m.get("id")
            for m in result.get("data", [])
            if m.get("model") or m.get("id")
        ]

"""Select a persistent file directory for a host-authorized memory principal."""

from hashlib import sha256
import json
from pathlib import Path
import re

LOCAL_OWNER = "local-owner"

# Only authenticated messaging adapters may assign a remote memory principal.
# Email From headers and webhook payloads do not establish a private principal.
_PRIVATE_GATEWAYS = frozenset({"telegram", "discord", "slack", "feishu"})


def gateway_memory_principal(platform, user_id):
    """Name an adapter account once the Gateway has authorized its private DM."""
    channel = getattr(platform, "value", platform)
    if channel not in _PRIVATE_GATEWAYS or user_id is None or not str(user_id).strip():
        return None
    identity = json.dumps([channel, str(user_id)], separators=(",", ":"))
    return "gateway-" + sha256(identity.encode("utf-8")).hexdigest()


def memory_directory(profile: Path, principal: str) -> Path:
    from agent.codex_environment import _private_directory

    if principal != LOCAL_OWNER and not re.fullmatch(
        r"gateway-[0-9a-f]{64}", principal
    ):
        raise ValueError("Invalid host memory principal")
    root = _private_directory(profile / "memory-workspaces")
    workspace = _private_directory(root / principal)
    return _private_directory(workspace / "memory")


def memory_instructions(directory: Path, *, writable: bool) -> str:
    source = Path(__file__).with_name("runtime_instructions") / "AGENTS.md"
    instructions = (
        source.read_text(encoding="utf-8").strip()
        + "\n\nAuthorized memory directory: "
        + json.dumps(str(directory), ensure_ascii=False)
    )
    if not writable:
        instructions += "\n\nThis session has read-only access to the memory directory."
    return instructions

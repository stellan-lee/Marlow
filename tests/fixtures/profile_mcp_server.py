"""Read-only MCP tenant fixture; no network or real credentials."""

import json
import os
import sys

for line in sys.stdin:
    message = json.loads(line)
    rid = message.get("id")
    if rid is None:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tenant-fixture", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "tenant_marker",
                    "description": "Read current tenant marker.",
                    "inputSchema": {"type": "object", "properties": {}},
                    "annotations": {"readOnlyHint": True},
                }
            ]
        }
    elif method == "tools/call":
        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({
                        "tenant": os.environ["TENANT"],
                        "home": os.environ["HOME"],
                        "personal_marker": os.environ.get("PERSONAL_MARKER"),
                    }),
                }
            ]
        }
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)

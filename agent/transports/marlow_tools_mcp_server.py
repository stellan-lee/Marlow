"""Isolated stdio MCP proxy. Business tools execute in the trusted Marlow host.

Invoked with Python -I and an absolute script path. This module intentionally
uses only stdlib: it does not discover plugins, credentials or agent state.
"""

from __future__ import annotations
import json
import os
import socket
import sys


def request_host(method, params, request_id):
    with socket.create_connection(
        ("127.0.0.1", int(os.environ["MARLOW_BRIDGE_PORT"])), timeout=5
    ) as sock:
        sock.settimeout(float(os.environ.get("MARLOW_BRIDGE_TIMEOUT", "615")))
        request = {
            "token": os.environ["MARLOW_BRIDGE_TOKEN"],
            "method": method,
            "params": params,
            "id": request_id,
        }
        sock.sendall((json.dumps(request) + "\n").encode())
        with sock.makefile("rb") as stream:
            response = stream.readline(8 * 1024 * 1024)
        if not response:
            raise RuntimeError(
                "Marlow bridge closed; do not replay an uncertain operation"
            )
        return json.loads(response)


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
            rid = msg.get("id")
            method = msg.get("method", "")
            if rid is None:
                continue
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "marlow-tools", "version": "2"},
                }
            elif method == "ping":
                result = {}
            elif method in {"tools/list", "tools/call"}:
                result = request_host(method, msg.get("params") or {}, rid)
            else:
                print(
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32601, "message": "Unsupported MCP method"},
                    }),
                    flush=True,
                )
                continue
            print(
                json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True
            )
        except Exception:
            # No exception text: it can contain provider/tool secrets.
            print(
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": msg.get("id") if isinstance(msg, dict) else None,
                    "error": {
                        "code": -32000,
                        "message": "Marlow bridge unavailable; operation may require verification",
                    },
                }),
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

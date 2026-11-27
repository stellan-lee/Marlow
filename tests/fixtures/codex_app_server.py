#!/usr/bin/python3
"""Deterministic app-server protocol fixture; no model or business network."""

import json
import os
from pathlib import Path
import sys
import tomllib
import uuid

VERSION = "0.159.2"
CONFIG_OVERRIDES = {}

if "--version" in sys.argv:
    print(f"codex-cli {VERSION}")
    raise SystemExit(0)

config = tomllib.loads(
    "\n".join(value for flag, value in zip(sys.argv, sys.argv[1:]) if flag == "-c")
)
config.update(CONFIG_OVERRIDES)
root = Path(os.environ["CODEX_HOME"])
record = root / "fixture-events.jsonl"
thread_id = None


def emit(msg):
    print(json.dumps(msg), flush=True)


def note(method, params):
    emit({"method": method, "params": params})


for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if not method or rid is None:
        continue
    with record.open("a", encoding="utf-8") as log:
        log.write(
            json.dumps({
                "method": method,
                "params": params,
                "home": os.environ.get("HOME"),
                "cwd": os.getcwd(),
                "personal_marker": os.environ.get("PERSONAL_MARKER"),
            })
            + "\n"
        )
    result = {}
    if method == "initialize":
        result = {"codexHome": str(root), "userAgent": "fixture"}
    elif method == "config/read":
        result = {"config": config, "layers": []}
    elif method == "skills/list":
        result = {"data": [{"cwd": cwd, "skills": [], "errors": []} for cwd in params["cwds"]]}
    elif method == "account/read":
        result = {"requiresOpenaiAuth": False, "account": None}
    elif method in {"thread/start", "thread/resume"}:
        thread_id = params.get("threadId") or str(uuid.uuid4())
        result = {"thread": {"id": thread_id}}
    elif method == "thread/compact/start":
        tid = str(uuid.uuid4())
        emit({"id": rid, "result": {}})
        note(
            "turn/started",
            {"threadId": thread_id, "turn": {"id": tid, "status": "inProgress"}},
        )
        note(
            "turn/completed",
            {"threadId": thread_id, "turn": {"id": tid, "status": "completed"}},
        )
        continue
    elif method == "turn/start":
        text = json.dumps(params.get("input", []))
        tid = str(uuid.uuid4())
        emit({"id": rid, "result": {"turn": {"id": tid}}})
        if "EXIT_FIXTURE" in text:
            raise SystemExit(2)
        if "HANG_FIXTURE" in text:
            continue
        note(
            "item/agentMessage/delta",
            {"threadId": thread_id, "turnId": tid, "delta": "fixture reply"},
        )
        note(
            "item/completed",
            {
                "threadId": thread_id,
                "turnId": tid,
                "item": {
                    "type": "agentMessage",
                    "id": "reply",
                    "text": "fixture reply",
                },
            },
        )
        note(
            "thread/tokenUsage/updated",
            {
                "threadId": thread_id,
                "turnId": tid,
                "tokenUsage": {
                    "total": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10},
                    "last": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10},
                },
            },
        )
        note(
            "turn/completed",
            {"threadId": thread_id, "turn": {"id": tid, "status": "completed"}},
        )
        continue
    emit({"id": rid, "result": result})

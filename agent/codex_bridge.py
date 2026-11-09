"""Turn-bound MCP business bridge, executing in the Marlow host process."""

from __future__ import annotations
import hmac
import json
import secrets
import socketserver
import sys
import threading
from pathlib import Path

# Shell/files belong to Codex. Explicit capability ownership also excludes
# retired Phase 1 tools, helper model routing and self-granting admin APIs.
BUSINESS_TOOLS = frozenset({
    "terminal",
    "read_file",
    "write_file",
    "patch",
    "search_files",
    "web_search",
    "web_extract",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_snapshot",
    "browser_scroll",
    "browser_back",
    "browser_get_images",
    "browser_console",
    "browser_vision",
    "vision_analyze",
    "memory",
    "session_search",
    "todo",
    "clarify",
    "delegate_task",
    "send_message",
    "cronjob",
    "skills_list",
    "skill_view",
    "skill_manage",
    "experience_decision",
    "request_admin_approval",
})


class BusinessBridge:
    def __init__(self, agent):
        self.agent = agent
        self.token = secrets.token_urlsafe(32)
        self._dispatch = None
        self._lock = threading.RLock()
        self._results = {}
        self._execution_lock = threading.Lock()
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(5)
                try:
                    req = json.loads(self.rfile.readline(1024 * 1024))
                    if not hmac.compare_digest(str(req.get("token", "")), owner.token):
                        return
                    result = owner.request(req)
                except Exception:
                    result = {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": "Marlow tool failed; operation may require verification",
                            }
                        ],
                    }
                self.wfile.write((json.dumps(result, default=str) + "\n").encode())

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def config(self):
        return {
            "marlow-tools": {
                "command": sys.executable,
                "args": [
                    "-I",
                    str(
                        Path(__file__).parent
                        / "transports"
                        / "marlow_tools_mcp_server.py"
                    ),
                ],
                "env": {
                    "MARLOW_BRIDGE_PORT": str(self.server.server_address[1]),
                    "MARLOW_BRIDGE_TOKEN": self.token,
                    "MARLOW_BRIDGE_TIMEOUT": str(self.agent.turn_timeout + 20),
                },
                # This proxy delegates authority to host execution-bound business
                # guards. Codex must not insert a second, disconnected UI approval.
                "default_tools_approval_mode": "approve",
                "tool_timeout_sec": int(self.agent.turn_timeout) + 15,
            }
        }

    def activate(self):
        from tools.thread_context import propagate_context_to_thread

        # Captured on the actual caller thread on EVERY turn, including
        # callbacks and ContextVars; never from model parameters.
        with self._lock:
            self._dispatch = propagate_context_to_thread(
                self.agent._dispatch_business_tool
            )
            self._results.clear()

    def deactivate(self):
        with self._lock:
            self._dispatch = None

    def request(self, req):
        if req["method"] == "tools/list":
            return {
                "tools": [
                    dict(
                        name=s["name"],
                        description=s.get("description", ""),
                        inputSchema=s.get("parameters", {"type": "object"}),
                    )
                    for s in self.agent.business_schemas()
                ]
            }
        with self._lock:
            dispatch = self._dispatch
            if dispatch is None:
                raise PermissionError("No active authorized turn")
            key = str(req["id"])
            payload = json.dumps(req.get("params"), sort_keys=True)
            if key in self._results:
                old_payload, result = self._results[key]
                if payload != old_payload:
                    raise PermissionError("Reused tool call id")
                return result
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            allowed = {s["name"] for s in self.agent.business_schemas()}
            if name not in allowed or not isinstance(args, dict):
                raise PermissionError("Tool not available in this scope")
            uncertain = {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": "Operation requires verification; do not replay",
                    }
                ],
            }
            self._results[key] = (payload, uncertain)
        # Never hold the lifecycle lock across network/business code. A timed
        # out operation remains uncertain, while cancellation can still close
        # the app-server and finish the task without replaying it.
        if not self._execution_lock.acquire(blocking=False):
            raise PermissionError("A business operation is still pending verification")
        try:
            started = __import__("time").monotonic()
            decision = self.agent._tool_guardrails.before_call(name, args)
            if not decision.allows_execution:
                raise PermissionError(decision.message)
            thread_id = threading.get_ident()
            from tools.interrupt import set_interrupt

            with self.agent._business_threads_lock:
                if self.agent._interrupt_requested:
                    raise PermissionError("Turn was cancelled before dispatch")
                self.agent._business_threads.add(thread_id)
                set_interrupt(False, thread_id)
            try:
                if name not in {
                    "terminal",
                    "write_file",
                    "patch",
                } and self.agent._tool_can_write(name, args):
                    self.agent._dispatched_write = True
                value = dispatch(name, args)
            except PermissionError:
                raise
            except Exception:
                if self.agent._tool_can_write(name, args):
                    self.agent._business_uncertain = True
                raise
            # Errors from mutating business tools can follow a remote side
            # effect. Do not interpret an error payload as proof of rollback.
            outcome = value
            if isinstance(outcome, str):
                try:
                    outcome = json.loads(outcome)
                except ValueError:
                    outcome = None
            if (
                name not in {"write_file", "patch"}
                and self.agent._tool_can_write(name, args)
                and isinstance(outcome, dict)
                and (
                    outcome.get("success") is False
                    or outcome.get("error")
                    or outcome.get("isError")
                )
            ):
                self.agent._business_uncertain = True
            from agent.usage_events import record_tool_usage_event

            record_tool_usage_event(
                self.agent,
                tool_name=name,
                args=args,
                tool_call_id=key,
                failed=False,
                duration_seconds=__import__("time").monotonic() - started,
            )
            from agent.redact import redact_sensitive_text

            if not (isinstance(value, dict) and value.get("_multimodal")):
                text_value = value if isinstance(value, str) else json.dumps(value)
                value = redact_sensitive_text(text_value, force=True)
            self.agent._tool_guardrails.after_call(
                name, args, value if isinstance(value, str) else None
            )
            if isinstance(value, dict) and value.get("_multimodal"):
                parts = []
                for part in value["content"]:
                    if part.get("type") == "image_url":
                        url = part.get("image_url") or {}
                        if isinstance(url, dict):
                            url = url.get("url", "")
                        if (
                            not isinstance(url, str)
                            or not url.startswith("data:image/")
                            or ";base64," not in url
                        ):
                            raise ValueError(
                                "MCP image results require inline image data"
                            )
                        header, data = url.split(",", 1)
                        parts.append({
                            "type": "image",
                            "mimeType": header[5:].split(";")[0],
                            "data": data,
                        })
                    else:
                        parts.append(part)
                result = {"content": parts}
            else:
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": value
                            if isinstance(value, str)
                            else json.dumps(value),
                        }
                    ]
                }
            with self._lock:
                if self._dispatch is dispatch:
                    self._results[key] = (payload, result)
            return result
        finally:
            with self.agent._business_threads_lock:
                self.agent._business_threads.discard(threading.get_ident())
                from tools.interrupt import set_interrupt

                set_interrupt(False, threading.get_ident())
            self._execution_lock.release()

    def close(self):
        self.deactivate()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

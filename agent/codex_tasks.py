"""Explicit model-driven business tasks use the same app-server adapter.

The response shape serves existing business callers, not a second model client.
Provider overrides and auxiliary model routing are deliberately unsupported.
"""

from __future__ import annotations
import asyncio
import threading


class TaskCancellation:
    """Interrupt a background worker even when its async waiter is cancelled."""

    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.agent = None

    def attach(self, agent):
        with self.lock:
            self.agent = agent
            if self.event.is_set():
                agent.interrupt("Business task cancelled")

    def cancel(self):
        self.event.set()
        with self.lock:
            if self.agent is not None:
                self.agent.interrupt("Business task cancelled")


from types import SimpleNamespace


def call_llm(*, task=None, messages, provider=None, model=None, **options):
    from run_agent import AIAgent
    from marlow_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider()
    if any(
        options.get(key)
        for key in (
            "api_key",
            "base_url",
            "profile_override",
            "client",
            "fallback_providers",
        )
    ):
        raise ValueError(
            "Task credentials/provider overrides are retired; use the profile Codex configuration"
        )
    if provider and provider not in {"auto", runtime["provider"], "openai-codex"}:
        raise ValueError("Auxiliary provider routing is retired; configure Codex")
    if model and model != runtime["model"]:
        raise ValueError("Auxiliary model routing is retired; configure Codex")
    response_format = (options.get("extra_body") or {}).get("response_format") or {}
    output_schema = (response_format.get("json_schema") or {}).get("schema")
    cancellation = options.get("cancellation") or TaskCancellation()
    parent = options.get("parent_agent")
    if cancellation.event.is_set() or (
        parent and (parent._interrupt_requested or parent._closed)
    ):
        raise InterruptedError("Business task cancelled before dispatch")
    agent = AIAgent(
        cancel_event=cancellation.event,
        **(options.get("host_scope") or {}),
        parent_session_id=options.get("parent_session_id"),
        output_schema=output_schema,
        enabled_toolsets=[],
        platform="business-task:" + (task or "plugin"),
        skip_memory=True,
        quiet_mode=True,
        timeout=options.get("timeout"),
    )
    cancellation.attach(agent)
    if parent:
        with parent._active_children_lock:
            if parent._interrupt_requested or parent._closed:
                cancellation.cancel()
            parent._active_children.append(agent)
    try:
        # Carry rich content to Codex; no image captions from another model.
        inputs = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                inputs.extend(content)
            elif content:
                inputs.append({"type": "text", "text": str(content)})
        result = agent.run_conversation(inputs)
        if not result.get("completed"):
            raise RuntimeError(
                result.get("error") or "Codex business task did not complete"
            )
        text = result["final_response"]
        last = (result.get("usage") or {}).get("last")
        usage = (
            SimpleNamespace(
                prompt_tokens=last.get("inputTokens"),
                completion_tokens=last.get("outputTokens"),
                total_tokens=last.get("totalTokens"),
            )
            if last
            else None
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=text), finish_reason="stop"
                )
            ],
            model=agent.model,
            usage=usage,
            provider=agent.provider,
        )
    finally:
        agent.close()
        if parent:
            with parent._active_children_lock:
                if agent in parent._active_children:
                    parent._active_children.remove(agent)


async def async_call_llm(**kwargs):
    cancellation = kwargs.pop("cancellation", None) or TaskCancellation()
    try:
        return await asyncio.to_thread(call_llm, cancellation=cancellation, **kwargs)
    finally:
        cancellation.cancel()


def extract_content_or_reasoning(response):
    return response.choices[0].message.content or ""


def _read_main_provider():
    from marlow_cli.runtime_provider import resolve_runtime_provider

    return resolve_runtime_provider()["provider"]


def _read_main_model():
    from marlow_cli.runtime_provider import resolve_runtime_provider

    return resolve_runtime_provider()["model"]

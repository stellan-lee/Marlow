"""Auxiliary LLM routing for Codex and local compatible endpoints."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import re
import threading
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse, urlunparse

from utils import normalize_proxy_env_vars

logger = logging.getLogger(__name__)

OMIT_TEMPERATURE: object = object()
_CODEX_AUX_BASE_URL = "https://chatgpt.com/backend-api/codex"
_DEFAULT_AUX_TIMEOUT = 30.0
_CLIENT_CACHE_MAX_SIZE = 32
_client_cache: Dict[tuple, tuple[Any, Optional[str], Any]] = {}
_client_cache_lock = threading.Lock()

_RUNTIME_MAIN_PROVIDER = ""
_RUNTIME_MAIN_MODEL = ""
_RUNTIME_MAIN_BASE_URL = ""
_RUNTIME_MAIN_API_KEY = ""
_RUNTIME_MAIN_API_MODE = ""


class _OpenAIProxy:
    def __call__(self, *args, **kwargs):
        from openai import OpenAI as cls

        return cls(*args, **kwargs)


OpenAI = _OpenAIProxy()


def _openai_client(*args, async_mode: bool = False, **kwargs):
    if async_mode:
        from openai import AsyncOpenAI

        return AsyncOpenAI(*args, **kwargs)
    return OpenAI(*args, **kwargs)


def _normalize_aux_provider(provider: Optional[str]) -> str:
    normalized = (provider or "auto").strip().lower()
    if normalized.startswith("custom:"):
        return normalized.split(":", 1)[1].strip() or "custom"
    if normalized == "codex":
        return "openai-codex"
    if normalized == "main":
        return _read_main_provider() or "custom"
    return normalized


def _normalize_vision_provider(provider: Optional[str]) -> str:
    return _normalize_aux_provider(provider)


def _fixed_temperature_for_model(
    model: Optional[str], base_url: Optional[str] = None
) -> "Optional[float] | object":
    del model, base_url
    return None


def _compression_threshold_for_model(model: Optional[str]) -> Optional[float]:
    del model
    return None


def _codex_cloudflare_headers(access_token: str) -> Dict[str, str]:
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Marlow Agent)",
        "originator": "codex_cli_rs",
    }
    try:
        payload = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        account_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id:
            headers["ChatGPT-Account-ID"] = account_id
    except Exception:
        pass
    return headers


def _to_openai_base_url(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/")


def _read_codex_access_token() -> Optional[str]:
    try:
        from marlow_cli.auth import resolve_codex_runtime_credentials

        return str(resolve_codex_runtime_credentials().get("api_key") or "").strip() or None
    except Exception:
        return None


def _convert_responses_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    converted = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in {"text", "input_text"}:
            converted.append({"type": "input_text", "text": part.get("text", "")})
        elif part.get("type") in {"image_url", "input_image"}:
            image = part.get("image_url", "")
            if isinstance(image, dict):
                image = image.get("url", "")
            converted.append({"type": "input_image", "image_url": image})
    return converted or ""


def _responses_output_text(response: Any) -> str:
    """Read plain text from a completed Responses object without deltas."""
    output_text = getattr(response, "output_text", "")
    if isinstance(output_text, str) and output_text:
        return output_text
    parts: list[str] = []
    for item in getattr(response, "output", ()) or ():
        item_type = getattr(item, "type", None)
        content = getattr(item, "content", None)
        if isinstance(item, dict):
            item_type = item.get("type", item_type)
            content = item.get("content")
        if item_type != "message":
            continue
        for part in content or ():
            part_type = getattr(part, "type", None)
            text = getattr(part, "text", None)
            if isinstance(part, dict):
                part_type = part.get("type", part_type)
                text = part.get("text", text)
            if part_type in {"output_text", "text"} and isinstance(text, str):
                parts.append(text)
    return "".join(parts)


class _CodexCompletions:
    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model

    def create(self, **kwargs) -> Any:
        instructions = "You are a helpful assistant."
        inputs = []
        for message in kwargs.get("messages", []):
            role = message.get("role", "user")
            if role == "system":
                instructions = str(message.get("content") or instructions)
                continue
            inputs.append({"role": role, "content": _convert_responses_content(message.get("content"))})
        request: Dict[str, Any] = {
            "model": kwargs.get("model") or self.model,
            "instructions": instructions,
            "input": inputs,
            "store": False,
        }
        if kwargs.get("tools"):
            request["tools"] = [
                {
                    "type": "function",
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "parameters": tool["function"].get("parameters", {}),
                }
                for tool in kwargs["tools"]
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
            ]
        # The ChatGPT Codex Responses endpoint only accepts streamed calls.
        # Reuse the runtime's event assembler so the auxiliary path sees the
        # same response shape as the primary Codex transport.
        request["stream"] = True
        response = self.client.responses.create(**request)
        if not (hasattr(response, "output") and not hasattr(response, "__iter__")):
            from agent.codex_runtime import _consume_codex_event_stream

            try:
                final_response = _consume_codex_event_stream(
                    response, model=request["model"]
                )
            finally:
                close_fn = getattr(response, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass
            response = final_response
        status = getattr(response, "status", None)
        if status in {"cancelled", "incomplete", "failed"}:
            detail = getattr(response, "error", None) or getattr(
                response, "incomplete_details", None
            )
            raise RuntimeError(
                f"Codex Responses stream ended with status={status}"
                + (f": {detail}" if detail else "")
            )
        content = _responses_output_text(response)
        message = SimpleNamespace(content=content, tool_calls=None, reasoning=None)
        usage = getattr(response, "usage", None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=usage
        )


class CodexAuxiliaryClient:
    def __init__(self, real_client: Any, model: str):
        self._real_client = real_client
        self.chat = SimpleNamespace(completions=_CodexCompletions(real_client, model))
        self.api_key = real_client.api_key
        self.base_url = real_client.base_url

    def close(self) -> None:
        self._real_client.close()


class AsyncCodexAuxiliaryClient:
    def __init__(self, sync_client: CodexAuxiliaryClient):
        sync_create = sync_client.chat.completions.create

        async def create(**kwargs):
            return await asyncio.to_thread(sync_create, **kwargs)

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
        self.api_key = sync_client.api_key
        self.base_url = sync_client.base_url
        self._real_client = sync_client._real_client


def _build_codex_client(model: str) -> Tuple[Optional[Any], Optional[str]]:
    if not model:
        logger.warning("Codex auxiliary calls require an explicit model")
        return None, None
    token = _read_codex_access_token() or ""
    if not token:
        return None, None
    client = OpenAI(
        api_key=token,
        base_url=_CODEX_AUX_BASE_URL,
        default_headers=_codex_cloudflare_headers(token),
    )
    return CodexAuxiliaryClient(client, model), model


def _read_main_model() -> str:
    if _RUNTIME_MAIN_MODEL:
        return _RUNTIME_MAIN_MODEL
    try:
        from marlow_cli.config import load_config

        cfg = load_config().get("model", {})
        if isinstance(cfg, str):
            return cfg.strip()
        if isinstance(cfg, dict):
            return str(cfg.get("default") or cfg.get("model") or "").strip()
    except Exception:
        pass
    return ""


def _read_main_provider() -> str:
    if _RUNTIME_MAIN_PROVIDER:
        return _RUNTIME_MAIN_PROVIDER
    try:
        from marlow_cli.config import load_config

        cfg = load_config().get("model", {})
        if isinstance(cfg, dict):
            return str(cfg.get("provider") or "").strip().lower()
    except Exception:
        pass
    return ""


def set_runtime_main(
    provider: str, model: str, *, base_url: str = "", api_key: str = "", api_mode: str = ""
) -> None:
    global _RUNTIME_MAIN_PROVIDER, _RUNTIME_MAIN_MODEL, _RUNTIME_MAIN_BASE_URL
    global _RUNTIME_MAIN_API_KEY, _RUNTIME_MAIN_API_MODE
    _RUNTIME_MAIN_PROVIDER = (provider or "").strip().lower()
    _RUNTIME_MAIN_MODEL = (model or "").strip()
    _RUNTIME_MAIN_BASE_URL = (base_url or "").strip()
    _RUNTIME_MAIN_API_KEY = api_key.strip() if isinstance(api_key, str) else ""
    _RUNTIME_MAIN_API_MODE = (api_mode or "").strip()


def clear_runtime_main() -> None:
    set_runtime_main("", "")


def _normalize_main_runtime(main_runtime: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(main_runtime, dict):
        return {}
    return {
        key: value.strip() if isinstance(value, str) else value
        for key, value in main_runtime.items()
        if key in {"provider", "model", "base_url", "api_key", "api_mode"}
        and (callable(value) or (isinstance(value, str) and value.strip()))
    }


def _validate_proxy_env_urls() -> None:
    normalize_proxy_env_vars()
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        value = str(os.getenv(name) or "").strip()
        if not value:
            continue
        try:
            _ = urlparse(value).port
        except ValueError as exc:
            raise RuntimeError(f"Malformed proxy environment variable {name}={value!r}") from exc


def _validate_base_url(base_url: str) -> None:
    candidate = str(base_url or "").strip()
    if not candidate:
        return
    try:
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("endpoint must use http or https")
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"Malformed compatible endpoint URL: {candidate!r}") from exc


def _get_auxiliary_task_config(task: str) -> Dict[str, Any]:
    if not task:
        return {}
    try:
        from marlow_cli.config import load_config

        auxiliary = load_config().get("auxiliary", {})
        result = auxiliary.get(task, {}) if isinstance(auxiliary, dict) else {}
        result = dict(result) if isinstance(result, dict) else {}
    except Exception:
        result = {}
    try:
        from marlow_cli.plugins import get_plugin_auxiliary_tasks

        for entry in get_plugin_auxiliary_tasks():
            if entry.get("key") == task and isinstance(entry.get("defaults"), dict):
                return {**entry["defaults"], **result}
    except Exception:
        pass
    return result


def _resolve_task_provider_model(
    task: str = None, provider: str = None, model: str = None,
    base_url: str = None, api_key: str = None,
) -> Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str]]:
    cfg = _get_auxiliary_task_config(task or "")
    resolved_provider = provider or cfg.get("provider") or "auto"
    resolved_model = model or cfg.get("model") or None
    resolved_base = base_url or cfg.get("base_url") or None
    resolved_key = api_key or cfg.get("api_key") or None
    resolved_mode = cfg.get("api_mode") or cfg.get("transport") or None
    if resolved_base and not provider:
        resolved_provider = "custom"
    return str(resolved_provider), resolved_model, resolved_base, resolved_key, resolved_mode


def _runtime_for_auxiliary(
    provider: str, *, explicit_base_url: Optional[str], explicit_api_key: Optional[str],
    main_runtime: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    runtime = _normalize_main_runtime(main_runtime)
    normalized = _normalize_aux_provider(provider)
    if normalized == "auto" and runtime:
        return runtime
    if normalized == "auto" and _RUNTIME_MAIN_PROVIDER:
        runtime = {
            "provider": _RUNTIME_MAIN_PROVIDER, "model": _RUNTIME_MAIN_MODEL,
            "base_url": _RUNTIME_MAIN_BASE_URL, "api_key": _RUNTIME_MAIN_API_KEY,
            "api_mode": _RUNTIME_MAIN_API_MODE,
        }
        if runtime["provider"] != "openai-codex" and not runtime["base_url"]:
            from marlow_cli.runtime_provider import resolve_runtime_provider

            resolved = resolve_runtime_provider(requested=runtime["provider"])
            if resolved:
                resolved.setdefault("model", runtime["model"])
                return resolved
        return runtime
    from marlow_cli.runtime_provider import resolve_runtime_provider

    return resolve_runtime_provider(
        requested=None if normalized == "auto" else normalized,
        explicit_base_url=explicit_base_url,
        explicit_api_key=explicit_api_key,
    )


def resolve_provider_client(
    provider: str, model: str = None, async_mode: bool = False,
    raw_codex: bool = False, explicit_base_url: str = None,
    explicit_api_key: str = None, api_mode: str = None,
    main_runtime: Optional[Dict[str, Any]] = None, is_vision: bool = False,
) -> Tuple[Optional[Any], Optional[str]]:
    del api_mode, is_vision
    if _normalize_aux_provider(provider) == "openai-codex" and not explicit_base_url:
        client, final_model = _build_codex_client(model or _read_main_model())
        if raw_codex and isinstance(client, CodexAuxiliaryClient):
            client = client._real_client
        elif async_mode and isinstance(client, CodexAuxiliaryClient):
            client = AsyncCodexAuxiliaryClient(client)
        return client, final_model
    try:
        runtime = _runtime_for_auxiliary(
            provider,
            explicit_base_url=explicit_base_url,
            explicit_api_key=explicit_api_key,
            main_runtime=main_runtime,
        )
    except Exception as exc:
        logger.debug("Auxiliary runtime unavailable: %s", exc)
        return None, model
    final_model = model or runtime.get("model") or _read_main_model()
    if not final_model:
        return None, None
    if runtime.get("provider") == "openai-codex":
        client, final_model = _build_codex_client(final_model)
        if raw_codex and isinstance(client, CodexAuxiliaryClient):
            client = client._real_client
        if client is not None and async_mode:
            client = AsyncCodexAuxiliaryClient(client) if isinstance(client, CodexAuxiliaryClient) else client
        return client, final_model
    base_url = str(runtime.get("base_url") or "").rstrip("/")
    api_key_value = runtime.get("api_key") or "no-key-required"
    if not base_url:
        return None, final_model
    _validate_base_url(base_url)
    kwargs: Dict[str, Any] = {"api_key": api_key_value, "base_url": base_url}
    parsed = urlparse(base_url)
    if parsed.query:
        kwargs["base_url"] = urlunparse(parsed._replace(query=""))
        kwargs["default_query"] = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    return _openai_client(async_mode=async_mode, **kwargs), final_model


def _get_cached_client(
    provider: str, model: str = None, async_mode: bool = False,
    base_url: str = None, api_key: str = None, api_mode: str = None,
    main_runtime: Optional[Dict[str, Any]] = None, is_vision: bool = False,
) -> Tuple[Optional[Any], Optional[str]]:
    loop = None
    if async_mode:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                pass
    runtime_key = tuple(sorted((str(k), str(v)) for k, v in _normalize_main_runtime(main_runtime).items()))
    key = (provider, model, async_mode, base_url, api_key, api_mode, runtime_key, is_vision)
    with _client_cache_lock:
        cached = _client_cache.get(key)
    if cached:
        cached_loop = cached[2]
        if cached_loop is None or (cached_loop is loop and not cached_loop.is_closed()):
            return cached[0], cached[1]
        with _client_cache_lock:
            _client_cache.pop(key, None)
    client, final_model = resolve_provider_client(
        provider, model, async_mode, explicit_base_url=base_url,
        explicit_api_key=api_key, api_mode=api_mode,
        main_runtime=main_runtime, is_vision=is_vision,
    )
    if client is not None:
        with _client_cache_lock:
            while len(_client_cache) >= _CLIENT_CACHE_MAX_SIZE:
                _client_cache.pop(next(iter(_client_cache)))
            _client_cache[key] = (client, final_model, loop)
    return client, final_model


def get_text_auxiliary_client(
    task: str = "", *, main_runtime: Optional[Dict[str, Any]] = None
) -> Tuple[Optional[Any], Optional[str]]:
    provider, model, base_url, api_key, api_mode = _resolve_task_provider_model(task or None)
    return _get_cached_client(provider, model, False, base_url, api_key, api_mode, main_runtime)


def resolve_vision_provider_client(
    provider: Optional[str] = None, model: Optional[str] = None, *,
    base_url: Optional[str] = None, api_key: Optional[str] = None,
    async_mode: bool = False,
) -> Tuple[Optional[str], Optional[Any], Optional[str]]:
    requested, resolved_model, resolved_base, resolved_key, mode = _resolve_task_provider_model(
        "vision", provider, model, base_url, api_key
    )
    client, final_model = _get_cached_client(
        requested, resolved_model, async_mode, resolved_base, resolved_key, mode,
        is_vision=True,
    )
    return _normalize_vision_provider(requested), client, final_model


def get_available_vision_backends() -> List[str]:
    provider = _read_main_provider() or "auto"
    resolved, client, _ = resolve_vision_provider_client(provider=provider, model=_read_main_model())
    return [resolved] if client is not None and resolved else []


def get_auxiliary_extra_body() -> dict:
    return {}


def _build_call_kwargs(
    provider: str, model: str, messages: list, temperature: Optional[float] = None,
    max_tokens: Optional[int] = None, tools: Optional[list] = None,
    timeout: float = 30.0, extra_body: Optional[dict] = None,
    base_url: Optional[str] = None,
) -> dict:
    del provider, max_tokens, base_url
    kwargs: Dict[str, Any] = {"model": model, "messages": messages, "timeout": timeout}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if tools:
        seen = set()
        kwargs["tools"] = [
            tool for tool in tools
            if not ((name := (tool.get("function") or {}).get("name")) in seen)
            and not (name and seen.add(name))
        ]
    if extra_body:
        kwargs["extra_body"] = dict(extra_body)
    return kwargs


def _is_payment_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return getattr(exc, "status_code", None) == 402 or any(
        word in text for word in ("payment required", "insufficient funds", "credits")
    )


def _is_rate_limit_error(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 429 or type(exc).__name__ == "RateLimitError"


def _is_connection_error(exc: Exception) -> bool:
    return type(exc).__name__ in {
        "APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout",
        "ReadTimeout", "PoolTimeout", "RemoteProtocolError",
    }


def _is_model_not_found_error(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 404 and "model" in str(exc).lower()


def _is_unsupported_parameter_error(exc: Exception, param: str) -> bool:
    if not param:
        return False
    text = str(exc).lower()
    return param.lower() in text and any(
        word in text
        for word in ("unsupported", "not support", "unknown", "unrecognized", "invalid")
    )


def _is_unsupported_temperature_error(exc: Exception) -> bool:
    return _is_unsupported_parameter_error(exc, "temperature")


def _build_call_context(
    task: Optional[str], provider: Optional[str], model: Optional[str],
    base_url: Optional[str], api_key: Optional[str], main_runtime: Optional[Dict[str, Any]],
    async_mode: bool,
):
    resolved_provider, resolved_model, resolved_base, resolved_key, mode = _resolve_task_provider_model(
        task, provider, model, base_url, api_key
    )
    client, final_model = _get_cached_client(
        resolved_provider, resolved_model, async_mode, resolved_base, resolved_key,
        mode, main_runtime, task == "vision",
    )
    if client is None or not final_model:
        raise RuntimeError(f"No retained LLM runtime configured for auxiliary task {task or 'call'}")
    cfg = _get_auxiliary_task_config(task or "")
    return resolved_provider, client, final_model, cfg


def _validate_llm_response(response: Any, task: str = None) -> Any:
    try:
        if not response.choices or not hasattr(response.choices[0], "message"):
            raise AttributeError
    except (AttributeError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"Auxiliary {task or 'call'} returned an invalid compatible response"
        ) from exc
    return response


def call_llm(
    task: str = None, *, provider: str = None, model: str = None,
    base_url: str = None, api_key: str = None,
    main_runtime: Optional[Dict[str, Any]] = None, messages: list,
    temperature: float = None, max_tokens: int = None, tools: list = None,
    timeout: float = None, extra_body: dict = None,
) -> Any:
    resolved, client, final_model, cfg = _build_call_context(
        task, provider, model, base_url, api_key, main_runtime, False
    )
    kwargs = _build_call_kwargs(
        resolved, final_model, messages, temperature, max_tokens, tools,
        float(timeout or cfg.get("timeout") or _DEFAULT_AUX_TIMEOUT),
        {**(cfg.get("extra_body") if isinstance(cfg.get("extra_body"), dict) else {}), **(extra_body or {})},
        base_url,
    )
    try:
        return _validate_llm_response(client.chat.completions.create(**kwargs), task)
    except Exception as exc:
        if "temperature" in kwargs and _is_unsupported_temperature_error(exc):
            kwargs.pop("temperature")
            return _validate_llm_response(client.chat.completions.create(**kwargs), task)
        raise


async def async_call_llm(
    task: str = None, *, provider: str = None, model: str = None,
    base_url: str = None, api_key: str = None,
    main_runtime: Optional[Dict[str, Any]] = None, messages: list,
    temperature: float = None, max_tokens: int = None, tools: list = None,
    timeout: float = None, extra_body: dict = None,
) -> Any:
    resolved, client, final_model, cfg = _build_call_context(
        task, provider, model, base_url, api_key, main_runtime, True
    )
    kwargs = _build_call_kwargs(
        resolved, final_model, messages, temperature, max_tokens, tools,
        float(timeout or cfg.get("timeout") or _DEFAULT_AUX_TIMEOUT),
        {**(cfg.get("extra_body") if isinstance(cfg.get("extra_body"), dict) else {}), **(extra_body or {})},
        base_url,
    )
    try:
        return _validate_llm_response(await client.chat.completions.create(**kwargs), task)
    except Exception as exc:
        if "temperature" in kwargs and _is_unsupported_temperature_error(exc):
            kwargs.pop("temperature")
            return _validate_llm_response(await client.chat.completions.create(**kwargs), task)
        raise


def extract_content_or_reasoning(response) -> str:
    message = response.choices[0].message
    content = str(getattr(message, "content", "") or "").strip()
    if content:
        cleaned = re.sub(r"<(?:think|thinking|reasoning)>.*?</(?:think|thinking|reasoning)>", "", content, flags=re.I | re.S).strip()
        if cleaned:
            return cleaned
    reasoning_parts = []
    for field in ("reasoning", "reasoning_content"):
        value = getattr(message, field, None)
        if isinstance(value, str) and value.strip() and value.strip() not in reasoning_parts:
            reasoning_parts.append(value.strip())
    details = getattr(message, "reasoning_details", None)
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            value = detail.get("summary") or detail.get("text")
            if isinstance(value, str) and value.strip() and value.strip() not in reasoning_parts:
                reasoning_parts.append(value.strip())
    return "\n\n".join(reasoning_parts)


def neuter_async_httpx_del() -> None:
    try:
        from openai._base_client import AsyncHttpxClientWrapper

        AsyncHttpxClientWrapper.__del__ = lambda self: None
    except (ImportError, AttributeError):
        pass


def shutdown_cached_clients() -> None:
    with _client_cache_lock:
        entries = list(_client_cache.values())
        _client_cache.clear()
    for client, _, _ in entries:
        close = getattr(client, "close", None)
        if callable(close) and not inspect.iscoroutinefunction(close):
            try:
                close()
            except Exception:
                pass


def cleanup_stale_async_clients() -> None:
    with _client_cache_lock:
        stale = [key for key, (_, _, loop) in _client_cache.items() if loop is not None and loop.is_closed()]
        for key in stale:
            _client_cache.pop(key, None)

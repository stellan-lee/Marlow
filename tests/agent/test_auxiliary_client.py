"""Tests for retained Codex and custom auxiliary-model paths."""
import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from agent.auxiliary_client import (
    _CodexCompletions, _build_call_kwargs, _is_model_not_found_error, _is_payment_error,
    _is_rate_limit_error, _normalize_aux_provider, _read_codex_access_token,
)


def _jwt(claims):
    enc=lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{enc({'alg':'none'})}.{enc(claims)}.sig"


def test_normalize_retained_providers():
    assert _normalize_aux_provider("codex") == "openai-codex"
    assert _normalize_aux_provider("openai-codex") == "openai-codex"
    assert _normalize_aux_provider("custom") == "custom"


def test_custom_call_omits_default_output_cap():
    kwargs = _build_call_kwargs(
        provider="custom", model="local-model",
        messages=[{"role":"user","content":"hi"}],
        max_tokens=1234, base_url="http://localhost:8080/v1",
    )
    assert "max_tokens" not in kwargs
    assert "max_completion_tokens" not in kwargs


def test_read_codex_token_from_auth_store():
    with patch(
        "marlow_cli.auth._read_codex_tokens",
        return_value={"tokens": {"access_token": "token"}},
    ):
        assert _read_codex_access_token() == "token"


def test_expired_codex_token_is_rejected():
    token=_jwt({"exp":int(time.time())-60})
    with patch(
        "marlow_cli.auth._read_codex_tokens",
        return_value={"tokens": {"access_token": token}},
    ):
        assert _read_codex_access_token() is None


def test_missing_auth_store_returns_none():
    with patch("marlow_cli.auth._read_codex_tokens", side_effect=OSError):
        assert _read_codex_access_token() is None


class ApiError(Exception):
    def __init__(self,status_code,message):
        self.status_code=status_code; super().__init__(message)


def test_error_classifiers():
    assert _is_payment_error(ApiError(402,"payment required"))
    assert _is_rate_limit_error(ApiError(429,"too many requests"))
    assert _is_model_not_found_error(ApiError(404,"model does not exist"))
    assert not _is_rate_limit_error(ApiError(500,"server error"))


class _EventStream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def __iter__(self):
        return iter(self.events)

    def close(self):
        self.closed = True


def test_codex_auxiliary_completions_streams_and_reconstructs_response():
    stream = _EventStream([
        {"type": "response.output_text.delta", "delta": "hello"},
        {"type": "response.output_text.delta", "delta": " world"},
        {"type": "response.completed", "response": {"status": "completed", "usage": {"total_tokens": 7}}},
    ])

    class Responses:
        def create(self, **kwargs):
            self.kwargs = kwargs
            return stream

    responses = Responses()
    result = _CodexCompletions(type("Client", (), {"responses": responses})(), "gpt-5").create(
        messages=[{"role": "user", "content": "hi"}]
    )

    assert responses.kwargs["stream"] is True
    assert result.choices[0].message.content == "hello world"
    assert result.usage == {"total_tokens": 7}
    assert stream.closed


def test_codex_auxiliary_completions_raises_for_terminal_failure_and_closes_stream():
    stream = _EventStream([
        {"type": "response.failed", "response": {"status": "failed", "error": "quota exceeded"}},
    ])

    class Responses:
        def create(self, **kwargs):
            return stream

    import pytest

    with pytest.raises(RuntimeError, match="status=failed"):
        _CodexCompletions(type("Client", (), {"responses": Responses()})(), "gpt-5").create(
            messages=[{"role": "user", "content": "hi"}]
        )
    assert stream.closed


def test_codex_auxiliary_completions_accepts_concrete_response_compatibly():
    concrete = SimpleNamespace(
        output=[], output_text="already assembled", usage={"total_tokens": 3}, status="completed"
    )

    class Responses:
        def create(self, **kwargs):
            self.kwargs = kwargs
            return concrete

    responses = Responses()
    result = _CodexCompletions(type("Client", (), {"responses": responses})(), "gpt-5").create(
        messages=[{"role": "user", "content": "hi"}]
    )

    assert responses.kwargs["stream"] is True
    assert result.choices[0].message.content == "already assembled"
    assert result.usage == {"total_tokens": 3}


def test_codex_auxiliary_completions_uses_done_message_text_without_deltas():
    stream = _EventStream([
        {"type": "response.output_item.done", "item": {
            "type": "message", "content": [{"type": "output_text", "text": "done-only text"}],
        }},
        {"type": "response.completed", "response": {"status": "completed"}},
    ])

    class Responses:
        def create(self, **kwargs):
            return stream

    result = _CodexCompletions(type("Client", (), {"responses": Responses()})(), "gpt-5").create(
        messages=[{"role": "user", "content": "hi"}]
    )

    assert result.choices[0].message.content == "done-only text"
    assert stream.closed


def test_codex_auxiliary_completions_does_not_expose_non_message_output_text():
    stream = _EventStream([
        {"type": "response.output_item.done", "item": {
            "type": "reasoning", "content": [{"type": "output_text", "text": "private"}],
        }},
        {"type": "response.output_item.done", "item": {
            "type": "message", "content": [{"type": "text", "text": "public"}],
        }},
        {"type": "response.completed", "response": {"status": "completed"}},
    ])

    class Responses:
        def create(self, **kwargs):
            return stream

    result = _CodexCompletions(type("Client", (), {"responses": Responses()})(), "gpt-5").create(
        messages=[{"role": "user", "content": "hi"}]
    )

    assert result.choices[0].message.content == "public"

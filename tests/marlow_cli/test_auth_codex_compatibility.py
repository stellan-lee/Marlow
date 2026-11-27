"""Profile authentication diagnostics for changing Codex CLI versions."""

from types import SimpleNamespace

import pytest

from marlow_cli import auth


@pytest.fixture
def profile_environment(monkeypatch, tmp_path):
    environment = SimpleNamespace(
        env={"HOME": str(tmp_path)},
        workspace=tmp_path,
        arguments=lambda: ["-c", 'cli_auth_credentials_store="file"'],
        redact=lambda message: message.replace("SYNTHETIC_SECRET", "[REDACTED]"),
    )
    monkeypatch.setattr(auth, "environment", lambda: environment)
    monkeypatch.setattr(auth, "resolve_codex_binary", lambda settings: "/profile/bin/codex")
    monkeypatch.setattr("marlow_cli.config.load_config", lambda: {
        "runtime": {"codex": {"binary": "/profile/bin/codex"}},
    })
    return environment


def test_login_uses_profile_arguments_and_preserves_native_exit_code(monkeypatch, profile_environment):
    monkeypatch.setattr("agent.transports.codex_app_server.check_codex_binary", lambda *args, **kwargs: (True, "0.172.0"))
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(auth.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        auth.login_command(None)
    assert exc.value.code == 7
    assert calls == [([
        "/profile/bin/codex", "-c", 'cli_auth_credentials_store="file"', "login",
    ], {"env": profile_environment.env, "cwd": profile_environment.workspace, "check": False})]


def test_invalid_binary_reports_redacted_diagnostic(monkeypatch, profile_environment):
    monkeypatch.setattr("agent.transports.codex_app_server.check_codex_binary", lambda *args, **kwargs: (False, "SYNTHETIC_SECRET failed to execute"))
    with pytest.raises(auth.AuthError, match=r"\[REDACTED\] failed to execute") as exc:
        auth._run_auth(["login"])
    assert exc.value.code == "invalid_codex"


@pytest.mark.parametrize("account,requires_auth,expected", [
    ({"type": "chatgpt"}, True, "ready"),
    (None, False, "ready"),
    (None, True, "auth_required"),
])
def test_status_validates_runtime_before_reading_account(monkeypatch, profile_environment, account, requires_auth, expected):
    calls = []

    class Client:
        version = "0.172.0"

        def __init__(self, **kwargs):
            assert kwargs == {"codex_bin": "/profile/bin/codex", "environment": profile_environment}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, params, **kwargs):
            calls.append(method)
            return {"account": account, "requiresOpenaiAuth": requires_auth}

    def validate(client, environment, *, cwd, bridge_enabled=False):
        assert environment is profile_environment
        assert cwd == str(profile_environment.workspace)
        assert bridge_enabled is False
        calls.append("validate")

    monkeypatch.setattr("agent.transports.codex_app_server.CodexAppServerClient", Client)
    monkeypatch.setattr("agent.transports.codex_app_server_session.validate_codex_runtime", validate)
    assert auth.get_codex_auth_status() == {
        "logged_in": expected == "ready", "status": expected, "version": "0.172.0",
    }
    assert calls == ["validate", "account/read"]


def test_status_keeps_runtime_failure_distinct_from_missing_auth(monkeypatch, profile_environment):
    class Client:
        version = "0.172.0"

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, *args, **kwargs):
            pytest.fail("Account lookup must follow runtime validation")

    def validate(*args, **kwargs):
        raise ValueError("Codex config/read failed: SYNTHETIC_SECRET")

    monkeypatch.setattr("agent.transports.codex_app_server.CodexAppServerClient", Client)
    monkeypatch.setattr("agent.transports.codex_app_server_session.validate_codex_runtime", validate)
    assert auth.get_codex_auth_status() == {
        "logged_in": False, "status": "incompatible", "version": "0.172.0",
        "error": "Codex config/read failed: [REDACTED]",
    }


def test_missing_binary_has_separate_status(monkeypatch, profile_environment):
    def missing(settings):
        raise FileNotFoundError("Install Codex CLI or configure runtime.codex.binary")

    monkeypatch.setattr(auth, "resolve_codex_binary", missing)
    status = auth.get_codex_auth_status()
    assert status["status"] == "codex_missing"
    assert "runtime.codex.binary" in status["error"]
    with pytest.raises(auth.AuthError) as exc:
        auth._run_auth(["login"])
    assert exc.value.code == "codex_missing"

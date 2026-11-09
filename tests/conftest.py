"""Shared fixtures for the marlow-agent test suite.

Hermetic-test invariants enforced here (see AGENTS.md for rationale):

1. **No credential env vars.** All provider/credential-shaped env vars
   (ending in _API_KEY, _TOKEN, _SECRET, _PASSWORD, _CREDENTIALS, etc.)
   are unset before every test. Local developer keys cannot leak in.
2. **Isolated MARLOW_HOME.** MARLOW_HOME points to a per-test tempdir so
   code reading ``~/.marlow/*`` via ``get_marlow_home()`` can't see the
   real one. (We do NOT also redirect HOME — that broke subprocesses in
   CI. Code using ``Path.home() / ".marlow"`` instead of the canonical
   ``get_marlow_home()`` is a bug to fix at the callsite.)
3. **Deterministic runtime.** TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0.
4. **No MARLOW_SESSION_* inheritance** — the agent's current gateway
   session must not leak into tests.

These invariants make the local test run match CI closely. Gaps that
remain (CPU count, xdist worker count) are addressed by the canonical
test runner at ``scripts/run_tests.sh``.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Per-file process isolation ──────────────────────────────────────────────
# Tests run via ``scripts/run_tests_parallel.py``, which spawns a fresh
# ``python -m pytest <file>`` subprocess per test file. Cross-file state
# leakage (module-level dicts, ContextVars, caches) is impossible: each
# file gets a clean Python interpreter. Intra-file ordering is the test
# author's responsibility — if test A in foo.py mutates state that test B
# in foo.py reads, that's a real bug to fix in the file (it would also
# bite anyone running ``pytest tests/foo.py`` directly).
#
# This replaces the historic _reset_module_state autouse fixture (manual
# state clearing) and the brief experiment with subprocess-per-test
# isolation (too slow at ~17k tests).
#
# See ``scripts/run_tests_parallel.py`` for the runner.


# ── Credential env-var filter ──────────────────────────────────────────────
#
# Any env var in the current process matching ONE of these patterns is
# unset for every test. Developers' local keys cannot leak into assertions
# about "auto-detect provider when key present".

_CREDENTIAL_SUFFIXES = (
    "_API_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_CREDENTIALS",
    "_ACCESS_KEY",
    "_SECRET_ACCESS_KEY",
    "_PRIVATE_KEY",
    "_OAUTH_TOKEN",
    "_WEBHOOK_SECRET",
    "_ENCRYPT_KEY",
    "_APP_SECRET",
    "_CLIENT_SECRET",
    "_CORP_SECRET",
    "_AES_KEY",
)

# Explicit names (for ones that don't fit the suffix pattern)
_CREDENTIAL_NAMES = frozenset({
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
    "MISTRAL_API_KEY",
    "MINIMAX_API_KEY",
    "OLLAMA_API_KEY",
    "WANDB_API_KEY",
    "ELEVENLABS_API_KEY",
    "HONCHO_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "EMAIL_PASSWORD",
    "FEISHU_APP_SECRET",
    "FEISHU_ENCRYPT_KEY",
    "FEISHU_VERIFICATION_TOKEN",
    "TERMINAL_SSH_KEY",
    "SUDO_PASSWORD",
    "TELEGRAM_WEBHOOK_SECRET",
    "WEBHOOK_SECRET",
    "VOICE_TOOLS_OPENAI_KEY",
    "CUSTOM_API_KEY",
    "GEMINI_BASE_URL",
    "OPENAI_BASE_URL",
    "OLLAMA_BASE_URL",
    "GROQ_BASE_URL",
    "XAI_BASE_URL",
})


def _looks_like_credential(name: str) -> bool:
    """True if env var name matches a credential-shaped pattern."""
    if name in _CREDENTIAL_NAMES:
        return True
    return any(name.endswith(suf) for suf in _CREDENTIAL_SUFFIXES)


# MARLOW_* vars that change test behavior by being set. Unset all of these
# unconditionally — individual tests that need them set do so explicitly.
_MARLOW_BEHAVIORAL_VARS = frozenset({
    "MARLOW_YOLO_MODE",
    "MARLOW_INTERACTIVE",
    "MARLOW_QUIET",
    "MARLOW_TOOL_PROGRESS",
    "MARLOW_TOOL_PROGRESS_MODE",
    "MARLOW_MAX_ITERATIONS",
    "MARLOW_SESSION_PLATFORM",
    "MARLOW_SESSION_CHAT_ID",
    "MARLOW_SESSION_CHAT_NAME",
    "MARLOW_SESSION_THREAD_ID",
    "MARLOW_SESSION_SOURCE",
    "MARLOW_SESSION_KEY",
    "MARLOW_GATEWAY_SESSION",
    "MARLOW_CRON_SESSION",
    "_MARLOW_GATEWAY",
    "MARLOW_PLATFORM",
    "MARLOW_MODEL",
    "MARLOW_INFERENCE_MODEL",
    "MARLOW_INFERENCE_PROVIDER",
    "MARLOW_TUI_PROVIDER",
    "MARLOW_MANAGED",
    "MARLOW_DEV",
    "MARLOW_CONTAINER",
    "MARLOW_EPHEMERAL_SYSTEM_PROMPT",
    "MARLOW_TIMEZONE",
    "MARLOW_REDACT_SECRETS",
    "MARLOW_BACKGROUND_NOTIFICATIONS",
    "MARLOW_EXEC_ASK",
    "MARLOW_HOME_MODE",
    "MARLOW_AGENT_USE_LEGACY_SESSION_KEYS",
    "MARLOW_TENANT",
    "TERMINAL_CWD",
    "TERMINAL_ENV",
    "TERMINAL_CONTAINER_CPU",
    "TERMINAL_CONTAINER_DISK",
    "TERMINAL_CONTAINER_MEMORY",
    "TERMINAL_CONTAINER_PERSISTENT",
    "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES",
    "TERMINAL_DOCKER_ORPHAN_REAPER",
    "TERMINAL_DOCKER_RUN_AS_HOST_USER",
    "BROWSER_CDP_URL",
    # Platform allowlists — not credentials, but if set from any source
    # (user shell, earlier leaky test, CI env), they change gateway auth
    # behavior and flake button-authorization tests.
    "TELEGRAM_ALLOWED_USERS",
    "DISCORD_ALLOWED_USERS",
    "SLACK_ALLOWED_USERS",
    "EMAIL_ALLOWED_USERS",
    "FEISHU_ALLOWED_USERS",
    "GATEWAY_ALLOWED_USERS",
    "GATEWAY_ALLOW_ALL_USERS",
    "TELEGRAM_ALLOW_ALL_USERS",
    "DISCORD_ALLOW_ALL_USERS",
    "SLACK_ALLOW_ALL_USERS",
    "EMAIL_ALLOW_ALL_USERS",
    # Gateway home channels are set by /sethome in real profiles. Tests must
    # opt in explicitly or they can subscribe against a real home channel.
    "TELEGRAM_HOME_CHANNEL",
    "TELEGRAM_HOME_CHANNEL_THREAD_ID",
    "TELEGRAM_HOME_CHANNEL_NAME",
    "TELEGRAM_CRON_THREAD_ID",
    "DISCORD_HOME_CHANNEL",
    "DISCORD_HOME_CHANNEL_THREAD_ID",
    "DISCORD_HOME_CHANNEL_NAME",
    "SLACK_HOME_CHANNEL",
    "SLACK_HOME_CHANNEL_THREAD_ID",
    "SLACK_HOME_CHANNEL_NAME",
    "EMAIL_HOME_CHANNEL",
    "EMAIL_HOME_CHANNEL_THREAD_ID",
    "EMAIL_HOME_CHANNEL_NAME",
    "FEISHU_HOME_CHANNEL",
    "FEISHU_HOME_CHANNEL_THREAD_ID",
    "FEISHU_HOME_CHANNEL_NAME",
    # Platform gating — set by load_gateway_config() as a side effect when
    # a config.yaml is present, so individual test bodies that call the
    # loader leak these values into later tests in the same process.
    # Force-clear on every test setup so the leak can't happen.
    "SLACK_REQUIRE_MENTION",
    "SLACK_STRICT_MENTION",
    "SLACK_FREE_RESPONSE_CHANNELS",
    "SLACK_ALLOW_BOTS",
    "SLACK_REACTIONS",
    "DISCORD_REQUIRE_MENTION",
    "DISCORD_FREE_RESPONSE_CHANNELS",
    "TELEGRAM_REQUIRE_MENTION",
})


@pytest.fixture(autouse=True)
def _hermetic_environment(tmp_path, monkeypatch):
    """Blank out all credential/behavioral env vars so local and CI match.

    Also redirects HOME and MARLOW_HOME to per-test tempdirs so code that
    reads ``~/.marlow/*`` can't touch the real one, and pins TZ/LANG so
    datetime/locale-sensitive tests are deterministic.
    """
    # 1. Blank every credential-shaped env var that's currently set.
    for name in list(os.environ.keys()):
        if _looks_like_credential(name):
            monkeypatch.delenv(name, raising=False)

    # 2. Blank behavioral MARLOW_* vars that could change test semantics.
    for name in _MARLOW_BEHAVIORAL_VARS:
        monkeypatch.delenv(name, raising=False)

    # 3. Redirect MARLOW_HOME to a per-test tempdir. Code that reads
    #    ``~/.marlow/*`` via ``get_marlow_home()`` now gets the tempdir.
    #
    #    NOTE: We do NOT also redirect HOME. Doing so broke CI because
    #    some tests (and their transitive deps) spawn subprocesses that
    #    inherit HOME and expect it to be stable. If a test genuinely
    #    needs HOME isolated, it should set it explicitly in its own
    #    fixture. Any code in the codebase reading ``~/.marlow/*`` via
    #    ``Path.home() / ".marlow"`` instead of ``get_marlow_home()``
    #    is a bug to fix at the callsite.
    fake_marlow_home = tmp_path / "marlow_test"
    fake_marlow_home.mkdir()
    (fake_marlow_home / "sessions").mkdir()
    (fake_marlow_home / "cron").mkdir()
    (fake_marlow_home / "memories").mkdir()
    (fake_marlow_home / "skills").mkdir()
    monkeypatch.setenv("MARLOW_HOME", str(fake_marlow_home))

    # 4. Deterministic locale / timezone / hashseed. CI runs in UTC with
    #    C.UTF-8 locale; local dev often doesn't. Pin everything.
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("PYTHONHASHSEED", "0")

    # 4b. Disable AWS IMDS lookups. Without this, any test that ends up
    #     calling has_aws_credentials() / resolve_aws_auth_env_var()
    #     (e.g. provider auto-detect, status command, cron run_job) burns
    #     ~2s waiting for the metadata service at 169.254.169.254 to time
    #     out. Tests don't run on EC2 — IMDS is always unreachable here.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_METADATA_SERVICE_TIMEOUT", "1")
    monkeypatch.setenv("AWS_METADATA_SERVICE_NUM_ATTEMPTS", "1")
    # Tirith auto-installs from GitHub when enabled and missing. Unit tests
    # should never perform that implicit network/bootstrap path; Tirith-specific
    # tests opt back in by patching the security config directly.
    monkeypatch.setenv("TIRITH_ENABLED", "false")

    # 5. Reset plugin singleton so tests don't leak plugins from
    #    ~/.marlow/plugins/ (which, per step 3, is now empty — but the
    #    singleton might still be cached from a previous test).
    try:
        import marlow_cli.plugins as _plugins_mod
        monkeypatch.setattr(_plugins_mod, "_plugin_managers", {})
    except Exception:
        pass
    # Explicitly clear provider-specific base URL overrides that don't match
    # the generic credential-shaped env-var filter above.
    monkeypatch.delenv("GMI_API_KEY", raising=False)
    monkeypatch.delenv("GMI_BASE_URL", raising=False)


# Backward-compat alias — old tests reference this fixture name. Keep it
# as a no-op wrapper so imports don't break.
@pytest.fixture(autouse=True)
def _isolate_marlow_home(_hermetic_environment):
    """Alias preserved for any test that yields this name explicitly."""
    return None


# ── Module-level state reset — replaced by per-file process isolation ──────
#
# Each test FILE runs in a freshly-spawned ``python -m pytest <file>``
# subprocess via ``scripts/run_tests_parallel.py``, so module-level dicts /
# sets / ContextVars from tests in one file cannot leak into tests in
# another file. No manual per-module clearing needed.
#
# Within a single file, ordering is the author's responsibility. If your
# tests in the same file share mutable state, either reset it explicitly
# in a fixture or split them across files.
#
# The skill ``test-suite-cascade-diagnosis`` documents the cascade patterns
# this replaces; the running example was ``test_command_guards`` failing
# 12/15 CI runs because ``tools.approval._session_approved`` carried
# approvals from one test's session into another's.


@pytest.fixture()
def tmp_dir(tmp_path):
    """Provide a temporary directory that is cleaned up automatically."""
    return tmp_path


@pytest.fixture()
def mock_config():
    """Return a minimal marlow config dict suitable for unit tests."""
    return {
        "model": "test/mock-model",
        "toolsets": ["terminal", "file"],
        "max_turns": 10,
        "terminal": {
            "backend": "local",
            "cwd": "/tmp",
            "timeout": 30,
        },
        "compression": {"enabled": False},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "command_allowlist": [],
    }


# ── Per-test timeout — handled by the isolation plugin ─────────────────────
#
# The subprocess-per-test plugin enforces the configured ``isolate_timeout``
# ini key by terminating the child if it overruns. The old SIGALRM-based
# fixture (POSIX-only, didn't work on Windows) is gone.


@pytest.fixture(autouse=True)
def _ensure_current_event_loop(request):
    """Provide a default event loop for sync tests that call get_event_loop().

    Python 3.11+ no longer guarantees a current loop for plain synchronous tests.
    A number of gateway tests still use asyncio.get_event_loop().run_until_complete(...).
    Ensure they always have a usable loop without interfering with pytest-asyncio's
    own loop management for @pytest.mark.asyncio tests.

    On Python 3.12+, ``asyncio.get_event_loop_policy().get_event_loop()`` with no
    *running* loop emits DeprecationWarning; skip that path and install a fresh
    loop via ``new_event_loop()`` instead.
    """
    if request.node.get_closest_marker("asyncio") is not None:
        yield
        return

    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        pass

    if loop is None and sys.version_info < (3, 12):
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            loop = None

    created = loop is None or loop.is_closed()
    if created:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        yield
    finally:
        if created and loop is not None:
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)


# ── Live-system guard ──────────────────────────────────────────────────────
#
# Several test files exercise the gateway-restart / kill code paths
# (``cmd_update``, ``kill_gateway_processes``, ``stop_profile_gateway``).
# When a single test forgets to mock either ``os.kill`` or the global
# ``find_gateway_pids`` helper, the real call leaks out of the hermetic
# environment and finds the developer's live ``marlow-gateway`` process
# via ``psutil`` — sending it SIGTERM mid-test. The shutdown forensics in
# PR #23285 caught this happening 5+ times in 3 days, every time
# correlated with a ``tests/marlow_cli/`` pytest run starting up.
#
# This fixture makes the leak impossible by intercepting the two
# primitives that actually do damage:
#
#  • ``os.kill`` rejects any PID outside the test process subtree with
#    a hard ``RuntimeError`` so the offending test gets a stack trace
#    instead of silently murdering the real gateway.
#  • ``subprocess.run`` / ``subprocess.Popen`` / ``call`` / ``check_call`` /
#    ``check_output`` reject any ``systemctl ... <verb> marlow-gateway``
#    invocation that would mutate the live unit. Read-only systemctl
#    calls (``status``, ``show``, ``list-units``) still pass through.
#
# We intentionally do NOT stub ``find_gateway_pids`` / ``_scan_gateway_pids``
# here — tests of those functions themselves need the real implementation.
# Even if a test gets the live gateway PID back from a real scan, the
# ``os.kill`` guard above catches the actual signal call, and the
# ``systemctl`` guard catches the systemd path. Discovery without
# delivery is harmless.

_LIVE_SYSTEM_GUARD_BYPASS_MARK = "live_system_guard_bypass"


def pytest_configure(config):  # noqa: D401 — pytest hook
    """Register markers used by hermetic conftest."""
    config.addinivalue_line(
        "markers",
        f"{_LIVE_SYSTEM_GUARD_BYPASS_MARK}: bypass the live-system guard "
        "(only for tests that genuinely need real os.kill / subprocess "
        "behaviour — e.g. PTY tests that signal their own child).",
    )


@pytest.fixture(autouse=True)
def _live_system_guard(request, monkeypatch):
    """Block real os.kill / systemctl / gateway-pid scans during tests.

    See block comment above for the why. Tests that genuinely need
    real signal delivery (e.g. PTY tests that SIGINT their own child)
    can opt out with ``@pytest.mark.live_system_guard_bypass``.

    Coverage (every primitive that can deliver a signal to or otherwise
    terminate a foreign process):
      • os.kill, os.killpg (POSIX)
      • subprocess.run / Popen / call / check_call / check_output
      • subprocess.getoutput / getstatusoutput
      • os.system / os.popen
      • pty.spawn
      • asyncio.create_subprocess_exec / create_subprocess_shell
    Subprocess inspection looks at the WHOLE command string (not just
    tokens[0]), so ``bash -c "systemctl restart marlow-gateway"``,
    ``sudo systemctl ...``, ``env systemctl ...``, ``setsid systemctl ...``
    are all caught. ``pkill``/``killall``/``taskkill`` invocations
    targeting marlow/python patterns are also blocked.
    """
    if request.node.get_closest_marker(_LIVE_SYSTEM_GUARD_BYPASS_MARK):
        yield
        return

    import os as _os
    import shlex as _shlex
    import subprocess as _subprocess

    test_pid = _os.getpid()
    # Capture the test process's existing children at fixture start —
    # any *new* children spawned by the test are also allowlisted via
    # the live psutil walk below. Static set keeps the fast path cheap.
    try:
        import psutil as _psutil
        _initial_children = {
            c.pid for c in _psutil.Process(test_pid).children(recursive=True)
        }
    except Exception:
        _psutil = None
        _initial_children = set()

    def _is_own_subtree(pid: int) -> bool:
        # PID 0 means "our own process group"; -1 means "every process we
        # can signal". Both are dangerous when paired with SIGTERM/SIGKILL,
        # but pid 0 is technically scoped to our group so allow it; pid -1
        # is treated as foreign (refuse).
        if pid == 0:
            return True
        if pid < 0:
            return False
        if pid == test_pid or pid in _initial_children:
            return True
        if _psutil is None:
            return False
        try:
            walker = _psutil.Process(pid)
        except Exception:
            # Stale PID — kill would be a no-op anyway, allow it.
            return True
        try:
            for parent in walker.parents():
                if parent.pid == test_pid:
                    return True
        except Exception:
            return False
        return False

    real_kill = _os.kill

    def _guarded_kill(pid, sig, *args, **kwargs):
        if _is_own_subtree(int(pid)):
            return real_kill(pid, sig, *args, **kwargs)
        raise RuntimeError(
            f"tests/conftest.py live-system guard: blocked os.kill("
            f"{pid}, {sig}) — PID is outside the test process subtree. "
            "If this fired in CI it means the test reached a real "
            "kill_gateway_processes / stop_profile_gateway / cmd_update "
            "code path without mocking find_gateway_pids and os.kill. "
            "Mock both, or mark the test with "
            "@pytest.mark.live_system_guard_bypass if real signal "
            "delivery is genuinely required."
        )

    monkeypatch.setattr(_os, "kill", _guarded_kill)

    # ``os.killpg`` is the same risk class — sends a signal to every
    # process in a group. The gateway is a session leader (its own
    # PGID == its PID), so killpg(gateway_pid, SIGTERM) is a one-shot
    # kill of the live process. Allow it only when the target PGID is
    # the test process's own group.
    if hasattr(_os, "killpg"):
        real_killpg = _os.killpg
        own_pgid = _os.getpgrp()

        def _guarded_killpg(pgid, sig, *args, **kwargs):
            if int(pgid) == own_pgid or _is_own_subtree(int(pgid)):
                return real_killpg(pgid, sig, *args, **kwargs)
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"os.killpg({pgid}, {sig}) — PGID is outside the test "
                "process group. See _live_system_guard for the why."
            )

        monkeypatch.setattr(_os, "killpg", _guarded_killpg)

    # ── Subprocess command-string inspection (whole-line) ──────────
    _MARLOW_TOKENS = (
        "marlow-gateway",
        "marlow.service",
        "marlow_cli.main gateway",
        "marlow_cli/main.py gateway",
        "gateway/run.py",
        "marlow gateway",
    )
    _MUTATING_VERBS = (
        "restart", "start", "stop", "kill", "reload",
        "reset-failed", "enable", "disable", "mask", "unmask",
        "daemon-reload", "try-restart", "reload-or-restart",
    )
    _PROCESS_KILLERS = ("pkill", "killall", "taskkill", "skill", "fuser")

    def _cmd_to_string(cmd) -> str:
        if cmd is None:
            return ""
        if isinstance(cmd, (bytes, bytearray)):
            try:
                return bytes(cmd).decode(errors="replace")
            except Exception:
                return ""
        if isinstance(cmd, str):
            return cmd
        if isinstance(cmd, (list, tuple)):
            try:
                return " ".join(str(t) for t in cmd)
            except Exception:
                return ""
        return str(cmd)

    def _matches_marlow_gateway(cmd_str: str) -> bool:
        low = cmd_str.lower()
        return any(tok in low for tok in _MARLOW_TOKENS)

    def _is_blocked_systemctl(cmd) -> bool:
        cmd_str = _cmd_to_string(cmd)
        if "systemctl" not in cmd_str:
            return False
        if not _matches_marlow_gateway(cmd_str):
            return False
        try:
            tokens = _shlex.split(cmd_str)
        except ValueError:
            tokens = cmd_str.split()
        return any(verb in tokens for verb in _MUTATING_VERBS)

    def _is_process_killer(cmd) -> bool:
        cmd_str = _cmd_to_string(cmd)
        try:
            tokens = _shlex.split(cmd_str)
        except ValueError:
            tokens = cmd_str.split()
        if not tokens:
            return False
        for tok in tokens:
            head = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if head in _PROCESS_KILLERS:
                low = cmd_str.lower()
                # pkill -f pattern: catch marlow-themed patterns + a
                # plain "python" -f which would catch the live gateway
                # whose cmdline contains "python -m marlow_cli.main".
                if (
                    "marlow" in low
                    or "gateway" in low
                    or ("python" in low and "-f" in tokens)
                ):
                    return True
        return False

    def _check_subprocess_cmd(name, cmd):
        if _is_blocked_systemctl(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — would mutate the "
                "live marlow-gateway systemd unit. Mock "
                "subprocess.run / _run_systemctl in the test, or "
                "mark with @pytest.mark.live_system_guard_bypass."
            )
        if _is_process_killer(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — process-killer command "
                "targeting marlow/python could hit the live gateway. "
                "Mark with @pytest.mark.live_system_guard_bypass if "
                "intentional."
            )

    def _wrap_subprocess(name, real):
        def _guarded(cmd, *args, **kwargs):
            _check_subprocess_cmd(name, cmd)
            return real(cmd, *args, **kwargs)
        _guarded.__name__ = f"_guarded_{name}"
        # Make the wrapper subscriptable like the wrapped callable when
        # the wrapped object is. ``subprocess.Popen[bytes]`` is used as
        # a type annotation in third-party packages (mcp, etc.); replacing
        # ``Popen`` with a plain function breaks ``Popen[bytes]`` at
        # import time. Defer ``__class_getitem__`` to the original.
        if hasattr(real, "__class_getitem__"):
            _guarded.__class_getitem__ = real.__class_getitem__
        return _guarded

    def _wrap_popen():
        """Subclass Popen so isinstance checks AND Popen[bytes] still work."""
        real = _subprocess.Popen

        class _GuardedPopen(real):  # type: ignore[misc, valid-type]
            def __init__(self, cmd, *args, **kwargs):
                _check_subprocess_cmd("Popen", cmd)
                super().__init__(cmd, *args, **kwargs)

        _GuardedPopen.__name__ = "Popen"
        _GuardedPopen.__qualname__ = "Popen"
        return _GuardedPopen

    real_run = _subprocess.run
    real_popen = _subprocess.Popen
    real_call = _subprocess.call
    real_check_call = _subprocess.check_call
    real_check_output = _subprocess.check_output
    real_getoutput = _subprocess.getoutput
    real_getstatusoutput = _subprocess.getstatusoutput

    monkeypatch.setattr(_subprocess, "run", _wrap_subprocess("run", real_run))
    monkeypatch.setattr(_subprocess, "Popen", _wrap_popen())
    monkeypatch.setattr(_subprocess, "call", _wrap_subprocess("call", real_call))
    monkeypatch.setattr(
        _subprocess, "check_call", _wrap_subprocess("check_call", real_check_call)
    )
    monkeypatch.setattr(
        _subprocess,
        "check_output",
        _wrap_subprocess("check_output", real_check_output),
    )
    monkeypatch.setattr(
        _subprocess, "getoutput", _wrap_subprocess("getoutput", real_getoutput)
    )
    monkeypatch.setattr(
        _subprocess,
        "getstatusoutput",
        _wrap_subprocess("getstatusoutput", real_getstatusoutput),
    )

    # os.system / os.popen — same risk class, completely unwrapped before.
    real_os_system = _os.system
    real_os_popen = _os.popen

    def _guarded_os_system(command):
        _check_subprocess_cmd("os.system", command)
        return real_os_system(command)

    def _guarded_os_popen(cmd, *args, **kwargs):
        _check_subprocess_cmd("os.popen", cmd)
        return real_os_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(_os, "system", _guarded_os_system)
    monkeypatch.setattr(_os, "popen", _guarded_os_popen)

    # pty.spawn — POSIX-only.
    try:
        import pty as _pty
        if hasattr(_pty, "spawn"):
            real_pty_spawn = _pty.spawn

            def _guarded_pty_spawn(argv, *args, **kwargs):
                _check_subprocess_cmd("pty.spawn", argv)
                return real_pty_spawn(argv, *args, **kwargs)

            monkeypatch.setattr(_pty, "spawn", _guarded_pty_spawn)
    except Exception:
        pass

    # asyncio.create_subprocess_* — bypasses subprocess module entirely.
    try:
        import asyncio as _asyncio
        real_async_exec = _asyncio.create_subprocess_exec
        real_async_shell = _asyncio.create_subprocess_shell

        async def _guarded_async_exec(program, *args, **kwargs):
            _check_subprocess_cmd(
                "asyncio.create_subprocess_exec", [program, *args]
            )
            return await real_async_exec(program, *args, **kwargs)

        async def _guarded_async_shell(cmd, *args, **kwargs):
            _check_subprocess_cmd("asyncio.create_subprocess_shell", cmd)
            return await real_async_shell(cmd, *args, **kwargs)

        monkeypatch.setattr(_asyncio, "create_subprocess_exec", _guarded_async_exec)
        monkeypatch.setattr(
            _asyncio, "create_subprocess_shell", _guarded_async_shell
        )
    except Exception:
        pass

    yield

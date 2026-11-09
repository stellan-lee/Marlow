"""
Doctor command for marlow CLI.

Diagnoses issues with Marlow Agent setup.
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path

from marlow_cli.config import get_project_root, get_marlow_home, get_env_path
from marlow_cli.env_loader import load_marlow_dotenv
from marlow_constants import display_marlow_home

PROJECT_ROOT = get_project_root()
MARLOW_HOME = get_marlow_home()
_DHH = display_marlow_home()  # user-facing display path (e.g. ~/.marlow or ~/.marlow/profiles/coder)

# Load environment variables from ~/.marlow/.env so API key checks work
_env_path = get_env_path()
load_marlow_dotenv(marlow_home=_env_path.parent, project_env=PROJECT_ROOT / ".env")

from marlow_cli.colors import Colors, color
_PROVIDER_ENV_HINTS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CUSTOM_BASE_URL",
    "LM_BASE_URL",
    "LM_API_KEY",
)


def _python_install_cmd() -> str:
    return "uv pip install"


def _system_package_install_cmd(pkg: str) -> str:
    if sys.platform == "darwin":
        return f"brew install {pkg}"
    return f"sudo apt install {pkg}"


def _safe_which(cmd: str) -> str | None:
    """shutil.which wrapper resilient to platform monkeypatching in tests."""
    try:
        return shutil.which(cmd)
    except Exception:
        return None


def _has_provider_env_config(content: str) -> bool:
    """Return True when ~/.marlow/.env contains provider auth/base URL settings."""
    return any(key in content for key in _PROVIDER_ENV_HINTS)


def _honcho_is_configured_for_doctor() -> bool:
    """Return True when Honcho is configured, even if this process has no active session."""
    try:
        from plugins.memory.honcho.client import HonchoClientConfig

        cfg = HonchoClientConfig.from_global_config()
        return bool(cfg.enabled and (cfg.api_key or cfg.base_url))
    except Exception:
        return False


def _doctor_tool_availability_detail(toolset: str) -> str:
    """Optional explanatory suffix for toolsets whose doctor status needs context."""
    return ""


def _apply_doctor_tool_availability_overrides(available: list[str], unavailable: list[dict]) -> tuple[list[str], list[dict]]:
    """Adjust runtime-gated tool availability for doctor diagnostics."""
    updated_available = list(available)
    updated_unavailable = []
    for item in unavailable:
        name = item.get("name")
        if name == "honcho" and _honcho_is_configured_for_doctor():
            if "honcho" not in updated_available:
                updated_available.append("honcho")
            continue
        updated_unavailable.append(item)
    return updated_available, updated_unavailable


def check_ok(text: str, detail: str = ""):
    print(f"  {color('✓', Colors.GREEN)} {text}" + (f" {color(detail, Colors.DIM)}" if detail else ""))

def check_warn(text: str, detail: str = ""):
    print(f"  {color('⚠', Colors.YELLOW)} {text}" + (f" {color(detail, Colors.DIM)}" if detail else ""))

def check_fail(text: str, detail: str = ""):
    print(f"  {color('✗', Colors.RED)} {text}" + (f" {color(detail, Colors.DIM)}" if detail else ""))

def check_info(text: str):
    print(f"    {color('→', Colors.CYAN)} {text}")


def _section(title: str) -> None:
    """Print a doctor section banner: blank line + bold cyan ◆ title."""
    print()
    print(color(f"◆ {title}", Colors.CYAN, Colors.BOLD))


def _fail_and_issue(text: str, detail: str, fix: str, issues: list[str]) -> None:
    """Emit a check_fail and append the corresponding fix instruction."""
    check_fail(text, detail)
    issues.append(fix)


def _read_pyproject_version() -> str | None:
    """Read the ``version = "..."`` from ``pyproject.toml`` at the project root.

    Returns None when running from an installed wheel (no pyproject.toml ships
    with the package) or when the file can't be parsed. Reads only the
    ``[project]`` version, ignoring any version strings that appear in other
    tables.
    """
    pyproject = PROJECT_ROOT / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    in_project = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if in_project and line.startswith("version") and "=" in line:
            value = line.split("=", 1)[1]
            value = value.split("#", 1)[0].strip().strip("\"'")
            return value or None
    return None


def _check_version_consistency(issues: list[str]) -> None:
    """Verify pyproject.toml version matches marlow_cli.__version__.

    A git conflict resolution (reset/merge) can revert one file without the
    other, leaving ``marlow --version`` reporting a stale version while
    ``pyproject.toml`` is current. Detect that drift so users can re-sync.
    Silent no-op for installed wheels where pyproject.toml isn't present.
    """
    try:
        from marlow_cli import __version__ as init_version
    except Exception:
        return
    pyproject_version = _read_pyproject_version()
    if pyproject_version is None:
        # Installed wheel or unreadable pyproject — nothing to cross-check.
        return
    if pyproject_version == init_version:
        check_ok("Version files consistent", f"({init_version})")
    else:
        _fail_and_issue(
            "Version mismatch between source files",
            f"(pyproject.toml {pyproject_version} != marlow_cli/__init__.py {init_version})",
            "Re-sync version files (e.g. run 'marlow update', or set "
            "marlow_cli/__init__.py __version__ to match pyproject.toml)",
            issues,
        )


def _check_s6_supervision(issues: list[str]) -> None:
    """Inside a container under our s6 /init, surface what s6 sees.

    Runs as a counterpart to :func:`_check_gateway_service_linger` for
    the systemd-on-host case. No-op everywhere except in the s6
    container so host runs aren't cluttered with irrelevant output.

    Reports:
      - Whether the main Marlow service is up
      - How many per-profile gateway slots are registered (via
        ``S6ServiceManager.list_profile_gateways()``) and how many are
        currently supervised as ``up``
    """
    try:
        from marlow_cli.service_manager import (
            S6ServiceManager,
            detect_service_manager,
        )
    except Exception:
        return

    if detect_service_manager() != "s6":
        return

    _section("s6 Supervision")

    mgr = S6ServiceManager()

    # Static services. They live under /run/service/ via s6-rc symlinks,
    # so the same s6-svstat probe works.
    for static in ("main-marlow",):
        if mgr.is_running(static):
            check_ok(f"{static}: up")
        else:
            check_info(f"{static}: down (expected if not enabled via env)")

    profiles = mgr.list_profile_gateways()
    if not profiles:
        check_info("No per-profile gateways registered yet — create one with `marlow profile create <name>`")
        return

    up_count = sum(1 for p in profiles if mgr.is_running(f"gateway-{p}"))
    check_ok(
        f"Per-profile gateways: {up_count}/{len(profiles)} supervised up"
        + (f" ({', '.join(sorted(profiles))})" if len(profiles) <= 8 else "")
    )


def _check_gateway_service_linger(issues: list[str]) -> None:
    """Warn when a systemd user gateway service will stop after logout.

    Skipped inside a container running under s6 — the linger concept
    (user-systemd surviving SSH logout) doesn't apply there, and the
    s6 supervision state is surfaced separately by
    ``_check_s6_supervision``.
    """
    try:
        from marlow_cli.gateway import (
            get_systemd_linger_status,
            get_systemd_unit_path,
            is_linux,
        )
        from marlow_cli.service_manager import detect_service_manager
    except Exception as e:
        check_warn("Gateway service linger", f"(could not import gateway helpers: {e})")
        return

    if not is_linux():
        return

    # Inside a container under our s6 /init, _check_s6_supervision
    # reports the live supervision state; the linger warning would be
    # confusing here (no systemd, no logout, no "lingering" concept).
    if detect_service_manager() == "s6":
        return

    unit_path = get_systemd_unit_path()
    if not unit_path.exists():
        return

    _section("Gateway Service")
    linger_enabled, linger_detail = get_systemd_linger_status()
    if linger_enabled is True:
        check_ok("Systemd linger enabled", "(gateway service survives logout)")
    elif linger_enabled is False:
        check_warn("Systemd linger disabled", "(gateway may stop after logout)")
        check_info("Run: sudo loginctl enable-linger $USER")
        issues.append("Enable linger for the gateway user service: sudo loginctl enable-linger $USER")
    else:
        check_warn("Could not verify systemd linger", f"({linger_detail})")



def run_doctor(args):
    """Run diagnostic checks."""
    should_fix = getattr(args, 'fix', False)
    ack_target = getattr(args, 'ack', None)

    # Doctor runs from the interactive CLI, so CLI-gated tool availability
    # checks (like cronjob management) should see the same context as `marlow`.
    os.environ.setdefault("MARLOW_INTERACTIVE", "1")

    # Handle `marlow doctor --ack <id>` as a fast path. Persist the ack and
    # return without running the rest of the diagnostics — the user has
    # already seen the advisory and just wants to silence it.
    if ack_target:
        from marlow_cli.security_advisories import (
            ADVISORIES,
            ack_advisory,
        )
        valid_ids = {a.id for a in ADVISORIES}
        if ack_target not in valid_ids:
            print(color(
                f"Unknown advisory ID: {ack_target!r}. Known IDs: "
                f"{', '.join(sorted(valid_ids)) or '(none)'}",
                Colors.RED,
            ))
            sys.exit(2)
        if ack_advisory(ack_target):
            print(color(
                f"  ✓ Acknowledged advisory {ack_target}. "
                f"It will no longer trigger startup banners.",
                Colors.GREEN,
            ))
        else:
            print(color(
                f"  ✗ Failed to persist ack for {ack_target}. "
                f"Check ~/.marlow/config.yaml is writable.",
                Colors.RED,
            ))
            sys.exit(1)
        return

    issues = []
    manual_issues = []  # issues that can't be auto-fixed
    fixed_count = 0

    print()
    print(color("┌─────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│                 🩺 Marlow Doctor                        │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.CYAN))

    _section("Security Advisories")
    try:
        from marlow_cli.security_advisories import (
            detect_compromised,
            filter_unacked,
            full_remediation_text,
            get_acked_ids,
        )
        all_hits = detect_compromised()
        fresh_hits = filter_unacked(all_hits)
        if fresh_hits:
            for hit in fresh_hits:
                check_fail(
                    f"{hit.advisory.title}",
                    f"({hit.package}=={hit.installed_version})",
                )
                # Print the full remediation block, indented under the
                # check_fail header so it reads as a single section.
                for line in full_remediation_text(hit):
                    if line:
                        print(f"    {color(line, Colors.YELLOW)}")
                    else:
                        print()
                # Funnel into the action list so the summary block surfaces it
                # for users who scroll past the section.
                manual_issues.append(
                    f"Resolve security advisory {hit.advisory.id}: "
                    f"uninstall {hit.package}=={hit.installed_version} and "
                    f"rotate credentials, then run "
                    f"`marlow doctor --ack {hit.advisory.id}`."
                )
            # Acked-but-still-installed: show as informational so the user
            # knows the package is still on disk after the ack.
            acked_ids = get_acked_ids()
            for h in all_hits:
                if h.advisory.id in acked_ids:
                    check_warn(
                        f"{h.package}=={h.installed_version} still installed "
                        f"(advisory {h.advisory.id} acknowledged)",
                    )
        else:
            check_ok("No active security advisories")
    except Exception as e:
        # Never let a bug in the advisory check block the rest of doctor.
        check_warn(f"Security advisory check failed: {e}")

    _section("Python Environment")
    py_version = sys.version_info
    if py_version >= (3, 11):
        check_ok(f"Python {py_version.major}.{py_version.minor}.{py_version.micro}")
    elif py_version >= (3, 10):
        check_ok(f"Python {py_version.major}.{py_version.minor}.{py_version.micro}")
        check_warn("Python 3.11+ recommended for RL Training tools (tinker requires >= 3.11)")
    elif py_version >= (3, 8):
        check_warn(f"Python {py_version.major}.{py_version.minor}.{py_version.micro}", "(3.10+ recommended)")
    else:
        _fail_and_issue(
            f"Python {py_version.major}.{py_version.minor}.{py_version.micro}",
            "(3.10+ required)",
            "Upgrade Python to 3.10+",
            issues,
        )

    # Check if in virtual environment
    in_venv = sys.prefix != sys.base_prefix
    if in_venv:
        check_ok("Virtual environment active")
    else:
        check_warn("Not in virtual environment", "(recommended)")

    # Detect drift between pyproject.toml and marlow_cli/__init__.py versions
    # (a git conflict resolution can silently revert one but not the other).
    _check_version_consistency(issues)

    _section("Required Packages")
    required_packages = [
        ("openai", "OpenAI SDK"),
        ("rich", "Rich (terminal UI)"),
        ("dotenv", "python-dotenv"),
        ("yaml", "PyYAML"),
        ("httpx", "HTTPX"),
    ]

    optional_packages = [
        ("croniter", "Croniter (cron expressions)"),
        ("telegram", "python-telegram-bot"),
        ("discord", "discord.py"),
    ]

    for module, name in required_packages:
        try:
            __import__(module)
            check_ok(name)
        except ImportError:
            _fail_and_issue(name, "(missing)", f"Install {name}: {_python_install_cmd()} {module}", issues)

    for module, name in optional_packages:
        try:
            __import__(module)
            check_ok(name, "(optional)")
        except ImportError:
            check_warn(name, "(optional, not installed)")

    _section("Configuration Files")
    # Check ~/.marlow/.env (primary location for user config)
    env_path = MARLOW_HOME / '.env'
    if env_path.exists():
        check_ok(f"{_DHH}/.env file exists")

        # Check for common issues. Pin encoding to UTF-8 because .env files are
        # written as UTF-8 everywhere in the codebase, while Path.read_text()
        # defaults to the system locale — which crashes on non-UTF-8 Windows
        # locales (e.g. GBK) as soon as the file contains any non-ASCII byte.
        content = env_path.read_text(encoding="utf-8")
        if _has_provider_env_config(content):
            check_ok("API key or custom endpoint configured")
        else:
            check_warn(f"No API key found in {_DHH}/.env")
            issues.append("Run 'marlow setup' to configure API keys")
    else:
        # Also check project root as fallback
        fallback_env = PROJECT_ROOT / '.env'
        if fallback_env.exists():
            check_ok(".env file exists (in project directory)")
        else:
            check_fail(f"{_DHH}/.env file missing")
            if should_fix:
                env_path.parent.mkdir(parents=True, exist_ok=True)
                env_path.touch()
                # .env holds API keys — restrict to owner-only access from
                # creation. touch() obeys umask which is commonly 0o022,
                # leaving the file world-readable; tighten explicitly.
                try:
                    os.chmod(str(env_path), 0o600)
                except OSError:
                    pass
                check_ok(f"Created empty {_DHH}/.env")
                check_info("Run 'marlow setup' to configure API keys")
                fixed_count += 1
            else:
                check_info("Run 'marlow setup' to create one")
                issues.append("Run 'marlow setup' to create .env")

    # Check ~/.marlow/config.yaml (primary) or project cli-config.yaml (fallback)
    config_path = MARLOW_HOME / 'config.yaml'
    if config_path.exists():
        check_ok(f"{_DHH}/config.yaml exists")

        try:
            from marlow_cli.runtime_provider import resolve_runtime_provider
            resolve_runtime_provider()
            check_ok("Profile Codex configuration is valid")
        except Exception as exc:
            _fail_and_issue("Invalid Codex configuration", f"({exc})",
                "Move retired model/provider settings to runtime/codex/config.toml; run marlow login for this profile.", issues)

    else:
        fallback_config = PROJECT_ROOT / 'cli-config.yaml'
        if fallback_config.exists():
            check_ok("cli-config.yaml exists (in project directory)")
        else:
            if should_fix:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                example_config = PROJECT_ROOT / 'cli-config.yaml.example'
                if example_config.exists():
                    shutil.copy2(str(example_config), str(config_path))
                    check_ok(f"Created {_DHH}/config.yaml from cli-config.yaml.example")
                else:
                    from marlow_cli.config import DEFAULT_CONFIG, save_config
                    save_config(DEFAULT_CONFIG)
                    check_ok(f"Created {_DHH}/config.yaml from defaults")
                fixed_count += 1
            else:
                check_warn("config.yaml not found", "(using defaults)")

    # Check config version and stale keys
    config_path = MARLOW_HOME / 'config.yaml'
    if config_path.exists():
        try:
            from marlow_cli.config import check_config_version, migrate_config
            current_ver, latest_ver = check_config_version()
            if current_ver < latest_ver:
                check_warn(
                    f"Config version outdated (v{current_ver} → v{latest_ver})",
                    "(new settings available)"
                )
                if should_fix:
                    try:
                        migrate_config(interactive=False, quiet=False)
                        check_ok("Config migrated to latest version")
                        fixed_count += 1
                    except Exception as mig_err:
                        check_warn(f"Auto-migration failed: {mig_err}")
                        issues.append("Run 'marlow setup' to migrate config")
                else:
                    issues.append("Run 'marlow doctor --fix' or 'marlow setup' to migrate config")
            else:
                check_ok(f"Config version up to date (v{current_ver})")
        except Exception:
            pass

        # Detect stale root-level model keys (known bug source — PR #4329)
        try:
            import yaml
            with open(config_path, encoding="utf-8") as f:
                raw_config = yaml.safe_load(f) or {}
            stale_root_keys = [k for k in ("provider", "base_url") if k in raw_config and isinstance(raw_config[k], str)]
            if stale_root_keys:
                check_warn(
                    f"Stale root-level config keys: {', '.join(stale_root_keys)}",
                    "(should be under 'model:' section)"
                )
                issues.append('Retired root-level inference settings need manual migration to runtime/codex/config.toml; historical data was left intact.')
        except Exception:
            pass

        # Validate config structure before checking runtime credentials.
        try:
            from marlow_cli.config import validate_config_structure
            config_issues = validate_config_structure()
            if config_issues:
                _section("Config Structure")
                for ci in config_issues:
                    if ci.severity == "error":
                        check_fail(ci.message)
                    else:
                        check_warn(ci.message)
                    # Show the hint indented
                    for hint_line in ci.hint.splitlines():
                        check_info(hint_line)
                    issues.append(ci.message)
        except Exception:
            pass


    _section("Auth Providers")
    try:
        from marlow_cli.auth import get_codex_auth_status
        codex_status = get_codex_auth_status()
        if codex_status.get("logged_in"):
            check_ok("OpenAI Codex auth", "(logged in)")
        else:
            check_warn("OpenAI Codex auth", "(not logged in)")
            if codex_status.get("error"):
                check_info(codex_status["error"])
            if not _safe_which("codex"):
                check_info("codex CLI not installed (optional; only needed to import an existing login)")
    except Exception as exc:
        check_warn("OpenAI Codex auth", f"(could not check: {exc})")

    _section("Directory Structure")
    marlow_home = MARLOW_HOME
    if marlow_home.exists():
        check_ok(f"{_DHH} directory exists")
    elif should_fix:
        marlow_home.mkdir(parents=True, exist_ok=True)
        check_ok(f"Created {_DHH} directory")
        fixed_count += 1
    else:
        check_warn(f"{_DHH} not found", "(will be created on first use)")

    # Check expected subdirectories
    expected_subdirs = ["cron", "sessions", "logs", "skills", "memories"]
    for subdir_name in expected_subdirs:
        subdir_path = marlow_home / subdir_name
        if subdir_path.exists():
            check_ok(f"{_DHH}/{subdir_name}/ exists")
        elif should_fix:
            subdir_path.mkdir(parents=True, exist_ok=True)
            check_ok(f"Created {_DHH}/{subdir_name}/")
            fixed_count += 1
        else:
            check_warn(f"{_DHH}/{subdir_name}/ not found", "(will be created on first use)")

    # Check for SOUL.md persona file
    soul_path = marlow_home / "SOUL.md"
    if soul_path.exists():
        content = soul_path.read_text(encoding="utf-8").strip()
        # Check if it's just the template comments (no real content)
        lines = [l for l in content.splitlines() if l.strip() and not l.strip().startswith(("<!--", "-->", "#"))]
        if lines:
            check_ok(f"{_DHH}/SOUL.md exists (persona configured)")
        else:
            check_info(f"{_DHH}/SOUL.md exists but is empty — edit it to set profile instructions")
    else:
        check_warn(f"{_DHH}/SOUL.md not found", "(create it to set profile instructions)")
        if should_fix:
            soul_path.parent.mkdir(parents=True, exist_ok=True)
            soul_path.write_text(
                "# Marlow Agent Persona\n\n"
                "<!-- Edit this file to customize how Marlow communicates. -->\n\n"
                "You are Marlow, a helpful AI assistant.\n",
                encoding="utf-8",
            )
            check_ok(f"Created {_DHH}/SOUL.md with basic template")
            fixed_count += 1

    # Check memory directory
    memories_dir = marlow_home / "memories"
    if memories_dir.exists():
        check_ok(f"{_DHH}/memories/ directory exists")
        memory_file = memories_dir / "MEMORY.md"
        user_file = memories_dir / "USER.md"
        if memory_file.exists():
            size = len(memory_file.read_text(encoding="utf-8").strip())
            check_ok(f"MEMORY.md exists ({size} chars)")
        else:
            check_info("MEMORY.md not created yet (will be created when the agent first writes a memory)")
        if user_file.exists():
            size = len(user_file.read_text(encoding="utf-8").strip())
            check_ok(f"USER.md exists ({size} chars)")
        else:
            check_info("USER.md not created yet (will be created when the agent first writes a memory)")
    else:
        check_warn(f"{_DHH}/memories/ not found", "(will be created on first use)")
        if should_fix:
            memories_dir.mkdir(parents=True, exist_ok=True)
            check_ok(f"Created {_DHH}/memories/")
            fixed_count += 1

    # Check SQLite session store
    state_db_path = marlow_home / "state.db"
    if state_db_path.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(state_db_path))
            cursor = conn.execute("SELECT COUNT(*) FROM sessions")
            count = cursor.fetchone()[0]
            conn.close()
            check_ok(f"{_DHH}/state.db exists ({count} sessions)")
        except Exception as e:
            check_warn(f"{_DHH}/state.db exists but has issues: {e}")
    else:
        check_info(f"{_DHH}/state.db not created yet (will be created on first session)")

    # Check WAL file size (unbounded growth indicates missed checkpoints)
    wal_path = marlow_home / "state.db-wal"
    if wal_path.exists():
        try:
            wal_size = wal_path.stat().st_size
            if wal_size > 50 * 1024 * 1024:  # 50 MB
                check_warn(
                    f"WAL file is large ({wal_size // (1024*1024)} MB)",
                    "(may indicate missed checkpoints)"
                )
                if should_fix:
                    import sqlite3
                    conn = sqlite3.connect(str(state_db_path))
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    conn.close()
                    new_size = wal_path.stat().st_size if wal_path.exists() else 0
                    check_ok(f"WAL checkpoint performed ({wal_size // 1024}K → {new_size // 1024}K)")
                    fixed_count += 1
                else:
                    issues.append("Large WAL file — run 'marlow doctor --fix' to checkpoint")
            elif wal_size > 10 * 1024 * 1024:  # 10 MB
                check_info(f"WAL file is {wal_size // (1024*1024)} MB (normal for active sessions)")
        except Exception:
            pass

    _check_gateway_service_linger(issues)
    _check_s6_supervision(issues)

    if os.name == "posix":
        _section("Command Installation")
        # Determine the venv entry point location
        _venv_bin = None
        for _venv_name in ("venv", ".venv"):
            _candidate = PROJECT_ROOT / _venv_name / "bin" / "marlow"
            if _candidate.exists():
                _venv_bin = _candidate
                break

        # Determine the expected command link directory (mirrors install.sh logic)
        _cmd_link_dir = Path.home() / ".local" / "bin"
        _cmd_link_display = "~/.local/bin"
        _cmd_link = _cmd_link_dir / "marlow"

        if _venv_bin is None:
            check_warn(
                "Venv entry point not found",
                "(marlow not in venv/bin/ or .venv/bin/ — reinstall with pip install -e '.[all]')"
            )
            manual_issues.append(
                f"Reinstall entry point: cd {PROJECT_ROOT} && source venv/bin/activate && pip install -e '.[all]'"
            )
        else:
            check_ok(f"Venv entry point exists ({_venv_bin.relative_to(PROJECT_ROOT)})")

            # Check the symlink at the command link location
            if _cmd_link.is_symlink():
                _target = _cmd_link.resolve()
                _expected = _venv_bin.resolve()
                if _target == _expected:
                    check_ok(f"{_cmd_link_display}/marlow → correct target")
                else:
                    check_warn(
                        f"{_cmd_link_display}/marlow points to wrong target",
                        f"(→ {_target}, expected → {_expected})"
                    )
                    if should_fix:
                        _cmd_link.unlink()
                        _cmd_link.symlink_to(_venv_bin)
                        check_ok(f"Fixed symlink: {_cmd_link_display}/marlow → {_venv_bin}")
                        fixed_count += 1
                    else:
                        issues.append(f"Broken symlink at {_cmd_link_display}/marlow — run 'marlow doctor --fix'")
            elif _cmd_link.exists():
                # It's a regular file, not a symlink — possibly a wrapper script
                check_ok(f"{_cmd_link_display}/marlow exists (non-symlink)")
            else:
                check_fail(
                    f"{_cmd_link_display}/marlow not found",
                    "(marlow command may not work outside the venv)"
                )
                if should_fix:
                    _cmd_link_dir.mkdir(parents=True, exist_ok=True)
                    _cmd_link.symlink_to(_venv_bin)
                    check_ok(f"Created symlink: {_cmd_link_display}/marlow → {_venv_bin}")
                    fixed_count += 1

                    # Check if the link dir is on PATH
                    _path_dirs = os.environ.get("PATH", "").split(os.pathsep)
                    if str(_cmd_link_dir) not in _path_dirs:
                        check_warn(
                            f"{_cmd_link_display} is not on your PATH",
                            "(add it to your shell config: export PATH=\"$HOME/.local/bin:$PATH\")"
                        )
                        manual_issues.append(f"Add {_cmd_link_display} to your PATH")
                else:
                    issues.append(f"Missing {_cmd_link_display}/marlow symlink — run 'marlow doctor --fix'")

    _section("External Tools")
    # Git
    if _safe_which("git"):
        check_ok("git")
    else:
        check_warn("git not found", "(optional)")

    # ripgrep (optional, for faster file search)
    if _safe_which("rg"):
        check_ok("ripgrep (rg)", "(faster file search)")
    else:
        check_warn("ripgrep (rg) not found", "(file search uses grep fallback)")
        check_info(f"Install for faster search: {_system_package_install_cmd('ripgrep')}")

    # Docker (optional)
    terminal_env = os.getenv("TERMINAL_ENV", "local")
    try:
        from marlow_constants import is_container as _is_container
        running_in_container = _is_container()
    except Exception:
        running_in_container = False

    if running_in_container:
        # Inside our container the Docker terminal backend is not
        # configured by default (Docker-in-Docker isn't set up); the
        # local backend is the intended one. Skip the noisy "docker
        # not found" warning. If the user has explicitly chosen
        # TERMINAL_ENV=docker inside the container they likely mounted
        # /var/run/docker.sock, so fall through to the normal check.
        if terminal_env != "docker":
            check_info(
                "Running inside a container — using local terminal backend "
                "(docker-in-docker is not configured by default)"
            )
            # Skip to next section; Docker isn't relevant here.
            terminal_env = "local"
    if terminal_env == "docker":
        if _safe_which("docker"):
            # Check if docker daemon is running
            try:
                result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
            except subprocess.TimeoutExpired:
                result = None
            if result is not None and result.returncode == 0:
                check_ok("docker", "(daemon running)")
            else:
                _fail_and_issue("docker daemon not running", "", "Start Docker daemon", issues)
        else:
            _fail_and_issue(
                "docker not found",
                "(required for TERMINAL_ENV=docker)",
                "Install Docker or change TERMINAL_ENV",
                issues,
            )
    elif _safe_which("docker"):
        check_ok("docker", "(optional)")
    elif running_in_container:
        pass  # already explained above
    else:
        check_warn("docker not found", "(optional)")

    # SSH (if using ssh backend)
    if terminal_env == "ssh":
        ssh_host = os.getenv("TERMINAL_SSH_HOST")
        if ssh_host:
            ssh_user = os.getenv("TERMINAL_SSH_USER")
            ssh_port = os.getenv("TERMINAL_SSH_PORT")
            ssh_key = os.getenv("TERMINAL_SSH_KEY")
            target = f"{ssh_user}@{ssh_host}" if ssh_user else ssh_host
            cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]
            if ssh_port:
                cmd += ["-p", ssh_port]
            if ssh_key:
                cmd += ["-i", os.path.expanduser(ssh_key)]
            cmd += [target, "echo ok"]
            # Try to connect
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=15
                )
            except subprocess.TimeoutExpired:
                result = None
            if result is not None and result.returncode == 0:
                check_ok(f"SSH connection to {ssh_host}")
            else:
                _fail_and_issue(f"SSH connection to {ssh_host}", "", f"Check SSH configuration for {ssh_host}", issues)
        else:
            _fail_and_issue(
                "TERMINAL_SSH_HOST not set",
                "(required for TERMINAL_ENV=ssh)",
                "Set TERMINAL_SSH_HOST in .env",
                issues,
            )

    # Node.js + agent-browser (for browser automation tools)
    if _safe_which("node"):
        check_ok("Node.js")
        # Check if agent-browser is installed
        agent_browser_path = PROJECT_ROOT / "node_modules" / "agent-browser"
        agent_browser_ok = False
        if agent_browser_path.exists():
            check_ok("agent-browser (Node.js)", "(browser automation)")
            agent_browser_ok = True
        elif shutil.which("agent-browser"):
            check_ok("agent-browser", "(browser automation)")
            agent_browser_ok = True
        else:
            check_warn("agent-browser not installed", "(run: npm install)")

        # Chromium presence — the browser tools silently fail to register when
        # agent-browser is found but no Playwright-managed Chromium is on disk
        # (tools/browser_tool.py::check_browser_requirements filters them out
        # before the agent ever sees them).  Reuse the exact predicate it uses
        # so the two checks cannot diverge.
        if agent_browser_ok:
            try:
                # Lazy import: browser_tool is a ~150KB module we don't want
                # to eagerly load in every `marlow doctor` invocation.
                from tools.browser_tool import (
                    _chromium_installed,
                    _get_cdp_override,
                    _using_lightpanda_engine,
                )
            except Exception:
                # If browser_tool can't even import, that's a separate bug
                # surfaced elsewhere; don't crash doctor.
                pass
            else:
                # CDP and Lightpanda use an externally supplied browser engine.
                skip_chromium_check = (
                    bool(_get_cdp_override())
                    or _using_lightpanda_engine()
                )
                if not skip_chromium_check:
                    if _chromium_installed():
                        check_ok("Playwright Chromium", "(browser engine)")
                    else:
                        check_warn(
                            "Playwright Chromium not installed",
                            "(browser_* tools will be hidden from the agent)",
                        )
                        check_info(
                            f"Install with: cd {PROJECT_ROOT} && "
                            "npx playwright install --with-deps chromium"
                        )
    else:
        check_warn("Node.js not found", "(optional, needed for browser tools)")

    # npm audit for all Node.js packages
    _npm_bin = _safe_which("npm")
    if _npm_bin:
        npm_dirs = [(PROJECT_ROOT, "Browser tools (agent-browser)")]
        for npm_dir, label in npm_dirs:
            if not (npm_dir / "node_modules").exists():
                continue
            try:
                audit_result = subprocess.run(
                    [_npm_bin, "audit", "--json"],
                    cwd=str(npm_dir),
                    capture_output=True, text=True, timeout=30,
                )
                import json as _json
                audit_data = _json.loads(audit_result.stdout) if audit_result.stdout.strip() else {}
                vuln_count = audit_data.get("metadata", {}).get("vulnerabilities", {})
                critical = vuln_count.get("critical", 0)
                high = vuln_count.get("high", 0)
                moderate = vuln_count.get("moderate", 0)
                total = critical + high + moderate
                if total == 0:
                    check_ok(f"{label} deps", "(no known vulnerabilities)")
                elif critical > 0 or high > 0:
                    check_warn(
                        f"{label} deps",
                        f"({critical} critical, {high} high, {moderate} moderate — run: cd {npm_dir} && npm audit fix)"
                    )
                    issues.append(
                        f"{label} has {total} npm "
                        f"{'vulnerability' if total == 1 else 'vulnerabilities'}"
                    )
                else:
                    check_ok(
                        f"{label} deps",
                        f"({moderate} moderate "
                        f"{'vulnerability' if moderate == 1 else 'vulnerabilities'})",
                    )
            except Exception:
                pass

    _section("Tool Availability")
    try:
        # Add project root to path for imports
        sys.path.insert(0, str(PROJECT_ROOT))
        from model_tools import check_tool_availability, TOOLSET_REQUIREMENTS

        available, unavailable = check_tool_availability()
        available, unavailable = _apply_doctor_tool_availability_overrides(available, unavailable)

        for tid in available:
            info = TOOLSET_REQUIREMENTS.get(tid, {})
            check_ok(info.get("name", tid), _doctor_tool_availability_detail(tid))

        for item in unavailable:
            env_vars = item.get("missing_vars") or item.get("env_vars") or []
            if env_vars:
                vars_str = ", ".join(env_vars)
                check_warn(item["name"], f"(missing {vars_str})")
            else:
                check_warn(item["name"], "(system dependency not met)")

        # Count disabled tools with API key requirements
        api_disabled = [u for u in unavailable if (u.get("missing_vars") or u.get("env_vars"))]
        if api_disabled:
            issues.append("Run 'marlow setup' to configure missing API keys for full tool access")
    except Exception as e:
        check_warn("Could not check tool availability", f"({e})")

    _section("Memory Provider")
    from plugins.memory import RETIRED_MEMORY_PROVIDER, RETIRED_MEMORY_PROVIDER_MESSAGE

    _active_memory_provider = ""
    try:
        import yaml as _yaml
        _mem_cfg_path = MARLOW_HOME / "config.yaml"
        if _mem_cfg_path.exists():
            with open(_mem_cfg_path, encoding="utf-8") as _f:
                _raw_cfg = _yaml.safe_load(_f) or {}
            _active_memory_provider = (_raw_cfg.get("memory") or {}).get("provider", "")
    except Exception:
        pass

    if not _active_memory_provider:
        check_ok("Built-in memory active", "(no external provider configured — this is fine)")
    elif _active_memory_provider == "honcho":
        try:
            from plugins.memory.honcho.client import HonchoClientConfig, resolve_config_path
            hcfg = HonchoClientConfig.from_global_config()
            _honcho_cfg_path = resolve_config_path()

            if not _honcho_cfg_path.exists():
                check_warn("Honcho config not found", "run: marlow memory setup")
            elif not hcfg.enabled:
                check_info(f"Honcho disabled (set enabled: true in {_honcho_cfg_path} to activate)")
            elif not (hcfg.api_key or hcfg.base_url):
                _fail_and_issue(
                    "Honcho API key or base URL not set",
                    "run: marlow memory setup",
                    "No Honcho API key — run 'marlow memory setup'",
                    issues,
                )
            else:
                from plugins.memory.honcho.client import get_honcho_client, reset_honcho_client
                reset_honcho_client()
                try:
                    get_honcho_client(hcfg)
                    check_ok(
                        "Honcho connected",
                        f"workspace={hcfg.workspace_id} mode={hcfg.recall_mode} freq={hcfg.write_frequency}",
                    )
                except Exception as _e:
                    _fail_and_issue("Honcho connection failed", str(_e), f"Honcho unreachable: {_e}", issues)
        except ImportError:
            _fail_and_issue(
                "honcho-ai not installed",
                "pip install honcho-ai",
                "Honcho is set as memory provider but honcho-ai is not installed",
                issues,
            )
        except Exception as _e:
            check_warn("Honcho check failed", str(_e))
    elif _active_memory_provider == RETIRED_MEMORY_PROVIDER:
        check_warn(RETIRED_MEMORY_PROVIDER_MESSAGE)
    else:
        check_warn(
            f"Unsupported memory provider '{_active_memory_provider}'",
            "run: marlow memory setup",
        )

    try:
        from marlow_cli.profiles import list_profiles, _get_wrapper_dir, profile_exists
        import re as _re

        named_profiles = [p for p in list_profiles() if not p.is_default]
        if named_profiles:
            _section("Profiles")
            check_ok(f"{len(named_profiles)} profile(s) found")
            wrapper_dir = _get_wrapper_dir()
            for p in named_profiles:
                parts = []
                if p.gateway_running:
                    parts.append("gateway running")
                if p.model:
                    parts.append(p.model[:30])
                if not (p.path / "config.yaml").exists():
                    parts.append("⚠ missing config")
                if not (p.path / ".env").exists():
                    parts.append("no .env")
                wrapper = wrapper_dir / p.name
                if not wrapper.exists():
                    parts.append("no alias")
                status = ", ".join(parts) if parts else "configured"
                check_ok(f"  {p.name}: {status}")

            # Check for orphan wrappers
            if wrapper_dir.is_dir():
                for wrapper in wrapper_dir.iterdir():
                    if not wrapper.is_file():
                        continue
                    try:
                        content = wrapper.read_text()
                        if "marlow -p" in content:
                            _m = _re.search(r"marlow -p (\S+)", content)
                            if _m and not profile_exists(_m.group(1)):
                                check_warn(f"Orphan alias: {wrapper.name} → profile '{_m.group(1)}' no longer exists")
                    except Exception:
                        pass
    except ImportError:
        pass
    except Exception:
        pass

    print()
    remaining_issues = issues + manual_issues
    if should_fix and fixed_count > 0:
        print(color("─" * 60, Colors.GREEN))
        print(color(f"  Fixed {fixed_count} issue(s).", Colors.GREEN, Colors.BOLD), end="")
        if remaining_issues:
            print(color(f" {len(remaining_issues)} issue(s) require manual intervention.", Colors.YELLOW, Colors.BOLD))
        else:
            print()
        print()
        if remaining_issues:
            for i, issue in enumerate(remaining_issues, 1):
                print(f"  {i}. {issue}")
            print()
    elif remaining_issues:
        print(color("─" * 60, Colors.YELLOW))
        print(color(f"  Found {len(remaining_issues)} issue(s) to address:", Colors.YELLOW, Colors.BOLD))
        print()
        for i, issue in enumerate(remaining_issues, 1):
            print(f"  {i}. {issue}")
        print()
        if not should_fix:
            print(color("  Tip: run 'marlow doctor --fix' to auto-fix what's possible.", Colors.DIM))
    else:
        print(color("─" * 60, Colors.GREEN))
        print(color("  All checks passed! 🎉", Colors.GREEN, Colors.BOLD))

    print()

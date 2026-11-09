"""Lazy dependency bootstrapper for non-Python runtime deps.

Detection and prompting live here in Python — not in install.sh — because:
  1. shutil.which() works on every platform; install.sh needs bash.
  2. Detection is instant; spawning bash for a "is node installed?" check is waste.
  3. Python controls the UX (rich prompts, non-interactive fallback, TTY detection).

install.sh is still the *installation* backend because it has 1900 lines of
battle-tested OS detection and package-manager logic (apt/brew/pacman/dnf/
zypper/…). Reimplementing that in Python would be huge duplication.

Deps that degrade gracefully (ripgrep → grep fallback)
don't need ensure_dependency wired in — only hard-fail sites do (browser tools need Node.js,
browser tool needs agent-browser).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_DEP_CHECKS = {
    "node": lambda: shutil.which("node") is not None,
    "browser": lambda: (
        shutil.which("agent-browser") is not None
        or _has_system_browser()
        or _has_marlow_agent_browser()
    ),
    "ripgrep": lambda: shutil.which("rg") is not None,
}

_DEP_DESCRIPTIONS = {
    "node": "Node.js (required for browser tools)",
    "browser": "Browser engine (Chromium, for web browsing tools)",
    "ripgrep": "ripgrep (fast file search)",
}


def _has_system_browser() -> bool:
    names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
    for name in names:
        if shutil.which(name):
            return True
    return False


def _has_marlow_agent_browser() -> bool:
    from marlow_constants import get_marlow_home
    home = get_marlow_home()
    # install.sh installs globally into $MARLOW_HOME/node/bin/ via npm -g --prefix
    # Also check legacy node_modules/.bin/ path for git-clone installs.
    return (
        (home / "node" / "bin" / "agent-browser").is_file()
        or (home / "node_modules" / ".bin" / "agent-browser").is_file()
    )


def _find_install_script(
    package_dir: Path | None = None,
    repo_root: Path | None = None,
) -> tuple[Path | None, str | None]:
    """Locate the install script — bundled in wheel or in git checkout.

    Returns a (path, shell) tuple, or (None, None) if neither is found.
    """
    if package_dir is None:
        package_dir = Path(__file__).parent
    if repo_root is None:
        repo_root = package_dir.parent

    for script_name, shell in (("install.sh", "bash"),):
        bundled = package_dir / "scripts" / script_name
        if bundled.is_file():
            return bundled, shell
        repo = repo_root / "scripts" / script_name
        if repo.is_file():
            return repo, shell

    return None, None


def ensure_dependency(
    dep: str,
    interactive: bool = True,
) -> bool:
    """Ensure a non-Python dependency is available. Returns True if available."""
    check = _DEP_CHECKS.get(dep)
    if check is None:
        # Unknown dep — don't silently forward to install script.
        return False
    if check():
        return True

    script, shell = _find_install_script()
    if script is None:
        if interactive:
            desc = _DEP_DESCRIPTIONS.get(dep, dep)
            print(f"  {desc} is not installed and no install script was found.")
            print(f"  Install {dep} manually and try again.")
        return False

    if interactive and sys.stdin.isatty():
        desc = _DEP_DESCRIPTIONS.get(dep, dep)
        try:
            reply = input(f"{desc} is not installed. Install now? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if reply not in ("", "y", "yes"):
            return False

    cmd = ["bash", str(script), "--ensure", dep]

    run_env = {**os.environ, "IS_INTERACTIVE": "false"}
    result = subprocess.run(
        cmd,
        env=run_env,
    )
    if result.returncode != 0:
        return False

    if check:
        return check()
    return True

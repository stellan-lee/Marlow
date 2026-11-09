"""Regression checks for Marlow's canonical project identity."""

from pathlib import Path

from marlow_constants import (
    MARLOW_INSTALL_SCRIPT_URL,
    MARLOW_REPOSITORY_GIT_URL,
    MARLOW_REPOSITORY_URL,
)


REPO_ROOT = Path(__file__).parent.parent


def test_canonical_repository_constants():
    assert MARLOW_REPOSITORY_URL == "https://github.com/stellan-lee/Marlow"
    assert MARLOW_REPOSITORY_GIT_URL == "https://github.com/stellan-lee/Marlow.git"
    assert MARLOW_INSTALL_SCRIPT_URL == (
        "https://raw.githubusercontent.com/stellan-lee/Marlow/main/scripts/install.sh"
    )


def test_primary_install_surfaces_use_canonical_repository():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    installer = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert f"curl -fsSL {MARLOW_INSTALL_SCRIPT_URL} | bash" in readme
    assert 'REPO_URL_HTTPS="https://github.com/stellan-lee/Marlow.git"' in installer
    assert 'REPO_URL_SSH="git@github.com:stellan-lee/Marlow.git"' in installer


def test_removed_project_endpoints_do_not_return():
    forbidden = (
        "github.com/NousResearch/marlow-agent",
        "raw.githubusercontent.com/NousResearch/marlow-agent",
        "marlow-agent.nousresearch.com",
        "nousresearch/marlow-agent",
        "ghcr.io/nousresearch/marlow-agent",
        "NOUS MARLOW",
        "created by Nous Research",
        "github.com/AaronWong1999/marlowclaw",
        "github.com/NousResearch/marlow-example-plugins",
    )
    checked = (
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "SECURITY.md",
        REPO_ROOT / "scripts" / "install.sh",
        REPO_ROOT / "marlow_cli" / "main.py",
        REPO_ROOT / "marlow_cli" / "banner.py",
        REPO_ROOT / "marlow_cli" / "config.py",
        REPO_ROOT / "agent" / "prompt_builder.py",
        REPO_ROOT / "skills" / "autonomous-ai-agents" / "marlow-agent" / "SKILL.md",
    )

    for path in checked:
        text = path.read_text(encoding="utf-8")
        for stale in forbidden:
            assert stale not in text, f"stale project identity {stale!r} in {path}"

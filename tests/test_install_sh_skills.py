"""The installer prepares an empty skills directory and preserves profile skills."""

import os
import subprocess
from pathlib import Path

import pytest


INSTALL_SH = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"


@pytest.mark.parametrize("no_skills", [False, True])
def test_config_stage_does_not_copy_bundled_skills(tmp_path, no_skills):
    install_dir = tmp_path / "checkout"
    bundled_skill = install_dir / "skills" / "mcp" / "native-mcp" / "SKILL.md"
    bundled_skill.parent.mkdir(parents=True)
    bundled_skill.write_text("Bundled skill.\n", encoding="utf-8")

    user_home = tmp_path / "home"
    user_home.mkdir()
    profile = user_home / ".marlow"
    env = {**os.environ, "HOME": str(user_home)}
    command = [
        "bash", str(INSTALL_SH), "--stage", "config", "--non-interactive",
        "--dir", str(install_dir), "--marlow-home", str(profile), "--skip-browser",
    ]
    if no_skills:
        command.append("--no-skills")

    subprocess.run(command, env=env, check=True, capture_output=True, text=True, timeout=10)
    skills = profile / "skills"
    assert list(skills.iterdir()) == []
    marker = profile / ".no-bundled-skills"
    assert marker.exists() is no_skills

    custom_skill = skills / "custom" / "SKILL.md"
    custom_skill.parent.mkdir()
    custom_skill.write_text("User skill.\n", encoding="utf-8")
    subprocess.run(command, env=env, check=True, capture_output=True, text=True, timeout=10)
    assert list(skills.iterdir()) == [custom_skill.parent]
    assert custom_skill.read_text(encoding="utf-8") == "User skill.\n"

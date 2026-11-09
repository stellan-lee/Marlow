"""Tests for cmd_update — branch fallback when remote branch doesn't exist."""
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from marlow_cli.main import cmd_update, PROJECT_ROOT


def _make_run_side_effect(branch="main", verify_ok=True, commit_count="0"):
    """Build a side_effect function for subprocess.run that simulates git commands."""

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        # git rev-parse --abbrev-ref HEAD  (get current branch)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{branch}\n", stderr="")

        # git rev-parse --verify origin/{branch}  (check remote branch exists)
        if "rev-parse" in joined and "--verify" in joined:
            rc = 0 if verify_ok else 128
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

        # git rev-list HEAD..origin/{branch} --count
        if "rev-list" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit_count}\n", stderr="")

        # Fallback: return a successful CompletedProcess with empty stdout
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return side_effect


@pytest.fixture
def mock_args():
    return SimpleNamespace()


# ---------------------------------------------------------------------------
# Managed-uv compatibility for tests that patch shutil.which
# ---------------------------------------------------------------------------
# The production code now uses ``ensure_uv()`` / ``update_managed_uv()``
# instead of ``shutil.which("uv")``.  Many tests in this file patch
# ``shutil.which`` to control whether uv is "available" — these autouse
# fixtures make the managed_uv functions delegate to the patched
# ``shutil.which`` so the existing test setup keeps working without
# per-test changes.
@pytest.fixture(autouse=True)
def _patch_managed_uv(request):
    """Make managed_uv helpers follow shutil.which mocking in tests."""
    import shutil

    # resolve_uv delegates to shutil.which("uv") so that test patches
    # on shutil.which flow through naturally.
    def _fake_resolve_uv():
        return shutil.which("uv")

    def _fake_ensure_uv():
        path = shutil.which("uv")
        return (path, False)  # never freshly bootstrapped in tests

    def _fake_update_managed_uv():
        return None  # never actually self-update in tests

    def _fake_rebuild_venv(*args, **kwargs):
        return True  # no-op in tests

    with patch("marlow_cli.managed_uv.resolve_uv", side_effect=_fake_resolve_uv), \
         patch("marlow_cli.managed_uv.ensure_uv", side_effect=_fake_ensure_uv), \
         patch("marlow_cli.managed_uv.update_managed_uv", side_effect=_fake_update_managed_uv), \
         patch("marlow_cli.managed_uv.rebuild_venv", side_effect=_fake_rebuild_venv):
        yield


class TestCmdUpdatePip:
    """Regression tests for pip-install update flows."""

    @patch("shutil.which", return_value="/usr/bin/uv")
    @patch("subprocess.run")
    def test_update_pip_exports_virtualenv_from_sys_prefix(
        self, mock_run, _mock_which, mock_args, monkeypatch
    ):
        from marlow_cli import main as hm

        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.setattr(hm.sys, "prefix", "/tmp/marlow-launcher-venv")
        monkeypatch.setattr(hm.sys, "base_prefix", "/usr")

        hm._cmd_update_pip(mock_args)

        assert mock_run.call_count == 1
        assert mock_run.call_args.args[0] == ["/usr/bin/uv", "pip", "install", "--upgrade", "marlow-agent"]
        assert mock_run.call_args.kwargs["env"]["VIRTUAL_ENV"] == "/tmp/marlow-launcher-venv"

    @patch("shutil.which", return_value="/usr/bin/uv")
    @patch("subprocess.run")
    def test_update_pip_does_not_export_virtualenv_for_system_python(
        self, mock_run, _mock_which, mock_args, monkeypatch
    ):
        from marlow_cli import main as hm

        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.setattr(hm.sys, "prefix", "/usr")
        monkeypatch.setattr(hm.sys, "base_prefix", "/usr")

        hm._cmd_update_pip(mock_args)

        assert mock_run.call_count == 1
        assert "env" not in mock_run.call_args.kwargs


class TestCmdUpdateBranchFallback:
    """cmd_update falls back to main when current branch has no remote counterpart."""

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_falls_back_to_main_when_branch_not_on_remote(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(
            branch="fix/stoicneko", verify_ok=False, commit_count="3"
        )

        cmd_update(mock_args)

        commands = [
            " ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list
        ]

        # rev-list should use origin/main, not origin/fix/stoicneko
        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert len(rev_list_cmds) == 1
        assert "origin/main" in rev_list_cmds[0]
        assert "origin/fix/stoicneko" not in rev_list_cmds[0]

        # pull should use main, not fix/stoicneko
        pull_cmds = [c for c in commands if "pull" in c]
        assert len(pull_cmds) == 1
        assert "main" in pull_cmds[0]

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_uses_current_branch_when_on_remote(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="2"
        )

        cmd_update(mock_args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]

        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert len(rev_list_cmds) == 1
        assert "origin/main" in rev_list_cmds[0]

        pull_cmds = [c for c in commands if "pull" in c]
        assert len(pull_cmds) == 1
        assert "main" in pull_cmds[0]

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_already_up_to_date(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="0"
        )

        cmd_update(mock_args)

        captured = capsys.readouterr()
        assert "Already up to date!" in captured.out

        # Should NOT have called pull
        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        pull_cmds = [c for c in commands if "pull" in c]
        assert len(pull_cmds) == 0

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_does_not_probe_for_forks_when_origin_is_up_to_date(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        """Updates follow origin without classifying or syncing fork remotes."""
        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="0"
        )

        cmd_update(mock_args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        assert not any("remote get-url" in command for command in commands)
        assert not any("upstream" in command for command in commands)
        captured = capsys.readouterr()
        assert "Already up to date!" in captured.out

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_update_refreshes_browser_dependencies(
        self, mock_run, mock_which, mock_args
    ):
        from marlow_cli import main as hm

        mock_which.side_effect = {"uv": "/usr/bin/uv", "npm": "/usr/bin/npm"}.get
        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="1"
        )
        cmd_update(mock_args)

        npm_calls = [
            (call.args[0], call.kwargs.get("cwd"))
            for call in mock_run.call_args_list
            if call.args and call.args[0][0] == "/usr/bin/npm"
        ]

        # Browser dependencies are installed once from the root lockfile.
        assert npm_calls == [(
            ["/usr/bin/npm", "ci", "--no-fund", "--no-audit", "--progress=false"],
            PROJECT_ROOT,
        )]
        # Regression for #18840: root npm installs must stream output
        # (capture_output=False) so postinstall progress is visible.
        root_install_calls = [
            call
            for call in mock_run.call_args_list
            if call.args
            and call.args[0][0] == "/usr/bin/npm"
            and call.args[0][1] == "ci"
            and call.kwargs.get("cwd") == PROJECT_ROOT
            and "--silent" not in call.args[0]
        ]
        assert len(root_install_calls) == 1
        for call in root_install_calls:
            assert call.kwargs.get("capture_output") is False, (
                "repo-root npm install must stream output "
                "(no capture_output) so postinstall progress is visible"
            )

    def test_update_non_interactive_runs_safe_config_migrations(self, mock_args, capsys):
        """Non-interactive updates apply safe migrations before restart."""
        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input") as mock_input, patch(
            "marlow_cli.config.get_missing_env_vars", return_value=["MISSING_KEY"]
        ), patch(
            "marlow_cli.config.get_missing_config_fields",
            return_value=[{"key": "new.option", "default": True}],
        ), patch("marlow_cli.config.check_config_version", return_value=(1, 2)), patch(
            "marlow_cli.config.migrate_config",
            return_value={"env_added": [], "config_added": ["new.option"]},
        ), patch("marlow_cli.main.sys") as mock_sys:
            mock_sys.stdin.isatty.return_value = False
            mock_sys.stdout.isatty.return_value = False
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )

            cmd_update(mock_args)

            mock_input.assert_not_called()
            from marlow_cli.config import migrate_config

            migrate_config.assert_called_once_with(interactive=False, quiet=False)
            captured = capsys.readouterr()
            assert "applying safe config migrations" in captured.out
            assert "API keys require manual entry" in captured.out


class TestCmdUpdateMigrationPrompt:
    """The config-migration prompt names what changed and skips the prompt
    entirely when only the config format version moved.

    Regression guard for the contentless-prompt report (ScottFive / Tt2021):
    previously the prompt printed only counts ("1 new config option") and
    asked "configure them now?" even for pure version bumps, where saying
    yes looked like a no-op.
    """

    def test_version_bump_only_applies_silently_without_prompt(
        self, mock_args, capsys
    ):
        """Only the version moved → apply non-interactively, never prompt."""
        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input") as mock_input, patch(
            "marlow_cli.config.get_missing_env_vars", return_value=[]
        ), patch(
            "marlow_cli.config.get_missing_config_fields", return_value=[]
        ), patch(
            "marlow_cli.config.check_config_version", return_value=(5, 24)
        ), patch(
            "marlow_cli.config.migrate_config",
            return_value={"env_added": [], "config_added": [], "warnings": []},
        ) as mock_migrate:
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )

            cmd_update(mock_args)

            mock_input.assert_not_called()
            mock_migrate.assert_called_once_with(interactive=False, quiet=True)
            out = capsys.readouterr().out
            assert "Updating config format (v5 → v24)" in out
            assert "no new settings to configure" in out
            # The misleading question must NOT appear for a pure version bump.
            assert "configure them now" not in out.lower()

    def test_new_options_are_listed_by_name_before_prompt(
        self, mock_args, capsys
    ):
        """New env/config keys are printed by name so the user can decide."""
        env_items = [
            {"name": "FOO_API_KEY", "description": "Foo service API key"},
        ]
        cfg_items = [
            {"key": "display.new_widget", "description": "New config option: display.new_widget"},
        ]
        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input", return_value="n"), patch(
            "marlow_cli.config.get_missing_env_vars", return_value=env_items
        ), patch(
            "marlow_cli.config.get_missing_config_fields", return_value=cfg_items
        ), patch(
            "marlow_cli.config.check_config_version", return_value=(1, 24)
        ), patch(
            "marlow_cli.config.migrate_config",
            return_value={"env_added": [], "config_added": [], "warnings": []},
        ), patch("marlow_cli.main.sys") as mock_sys:
            mock_sys.stdin.isatty.return_value = True
            mock_sys.stdout.isatty.return_value = True
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )

            cmd_update(mock_args)

            out = capsys.readouterr().out
            # Names, not just counts.
            assert "FOO_API_KEY" in out
            assert "Foo service API key" in out
            assert "display.new_widget" in out


@patch("shutil.which", return_value=None)
@patch("subprocess.run")
def test_update_leaves_profile_skills_untouched(
    mock_run, _mock_which, mock_args, tmp_path, monkeypatch
):
    from pathlib import Path

    mock_run.side_effect = _make_run_side_effect(
        branch="main", verify_ok=True, commit_count="1"
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".marlow"
    active_home = default_home / "profiles" / "work"
    monkeypatch.setenv("MARLOW_HOME", str(active_home))
    default_skills = default_home / "skills"
    active_skills = active_home / "skills"
    default_skills.mkdir(parents=True)
    custom_skill = active_skills / "custom" / "SKILL.md"
    custom_skill.parent.mkdir(parents=True)
    content = "---\nname: custom\n---\nUser skill.\n"
    custom_skill.write_text(content, encoding="utf-8")

    cmd_update(mock_args)

    assert list(default_skills.iterdir()) == []
    assert list(active_skills.iterdir()) == [custom_skill.parent]
    assert custom_skill.read_text(encoding="utf-8") == content


class TestCmdUpdateBranchFlag:
    """``marlow update --branch <name>`` targets the requested branch.

    The CLI default stays 'main'; --branch lets callers pick a different
    target without monkey-patching the implementation.
    """

    def _branch_side_effect(self, current_branch, target_branch, *, checkout_fails=False, track_fails=False, commit_count="0"):
        """Mock side-effect that knows about checkout/track behavior.

        - ``current_branch``  what ``git rev-parse --abbrev-ref HEAD`` returns
        - ``target_branch``   passed via --branch; what we expect the code to switch to
        - ``checkout_fails``  if True, ``git checkout <target>`` returns non-zero
                              (simulates branch absent locally; code should retry with -B)
        - ``track_fails``     if True, ``git checkout -B <target> origin/<target>`` ALSO fails
                              (simulates branch absent on origin too)
        - ``commit_count``    rev-list count returned (0 = up-to-date, >0 = behind)
        """

        def side_effect(cmd, **kwargs):
            joined = " ".join(str(c) for c in cmd)

            if "rev-parse" in joined and "--abbrev-ref" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{current_branch}\n", stderr="")

            if "checkout" in joined and "-B" in joined:
                rc = 128 if track_fails else 0
                err = f"fatal: '{target_branch}' did not match any file(s) known to git\n" if track_fails else ""
                return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=err)

            if "checkout" in joined and "-B" not in joined and "rev-parse" not in joined:
                rc = 128 if checkout_fails else 0
                err = f"error: pathspec '{target_branch}' did not match\n" if checkout_fails else ""
                return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=err)

            if "rev-list" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit_count}\n", stderr="")

            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        return side_effect

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_branch_flag_pulls_against_named_branch(self, mock_run, _mock_which, capsys):
        """--branch bb/gui makes rev-list and pull target origin/bb/gui."""
        mock_run.side_effect = self._branch_side_effect(
            current_branch="bb/gui", target_branch="bb/gui", commit_count="3"
        )
        args = SimpleNamespace(branch="bb/gui")

        cmd_update(args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]

        # rev-list must compare against origin/bb/gui, not origin/main
        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert any("origin/bb/gui" in c for c in rev_list_cmds), rev_list_cmds
        assert not any("origin/main" in c for c in rev_list_cmds), rev_list_cmds

        # pull must target bb/gui
        pull_cmds = [c for c in commands if "pull" in c and "ff-only" in c]
        assert any("bb/gui" in c and "main" not in c.split() for c in pull_cmds), pull_cmds

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_branch_flag_defaults_to_main_when_none(self, mock_run, _mock_which, capsys):
        """No --branch (or --branch=None) preserves the historical 'main' default."""
        mock_run.side_effect = self._branch_side_effect(
            current_branch="main", target_branch="main", commit_count="0"
        )
        args = SimpleNamespace(branch=None)

        cmd_update(args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert all("origin/main" in c for c in rev_list_cmds), rev_list_cmds

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_branch_flag_switches_from_different_branch(self, mock_run, _mock_which, capsys):
        """When HEAD is on main and --branch=bb/gui, switch to bb/gui first."""
        mock_run.side_effect = self._branch_side_effect(
            current_branch="main", target_branch="bb/gui", commit_count="2"
        )
        args = SimpleNamespace(branch="bb/gui")

        cmd_update(args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        # First checkout call should switch us to bb/gui (not -B; happy-path branch exists locally)
        checkout_cmds = [c for c in commands if "checkout" in c and "rev-parse" not in c]
        assert len(checkout_cmds) >= 1
        assert "bb/gui" in checkout_cmds[0]

        out = capsys.readouterr().out
        assert "switching to bb/gui" in out

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_branch_flag_tracks_remote_when_branch_absent_locally(self, mock_run, _mock_which, capsys):
        """If local lacks the branch but origin has it, fall back to ``checkout -B``."""
        mock_run.side_effect = self._branch_side_effect(
            current_branch="main",
            target_branch="bb/gui",
            checkout_fails=True,  # plain checkout fails
            track_fails=False,    # -B from origin/bb/gui succeeds
            commit_count="2",
        )
        args = SimpleNamespace(branch="bb/gui")

        cmd_update(args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        # Should have BOTH a failed `checkout bb/gui` AND a successful `checkout -B bb/gui origin/bb/gui`
        track_cmds = [c for c in commands if "checkout" in c and "-B" in c]
        assert len(track_cmds) == 1
        assert "bb/gui" in track_cmds[0]
        assert "origin/bb/gui" in track_cmds[0]

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_branch_flag_fails_when_branch_missing_everywhere(self, mock_run, _mock_which, capsys):
        """If branch doesn't exist locally OR on origin, exit non-zero with clear error."""
        mock_run.side_effect = self._branch_side_effect(
            current_branch="main",
            target_branch="nonexistent",
            checkout_fails=True,
            track_fails=True,
            commit_count="0",
        )
        args = SimpleNamespace(branch="nonexistent")

        with pytest.raises(SystemExit) as exc_info:
            cmd_update(args)
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        assert "does not exist locally or on origin" in out
        assert "nonexistent" in out


class TestCmdUpdateCheckBranchFlag:
    """``marlow update --check --branch <name>`` honors the branch override.

    The check path used to call ``git rev-list HEAD..origin/<branch> --count``
    with ``check=True``. When the branch didn't exist on origin, the fetch
    silently succeeded (no refspec) but rev-list exited 128 and a raw
    ``CalledProcessError`` propagated to the user. These tests pin the
    friendlier behavior: detect-the-missing-ref before rev-list, exit 1
    with a clear message.
    """

    def _check_side_effect(
        self,
        target_branch: str,
        *,
        verify_ok: bool = True,
        commit_count: str = "0",
    ):
        """Mock side-effect for the _cmd_update_check git pipeline.

        - ``target_branch``      what we expect compare ref to point at
        - ``verify_ok``          if False, ``git rev-parse --verify --quiet
                                 origin/<branch>`` fails (branch missing
                                 on origin)
        - ``commit_count``       rev-list count (0 = up-to-date)
        """

        def side_effect(cmd, **kwargs):
            joined = " ".join(str(c) for c in cmd)

            if "fetch" in joined and "origin" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            if "rev-parse" in joined and "--verify" in joined:
                rc = 0 if verify_ok else 1
                return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

            if "rev-list" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit_count}\n", stderr="")

            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        return side_effect

    @patch("marlow_cli.config.detect_install_method", return_value="git")
    @patch("subprocess.run")
    def test_check_branch_compares_against_named_origin_branch(
        self, mock_run, _mock_method, capsys
    ):
        """--check --branch bb/gui compares against origin/bb/gui, never origin/main."""
        mock_run.side_effect = self._check_side_effect(
            target_branch="bb/gui", verify_ok=True, commit_count="2"
        )
        args = SimpleNamespace(check=True, branch="bb/gui")

        cmd_update(args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        # Every branch checks its matching origin ref directly.
        assert not any("fetch" in c and "upstream" in c for c in commands), commands
        # Verify and rev-list both target origin/bb/gui.
        verify_cmds = [c for c in commands if "rev-parse" in c and "--verify" in c]
        assert any("origin/bb/gui" in c for c in verify_cmds), verify_cmds
        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert any("origin/bb/gui" in c for c in rev_list_cmds), rev_list_cmds
        assert not any("origin/main" in c for c in rev_list_cmds), rev_list_cmds

    @patch("marlow_cli.config.detect_install_method", return_value="git")
    @patch("subprocess.run")
    def test_check_branch_missing_on_origin_exits_cleanly(
        self, mock_run, _mock_method, capsys
    ):
        """If origin/<branch> doesn't exist, surface a friendly error and exit 1.

        Pre-fix this case raised CalledProcessError from rev-list's check=True
        and dumped a Python traceback to stdout.
        """
        mock_run.side_effect = self._check_side_effect(
            target_branch="ghost", verify_ok=False
        )
        args = SimpleNamespace(check=True, branch="ghost")

        with pytest.raises(SystemExit) as exc_info:
            cmd_update(args)
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        # No raw Python traceback.
        assert "Traceback" not in out
        assert "CalledProcessError" not in out
        # Friendly message naming the branch.
        assert "ghost" in out
        assert "not found" in out

        # rev-list must never have been called once verify failed.
        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        assert not any("rev-list" in c for c in commands), commands

    @patch("marlow_cli.config.detect_install_method", return_value="git")
    @patch("subprocess.run")
    def test_check_default_main_uses_origin(
        self, mock_run, _mock_method, capsys
    ):
        """No --branch (or --branch=None) checks origin/main directly."""
        mock_run.side_effect = self._check_side_effect(
            target_branch="main", verify_ok=True, commit_count="0"
        )
        args = SimpleNamespace(check=True, branch=None)

        cmd_update(args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        assert any("fetch" in c and "origin" in c for c in commands), commands
        assert not any("upstream" in c for c in commands), commands
        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert any("origin/main" in c for c in rev_list_cmds), rev_list_cmds

    @patch("marlow_cli.config.detect_install_method", return_value="pip")
    @patch("marlow_cli.banner.check_via_pypi", return_value=0)
    @patch("subprocess.run")
    def test_check_branch_warns_on_pypi_install(
        self, mock_run, _mock_pypi, _mock_method, capsys
    ):
        """PyPI install + --branch=<non-main> surfaces a warning instead of silent drop."""
        args = SimpleNamespace(check=True, branch="bb/gui")

        cmd_update(args)

        out = capsys.readouterr().out
        assert "--branch is ignored for PyPI installs" in out
        assert "bb/gui" in out

"""Regression tests for numbered fallbacks when the interactive curses menu
cannot initialize (e.g. non-TTY, curses unavailable, terminal error)."""

import subprocess

from marlow_cli.config import load_config, save_config


def _raise_menu(*args, **kwargs):
    # Mimic curses_radiolist hitting an unrecoverable terminal error so the
    # caller's except clause routes to the numbered-input fallback.
    raise subprocess.CalledProcessError(2, ["tput", "clear"])




def test_prompt_reasoning_effort_falls_back_on_menu_runtime_error(monkeypatch):
    from marlow_cli.main import _prompt_reasoning_effort_selection

    monkeypatch.setattr("marlow_cli.curses_ui.curses_radiolist", _raise_menu)
    responses = iter(["3"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))

    selected = _prompt_reasoning_effort_selection(["low", "medium", "high"], current_effort="")

    assert selected == "high"

"""Memory migration is an explicit operator command with a safe report."""

import json
import sys
from types import SimpleNamespace

import pytest

from marlow_cli import main


@pytest.mark.parametrize("status,exit_code", [("complete", None), ("partial", None), ("blocked", 1), ("failed", 1)])
def test_memory_migration_reports_status(monkeypatch, capsys, status, exit_code):
    report = {"status": status, "migrated": {}, "errors": []}
    calls = []

    def migrate_memory():
        calls.append(True)
        return report

    monkeypatch.setitem(sys.modules, "agent.memory_migration", SimpleNamespace(migrate_memory=migrate_memory))
    monkeypatch.setattr(sys, "argv", ["marlow", "memory", "migrate"])
    if exit_code is None:
        main.main()
    else:
        with pytest.raises(SystemExit) as raised:
            main.main()
        assert raised.value.code == exit_code
    assert calls == [True]
    assert json.loads(capsys.readouterr().out) == report


@pytest.mark.parametrize("command", ["setup", "reset", "off", "status"])
def test_retired_memory_commands_are_rejected(monkeypatch, command):
    monkeypatch.setattr(sys, "argv", ["marlow", "memory", command])
    with pytest.raises(SystemExit) as raised:
        main.main()
    assert raised.value.code == 2


def test_memory_migration_cli_uses_active_profile(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["marlow", "memory", "migrate"])
    main.main()
    report = json.loads(capsys.readouterr().out)
    assert report["profile"] == str(tmp_path)
    assert report["status"] == "complete"
    assert report["errors"] == []

from __future__ import annotations

import argparse
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.experience.models import (
    EgressPolicy,
    LOCAL_OWNER_PRINCIPAL,
    ScopePolicy,
    ScopeRef,
    ScopeType,
)
from marlow_cli import experience
from marlow_cli.config import DEFAULT_CONFIG, load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    experience.register_cli(parser)
    return parser


@contextmanager
def _store_context(store):
    yield store


def _policy() -> ScopePolicy:
    return ScopePolicy(
        principal_id=LOCAL_OWNER_PRINCIPAL,
        repository_id="repo_example",
        project_id="project_example",
        project_root_rel=".",
        capture_allowed=True,
        recall_allowed=True,
        injection_allowed=False,
        reflection_allowed=False,
        max_egress_policy=EgressPolicy.LOCAL_ONLY,
        updated_at=1.0,
    )


def _lesson(*, revision: int = 1) -> dict:
    return {
        "id": "lesson_example",
        "current_status": "candidate",
        "current_revision": revision,
        "scope_type": "project",
        "scope_id": "project_example",
        "repository_id": "repo_example",
        "project_id": "project_example",
        "sensitivity": "local_only",
        "egress_policy": "local_only",
        "revision": {
            "item_id": "lesson_example",
            "revision": revision,
            "title": "Verify the real side effect",
            "summary": "Resource creation is not enough.",
            "body": {
                "applies_when": "An asynchronous job changes external state",
                "does_not_apply_when": "The task only creates an artifact",
                "guidance": "Verify the workload and the external state.",
                "rationale": "A completed resource can hide a failed process.",
            },
            "tags": [{"namespace": "task_type", "value": "verification"}],
        },
    }


def test_default_config_is_disabled_and_non_reflective() -> None:
    defaults = DEFAULT_CONFIG["experience"]

    assert defaults["mode"] == "off"
    assert defaults["reflection_enabled"] is False
    assert defaults["gateway_capture"] is False
    assert defaults["max_injected_chars"] == 1500
    assert defaults["telegram_recall"] == {
        "enabled": False,
        "owner_user_id": "",
    }
    assert defaults["telegram_digital_twin"]["enabled"] is False
    assert "digital twin" in defaults["telegram_digital_twin"]["system_prompt"]


def test_partial_user_config_receives_new_experience_defaults(tmp_path: Path) -> None:
    from unittest.mock import patch

    (tmp_path / "config.yaml").write_text("experience:\n  mode: shadow\n", encoding="utf-8")
    with patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path)}):
        config = load_config()

    assert config["experience"]["mode"] == "shadow"
    assert config["experience"]["max_retrieved_items"] == 3
    assert config["experience"]["max_injected_chars"] == 1500
    assert config["experience"]["reflection_enabled"] is False
    assert config["experience"]["telegram_recall"] == {
        "enabled": False,
        "owner_user_id": "",
    }
    assert config["experience"]["telegram_digital_twin"]["enabled"] is False
    assert (
        "Guest status grants no additional authority"
        in config["experience"]["telegram_digital_twin"]["system_prompt"]
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("off", (False, False, False, False)),
        ("capture", (True, False, False, False)),
        ("shadow", (True, True, False, False)),
        ("assist", (True, True, True, False)),
    ],
)
def test_policy_modes_map_to_separate_capture_recall_and_injection_consents(
    mode: str,
    expected: tuple[bool, bool, bool, bool],
) -> None:
    assert experience._policy_mode_flags(mode) == expected


def test_project_denial_wins_over_global_assist(monkeypatch) -> None:
    import marlow_cli.config

    monkeypatch.setattr(
        marlow_cli.config,
        "load_config",
        lambda: {"experience": {"mode": "assist"}},
    )
    denied = _policy()
    denied = ScopePolicy(
        principal_id=denied.principal_id,
        repository_id=denied.repository_id,
        project_id=denied.project_id,
        project_root_rel=denied.project_root_rel,
        capture_allowed=False,
        recall_allowed=False,
        injection_allowed=False,
        reflection_allowed=False,
        max_egress_policy=denied.max_egress_policy,
        updated_at=denied.updated_at,
    )

    assert experience._effective_mode(denied) == (
        "off",
        "project policy does not permit experience recall",
    )


def test_capture_only_project_stays_capture_when_global_mode_is_assist(
    monkeypatch,
) -> None:
    import marlow_cli.config

    monkeypatch.setattr(
        marlow_cli.config,
        "load_config",
        lambda: {"experience": {"mode": "assist"}},
    )
    capture_only = ScopePolicy(
        principal_id=LOCAL_OWNER_PRINCIPAL,
        repository_id="repo_example",
        project_id="project_example",
        project_root_rel=".",
        capture_allowed=True,
        recall_allowed=False,
        injection_allowed=False,
        reflection_allowed=False,
        max_egress_policy=EgressPolicy.LOCAL_ONLY,
        updated_at=1.0,
    )

    assert experience._effective_mode(capture_only) == (
        "capture",
        "project policy permits capture but not recall",
    )


@pytest.mark.parametrize(
    "argv, command",
    [
        (["policy", "set", "--project-root", ".", "--mode", "shadow"], "policy"),
        (["policy", "show"], "policy"),
        (["list"], "list"),
        (["show", "lesson_1"], "show"),
        (["approve", "lesson_1"], "approve"),
        (["edit", "lesson_1", "--title", "New title"], "edit"),
        (["retract", "lesson_1", "--reason", "incorrect"], "retract"),
        (["purge", "lesson_1", "--yes"], "purge"),
        (["delete", "lesson_1", "--purge", "--yes"], "delete"),
        (["why", "--last"], "why"),
        (["why-last"], "why-last"),
        (["status"], "status"),
        (["doctor", "--repository-root", "."], "doctor"),
        (["rebuild-index"], "rebuild-index"),
        (["prune", "--apply"], "prune"),
    ],
)
def test_parser_exposes_governance_commands(argv: list[str], command: str) -> None:
    parsed = _parser().parse_args(argv)

    assert parsed.experience_command == command
    assert callable(parsed.func)


def test_policy_set_writes_database_policy_without_mutating_config(
    monkeypatch, capsys
) -> None:
    calls: list[dict] = []

    class Store:
        def upsert_scope_policy(self, **kwargs):
            calls.append(kwargs)
            return kwargs

    monkeypatch.setattr(experience, "_make_policy", lambda _args: _policy())
    monkeypatch.setattr(experience, "_open_store", lambda: _store_context(Store()))
    monkeypatch.setattr(experience, "_effective_mode", lambda _policy: ("shadow", "test"))
    monkeypatch.setattr(experience, "_global_mode", lambda: "shadow")

    result = experience._cmd_policy_set(
        argparse.Namespace(project_root=".", mode="shadow", egress="local_only")
    )

    assert result == 0
    assert calls == [
        {
            "principal_id": "local-owner",
            "repository_id": "repo_example",
            "project_id": "project_example",
            "project_root_rel": ".",
            "capture_allowed": True,
            "recall_allowed": True,
            "injection_allowed": False,
            "reflection_allowed": False,
            "max_egress_policy": "local_only",
            "updated_at": 1.0,
            "workspace_root": None,
        }
    ]
    assert "policy mode:   shadow" in capsys.readouterr().out


def test_fresh_project_enable_promotes_global_rollout_gate(monkeypatch, tmp_path) -> None:
    writes: list[tuple[Path, str, str]] = []
    monkeypatch.setattr(experience, "_global_mode", lambda: "off")
    monkeypatch.setattr("marlow_cli.config.ensure_marlow_home", lambda: None)
    monkeypatch.setattr(
        "marlow_cli.config.get_config_path",
        lambda: tmp_path / "config.yaml",
    )
    monkeypatch.setattr(
        "utils.atomic_roundtrip_yaml_update",
        lambda path, key, value: writes.append((path, key, value)),
    )

    experience._promote_global_mode("assist")

    assert writes == [
        (tmp_path / "config.yaml", "experience.mode", "assist")
    ]


def test_policy_show_and_policy_list_preserve_recall_consent(
    monkeypatch,
    capsys,
) -> None:
    policy = _policy()

    class Store:
        def list_scope_policies(self, **_kwargs):
            return [policy.to_dict()]

    monkeypatch.setattr(experience, "_open_store", lambda: _store_context(Store()))
    monkeypatch.setattr(
        experience,
        "_effective_mode",
        lambda _policy: ("shadow", "recall permitted"),
    )

    listed = experience._policies(Store())
    assert len(listed) == 1
    assert listed[0].recall_allowed is True

    assert experience._cmd_policy_show(
        argparse.Namespace(all=True, project_root=None, json=False)
    ) == 0
    output = capsys.readouterr().out
    assert "Policy mode:      shadow" in output
    assert "Recall allowed:   yes" in output


def test_list_pushes_multiple_status_filters_into_store(monkeypatch, capsys) -> None:
    calls: list[dict] = []

    class Store:
        def list_items(self, **kwargs):
            calls.append(kwargs)
            return []

    monkeypatch.setattr(experience, "_open_store", lambda: _store_context(Store()))

    assert experience._cmd_list(
        argparse.Namespace(
            all_scopes=True,
            project_root=None,
            status=["candidate", "rejected"],
            limit=7,
            json=False,
        )
    ) == 0
    assert calls == [
        {
            "principal_id": "local-owner",
            "repository_id": None,
            "project_id": None,
            "status": ["candidate", "rejected"],
            "limit": 7,
        }
    ]
    assert "No matching" in capsys.readouterr().out


def test_relative_policy_root_is_resolved_from_nested_cwd(monkeypatch, tmp_path) -> None:
    repository = tmp_path / "repo"
    project = repository / "apps" / "api"
    project.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    from agent.experience.scope import ScopeResolver

    monkeypatch.chdir(repository / "apps")
    monkeypatch.setattr(
        experience,
        "_scope_resolver",
        lambda: ScopeResolver(str(tmp_path / "profile")),
    )

    policy = experience._make_policy(
        argparse.Namespace(project_root="api", mode="assist", egress="local_only")
    )

    assert policy.project_root_rel == "apps/api"
    assert policy.capture_allowed is True
    assert policy.recall_allowed is True
    assert policy.injection_allowed is True


def test_add_always_creates_user_candidate_in_resolved_scope(monkeypatch, capsys) -> None:
    calls: list[dict] = []
    scope = ScopeRef(
        principal_id=LOCAL_OWNER_PRINCIPAL,
        scope_type=ScopeType.PROJECT,
        scope_id="project_example",
        repository_id="repo_example",
        project_id="project_example",
    )
    resolved = SimpleNamespace(as_ref=lambda: scope)

    class Store:
        def create_lesson(self, **kwargs):
            calls.append(kwargs)
            return _lesson()

    monkeypatch.setattr(experience, "_open_store", lambda: _store_context(Store()))
    monkeypatch.setattr(experience, "_resolved_scope", lambda _store, _root: resolved)
    args = argparse.Namespace(
        project_root=None,
        title="Verify the real side effect",
        summary="Resource creation is not enough.",
        applies_when="An asynchronous job changes external state",
        does_not_apply_when="The task only creates an artifact",
        guidance="Verify the workload and the external state.",
        rationale="A completed resource can hide a failed process.",
        task_type=["verification"],
        technology=["kubernetes"],
        entity=[],
        failure=[],
        confidence=0.6,
        sensitivity="local_only",
        egress="local_only",
        producer_trust_domain=None,
    )

    assert experience._cmd_add(args) == 0
    assert calls[0]["principal_id"] == LOCAL_OWNER_PRINCIPAL
    assert calls[0]["scope_type"] is ScopeType.PROJECT
    assert calls[0]["created_by"].value == "user"
    assert calls[0]["confidence"] == 0.6
    assert calls[0]["tags"] == (
        ("task_type", "verification"),
        ("technology", "kubernetes"),
    )
    assert "Candidate lesson created: lesson_example" in capsys.readouterr().out


def test_direct_edit_appends_revision_and_preserves_tags(monkeypatch) -> None:
    calls: list[dict] = []

    class Store:
        def get_item(self, item_id):
            assert item_id == "lesson_example"
            return _lesson()

        def edit_lesson(self, item_id, **kwargs):
            calls.append({"item_id": item_id, **kwargs})
            return _lesson(revision=2)

    monkeypatch.setattr(experience, "_open_store", lambda: _store_context(Store()))
    args = argparse.Namespace(
        lesson_id="lesson_example",
        reason="narrow the condition",
        title="Narrowed title",
        summary=None,
        applies_when=None,
        does_not_apply_when=None,
        guidance=None,
        rationale=None,
    )

    assert experience._cmd_edit(args) == 0
    assert calls[0]["title"] == "Narrowed title"
    assert calls[0]["edit_reason"] == "narrow the condition"
    assert calls[0]["tags"] == (("task_type", "verification"),)


def test_purge_discloses_limits_and_requires_confirmation(monkeypatch, capsys) -> None:
    opened = False

    @contextmanager
    def should_not_open():
        nonlocal opened
        opened = True
        yield None

    monkeypatch.setattr(experience, "_open_store", should_not_open)
    monkeypatch.setattr(experience, "_confirm", lambda _prompt: False)

    assert experience._cmd_purge(
        argparse.Namespace(lesson_id="lesson_example", yes=False)
    ) == 0
    output = capsys.readouterr().out
    assert "backups, WAL history, filesystem snapshots" in output
    assert "Purge cancelled" in output
    assert opened is False


def test_why_last_fails_cleanly_when_store_has_no_latest_lookup(monkeypatch, capsys) -> None:
    resolved = SimpleNamespace(
        principal_id=LOCAL_OWNER_PRINCIPAL,
        repository_id="repo_example",
        project_id="project_example",
    )
    monkeypatch.setattr(experience, "_open_store", lambda: _store_context(object()))
    monkeypatch.setattr(experience, "_resolved_scope", lambda _store, _root: resolved)

    assert experience._cmd_why_last(
        argparse.Namespace(project_root=None, json=False)
    ) == 0
    assert "not available in this Marlow build" in capsys.readouterr().out


def test_status_doctor_rebuild_and_prune_commands_emit_metadata_only(monkeypatch, capsys) -> None:
    class Store:
        def schema_status(self):
            return {"schema_version": "5", "schema_current": True}
        def diagnostic_stats(self):
            return {"retrieval_count": 1, "tag_count": 2, "link_count": 3}
        def doctor(self, repository_root=None):
            assert repository_root == "/repo"
            return {"ok": True, "foreign_key_violations": [], "orphan_current_revisions": [], "supersession_cycles": [], "active_decision_authority_violations": [], "policy_anchor_violations": [], "stale_migration_mappings": [], "schema_current": True, "fts_current": True}
        def rebuild_search_index(self):
            return {"rebuilt": True, "fts_enabled": True}
        def diagnostic_prune_plan(self, **kwargs):
            return {"retrievals_to_remove": 1, "events_to_remove": 2}

    monkeypatch.setattr(experience, "_open_store", lambda: _store_context(Store()))

    assert experience._cmd_status(argparse.Namespace(json=True)) == 0
    assert "schema_version" in capsys.readouterr().out
    assert experience._cmd_doctor(argparse.Namespace(repository_root="/repo", json=True)) == 0
    assert "ok" in capsys.readouterr().out
    assert experience._cmd_rebuild_index(argparse.Namespace(json=True)) == 0
    assert "rebuilt" in capsys.readouterr().out
    assert experience._cmd_prune(
        argparse.Namespace(apply=False, now=None, max_age_days=30, max_retrievals=10, max_events=20, json=True)
    ) == 0
    assert "retrievals_to_remove" in capsys.readouterr().out


def test_latest_retrieval_uses_public_store_capability() -> None:
    calls = []

    class Store:
        def get_latest_retrieval(self, **scope):
            calls.append(scope)
            return {"retrieval": {"id": "retrieval_example"}, "items": []}

    result = experience._get_latest_retrieval(
        Store(),
        principal_id=LOCAL_OWNER_PRINCIPAL,
        repository_id="repo_example",
        project_id="project_example",
    )

    assert result["retrieval"]["id"] == "retrieval_example"
    assert calls[0]["project_id"] == "project_example"


def test_unexpected_errors_do_not_echo_sensitive_payload(monkeypatch, capsys) -> None:
    def fail(_args):
        raise RuntimeError("database failed while writing secret-token-value")

    wrapped = experience._dispatch(fail)
    with pytest.raises(SystemExit) as caught:
        wrapped(argparse.Namespace())

    assert caught.value.code == 2
    output = capsys.readouterr().out
    assert "secret-token-value" not in output
    assert "failed safely" in output


def test_state_database_path_is_resolved_from_active_profile(monkeypatch, tmp_path: Path) -> None:
    import marlow_constants

    monkeypatch.setattr(marlow_constants, "get_marlow_home", lambda: tmp_path / "profile")

    assert experience._state_db_path() == (tmp_path / "profile" / "state.db").resolve()


def test_governance_commands_round_trip_through_profile_state_db(
    monkeypatch, tmp_path: Path
) -> None:
    import marlow_constants

    profile = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(marlow_constants, "get_marlow_home", lambda: profile)
    monkeypatch.setattr(experience, "_effective_mode", lambda _policy: ("capture", "test"))
    monkeypatch.setattr(experience, "_global_mode", lambda: "capture")

    assert experience._cmd_policy_set(
        argparse.Namespace(
            project_root=str(workspace),
            mode="capture",
            egress="local_only",
        )
    ) == 0
    add_args = argparse.Namespace(
        project_root=str(workspace),
        title="Verify the real side effect",
        summary="Resource creation is not completion.",
        applies_when="An asynchronous job changes external state",
        does_not_apply_when="The task only creates an artifact",
        guidance="Verify the workload and the external state.",
        rationale="A completed resource can hide a failed process.",
        task_type=["verification"],
        technology=[],
        entity=[],
        failure=[],
        sensitivity="local_only",
        egress="local_only",
        producer_trust_domain=None,
    )
    assert experience._cmd_add(add_args) == 0

    with experience._open_store() as store:
        lessons = store.list_items(principal_id=LOCAL_OWNER_PRINCIPAL)
    assert len(lessons) == 1
    lesson_id = lessons[0]["id"]

    assert experience._cmd_approve(
        argparse.Namespace(lesson_id=lesson_id, reason="reviewed")
    ) == 0
    assert experience._cmd_edit(
        argparse.Namespace(
            lesson_id=lesson_id,
            reason="make the check concrete",
            title=None,
            summary=None,
            applies_when=None,
            does_not_apply_when=None,
            guidance="Verify process completion and the intended external state.",
            rationale=None,
        )
    ) == 0
    with experience._open_store() as store:
        revised = store.get_item(lesson_id)
    assert revised["current_status"] == "active"
    assert revised["current_revision"] == 2

    assert experience._cmd_retract(
        argparse.Namespace(lesson_id=lesson_id, reason="superseded")
    ) == 0
    assert experience._cmd_purge(
        argparse.Namespace(lesson_id=lesson_id, yes=True)
    ) == 0
    with experience._open_store() as store:
        assert store.get_item(lesson_id) is None

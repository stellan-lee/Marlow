from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from agent.experience.authority import DecisionTurnAuthority
from agent.experience.models import EgressPolicy, ScopePolicy, ScopeRef, ScopeType
from agent.experience.store import ExperienceStore
from marlow_cli import experience


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    experience.register_cli(parser)
    return parser


def _policy(project_id: str = "project_cli") -> ScopePolicy:
    return ScopePolicy(
        principal_id="local-owner",
        repository_id="repo_cli",
        project_id=project_id,
        project_root_rel=".",
        capture_allowed=True,
        recall_allowed=True,
        injection_allowed=True,
        reflection_allowed=False,
        max_egress_policy=EgressPolicy.EXPLICIT_ANY_PROVIDER,
        updated_at=1.0,
    )


def _authority(source_hash: str, **kwargs) -> DecisionTurnAuthority:
    return DecisionTurnAuthority(
        source_turn_id="cli-turn",
        source_session_id="cli-session",
        raw_user_text_hash=source_hash,
        explicit_remember_grant=False,
        approved_item_ids=kwargs.get("approve", ()),
        supersede_target_ids=kwargs.get("supersede", ()),
        revoke_target_ids=kwargs.get("revoke", ()),
    )


def test_parser_exposes_decision_commands() -> None:
    cases = [
        (["decision", "add", "--title", "t", "--summary", "s", "--statement", "x", "--rationale", "r", "--effective-at", "1"], "decision", "add"),
        (["decision", "propose", "--statement", "x", "--rationale", "r", "--effective-at", "1"], "decision", "propose"),
        (["decision", "list"], "decision", "list"),
        (["decision", "show", "decision_1"], "decision", "show"),
        (["decision", "approve", "decision_1"], "decision", "approve"),
        (["decision", "edit", "decision_1", "--title", "t"], "decision", "edit"),
        (["decision", "supersede", "decision_1", "--statement", "x", "--rationale", "r", "--effective-at", "1"], "decision", "supersede"),
        (["decision", "revoke", "decision_1"], "decision", "revoke"),
        (["decision", "reapprove", "decision_1"], "decision", "reapprove"),
        (["decision", "related", "decision_1"], "decision", "related"),
        (["purge", "decision_1", "--yes"], "purge", None),
        (["decision", "import-policy", "--project-root", ".", "--title", "t", "--summary", "s", "--statement", "x", "--rationale", "r", "--effective-at", "1", "--policy-anchor-path", "AGENTS.md", "--policy-anchor-hash", "a" * 64], "decision", "import-policy"),
        (["migrate", "consolidation", "--dry-run", "--limit", "10"], "migrate", "consolidation"),
    ]
    for argv, expected_root, expected_command in cases:
        parsed = _parser().parse_args(argv)
        assert parsed.experience_command == expected_root
        assert callable(parsed.func)
        if expected_command is None:
            continue
        if expected_root == "decision":
            assert parsed.experience_decision_command == expected_command
        else:
            assert parsed.experience_migrate_command == expected_command


def test_decision_cli_round_trip_add_approve_supersede_related(tmp_path: Path, monkeypatch) -> None:
    profile = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("marlow_constants.get_marlow_home", lambda: profile)
    monkeypatch.setattr(experience, "_global_mode", lambda: "capture")
    monkeypatch.setattr(experience, "_effective_mode", lambda _policy: ("capture", "test"))

    policy = _policy()
    resolved = SimpleNamespace(
        as_ref=lambda: ScopeRef(
            principal_id=policy.principal_id,
            scope_type=ScopeType.PROJECT,
            scope_id=policy.project_id,
            repository_id=policy.repository_id,
            project_id=policy.project_id,
        ),
        repository_root=str(workspace),
        policy=policy,
    )
    monkeypatch.setattr(experience, "_resolved_scope", lambda _store, _root: resolved)
    with ExperienceStore(profile / "state.db") as store:
        store.upsert_scope_policy(**policy.to_dict())

    add_args = argparse.Namespace(
        project_root=str(workspace),
        title="Use Decision Memory after approval",
        summary="Decision Memory requires authority.",
        statement="Use Decision Memory after approval.",
        rationale="Authority prevents accidental persistence.",
        effective_at=1.0,
        expires_at=None,
        task_type=[],
        technology=[],
        entity=[],
        failure=[],
        sensitivity="normal",
        egress="local_only",
    )
    assert experience._cmd_decision_add(add_args) == 0

    with ExperienceStore(profile / "state.db") as store:
        decision_id = store.list_decisions(principal_id="local-owner")[0]["id"]

    assert experience._cmd_decision_list(
        argparse.Namespace(project_root=str(workspace), all_scopes=False, status=None, limit=10, json=False)
    ) == 0

    assert experience._cmd_decision_approve(
        argparse.Namespace(decision_id=decision_id, project_root=str(workspace), reason="reviewed")
    ) == 0

    assert experience._cmd_decision_supersede(
        argparse.Namespace(
            decision_id=decision_id,
            project_root=str(workspace),
            title="Use narrower Decision Memory",
            summary="Narrower policy is safer.",
            statement="Use narrower Decision Memory.",
            rationale="The old policy is too broad.",
            effective_at=2.0,
            expires_at=None,
            task_type=[],
            technology=[],
            entity=[],
            failure=[],
            reason="superseded by narrower policy",
        )
    ) == 0

    assert experience._cmd_decision_related(
        argparse.Namespace(decision_id=decision_id, project_root=str(workspace))
    ) == 0

    output = experience._cmd_decision_related(
        argparse.Namespace(decision_id=decision_id, project_root=str(workspace))
    )
    assert output == 0


def test_decision_cli_import_policy_imports_valid_anchor_as_active(tmp_path: Path, monkeypatch, capsys) -> None:
    profile = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_file = workspace / "AGENTS.md"
    policy_file.write_text("# repository policy\n", encoding="utf-8")
    monkeypatch.setattr("marlow_constants.get_marlow_home", lambda: profile)
    monkeypatch.setattr(experience, "_global_mode", lambda: "capture")
    monkeypatch.setattr(experience, "_effective_mode", lambda _policy: ("capture", "test"))

    policy = _policy()
    resolved = SimpleNamespace(
        as_ref=lambda: ScopeRef(
            principal_id=policy.principal_id,
            scope_type=ScopeType.PROJECT,
            scope_id=policy.project_id,
            repository_id=policy.repository_id,
            project_id=policy.project_id,
        ),
        repository_root=str(workspace),
        policy=policy,
    )
    monkeypatch.setattr(experience, "_resolved_scope", lambda _store, _root: resolved)
    with ExperienceStore(profile / "state.db") as store:
        store.upsert_scope_policy(**policy.to_dict())

    args = argparse.Namespace(
        project_root=str(workspace),
        title="Repository Policy Decision",
        summary="Validated repository policy.",
        statement="Every guard review must spawn a read-only subagent.",
        rationale="Repository policy requires independent review isolation.",
        effective_at=1.0,
        expires_at=None,
        policy_anchor_path="AGENTS.md",
        policy_anchor_hash=__import__("hashlib").sha256(policy_file.read_bytes()).hexdigest(),
        task_type=[],
        technology=[],
        entity=[],
        failure=[],
    )
    assert experience._cmd_decision_import_policy(args) == 0
    output = capsys.readouterr().out
    assert "Repository-policy decision imported:" in output
    with ExperienceStore(profile / "state.db") as store:
        decision = store.list_decisions(principal_id="local-owner")[0]
    assert decision["current_status"] == "active"
    assert decision["revision"]["body"]["authority"] == "repository_policy"


def test_decision_cli_show_json_and_revoke(tmp_path: Path, monkeypatch, capsys) -> None:
    profile = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("marlow_constants.get_marlow_home", lambda: profile)
    monkeypatch.setattr(experience, "_global_mode", lambda: "capture")
    monkeypatch.setattr(experience, "_effective_mode", lambda _policy: ("capture", "test"))

    policy = _policy()
    resolved = SimpleNamespace(
        as_ref=lambda: ScopeRef(
            principal_id=policy.principal_id,
            scope_type=ScopeType.PROJECT,
            scope_id=policy.project_id,
            repository_id=policy.repository_id,
            project_id=policy.project_id,
        ),
        repository_root=str(workspace),
        policy=policy,
    )
    monkeypatch.setattr(experience, "_resolved_scope", lambda _store, _root: resolved)
    with ExperienceStore(profile / "state.db") as store:
        store.upsert_scope_policy(**policy.to_dict())
        item = store.create_decision(
            principal_id="local-owner",
            scope_type="project",
            scope_id=policy.project_id,
            repository_id=policy.repository_id,
            project_id=policy.project_id,
            title="Revocable Decision",
            summary="Can be revoked.",
            body={
                "statement": "Do not revoke without authority.",
                "rationale": "Revocation is terminal.",
                "source_type": "agent_proposal",
                "authority": "unapproved",
                "effective_at": 1.0,
            },
            created_by="agent",
        )
        store.activate_decision(
            item["id"],
            authority=_authority("a" * 64, approve=(item["id"],)),
            repository_root=str(workspace),
            repository_id=policy.repository_id,
        )

    assert experience._cmd_decision_show(
        argparse.Namespace(decision_id=item["id"], project_root=str(workspace), json=True)
    ) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == item["id"]
    assert shown["current_status"] == "active"

    assert experience._cmd_decision_revoke(
        argparse.Namespace(decision_id=item["id"], project_root=str(workspace), reason="revoked")
    ) == 0
    with ExperienceStore(profile / "state.db") as store:
        revoked = store.get_decision(item["id"], include_history=True)
    assert revoked["current_status"] == "revoked"


def test_decision_cli_show_includes_relationship_links(tmp_path: Path, monkeypatch, capsys) -> None:
    profile = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("marlow_constants.get_marlow_home", lambda: profile)
    monkeypatch.setattr(experience, "_global_mode", lambda: "capture")
    monkeypatch.setattr(experience, "_effective_mode", lambda _policy: ("capture", "test"))

    policy = _policy()
    resolved = SimpleNamespace(
        as_ref=lambda: ScopeRef(
            principal_id=policy.principal_id,
            scope_type=ScopeType.PROJECT,
            scope_id=policy.project_id,
            repository_id=policy.repository_id,
            project_id=policy.project_id,
        ),
        repository_root=str(workspace),
        policy=policy,
    )
    monkeypatch.setattr(experience, "_resolved_scope", lambda _store, _root: resolved)
    with ExperienceStore(profile / "state.db") as store:
        store.upsert_scope_policy(**policy.to_dict())
        old = store.create_decision(
            principal_id="local-owner",
            scope_type="project",
            scope_id=policy.project_id,
            repository_id=policy.repository_id,
            project_id=policy.project_id,
            title="Old Decision",
            summary="Superseded.",
            body={
                "statement": "Use the old approach.",
                "rationale": "Historical decision.",
                "source_type": "agent_proposal",
                "authority": "unapproved",
                "effective_at": 1.0,
            },
            created_by="agent",
        )
        store.activate_decision(
            old["id"],
            authority=_authority("a" * 64, approve=(old["id"],)),
            repository_root=str(workspace),
            repository_id=policy.repository_id,
        )
        replacement = store.supersede_decision(
            old["id"],
            authority=_authority("a" * 64, supersede=(old["id"],)),
            principal_id="local-owner",
            scope_type="project",
            scope_id=policy.project_id,
            repository_id=policy.repository_id,
            project_id=policy.project_id,
            title="Replacement Decision",
            summary="Replaces the old decision.",
            body={
                "statement": "Use the replacement approach.",
                "rationale": "The old approach is too broad.",
                "source_type": "agent_proposal",
                "authority": "unapproved",
                "effective_at": 2.0,
            },
            repository_root=str(workspace),
        )

    assert experience._cmd_decision_show(
        argparse.Namespace(decision_id=replacement["id"], project_root=str(workspace), json=False)
    ) == 0
    output = capsys.readouterr().out
    assert "Relationships:" in output
    assert "supersedes" in output
    assert old["id"] in output

    assert experience._cmd_decision_show(
        argparse.Namespace(decision_id=replacement["id"], project_root=str(workspace), json=True)
    ) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["links"][0]["relation"] == "supersedes"


def test_why_last_includes_item_kind_status_title_and_causality_warning(tmp_path: Path, monkeypatch, capsys) -> None:
    profile = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("marlow_constants.get_marlow_home", lambda: profile)
    policy = _policy()
    resolved = SimpleNamespace(
        principal_id=policy.principal_id,
        repository_id=policy.repository_id,
        project_id=policy.project_id,
    )
    monkeypatch.setattr(experience, "_resolved_scope", lambda _store, _root: resolved)

    with ExperienceStore(profile / "state.db") as store:
        store.upsert_scope_policy(**policy.to_dict())
        lesson = store.create_lesson(
            item_id="lesson_why",
            principal_id="local-owner",
            scope_type="project",
            scope_id=policy.project_id,
            repository_id=policy.repository_id,
            project_id=policy.project_id,
            title="Diagnostic title",
            summary="diagnostic",
            body={
                "applies_when": "why last diagnostics run",
                "guidance": "Show item metadata.",
                "rationale": "Explainability needs diagnostics.",
            },
            tags={"task_type": ["diagnostics"]},
            created_at=1.0,
        )
        lesson_item = store.get_item(lesson["id"])
        assert lesson_item is not None
        lesson_revision = lesson_item["revision"]["revision"]
        retrieval = store.record_retrieval(
            retrieval_id="retrieval_why",
            idempotency_key="retrieval:why",
            turn_id="turn-why",
            work_id="work-why",
            principal_id="local-owner",
            repository_id=policy.repository_id,
            project_id=policy.project_id,
            task_signature_hash="f" * 64,
            provider_trust_domain="provider:a",
            items=[
                {
                    "item_id": lesson["id"],
                    "item_revision": lesson_revision,
                    "rank": 1,
                    "score": 1.0,
                    "match_reasons": ("diagnostic match",),
                }
            ],
            created_at=2.0,
        )

    assert experience._cmd_why_last(
        argparse.Namespace(project_root=str(workspace), json=False)
    ) == 0
    text_output = capsys.readouterr().out
    assert "kind=lesson" in text_output
    assert "status=candidate" in text_output
    assert "title=Diagnostic title" in text_output
    assert "not proof" in text_output

    assert experience._cmd_why_last(
        argparse.Namespace(project_root=str(workspace), json=True)
    ) == 0
    json_output = json.loads(capsys.readouterr().out)
    assert json_output["id"] == retrieval["id"]
    assert json_output["items"][0]["kind"] == "lesson"
    assert json_output["items"][0]["status"] == "candidate"
    assert json_output["items"][0]["title"] == "Diagnostic title"

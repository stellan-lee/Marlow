from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.experience.models import DecisionBody, DecisionSourceType, EgressPolicy, LessonBody
from agent.experience.runtime import (
    ExperienceMode,
    TurnOrigin,
    prepare_experience_turn,
)
from agent.experience.scope import ScopeResolver
from agent.experience.store import ExperienceStore


def _seed_active_local_lesson(home: Path, repository: Path) -> str:
    resolver = ScopeResolver(str(home))
    policy = resolver.make_git_policy(
        repository,
        repository,
        capture_allowed=True,
        recall_allowed=True,
        injection_allowed=True,
        max_egress_policy=EgressPolicy.LOCAL_ONLY,
    )
    with ExperienceStore(home / "state.db") as store:
        store.upsert_scope_policy(
            principal_id=policy.principal_id,
            repository_id=policy.repository_id,
            project_id=policy.project_id,
            project_root_rel=policy.project_root_rel,
            capture_allowed=policy.capture_allowed,
            recall_allowed=policy.recall_allowed,
            injection_allowed=policy.injection_allowed,
            reflection_allowed=policy.reflection_allowed,
            max_egress_policy=policy.max_egress_policy,
        )
        scope = policy.project_id
        lesson = store.create_lesson(
            principal_id=policy.principal_id,
            scope_type="project",
            scope_id=scope,
            repository_id=policy.repository_id,
            project_id=policy.project_id,
            title="Verify asynchronous side effects",
            summary="A completed workload may not prove its external effect.",
            body=LessonBody(
                applies_when="cronjob external side effect",
                does_not_apply_when="artifact generation only",
                guidance="Verify both workload completion and the external state change.",
                rationale="The earlier job completed without rotating the credential.",
            ),
            confidence=0.9,
            sensitivity="local_only",
            egress_policy="local_only",
        )
        store.approve_lesson(lesson["id"], reason="fixture approval")
        return lesson["id"]


def _active_decision_authority(source_hash: str) -> object:
    from agent.experience.authority import DecisionTurnAuthority

    return DecisionTurnAuthority(
        source_turn_id="turn-runtime-decision",
        source_session_id="session-runtime",
        raw_user_text_hash=source_hash,
        explicit_remember_grant=True,
    )


@pytest.mark.parametrize(
    ("turn_origin", "agent_identity"),
    [
        (TurnOrigin.CLASSIC_CLI, {}),
        (
            TurnOrigin.TELEGRAM,
            {
                "platform": "telegram",
                "_chat_type": "dm",
                "_user_id": "12345",
                "_user_id_alt": None,
            },
        ),
        (
            TurnOrigin.TELEGRAM_GUEST,
            {
                "platform": "telegram",
                "_chat_type": "group",
                "_chat_id": "guest-chat",
                "_thread_id": None,
                "_telegram_digital_twin_guest": True,
            },
        ),
    ],
)
def test_prepare_retrieves_once_and_builds_local_assist_context(
    tmp_path: Path,
    monkeypatch,
    turn_origin: TurnOrigin,
    agent_identity: dict[str, object],
) -> None:
    home = tmp_path / "marlow-home"
    repository = tmp_path / "repository"
    home.mkdir()
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    lesson_id = _seed_active_local_lesson(home, repository)

    monkeypatch.setenv("MARLOW_HOME", str(home))
    monkeypatch.setattr(
        "marlow_cli.config.load_config",
        lambda: {
            "experience": {
                "mode": "assist",
                "max_retrieved_items": 3,
                "max_injected_chars": 1_500,
                "min_retrieval_confidence": 0.0,
                "telegram_recall": {
                    "enabled": True,
                    "owner_user_id": "12345",
                },
                "telegram_digital_twin": {
                    "enabled": True,
                },
            },
            "approvals": {
                "admin": {
                    "enabled": True,
                    "conversation_mode": "super_admin",
                    "platform": "telegram",
                    "user_id": "12345",
                    "chat_id": "admin-chat",
                    "thread_id": None,
                }
            },
        },
    )
    monkeypatch.setattr("agent.runtime_cwd.resolve_agent_cwd", lambda: repository)
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
        **agent_identity,
    )

    turn = prepare_experience_turn(
        agent,
        raw_user_message=(
            "Please troubleshoot the CronJob because its external update is missing"
        ),
        turn_origin=turn_origin,
    )

    assert turn is not None
    assert turn.mode is ExperienceMode.ASSIST
    context = turn.context_for_request(
        provider=agent.provider,
        base_url=agent.base_url,
    )
    assert lesson_id[:24] in context
    assert "Verify both workload completion" in context
    assert len(context) <= 1_500

    with ExperienceStore(home / "state.db") as store:
        diagnostic = store.get_retrieval(turn.result.diagnostic.id)
    assert diagnostic is not None
    assert [item["item_id"] for item in diagnostic["items"]] == [lesson_id]

    unrelated = prepare_experience_turn(
        agent,
        raw_user_message="Draft a README for the frontend color palette",
        turn_origin=turn_origin,
    )
    assert unrelated is not None
    assert unrelated.result.decisions == ()
    assert unrelated.result.lessons == ()
    assert unrelated.context_for_request(
        provider=agent.provider,
        base_url=agent.base_url,
    ) == ""


@pytest.mark.parametrize("global_mode", ["shadow", "assist"])
def test_global_recall_mode_cannot_retrieve_from_capture_only_peer_project(
    tmp_path: Path,
    monkeypatch,
    global_mode: str,
) -> None:
    home = tmp_path / "marlow-home"
    repository = tmp_path / "repository"
    capture_project = repository / "apps" / "capture-only"
    assist_project = repository / "apps" / "assist"
    home.mkdir()
    capture_project.mkdir(parents=True)
    assist_project.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )

    resolver = ScopeResolver(str(home))
    capture_policy = resolver.make_git_policy(
        repository,
        capture_project,
        capture_allowed=True,
        recall_allowed=False,
        injection_allowed=False,
        max_egress_policy=EgressPolicy.LOCAL_ONLY,
    )
    assist_policy = resolver.make_git_policy(
        repository,
        assist_project,
        capture_allowed=True,
        recall_allowed=True,
        injection_allowed=True,
        max_egress_policy=EgressPolicy.LOCAL_ONLY,
    )
    with ExperienceStore(home / "state.db") as store:
        store.upsert_scope_policy(**capture_policy.to_dict())
        store.upsert_scope_policy(**assist_policy.to_dict())
        lesson = store.create_lesson(
            principal_id=capture_policy.principal_id,
            scope_type="project",
            scope_id=capture_policy.project_id,
            repository_id=capture_policy.repository_id,
            project_id=capture_policy.project_id,
            title="Capture-only project lesson",
            summary="This lesson must not be recalled without project consent.",
            body=LessonBody(
                applies_when="the capture-only project is active",
                guidance="Never return this text without recall consent.",
                rationale="Global rollout gates cannot grant another project access.",
            ),
            sensitivity="local_only",
            egress_policy="local_only",
        )
        store.approve_lesson(lesson["id"], reason="fixture approval")

    monkeypatch.setenv("MARLOW_HOME", str(home))
    monkeypatch.setattr(
        "marlow_cli.config.load_config",
        lambda: {
            "experience": {
                "mode": global_mode,
                "max_retrieved_items": 3,
                "max_injected_chars": 1_500,
                "min_retrieval_confidence": 0.0,
            }
        },
    )
    monkeypatch.setattr(
        "agent.runtime_cwd.resolve_agent_cwd",
        lambda: capture_project,
    )
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
    )

    turn = prepare_experience_turn(
        agent,
        raw_user_message="Use the capture-only project lesson",
        turn_origin=TurnOrigin.CLASSIC_CLI,
    )

    assert turn is None
    with ExperienceStore(home / "state.db") as store:
        assert store.diagnostic_stats()["retrieval_count"] == 0
        assert store.get_scope_policy(
            principal_id=assist_policy.principal_id,
            repository_id=assist_policy.repository_id,
            project_id=assist_policy.project_id,
        )["recall_allowed"] is True


def test_prepare_retrieves_active_decision_context(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "marlow-home"
    repository = tmp_path / "repository"
    home.mkdir()
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    resolver = ScopeResolver(str(home))
    policy = resolver.make_git_policy(
        repository,
        repository,
        capture_allowed=True,
        recall_allowed=True,
        injection_allowed=True,
        max_egress_policy=EgressPolicy.LOCAL_ONLY,
    )
    with ExperienceStore(home / "state.db") as store:
        store.upsert_scope_policy(
            principal_id=policy.principal_id,
            repository_id=policy.repository_id,
            project_id=policy.project_id,
            project_root_rel=policy.project_root_rel,
            capture_allowed=policy.capture_allowed,
            recall_allowed=policy.recall_allowed,
            injection_allowed=policy.injection_allowed,
            reflection_allowed=policy.reflection_allowed,
            max_egress_policy=policy.max_egress_policy,
        )
        decision = store.create_decision(
            principal_id=policy.principal_id,
            scope_type="project",
            scope_id=policy.project_id,
            repository_id=policy.repository_id,
            project_id=policy.project_id,
            title="Compute reporting state at response time",
            summary="Do not persist reporting_state.",
            body=DecisionBody(
                statement="reporting_state is computed at response time and is not persisted.",
                rationale="The current repository policy treats it as derived state.",
                source_type=DecisionSourceType.AGENT_PROPOSAL,
                authority="unapproved",
                effective_at=1.0,
            ),
            sensitivity="local_only",
            egress_policy="local_only",
            tags={"entity": ["reporting_state"]},
            created_by="agent",
            source_hash="b" * 64,
        )
        store.activate_decision(
            decision["id"],
            authority=_active_decision_authority("b" * 64),
            transitioned_at=3.0,
        )

    monkeypatch.setenv("MARLOW_HOME", str(home))
    monkeypatch.setattr(
        "marlow_cli.config.load_config",
        lambda: {
            "experience": {
                "mode": "assist",
                "max_retrieved_items": 3,
                "max_injected_chars": 1_500,
                "min_retrieval_confidence": 0.0,
            }
        },
    )
    monkeypatch.setattr("agent.runtime_cwd.resolve_agent_cwd", lambda: repository)
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
    )

    turn = prepare_experience_turn(
        agent,
        raw_user_message="Should reporting_state be persisted?",
        turn_origin=TurnOrigin.CLASSIC_CLI,
    )

    assert turn is not None
    context = turn.context_for_request(
        provider=agent.provider,
        base_url=agent.base_url,
    )
    assert "<active-decision-context>" in context
    assert "reporting_state is computed" in context
    assert "<work-experience-context>" not in context

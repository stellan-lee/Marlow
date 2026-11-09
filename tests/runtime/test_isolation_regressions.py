"""Regression cases from the independent Phase 2 boundary review."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.runtime.test_codex_runtime import profile, new_agent, events


def test_stateless_repeated_inputs_keep_identity_without_reusing_thread(profile):
    agent = new_agent(session_id="stateless", conversation_persistence_policy="none")
    first = agent.run_conversation("first input")
    second = agent.run_conversation("second input")
    assert first["completed"] and second["completed"], (first, second)
    assert first["codex_thread_id"] != second["codex_thread_id"]
    assert agent._session_db is None
    agent.close()


def test_shared_thread_preserves_conversation_and_current_actor(profile):
    agents = []
    try:
        for actor in ("alice", "bob"):
            agent = new_agent(
                session_id="shared",
                platform="slack",
                user_id=actor,
                chat_id="channel",
                thread_id="thread",
                conversation_shared=True,
            )
            agents.append(agent)
            assert agent.run_conversation("input from " + actor)["completed"]
        assert agents[0].state.mapping()[0] == agents[1].state.mapping()[0]
        assert agents[0].environment.workspace == agents[1].environment.workspace
        assert agents[1]._user_id == "bob"
    finally:
        for agent in agents:
            agent.close()


def test_legacy_resume_imports_dialogue_without_granting_old_cwd(profile, tmp_path):
    from marlow_state import SessionDB

    old = tmp_path / "unrelated"
    old.mkdir()
    db = SessionDB(profile / "state.db")
    db.create_session("legacy", "cli", cwd=str(old))
    db.append_message("legacy", "user", "LEGACY_DIALOGUE_CONTROL")
    db.close()
    agent = new_agent(session_id="legacy")
    result = agent.run_conversation("explicitly resume")
    assert result["completed"], result
    dispatched = next(
        event for event in events(profile) if event["method"] == "turn/start"
    )
    assert "Legacy Marlow dialogue" in json.dumps(dispatched["params"])
    assert "LEGACY_DIALOGUE_CONTROL" in json.dumps(dispatched["params"])
    from agent.codex_execution import allowed_path

    with pytest.raises(PermissionError):
        allowed_path(agent, str(old / "private"))
    agent.close()


def test_profile_project_allowlist_requires_task_selection(profile, tmp_path):
    from run_agent import AIAgent
    from agent.codex_execution import allowed_path
    import yaml

    roots = [tmp_path / "project-A", tmp_path / "project-B"]
    for root in roots:
        root.mkdir()
    config = yaml.safe_load((profile / "config.yaml").read_text())
    config["runtime"]["codex"]["projects"] = [
        {"path": str(root), "writable": True} for root in roots
    ]
    (profile / "config.yaml").write_text(yaml.safe_dump(config))
    agent = new_agent()
    assert agent.environment.projects == ()
    agent.close()
    agent = AIAgent(project_path=str(roots[0]), enabled_toolsets=[], skip_memory=True)
    assert allowed_path(agent, str(roots[0] / "ok"))
    with pytest.raises(PermissionError):
        allowed_path(agent, str(roots[1] / "private"))
    agent.close()


def test_host_media_cannot_disclose_auth_other_conversation_or_cache(profile):
    from agent.codex_file_scope import file_scope, register_generated
    from gateway.platforms.base import validate_media_delivery_path

    alice = new_agent(platform="telegram", user_id="alice", chat_id="A")
    bob = new_agent(platform="telegram", user_id="bob", chat_id="B")
    auth = profile / "runtime/codex/auth.json"
    auth.write_text("non-secret regression fixture")
    own, other = (
        alice.environment.workspace / "own.txt",
        bob.environment.workspace / "private.txt",
    )
    own.write_text("own")
    other.write_text("other")
    cached = profile / "cache/images/other.png"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"fake image")
    assert validate_media_delivery_path(str(auth)) is None
    assert validate_media_delivery_path(str(other)) is None
    with alice._profile_context():
        assert validate_media_delivery_path(str(own)) == str(own)
        assert validate_media_delivery_path(str(other)) is None
        assert validate_media_delivery_path(str(cached)) is None
        register_generated(cached)
        assert validate_media_delivery_path(str(cached)) == str(cached)
    with bob._profile_context():
        assert validate_media_delivery_path(str(cached)) is None
    alice.close()
    bob.close()


def test_host_browser_cannot_open_local_files_or_evaluate_privileged_js(profile):
    agent = new_agent()
    for name, args in [
        (
            "browser_navigate",
            {"url": "file://" + str(profile / "runtime/codex/auth.json")},
        ),
        ("browser_navigate", {"url": "javascript:alert(1)"}),
        ("browser_console", {"expression": 'location.href="file:///private"'}),
    ]:
        with pytest.raises(PermissionError):
            agent._dispatch_business_tool(name, args)
    agent.close()


def test_cron_owner_scope_and_profile_storage_do_not_expand(
    profile, tmp_path, monkeypatch
):
    from agent.codex_cron_scope import dispatch_cron
    from cron.jobs import list_jobs, get_job
    from model_tools import handle_function_call
    from tools.cronjob_tools import cronjob

    monkeypatch.setattr(
        "model_tools.handle_function_call", lambda name, args, **kwargs: cronjob(**args)
    )
    alice = new_agent(platform="telegram", user_id="alice", chat_id="A")
    bob = new_agent(platform="telegram", user_id="bob", chat_id="B")
    created = json.loads(
        dispatch_cron(
            alice,
            {
                "action": "create",
                "prompt": "A scoped report",
                "schedule": "every 1h",
                "deliver": "local",
            },
        )
    )
    assert created["success"], created
    jobs = list_jobs(True)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["execution_scope"]["workspace"] == str(alice.environment.workspace)
    assert str(bob.environment.workspace) not in json.dumps(job["execution_scope"])
    visible = json.loads(dispatch_cron(bob, {"action": "list"}))
    assert job["id"] not in json.dumps(visible)
    for args in (
        {
            "action": "create",
            "prompt": "x",
            "schedule": "every 1h",
            "script": "/tmp/code.py",
        },
        {"action": "create", "prompt": "x", "schedule": "every 1h", "profile": "admin"},
        {
            "action": "create",
            "prompt": "x",
            "schedule": "every 1h",
            "workdir": str(bob.environment.workspace),
        },
    ):
        with pytest.raises(PermissionError):
            dispatch_cron(alice, args)
    other = tmp_path / "profile-other"
    from marlow_constants import set_marlow_home_override, reset_marlow_home_override

    token = set_marlow_home_override(other)
    try:
        assert list_jobs(True) == []
    finally:
        reset_marlow_home_override(token)
    alice.close()
    bob.close()


def test_sampling_rejects_idle_requests_and_cancels_timed_out_worker(
    profile, monkeypatch
):
    from tools.mcp_tool import SamplingHandler
    from agent.codex_tasks import TaskCancellation

    params = SimpleNamespace(
        maxTokens=32,
        messages=[SimpleNamespace(role="user", content=SimpleNamespace(text="hello"))],
        tools=None,
        modelPreferences=None,
        systemPrompt=None,
        temperature=None,
    )
    handler = SamplingHandler("explicit-server", {})
    called = []

    def call(**kwargs):
        called.append(kwargs["cancellation"])
        kwargs["cancellation"].event.wait(1)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="late"), finish_reason="stop"
                )
            ],
            model="test",
            usage=None,
        )

    monkeypatch.setattr("agent.codex_tasks.call_llm", call)
    result = asyncio.run(handler(None, params))
    assert "active host-authorized" in result.message and not called
    parent = new_agent()
    handler._authorization = parent
    parent._sampling_remaining = 1
    handler.timeout = 0.05
    result = asyncio.run(handler(None, params))
    assert "timed out" in result.message
    assert len(called) == 1 and called[0].event.is_set()
    result = asyncio.run(handler(None, params))
    assert "budget" in result.message
    parent.close()


def test_media_upload_freezes_bytes_outside_model_write_grants(profile):
    from gateway.platforms.base import BasePlatformAdapter

    agent = new_agent(platform="slack", user_id="owner", chat_id="room")
    source = agent.environment.workspace / "report.txt"
    source.write_text("approved bytes")
    with agent._profile_context():
        frozen = BasePlatformAdapter.filter_local_delivery_paths([str(source)])[0]
        source.write_text("changed later")
        assert Path(frozen).read_text() == "approved bytes"
        assert Path(frozen).is_relative_to(profile / ".delivery-outbox")
        from agent.codex_execution import allowed_path

        with pytest.raises(PermissionError):
            allowed_path(agent, frozen)
    agent.close()


def test_manual_compaction_is_audited_without_rewriting_raw_history(profile):
    agent = new_agent(session_id="compact-audit")
    assert agent.run_conversation("canonical raw message")["completed"]
    assert agent.compact()
    assert "canonical raw message" in json.dumps(
        agent._session_db.get_messages(agent.session_id)
    )
    with agent.state.connect() as db:
        rows = db.execute(
            "SELECT trigger,status,turn_id FROM runs ORDER BY id"
        ).fetchall()
    assert len(rows) == 2
    assert rows[1][0] == "operator_compaction:input"
    assert rows[1][1] == "completed" and rows[1][2]
    agent.close()


def test_task_lease_is_shared_across_sessions_and_pending_task_never_replays(profile):
    import threading
    from agent.codex_state import RuntimeState

    first = RuntimeState(profile, {"session": "first", "task": "job"})
    second = RuntimeState(profile, {"session": "second", "task": "job"})
    errors = []

    def contend():
        try:
            with second.lease(timeout=0.05):
                errors.append("incorrectly acquired")
        except TimeoutError:
            errors.append("busy")

    with first.lease():
        first.thread("thread")
        first.start("job:due")
        worker = threading.Thread(target=contend)
        worker.start()
        worker.join(1)
    assert errors == ["busy"]
    with second.lease(), pytest.raises(RuntimeError, match="verification"):
        second.claim_scope()


def test_delegated_task_inherits_private_workspace_without_parent_task_lease(profile):
    from tools.delegate_tool import _build_child_agent

    parent = new_agent(platform="cron", runtime_task_id="scheduled-task")
    child = _build_child_agent(
        0,
        goal="A bounded delegated task",
        context=None,
        toolsets=[],
        model=None,
        max_iterations=2,
        task_count=1,
        parent_agent=parent,
    )
    try:
        assert parent.environment.workspace == child.environment.workspace
        assert child.state.lease_key != parent.state.lease_key
        other = profile / "workspace/conversations/other/private"
        from agent.codex_execution import allowed_path

        with pytest.raises(PermissionError):
            allowed_path(child, str(other))
    finally:
        child.close()
        parent.close()


def test_skill_creation_uses_current_profile_after_another_profile_import(
    profile, tmp_path
):
    from tools.skill_manager_tool import _resolve_skill_dir
    from marlow_constants import set_marlow_home_override, reset_marlow_home_override

    first = _resolve_skill_dir("scope-fixture")
    other = tmp_path / "second"
    token = set_marlow_home_override(other)
    try:
        second = _resolve_skill_dir("scope-fixture")
        assert second.is_relative_to(other) and second != first
    finally:
        reset_marlow_home_override(token)

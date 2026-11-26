"""Tests for metadata-only usage-event classification."""

from types import SimpleNamespace

from agent.usage_events import record_tool_usage_event
from marlow_state import SessionDB


def _agent(db, *, platform="cli"):
    return SimpleNamespace(
        _session_db=db,
        session_id="s1",
        platform=platform,
    )


def test_classifies_skills_and_mcp_without_payloads(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s1", source="cli")
    agent = _agent(db)

    from tools.registry import registry

    original = registry.get_toolset_for_tool
    monkeypatch.setattr(
        registry,
        "get_toolset_for_tool",
        lambda name: "mcp-github" if name == "mcp_github_search" else original(name),
    )

    calls = [
        ("skill_view", {"name": "debugging"}, "skill-call"),
        ("mcp_github_search", {"query": "secret"}, "mcp-call"),
    ]
    for name, args, call_id in calls:
        record_tool_usage_event(
            agent,
            tool_name=name,
            args=args,
            tool_call_id=call_id,
            failed=False,
            duration_seconds=0.125,
        )

    rows = db.list_usage_events()
    assert [
        (row["subsystem"], row["action"], row["item_name"], row["parent_name"])
        for row in rows
    ] == [
        ("skill", "load", "debugging", None),
        ("mcp", "call", "mcp_github_search", "github"),
    ]
    assert all(row["metadata_json"] is None for row in rows)
    assert all(row["duration_ms"] == 125 for row in rows)
    db.close()


def test_curator_tool_calls_are_attributed_separately(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s1", source="curator")
    agent = _agent(db, platform="curator")

    record_tool_usage_event(
        agent,
        tool_name="skill_view",
        args={"name": "codebase-inspection"},
        tool_call_id="call-1",
        failed=False,
        duration_seconds=0,
    )

    assert db.list_usage_events()[0]["source"] == "curator"
    db.close()

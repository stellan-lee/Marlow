"""Session reset preserves old data while creating a distinct Codex mapping."""
from tests.runtime.test_codex_runtime import profile, new_agent


def test_reset_clears_turn_usage_and_memory_context(profile):
    agent = new_agent()
    first = agent.run_conversation('first')
    old_session = agent.session_id
    agent.reset_session_state()
    assert agent.session_id != old_session
    assert agent._user_turn_count == 0
    assert agent.session_total_tokens == 0
    assert agent._session_messages == []
    assert agent._session_db.get_messages(old_session)
    second = agent.run_conversation('second')
    assert second['codex_thread_id'] != first['codex_thread_id']
    agent.close()

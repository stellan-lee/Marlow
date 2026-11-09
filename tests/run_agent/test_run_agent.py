"""Retained SDK contracts on the sole subprocess-backed Codex runtime.

Native client/request/retry-loop tests have retired with their implementations.
The protocol fixture performs no network or business operations.
"""
import json
from unittest.mock import MagicMock
import pytest

from tests.runtime.test_codex_runtime import profile, new_agent


@pytest.mark.parametrize('platform', ['cli','telegram','cron','sdk'])
def test_sdk_platforms_use_the_same_adapter_and_persist_usage(profile, platform):
    agent = new_agent(platform=platform)
    result = agent.run_conversation('explicit SDK input')
    assert result['completed'], result
    assert result['codex_thread_id'] and result['codex_turn_id']
    row = agent._session_db.get_session(agent.session_id)
    assert row['input_tokens'] == 7
    assert row['output_tokens'] == 3
    agent.close()


def test_chat_returns_text_and_raises_explicit_failure(profile):
    agent = new_agent()
    assert agent.chat('first') == 'fixture reply'
    agent.run_conversation = lambda *args, **kwargs: {'error':'authentication required','final_response':''}
    with pytest.raises(RuntimeError, match='authentication required'):
        agent.chat('next')
    agent.close()


def test_interrupt_propagates_only_to_owned_children(profile):
    parent, child, independent = new_agent(), new_agent(), new_agent()
    parent._active_children.append(child)
    parent.interrupt('cancel this task')
    assert parent._interrupt_requested and child._interrupt_requested
    assert not independent._interrupt_requested
    parent.clear_interrupt()
    assert not parent._interrupt_requested
    for agent in (parent,child,independent):
        agent.close()


def test_todo_history_hydrates_without_replaying_writes(profile):
    agent = new_agent()
    agent._hydrate_todo_store([{'role':'tool','content':json.dumps({'todos':[{
        'id':'pending','content':'Verify work','status':'in_progress'}]})}])
    assert agent._todo_store.read()[0]['id'] == 'pending'
    assert agent._codex_session is None
    agent.close()


def test_release_then_resume_preserves_profile_session(profile):
    agent = new_agent(session_id='durable')
    first = agent.run_conversation('first')
    agent.release_clients()
    assert agent._codex_session is None and not agent._closed
    next_turn = agent.run_conversation('next')
    assert next_turn['codex_thread_id'] == first['codex_thread_id']
    agent.close()

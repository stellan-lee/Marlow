"""Resource cleanup closes agents after retirement of provider lifecycle hooks."""

from unittest.mock import Mock

from gateway.run import GatewayRunner


def test_cleanup_closes_agent_without_running_retired_memory_hooks():
    runner = object.__new__(GatewayRunner)
    agent = Mock()
    runner._cleanup_agent_resources(agent)
    agent.close.assert_called_once_with()
    agent.shutdown_memory_provider.assert_not_called()


def test_cleanup_accepts_missing_agent():
    runner = object.__new__(GatewayRunner)
    runner._cleanup_agent_resources(None)

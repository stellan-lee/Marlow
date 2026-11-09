"""Session diagnostics report canonical native configuration."""
from unittest.mock import patch
from types import SimpleNamespace
from gateway.run import GatewayRunner


def test_session_info_reads_codex_model_without_probing_provider():
    runner = GatewayRunner.__new__(GatewayRunner)
    environment = SimpleNamespace(config={'model':'configured-native-model','model_provider':'configured-native-provider'})
    with patch('agent.codex_environment.managed_environment', return_value=environment):
        text = runner._format_session_info()
    assert 'configured-native-model' in text and 'configured-native-provider' in text
    assert 'managed by Codex' in text

"""Status reads native configuration without a second YAML provider map."""
from types import SimpleNamespace
from unittest.mock import patch
from marlow_cli.status import _configured_model


def test_model_status_uses_managed_native_configuration():
    with patch('agent.codex_environment.managed_environment', return_value=SimpleNamespace(config={'model':'native','model_provider':'fixture'})):
        assert _configured_model({'model':{'default':'legacy'}}) == ('native','fixture','')

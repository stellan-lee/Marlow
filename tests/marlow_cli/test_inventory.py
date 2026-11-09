"""Picker state uses only the managed profile's Codex model configuration."""
from types import SimpleNamespace
from unittest.mock import patch
from marlow_cli.inventory import load_picker_context, build_models_payload


def test_context_uses_native_model_and_does_not_copy_provider_credentials():
    environment = SimpleNamespace(config={'model':'native-model','model_provider':'custom-native',
        'model_providers':{'custom-native':{'env_key':'PRIVATE_ENV_NAME'}}})
    with patch('agent.codex_environment.managed_environment', return_value=environment):
        context = load_picker_context()
    assert context.current_model == 'native-model'
    assert context.current_provider == 'custom-native'
    assert context.user_providers == {} and context.custom_providers == []
    assert 'PRIVATE_ENV_NAME' not in repr(context)

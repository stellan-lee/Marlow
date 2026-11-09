"""Tests for config.yaml structure validation (validate_config_structure)."""


from marlow_cli.config import validate_config_structure, ConfigIssue




def test_retired_provider_and_compression_config_has_migration_error():
    for key in ('model','providers','fallback_providers','auxiliary','context','compression'):
        issues = validate_config_structure({key:{'legacy':'value'}})
        assert any(issue.severity == 'error' and 'retired' in issue.message for issue in issues)


class TestConfigIssueDataclass:
    """ConfigIssue should be a proper dataclass."""

    def test_fields(self):
        issue = ConfigIssue(severity="error", message="test msg", hint="test hint")
        assert issue.severity == "error"
        assert issue.message == "test msg"
        assert issue.hint == "test hint"

    def test_equality(self):
        a = ConfigIssue("error", "msg", "hint")
        b = ConfigIssue("error", "msg", "hint")
        assert a == b

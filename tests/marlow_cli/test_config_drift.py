"""Regression coverage for an unused delegation configuration key."""


def test_delegation_default_toolsets_is_not_advertised():
    from marlow_cli.config import DEFAULT_CONFIG

    assert "default_toolsets" not in DEFAULT_CONFIG["delegation"]

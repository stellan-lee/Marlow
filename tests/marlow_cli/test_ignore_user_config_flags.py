"""Single-query CLI option parsing."""


def test_chat_parser_accepts_both_isolation_flags():
    from marlow_cli._parser import build_top_level_parser

    parser, _subparsers, _chat_parser = build_top_level_parser()
    args = parser.parse_args(["chat", "-q", "Hello", "--ignore-user-config", "--ignore-rules"])

    assert args.ignore_user_config is True
    assert args.ignore_rules is True

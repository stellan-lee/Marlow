from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from agent.experience.runtime import (
    ExperienceMode,
    ExperienceRuntimeTurn,
    TurnOrigin,
    _effective_mode,
    _telegram_guest_recall_authorized,
    _telegram_recall_authorized,
    copy_messages_with_experience_context,
    normalize_experience_mode,
    normalize_turn_origin,
    provider_identity,
)


@dataclass(frozen=True)
class _Result:
    items: tuple[object, ...]
    disclosures: tuple[object, ...]


class _FormattingService:
    def format_context(self, result: _Result) -> str:
        return ",".join(item.item_id for item in result.items)


def test_only_supported_origins_are_experience_eligible() -> None:
    assert TurnOrigin.CLASSIC_CLI.experience_eligible is True
    assert TurnOrigin.TELEGRAM.experience_eligible is True
    assert TurnOrigin.TELEGRAM_GUEST.experience_eligible is True
    assert TurnOrigin.TEAMS.experience_eligible is True
    assert all(
        not origin.experience_eligible
        for origin in TurnOrigin
        if origin
        not in {
            TurnOrigin.CLASSIC_CLI,
            TurnOrigin.TELEGRAM,
            TurnOrigin.TELEGRAM_GUEST,
            TurnOrigin.TEAMS,
        }
    )
    assert normalize_turn_origin("not-a-runtime") is TurnOrigin.UNKNOWN


def test_telegram_recall_requires_exact_configured_owner_dm() -> None:
    config = {
        "telegram_recall": {
            "enabled": True,
            "owner_user_id": "12345",
        }
    }
    owner_dm = SimpleNamespace(
        platform="telegram",
        _chat_type="dm",
        _user_id="12345",
        _user_id_alt=None,
    )

    assert _telegram_recall_authorized(owner_dm, config) is True
    assert _telegram_recall_authorized(
        SimpleNamespace(**{**owner_dm.__dict__, "_user_id": "54321"}),
        config,
    ) is False
    assert _telegram_recall_authorized(
        SimpleNamespace(**{**owner_dm.__dict__, "_chat_type": "group"}),
        config,
    ) is False
    assert _telegram_recall_authorized(
        owner_dm,
        {"telegram_recall": {"enabled": False, "owner_user_id": "12345"}},
    ) is False


def test_telegram_guest_recall_requires_non_admin_destination_and_gateway_role() -> None:
    config = {
        "experience": {
            "telegram_digital_twin": {
                "enabled": True,
            }
        },
        "approvals": {
            "admin": {
                "enabled": True,
                "conversation_mode": "super_admin",
                "platform": "telegram",
                "user_id": "admin-user",
                "chat_id": "admin-chat",
                "thread_id": "admin-topic",
            }
        },
    }
    guest = SimpleNamespace(
        platform="telegram",
        _chat_id="guest-chat",
        _thread_id=None,
        _user_id="guest-user",
        _user_id_alt=None,
        _telegram_digital_twin_guest=True,
    )

    assert _telegram_guest_recall_authorized(guest, config) is True
    assert _telegram_guest_recall_authorized(
        SimpleNamespace(
            **{
                **guest.__dict__,
                "_chat_id": "admin-chat",
                "_thread_id": "sibling-topic",
            }
        ),
        config,
    ) is True
    assert _telegram_guest_recall_authorized(
        SimpleNamespace(
            **{
                **guest.__dict__,
                "_chat_id": "admin-chat",
                "_thread_id": "admin-topic",
                "_user_id": "admin-user",
            }
        ),
        config,
    ) is False
    assert _telegram_guest_recall_authorized(
        SimpleNamespace(**{**guest.__dict__, "_telegram_digital_twin_guest": False}),
        config,
    ) is False
    assert _telegram_guest_recall_authorized(
        guest,
        {
            **config,
            "experience": {
                "telegram_digital_twin": {
                    "enabled": False,
                }
            },
        },
    ) is False
    assert _telegram_guest_recall_authorized(
        SimpleNamespace(
            **{
                **guest.__dict__,
                "_chat_id": "admin-chat",
                "_thread_id": "sibling-topic",
                "_user_id": "admin-user",
            }
        ),
        {
            **config,
            "approvals": {
                "admin": {
                    "enabled": True,
                    "conversation_mode": "super_admin",
                    "platform": "telegram",
                    "user_id": "admin-user",
                    "chat_id": "admin-chat",
                    "thread_id": None,
                }
            },
        },
    ) is False


def test_only_shadow_and_assist_enable_recall() -> None:
    assert ExperienceMode.SHADOW.recall_enabled is True
    assert ExperienceMode.ASSIST.recall_enabled is True
    assert ExperienceMode.OFF.recall_enabled is False
    assert ExperienceMode.CAPTURE.recall_enabled is False
    assert normalize_experience_mode("invalid") is ExperienceMode.OFF


def test_project_recall_policy_is_required_for_shadow_or_assist() -> None:
    denied = SimpleNamespace(recall_allowed=False, injection_allowed=True)
    shadow_only = SimpleNamespace(recall_allowed=True, injection_allowed=False)
    assist = SimpleNamespace(recall_allowed=True, injection_allowed=True)

    assert _effective_mode(ExperienceMode.SHADOW, denied) is None
    assert _effective_mode(ExperienceMode.ASSIST, denied) is None
    assert _effective_mode(ExperienceMode.SHADOW, shadow_only) is ExperienceMode.SHADOW
    assert _effective_mode(ExperienceMode.ASSIST, shadow_only) is ExperienceMode.SHADOW
    assert _effective_mode(ExperienceMode.ASSIST, assist) is ExperienceMode.ASSIST


def test_experience_locality_is_loopback_only() -> None:
    assert provider_identity(
        provider="ollama", base_url="http://127.0.0.1:11434"
    ).is_local
    assert not provider_identity(
        provider="ollama", base_url="http://192.168.1.20:11434"
    ).is_local
    assert not provider_identity(
        provider="ollama", base_url="http://100.64.1.20:11434"
    ).is_local


def test_request_copy_injects_only_current_user_without_mutating_source() -> None:
    source = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "current"},
    ]

    result = copy_messages_with_experience_context(
        source,
        current_user_index=3,
        context="<work-experience-context>lesson</work-experience-context>",
    )

    assert result is not source
    assert result[0] is not source[0]
    assert source[3]["content"] == "current"
    assert result[0]["content"] == "stable"
    assert result[1]["content"] == "old"
    assert result[3]["content"].endswith(
        "<work-experience-context>lesson</work-experience-context>"
    )


def test_request_copy_clones_multimodal_parts_before_injection() -> None:
    source = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            ],
        }
    ]

    result = copy_messages_with_experience_context(
        source,
        current_user_index=0,
        context="advisory",
    )

    assert result[0]["content"] is not source[0]["content"]
    assert result[0]["content"][0] is not source[0]["content"][0]
    assert len(source[0]["content"]) == 2
    assert result[0]["content"][-1] == {"type": "text", "text": "advisory"}


def test_request_copy_fails_closed_for_invalid_target_or_shape() -> None:
    source = [{"role": "assistant", "content": "answer"}]
    assert copy_messages_with_experience_context(
        source, current_user_index=0, context="must not leak"
    ) == source
    assert copy_messages_with_experience_context(
        [{"role": "user", "content": {"opaque": True}}],
        current_user_index=0,
        context="must not leak",
    ) == [{"role": "user", "content": {"opaque": True}}]


def test_fallback_provider_rechecks_egress_before_injection() -> None:
    primary_identity = provider_identity(
        provider="primary", base_url="https://primary.example"
    )
    assert primary_identity is not None
    match = SimpleNamespace(item_id="lesson-1", item_revision=1)
    disclosure = SimpleNamespace(
        item_id="lesson-1",
        item_revision=1,
        sensitivity="private_repo",
        egress_policy="same_provider_trust_domain",
        producer_trust_domain=primary_identity.trust_domain,
    )
    turn = ExperienceRuntimeTurn(
        mode=ExperienceMode.ASSIST,
        policy=SimpleNamespace(
            recall_allowed=True,
            injection_allowed=True,
            max_egress_policy="same_provider_trust_domain",
        ),
        service=_FormattingService(),
        result=_Result(items=(match,), disclosures=(disclosure,)),
    )

    assert (
        turn.context_for_request(
            provider="primary", base_url="https://primary.example"
        )
        == "lesson-1"
    )
    assert (
        turn.context_for_request(
            provider="fallback", base_url="https://fallback.example"
        )
        == ""
    )


def test_context_details_reports_only_items_actually_disclosed() -> None:
    matches = tuple(
        SimpleNamespace(item_id=f"lesson-{index}", item_revision=1)
        for index in range(1, 4)
    )
    disclosures = tuple(
        SimpleNamespace(
            item_id=match.item_id,
            item_revision=1,
            sensitivity="normal",
            egress_policy="explicit_any_provider",
            producer_trust_domain=None,
        )
        for match in matches
    )
    turn = ExperienceRuntimeTurn(
        mode=ExperienceMode.ASSIST,
        policy=SimpleNamespace(
            recall_allowed=True,
            injection_allowed=True,
            max_egress_policy="explicit_any_provider",
        ),
        service=_FormattingService(),
        result=_Result(items=matches, disclosures=disclosures),
        max_primary_lessons=2,
    )

    context, count = turn.context_for_request_details(
        provider="any",
        base_url="https://any.example",
    )
    assert context == "lesson-1,lesson-2"
    assert count == 2


def test_shadow_turn_never_formats_or_injects() -> None:
    service = _FormattingService()
    turn = ExperienceRuntimeTurn(
        mode=ExperienceMode.SHADOW,
        policy=SimpleNamespace(
            recall_allowed=True,
            injection_allowed=True,
            max_egress_policy="explicit_any_provider",
        ),
        service=service,
        result=_Result(
            items=(SimpleNamespace(item_id="lesson-1", item_revision=1),),
            disclosures=(
                SimpleNamespace(
                    item_id="lesson-1",
                    item_revision=1,
                    sensitivity="normal",
                    egress_policy="explicit_any_provider",
                    producer_trust_domain=None,
                ),
            ),
        ),
    )

    assert turn.context_for_request(provider="any", base_url="https://any.example") == ""


def test_assist_turn_requires_recall_and_injection_consents() -> None:
    match = SimpleNamespace(item_id="lesson-1", item_revision=1)
    disclosure = SimpleNamespace(
        item_id="lesson-1",
        item_revision=1,
        sensitivity="normal",
        egress_policy="explicit_any_provider",
        producer_trust_domain=None,
    )
    turn = ExperienceRuntimeTurn(
        mode=ExperienceMode.ASSIST,
        policy=SimpleNamespace(
            recall_allowed=False,
            injection_allowed=True,
            max_egress_policy="explicit_any_provider",
        ),
        service=_FormattingService(),
        result=_Result(items=(match,), disclosures=(disclosure,)),
    )

    assert turn.context_for_request(
        provider="any", base_url="https://any.example"
    ) == ""


def test_unsupported_origin_and_codex_runtime_never_open_store(monkeypatch) -> None:
    import agent.experience.store as store_module
    from agent.experience.runtime import prepare_experience_turn

    opened = 0

    def fail_if_opened(*_args, **_kwargs):
        nonlocal opened
        opened += 1
        raise AssertionError("experience store must not be opened")

    monkeypatch.setattr(store_module, "ExperienceStore", fail_if_opened)
    monkeypatch.setattr(
        "marlow_cli.config.load_config",
        lambda: {
            "experience": {
                "mode": "assist",
                "telegram_recall": {
                    "enabled": True,
                    "owner_user_id": "configured-owner",
                },
            }
        },
    )
    regular = SimpleNamespace(api_mode="chat_completions")
    codex = SimpleNamespace(api_mode="codex_app_server")
    unbound_telegram = SimpleNamespace(
        api_mode="chat_completions",
        platform="telegram",
        _chat_type="dm",
        _user_id="different-user",
        _user_id_alt=None,
    )

    assert prepare_experience_turn(
        regular,
        raw_user_message="hello",
        turn_origin=TurnOrigin.GATEWAY,
    ) is None
    assert prepare_experience_turn(
        codex,
        raw_user_message="hello",
        turn_origin=TurnOrigin.CLASSIC_CLI,
    ) is None
    assert prepare_experience_turn(
        unbound_telegram,
        raw_user_message="hello",
        turn_origin=TurnOrigin.TELEGRAM,
    ) is None
    assert opened == 0

"""Security and concurrency tests for routed administrator approvals."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import AdminApprovalConfig, GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _clear_approval_state() -> None:
    from tools import approval

    approval._gateway_queues.clear()
    approval._gateway_notify_cbs.clear()
    approval._session_approved.clear()
    approval._session_yolo.clear()
    approval._permanent_approved.clear()
    approval._pending.clear()
    approval._intent_grants.clear()


class _Adapter:
    pass


def _runner(admin: AdminApprovalConfig):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(admin_approval=admin)
    runner.adapters = {}
    runner._pending_approvals = {}
    return runner


def _event(
    text: str,
    *,
    platform: Platform = Platform.TELEGRAM,
    user_id: str = "admin-user",
    chat_id: str = "admin-chat",
    thread_id: str | None = "admin-thread",
    chat_type: str = "dm",
) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=platform,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            chat_type=chat_type,
        ),
        message_id="message-1",
    )


class TestAdminApprovalConfig:
    def test_round_trip_and_completeness(self):
        config = AdminApprovalConfig.from_dict(
            {
                "enabled": True,
                "conversation_mode": "super_admin",
                "platform": "telegram",
                "user_id": "  admin-user  ",
                "chat_id": 1234,
                "thread_id": 99,
            }
        )

        assert config.is_complete is True
        assert config.platform is Platform.TELEGRAM
        assert config.to_dict() == {
            "enabled": True,
            "conversation_mode": "super_admin",
            "platform": "telegram",
            "user_id": "admin-user",
            "chat_id": "1234",
            "thread_id": "99",
        }

    def test_invalid_platform_is_incomplete(self):
        config = AdminApprovalConfig.from_dict(
            {
                "enabled": True,
                "platform": "not-a-platform",
                "user_id": "admin-user",
                "chat_id": "admin-chat",
            }
        )

        assert config.platform is None
        assert config.is_complete is False

    def test_invalid_conversation_mode_falls_back_to_approval_only(self):
        config = AdminApprovalConfig.from_dict(
            {
                "enabled": True,
                "conversation_mode": "root_everywhere",
                "platform": "telegram",
                "user_id": "admin-user",
                "chat_id": "admin-chat",
            }
        )

        assert config.conversation_mode == "approval_only"
        assert config.is_super_admin_enabled is False

    def test_top_level_yaml_is_loaded_into_gateway_runtime(self, tmp_path):
        from gateway import config as gateway_config

        (tmp_path / "config.yaml").write_text(
            """
approvals:
  admin:
    enabled: true
    conversation_mode: super_admin
    platform: slack
    user_id: U_ADMIN
    chat_id: C_ADMIN
    thread_id: '171.2'
""".lstrip(),
            encoding="utf-8",
        )

        with patch.object(gateway_config, "get_marlow_home", return_value=tmp_path):
            config = gateway_config.load_gateway_config()

        assert config.admin_approval == AdminApprovalConfig(
            enabled=True,
            conversation_mode="super_admin",
            platform=Platform.SLACK,
            user_id="U_ADMIN",
            chat_id="C_ADMIN",
            thread_id="171.2",
        )

class TestAdminApprovalQueue:
    def setup_method(self):
        _clear_approval_state()

    def teardown_method(self):
        _clear_approval_state()

    def test_concurrent_requests_resolve_by_id_and_reject_broad_grants(self):
        from tools.approval import (
            register_gateway_notify,
            request_admin_approval,
            resolve_gateway_approval,
            reset_current_session_key,
            set_current_session_key,
        )

        session_key = "agent:main:telegram:dm:user"
        payloads: list[dict] = []
        payloads_ready = threading.Event()
        payloads_lock = threading.Lock()
        results: dict[str, dict] = {}

        def notify(payload: dict) -> None:
            with payloads_lock:
                payloads.append(dict(payload))
                if len(payloads) == 2:
                    payloads_ready.set()

        def request(action: str) -> None:
            token = set_current_session_key(session_key)
            try:
                results[action] = request_admin_approval(action, "test reason")
            finally:
                reset_current_session_key(token)

        register_gateway_notify(session_key, notify)
        first = threading.Thread(target=request, args=("first action",))
        second = threading.Thread(target=request, args=("second action",))
        first.start()
        second.start()

        assert payloads_ready.wait(timeout=3)
        by_action = {
            payload["action_intent"]["operation"]: payload
            for payload in payloads
        }
        first_id = by_action["first action"]["request_id"]
        second_id = by_action["second action"]["request_id"]
        assert first_id != second_id
        assert by_action["first action"]["allowed_choices"] == ["once", "deny"]
        assert by_action["first action"]["request_scoped_only"] is True

        # Admin requests cannot be resolved by legacy FIFO calls and never
        # create a session-wide or permanent grant.
        assert resolve_gateway_approval(session_key, "once") == 0
        assert resolve_gateway_approval(
            session_key, "always", request_id=first_id
        ) == 0
        assert first.is_alive()

        # Cards can be answered out of order without resolving the wrong tool.
        assert resolve_gateway_approval(
            session_key, "once", request_id=second_id
        ) == 1
        second.join(timeout=3)
        assert not second.is_alive()
        assert results["second action"]["approved"] is True
        assert first.is_alive()

        assert resolve_gateway_approval(
            session_key, "deny", request_id=first_id
        ) == 1
        first.join(timeout=3)
        assert not first.is_alive()
        assert results["first action"]["approved"] is False
        assert results["first action"]["decision"] == "declined"

    def test_delivery_failure_fails_closed_and_resumes_origin(self):
        from tools.approval import (
            register_gateway_notify,
            request_admin_approval,
            reset_current_session_key,
            set_current_session_key,
        )

        session_key = "agent:main:telegram:dm:user"
        resumed = threading.Event()

        def notify(_payload: dict) -> None:
            raise RuntimeError("admin adapter unavailable")

        notify.on_resolved = resumed.set
        register_gateway_notify(session_key, notify)
        token = set_current_session_key(session_key)
        try:
            result = request_admin_approval("restart production")
        finally:
            reset_current_session_key(token)

        assert result["approved"] is False
        assert result["decision"] == "delivery_failed"
        assert resumed.is_set()

    def test_tool_wrapper_returns_machine_readable_decision(self):
        from tools.admin_approval_tool import admin_approval_tool

        with patch(
            "tools.admin_approval_tool.request_admin_approval",
            return_value={"approved": False, "decision": "declined"},
        ):
            result = json.loads(admin_approval_tool("deploy", "production"))

        assert result == {"approved": False, "decision": "declined"}

    def test_admin_route_overrides_mode_off_yolo_and_prior_grants(self):
        from tools import approval
        from tools.action_intent import argument_digest

        session_key = "agent:main:telegram:dm:user"
        secret = "sk-test1234567890"
        purpose = f"Remove the temporary admin approval fixture using {secret}"
        command = "rm -rf /tmp/admin-approval-test"
        token = approval.set_current_session_key(session_key)
        approval.enable_session_yolo(session_key)
        _, pattern_key, _ = approval.detect_dangerous_command(
            "rm -rf /tmp/admin-approval-test"
        )
        approval.approve_session(session_key, pattern_key)
        seen = []

        def notify(payload):
            seen.append(dict(payload))
            approval.resolve_gateway_approval(
                session_key,
                "once",
                request_id=payload["request_id"],
            )

        approval.register_gateway_notify(session_key, notify)
        try:
            with (
                patch.object(approval, "_is_gateway_approval_context", return_value=True),
                patch.object(
                    approval,
                    "_get_approval_config",
                    return_value={
                        "mode": "off",
                        "admin": {"enabled": True},
                    },
                ),
                patch(
                    "tools.tirith_security.check_command_security",
                    return_value={"action": "allow", "findings": [], "summary": ""},
                ),
            ):
                result = approval.check_all_command_guards(
                    command,
                    "local",
                    purpose=purpose,
                )
        finally:
            approval.unregister_gateway_notify(session_key)
            approval.disable_session_yolo(session_key)
            approval.reset_current_session_key(token)

        assert result["approved"] is True
        assert result["user_approved"] is True
        assert len(seen) == 1
        assert seen[0]["kind"] == "action_intent"
        assert seen[0]["action_intent"]["action_type"] == "terminal.execute"
        assert seen[0]["action_intent"]["operation"] == (
            "Remove the temporary admin approval fixture using ***"
        )
        assert secret not in seen[0]["action"]
        assert seen[0]["action"].startswith(
            "Action: Remove the temporary admin approval fixture using ***"
        )
        assert "Action: execute terminal command" not in seen[0]["action"]
        assert not seen[0]["action"].lstrip().startswith("{")
        assert seen[0]["action_intent"]["argument_digest"] == argument_digest(
            "terminal",
            {
                "purpose": purpose,
                "command": command,
                "environment": "local",
                "workdir": "",
            },
        )
        assert "Impact: The command was flagged as capable of changing" in seen[0][
            "description"
        ]

    def test_admin_route_overrides_smart_auto_approval_for_execute_code(self):
        from tools import approval

        session_key = "agent:main:telegram:dm:user"
        token = approval.set_current_session_key(session_key)
        seen = []

        def notify(payload):
            seen.append(dict(payload))
            approval.resolve_gateway_approval(
                session_key,
                "once",
                request_id=payload["request_id"],
            )

        approval.register_gateway_notify(session_key, notify)
        try:
            with (
                patch.object(approval, "_is_gateway_approval_context", return_value=True),
                patch.object(
                    approval,
                    "_get_approval_config",
                    return_value={
                        "mode": "smart",
                        "admin": {"enabled": True},
                    },
                ),
                patch.object(approval, "_smart_approve", return_value="approve") as smart,
            ):
                result = approval.check_execute_code_guard("print('hello')", "local")
        finally:
            approval.unregister_gateway_notify(session_key)
            approval.reset_current_session_key(token)

        assert result["approved"] is True
        assert result["user_approved"] is True
        assert len(seen) == 1
        assert seen[0]["kind"] == "action_intent"
        assert seen[0]["action_intent"]["action_type"] == "code.execute"
        assert seen[0]["action"].startswith("Action: execute Python code")
        assert "Impact: The script can spawn processes" in seen[0]["description"]
        smart.assert_not_called()

    def test_admin_mode_overrides_yolo_mode_off_and_prior_grants(self):
        from tools import approval

        session_key = "agent:main:telegram:dm:user"
        token = approval.set_current_session_key(session_key)
        approval.enable_session_yolo(session_key)
        approval.approve_session(session_key, "recursive delete")
        try:
            with (
                patch.object(approval, "_is_gateway_approval_context", return_value=True),
                patch.object(
                    approval,
                    "_get_approval_config",
                    return_value={
                        "mode": "off",
                        "admin": {"enabled": True},
                    },
                ),
            ):
                result = approval.check_all_command_guards(
                    "rm -rf /tmp/admin-review-required",
                    "local",
                    purpose="Remove the temporary review fixture",
                )
        finally:
            approval.reset_current_session_key(token)

        assert result["approved"] is False
        assert result["approval_pending"] is True

    def test_admin_terminal_approval_without_purpose_fails_before_notification(self):
        from tools import approval

        session_key = "agent:main:telegram:dm:user"
        notified = []
        token = approval.set_current_session_key(session_key)
        approval.register_gateway_notify(session_key, lambda payload: notified.append(payload))
        try:
            with (
                patch.object(approval, "_is_gateway_approval_context", return_value=True),
                patch.object(
                    approval,
                    "_get_approval_config",
                    return_value={"mode": "manual", "admin": {"enabled": True}},
                ),
                patch(
                    "tools.tirith_security.check_command_security",
                    return_value={"action": "allow", "findings": [], "summary": ""},
                ),
            ):
                result = approval.check_all_command_guards(
                    "rm -rf /tmp/admin-review-required",
                    "local",
                )
        finally:
            approval.unregister_gateway_notify(session_key)
            approval.reset_current_session_key(token)

        assert result["approved"] is False
        assert result["outcome"] == "missing_purpose"
        assert "purpose field" in result["message"]
        assert notified == []

    def test_approved_plan_returns_grant_and_consumes_exact_next_command(self):
        from tools import approval

        session_key = "agent:main:telegram:dm:planned"
        purpose = "Clean two temporary deployment fixtures"
        first = "rm -rf /tmp/intent-plan-one"
        second = "rm -rf /tmp/intent-plan-two"
        plan = [
            {"command": second, "workdir": "/tmp"},
            {"command": first, "workdir": "/tmp"},
        ]
        seen = []

        def notify(payload):
            seen.append(dict(payload))
            approval.resolve_gateway_approval(
                session_key, "once", request_id=payload["request_id"]
            )

        token = approval.set_current_session_key(session_key)
        approval.register_gateway_notify(session_key, notify)
        try:
            with (
                patch.object(approval, "_is_gateway_approval_context", return_value=True),
                patch.object(
                    approval,
                    "_get_approval_config",
                    return_value={"mode": "manual", "admin": {"enabled": True}},
                ),
                patch(
                    "tools.tirith_security.check_command_security",
                    return_value={"action": "allow", "findings": [], "summary": ""},
                ),
            ):
                approved = approval.check_all_command_guards(
                    first,
                    "local",
                    purpose=purpose,
                    workdir="/tmp",
                    approval_plan=plan,
                )
                grant_id = approved["intent_grant_id"]
                consumed = approval.check_all_command_guards(
                    second,
                    "local",
                    purpose=purpose,
                    workdir="/tmp",
                    intent_grant_id=grant_id,
                )
        finally:
            approval.unregister_gateway_notify(session_key)
            approval.reset_current_session_key(token)

        assert approved["intent_grant_remaining"] == 1
        assert approved["intent_grant_expires_in_seconds"] == 900
        assert consumed == {
            "approved": True,
            "message": None,
            "intent_grant_used": True,
            "intent_grant_id": grant_id,
            "intent_grant_remaining": 0,
        }
        assert len(seen) == 1
        assert seen[0]["action_intent"]["parameters"]["command_plan"] == [
            {"command": first, "environment": "local", "workdir": "/tmp"},
            {"command": second, "environment": "local", "workdir": "/tmp"},
        ]

    @pytest.mark.parametrize(
        ("changed_purpose", "changed_command", "changed_workdir"),
        [
            ("A different intent", None, None),
            (None, "rm -rf /tmp/not-in-the-plan", None),
            (None, None, "/var/tmp"),
        ],
    )
    def test_grant_mismatch_requires_new_approval(
        self, changed_purpose, changed_command, changed_workdir
    ):
        from tools import approval

        session_key = "agent:main:telegram:dm:mismatch"
        purpose = "Clean temporary fixtures"
        first = "rm -rf /tmp/intent-plan-first"
        second = "rm -rf /tmp/intent-plan-second"
        seen = []

        def notify(payload):
            seen.append(dict(payload))
            choice = "once" if len(seen) == 1 else "deny"
            approval.resolve_gateway_approval(
                session_key, choice, request_id=payload["request_id"]
            )

        token = approval.set_current_session_key(session_key)
        approval.register_gateway_notify(session_key, notify)
        try:
            with (
                patch.object(approval, "_is_gateway_approval_context", return_value=True),
                patch.object(
                    approval,
                    "_get_approval_config",
                    return_value={"mode": "manual", "admin": {"enabled": True}},
                ),
                patch(
                    "tools.tirith_security.check_command_security",
                    return_value={"action": "allow", "findings": [], "summary": ""},
                ),
            ):
                approved = approval.check_all_command_guards(
                    first,
                    "local",
                    purpose=purpose,
                    workdir="/tmp",
                    approval_plan=[
                        {"command": first, "workdir": "/tmp"},
                        {"command": second, "workdir": "/tmp"},
                    ],
                )
                result = approval.check_all_command_guards(
                    changed_command or second,
                    "local",
                    purpose=changed_purpose or purpose,
                    workdir=changed_workdir or "/tmp",
                    intent_grant_id=approved["intent_grant_id"],
                )
        finally:
            approval.unregister_gateway_notify(session_key)
            approval.reset_current_session_key(token)

        assert result["approved"] is False
        assert result["outcome"] == "denied"
        assert len(seen) == 2

    def test_grant_is_session_bound_and_expires(self):
        from tools import approval

        purpose = "Clean a temporary fixture"
        command = "rm -rf /tmp/session-bound-plan"
        digest = approval._terminal_grant_digest(command, "local", "/tmp")
        grant_id = "test-expiring-grant"
        approval._intent_grants[grant_id] = {
            "session_key": "session-a",
            "purpose_digest": approval._purpose_grant_digest(purpose),
            "expires_at": 100.0,
            "remaining": {digest: 1},
        }

        with patch.object(approval.time, "monotonic", return_value=50.0):
            assert approval._consume_terminal_intent_grant(
                grant_id=grant_id,
                session_key="session-b",
                purpose=purpose,
                command=command,
                env_type="local",
                workdir="/tmp",
            ) is None
        with patch.object(approval.time, "monotonic", return_value=101.0):
            assert approval._consume_terminal_intent_grant(
                grant_id=grant_id,
                session_key="session-a",
                purpose=purpose,
                command=command,
                env_type="local",
                workdir="/tmp",
            ) is None
        assert grant_id not in approval._intent_grants

    def test_grant_consumption_is_atomic(self):
        from tools import approval

        purpose = "Clean one temporary fixture"
        command = "rm -rf /tmp/atomic-plan"
        digest = approval._terminal_grant_digest(command, "local", "/tmp")
        grant_id = "test-atomic-grant"
        approval._intent_grants[grant_id] = {
            "session_key": "session-a",
            "purpose_digest": approval._purpose_grant_digest(purpose),
            "expires_at": approval.time.monotonic() + 60,
            "remaining": {digest: 1},
        }
        barrier = threading.Barrier(3)
        results = []

        def consume():
            barrier.wait()
            results.append(approval._consume_terminal_intent_grant(
                grant_id=grant_id,
                session_key="session-a",
                purpose=purpose,
                command=command,
                env_type="local",
                workdir="/tmp",
            ))

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        assert sorted(results, key=lambda value: value is None) == [0, None]

    def test_plan_cannot_preapprove_hardline_command(self):
        from tools import approval

        notified = []
        with (
            patch.object(approval, "_is_gateway_approval_context", return_value=True),
            patch.object(
                approval,
                "_get_approval_config",
                return_value={"mode": "manual", "admin": {"enabled": True}},
            ),
        ):
            result = approval.check_all_command_guards(
                "echo prepare",
                "local",
                purpose="Prepare then erase the host",
                approval_plan=[
                    {"command": "echo prepare"},
                    {"command": "rm -rf /"},
                ],
            )

        assert result["approved"] is False
        assert result["outcome"] == "invalid_approval_plan"
        assert "unconditionally blocked" in result["message"]
        assert notified == []

    def test_hardline_guard_runs_before_even_a_forged_grant(self):
        from tools import approval

        purpose = "Erase the host"
        command = "rm -rf /"
        grant_id = "forged-hardline-grant"
        digest = approval._terminal_grant_digest(command, "local", "/")
        approval._intent_grants[grant_id] = {
            "session_key": "session-a",
            "purpose_digest": approval._purpose_grant_digest(purpose),
            "expires_at": approval.time.monotonic() + 60,
            "remaining": {digest: 1},
        }
        token = approval.set_current_session_key("session-a")
        try:
            result = approval.check_all_command_guards(
                command,
                "local",
                purpose=purpose,
                workdir="/",
                intent_grant_id=grant_id,
            )
        finally:
            approval.reset_current_session_key(token)

        assert result["approved"] is False
        assert result["hardline"] is True
        assert grant_id in approval._intent_grants


class TestAdminApprovalRouting:
    def test_routes_to_exact_configured_admin_destination(self):
        admin = AdminApprovalConfig(
            enabled=True,
            platform=Platform.SLACK,
            user_id="U_ADMIN",
            chat_id="C_ADMIN",
            thread_id="171.2",
        )
        runner = _runner(admin)
        origin_adapter = _Adapter()
        admin_adapter = _Adapter()
        runner.adapters = {Platform.SLACK: admin_adapter}

        route = runner._resolve_approval_delivery_route(
            origin_adapter=origin_adapter,
            origin_chat_id="requester-chat",
            origin_metadata=None,
            kind="command",
        )

        assert route == {
            "adapter": admin_adapter,
            "chat_id": "C_ADMIN",
            "metadata": {"notify": True, "thread_id": "171.2"},
            "authorized_user_id": "U_ADMIN",
            "binary": True,
            "title": "Admin Approval Required",
            "admin_routed": True,
            "admin_platform": Platform.SLACK,
            "presentation": "fallback",
        }

    def test_general_admin_request_never_falls_back_to_requester(self):
        runner = _runner(AdminApprovalConfig(enabled=False))

        with pytest.raises(RuntimeError, match="not enabled"):
            runner._resolve_approval_delivery_route(
                origin_adapter=_Adapter(),
                origin_chat_id="requester-chat",
                origin_metadata=None,
                kind="admin_action",
            )

    def test_action_intent_uses_action_title_and_never_falls_back(self):
        runner = _runner(AdminApprovalConfig(enabled=False))

        with pytest.raises(RuntimeError, match="not enabled"):
            runner._resolve_approval_delivery_route(
                origin_adapter=_Adapter(),
                origin_chat_id="requester-chat",
                origin_metadata=None,
                kind="action_intent",
            )

        runner = _runner(
            AdminApprovalConfig(
                enabled=True,
                platform=Platform.SLACK,
                user_id="U_ADMIN",
                chat_id="C_ADMIN",
            )
        )
        runner.adapters = {Platform.SLACK: _Adapter()}
        route = runner._resolve_approval_delivery_route(
            origin_adapter=_Adapter(),
            origin_chat_id="requester-chat",
            origin_metadata=None,
            kind="action_intent",
        )

        assert route["title"] == "Action Approval Required"

    def test_enabled_incomplete_route_fails_closed(self):
        runner = _runner(
            AdminApprovalConfig(
                enabled=True,
                platform=Platform.TELEGRAM,
                user_id="admin-user",
                chat_id="",
            )
        )

        with pytest.raises(RuntimeError, match="chat_id is missing"):
            runner._resolve_approval_delivery_route(
                origin_adapter=_Adapter(),
                origin_chat_id="requester-chat",
                origin_metadata=None,
                kind="command",
            )

    def test_disconnected_admin_platform_fails_closed(self):
        runner = _runner(
            AdminApprovalConfig(
                enabled=True,
                platform=Platform.SLACK,
                user_id="admin-user",
                chat_id="admin-chat",
            )
        )

        with pytest.raises(RuntimeError, match="not connected"):
            runner._resolve_approval_delivery_route(
                origin_adapter=_Adapter(),
                origin_chat_id="requester-chat",
                origin_metadata=None,
                kind="command",
            )


    def test_admin_principal_private_origin_receives_request_scoped_approval(self):
        admin = AdminApprovalConfig(
            enabled=True,
            platform=Platform.TELEGRAM,
            user_id="admin-user",
            chat_id="admin-chat",
            thread_id="admin-thread",
            chat_type="dm",
        )
        runner = _runner(admin)
        admin_adapter = _Adapter()
        runner.adapters = {Platform.TELEGRAM: admin_adapter}

        route = runner._resolve_approval_delivery_route(
            origin_adapter=admin_adapter,
            origin_chat_id="admin-chat",
            origin_metadata={"thread_id": "admin-thread"},
            kind="command",
            source=_event("hello", user_id="admin-user", chat_id="admin-chat", thread_id="admin-thread", chat_type="dm").source,
        )

        assert route["adapter"] is admin_adapter
        assert route["chat_id"] == "admin-chat"
        assert route["metadata"] == {"thread_id": "admin-thread"}
        assert route["authorized_user_id"] == "admin-user"
        assert route["admin_routed"] is True
        assert route["presentation"] == "origin"

    def test_non_admin_request_uses_configured_fallback(self):
        admin = AdminApprovalConfig(
            enabled=True,
            platform=Platform.SLACK,
            user_id="U_ADMIN",
            chat_id="C_ADMIN",
        )
        runner = _runner(admin)
        admin_adapter = _Adapter()
        runner.adapters = {Platform.SLACK: admin_adapter}

        route = runner._resolve_approval_delivery_route(
            origin_adapter=_Adapter(),
            origin_chat_id="requester-chat",
            origin_metadata=None,
            kind="command",
            source=_event("hello", user_id="requester", chat_id="requester-chat").source,
        )

        assert route["adapter"] is admin_adapter
        assert route["chat_id"] == "C_ADMIN"
        assert route["presentation"] == "fallback"

    def test_shared_fallback_is_disabled_by_default(self):
        runner = _runner(
            AdminApprovalConfig(
                enabled=True,
                platform=Platform.SLACK,
                user_id="U_ADMIN",
                chat_id="C_ADMIN",
                chat_type="group",
            )
        )
        runner.adapters = {Platform.SLACK: _Adapter()}

        with pytest.raises(RuntimeError, match="shared fallback is disabled"):
            runner._resolve_approval_delivery_route(
                origin_adapter=_Adapter(),
                origin_chat_id="requester-chat",
                origin_metadata=None,
                kind="command",
                source=_event("hello", user_id="requester", chat_id="requester-chat").source,
            )

    def test_shared_fallback_requires_explicit_enablement(self):
        runner = _runner(
            AdminApprovalConfig(
                enabled=True,
                platform=Platform.SLACK,
                user_id="U_ADMIN",
                chat_id="C_ADMIN",
                chat_type="group",
                allow_shared_fallback=True,
            )
        )
        runner.adapters = {Platform.SLACK: _Adapter()}

        route = runner._resolve_approval_delivery_route(
            origin_adapter=_Adapter(),
            origin_chat_id="requester-chat",
            origin_metadata=None,
            kind="command",
            source=_event("hello", user_id="requester", chat_id="requester-chat").source,
        )

        assert route["presentation"] == "fallback"


class TestSuperAdminConversation:
    def test_exact_identity_chat_and_thread_match(self):
        admin = AdminApprovalConfig(
            enabled=True,
            conversation_mode="super_admin",
            platform=Platform.TELEGRAM,
            user_id="admin-user",
            chat_id="admin-chat",
            thread_id="admin-thread",
        )
        runner = _runner(admin)

        assert runner._is_super_admin_source(_event("hello").source) is True
        assert runner._is_super_admin_source(
            _event("hello", user_id="attacker").source
        ) is False
        assert runner._is_super_admin_source(
            _event("hello", chat_id="other-chat").source
        ) is False
        assert runner._is_super_admin_source(
            _event("hello", thread_id="other-thread").source
        ) is False
        assert runner._is_super_admin_source(
            _event("hello", platform=Platform.SLACK).source
        ) is False

    def test_missing_thread_configuration_trusts_admin_across_chat(self):
        admin = AdminApprovalConfig(
            enabled=True,
            conversation_mode="super_admin",
            platform=Platform.TELEGRAM,
            user_id="admin-user",
            chat_id="admin-chat",
            thread_id=None,
        )
        runner = _runner(admin)

        assert runner._is_super_admin_source(
            _event("hello", thread_id="any-thread").source
        ) is True
        assert runner._is_user_authorized(
            _event("hello", thread_id="any-thread").source
        ) is True

    def test_approval_only_route_does_not_elevate_conversation(self):
        runner = _runner(
            AdminApprovalConfig(
                enabled=True,
                conversation_mode="approval_only",
                platform=Platform.TELEGRAM,
                user_id="admin-user",
                chat_id="admin-chat",
            )
        )

        assert runner._is_super_admin_source(_event("hello").source) is False

    def test_super_admin_bypasses_slash_access_and_gets_authority_prompt(self):
        runner = _runner(
            AdminApprovalConfig(
                enabled=True,
                conversation_mode="super_admin",
                platform=Platform.TELEGRAM,
                user_id="admin-user",
                chat_id="admin-chat",
                thread_id="admin-thread",
            )
        )

        assert runner._check_slash_access(_event("/restart").source, "restart") is None
        prompt = runner._super_admin_authority_prompt()
        assert "do not refuse solely because an action is risky" in prompt
        assert "authorized automatically" in prompt

    @pytest.mark.asyncio
    async def test_whoami_reports_super_admin_authority(self):
        runner = _runner(
            AdminApprovalConfig(
                enabled=True,
                conversation_mode="super_admin",
                platform=Platform.TELEGRAM,
                user_id="admin-user",
                chat_id="admin-chat",
                thread_id="admin-thread",
            )
        )

        result = await runner._handle_whoami_command(_event("/whoami"))

        assert "Tier: **super admin**" in result
        assert "automatically authorized" in result

    @pytest.mark.asyncio
    async def test_yolo_reports_that_super_admin_is_already_authorized(self):
        runner = _runner(
            AdminApprovalConfig(
                enabled=True,
                conversation_mode="super_admin",
                platform=Platform.TELEGRAM,
                user_id="admin-user",
                chat_id="admin-chat",
                thread_id="admin-thread",
            )
        )

        result = await runner._handle_yolo_command(_event("/yolo"))

        assert "already auto-approves" in str(result)
        assert "disabled" not in str(result).lower()

    @pytest.mark.asyncio
    async def test_super_admin_auto_approves_slash_confirmation(self):
        runner = _runner(
            AdminApprovalConfig(
                enabled=True,
                conversation_mode="super_admin",
                platform=Platform.TELEGRAM,
                user_id="admin-user",
                chat_id="admin-chat",
                thread_id="admin-thread",
            )
        )
        calls = []

        async def execute():
            calls.append(True)
            return "executed"

        result = await runner._maybe_confirm_destructive_slash(
            event=_event("/new"),
            command="new",
            title="/new",
            detail="reset",
            execute=execute,
        )

        assert result == "executed"
        assert calls == [True]


class TestSuperAdminApprovals:
    def setup_method(self):
        _clear_approval_state()

    def teardown_method(self):
        _clear_approval_state()

    def test_common_and_execute_code_approvals_are_automatic(self):
        from tools.approval import (
            check_all_command_guards,
            check_execute_code_guard,
            reset_super_admin_context,
            set_super_admin_context,
        )

        token = set_super_admin_context(True)
        try:
            command = check_all_command_guards(
                "rm -rf /tmp/marlow-super-admin-test",
                "local",
            )
            code = check_execute_code_guard("print('trusted')", "local")
        finally:
            reset_super_admin_context(token)

        assert command["approved"] is True
        assert command["super_admin_approved"] is True
        assert code["approved"] is True
        assert code["super_admin_approved"] is True

    def test_non_approvable_hardline_guard_remains_blocked(self):
        from tools.approval import (
            check_all_command_guards,
            reset_super_admin_context,
            set_super_admin_context,
        )

        token = set_super_admin_context(True)
        try:
            result = check_all_command_guards("rm -rf /", "local")
        finally:
            reset_super_admin_context(token)

        assert result["approved"] is False
        assert result.get("hardline") is True

    def test_structured_admin_action_is_automatic_and_not_queued(self):
        from tools import approval
        from tools.action_intent import build_manual_action_intent

        session_token = approval.set_current_session_key("telegram:admin")
        admin_token = approval.set_super_admin_context(True)
        try:
            result = approval.request_action_intent_approval(
                build_manual_action_intent(
                    "deploy production",
                    "requested by administrator",
                    operation="deploy",
                    target="production",
                    impact="Production will be updated.",
                )
            )
        finally:
            approval.reset_super_admin_context(admin_token)
            approval.reset_current_session_key(session_token)

        assert result["approved"] is True
        assert result["decision"] == "super_admin"
        assert approval._gateway_queues == {}


class TestAdminApprovalCommands:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/approve", "/deny"])
    async def test_requester_text_commands_cannot_bypass_admin_controls(self, command):
        runner = _runner(
            AdminApprovalConfig(
                enabled=True,
                platform=Platform.TELEGRAM,
                user_id="admin-user",
                chat_id="admin-chat",
            )
        )

        if command == "/approve":
            result = await runner._handle_approve_command(
                _event(command, user_id="requester", chat_id="requester-chat")
            )
        else:
            result = await runner._handle_deny_command(
                _event(command, user_id="requester", chat_id="requester-chat")
            )

        assert "Admin-routed approvals" in result

    @pytest.mark.asyncio
    async def test_yolo_is_disabled_while_admin_routing_is_enabled(self):
        runner = _runner(
            AdminApprovalConfig(
                enabled=True,
                platform=Platform.TELEGRAM,
                user_id="admin-user",
                chat_id="admin-chat",
            )
        )

        result = await runner._handle_yolo_command(
            _event("/yolo", user_id="requester", chat_id="requester-chat")
        )

        assert "disabled while admin approval routing is enabled" in str(result)

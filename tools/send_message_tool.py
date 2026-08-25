"""Send Message Tool -- cross-channel messaging via platform APIs.

Sends a message to a user or channel on a retained messaging platform
(Telegram, Discord, Slack, Feishu, email, or a plugin platform). Supports listing available targets and resolving
human-friendly channel names to IDs. Works in both CLI and gateway contexts.
"""

import asyncio
import json
import logging
import os
import re
import ssl
import time
from email.utils import formatdate

from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)

_TELEGRAM_TOPIC_TARGET_RE = re.compile(r"^\s*(-?\d+)(?::(\d+))?\s*$")
_FEISHU_TARGET_RE = re.compile(r"^\s*((?:oc|ou|on|chat|open)_[-A-Za-z0-9]+)(?::([-A-Za-z0-9_]+))?\s*$")
# Slack conversation IDs: C (public channel), G (private/group channel), D (DM).
# Must be uppercase alphanumeric, 9+ chars. User IDs (U...) and workspace IDs
# (W...) are NOT valid chat.postMessage channel values — posting to them fails
# because the API requires a conversation ID. To DM a user you must first call
# conversations.open to obtain a D... ID. Without this gate, Slack IDs fall
# through to channel-name resolution, which only matches by name and fails.
_SLACK_TARGET_RE = re.compile(r"^\s*([CGDU][A-Z0-9]{8,})\s*$")
# Session-derived Slack thread targets use "<conversation_id>:<thread_ts>".
_SLACK_THREAD_TARGET_RE = re.compile(r"^\s*([CGD][A-Z0-9]{8,}):([^\s:]+)\s*$")
# Discord snowflake IDs are numeric, same regex pattern as Telegram topic targets.
_NUMERIC_TOPIC_RE = _TELEGRAM_TOPIC_TARGET_RE
# Email addresses — a valid email like "user@domain.com" should be treated as
# an explicit target for the email platform, not fall through to channel-name
# resolution which has no way to resolve a raw address.
_EMAIL_TARGET_RE = re.compile(r"^\s*[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\s*$")
# Most platforms read their home channel from "<PLATFORM>_HOME_CHANNEL", but a
# few diverge. Email reads EMAIL_HOME_ADDRESS (see gateway/config.py), so the
# generic "<PLATFORM>_HOME_CHANNEL" hint would point users at a variable that is
# never read. Map the exceptions so the error guidance is actually actionable.
_HOME_CHANNEL_ENV_OVERRIDES = {"email": "EMAIL_HOME_ADDRESS"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".3gp"}
_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac"}
_VOICE_EXTS = {".ogg", ".opus"}
# Telegram's Bot API sendAudio only accepts MP3 / M4A. Other audio
# formats either route through sendVoice (Opus/OGG) or fall back to
# document delivery.
_TELEGRAM_SEND_AUDIO_EXTS = {".mp3", ".m4a"}
_URL_SECRET_QUERY_RE = re.compile(
    r"([?&](?:access_token|api[_-]?key|auth[_-]?token|token|signature|sig)=)([^&#\s]+)",
    re.IGNORECASE,
)
_GENERIC_SECRET_ASSIGN_RE = re.compile(
    r"\b(access_token|api[_-]?key|auth[_-]?token|signature|sig)\s*=\s*([^\s,;]+)",
    re.IGNORECASE,
)


def _sanitize_error_text(text) -> str:
    """Redact secrets from error text before surfacing it to users/models."""
    redacted = redact_sensitive_text(text)
    redacted = _URL_SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}***", redacted)
    redacted = _GENERIC_SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=***", redacted)
    return redacted


def _error(message: str) -> dict:
    """Build a standardized error payload with redacted content."""
    return {"error": _sanitize_error_text(message)}


def _telegram_retry_delay(exc: Exception, attempt: int) -> float | None:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        try:
            return max(float(retry_after), 0.0)
        except (TypeError, ValueError):
            return 1.0

    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        return None
    if (
        "bad gateway" in text
        or "502" in text
        or "too many requests" in text
        or "429" in text
        or "service unavailable" in text
        or "503" in text
        or "gateway timeout" in text
        or "504" in text
    ):
        return float(2 ** attempt)
    return None


async def _send_telegram_message_with_retry(bot, *, attempts: int = 3, **kwargs):
    for attempt in range(attempts):
        try:
            return await bot.send_message(**kwargs)
        except Exception as exc:
            delay = _telegram_retry_delay(exc, attempt)
            if delay is None or attempt >= attempts - 1:
                raise
            logger.warning(
                "Transient Telegram send failure (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1,
                attempts,
                delay,
                _sanitize_error_text(exc),
            )
            await asyncio.sleep(delay)


SEND_MESSAGE_SCHEMA = {
    "name": "send_message",
    "description": (
        "Send a message to a connected messaging platform, or list available targets.\n\n"
        "This tool sends to a different conversation. Never use it to reply to the "
        "current conversation; put current-conversation content in the assistant "
        "response. When the user asks to send to a specific channel or person, call "
        "send_message(action='list') FIRST to discover a concrete target. During an "
        "interactive turn, a bare platform name is ambiguous and will be rejected. "
        "Marlow will ask the initiating user to confirm the exact destination and "
        "payload before sending."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "list"],
                "description": "Action to perform. 'send' (default) sends a message. 'list' returns all available channels/contacts across connected platforms."
            },
            "target": {
                "type": "string",
                "description": "Delivery target. During an interactive turn, use a concrete target returned by action='list'; such as 'telegram:chat_id:thread_id', 'discord:channel_id:thread_id', 'slack:conversation_id:thread_ts', 'feishu:chat_id:thread_id', or 'email:user@example.com'. A bare platform name is ambiguous and will be rejected before sending."
            },
            "message": {
                "type": "string",
                "description": "The message text to send. To send an image or file, include MEDIA:<local_path> (e.g. 'MEDIA:/tmp/report.pdf') in the message — the platform will deliver it as a native media attachment."
            }
        },
        "required": []
    }
}


def send_message_tool(args, **kw):
    """Handle cross-channel send_message tool calls."""
    action = args.get("action", "send")

    if action == "list":
        return _handle_list()

    return _handle_send(args)


def _get_turn_context_for_send_message():
    """Return the typed gateway turn context when available."""
    try:
        from gateway.turn_context import get_current_turn, ExecutionMode
        turn = get_current_turn()
        if turn is not None and turn.mode == ExecutionMode.INTERACTIVE:
            return turn
    except Exception:
        pass
    return None


def _parse_target_identity(target: str):
    """Parse a target into platform/chat/thread without home fallback."""
    parts = target.split(":", 2)
    platform_name = parts[0].strip().lower() if parts else ""
    if not platform_name or len(parts) < 2:
        return platform_name, None, None, False
    target_ref = parts[1].strip()
    thread_id = parts[2].strip() if len(parts) > 2 else None
    if not target_ref:
        return platform_name, None, None, False
    if target_ref.lower() == "home":
        return platform_name, None, thread_id, False
    chat_id, parsed_thread_id, is_explicit = _parse_target_ref(platform_name, target_ref)
    if is_explicit:
        return platform_name, chat_id, parsed_thread_id or thread_id, True
    return platform_name, None, thread_id, False


def _handle_list():
    """Return formatted list of available messaging targets."""
    try:
        from gateway.channel_directory import format_directory_for_display
        return json.dumps({"targets": format_directory_for_display()})
    except Exception as e:
        return json.dumps(_error(f"Failed to load channel directory: {e}"))


def _handle_send(args):
    """Send a message to a platform target."""
    target = args.get("target", "")
    message = args.get("message", "")
    if not target or not message:
        return tool_error("Both 'target' and 'message' are required when action='send'")

    turn = _get_turn_context_for_send_message()
    interactive = turn is not None
    target_ref = target.split(":", 1)[1].strip() if ":" in target else ""
    platform_name, chat_id, thread_id, is_explicit = _parse_target_identity(target)

    if not platform_name:
        return json.dumps({"success": False, "policy_denied": True, "reason": "ambiguous_destination"})

    if interactive and not target_ref:
        return json.dumps({"success": False, "policy_denied": True, "reason": "ambiguous_destination"})

    # Resolve human-friendly channel names to numeric IDs when the model
    # supplied a concrete non-home target. Bare platform names are rejected
    # for interactive turns instead of falling back to home channels.
    if target_ref.lower() == "home":
        try:
            from gateway.config import load_gateway_config, Platform
            config = load_gateway_config()
        except Exception as e:
            return json.dumps(_error(f"Failed to load gateway config: {e}"))
        home = config.get_home_channel(Platform(platform_name))
        if home:
            chat_id = home.chat_id
            thread_id = thread_id or home.thread_id
            is_explicit = True
        elif interactive:
            return json.dumps({
                "success": False,
                "policy_denied": True,
                "reason": "ambiguous_destination",
            })
        else:
            return tool_error(f"No home channel set for {platform_name} to determine where to send the message.")

    if chat_id is None and thread_id is None and not is_explicit and target_ref:
        try:
            from gateway.channel_directory import resolve_channel_name
            resolved = resolve_channel_name(platform_name, target_ref)
            if resolved:
                platform_name, chat_id, thread_id, is_explicit = _parse_target_identity(f"{platform_name}:{resolved}")
            elif interactive:
                return json.dumps({
                    "success": False,
                    "policy_denied": True,
                    "reason": "ambiguous_destination",
                })
            else:
                return json.dumps({
                    "error": f"Could not resolve '{target_ref}' on {platform_name}. "
                    f"Use send_message(action='list') to see available targets."
                })
        except Exception:
            if interactive:
                return json.dumps({
                    "success": False,
                    "policy_denied": True,
                    "reason": "ambiguous_destination",
                })
            return json.dumps({
                "error": f"Could not resolve '{target_ref}' on {platform_name}. "
                f"Try using a numeric channel ID instead."
            })

    from tools.interrupt import is_interrupted
    if is_interrupted():
        return tool_error("Interrupted")

    try:
        from gateway.config import load_gateway_config, Platform
        config = load_gateway_config()
    except Exception as e:
        return json.dumps(_error(f"Failed to load gateway config: {e}"))

    if not interactive and not chat_id and not target_ref:
        home = config.get_home_channel(Platform(platform_name))
        if home:
            chat_id = home.chat_id
            thread_id = thread_id or home.thread_id
            is_explicit = True
        else:
            home_env = _HOME_CHANNEL_ENV_OVERRIDES.get(
                platform_name, f"{platform_name.upper()}_HOME_CHANNEL"
            )
            return json.dumps({
                "error": f"No home channel set for {platform_name} to determine where to send the message. "
                f"Either specify a channel directly with '{platform_name}:CHANNEL_NAME', "
                f"or set a home channel via: marlow config set {home_env} <channel_id>"
            })

    # Accept any platform name — built-in names resolve to their enum
    # member, plugin platform names create dynamic members via _missing_().
    try:
        platform = Platform(platform_name)
    except (ValueError, KeyError):
        return tool_error(f"Unknown platform: {platform_name}")

    pconfig = config.platforms.get(platform)
    if not pconfig or not pconfig.enabled:
        return tool_error(f"Platform '{platform_name}' is not configured. Set up credentials in ~/.marlow/config.yaml or environment variables.")

    from gateway.platforms.base import BasePlatformAdapter

    # Capture [[as_document]] directive before extract_media strips it.
    # Image-extension files in this batch will route through send_document
    # instead of send_photo so the original bytes survive (e.g. info-graph
    # JPGs where Telegram's sendPhoto recompresses to 1280px).
    force_document_attachments = "[[as_document]]" in message

    media_files, cleaned_message = BasePlatformAdapter.extract_media(message)
    media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)

    # Slack: resolve user IDs (U...) to DM channel IDs before route policy
    # locks the destination. This keeps interactive confirmation bound to the
    # actual conversation ID, not a model-facing user ID.
    if platform_name == "slack" and chat_id and chat_id.startswith("U"):
        try:
            import aiohttp
            async def _open_slack_dm(token, user_id):
                url = "https://slack.com/api/conversations.open"
                headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                    async with session.post(url, headers=headers, json={"users": [user_id]}) as resp:
                        data = await resp.json()
                        if data.get("ok"):
                            return data["channel"]["id"]
                        return None
            from model_tools import _run_async
            dm_channel = _run_async(_open_slack_dm(pconfig.token, chat_id))
            if dm_channel:
                chat_id = dm_channel
            else:
                return json.dumps({"error": f"Could not open DM with Slack user {chat_id}. Check bot permissions (im:write)."})
        except Exception as e:
            return json.dumps({"error": f"Failed to open Slack DM: {e}"})

    if turn is not None:
        from gateway.turn_context import ConversationRoute
        from gateway.outbound import OutboundPolicy, OutboundKind
        destination = ConversationRoute.from_parts(platform, chat_id, thread_id)
        decision = OutboundPolicy.evaluate(turn, OutboundKind.CROSS_CONVERSATION, destination)
        if not decision.allowed:
            if decision.reason == "same_origin_reply":
                return json.dumps({
                    "error": "same_origin_reply",
                    "message": "Do not use send_message to reply to the current conversation; place the content in the final response.",
                })
            return json.dumps({"success": False, "policy_denied": True, "reason": decision.reason})
        if decision.requires_confirmation:
            return _handle_interactive_cross_conversation_send(
                turn=turn,
                platform=platform,
                destination=destination,
                cleaned_message=cleaned_message,
                media_files=media_files,
                force_document=force_document_attachments,
            )

    duplicate_skip = _maybe_skip_cron_duplicate_send(platform_name, chat_id, thread_id)
    if duplicate_skip:
        return json.dumps(duplicate_skip)

    try:
        from model_tools import _run_async
        result = _run_async(
            _send_to_platform(
                platform,
                pconfig,
                chat_id,
                cleaned_message,
                thread_id=thread_id,
                media_files=media_files,
                force_document=force_document_attachments,
            )
        )
        if isinstance(result, dict) and "error" in result:
            result["error"] = _sanitize_error_text(result["error"])
        return json.dumps(result)
    except Exception as e:
        return json.dumps(_error(f"Send failed: {e}"))


def _parse_target_ref(platform_name: str, target_ref: str):
    """Parse a tool target into chat_id/thread_id and whether it is explicit."""
    if platform_name == "telegram":
        match = _TELEGRAM_TOPIC_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
    if platform_name == "feishu":
        match = _FEISHU_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
    if platform_name == "discord":
        match = _NUMERIC_TOPIC_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
    if platform_name == "slack":
        match = _SLACK_THREAD_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
        match = _SLACK_TARGET_RE.fullmatch(target_ref)
        if match:
            chat_id = match.group(1)
            # Slack user IDs (U...) and workspace IDs (W...) are NOT valid
            # explicit send targets — chat.postMessage rejects them. A DM
            # must be opened first via conversations.open to get a D...
            # conversation ID. Caller still gets the chat_id so the U→D
            # resolution path in send_message() can run.
            is_explicit = chat_id[0] not in {"U", "W"}
            return chat_id, None, is_explicit
    if platform_name == "email":
        match = _EMAIL_TARGET_RE.fullmatch(target_ref)
        if match:
            return target_ref.strip(), None, True
    if target_ref.lstrip("-").isdigit():
        return target_ref, None, True
    # XMPP JIDs (user@server or room@conference.server) are explicit
    if platform_name == "xmpp" and "@" in target_ref:
        return target_ref, None, True
    return None, None, False


def _describe_media_for_mirror(media_files):
    """Return a human-readable mirror summary when a message only contains media."""
    if not media_files:
        return ""
    if len(media_files) == 1:
        media_path, is_voice = media_files[0]
        ext = os.path.splitext(media_path)[1].lower()
        if is_voice and ext in _VOICE_EXTS:
            return "[Sent voice message]"
        if ext in _IMAGE_EXTS:
            return "[Sent image attachment]"
        if ext in _VIDEO_EXTS:
            return "[Sent video attachment]"
        if ext in _AUDIO_EXTS:
            return "[Sent audio attachment]"
        return "[Sent document attachment]"
    return f"[Sent {len(media_files)} media attachments]"


def _get_cron_auto_delivery_target():
    """Return the cron scheduler's auto-delivery target for the current run, if any."""
    from gateway.session_context import get_session_env
    platform = get_session_env("MARLOW_CRON_AUTO_DELIVER_PLATFORM", "").strip().lower()
    chat_id = get_session_env("MARLOW_CRON_AUTO_DELIVER_CHAT_ID", "").strip()
    if not platform or not chat_id:
        return None
    thread_id = get_session_env("MARLOW_CRON_AUTO_DELIVER_THREAD_ID", "").strip() or None
    return {
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }


def _handle_interactive_cross_conversation_send(
    *,
    turn,
    platform,
    destination,
    cleaned_message: str,
    media_files,
    force_document: bool = False,
) -> str:
    """Confirm and send an interactive cross-conversation payload once."""
    from gateway import outbound as outbound_mod, pending_delivery as pending_delivery_mod
    from gateway.outbound import OutboundEnvelope, OutboundKind
    from gateway.pending_delivery import PendingDelivery

    if turn.actor is None or not turn.session_key:
        return json.dumps({
            "success": False,
            "policy_denied": True,
            "reason": "missing_actor_or_session",
        })

    request_id = pending_delivery_mod.make_request_id()
    try:
        attachments = tuple(
            pending_delivery_mod.stage_attachment(path, request_id)
            for path, _is_voice in media_files or []
        )
    except Exception as exc:
        pending_delivery_mod.cleanup_delivery_files(request_id)
        return json.dumps(_error(f"Failed to stage delivery attachment: {exc}"))

    envelope = OutboundEnvelope(
        delivery_id=request_id,
        kind=OutboundKind.CROSS_CONVERSATION,
        destination=destination,
        text=cleaned_message,
        attachments=attachments,
        hints=turn.hints,
        turn_id=turn.turn_id,
        grant_id=request_id,
    )
    digest = pending_delivery_mod.payload_digest(cleaned_message, attachments)
    entry = PendingDelivery(
        request_id=request_id,
        turn_id=turn.turn_id,
        session_key=turn.session_key,
        origin=turn.origin,
        actor=turn.actor,
        destination=destination,
        payload_digest=digest,
        envelope=envelope,
        created_at=time.time(),
        expires_at=time.time() + pending_delivery_mod._DEFAULT_TIMEOUT_SECONDS,
    )

    confirmation_result = pending_delivery_mod.request_confirmation(entry)
    if not confirmation_result or not confirmation_result.get("success") or not confirmation_result.get("approved"):
        return json.dumps(confirmation_result or {
            "success": False,
            "cancelled": True,
            "delivery_id": request_id,
            "message": "Delivery confirmation was not approved.",
        })

    authorized_envelope = pending_delivery_mod.claim(request_id)
    if authorized_envelope is None:
        pending_delivery_mod.finish(
            request_id,
            state=pending_delivery_mod.PendingDeliveryState.FAILED,
            result={"success": False, "error": "delivery_grant_claim_failed"},
        )
        return json.dumps({"success": False, "error": "delivery_grant_claim_failed"})

    try:
        from model_tools import _run_async
        send_result = _run_async(
            outbound_mod.OutboundDeliveryService(force_document=force_document).send(
                turn,
                authorized_envelope,
            )
        )
    except Exception as exc:
        pending_delivery_mod.finish(
            request_id,
            state=pending_delivery_mod.PendingDeliveryState.FAILED,
            result={"success": False, "error": _sanitize_error_text(str(exc))},
        )
        return json.dumps(_error(f"Interactive delivery failed: {exc}"))

    if isinstance(send_result, dict) and send_result.get("success"):
        pending_delivery_mod.finish(
            request_id,
            state=pending_delivery_mod.PendingDeliveryState.SENT,
            result=send_result,
        )
        message_ids = tuple(
            str(mid)
            for mid in send_result.get("message_ids") or ([] if send_result.get("message_id") is None else [send_result["message_id"]])
        )
        delivery = outbound_mod.SuccessfulDelivery(
            delivery_id=request_id,
            destination=destination,
            text=cleaned_message,
            message_ids=message_ids,
        )
        record_result = outbound_mod.DeliveryRecorder().record_success(delivery)
        response = dict(send_result)
        response.update({
            "delivery_id": request_id,
            "target": destination.public_label(),
            "confirmed": True,
        })
        if record_result.mirrored:
            response["mirrored"] = True
        return json.dumps(response)

    pending_delivery_mod.finish(
        request_id,
        state=pending_delivery_mod.PendingDeliveryState.FAILED,
        result=send_result if isinstance(send_result, dict) else None,
    )
    if isinstance(send_result, dict) and "error" in send_result:
        send_result["error"] = _sanitize_error_text(send_result["error"])
    return json.dumps(send_result)


def _maybe_skip_cron_duplicate_send(platform_name: str, chat_id: str, thread_id: str | None):
    """Skip redundant cron send_message calls when the scheduler will auto-deliver there."""
    auto_target = _get_cron_auto_delivery_target()
    if not auto_target:
        return None

    same_target = (
        auto_target["platform"] == platform_name
        and str(auto_target["chat_id"]) == str(chat_id)
        and auto_target.get("thread_id") == thread_id
    )
    if not same_target:
        return None

    target_label = f"{platform_name}:{chat_id}"
    if thread_id is not None:
        target_label += f":{thread_id}"

    return {
        "success": True,
        "skipped": True,
        "reason": "cron_auto_delivery_duplicate_target",
        "target": target_label,
        "note": (
            f"Skipped send_message to {target_label}. This cron job will already auto-deliver "
            "its final response to that same target. Put the intended user-facing content in "
            "your final response instead, or use a different target if you want an additional message."
        ),
    }


async def _send_via_adapter(
    platform,
    pconfig,
    chat_id,
    chunk,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Send a message via a live gateway adapter, with a standalone fallback
    for out-of-process callers (e.g. cron running separately from the gateway).

    Order of attempts:
      1. Live in-process adapter via ``_gateway_runner_ref()`` (the path that
         existed before this change).
      2. The plugin's ``standalone_sender_fn`` registered on its
         ``PlatformEntry`` (used when the gateway is not in this process, so
         the runner weakref is ``None``).
      3. A descriptive error explaining both options.
    """
    runner = None
    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
    except Exception:
        runner = None

    if runner is not None:
        try:
            adapter = runner.adapters.get(platform)
        except Exception:
            adapter = None
        if adapter is not None:
            try:
                metadata = {"thread_id": thread_id} if thread_id else None
                result = await adapter.send(chat_id=chat_id, content=chunk, metadata=metadata)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return {"error": f"Plugin platform send failed: {e}"}
            if result.success:
                return {"success": True, "message_id": result.message_id}
            return {"error": f"Adapter send failed: {result.error}"}

    platform_name = platform.value if hasattr(platform, "value") else str(platform)
    entry = None
    try:
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get(platform_name)
    except Exception:
        entry = None

    if entry is not None and entry.standalone_sender_fn is not None:
        try:
            result = await entry.standalone_sender_fn(
                pconfig,
                chat_id,
                chunk,
                thread_id=thread_id,
                media_files=media_files,
                force_document=force_document,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Plugin standalone send for %s raised", platform_name, exc_info=True)
            return {"error": f"Plugin standalone send failed: {e}"}

        if isinstance(result, dict) and (result.get("success") or result.get("error")):
            return result
        return {
            "error": (
                f"Plugin standalone send for '{platform_name}' returned an "
                f"invalid result: expected a dict with 'success' or 'error' "
                f"keys, got {type(result).__name__}"
            )
        }

    return {
        "error": (
            f"No live adapter for platform '{platform_name}'. Is the gateway "
            f"running with this platform connected? For out-of-process delivery "
            f"(e.g. cron in a separate process), the platform plugin must "
            f"register a standalone_sender_fn on its PlatformEntry."
        )
    }


async def _send_to_platform(platform, pconfig, chat_id, message, thread_id=None, media_files=None, force_document=False):
    """Route a message to the appropriate platform sender.

    Long messages are automatically chunked to fit within platform limits
    using the same smart-splitting algorithm as the gateway adapters
    (preserves code-block boundaries, adds part indicators).
    """
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter, utf16_len
    from gateway.platforms.slack import SlackAdapter

    # Telegram adapter import is optional (requires python-telegram-bot)
    try:
        from gateway.platforms.telegram import TelegramAdapter
        _telegram_available = True
    except ImportError:
        _telegram_available = False

    # Feishu adapter import is optional (requires lark-oapi)
    try:
        from gateway.platforms.feishu import FeishuAdapter
        _feishu_available = True
    except ImportError:
        _feishu_available = False

    media_files = media_files or []

    if platform == Platform.SLACK and message:
        try:
            slack_adapter = SlackAdapter.__new__(SlackAdapter)
            message = slack_adapter.format_message(message)
        except Exception:
            logger.debug("Failed to apply Slack mrkdwn formatting in _send_to_platform", exc_info=True)

    # Platform message length limits (from adapter class attributes for
    # built-in platforms; from PlatformEntry.max_message_length for plugins).
    _MAX_LENGTHS = {
        Platform.TELEGRAM: TelegramAdapter.MAX_MESSAGE_LENGTH if _telegram_available else 4096,
        Platform.SLACK: SlackAdapter.MAX_MESSAGE_LENGTH,
    }
    if _feishu_available:
        _MAX_LENGTHS[Platform.FEISHU] = FeishuAdapter.MAX_MESSAGE_LENGTH

    # Check plugin registry for max_message_length
    if platform not in _MAX_LENGTHS:
        try:
            from gateway.platform_registry import platform_registry
            entry = platform_registry.get(platform.value)
            if entry and entry.max_message_length > 0:
                _MAX_LENGTHS[platform] = entry.max_message_length
        except Exception:
            pass

    # Smart-chunk the message to fit within platform limits.
    # For short messages or platforms without a known limit this is a no-op.
    # Telegram measures length in UTF-16 code units, not Unicode codepoints.
    max_len = _MAX_LENGTHS.get(platform)
    if max_len:
        _len_fn = utf16_len if platform == Platform.TELEGRAM else None
        chunks = BasePlatformAdapter.truncate_message(message, max_len, len_fn=_len_fn)
    else:
        chunks = [message]

    # --- Telegram: special handling for media attachments ---
    if platform == Platform.TELEGRAM:
        last_result = None
        disable_link_previews = bool(getattr(pconfig, "extra", {}) and pconfig.extra.get("disable_link_previews"))
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            result = await _send_telegram(
                pconfig.token,
                chat_id,
                chunk,
                media_files=media_files if is_last else [],
                thread_id=thread_id,
                disable_link_previews=disable_link_previews,
                force_document=force_document,
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- Discord: chunked delivery via the registry's standalone_sender_fn.
    # The plugin's ``_standalone_send`` (registered in
    # plugins/platforms/discord/adapter.py) handles forum channels, threads,
    # and multipart media uploads.  ``_send_via_adapter`` tries the live
    # in-process adapter first via ``adapter.send()``, but Discord's elif
    # historically went straight to the HTTP path; we preserve that by
    # explicitly invoking the registry hook here so behavior is unchanged.
    if platform == Platform.DISCORD:
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get("discord")
        if entry is None or entry.standalone_sender_fn is None:
            return {"error": "Discord plugin not registered or missing standalone_sender_fn"}
        last_result = None
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            result = await entry.standalone_sender_fn(
                pconfig,
                chat_id,
                chunk,
                thread_id=thread_id,
                media_files=media_files if is_last else [],
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- Feishu: native media attachment support via adapter ---
    if platform == Platform.FEISHU and media_files:
        last_result = None
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            result = await _send_feishu(
                pconfig,
                chat_id,
                chunk,
                media_files=media_files if is_last else None,
                thread_id=thread_id,
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- Non-media platforms ---
    if media_files and not message.strip():
        return {
            "error": (
                f"send_message MEDIA delivery is currently only supported for telegram, discord, and feishu; "
                f"target {platform.value} had only media attachments"
            )
        }
    warning = None
    if media_files:
        warning = (
            f"MEDIA attachments were omitted for {platform.value}; "
            "native send_message media delivery is currently only supported for telegram, discord, and feishu"
        )

    last_result = None
    for chunk in chunks:
        if platform == Platform.SLACK:
            result = await _send_slack(pconfig.token, chat_id, chunk)
        elif platform == Platform.EMAIL:
            result = await _send_email(pconfig.extra, chat_id, chunk)
        elif platform == Platform.FEISHU:
            result = await _send_feishu(pconfig, chat_id, chunk, thread_id=thread_id)
        else:
            # Plugin platform: route through the gateway's live adapter if
            # available, otherwise the plugin's standalone_sender_fn.
            result = await _send_via_adapter(
                platform,
                pconfig,
                chat_id,
                chunk,
                thread_id=thread_id,
                media_files=media_files,
                force_document=force_document,
            )

        if isinstance(result, dict) and result.get("error"):
            return result
        last_result = result

    if warning and isinstance(last_result, dict) and last_result.get("success"):
        warnings = list(last_result.get("warnings", []))
        warnings.append(warning)
        last_result["warnings"] = warnings
    return last_result


def _is_telegram_thread_not_found(error: Exception) -> bool:
    """Check if a Telegram error is a thread-not-found failure.

    Matches the gateway adapter's ``_is_thread_not_found_error`` for
    the standalone ``_send_telegram`` path (issue #27012).
    """
    return "thread not found" in str(error).lower()


async def _send_telegram(token, chat_id, message, media_files=None, thread_id=None, disable_link_previews=False, force_document=False):
    """Send via Telegram Bot API (one-shot, no polling needed).

    Applies markdown→MarkdownV2 formatting (same as the gateway adapter)
    so that bold, links, and headers render correctly.  If the message
    already contains HTML tags, it is sent with ``parse_mode='HTML'``
    instead, bypassing MarkdownV2 conversion.
    """
    try:
        from telegram import Bot
        from telegram.constants import ParseMode

        # Auto-detect HTML tags — if present, skip MarkdownV2 and send as HTML.
        # Inspired by github.com/ashaney — PR #1568.
        _has_html = bool(re.search(r'<[a-zA-Z/][^>]*>', message))

        if _has_html:
            formatted = message
            send_parse_mode = ParseMode.HTML
        else:
            # Reuse the gateway adapter's format_message for markdown→MarkdownV2
            try:
                from gateway.platforms.telegram import TelegramAdapter
                _adapter = TelegramAdapter.__new__(TelegramAdapter)
                formatted = _adapter.format_message(message)
            except Exception:
                # Fallback: send as-is if formatting unavailable
                formatted = message
            send_parse_mode = ParseMode.MARKDOWN_V2

        # Honour a configured proxy (telegram.proxy_url in config.yaml, exported
        # as TELEGRAM_PROXY env var by load_gateway_config). Without this, the
        # standalone send path bypasses the proxy and times out in regions
        # where api.telegram.org is blocked. The in-gateway adapter does the
        # same thing in gateway/platforms/telegram.py.
        try:
            from gateway.platforms.base import resolve_proxy_url
            _tg_proxy = resolve_proxy_url("TELEGRAM_PROXY", target_hosts=["api.telegram.org"])
        except Exception:
            _tg_proxy = None
        if _tg_proxy:
            try:
                from telegram.request import HTTPXRequest
                logger.info("send_message: standalone Telegram send routed through proxy %s", _tg_proxy)
                bot = Bot(
                    token=token,
                    request=HTTPXRequest(proxy=_tg_proxy),
                    get_updates_request=HTTPXRequest(proxy=_tg_proxy),
                )
            except Exception as _proxy_err:
                logger.warning("send_message: failed to attach Telegram proxy (%s), falling back to direct connection", _proxy_err)
                bot = Bot(token=token)
        else:
            bot = Bot(token=token)
        int_chat_id = int(chat_id)
        media_files = media_files or []
        thread_kwargs = {}
        if thread_id is not None:
            # Reuse the gateway adapter's General-topic mapping: in Telegram
            # forum supergroups, the General topic is addressed as
            # message_thread_id="1" on incoming updates, but Bot API
            # sendMessage rejects message_thread_id=1 with "Message thread
            # not found". The adapter's helper maps "1" to None for that
            # reason; the send_message tool needs the same mapping or a
            # send to a forum group's General topic always errors out
            # (see issue #22267).
            try:
                from gateway.platforms.telegram import TelegramAdapter
                effective_thread_id = TelegramAdapter._message_thread_id_for_send(
                    str(thread_id)
                )
            except Exception:
                # Fallback: explicit mapping in case the adapter import
                # fails (e.g. python-telegram-bot missing in this venv).
                effective_thread_id = (
                    None if str(thread_id) == "1" else int(thread_id)
                )
            if effective_thread_id is not None:
                thread_kwargs["message_thread_id"] = effective_thread_id
        # disable_web_page_preview is only valid for send_message, not
        # send_photo/send_video/etc.  Keep it separate so media sends
        # don't inherit an invalid parameter (issue #27012).
        text_kwargs = dict(thread_kwargs)
        if disable_link_previews:
            text_kwargs["disable_web_page_preview"] = True

        last_msg = None
        warnings = []

        if formatted.strip():
            try:
                last_msg = await _send_telegram_message_with_retry(
                    bot,
                    chat_id=int_chat_id, text=formatted,
                    parse_mode=send_parse_mode, **text_kwargs
                )
            except Exception as md_error:
                # Thread not found — retry without message_thread_id so the
                # message still delivers (matching the gateway adapter's
                # fallback behaviour, issue #27012).
                if _is_telegram_thread_not_found(md_error) and thread_kwargs:
                    logger.warning(
                        "Thread %s not found in _send_telegram, retrying without message_thread_id",
                        thread_kwargs.get("message_thread_id"),
                    )
                    text_kwargs.pop("message_thread_id", None)
                    last_msg = await _send_telegram_message_with_retry(
                        bot,
                        chat_id=int_chat_id, text=formatted,
                        parse_mode=send_parse_mode, **text_kwargs
                    )
                elif "parse" in str(md_error).lower() or "markdown" in str(md_error).lower() or "html" in str(md_error).lower():
                    logger.warning(
                        "Parse mode %s failed in _send_telegram, falling back to plain text: %s",
                        send_parse_mode,
                        _sanitize_error_text(md_error),
                    )
                    if not _has_html:
                        try:
                            from gateway.platforms.telegram import _strip_mdv2
                            plain = _strip_mdv2(formatted)
                        except Exception:
                            plain = message
                    else:
                        plain = message
                    last_msg = await _send_telegram_message_with_retry(
                        bot,
                        chat_id=int_chat_id, text=plain,
                        parse_mode=None, **text_kwargs
                    )
                else:
                    raise

        for media_path, is_voice in media_files:
            if not os.path.exists(media_path):
                warning = f"Media file not found, skipping: {media_path}"
                logger.warning(warning)
                warnings.append(warning)
                continue

            ext = os.path.splitext(media_path)[1].lower()
            try:
                with open(media_path, "rb") as f:
                    media_kwargs = dict(thread_kwargs)
                    try:
                        if ext in _IMAGE_EXTS and not force_document:
                            last_msg = await bot.send_photo(
                                chat_id=int_chat_id, photo=f, **media_kwargs
                            )
                        elif ext in _VIDEO_EXTS:
                            last_msg = await bot.send_video(
                                chat_id=int_chat_id, video=f, **media_kwargs
                            )
                        elif ext in _VOICE_EXTS and is_voice:
                            last_msg = await bot.send_voice(
                                chat_id=int_chat_id, voice=f, **media_kwargs
                            )
                        elif ext in _TELEGRAM_SEND_AUDIO_EXTS:
                            last_msg = await bot.send_audio(
                                chat_id=int_chat_id, audio=f, **media_kwargs
                            )
                        else:
                            last_msg = await bot.send_document(
                                chat_id=int_chat_id, document=f, **media_kwargs
                            )
                    except Exception as media_err:
                        if _is_telegram_thread_not_found(media_err) and media_kwargs.get("message_thread_id"):
                            # Thread not found for media — retry without
                            # message_thread_id (issue #27012).
                            logger.warning(
                                "Thread %s not found for media send, retrying without message_thread_id",
                                media_kwargs["message_thread_id"],
                            )
                            # Re-seek the file since the first attempt consumed it
                            f.seek(0)
                            media_kwargs.pop("message_thread_id", None)
                            if ext in _IMAGE_EXTS and not force_document:
                                last_msg = await bot.send_photo(
                                    chat_id=int_chat_id, photo=f, **media_kwargs
                                )
                            elif ext in _VIDEO_EXTS:
                                last_msg = await bot.send_video(
                                    chat_id=int_chat_id, video=f, **media_kwargs
                                )
                            elif ext in _VOICE_EXTS and is_voice:
                                last_msg = await bot.send_voice(
                                    chat_id=int_chat_id, voice=f, **media_kwargs
                                )
                            elif ext in _TELEGRAM_SEND_AUDIO_EXTS:
                                last_msg = await bot.send_audio(
                                    chat_id=int_chat_id, audio=f, **media_kwargs
                                )
                            else:
                                last_msg = await bot.send_document(
                                    chat_id=int_chat_id, document=f, **media_kwargs
                                )
                        else:
                            raise
            except Exception as e:
                warning = _sanitize_error_text(f"Failed to send media {media_path}: {e}")
                logger.error(warning)
                warnings.append(warning)

        if last_msg is None:
            error = "No deliverable text or media remained after processing MEDIA tags"
            if warnings:
                return {"error": error, "warnings": warnings}
            return {"error": error}

        result = {
            "success": True,
            "platform": "telegram",
            "chat_id": chat_id,
            "message_id": str(last_msg.message_id),
        }
        if warnings:
            result["warnings"] = warnings
        return result
    except ImportError:
        return {"error": "python-telegram-bot not installed. Run: pip install python-telegram-bot"}
    except Exception as e:
        return _error(f"Telegram send failed: {e}")


async def _send_slack(token, chat_id, message):
    """Send via Slack Web API."""
    try:
        import aiohttp
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}
    try:
        from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
        _proxy = resolve_proxy_url()
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
        url = "https://slack.com/api/chat.postMessage"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **_sess_kw) as session:
            payload = {"channel": chat_id, "text": message, "mrkdwn": True}
            async with session.post(url, headers=headers, json=payload, **_req_kw) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return {"success": True, "platform": "slack", "chat_id": chat_id, "message_id": data.get("ts")}
                return _error(f"Slack API error: {data.get('error', 'unknown')}")
    except Exception as e:
        return _error(f"Slack send failed: {e}")






async def _send_email(extra, chat_id, message):
    """Send via SMTP (one-shot, no persistent connection needed)."""
    import smtplib
    from email.mime.text import MIMEText

    address = extra.get("address") or os.getenv("EMAIL_ADDRESS", "")
    password = os.getenv("EMAIL_PASSWORD", "")
    smtp_host = extra.get("smtp_host") or os.getenv("EMAIL_SMTP_HOST", "")
    try:
        smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587"))
    except (ValueError, TypeError):
        smtp_port = 587

    if not all([address, password, smtp_host]):
        return {"error": "Email not configured (EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_SMTP_HOST required)"}

    try:
        msg = MIMEText(message, "plain", "utf-8")
        msg["From"] = address
        msg["To"] = chat_id
        msg["Subject"] = "Marlow Agent"
        msg["Date"] = formatdate(localtime=True)

        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls(context=ssl.create_default_context())
        server.login(address, password)
        server.send_message(msg)
        server.quit()
        return {"success": True, "platform": "email", "chat_id": chat_id}
    except Exception as e:
        return _error(f"Email send failed: {e}")


















async def _send_feishu(pconfig, chat_id, message, media_files=None, thread_id=None):
    """Send via Feishu/Lark using the adapter's send pipeline."""
    try:
        from gateway.platforms.feishu import FeishuAdapter, FEISHU_AVAILABLE
        if not FEISHU_AVAILABLE:
            return {"error": "Feishu dependencies not installed. Run: pip install 'marlow-agent[feishu]'"}
        from gateway.platforms.feishu import FEISHU_DOMAIN, LARK_DOMAIN
    except ImportError:
        return {"error": "Feishu dependencies not installed. Run: pip install 'marlow-agent[feishu]'"}

    media_files = media_files or []

    try:
        adapter = FeishuAdapter(pconfig)
        domain_name = getattr(adapter, "_domain_name", "feishu")
        domain = FEISHU_DOMAIN if domain_name != "lark" else LARK_DOMAIN
        adapter._client = adapter._build_lark_client(domain)
        metadata = {"thread_id": thread_id} if thread_id else None

        last_result = None
        if message.strip():
            last_result = await adapter.send(chat_id, message, metadata=metadata)
            if not last_result.success:
                return _error(f"Feishu send failed: {last_result.error}")

        for media_path, is_voice in media_files:
            if not os.path.exists(media_path):
                return _error(f"Media file not found: {media_path}")

            ext = os.path.splitext(media_path)[1].lower()
            if ext in _IMAGE_EXTS:
                last_result = await adapter.send_image_file(chat_id, media_path, metadata=metadata)
            elif ext in _VIDEO_EXTS:
                last_result = await adapter.send_video(chat_id, media_path, metadata=metadata)
            elif ext in _VOICE_EXTS and is_voice:
                last_result = await adapter.send_voice(chat_id, media_path, metadata=metadata)
            elif ext in _AUDIO_EXTS:
                last_result = await adapter.send_voice(chat_id, media_path, metadata=metadata)
            else:
                last_result = await adapter.send_document(chat_id, media_path, metadata=metadata)

            if not last_result.success:
                return _error(f"Feishu media send failed: {last_result.error}")

        if last_result is None:
            return {"error": "No deliverable text or media remained after processing MEDIA tags"}

        return {
            "success": True,
            "platform": "feishu",
            "chat_id": chat_id,
            "message_id": last_result.message_id,
        }
    except Exception as e:
        return _error(f"Feishu send failed: {e}")


def _check_send_message():
    """Gate send_message on gateway running (always available on messaging platforms)."""
    from gateway.session_context import get_session_env
    platform = get_session_env("MARLOW_SESSION_PLATFORM", "")
    if platform and platform != "local":
        return True
    try:
        from gateway.status import is_gateway_running
        return is_gateway_running()
    except Exception:
        return False






# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="send_message",
    toolset="messaging",
    schema=SEND_MESSAGE_SCHEMA,
    handler=send_message_tool,
    check_fn=_check_send_message,
    emoji="📨",
)

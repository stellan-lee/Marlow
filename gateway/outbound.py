"""Outbound routing policy and live-adapter delivery primitives."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional

from .turn_context import ConversationRoute, ExecutionMode, TurnContext


class OutboundKind(str, Enum):
    REPLY = "reply"
    INTERIM = "interim"
    STATUS = "status"
    CLARIFY = "clarify"
    CONFIRMATION = "confirmation"
    ERROR = "error"
    COMPLETION = "completion"
    APPROVAL = "approval"
    CROSS_CONVERSATION = "cross_conversation"
    SCHEDULED = "scheduled"
    SYSTEM_NOTICE = "system_notice"


_ORIGIN_ONLY_KINDS = frozenset({
    OutboundKind.REPLY,
    OutboundKind.INTERIM,
    OutboundKind.STATUS,
    OutboundKind.CLARIFY,
    OutboundKind.CONFIRMATION,
    OutboundKind.ERROR,
    OutboundKind.COMPLETION,
})

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".3gp"}
_VOICE_EXTS = {".ogg", ".opus"}
_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac"}


@dataclass(frozen=True, slots=True)
class StagedAttachment:
    path: str
    name: str
    size: int
    digest: str
    media_type: Optional[str] = None


@dataclass(frozen=True, slots=True)
class OutboundEnvelope:
    delivery_id: str
    kind: OutboundKind
    destination: ConversationRoute
    text: str
    attachments: tuple[StagedAttachment, ...] = field(default_factory=tuple)
    hints: object | None = None
    turn_id: Optional[str] = None
    grant_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class SuccessfulDelivery:
    """Authorized adapter-confirmed outbound delivery."""

    delivery_id: str
    destination: ConversationRoute
    text: str
    message_ids: tuple[str, ...]
    completed_at: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason: str
    requires_confirmation: bool = False


@dataclass(frozen=True, slots=True)
class RecordResult:
    delivery_id: str
    mirrored: bool
    skipped_reason: Optional[str] = None


class OutboundPolicy:
    """Pure policy checks for outbound delivery decisions."""

    @staticmethod
    def evaluate(turn: TurnContext, kind: OutboundKind, destination: ConversationRoute) -> PolicyDecision:
        if kind in _ORIGIN_ONLY_KINDS:
            if not turn.is_interactive():
                return PolicyDecision(True, "non_interactive_origin_only")
            origin = turn.origin
            if origin is None:
                return PolicyDecision(False, "missing_origin")
            if destination != origin:
                return PolicyDecision(False, "origin_only_destination_mismatch")
            return PolicyDecision(True, "origin")

        if turn.is_interactive() and kind == OutboundKind.CROSS_CONVERSATION:
            if turn.origin is None:
                return PolicyDecision(False, "missing_origin")
            if not destination.is_concrete():
                return PolicyDecision(False, "ambiguous_destination")
            if destination == turn.origin:
                return PolicyDecision(False, "same_origin_reply")
            return PolicyDecision(
                True,
                "cross_conversation_requires_grant",
                requires_confirmation=True,
            )

        if turn.mode == ExecutionMode.SCHEDULED and kind == OutboundKind.SCHEDULED:
            return PolicyDecision(True, "scheduled")
        if turn.mode == ExecutionMode.SYSTEM and kind == OutboundKind.SYSTEM_NOTICE:
            return PolicyDecision(True, "system")
        if turn.mode == ExecutionMode.LOCAL:
            return PolicyDecision(False, "local_mode_denied")

        return PolicyDecision(False, "unclassified")

    @staticmethod
    def origin_only_kinds() -> Iterable[OutboundKind]:
        return iter(_ORIGIN_ONLY_KINDS)


class OutboundDeliveryService:
    """Send an already-authorized outbound envelope through the live gateway."""

    def __init__(self, runner: Any = None, *, force_document: bool = False):
        self.runner = runner
        self.force_document = force_document

    async def send(self, turn: TurnContext, envelope: OutboundEnvelope) -> dict:
        decision = OutboundPolicy.evaluate(turn, envelope.kind, envelope.destination)
        if not decision.allowed:
            return {
                "success": False,
                "policy_denied": True,
                "reason": decision.reason,
            }
        if turn.is_interactive() and envelope.kind == OutboundKind.CROSS_CONVERSATION:
            if envelope.grant_id != envelope.delivery_id:
                return {
                    "success": False,
                    "policy_denied": True,
                    "reason": "missing_delivery_grant",
                }
            if envelope.destination == turn.origin:
                return {
                    "success": False,
                    "policy_denied": True,
                    "reason": "same_origin_reply",
                }
            return await self._send_live(envelope)
        return {
            "success": False,
            "policy_denied": True,
            "reason": "non_interactive_delivery_not_owned_by_service",
        }

    async def _send_live(self, envelope: OutboundEnvelope) -> dict:
        runner = self.runner
        if runner is None:
            try:
                from gateway.run import _gateway_runner_ref

                runner = _gateway_runner_ref()
            except Exception:
                runner = None
        adapter = None
        if runner is not None:
            try:
                adapter = runner.adapters.get(envelope.destination.platform)
            except Exception:
                adapter = None
        if adapter is None:
            return {
                "success": False,
                "policy_denied": True,
                "reason": "live_adapter_unavailable",
                "message": (
                    f"Interactive delivery requires the connected live adapter "
                    f"for {envelope.destination.platform.value}."
                ),
            }
        return await _send_envelope_with_adapter(adapter, envelope, force_document=self.force_document)


class DeliveryRecorder:
    """Record only adapter-confirmed authorized deliveries."""

    def record_success(self, delivery: SuccessfulDelivery) -> RecordResult:
        mirrored = False
        try:
            from gateway.mirror import mirror_to_session

            mirrored = bool(
                delivery.text
                and mirror_to_session(
                    delivery.destination.platform.value,
                    delivery.destination.chat_id,
                    delivery.text,
                    source_label="gateway",
                    thread_id=delivery.destination.thread_id,
                )
            )
        except Exception:
            mirrored = False
        return RecordResult(
            delivery_id=delivery.delivery_id,
            mirrored=mirrored,
        )


async def _send_envelope_with_adapter(
    adapter: Any,
    envelope: OutboundEnvelope,
    *,
    force_document: bool = False,
) -> dict:
    if not envelope.text and not envelope.attachments:
        return {
            "success": False,
            "error": "Live adapter delivery requires text or media.",
        }
    message_ids: list[str] = []
    metadata = _metadata_for_destination(envelope.destination)

    for chunk in _chunk_text(adapter, envelope.text, envelope.destination.platform):
        result = _to_send_result(
            await _run_adapter_send(adapter, envelope.destination.chat_id, chunk, metadata)
        )
        if not result.success:
            return _adapter_failure(envelope.destination.platform, result.error)
        if result.message_id:
            message_ids.append(str(result.message_id))
        message_ids.extend(str(mid) for mid in getattr(result, "continuation_message_ids", ()) or ())

    for attachment in envelope.attachments:
        method = _attachment_method(adapter, attachment, force_document=force_document)
        kwargs: dict[str, Any] = {
            "chat_id": envelope.destination.chat_id,
            "metadata": metadata,
        }
        if method.__name__ == "send_document":
            kwargs.update({
                "file_path": attachment.path,
                "caption": None,
                "file_name": attachment.name,
            })
        elif method.__name__ == "send_voice":
            kwargs.update({
                "audio_path": attachment.path,
                "caption": None,
            })
        elif method.__name__ == "send_video":
            kwargs.update({
                "video_path": attachment.path,
                "caption": None,
            })
        elif method.__name__ == "send_image_file":
            kwargs.update({
                "image_path": attachment.path,
                "caption": None,
            })
        else:
            kwargs["content"] = _attachment_fallback_content(attachment)
        result = _to_send_result(await method(**kwargs))
        if not result.success:
            return _adapter_failure(envelope.destination.platform, result.error)
        if result.message_id:
            message_ids.append(str(result.message_id))

    platform = envelope.destination.platform
    return {
        "success": True,
        "platform": platform.value,
        "chat_id": envelope.destination.chat_id,
        "message_id": message_ids[-1] if message_ids else None,
        "message_ids": message_ids,
    }


def _to_send_result(result: Any) -> Any:
    if hasattr(result, "success"):
        return result
    if isinstance(result, dict):
        class _DictSendResult:
            success = bool(result.get("success"))
            message_id = result.get("message_id")
            error = result.get("error")
            raw_response = result
            retryable = False
            continuation_message_ids = result.get("continuation_message_ids", ())

        return _DictSendResult()
    class _FailedSendResult:
        success = False
        message_id = None
        error = f"Adapter returned {type(result).__name__}, expected SendResult or dict"
        raw_response = result
        retryable = False
        continuation_message_ids = ()

    return _FailedSendResult()


async def _run_adapter_send(adapter: Any, chat_id: str, chunk: str, metadata: Optional[dict[str, Any]]) -> Any:
    try:
        return await adapter.send(chat_id=chat_id, content=chunk, metadata=metadata)
    except TypeError:
        return await adapter.send(chat_id, chunk, metadata=metadata)


def _chunk_text(adapter: Any, text: str, platform) -> list[str]:
    if not text:
        return []
    try:
        from gateway.platforms.base import BasePlatformAdapter, utf16_len

        max_len = getattr(type(adapter), "MAX_MESSAGE_LENGTH", 0) or 0
        if not max_len:
            return [text]
        len_fn = utf16_len if platform.value == "telegram" else None
        return BasePlatformAdapter.truncate_message(text, max_len, len_fn=len_fn)
    except Exception:
        return [text]


def _metadata_for_destination(destination: ConversationRoute) -> Optional[dict[str, Any]]:
    if not destination.thread_id:
        return None
    metadata: dict[str, Any] = {"thread_id": destination.thread_id}
    if destination.platform.value == "telegram":
        try:
            if int(destination.chat_id) > 0:
                metadata.update({
                    "telegram_dm_topic_reply_fallback": True,
                    "direct_messages_topic_id": destination.thread_id,
                })
        except (TypeError, ValueError):
            pass
    return metadata


def _attachment_method(adapter: Any, attachment: StagedAttachment, *, force_document: bool = False):
    ext = os.path.splitext(attachment.path)[1].lower()
    if force_document:
        return adapter.send_document if hasattr(adapter, "send_document") else adapter.send
    if ext in _IMAGE_EXTS:
        return adapter.send_image_file if hasattr(adapter, "send_image_file") else adapter.send_document if hasattr(adapter, "send_document") else adapter.send
    if ext in _VIDEO_EXTS:
        return adapter.send_video if hasattr(adapter, "send_video") else adapter.send_document if hasattr(adapter, "send_document") else adapter.send
    if ext in _VOICE_EXTS:
        return adapter.send_voice if hasattr(adapter, "send_voice") else adapter.send_document if hasattr(adapter, "send_document") else adapter.send
    if ext in _AUDIO_EXTS:
        return adapter.send_document if hasattr(adapter, "send_document") else adapter.send
    return adapter.send_document if hasattr(adapter, "send_document") else adapter.send



def _attachment_fallback_content(attachment: StagedAttachment) -> str:
    ext = os.path.splitext(attachment.path)[1].lower()
    if ext in _IMAGE_EXTS:
        label = "Image"
    elif ext in _VIDEO_EXTS:
        label = "Video"
    elif ext in _VOICE_EXTS or ext in _AUDIO_EXTS:
        label = "Audio"
    else:
        label = "File"
    return f"📎 {label}: {attachment.path}"

def _adapter_failure(platform, error: Optional[str]) -> dict:
    return {
        "success": False,
        "error": f"Live {platform.value} adapter delivery failed: {error or 'unknown'}",
    }

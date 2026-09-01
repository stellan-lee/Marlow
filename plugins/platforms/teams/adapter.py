from __future__ import annotations

"""
Microsoft Teams platform adapter.

This plugin implements the documented Milestone 1 Teams gateway:
authenticated Bot Framework ingress, single-tenant validation, durable
duplicate receipts, bounded async handoff, text/media delivery, health, and
request-bound Adaptive Card approval callbacks.
"""

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import mimetypes
import os
import re
import secrets
import sys
import time
import uuid
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import socket
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlparse

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    ExternalActor,
    ExternalActorKind,
    ExternalAttachmentDescriptor,
    ExternalConversationMessage,
    ExternalConversationSnapshot,
    ExternalHistoryMode,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    cache_image_from_bytes,
    cache_media_bytes,
    is_network_accessible,
    render_external_conversation_snapshot,
)
from marlow_constants import get_marlow_home
from tools.url_safety import is_safe_url
from utils import atomic_json_write, interprocess_file_lock

logger = logging.getLogger(__name__)

TEAMS_PLATFORM = "teams"
TEAMS_CHANNEL_ID = "msteams"
SUPPORTED_CONVERSATION_TYPES = {"personal", "groupChat", "channel"}
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3978
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
DEFAULT_READ_TIMEOUT_SECONDS = 5.0
DEFAULT_AUTH_TIMEOUT_SECONDS = 10.0
DEFAULT_DISPATCH_MAX_ACTIVE = 16
DEFAULT_DISPATCH_MAX_PENDING = 128
DEFAULT_RECEIPT_TTL_DAYS = 7
DEFAULT_TEXT_BUDGET_BYTES = 64 * 1024
DEFAULT_TEXT_CHUNK_SECONDS = 30.0
DEFAULT_TYPING_INTERVAL_SECONDS = 2.0
DEFAULT_OUTBOUND_MAX_ATTEMPTS = 3
DEFAULT_OUTBOUND_BASE_DELAY = 2.0
DEFAULT_ATTACHMENT_TIMEOUT_SECONDS = 30.0
DEFAULT_ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0
SUPPORTED_IMAGE_MIME_PREFIXES = ("image/png", "image/jpeg", "image/gif")
SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif"}
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_REDACTION = "[REDACTED]"
_LOCK_SCOPE = "teams-client"
_RECEIPT_VERSION = 1

TEAMS_THREAD_CONTEXT_RE = re.compile(r"(?:^|;)messageid=([^;]+)(?=;|$)")
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_REPLY_TOP = 50
GRAPH_CONTEXT_TIMEOUT_SECONDS = 30.0
GRAPH_RETRY_AFTER_DEFAULT_SECONDS = 1.0
GRAPH_CONTEXT_RETRY_ATTEMPTS = 2
GRAPH_CONTEXT_RETRY_DELAY_SECONDS = 1.0


class TeamsThreadContextError(Exception):
    user_facing_message: str

    def __init__(self, message: str, *, user_facing_message: str) -> None:
        super().__init__(message)
        self.user_facing_message = user_facing_message


@dataclass(frozen=True, slots=True)
class TeamsThreadLocator:
    tenant_id: str
    team_aad_group_id: str
    channel_id: str
    root_message_id: str

try:  # pragma: no cover - exercised by dependency install in live environments.
    from microsoft_teams.api.activities.message.message import MessageActivityInput
    from microsoft_teams.api.activities.typing import TypingActivityInput
    from microsoft_teams.api.auth.credentials import ClientCredentials
    from microsoft_teams.api.models.account import Account, ConversationAccount
    from microsoft_teams.api.models.activity import Activity
    from microsoft_teams.api.models.attachment.attachment import Attachment
    from microsoft_teams.api.models.attachment.card_attachment import AdaptiveCardAttachment
    from microsoft_teams.api.models.conversation.conversation_reference import ConversationReference
    from microsoft_teams.api.models.entity.mention_entity import MentionEntity
    from microsoft_teams.api.models.entity.message_entity import MessageEntity
    from microsoft_teams.api.models.invoke_response import InvokeResponse
    from microsoft_teams.api.models.adaptive_card import (
        AdaptiveCardActionErrorResponse,
        AdaptiveCardActionMessageResponse,
        AdaptiveCardInvokeAction,
        AdaptiveCardInvokeValue,
    )
    from microsoft_teams.apps import App
    from microsoft_teams.apps.http.adapter import HttpRequest, HttpResponse, HttpServerAdapter
    from microsoft_teams.apps.http.http_server import HttpServer
    from microsoft_teams.common import Client, ClientOptions
    from microsoft_teams.cards import AdaptiveCard, SubmitAction, TextBlock

    TEAMS_AVAILABLE = True
except ImportError:  # pragma: no cover - import failure is validated by tests.
    TEAMS_AVAILABLE = False
    MessageActivityInput = None  # type: ignore[assignment]
    TypingActivityInput = None  # type: ignore[assignment]
    ClientCredentials = None  # type: ignore[assignment]
    Account = None  # type: ignore[assignment]
    ConversationAccount = None  # type: ignore[assignment]
    Activity = None  # type: ignore[assignment]
    Attachment = None  # type: ignore[assignment]
    AdaptiveCardAttachment = None  # type: ignore[assignment]
    ConversationReference = None  # type: ignore[assignment]
    MentionEntity = None  # type: ignore[assignment]
    MessageEntity = None  # type: ignore[assignment]
    InvokeResponse = None  # type: ignore[assignment]
    AdaptiveCardActionErrorResponse = None  # type: ignore[assignment]
    AdaptiveCardActionMessageResponse = None  # type: ignore[assignment]
    AdaptiveCardInvokeAction = None  # type: ignore[assignment]
    AdaptiveCardInvokeValue = None  # type: ignore[assignment]
    App = None  # type: ignore[assignment]
    _SdkMessageActivityInput = None  # type: ignore[assignment]
    HttpRequest = None  # type: ignore[assignment]
    HttpResponse = None  # type: ignore[assignment]
    HttpServerAdapter = None  # type: ignore[assignment]
    HttpServer = None  # type: ignore[assignment]
    Client = None  # type: ignore[assignment]
    ClientOptions = None  # type: ignore[assignment]
    AdaptiveCard = None  # type: ignore[assignment]
    SubmitAction = None  # type: ignore[assignment]
    TextBlock = None  # type: ignore[assignment]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalize_uuid(value: Any) -> str:
    if value is None:
        return ""
    raw = str(value).strip().lower()
    try:
        return str(uuid.UUID(raw))
    except (TypeError, ValueError):
        return raw


def _is_valid_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def _safe_text(value: Any) -> str:
    return re.sub(r"[\x00-\x1f\x7f\x85\u2028\u2029]", " ", str(value or "")).strip()


def _redact(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return _REDACTION
    return f"{value[:4]}…{value[-4:]}"


def _activity_correlation(activity: Any) -> str:
    raw = str(getattr(activity, "id", "") or "").strip()
    if not raw:
        return "unknown"
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _canonical_tuple(parts: Iterable[Any]) -> bytes:
    chunks: List[bytes] = []
    for part in parts:
        raw = str(part or "").encode("utf-8", errors="surrogatepass")
        chunks.append(str(len(raw)).encode("ascii") + b":" + raw)
    return b"\n".join(chunks)


def _hash_json(data: Any) -> str:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _activity_payload_for_hash(activity: Any) -> Dict[str, Any]:
    return {
        "id": getattr(activity, "id", None),
        "type": getattr(activity, "type", None),
        "channelId": getattr(activity, "channel_id", None),
        "serviceUrl": getattr(activity, "service_url", None),
        "from": _account_dict(getattr(activity, "from_", None)),
        "conversation": _conversation_dict(getattr(activity, "conversation", None)),
        "recipient": _account_dict(getattr(activity, "recipient", None)),
        "tenant": _tenant_dict(getattr(activity, "tenant", None)),
        "text": getattr(activity, "text", None),
        "attachments": _attachment_dict_list(getattr(activity, "attachments", None)),
        "channelData": _extract_channel_data(activity),
        "entities": _entity_dict_list(getattr(activity, "entities", None)),
    }


def _account_dict(account: Any) -> Dict[str, Any]:
    if account is None:
        return {}
    return {
        "id": getattr(account, "id", None),
        "aadObjectId": getattr(account, "aad_object_id", None),
        "type": getattr(account, "type", None),
    }


def _conversation_dict(conv: Any) -> Dict[str, Any]:
    if conv is None:
        return {}
    return {
        "id": getattr(conv, "id", None),
        "tenantId": getattr(conv, "tenant_id", None),
        "conversationType": getattr(conv, "conversation_type", None),
    }


def _tenant_dict(tenant: Any) -> Dict[str, Any]:
    if tenant is None:
        return {}
    if isinstance(tenant, dict):
        return dict(tenant)
    try:
        return tenant.model_dump(mode="json", exclude_none=True)
    except Exception:
        return {"id": getattr(tenant, "id", None)}


def _attachment_dict_list(attachments: Any) -> List[Dict[str, Any]]:
    if not attachments:
        return []
    result: List[Dict[str, Any]] = []
    for attachment in attachments:
        result.append(
            {
                "contentType": getattr(attachment, "content_type", None),
                "contentUrl": getattr(attachment, "content_url", None),
                "name": getattr(attachment, "name", None),
            }
        )
    return result


def _entity_dict_list(entities: Any) -> List[Dict[str, Any]]:
    if not entities:
        return []
    result: List[Dict[str, Any]] = []
    for entity in entities:
        result.append(_dict_from_sdk_object(entity))
    return result


def _conversation_reference_dict(ref: Any) -> Dict[str, Any]:
    if ref is None:
        return {}
    try:
        return ref.model_dump(mode="json", exclude_none=True)
    except Exception:
        return dict(getattr(ref, "__dict__", {}))


def _ref_from_dict(data: Dict[str, Any]) -> Any:
    if ConversationReference is None:
        return None
    return ConversationReference.model_validate(data)


def _activity_to_dict(activity: Any) -> Dict[str, Any]:
    if isinstance(activity, dict):
        return dict(activity)
    try:
        return activity.model_dump(mode="json", exclude_none=True)
    except Exception:
        return dict(getattr(activity, "__dict__", {}))


def _activity_from_core(activity: Any) -> Any:
    if isinstance(activity, dict):
        if Activity is None:
            return activity
        try:
            return Activity.model_validate(activity)
        except Exception:
            return activity
    if Activity is None:
        return activity
    try:
        return Activity.model_validate(activity.model_dump(mode="json"))
    except Exception:
        return activity


def _dict_from_sdk_object(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return dict(obj)
    try:
        return obj.model_dump(mode="json", exclude_none=True)
    except Exception:
        return dict(getattr(obj, "__dict__", {}))


def _sdk_message_activity(text: str) -> Any:
    if MessageActivityInput is None:
        raise RuntimeError("Teams SDK message input is unavailable")
    return MessageActivityInput(text=text)


def _sdk_typing_activity() -> Any:
    if TypingActivityInput is None:
        raise RuntimeError("Teams SDK typing input is unavailable")
    return TypingActivityInput()


def _sdk_attachment(content_type: str, content: Any) -> Any:
    if Attachment is None:
        raise RuntimeError("Teams SDK attachment model is unavailable")
    return Attachment(content_type=content_type, content=content)


def _sdk_attachment_url(content_type: str, content_url: str) -> Any:
    if Attachment is None:
        raise RuntimeError("Teams SDK attachment model is unavailable")
    return Attachment(content_type=content_type, content_url=content_url)


def _sdk_adaptive_card(body: List[Any], actions: List[Any]) -> Any:
    if AdaptiveCard is None:
        raise RuntimeError("Teams SDK adaptive card model is unavailable")
    return AdaptiveCard(version="1.6", body=body, actions=actions)


def _sdk_adaptive_card_attachment(card: Any) -> Any:
    if AdaptiveCardAttachment is None:
        raise RuntimeError("Teams SDK adaptive card attachment model is unavailable")
    return AdaptiveCardAttachment(content=card)


def _sdk_conversation_reference(service_url: str, bot: Any, conversation: Any) -> Any:
    if ConversationReference is None:
        raise RuntimeError("Teams SDK conversation reference model is unavailable")
    return ConversationReference(
        serviceUrl=service_url,
        bot=bot,
        conversation=conversation,
        channelId=TEAMS_CHANNEL_ID,
    )


def _extract_channel_data(activity: Any) -> Dict[str, Any]:
    if isinstance(activity, dict):
        data = activity.get("channelData")
        if data is None:
            data = activity.get("channel_data")
        if isinstance(data, dict):
            return dict(data)
        return {}
    channel_data = getattr(activity, "channel_data", None)
    if channel_data is None:
        return {}
    return _dict_from_sdk_object(channel_data)


def _extract_team_id(channel_data: Dict[str, Any]) -> Optional[str]:
    team = channel_data.get("team") or channel_data.get("teamId")
    if isinstance(team, dict):
        return team.get("id") or team.get("tenantId")
    return team


def _extract_channel_id(channel_data: Dict[str, Any]) -> Optional[str]:
    channel = channel_data.get("channel") or channel_data.get("channelId")
    if isinstance(channel, dict):
        return channel.get("id") or channel.get("tenantId")
    return channel


def _extract_channel_type(channel_data: Dict[str, Any]) -> Optional[str]:
    channel = channel_data.get("channel")
    if isinstance(channel, dict):
        return channel.get("type")
    return None


def _extract_conversation_type(activity: Any) -> str:
    if isinstance(activity, dict):
        conv = activity.get("conversation") or {}
        if isinstance(conv, dict):
            return str(conv.get("conversation_type") or conv.get("conversationType") or conv.get("is_group") or "").strip()
    conv = getattr(activity, "conversation", None)
    if conv is None:
        return ""
    return str(getattr(conv, "conversation_type", "") or "").strip() or str(getattr(conv, "is_group", ""))


def _activity_text(activity: Any) -> str:
    text = getattr(activity, "text", "") or ""
    return _safe_text(text)


def _mentioned_id(entity: Any) -> str:
    mentioned = entity.get("mentioned") if isinstance(entity, dict) else getattr(entity, "mentioned", None)
    if mentioned is None:
        return ""
    if isinstance(mentioned, dict):
        raw = mentioned.get("aadObjectId") or mentioned.get("aad_object_id") or mentioned.get("id")
    else:
        raw = getattr(mentioned, "aad_object_id", None) or getattr(mentioned, "id", None)
    return _normalize_uuid(raw)


def _mentioned_account_id(entity: Any) -> str:
    mentioned = entity.get("mentioned") if isinstance(entity, dict) else getattr(entity, "mentioned", None)
    if mentioned is None:
        return ""
    raw = mentioned.get("id") if isinstance(mentioned, dict) else getattr(mentioned, "id", None)
    return _normalize_uuid(raw)


def _is_mention_entity(entity: Any) -> bool:
    entity_type = entity.get("type") if isinstance(entity, dict) else getattr(entity, "type", None)
    return entity_type == "mention"


def _recipient_matches_configured_bot(recipient_id: str, client_id: str) -> bool:
    if recipient_id == client_id:
        return True
    if not recipient_id.startswith("28:"):
        return False
    return _normalize_uuid(recipient_id.partition(":")[2]) == client_id


def _entity_mentions_bot(entity: Any, recipient_id: str, fallback_bot_id: str) -> bool:
    if not _is_mention_entity(entity):
        return False
    if recipient_id and _mentioned_account_id(entity) == recipient_id:
        return True
    return bool(
        fallback_bot_id
        and (not recipient_id or _recipient_matches_configured_bot(recipient_id, fallback_bot_id))
        and _mentioned_id(entity) == fallback_bot_id
    )


def _recipient_bot_id(activity: Any) -> str:
    if isinstance(activity, dict):
        recipient = activity.get("recipient") or {}
        if isinstance(recipient, dict):
            return _normalize_uuid(recipient.get("id"))
        return ""
    recipient = getattr(activity, "recipient", None)
    return _normalize_uuid(recipient.get("id") if isinstance(recipient, dict) else getattr(recipient, "id", None))


def _strip_bot_mentions(activity: Any, bot_id: str) -> Tuple[str, bool]:
    text = _activity_text(activity)
    entities = list(getattr(activity, "entities", None) or [])
    if not entities:
        return text.strip(), False
    recipient_id = _recipient_bot_id(activity)
    fallback_bot_id = _normalize_uuid(bot_id)
    matched_entities = [
        _entity_mentions_bot(entity, recipient_id, fallback_bot_id)
        for entity in entities
    ]
    if not any(matched_entities):
        return text.strip(), False

    pieces = []
    cursor = 0
    for entity, matches_bot in zip(entities, matched_entities):
        if not _is_mention_entity(entity):
            continue
        mention_text = entity.get("text") if isinstance(entity, dict) else getattr(entity, "text", None)
        if not mention_text:
            continue
        position = text.find(mention_text, cursor)
        if position < 0:
            continue
        pieces.append(text[cursor:position])
        if not matches_bot:
            pieces.append(text[position:position + len(mention_text)])
        cursor = position + len(mention_text)
    pieces.append(text[cursor:])
    return "".join(pieces).strip(), True


def _activity_mentions_bot(activity: Any, bot_id: str) -> bool:
    recipient_id = _recipient_bot_id(activity)
    fallback_bot_id = _normalize_uuid(bot_id)
    entities = activity.get("entities", []) if isinstance(activity, dict) else getattr(activity, "entities", None) or []
    for entity in entities:
        if _entity_mentions_bot(entity, recipient_id, fallback_bot_id):
            return True
    return False


def _attachment_content_type(attachment: Any) -> str:
    return str(getattr(attachment, "content_type", "") or "").lower()


def _attachment_content_url(attachment: Any) -> str:
    return str(getattr(attachment, "content_url", "") or "").strip()


def _attachment_name(attachment: Any) -> str:
    return str(getattr(attachment, "name", "") or "").strip()


def _is_supported_image_attachment(attachment: Any) -> bool:
    content_type = _attachment_content_type(attachment)
    name = _attachment_name(attachment)
    return any(content_type.startswith(prefix) for prefix in SUPPORTED_IMAGE_MIME_PREFIXES) or Path(name).suffix.lower() in SUPPORTED_IMAGE_EXTS


def _conversation_key(activity: Any, client_id: str, tenant_id: str) -> Tuple[str, str, str, str, str]:
    conversation = getattr(activity, "conversation", None)
    return (
        TEAMS_PLATFORM,
        _normalize_uuid(client_id),
        _normalize_uuid(tenant_id),
        str(getattr(conversation, "id", "") or ""),
        str(getattr(activity, "id", "") or ""),
    )


def _canonical_activity_key(activity: Any, client_id: str, tenant_id: str) -> str:
    return _canonical_tuple(_conversation_key(activity, client_id, tenant_id)).hex()


def _canonical_payload_hash(activity: Any) -> str:
    return _hash_json(_activity_payload_for_hash(activity))


@dataclass
class ReceiptRecord:
    canonical_key_hash: str
    canonical_payload_hash: str
    claimed_at: datetime
    expires_at: datetime

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReceiptRecord":
        return cls(
            canonical_key_hash=str(data["canonical_key_hash"]),
            canonical_payload_hash=str(data["canonical_payload_hash"]),
            claimed_at=_parse_iso(data["claimed_at"]) or _utc_now(),
            expires_at=_parse_iso(data["expires_at"]) or _utc_now(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canonical_key_hash": self.canonical_key_hash,
            "canonical_payload_hash": self.canonical_payload_hash,
            "claimed_at": _iso(self.claimed_at),
            "expires_at": _iso(self.expires_at),
        }


class TeamsReceiptStore:
    """Durable activity receipt store used for duplicate suppression."""

    def __init__(self, profile_dir: Path) -> None:
        self._path = profile_dir / "teams_receipts.json"
        self._lock_path = profile_dir / "teams_receipts.lock"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._receipts: Dict[str, ReceiptRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Teams receipt store is corrupt: {exc}") from exc
        records = data.get("records") if isinstance(data, dict) else None
        if records is None:
            raise RuntimeError("Teams receipt store has invalid schema")
        if not isinstance(records, dict):
            raise RuntimeError("Teams receipt store records must be an object")
        self._receipts = {}
        for key, value in records.items():
            try:
                self._receipts[str(key)] = ReceiptRecord.from_dict(value)
            except Exception as exc:
                raise RuntimeError(f"Teams receipt store has invalid record {key}: {exc}") from exc

    def _save_locked(self) -> None:
        atomic_json_write(self._path, {"version": _RECEIPT_VERSION, "records": {k: v.to_dict() for k, v in self._receipts.items()}}, mode=0o600)

    def cleanup_expired(self, now: Optional[datetime] = None, limit: int = 256) -> int:
        now = now or _utc_now()
        with interprocess_file_lock(self._lock_path):
            self._load()
            expired = [key for key, record in self._receipts.items() if record.expires_at <= now]
            expired = expired[:limit]
            if not expired:
                return 0
            for key in expired:
                self._receipts.pop(key, None)
            self._save_locked()
        return len(expired)

    def claim(self, canonical_key_hash: str, payload_hash: str, ttl_days: int) -> Tuple[str, bool]:
        now = _utc_now()
        expires_at = now + timedelta(days=ttl_days)
        with interprocess_file_lock(self._lock_path):
            self._load()
            expired = [key for key, record in self._receipts.items() if record.expires_at <= now]
            for key in expired[:64]:
                self._receipts.pop(key, None)
            existing = self._receipts.get(canonical_key_hash)
            if existing is not None:
                if existing.canonical_payload_hash == payload_hash:
                    return "duplicate", False
                return "collision", False
            self._receipts[canonical_key_hash] = ReceiptRecord(canonical_key_hash, payload_hash, now, expires_at)
            self._save_locked()
            return "claimed", True

    def remove(self, canonical_key_hash: str) -> bool:
        with interprocess_file_lock(self._lock_path):
            self._load()
            existed = canonical_key_hash in self._receipts
            if existed:
                self._receipts.pop(canonical_key_hash, None)
                self._save_locked()
            return existed


@dataclass
class TeamsDispatchTask:
    activity: Any
    event: MessageEvent
    reference: Any
    receipt_key: str
    payload_hash: str
    created_at: datetime = field(default_factory=_utc_now)


@dataclass
class DispatchCapacity:
    active: int
    pending: int
    max_active: int
    max_pending: int

    @property
    def available(self) -> bool:
        return self.max_active > 0 and (self.active + self.pending) < (self.max_active + self.max_pending)


class TeamsDispatchSupervisor:
    """Bounded in-process handoff for authenticated Teams activities."""

    def __init__(self, max_active: int, max_pending: int) -> None:
        self._max_active = max_active
        self._max_pending = max_pending
        self._active: set[asyncio.Task] = set()
        self._pending: asyncio.Queue[TeamsDispatchTask] = asyncio.Queue(maxsize=max_active + max_pending)
        self._processing = 0
        self._closed = False
        self._accepting = True

    @property
    def capacity(self) -> DispatchCapacity:
        return DispatchCapacity(self._processing, self._pending.qsize(), self._max_active, self._max_pending)

    def close(self) -> None:
        self._closed = True
        self._accepting = False

    async def stop(self) -> None:
        self.close()
        for task in list(self._active):
            task.cancel()
        await asyncio.gather(*self._active, return_exceptions=True)
        self._active.clear()
        while True:
            try:
                self._pending.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._pending.task_done()

    async def submit(self, task: TeamsDispatchTask) -> bool:
        if self._closed or not self._accepting or self._max_active <= 0:
            return False
        if not self.capacity.available:
            return False
        try:
            self._pending.put_nowait(task)
            return True
        except asyncio.QueueFull:
            return False

    def start_worker(self, handler: Callable[[TeamsDispatchTask], Awaitable[None]]) -> None:
        async def _worker() -> None:
            while not self._closed:
                item = await self._pending.get()
                self._processing += 1
                try:
                    await handler(item)
                except asyncio.CancelledError:
                    if self._closed:
                        raise
                    logger.debug("Teams dispatch task cancelled; keeping supervisor worker available")
                except Exception:
                    logger.exception("Teams dispatch task failed", exc_info=True)
                finally:
                    self._processing -= 1
                    self._pending.task_done()
                if self._closed:
                    return

        for _ in range(self._max_active):
            task = asyncio.create_task(_worker())
            self._active.add(task)
            task.add_done_callback(self._active.discard)


@dataclass
class TeamsTelemetry:
    """In-process Teams counters and latency samples."""

    counters: Dict[str, Dict[str, int]] = field(default_factory=dict)
    latencies: Dict[str, List[float]] = field(default_factory=dict)

    def increment(self, name: str, labels: Optional[Dict[str, Any]] = None) -> None:
        key = _metric_label_key(labels)
        self.counters.setdefault(name, {})[key] = self.counters.setdefault(name, {}).get(key, 0) + 1

    def observe(self, name: str, duration: float) -> None:
        samples = self.latencies.setdefault(name, [])
        samples.append(max(0.0, duration))
        if len(samples) > 1000:
            del samples[:-1000]

    def snapshot(self) -> Dict[str, Any]:
        return {
            "counters": {name: dict(values) for name, values in sorted(self.counters.items())},
            "latencies": {name: list(values) for name, values in sorted(self.latencies.items())},
        }


def _metric_label_key(labels: Optional[Dict[str, Any]]) -> str:
    if not labels:
        return "{}"
    return json.dumps({str(k): str(v) for k, v in sorted(labels.items())}, sort_keys=True)


class TeamsAiohttpAdapter:
    """SDK HTTP adapter bridge over aiohttp.web."""

    def __init__(self, app: Any, ingress_handler: Optional[Callable[[Any], Awaitable[Any]]] = None) -> None:
        self._app = app
        self._ingress_handler = ingress_handler
        self._routes: Dict[Tuple[str, str], Callable[..., Awaitable[Any]]] = {}

    def register_route(self, method: str, path: str, handler: Callable[[Any], Awaitable[Any]]) -> None:
        if method != "POST":
            raise ValueError(f"Unsupported HTTP method: {method}")
        self._routes[(method.upper(), path)] = handler

        async def route(request: Any) -> Any:
            if request.method != "POST":
                return web.json_response({"error": "Method not allowed"}, status=405)
            if self._ingress_handler is not None:
                return await self._ingress_handler(request)
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"error": "Bad request"}, status=400)
            if not isinstance(body, dict):
                return web.json_response({"error": "Bad request"}, status=400)
            result = await handler(HttpRequest(body=body, headers=dict(request.headers)))
            return self._to_response(result)

        self._app.router.add_post(path, route)

    def _to_response(self, result: Any) -> Any:
        if hasattr(result, "model_dump"):
            data = result.model_dump(exclude_none=True)
        else:
            data = result
        status = int(data.get("status", 200))
        body = data.get("body")
        if body is None:
            return web.Response(status=status)
        if isinstance(body, (dict, list)):
            return web.json_response(body, status=status)
        return web.Response(text=str(body), status=status)

    async def stop(self) -> None:
        return None


class TeamsPlatformAdapter(BasePlatformAdapter):
    """Microsoft Teams gateway adapter."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(TEAMS_PLATFORM))
        self._enabled = self._coerce_enabled(config.extra.get("enabled", False))
        self._client_id = str(config.extra.get("client_id", "") or "").strip()
        self._tenant_id = str(config.extra.get("tenant_id", "") or "").strip()
        self._host = self._coerce_host(config.extra.get("host", DEFAULT_HOST))
        self._port = self._coerce_port(config.extra.get("port", DEFAULT_PORT))
        self._allowed_users = self._normalize_allowed_users(config.extra.get("allowed_users", []))
        self._allow_all_users = config.extra.get("allow_all_users", False)
        if not isinstance(self._allow_all_users, bool):
            raise ValueError("teams.allow_all_users must be explicitly boolean")
        self._secret = str(config.extra.get("client_secret", "") or os.getenv("TEAMS_CLIENT_SECRET", "")).strip()
        self._max_body_bytes = int(config.extra.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES) or DEFAULT_MAX_BODY_BYTES)
        self._read_timeout_seconds = float(config.extra.get("read_timeout_seconds", DEFAULT_READ_TIMEOUT_SECONDS) or DEFAULT_READ_TIMEOUT_SECONDS)
        self._auth_timeout_seconds = float(config.extra.get("auth_timeout_seconds", DEFAULT_AUTH_TIMEOUT_SECONDS) or DEFAULT_AUTH_TIMEOUT_SECONDS)
        self._dispatch_max_active = int(config.extra.get("dispatch_max_active", DEFAULT_DISPATCH_MAX_ACTIVE) or DEFAULT_DISPATCH_MAX_ACTIVE)
        self._dispatch_max_pending = int(config.extra.get("dispatch_max_pending", DEFAULT_DISPATCH_MAX_PENDING) or DEFAULT_DISPATCH_MAX_PENDING)
        self._receipt_ttl_days = int(config.extra.get("receipt_ttl_days", DEFAULT_RECEIPT_TTL_DAYS) or DEFAULT_RECEIPT_TTL_DAYS)
        self._text_budget_bytes = int(config.extra.get("text_budget_bytes", DEFAULT_TEXT_BUDGET_BYTES) or DEFAULT_TEXT_BUDGET_BYTES)
        self._text_chunk_seconds = float(config.extra.get("text_chunk_seconds", DEFAULT_TEXT_CHUNK_SECONDS) or DEFAULT_TEXT_CHUNK_SECONDS)
        self._typing_interval_seconds = float(config.extra.get("typing_interval_seconds", DEFAULT_TYPING_INTERVAL_SECONDS) or DEFAULT_TYPING_INTERVAL_SECONDS)
        self._outbound_max_attempts = int(config.extra.get("outbound_max_attempts", DEFAULT_OUTBOUND_MAX_ATTEMPTS) or DEFAULT_OUTBOUND_MAX_ATTEMPTS)
        self._outbound_base_delay = float(config.extra.get("outbound_base_delay", DEFAULT_OUTBOUND_BASE_DELAY) or DEFAULT_OUTBOUND_BASE_DELAY)
        self._attachment_timeout_seconds = float(config.extra.get("attachment_timeout_seconds", DEFAULT_ATTACHMENT_TIMEOUT_SECONDS) or DEFAULT_ATTACHMENT_TIMEOUT_SECONDS)
        self._attachment_max_bytes = int(config.extra.get("attachment_max_bytes", DEFAULT_ATTACHMENT_MAX_BYTES) or DEFAULT_ATTACHMENT_MAX_BYTES)
        self._approval_timeout_seconds = float(config.extra.get("approval_timeout_seconds", DEFAULT_APPROVAL_TIMEOUT_SECONDS) or DEFAULT_APPROVAL_TIMEOUT_SECONDS)
        self._runner = None
        self._site = None
        self._app = None
        self._teams_app = None
        self._teams_adapter = None
        self._supervisor: Optional[TeamsDispatchSupervisor] = None
        self._receipt_store: Optional[TeamsReceiptStore] = None
        self._references: Dict[str, Any] = {}
        self._approval_waiters: Dict[str, asyncio.Future] = {}
        self._approval_state: Dict[str, Dict[str, Any]] = {}
        self._approval_lock = asyncio.Lock()
        self._last_receipt_error: Optional[str] = None
        self._telemetry = TeamsTelemetry()
        self._status = "disabled"
        self._status_reasons: List[str] = []
        self._lock_acquired = False
        self._message_handler = None
        self._team_aad_group_cache: Dict[Tuple[str, str], str] = {}


    @staticmethod
    def _coerce_enabled(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        raise ValueError("teams.enabled must be explicitly boolean")

    @staticmethod
    def _coerce_port(value: Any) -> int:
        try:
            port = int(value)
        except (TypeError, ValueError):
            raise ValueError("teams.port must be an integer within the valid TCP port range") from None
        if not 1 <= port <= 65535:
            raise ValueError("teams.port must be within the valid TCP port range")
        return port

    @staticmethod
    def _coerce_host(value: Any) -> str:
        host = str(value or DEFAULT_HOST).strip()
        if not host:
            raise ValueError("teams.host must be a valid bind host")
        try:
            socket.getaddrinfo(host, 0)
        except OSError as exc:
            raise ValueError("teams.host must be a valid bind host") from exc
        return host

    def _normalize_allowed_users(self, value: Any) -> List[str]:
        if isinstance(value, str):
            raw = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            raw = list(value)
        elif value is None:
            raw = []
        else:
            raise ValueError("teams.allowed_users must be a comma-separated string or list")
        seen = set()
        result = []
        for item in raw:
            normalized = _normalize_uuid(item)
            if not normalized or not _is_valid_uuid(normalized):
                raise ValueError("teams.allowed_users entries must be valid Azure AD object IDs")
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _validate_config(self) -> None:
        if not self._enabled:
            return
        if not _is_valid_uuid(self._client_id):
            raise ValueError("teams.client_id must be a valid UUID")
        if not _is_valid_uuid(self._tenant_id):
            raise ValueError("teams.tenant_id must be a valid UUID")
        if not self._secret:
            raise ValueError("TEAMS_CLIENT_SECRET is required")
        if is_network_accessible(self._host):
            logger.warning("Teams listener binds to non-loopback host %s; ensure an operator-managed ingress and firewall policy.", self._host)
        for user_id in self._allowed_users:
            if not _is_valid_uuid(user_id):
                raise ValueError("teams.allowed_users entries must be valid Azure AD object IDs")
        if not isinstance(self._allow_all_users, bool):
            raise ValueError("teams.allow_all_users must be explicitly boolean")

    def _ensure_runtime(self) -> None:
        if sys.version_info < (3, 11):
            raise RuntimeError("Microsoft Teams SDK requires Python >=3.11,<4.0")
        if not TEAMS_AVAILABLE:
            raise RuntimeError("microsoft-teams-apps==2.0.16 is unavailable")

    async def connect(self) -> bool:
        self._status = "starting"
        self._validate_config()
        if not self._enabled:
            self._status = "disabled"
            return False
        self._ensure_runtime()
        try:
            self._acquire_lock()
            self._receipt_store = TeamsReceiptStore(self._receipt_dir())
            try:
                self._receipt_store.cleanup_expired()
            except Exception as exc:
                self._set_status("degraded", f"receipt cleanup failed: {exc}")
            self._build_http_app()
            self._build_sdk_app()
            await self._teams_app.initialize()
            self._restore_teams_handler()
            self._supervisor = TeamsDispatchSupervisor(self._dispatch_max_active, self._dispatch_max_pending)
            self._supervisor.start_worker(self._dispatch_one)
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
            self._status_reasons.clear()
            self._mark_connected()
            self._status = "connected"
            return True
        except Exception:
            await self._cleanup_partial()
            self._set_status("failed", "startup failed")
            raise

    def _acquire_lock(self) -> None:
        acquired, _ = acquire_scoped_lock(_LOCK_SCOPE, self._client_id)
        self._lock_acquired = acquired
        if not acquired:
            raise RuntimeError("Teams credential lock is already held")

    def _release_lock(self) -> None:
        if self._lock_acquired:
            release_scoped_lock(_LOCK_SCOPE, self._client_id)
            self._lock_acquired = False

    def _set_status(self, status: str, reason: str = "") -> None:
        self._status = status
        if reason and reason not in self._status_reasons:
            self._status_reasons.append(reason)
        error_code = None if status in {"disabled", "starting", "connected"} else f"teams_{status}"
        error_message = reason or (status if status not in {"disabled", "starting", "connected"} else None)
        self._write_runtime_status_safe(
            status,
            platform_state=status,
            error_code=error_code,
            error_message=error_message,
            telemetry=self._telemetry.snapshot(),
        )

    def _receipt_dir(self) -> Path:
        return get_marlow_home() / "teams" / "receipts"

    async def _cleanup_partial(self) -> None:
        if self._supervisor:
            await self._supervisor.stop()
            self._supervisor = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._teams_app:
            try:
                await self._teams_app.stop()
            except Exception:
                pass
            self._teams_app = None
        self._teams_adapter = None
        self._team_aad_group_cache.clear()
        self._release_lock()

    def _build_http_app(self) -> None:
        self._app = web.Application(client_max_size=self._max_body_bytes)
        self._app.router.add_get("/healthz", self._handle_healthz)

    def _build_sdk_app(self) -> None:
        if App is None:
            raise RuntimeError("Teams SDK App is unavailable")
        self._teams_adapter = TeamsAiohttpAdapter(self._app, ingress_handler=self._handle_ingress)
        self._teams_app = App(
            client_id=self._client_id,
            client_secret=self._secret,
            tenant_id=self._tenant_id,
            http_server_adapter=self._teams_adapter,
            messaging_endpoint="/api/messages",
        )
        self._teams_app.server.on_request = self._handle_teams_activity

    def _restore_teams_handler(self) -> None:
        if self._teams_app is not None:
            self._teams_app.server.on_request = self._handle_teams_activity

    async def disconnect(self) -> None:
        if self._status == "disabled":
            return
        self._status = "stopping"
        await self._fail_approval_waiters()
        if self._supervisor:
            await self._supervisor.stop()
            self._supervisor = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._teams_app:
            try:
                await self._teams_app.stop()
            except Exception:
                pass
            self._teams_app = None
        self._teams_adapter = None
        self._team_aad_group_cache.clear()
        self._release_lock()
        self._status_reasons.clear()
        self._status = "disabled"
        self._mark_disconnected()

    async def _handle_healthz(self, request: Any) -> Any:
        ready = self._status == "connected" and self._runner is not None and self._supervisor is not None
        return web.json_response({"status": "ok" if ready else "not_ready"}, status=200 if ready else 503)

    async def _handle_ingress(self, request: Any) -> Any:
        started = time.perf_counter()
        result = "error"
        if self._status != "connected":
            self._telemetry.increment("teams_http_requests_total", {"result": "not_ready"})
            self._telemetry.observe("teams_http_ack_duration", time.perf_counter() - started)
            return web.json_response({"error": "Teams adapter is not ready"}, status=503)
        if request.method != "POST":
            result = "method_not_allowed"
            self._telemetry.increment("teams_http_requests_total", {"result": result})
            self._telemetry.observe("teams_http_ack_duration", time.perf_counter() - started)
            return web.json_response({"error": "Method not allowed"}, status=405)
        if int(request.content_length or 0) > self._max_body_bytes:
            result = "too_large"
            self._telemetry.increment("teams_http_requests_total", {"result": result})
            self._telemetry.observe("teams_http_ack_duration", time.perf_counter() - started)
            return web.json_response({"error": "Payload too large"}, status=413)
        if "application/json" not in (request.content_type or "").lower():
            result = "bad_content_type"
            self._telemetry.increment("teams_http_requests_total", {"result": result})
            self._telemetry.observe("teams_http_ack_duration", time.perf_counter() - started)
            return web.json_response({"error": "Unsupported content type"}, status=400)
        try:
            raw_body = await asyncio.wait_for(request.read(), timeout=self._read_timeout_seconds)
        except Exception:
            result = "bad_request"
            self._telemetry.increment("teams_http_requests_total", {"result": result})
            self._telemetry.observe("teams_http_ack_duration", time.perf_counter() - started)
            return web.json_response({"error": "Bad request"}, status=400)
        if len(raw_body) > self._max_body_bytes:
            result = "too_large"
            self._telemetry.increment("teams_http_requests_total", {"result": result})
            self._telemetry.observe("teams_http_ack_duration", time.perf_counter() - started)
            return web.json_response({"error": "Payload too large"}, status=413)
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception:
            result = "bad_json"
            self._telemetry.increment("teams_http_requests_total", {"result": result})
            self._telemetry.observe("teams_http_ack_duration", time.perf_counter() - started)
            return web.json_response({"error": "Bad request"}, status=400)
        if not isinstance(body, dict):
            result = "bad_json"
            self._telemetry.increment("teams_http_requests_total", {"result": result})
            self._telemetry.observe("teams_http_ack_duration", time.perf_counter() - started)
            return web.json_response({"error": "Bad request"}, status=400)
        try:
            response = await asyncio.wait_for(
                self._teams_app.server.handle_request(HttpRequest(body=body, headers=dict(request.headers))),
                timeout=self._auth_timeout_seconds,
            )
        except asyncio.TimeoutError:
            result = "auth_timeout"
            self._telemetry.increment("teams_http_requests_total", {"result": result})
            self._telemetry.observe("teams_http_ack_duration", time.perf_counter() - started)
            return web.json_response({"error": "Authentication timed out"}, status=503)
        except Exception:
            logger.warning("Teams SDK request handling failed")
            result = "sdk_error"
            self._telemetry.increment("teams_http_requests_total", {"result": result})
            self._telemetry.observe("teams_http_ack_duration", time.perf_counter() - started)
            return web.json_response({"error": "Teams request handling failed"}, status=500)
        result = "ok"
        self._telemetry.increment("teams_http_requests_total", {"result": result})
        self._telemetry.observe("teams_http_ack_duration", time.perf_counter() - started)
        return self._teams_adapter._to_response(response)

    async def _handle_teams_activity(self, event: Any) -> Any:
        activity = _activity_from_core(event.body)
        activity_type = getattr(activity, "type", None) or "unknown"
        if activity_type == "invoke":
            return await self._handle_invoke(activity)
        if activity_type != "message":
            self._telemetry.increment("teams_activities_total", {"type": activity_type, "result": "ignored"})
            return InvokeResponse(status=200)
        if not self._validate_activity(activity):
            self._telemetry.increment("teams_activities_total", {"type": activity_type, "result": "rejected"})
            return InvokeResponse(status=200)
        if not self._supervisor or not self._supervisor.capacity.available:
            self._telemetry.increment("teams_activities_total", {"type": activity_type, "result": "dispatch_rejected"})
            self._telemetry.increment("teams_dispatch_rejected_total", {"reason": "capacity"})
            return InvokeResponse(status=503)
        self._last_receipt_error = None
        dispatch_task = await self._build_dispatch_task(activity)
        if dispatch_task is None:
            if self._last_receipt_error:
                logger.warning("Teams activity rejected by receipt store: %s", self._last_receipt_error)
                self._telemetry.increment("teams_activities_total", {"type": activity_type, "result": "receipt_rejected"})
                self._telemetry.increment("teams_dispatch_rejected_total", {"reason": "receipt_store"})
                return InvokeResponse(status=503)
            self._telemetry.increment("teams_activities_total", {"type": activity_type, "result": "duplicate"})
            self._telemetry.increment("teams_duplicate_activities_total")
            return InvokeResponse(status=200)
        if not await self._supervisor.submit(dispatch_task):
            if dispatch_task.receipt_key and self._receipt_store and self._receipt_store.remove(dispatch_task.receipt_key):
                self._telemetry.increment("teams_receipt_operations_total", {"operation": "remove", "result": "removed"})
            self._telemetry.increment("teams_activities_total", {"type": activity_type, "result": "dispatch_rejected"})
            self._telemetry.increment("teams_dispatch_rejected_total", {"reason": "submit"})
            return InvokeResponse(status=503)
        self._telemetry.increment("teams_activities_total", {"type": activity_type, "result": "accepted"})
        return InvokeResponse(status=200)

    async def _authenticate_activity(self, activity: Any, authorization: str) -> None:
        if not authorization.startswith("Bearer "):
            raise ValueError("missing bearer token")
        if not self._secret:
            raise ValueError("missing client secret")

    def _validate_activity(self, activity: Any) -> bool:
        if not getattr(activity, "id", None):
            return False
        if getattr(activity, "channel_id", "") != TEAMS_CHANNEL_ID:
            return False
        tenant_id = self._activity_tenant_id(activity)
        if _normalize_uuid(tenant_id) != self._tenant_id:
            logger.warning("Teams activity tenant mismatch: configured tenant rejected")
            return False
        sender = getattr(activity, "from_", None)
        sender_id = _normalize_uuid(getattr(sender, "aad_object_id", None))
        if not sender_id:
            return False
        bot_id = _normalize_uuid(self._client_id)
        if sender_id == bot_id:
            return False
        if _extract_conversation_type(activity) not in SUPPORTED_CONVERSATION_TYPES:
            return False
        if _extract_conversation_type(activity) == "channel":
            channel_data = _extract_channel_data(activity)
            if not _extract_team_id(channel_data) or not _extract_channel_id(channel_data):
                return False
            if _extract_channel_type(channel_data) not in {"standard", None}:
                return False
        text = _activity_text(activity)
        has_image = any(_is_supported_image_attachment(a) for a in getattr(activity, "attachments", None) or [])
        if not text and not has_image:
            return False
        bot_id = _normalize_uuid(self._client_id)
        if _extract_conversation_type(activity) in {"groupChat", "channel"} and not _activity_mentions_bot(activity, bot_id):
            return False
        return True

    def _authorize_activity(self, activity: Any) -> bool:
        sender = getattr(activity, "from_", None)
        user_id = _normalize_uuid(getattr(sender, "aad_object_id", None))
        if not user_id:
            return False
        if self._allow_all_users:
            return True
        return user_id in self._allowed_users

    def _activity_tenant_id(self, activity: Any) -> str:
        tenant_id = getattr(getattr(activity, "conversation", None), "tenant_id", None)
        if tenant_id:
            return str(tenant_id)
        channel_data = _extract_channel_data(activity)
        tenant = channel_data.get("tenant") or channel_data.get("tenantId")
        if isinstance(tenant, dict):
            return str(tenant.get("id") or "")
        if tenant:
            return str(tenant)
        return ""


    def _is_safe_attachment_url(self, url: str) -> bool:
        return bool(is_safe_url(url))

    def _thread_context_enabled(self) -> bool:
        thread_context = self.config.extra.get("thread_context") or {}
        return bool(thread_context.get("enabled", False))

    def _thread_context_require_complete(self) -> bool:
        thread_context = self.config.extra.get("thread_context") or {}
        return bool(thread_context.get("require_complete", True))

    def _graph_client(self) -> Any:
        if not self._teams_app:
            raise TeamsThreadContextError(
                "Teams app is not connected",
                user_facing_message="I could not read the Teams thread because Marlow is not connected to Teams right now.",
            )
        get_graph = getattr(self._teams_app, "get_app_graph", None)
        if get_graph is not None:
            try:
                graph = get_graph(self._tenant_id)
                if hasattr(graph, "get") and callable(graph.get):
                    return graph
            except ImportError:
                pass
        get_graph_token = getattr(self._teams_app, "_get_graph_token", None)
        if get_graph_token is None or Client is None or ClientOptions is None:
            raise TeamsThreadContextError(
                "Teams SDK Graph client is unavailable",
                user_facing_message="I could not read the Teams thread because the Teams Graph client is not available.",
            )
        return Client(
            ClientOptions(
                base_url=GRAPH_BASE_URL,
                timeout=GRAPH_CONTEXT_TIMEOUT_SECONDS,
                token=lambda: get_graph_token(self._tenant_id),
            )
        )

    def _parse_root_message_id(self, activity: Any) -> Optional[str]:
        conversation = getattr(activity, "conversation", None)
        conversation_id = getattr(conversation, "id", None) if conversation is not None else None
        if conversation_id is None and isinstance(activity, dict):
            conversation = activity.get("conversation") or {}
            conversation_id = conversation.get("id") if isinstance(conversation, dict) else None
        conversation_id = str(conversation_id or "")
        if conversation_id.count("messageid=") != 1:
            return None
        matches = TEAMS_THREAD_CONTEXT_RE.findall(conversation_id)
        if len(matches) != 1:
            return None
        value = matches[0].strip()
        return value or None

    async def _thread_locator(self, activity: Any) -> Optional[TeamsThreadLocator]:
        channel_data = _extract_channel_data(activity)
        if _extract_channel_type(channel_data) not in {"standard", None}:
            return None
        team_id = await self._resolve_team_aad_group_id_from_channel_data(channel_data)
        if not team_id or not _is_valid_uuid(team_id):
            return None
        channel_id = _extract_channel_id(channel_data)
        root_message_id = self._parse_root_message_id(activity)
        if not channel_id or not root_message_id:
            return None
        return TeamsThreadLocator(
            tenant_id=self._tenant_id,
            team_aad_group_id=_normalize_uuid(team_id),
            channel_id=str(channel_id),
            root_message_id=str(root_message_id),
        )

    async def _resolve_team_aad_group_id_from_channel_data(self, channel_data: Dict[str, Any]) -> Optional[str]:
        team_data = channel_data.get("team") if isinstance(channel_data.get("team"), dict) else {}
        direct = (
            channel_data.get("aad_group_id")
            or channel_data.get("aadGroupId")
            or channel_data.get("team_aad_group_id")
            or team_data.get("aad_group_id")
            or team_data.get("aadGroupId")
            or (_normalize_uuid(team_data.get("id")) if team_data.get("id") and _is_valid_uuid(team_data.get("id")) else None)
        )
        if direct and _is_valid_uuid(direct):
            return _normalize_uuid(direct)

        internal_team_id = _extract_team_id(channel_data)
        if not internal_team_id:
            return None
        cache_key = (self._tenant_id, str(internal_team_id))
        cached = self._team_aad_group_cache.get(cache_key)
        if cached:
            return cached

        api = getattr(getattr(self._teams_app, "api", None), "teams", None)
        get_team = getattr(api, "get_by_id", None)
        if get_team is None:
            return None

        details = get_team(str(internal_team_id))
        if inspect.isawaitable(details):
            details = await details
        aad_group_id = getattr(details, "aad_group_id", None) if not isinstance(details, dict) else details.get("aad_group_id")
        if not aad_group_id or not _is_valid_uuid(aad_group_id):
            return None
        normalized = _normalize_uuid(aad_group_id)
        self._team_aad_group_cache[cache_key] = normalized
        return normalized

    async def enrich_authorized_event(self, event: MessageEvent) -> MessageEvent:
        activity = getattr(event, "raw_message", None)
        if activity is None or not isinstance(getattr(event, "raw_message", None), dict):
            return event
        activity_obj = _activity_from_core(activity)
        return await self._enrich_thread_context(event, activity_obj)

    async def _enrich_thread_context(self, event: MessageEvent, activity: Any) -> MessageEvent:
        if not self._thread_context_enabled():
            return event
        if not event.source or event.source.chat_type != "channel":
            return event
        channel_data = _extract_channel_data(activity)
        if _extract_channel_type(channel_data) not in {"standard", None}:
            raise TeamsThreadContextError(
                "Teams thread context is supported only for standard channels",
                user_facing_message="Full-thread context is currently supported only in standard Teams channels.",
            )
        if not self._thread_context_require_complete():
            raise TeamsThreadContextError(
                "Teams thread context require_complete must remain enabled",
                user_facing_message="Full-thread context cannot run with partial context enabled.",
            )
        try:
            locator = await self._thread_locator(activity)
            if locator is None:
                raise TeamsThreadContextError(
                    "Teams thread context cannot resolve a complete thread locator",
                    user_facing_message="I could not read the full Teams thread because the thread locator is incomplete.",
                )
            snapshot = await self._load_thread_snapshot(event, activity, locator)
            if not snapshot.complete_through_trigger:
                raise TeamsThreadContextError(
                    "Teams thread context was not complete through the trigger",
                    user_facing_message="I could not read the full Teams thread through your message, so I did not answer from partial context.",
                )
            rendered = render_external_conversation_snapshot(snapshot, event)
            if len(rendered.encode("utf-8")) > self._text_budget_bytes:
                raise TeamsThreadContextError(
                    "Teams thread context exceeds the local text budget",
                    user_facing_message="This Teams thread is too large for the current full-thread context limit, so I did not answer from partial context.",
                )
            return dataclasses.replace(event, external_conversation_snapshot=snapshot)
        except TeamsThreadContextError:
            raise
        except Exception as exc:
            raise TeamsThreadContextError(
                f"Teams thread context load failed: {type(exc).__name__}",
                user_facing_message="I could not read the full Teams thread, so I did not answer from partial context.",
            ) from exc

    async def _load_thread_snapshot(self, event: MessageEvent, activity: Any, locator: TeamsThreadLocator) -> ExternalConversationSnapshot:
        graph = self._graph_client()
        messages: Dict[str, Dict[str, Any]] = {}
        for attempt in range(GRAPH_CONTEXT_RETRY_ATTEMPTS + 1):
            try:
                root = await self._graph_get(graph, locator, "root")
                replies = await self._graph_get_replies(graph, locator)
                messages.clear()
                for item in [root] + replies:
                    message_id = str(item.get("id") or "")
                    if not message_id:
                        raise TeamsThreadContextError(
                            "Graph message missing id",
                            user_facing_message="I could not read the full Teams thread because Graph returned an incomplete message.",
                        )
                    if message_id in messages and messages[message_id] != item:
                        raise TeamsThreadContextError(
                            "Graph returned conflicting payloads for message id",
                            user_facing_message="I could not read the full Teams thread because Graph returned conflicting message data.",
                        )
                    messages[message_id] = item
                return self._build_snapshot(event, activity, locator, messages)
            except TeamsThreadContextError:
                raise
            except Exception as exc:
                if attempt >= GRAPH_CONTEXT_RETRY_ATTEMPTS or not self._is_retryable_graph_error(exc):
                    raise TeamsThreadContextError(
                        f"Teams thread context load failed after retries: {type(exc).__name__}",
                        user_facing_message=self._graph_user_facing_message(exc),
                    ) from exc
                await asyncio.sleep(self._retry_after_delay(exc, attempt))
        raise TeamsThreadContextError(
            "Teams thread context load failed",
            user_facing_message="I could not read the full Teams thread, so I did not answer from partial context.",
        )

    async def _graph_get(self, graph: Any, locator: TeamsThreadLocator, operation: str) -> Dict[str, Any]:
        url = self._graph_thread_message_url(locator)
        return await self._graph_request_json(graph, url, operation)

    async def _graph_get_replies(self, graph: Any, locator: TeamsThreadLocator) -> List[Dict[str, Any]]:
        replies: List[Dict[str, Any]] = []
        next_link: Optional[str] = None
        seen_links: set[str] = set()
        url = self._graph_thread_replies_url(locator)
        while True:
            if next_link:
                if next_link in seen_links:
                    raise TeamsThreadContextError(
                        "Graph replies nextLink loop detected",
                        user_facing_message="I could not read the full Teams thread because Graph returned a pagination loop.",
                    )
                seen_links.add(next_link)
                url = next_link
            result = await self._graph_request_json(graph, url, "replies")
            values = result.get("value")
            if not isinstance(values, list):
                raise TeamsThreadContextError(
                    "Graph replies response missing value",
                    user_facing_message="I could not read the full Teams thread because Graph returned an incomplete reply page.",
                )
            replies.extend(values)
            next_link = result.get("@odata.nextLink")
            if not next_link:
                break
        return replies

    async def _graph_request_json(self, graph: Any, url: str, operation: str) -> Dict[str, Any]:
        result = graph.get(url)
        if inspect.isawaitable(result):
            result = await result
        if hasattr(result, "json"):
            try:
                result = result.json()
            except Exception as exc:
                raise TeamsThreadContextError(
                    f"Graph {operation} response could not be decoded",
                    user_facing_message=f"I could not read the full Teams thread because Graph returned malformed {operation} data.",
                ) from exc
        if not isinstance(result, dict):
            raise TeamsThreadContextError(
                f"Graph {operation} response was not JSON",
                user_facing_message=f"I could not read the full Teams thread because Graph returned malformed {operation} data.",
            )
        return result

    def _graph_status_code(self, exc: Exception) -> Optional[int]:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        try:
            return int(status) if status is not None else None
        except (TypeError, ValueError):
            return None

    def _graph_user_facing_message(self, exc: Exception) -> str:
        status = self._graph_status_code(exc)
        if status == 401:
            return "I could not read the full Teams thread because Marlow could not authenticate to Microsoft Graph."
        if status == 403:
            return "I could not read the full Teams thread because Marlow is missing thread-read consent in this Team."
        if status == 404:
            return "I could not read the full Teams thread because the Team, channel, or root message was not found."
        if status == 429:
            return "I could not read the full Teams thread because Microsoft Graph throttled the request. Please retry the mention."
        if status in {500, 502, 503, 504}:
            return "I could not read the full Teams thread after a Microsoft Graph service error. Please retry the mention."
        return "I could not read the full Teams thread, so I did not answer from partial context."

    def _is_retryable_graph_error(self, exc: Exception) -> bool:
        status = self._graph_status_code(exc)
        if status == 429 or status in {500, 502, 503, 504}:
            return True
        message = str(exc).lower()
        return any(token in message for token in ("timeout", "429", "500", "502", "503", "504", "rate limit"))

    def _retry_after_delay(self, exc: Exception, attempt: int) -> float:
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            return max(0.0, retry_after)
        return max(GRAPH_RETRY_AFTER_DEFAULT_SECONDS, GRAPH_CONTEXT_RETRY_DELAY_SECONDS * (attempt + 1))

    def _graph_thread_message_url(self, locator: TeamsThreadLocator) -> str:
        return "/".join(
            [
                "",
                "teams",
                self._quote_graph_segment(locator.team_aad_group_id),
                "channels",
                self._quote_graph_segment(locator.channel_id),
                "messages",
                self._quote_graph_segment(locator.root_message_id),
            ]
        )

    def _graph_thread_replies_url(self, locator: TeamsThreadLocator) -> str:
        return f"{self._graph_thread_message_url(locator)}/replies?$top={GRAPH_REPLY_TOP}"

    def _quote_graph_segment(self, value: str) -> str:
        return quote(value, safe="")

    def _build_snapshot(
        self,
        event: MessageEvent,
        activity: Any,
        locator: TeamsThreadLocator,
        graph_messages: Dict[str, Dict[str, Any]],
    ) -> ExternalConversationSnapshot:
        activity_id = str(getattr(activity, "id", "") or "")
        if not activity_id:
            raise TeamsThreadContextError(
                "Triggering activity is missing id",
                user_facing_message="I could not read the full Teams thread because the triggering Teams message has no stable id.",
            )
        activity_data = _activity_to_dict(activity)
        activity_ts = self._parse_graph_datetime(activity_data.get("created_date_time") or activity_data.get("createdDateTime"))
        normalized: List[ExternalConversationMessage] = []
        for message_id, item in graph_messages.items():
            msg = self._normalize_graph_message(item)
            if msg is None:
                raise TeamsThreadContextError(
                    f"Graph message {message_id} could not be normalized",
                    user_facing_message="I could not read the full Teams thread because Graph returned an incomplete message.",
                )
            normalized.append(msg)
        normalized.sort(key=lambda msg: (0 if msg.message_id == locator.root_message_id else 1, msg.created_at or datetime.min.replace(tzinfo=timezone.utc), msg.message_id))
        trigger_index = next((i for i, msg in enumerate(normalized) if msg.message_id == activity_id), None)
        if trigger_index is not None:
            graph_message = normalized[trigger_index]
            normalized = normalized[: trigger_index + 1]
            normalized[-1] = dataclasses.replace(graph_message, is_trigger=True)
        else:
            if activity_ts is None:
                raise TeamsThreadContextError(
                    "Triggering activity timestamp is missing",
                    user_facing_message="I could not read the full Teams thread because the triggering Teams message has no timestamp.",
                )
            normalized = [msg for msg in normalized if msg.created_at < activity_ts]
            trigger_activity = self._normalize_activity(activity)
            if trigger_activity is None:
                raise TeamsThreadContextError(
                    "Triggering activity could not be normalized",
                    user_facing_message="I could not read the full Teams thread because the triggering Teams message could not be normalized.",
                )
            normalized.append(trigger_activity)
        return ExternalConversationSnapshot(
            source_kind="teams_channel_thread",
            platform=Platform(TEAMS_PLATFORM),
            chat_id=getattr(getattr(event, "source", None), "chat_id", ""),
            thread_id=getattr(getattr(event, "source", None), "thread_id", ""),
            captured_at=_utc_now(),
            trigger_message_id=activity_id,
            complete_through_trigger=True,
            history_mode=ExternalHistoryMode.REPLACE_VISIBLE_SESSION_HISTORY,
            messages=tuple(normalized),
        )

    def _normalize_graph_message(self, item: Dict[str, Any]) -> Optional[ExternalConversationMessage]:
        message_id = str(item.get("id") or "")
        if not message_id:
            return None
        created_at = self._parse_graph_datetime(item.get("createdDateTime") or item.get("created_date_time"))
        edited_at = self._parse_graph_datetime(item.get("lastEditedDateTime") or item.get("last_edited_date_time"))
        deleted_at = self._parse_graph_datetime(item.get("deletedDateTime") or item.get("deleted_date_time"))
        subject = item.get("subject")
        text = self._graph_body_text(item.get("body"))
        if deleted_at is not None:
            text = "[message deleted]"
        elif edited_at is not None:
            text = f"{text}\n[edited]"
        attachments = self._normalize_graph_attachments(item.get("attachments") or [])
        return ExternalConversationMessage(
            message_id=message_id,
            parent_message_id=item.get("replyToId"),
            actor=self._actor_from_graph(item.get("from")),
            created_at=created_at or _utc_now(),
            edited_at=edited_at,
            deleted_at=deleted_at,
            subject=str(subject) if subject is not None else None,
            text=text,
            attachments=tuple(attachments),
        )

    def _normalize_activity(self, activity: Any) -> Optional[ExternalConversationMessage]:
        message_id = str(getattr(activity, "id", "") or "")
        if not message_id:
            return None
        created_at = self._parse_graph_datetime(getattr(activity, "created_date_time", None) or getattr(activity, "createdDateTime", None)) or _utc_now()
        text = _activity_text(activity)
        if text:
            text = _strip_bot_mentions(activity, _normalize_uuid(self._client_id))[0]
        return ExternalConversationMessage(
            message_id=message_id,
            parent_message_id=getattr(activity, "reply_to_id", None) or getattr(activity, "replyToId", None),
            actor=self._actor_from_graph(_activity_to_dict(activity).get("from")),
            created_at=created_at,
            edited_at=self._parse_graph_datetime(getattr(activity, "last_edited_date_time", None) or getattr(activity, "lastEditedDateTime", None)),
            deleted_at=self._parse_graph_datetime(getattr(activity, "deleted_date_time", None) or getattr(activity, "deletedDateTime", None)),
            subject=getattr(activity, "subject", None),
            text=text,
            attachments=(),
            is_trigger=True,
        )

    def _graph_body_text(self, body: Any) -> str:
        if isinstance(body, dict):
            content = body.get("content")
            content_type = body.get("contentType") or body.get("content_type")
            if isinstance(content, str):
                if content_type == "text":
                    return _safe_text(content)
                return self._html_to_text(content)
        if body is None:
            return ""
        return _safe_text(body)

    def _html_to_text(self, html: str) -> str:
        html = re.sub(r"(?is)<(script|style|noscript)\b[^>]*>.*?</\1>", "", html)
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            for br in soup.find_all("br"):
                br.replace_with("\n")
            for p in soup.find_all("p"):
                p.append("\n\n")
            return _safe_text(soup.get_text("\n"))
        except Exception:
            return _safe_text(re.sub(r"<[^>]+>", "\n", html))

    def _normalize_graph_attachments(self, attachments: Any) -> List[ExternalAttachmentDescriptor]:
        result: List[ExternalAttachmentDescriptor] = []
        for index, attachment in enumerate(attachments or []):
            if not isinstance(attachment, dict):
                attachment = _dict_from_sdk_object(attachment)
            name = attachment.get("name") or attachment.get("id")
            content_type = attachment.get("contentType") or attachment.get("content_type")
            result.append(
                ExternalAttachmentDescriptor(
                    attachment_id=attachment.get("id"),
                    name=str(name) if name is not None else None,
                    content_type=str(content_type) if content_type is not None else None,
                    reference_kind=self._attachment_reference_kind(str(content_type or ""), str(name or "")),
                )
            )
        return result[:12]

    def _attachment_reference_kind(self, content_type: str, name: str) -> str:
        if content_type.startswith("image/"):
            return "image"
        if content_type.startswith("text/"):
            return "file"
        if content_type.startswith("application/vnd.microsoft.card"):
            return "card"
        if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            return "image"
        if name.endswith((".txt", ".md", ".csv", ".log")):
            return "file"
        return "unknown"

    def _actor_from_graph(self, identity: Any) -> ExternalActor:
        if isinstance(identity, dict):
            user = identity.get("user") or identity.get("application") or identity.get("device") or identity.get("conversation")
            if isinstance(user, dict):
                display = user.get("displayName") or user.get("display_name") or user.get("name")
                stable_id = user.get("id") or user.get("aadObjectId") or user.get("aad_object_id")
                kind = ExternalActorKind.USER
                if identity.get("application") or "application" in user:
                    kind = ExternalActorKind.APPLICATION
                return ExternalActor(kind=kind, stable_id=str(stable_id) if stable_id else None, display_name=str(display) if display else None)
        return ExternalActor(kind=ExternalActorKind.UNKNOWN, stable_id=None, display_name=None)

    def _parse_graph_datetime(self, value: Any) -> Optional[datetime]:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    async def _build_dispatch_task(self, activity: Any) -> Optional[TeamsDispatchTask]:
        receipt_key = _canonical_activity_key(activity, self._client_id, self._tenant_id)
        payload_hash = _canonical_payload_hash(activity)
        if self._receipt_store:
            claim, accepted = self._receipt_store.claim(receipt_key, payload_hash, self._receipt_ttl_days)
            self._telemetry.increment("teams_receipt_operations_total", {"operation": "claim", "result": claim})
        else:
            claim, accepted = "unavailable", False
            self._telemetry.increment("teams_receipt_operations_total", {"operation": "claim", "result": "unavailable"})
        if not accepted:
            if claim == "collision":
                self._last_receipt_error = "activity id collision"
            return None
        text, _ = _strip_bot_mentions(activity, _normalize_uuid(self._client_id))
        reference = _sdk_conversation_reference(getattr(activity, "service_url", ""), getattr(activity, "recipient", None), getattr(activity, "conversation", None))
        source = self._build_source(activity, reference=reference)
        self._references[(source.chat_id, source.thread_id or "")] = _conversation_reference_dict(reference)
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=_activity_to_dict(activity),
            message_id=getattr(activity, "id", None),
        )
        return TeamsDispatchTask(activity=activity, event=event, reference=reference, receipt_key=receipt_key, payload_hash=payload_hash)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        reference = self._references.get((chat_id, ""), {})
        if not reference:
            reference = self._references.get(chat_id, {})
        conversation = reference.get("conversation", {}) if isinstance(reference, dict) else {}
        return {
            "name": str(conversation.get("id") or chat_id),
            "type": "channel" if isinstance(conversation.get("conversationType"), str) and conversation["conversationType"] == "channel" else "group",
            "reference": reference,
        }

    def _build_source(self, activity: Any, reference: Any = None) -> Any:
        channel_data = _extract_channel_data(activity)
        conversation_type = _extract_conversation_type(activity)
        if conversation_type == "channel":
            chat_id = json.dumps([self._tenant_id, self._client_id, _extract_team_id(channel_data), _extract_channel_id(channel_data)], separators=(",", ":"))
            thread_id = str(getattr(getattr(activity, "conversation", None), "id", "") or "")
            chat_type = "channel"
        else:
            chat_id = json.dumps([self._tenant_id, self._client_id, getattr(getattr(activity, "conversation", None), "id", "")], separators=(",", ":"))
            thread_id = None
            chat_type = "dm" if conversation_type == "personal" else "group"
        sender = getattr(activity, "from_", None)
        user_id = _normalize_uuid(getattr(sender, "aad_object_id", None))
        metadata = {"teams_reference": _conversation_reference_dict(reference)} if reference is not None else None
        if reference is not None and thread_id:
            metadata["thread_id"] = str(thread_id)
        return SessionSource(
            platform=Platform(TEAMS_PLATFORM),
            chat_id=chat_id,
            chat_type=chat_type,
            thread_id=thread_id,
            user_id=user_id,
            user_id_alt=f"{self._tenant_id}/{user_id}" if user_id else "",
            user_name=str(getattr(sender, "name", "") or ""),
            metadata=metadata,
        )

    async def _dispatch_one(self, task: TeamsDispatchTask) -> None:
        started = time.perf_counter()
        try:
            media_urls, media_types = await self._fetch_supported_images(task.activity)
            task.event.media_urls = media_urls
            task.event.media_types = media_types
            if not task.event.text and not media_urls:
                if self._receipt_store and self._receipt_store.remove(task.receipt_key):
                    self._telemetry.increment("teams_receipt_operations_total", {"operation": "remove", "result": "removed"})
                self._telemetry.increment("teams_agent_dispatch_total", {"result": "empty_after_media"})
                return
            task.event.message_type = MessageType.PHOTO if not task.event.text and media_urls else MessageType.TEXT
            if self._message_handler:
                processing_task = await self.handle_message(task.event)
                if processing_task is not None:
                    await processing_task
            self._telemetry.increment("teams_agent_dispatch_total", {"result": "accepted"})
        except asyncio.CancelledError:
            self._telemetry.increment("teams_agent_dispatch_total", {"result": "cancelled"})
            raise
        except Exception:
            self._telemetry.increment("teams_agent_dispatch_total", {"result": "failed"})
            raise
        finally:
            self._telemetry.observe("teams_agent_duration", time.perf_counter() - started)

    async def _fetch_supported_images(self, activity: Any) -> Tuple[List[str], List[str]]:
        urls: List[str] = []
        types: List[str] = []
        for attachment in getattr(activity, "attachments", None) or []:
            if not _is_supported_image_attachment(attachment):
                continue
            url = _attachment_content_url(attachment)
            if not url:
                continue
            try:
                path, mime_type = await self._fetch_image_attachment(attachment, url)
            except Exception:
                logger.warning("Teams image attachment skipped")
                continue
            urls.append(path)
            types.append(mime_type)
        return urls, types

    async def _fetch_image_attachment(self, attachment: Any, url: str) -> Tuple[str, str]:
        started = time.perf_counter()
        try:
            parsed = urlparse(url)
            if parsed.scheme != "https":
                raise ValueError("unsupported attachment URL scheme")
            if not self._is_safe_attachment_url(url):
                raise ValueError("attachment URL failed media safety check")
            client = getattr(self._teams_app, "http_client", None) if self._teams_app is not None else None
            if client is None:
                client = Client(timeout=self._attachment_timeout_seconds)
            response = client.get(url, timeout=self._attachment_timeout_seconds, follow_redirects=False)
            if inspect.isawaitable(response):
                response = await response
            path, mime_type = await self._read_valid_image_response(response, attachment)
            self._telemetry.increment("teams_media_total", {"direction": "inbound", "result": "success"})
            return path, mime_type
        except Exception:
            self._telemetry.increment("teams_media_total", {"direction": "inbound", "result": "failure"})
            raise
        finally:
            self._telemetry.observe("teams_media_fetch_duration", time.perf_counter() - started)

    async def _read_valid_image_response(self, response: Any, attachment: Any) -> Tuple[str, str]:
        status = getattr(response, "status_code", None) or getattr(response, "status", 0)
        if status >= 400:
            raise ValueError(f"attachment fetch status {status}")
        if 300 <= status < 400:
            raise ValueError("attachment redirect is not allowed")
        headers = getattr(response, "headers", {})
        content_type = str(headers.get("Content-Type", "")).split(";", 1)[0].lower()
        if not content_type:
            content_type = _attachment_content_type(attachment)
        if not any(content_type.startswith(prefix) for prefix in SUPPORTED_IMAGE_MIME_PREFIXES):
            raise ValueError(f"unsupported attachment content type {content_type}")
        ext = mimetypes.guess_extension(content_type) or ".jpg"
        chunks: List[bytes] = []
        total = 0
        async for chunk in self._iter_response_bytes(response, 64 * 1024):
            total += len(chunk)
            if total > self._attachment_max_bytes:
                raise ValueError("attachment exceeds max bytes")
            chunks.append(chunk)
        data = b"".join(chunks)
        path = cache_image_from_bytes(data, ext=ext)
        return path, content_type

    async def _iter_response_bytes(self, response: Any, chunk_size: int):
        aiter_bytes = getattr(response, "aiter_bytes", None)
        if aiter_bytes is not None:
            async for chunk in aiter_bytes(chunk_size):
                yield chunk
            return
        iter_bytes = getattr(response, "iter_bytes", None)
        if iter_bytes is not None:
            for chunk in iter_bytes(chunk_size):
                yield chunk
            return
        content = getattr(response, "content", None)
        if isinstance(content, bytes):
            yield content
            return
        raise ValueError("attachment response has no byte iterator")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        metadata = metadata or {}
        ref_data = metadata.get("teams_reference") if isinstance(metadata, dict) else None
        ref = _ref_from_dict(ref_data) if ref_data else None
        if ref is None:
            return SendResult(success=False, error="Missing Teams conversation reference")
        last_message_id: Optional[str] = None
        for chunk in self._chunk_text(content):
            result = await self._send_activity(ref, _sdk_message_activity(chunk), activity_type="text")
            if not result.success:
                return result
            last_message_id = result.message_id
        return SendResult(success=True, message_id=last_message_id)

    async def _send_with_retry(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Any = None,
        max_retries: int = 2,
        base_delay: float = 2.0,
    ) -> SendResult:
        """Avoid replaying completed Teams chunks after a later chunk fails."""
        result = await self.send(
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=metadata,
        )
        if not result.success:
            logger.warning("[%s] Teams response delivery failed after bounded send attempt", self.name)
        return result

    def _chunk_text(self, content: str) -> List[str]:
        if _utf16_len(content) <= self._text_budget_bytes:
            return [content]
        chunks: List[str] = []
        while content:
            chunk = self._take_text_chunk(content)
            if not chunk:
                break
            chunks.append(chunk)
            content = content[len(chunk) :].lstrip()
        return chunks

    def _take_text_chunk(self, content: str) -> str:
        prefix = _prefix_within_utf16_limit(content, self._text_budget_bytes)
        if not prefix:
            return ""
        for delimiter in ("\n\n", "\r\n\r\n"):
            index = prefix.rfind(delimiter)
            if index > 0:
                return prefix[:index].rstrip()
        for delimiter in ("\n", "\r"):
            index = prefix.rfind(delimiter)
            if index > 0:
                return prefix[:index].rstrip()
        index = prefix.rfind(" ")
        if index > 0:
            return prefix[:index].rstrip()
        return prefix

    async def _send_activity(self, ref: Any, activity: Any, activity_type: str = "message") -> SendResult:
        if not self._teams_app:
            self._telemetry.increment("teams_delivery_total", {"type": activity_type, "result": "not_connected"})
            return SendResult(success=False, error="Teams app is not connected")
        started = time.perf_counter()
        result_label = "failure"
        try:
            for attempt in range(1, self._outbound_max_attempts + 1):
                try:
                    sent = await self._teams_app.activity_sender.send(activity, ref)
                    result_label = "success"
                    return SendResult(success=True, message_id=getattr(sent, "id", None), raw_response=sent)
                except Exception as exc:
                    if attempt >= self._outbound_max_attempts or not _is_retryable_exception(exc):
                        result_label = "retryable_failure" if _is_retryable_exception(exc) else "failure"
                        return SendResult(success=False, error=str(exc), retryable=_is_retryable_exception(exc))
                    await asyncio.sleep(self._retry_delay(exc, attempt))
            return SendResult(success=False, error="send failed")
        finally:
            self._telemetry.increment("teams_delivery_total", {"type": activity_type, "result": result_label})
            self._telemetry.observe("teams_delivery_duration", time.perf_counter() - started)

    def _retry_delay(self, exc: Exception, attempt: int) -> float:
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            return max(0.0, retry_after)
        return self._outbound_base_delay * attempt

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        metadata = metadata or {}
        ref_data = metadata.get("teams_reference") if isinstance(metadata, dict) else None
        ref = _ref_from_dict(ref_data) if ref_data else None
        if ref is None:
            return SendResult(success=False, error="Missing Teams conversation reference")
        return await self._send_activity(ref, _sdk_typing_activity(), activity_type="typing")


    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        metadata = metadata or {}
        safe_path = self.validate_media_delivery_path(image_path)
        if not safe_path:
            return SendResult(success=False, error="Unsafe or unsupported image path")
        try:
            data = Path(safe_path).read_bytes()
            if len(data) > self._attachment_max_bytes:
                return SendResult(success=False, error="Image exceeds Teams outbound limit", retryable=False)
            ext = Path(safe_path).suffix.lower()
            if ext not in SUPPORTED_IMAGE_EXTS:
                return SendResult(success=False, error="Unsupported image type", retryable=False)
            mime_type = mimetypes.guess_type(safe_path)[0] or "application/octet-stream"
            if not mime_type.startswith("image/"):
                return SendResult(success=False, error="Unsupported image MIME type", retryable=False)
            data_url = f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"
            return await self._send_image_url(chat_id, data_url, caption, reply_to, metadata)
        except FileNotFoundError:
            return SendResult(success=False, error="Image file not found", retryable=False)
        except OSError:
            return SendResult(success=False, error="Image read failed", retryable=False)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        metadata = metadata or {}
        if image_url.startswith("data:image/"):
            try:
                mime_type, data = _decode_data_image(image_url)
                if not any(mime_type.startswith(prefix) for prefix in SUPPORTED_IMAGE_MIME_PREFIXES):
                    return SendResult(success=False, error="Unsupported image MIME type", retryable=False)
                if len(data) > self._attachment_max_bytes:
                    return SendResult(success=False, error="Image exceeds Teams outbound limit", retryable=False)
                cache_image_from_bytes(data, ext=mimetypes.guess_extension(mime_type) or ".jpg")
            except Exception as exc:
                return SendResult(success=False, error=f"Invalid image data URL: {exc}", retryable=False)
            result = await self._send_image_url(chat_id, image_url, caption, reply_to, metadata)
            if not result.success and image_url.startswith("data:") and "Missing Teams conversation reference" not in str(result.error):
                result.error = "Teams outbound image delivery failed"
            return result
        return SendResult(success=False, error="Teams outbound images require validated local files or data URLs", retryable=False)

    async def _send_image_url(self, chat_id: str, image_url: str, caption: Optional[str], reply_to: Optional[str], metadata: Optional[Dict[str, Any]]) -> SendResult:
        metadata = metadata or {}
        ref_data = metadata.get("teams_reference") if isinstance(metadata, dict) else None
        ref = _ref_from_dict(ref_data) if ref_data else None
        if ref is None:
            return SendResult(success=False, error="Missing Teams conversation reference")
        mime_type = "image/png"
        if image_url.startswith("data:"):
            mime_type = image_url.split(";", 1)[0].split(":", 1)[1]
        activity = _sdk_message_activity(caption or "")
        attachment = _sdk_attachment_url(mime_type, image_url)
        activity.attachments = [attachment]
        result = await self._send_activity(ref, activity, activity_type="image")
        if not result.success and "413" in str(result.error):
            result.retryable = False
        return result

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        metadata = metadata or {}
        for index, (image_url, alt_text) in enumerate(images):
            result = await self.send_image(chat_id, image_url, caption=alt_text, metadata=metadata)
            if not result.success:
                logger.warning("Teams image send failed: %s", result.error)

    async def _handle_invoke(self, activity: Any) -> Any:
        if getattr(activity, "name", None) != "adaptiveCard/action":
            return InvokeResponse(status=400)
        value = getattr(activity, "value", None)
        if not isinstance(value, dict):
            return InvokeResponse(status=400)
        try:
            parsed = AdaptiveCardInvokeValue.model_validate(value)
            action = parsed.action
            data = action.data
            if not isinstance(data, dict) or data.get("kind") != "marlow.approval.v1":
                return InvokeResponse(status=400)
            nonce = str(data.get("nonce", "") or "")
            request_id = str(data.get("request_id", "") or "")
            decision = str(data.get("decision", "") or "")
            if decision not in {"approve", "deny"}:
                return InvokeResponse(status=400)
            return await self._resolve_approval(activity, request_id, nonce, decision)
        except Exception:
            return InvokeResponse(status=400)

    async def _resolve_approval(self, activity: Any, request_id: str, nonce: str, decision: str) -> Any:
        started = time.perf_counter()
        result_label = "failure"
        try:
            if not nonce or not request_id:
                self._telemetry.increment("teams_approvals_total", {"decision": decision, "result": "malformed"})
                return InvokeResponse(status=400)
            choice = "once" if decision == "approve" else "deny"
            async with self._approval_lock:
                state = self._approval_state.get(nonce)
                waiter = self._approval_waiters.get(nonce)
                if state is None or waiter is None or waiter.done():
                    self._telemetry.increment("teams_approvals_total", {"decision": decision, "result": "not_pending"})
                    return InvokeResponse(status=400)
                expires_at = state.get("expires_at")
                if isinstance(expires_at, datetime) and expires_at <= _utc_now():
                    self._telemetry.increment("teams_approvals_total", {"decision": decision, "result": "expired"})
                    return InvokeResponse(status=400)
                sender = getattr(activity, "from_", None)
                user_id = _normalize_uuid(getattr(sender, "aad_object_id", None))
                if user_id != state.get("user_id"):
                    self._telemetry.increment("teams_approvals_total", {"decision": decision, "result": "wrong_user"})
                    return InvokeResponse(status=400)
                if _normalize_uuid(self._activity_tenant_id(activity)) != state.get("tenant_id"):
                    self._telemetry.increment("teams_approvals_total", {"decision": decision, "result": "wrong_tenant"})
                    return InvokeResponse(status=400)
                if str(request_id) != state.get("request_id"):
                    self._telemetry.increment("teams_approvals_total", {"decision": decision, "result": "wrong_request"})
                    return InvokeResponse(status=400)
                source = self._build_source(activity)
                if str(source.platform.value) != state.get("platform"):
                    self._telemetry.increment("teams_approvals_total", {"decision": decision, "result": "wrong_platform"})
                    return InvokeResponse(status=400)
                if str(source.chat_id) != state.get("chat_id") or str(source.thread_id or "") != state.get("thread_id", ""):
                    self._telemetry.increment("teams_approvals_total", {"decision": decision, "result": "wrong_route"})
                    return InvokeResponse(status=400)
                session_key = str(state.get("session_key", ""))
                approval_request_id = str(state.get("request_id", ""))
            try:
                from tools.approval import resolve_gateway_approval
                count = resolve_gateway_approval(session_key, choice, request_id=approval_request_id)
                if count <= 0:
                    async with self._approval_lock:
                        self._approval_state.pop(nonce, None)
                        waiter = self._approval_waiters.pop(nonce, None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result("deny")
                    result_label = "not_pending"
                    return InvokeResponse(status=400, body={"error": "approval not pending"})
            except Exception as exc:
                logger.warning("Teams approval callback failed to resolve gateway approval: %s", exc)
                result_label = "resolution_failed"
                return InvokeResponse(status=400, body={"error": "approval resolution failed"})
            async with self._approval_lock:
                self._approval_state.pop(nonce, None)
                waiter = self._approval_waiters.pop(nonce, None)
            if waiter is not None and not waiter.done():
                waiter.set_result(choice)
            result_label = "resolved"
            return InvokeResponse(status=200, body={"success": True})
        finally:
            self._telemetry.increment("teams_approvals_total", {"decision": decision, "result": result_label})
            self._telemetry.observe("teams_approval_callback_duration", time.perf_counter() - started)


    async def request_approval(
        self,
        chat_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        metadata = metadata or {}
        nonce = secrets.token_urlsafe(32)
        ref_data = metadata.get("teams_reference") if isinstance(metadata, dict) else None
        ref = _ref_from_dict(ref_data) if ref_data else None
        if ref is None:
            self._telemetry.increment("teams_approvals_total", {"decision": "request", "result": "missing_reference"})
            return SendResult(success=False, error="Missing Teams conversation reference")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        expires_at = _utc_now() + timedelta(seconds=self._approval_timeout_seconds)
        state = {
            "platform": TEAMS_PLATFORM,
            "request_id": str(metadata.get("request_id", "") if isinstance(metadata, dict) else ""),
            "session_key": str(metadata.get("session_key", "") if isinstance(metadata, dict) else ""),
            "user_id": _normalize_uuid(metadata.get("user_id", "") if isinstance(metadata, dict) else ""),
            "tenant_id": self._tenant_id,
            "chat_id": chat_id,
            "thread_id": str(metadata.get("thread_id", "") if isinstance(metadata, dict) else ""),
            "nonce_hash": _hash_bytes(nonce.encode("utf-8")),
            "expires_at": expires_at,
        }
        async with self._approval_lock:
            self._approval_waiters[nonce] = future
            self._approval_state[nonce] = dict(state)
        card = _sdk_adaptive_card(
            body=[TextBlock(text=str(content)[:3000], wrap=True)],
            actions=[
                SubmitAction(title="Approve", data={"kind": "marlow.approval.v1", "request_id": state["request_id"], "decision": "approve", "nonce": nonce}),
                SubmitAction(title="Deny", data={"kind": "marlow.approval.v1", "request_id": state["request_id"], "decision": "deny", "nonce": nonce}),
            ],
        )
        attachment = _sdk_adaptive_card_attachment(card)
        activity = _sdk_message_activity("")
        activity.attachments = [attachment]
        result = await self._send_activity(ref, activity)
        if not result.success:
            await self._deny_local_approval(nonce, reason=result.error or "approval delivery failed")
            self._telemetry.increment("teams_approvals_total", {"decision": "request", "result": "delivery_failed"})
            return result
        self._telemetry.increment("teams_approvals_total", {"decision": "request", "result": "sent"})
        return result

    async def _deny_local_approval(self, nonce: str, reason: str = "approval delivery failed") -> None:
        async with self._approval_lock:
            state = self._approval_state.pop(nonce, None)
            waiter = self._approval_waiters.pop(nonce, None)
        if waiter is not None and not waiter.done():
            waiter.set_result("deny")
        if state:
            session_key = str(state.get("session_key") or "")
            request_id = str(state.get("request_id") or "")
            if session_key and request_id:
                try:
                    from tools.approval import resolve_gateway_approval
                    resolve_gateway_approval(session_key, "deny", request_id=request_id)
                    self._telemetry.increment("teams_approvals_total", {"decision": "deny", "result": "local_denied"})
                except Exception as exc:
                    logger.debug("Teams local approval denial failed: %s", exc)
                    self._telemetry.increment("teams_approvals_total", {"decision": "deny", "result": "local_denial_failed"})

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
        request_id: str = "",
        authorized_user_id: str = "",
        binary: bool = False,
        title: str = "Command Approval Required",
        action_intent: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        metadata = metadata or {}
        ref_data = metadata.get("teams_reference") if isinstance(metadata, dict) else None
        ref = _ref_from_dict(ref_data) if ref_data else None
        if ref is None:
            return SendResult(success=False, error="Missing Teams conversation reference")
        request_id = request_id or str(uuid.uuid4())
        command_preview = command[:2000] + ("..." if len(command) > 2000 else "")
        content = f"{title}\n\n{command_preview}\n\nReason: {description}"
        result = await self.request_approval(
            chat_id,
            content,
            metadata={
                **(metadata or {}),
                "request_id": request_id,
                "session_key": session_key,
                "user_id": _normalize_uuid(authorized_user_id),
                "thread_id": str(metadata.get("thread_id", "") if isinstance(metadata, dict) else ""),
            },
        )
        if not result.success:
            return result
        return result

    async def _fail_approval_waiters(self) -> None:
        async with self._approval_lock:
            waiters = list(self._approval_waiters.items())
            states = list(self._approval_state.values())
            self._approval_waiters.clear()
            self._approval_state.clear()
        for _, future in waiters:
            if not future.done():
                future.set_result("deny")
        for state in states:
            session_key = str(state.get("session_key") or "")
            if session_key and state.get("request_id"):
                try:
                    from tools.approval import resolve_gateway_approval
                    resolve_gateway_approval(session_key, "deny", request_id=str(state.get("request_id") or ""))
                    self._telemetry.increment("teams_approvals_total", {"decision": "deny", "result": "shutdown_denied"})
                except Exception as exc:
                    logger.debug("Teams pending approval close failed: %s", exc)
                    self._telemetry.increment("teams_approvals_total", {"decision": "deny", "result": "shutdown_denial_failed"})


def check_teams_requirements() -> bool:
    """Return True when the Teams SDK dependency is installed."""
    global TEAMS_AVAILABLE
    if TEAMS_AVAILABLE:
        return True
    try:
        from tools.lazy_deps import ensure
        ensure("platform.teams", prompt=False)
    except Exception:
        return False
    try:
        from microsoft_teams.api.activities.message.message import MessageActivityInput as _MessageActivityInput
        from microsoft_teams.api.activities.typing import TypingActivityInput as _TypingActivityInput
        from microsoft_teams.api.auth.credentials import ClientCredentials as _ClientCredentials
        from microsoft_teams.api.models.account import Account as _Account, ConversationAccount as _ConversationAccount
        from microsoft_teams.api.models.activity import Activity as _Activity
        from microsoft_teams.api.models.attachment.attachment import Attachment as _Attachment
        from microsoft_teams.api.models.attachment.card_attachment import AdaptiveCardAttachment as _AdaptiveCardAttachment
        from microsoft_teams.api.models.conversation.conversation_reference import ConversationReference as _ConversationReference
        from microsoft_teams.api.models.entity.mention_entity import MentionEntity as _MentionEntity
        from microsoft_teams.api.models.entity.message_entity import MessageEntity as _MessageEntity
        from microsoft_teams.api.models.invoke_response import InvokeResponse as _InvokeResponse
        from microsoft_teams.api.models.adaptive_card import (
            AdaptiveCardActionErrorResponse as _AdaptiveCardActionErrorResponse,
            AdaptiveCardActionMessageResponse as _AdaptiveCardActionMessageResponse,
            AdaptiveCardInvokeAction as _AdaptiveCardInvokeAction,
            AdaptiveCardInvokeValue as _AdaptiveCardInvokeValue,
        )
        from microsoft_teams.apps import App as _App
        from microsoft_teams.apps.http.adapter import HttpRequest as _HttpRequest, HttpResponse as _HttpResponse, HttpServerAdapter as _HttpServerAdapter
        from microsoft_teams.apps.http.http_server import HttpServer as _HttpServer
        from microsoft_teams.common import Client as _Client, ClientOptions as _ClientOptions
        from microsoft_teams.cards import AdaptiveCard as _AdaptiveCard, SubmitAction as _SubmitAction, TextBlock as _TextBlock
    except ImportError:
        return False
    globals().update({
        "MessageActivityInput": _MessageActivityInput,
        "TypingActivityInput": _TypingActivityInput,
        "ClientCredentials": _ClientCredentials,
        "Account": _Account,
        "ConversationAccount": _ConversationAccount,
        "Activity": _Activity,
        "Attachment": _Attachment,
        "AdaptiveCardAttachment": _AdaptiveCardAttachment,
        "ConversationReference": _ConversationReference,
        "MentionEntity": _MentionEntity,
        "MessageEntity": _MessageEntity,
        "InvokeResponse": _InvokeResponse,
        "AdaptiveCardActionErrorResponse": _AdaptiveCardActionErrorResponse,
        "AdaptiveCardActionMessageResponse": _AdaptiveCardActionMessageResponse,
        "AdaptiveCardInvokeAction": _AdaptiveCardInvokeAction,
        "AdaptiveCardInvokeValue": _AdaptiveCardInvokeValue,
        "App": _App,
        "HttpRequest": _HttpRequest,
        "HttpResponse": _HttpResponse,
        "HttpServerAdapter": _HttpServerAdapter,
        "HttpServer": _HttpServer,
        "Client": _Client,
        "ClientOptions": _ClientOptions,
        "AdaptiveCard": _AdaptiveCard,
        "SubmitAction": _SubmitAction,
        "TextBlock": _TextBlock,
    })
    TEAMS_AVAILABLE = True
    return True


def _is_connected(config: PlatformConfig) -> bool:
    return bool(
        config.extra.get("enabled")
        and config.extra.get("client_id")
        and config.extra.get("tenant_id")
        and (config.extra.get("client_secret") or os.getenv("TEAMS_CLIENT_SECRET"))
    )


def _validate_config(config: PlatformConfig) -> bool:
    try:
        TeamsPlatformAdapter(config)._validate_config()
        return True
    except Exception:
        return False


def _build_adapter(config: PlatformConfig):
    return TeamsPlatformAdapter(config)


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> Optional[dict]:
    teams_cfg = yaml_cfg.get("teams") if isinstance(yaml_cfg, dict) else None
    if not isinstance(teams_cfg, dict):
        return None
    extra = dict(platform_cfg.get("extra", {}))
    for key, value in teams_cfg.items():
        extra[key] = value
    return {"extra": extra}


def register(ctx) -> None:
    """Register the Teams platform with Marlow."""
    ctx.register_platform(
        name="teams",
        label="Microsoft Teams",
        adapter_factory=_build_adapter,
        check_fn=check_teams_requirements,
        validate_config=_validate_config,
        is_connected=_is_connected,
        required_env=["TEAMS_CLIENT_SECRET"],
        install_hint="pip install 'microsoft-teams-apps==2.0.16'",
        setup_fn=_setup_teams,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="TEAMS_ALLOWED_USERS",
        allow_all_env="TEAMS_ALLOW_ALL_USERS",
        max_message_length=DEFAULT_TEXT_BUDGET_BYTES,
        emoji="💬",
        allow_update_command=True,
    )


def _setup_teams() -> None:
    from marlow_cli.config import get_env_value, save_env_value
    from marlow_cli.gateway import prompt, prompt_yes_no, print_info, print_success, print_warning

    print_info("Microsoft Teams is single-tenant and uses Bot Framework /api/messages.")
    print_info("Public callback: https://<operator-host>/api/messages")
    print_info("The local listener is HTTP; Marlow does not provision DNS, TLS, tunnels, or Azure resources.")
    print_info("Supported: personal chats, group chats, and standard Teams channel threads.")
    print_info("Unsupported: meetings, private/shared channels, proactive delivery, Graph ingestion.")
    client_id = prompt("Microsoft Entra application client ID")
    tenant_id = prompt("Tenant ID")
    secret = prompt("Client secret", password=True)
    if not client_id or not tenant_id or not secret:
        return
    save_env_value("TEAMS_CLIENT_SECRET", secret)
    print_success("TEAMS_CLIENT_SECRET saved")
    allowed_users = prompt("Allowed Azure AD object IDs (comma-separated, leave empty for deny-by-default)")
    if allowed_users:
        save_env_value("TEAMS_ALLOWED_USERS", allowed_users.replace(" ", ""))
    else:
        print_warning("No allowlist set; unpaired users will be denied by default.")
    if prompt_yes_no("Allow all authenticated users in the configured tenant?", False):
        save_env_value("TEAMS_ALLOW_ALL_USERS", "true")
    print_info("Configure config.yaml teams.enabled: true, teams.client_id, teams.tenant_id, teams.host, teams.port.")


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _prefix_within_utf16_limit(text: str, limit: int) -> str:
    if _utf16_len(text) <= limit:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _utf16_len(text[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    response = getattr(exc, "response", None)
    value = None
    if response is not None:
        value = getattr(response, "headers", {}).get("Retry-After") or getattr(response, "headers", {}).get("retry-after")
    if value is None:
        match = re.search(r"retry-after[:=]\s*([\d.]+)", str(exc), flags=re.IGNORECASE)
        value = match.group(1).strip() if match else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(value) - _utc_now()).total_seconds())
    except Exception:
        return None


def _decode_data_image(data_url: str) -> Tuple[str, bytes]:
    if not data_url.startswith("data:image/"):
        raise ValueError("unsupported data URL scheme")
    header, encoded = data_url.split(",", 1)
    if ";base64" not in header.lower():
        raise ValueError("only base64 data URLs are supported")
    mime_type = header.split(":", 1)[1].split(";", 1)[0].lower()
    return mime_type, base64.b64decode(encoded, validate=True)


def _is_retryable_exception(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(token in message for token in ("timeout", "429", "500", "502", "503", "504", "temporary", "rate limit"))


try:
    from aiohttp import web
except ImportError:  # pragma: no cover
    web = None  # type: ignore[assignment]

try:
    from gateway.status import acquire_scoped_lock, release_scoped_lock
except Exception:  # pragma: no cover
    def acquire_scoped_lock(scope: str, identity: str):
        return True, None

    def release_scoped_lock(scope: str, identity: str):
        return None

try:
    from gateway.session import SessionSource
except Exception:  # pragma: no cover
    class SessionSource:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

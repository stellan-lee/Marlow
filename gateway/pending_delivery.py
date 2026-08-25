"""Request-scoped cross-conversation delivery confirmation state."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from marlow_cli.config import get_marlow_home

from .outbound import OutboundEnvelope, StagedAttachment
from .turn_context import ActorIdentity, ConversationRoute


class PendingDeliveryState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


@dataclass(slots=True)
class PendingDelivery:
    request_id: str
    turn_id: str
    session_key: str
    origin: ConversationRoute
    actor: ActorIdentity
    destination: ConversationRoute
    payload_digest: str
    envelope: OutboundEnvelope
    created_at: float
    expires_at: float
    state: PendingDeliveryState = PendingDeliveryState.PENDING
    event: threading.Event = field(default_factory=threading.Event)
    result: Optional[dict] = None


_lock = threading.RLock()
_entries: dict[str, PendingDelivery] = {}
_session_index: dict[str, set[str]] = {}
_gateway_notify_cbs: dict[str, Callable[[dict], None]] = {}
_DEFAULT_TIMEOUT_SECONDS = 10 * 60.0


def payload_digest(text: str, attachments: tuple[StagedAttachment, ...]) -> str:
    h = hashlib.sha256()
    h.update(text.encode("utf-8", errors="replace"))
    for attachment in attachments:
        h.update(b"\0")
        h.update(attachment.path.encode("utf-8", errors="replace"))
        h.update(str(attachment.size).encode("ascii"))
        h.update(attachment.digest.encode("utf-8", errors="replace"))
    return h.hexdigest()


def _spool_dir(request_id: str) -> Path:
    return get_marlow_home() / "pending_deliveries" / request_id


def _unique_spool_name(destination_dir: Path, source: Path) -> str:
    base = source.name or "attachment.bin"
    stem = Path(base).stem
    suffix = Path(base).suffix
    candidate = base
    counter = 1
    while (destination_dir / candidate).exists():
        candidate = f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def stage_attachment(path: str, request_id: str) -> StagedAttachment:
    source = Path(path)
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Media file not found: {path}")
    destination_dir = _spool_dir(request_id)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_name = _unique_spool_name(destination_dir, source)
    destination = destination_dir / destination_name
    shutil.copy2(source, destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    size, digest = hash_file(str(destination))
    return StagedAttachment(
        path=str(destination),
        name=destination_name,
        size=size,
        digest=digest,
        media_type=file_media_type(str(destination)),
    )


def cleanup_delivery_files(request_id: str) -> None:
    shutil.rmtree(_spool_dir(request_id), ignore_errors=True)


def register(entry: PendingDelivery) -> None:
    with _lock:
        _entries[entry.request_id] = entry
        _session_index.setdefault(entry.session_key, set()).add(entry.request_id)


def get(request_id: str) -> Optional[PendingDelivery]:
    with _lock:
        return _entries.get(request_id)


def register_gateway_notify(session_key: str, cb: Callable[[dict], None]) -> None:
    with _lock:
        _gateway_notify_cbs[session_key] = cb


def unregister_gateway_notify(session_key: str) -> None:
    with _lock:
        _gateway_notify_cbs.pop(session_key, None)
        ids = _session_index.pop(session_key, set())
        entries = [_entries.pop(request_id, None) for request_id in ids]
    for entry in entries:
        if entry is None:
            continue
        entry.event.set()
        cleanup_delivery_files(entry.request_id)
        if entry.state in {PendingDeliveryState.SENT, PendingDeliveryState.FAILED}:
            continue
        entry.state = PendingDeliveryState.CANCELLED
        if entry.result is None:
            entry.result = {
                "success": False,
                "cancelled": True,
                "delivery_id": entry.request_id,
                "message": "The gateway session ended before this delivery was approved.",
            }


def request_confirmation(
    entry: PendingDelivery,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> Optional[dict]:
    register(entry)
    attachments = [
        {
            "name": attachment.name,
            "size": attachment.size,
            "digest": attachment.digest[:16],
            "media_type": attachment.media_type,
        }
        for attachment in entry.envelope.attachments
    ]
    payload = {
        "request_id": entry.request_id,
        "destination": entry.destination.public_label(),
        "preview": entry.envelope.text,
        "attachment_count": len(attachments),
        "attachments": attachments,
        "expires_in": max(int(entry.expires_at - time.time()), 0),
    }
    with _lock:
        cb = _gateway_notify_cbs.get(entry.session_key)
    if cb is None:
        entry.state = PendingDeliveryState.CANCELLED
        entry.event.set()
        cleanup_delivery_files(entry.request_id)
        _forget(entry.request_id)
        return {
            "success": False,
            "cancelled": True,
            "delivery_id": entry.request_id,
            "message": "Delivery confirmation is unavailable in this execution context.",
        }
    try:
        cb(payload)
    except Exception as exc:
        cancel(entry.request_id)
        return {
            "success": False,
            "cancelled": True,
            "delivery_id": entry.request_id,
            "message": f"Delivery confirmation could not be delivered: {exc}",
        }
    with _lock:
        if entry.state == PendingDeliveryState.CANCELLED and entry.result is not None:
            result = entry.result
            cleanup_delivery_files(entry.request_id)
            _forget(entry.request_id)
            return result
    return wait_for_approval(entry.request_id, timeout=timeout)


def wait_for_approval(request_id: str, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> Optional[dict]:
    entry = get(request_id)
    if entry is None:
        return {"success": False, "policy_denied": True, "reason": "missing_delivery_grant"}

    deadline = time.time() + max(timeout, 0.0)
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            _expire(request_id)
            return {
                "success": False,
                "timed_out": True,
                "delivery_id": request_id,
                "message": "Delivery confirmation expired. Nothing was sent.",
            }
        if entry.event.wait(timeout=min(1.0, remaining)):
            with _lock:
                if entry.result is not None:
                    result = entry.result
                    if entry.state in {PendingDeliveryState.SENT, PendingDeliveryState.FAILED}:
                        cleanup_delivery_files(entry.request_id)
                        _forget(entry.request_id)
                    return result
                if entry.state == PendingDeliveryState.APPROVED:
                    return {
                        "success": True,
                        "approved": True,
                        "delivery_id": request_id,
                    }
                if entry.state == PendingDeliveryState.CANCELLED:
                    return {
                        "success": False,
                        "cancelled": True,
                        "delivery_id": request_id,
                        "message": "The user cancelled this delivery. Do not retry unless the user asks again.",
                    }
                if entry.state == PendingDeliveryState.EXPIRED:
                    return {
                        "success": False,
                        "timed_out": True,
                        "delivery_id": request_id,
                        "message": "Delivery confirmation expired. Nothing was sent.",
                    }
            return {"success": False, "cancelled": True, "delivery_id": request_id}


def _expire(request_id: str) -> Optional[PendingDelivery]:
    with _lock:
        entry = _entries.get(request_id)
        if entry is None or entry.state not in {
            PendingDeliveryState.PENDING,
            PendingDeliveryState.APPROVED,
        }:
            return entry
        entry.state = PendingDeliveryState.EXPIRED
        entry.result = {
            "success": False,
            "timed_out": True,
            "delivery_id": request_id,
            "message": "Delivery confirmation expired. Nothing was sent.",
        }
    entry.event.set()
    cleanup_delivery_files(entry.request_id)
    _forget(entry.request_id)
    return entry


def approve(
    request_id: str,
    *,
    actor: ActorIdentity,
    route: ConversationRoute,
    session_key: str,
) -> Optional[dict]:
    with _lock:
        entry = _entries.get(request_id)
        if entry is None:
            return None
        if entry.state != PendingDeliveryState.PENDING:
            return {"success": False, "already_resolved": True, "delivery_id": request_id}
        if time.time() > entry.expires_at:
            entry.state = PendingDeliveryState.EXPIRED
            entry.result = {
                "success": False,
                "timed_out": True,
                "delivery_id": request_id,
                "message": "Delivery confirmation expired. Nothing was sent.",
            }
            return {"success": False, "timed_out": True, "delivery_id": request_id}
        if entry.session_key != session_key or entry.origin != route:
            return {"success": False, "route_mismatch": True, "delivery_id": request_id}
        if not entry.actor.matches(actor):
            return {"success": False, "actor_mismatch": True, "delivery_id": request_id}
        entry.state = PendingDeliveryState.APPROVED
    entry.event.set()
    return {"success": True, "delivery_id": request_id}


def claim(request_id: str) -> Optional[OutboundEnvelope]:
    with _lock:
        entry = _entries.get(request_id)
        if entry is None:
            return None
        if entry.state != PendingDeliveryState.APPROVED:
            return None
        entry.state = PendingDeliveryState.SENDING
        return entry.envelope


def finish(request_id: str, *, state: PendingDeliveryState, result: Optional[dict] = None) -> None:
    with _lock:
        entry = _entries.get(request_id)
        if entry is None:
            return
        if entry.state != PendingDeliveryState.SENDING and state not in {
            PendingDeliveryState.CANCELLED,
            PendingDeliveryState.EXPIRED,
        }:
            return
        if state == PendingDeliveryState.SENT:
            entry.state = PendingDeliveryState.SENT
        elif state == PendingDeliveryState.FAILED:
            entry.state = PendingDeliveryState.FAILED
        else:
            entry.state = state
        if result is not None:
            entry.result = result
    entry.event.set()
    if state in {PendingDeliveryState.SENT, PendingDeliveryState.FAILED}:
        cleanup_delivery_files(entry.request_id)
        _forget(entry.request_id)


def cancel(request_id: str) -> bool:
    with _lock:
        entry = _entries.get(request_id)
        if entry is None or entry.state not in {
            PendingDeliveryState.PENDING,
            PendingDeliveryState.APPROVED,
        }:
            return False
        entry.state = PendingDeliveryState.CANCELLED
        entry.result = {
            "success": False,
            "cancelled": True,
            "delivery_id": request_id,
            "message": "The user cancelled this delivery. Do not retry unless the user asks again.",
        }
    entry.event.set()
    cleanup_delivery_files(entry.request_id)
    _forget(entry.request_id)
    return True


def clear_session(session_key: str) -> int:
    with _lock:
        ids = _session_index.pop(session_key, set())
        entries = [_entries.pop(request_id, None) for request_id in ids]
    cancelled = 0
    for entry in entries:
        if entry is None or entry.state not in {
            PendingDeliveryState.PENDING,
            PendingDeliveryState.APPROVED,
        }:
            continue
        entry.state = PendingDeliveryState.CANCELLED
        if entry.result is None:
            entry.result = {
                "success": False,
                "cancelled": True,
                "delivery_id": entry.request_id,
                "message": "The gateway session ended before this delivery was approved.",
            }
        entry.event.set()
        cleanup_delivery_files(entry.request_id)
        _forget(entry.request_id, ids=ids)
        cancelled += 1
    return cancelled


def resolve_from_event(session_key: str, event) -> Optional[str]:
    cmd = event.get_command()
    if cmd in {"send-approve", "send-confirm"}:
        choice = "once"
    elif cmd in {"send-cancel", "send-deny"}:
        choice = "cancel"
    else:
        return None

    source = getattr(event, "source", None)
    if source is None:
        return "Delivery confirmation could not be resolved without source metadata."

    args = getattr(event, "get_command_args", lambda: "")()
    request_id = args.strip().split()[0] if args else None
    if not request_id:
        return "Delivery confirmation could not be resolved without a request ID."

    try:
        from .turn_context import ActorIdentity, ConversationRoute
        route = ConversationRoute.from_source(source)
        actor = ActorIdentity.from_source(source)
    except Exception:
        return "Delivery confirmation could not be resolved without route metadata."

    with _lock:
        entry = _entries.get(request_id)

    if entry is None or entry.state != PendingDeliveryState.PENDING:
        return ""
    if time.time() > entry.expires_at:
        _expire(entry.request_id)
        return ""

    if entry.session_key != session_key or entry.origin != route or not entry.actor.matches(actor):
        return ""

    if choice == "cancel":
        cancel(entry.request_id)
        return ""

    result = approve(
        entry.request_id,
        actor=actor,
        route=route,
        session_key=session_key,
    )
    if result and result.get("success"):
        return ""
    return ""


def format_confirmation_text(payload: dict) -> str:
    request_id = payload.get("request_id", "?")
    destination = payload.get("destination", "?")
    preview = str(payload.get("preview", ""))
    if len(preview) > 900:
        preview = preview[:897] + "..."
    attachment_count = int(payload.get("attachment_count") or 0)
    attachments = payload.get("attachments") or []
    lines = [
        "📨 Send message to another conversation?",
        "",
        f"Destination: `{destination}`",
        "This destination is not the current conversation.",
    ]
    if attachment_count:
        lines.append(f"Attachments: {attachment_count}")
        for attachment in attachments:
            name = attachment.get("name") or "attachment"
            size = attachment.get("size")
            digest = attachment.get("digest") or "??"
            media_type = attachment.get("media_type") or "application/octet-stream"
            lines.append(f"- `{name}` ({media_type}, {size} bytes, sha256:{digest}...)")
    lines.extend([
        "",
        "Preview:",
        "```",
        preview,
        "```",
        "",
        "This sends once to the destination above. Nothing has been sent yet.",
        "Reply `/send-approve` to send once, or `/send-cancel` to cancel. This approval cannot be reused.",
        f"Request: `{request_id}`",
    ])
    return "\n".join(lines)


def encode_state(entry: PendingDelivery) -> str:
    return json.dumps(
        {
            "request_id": entry.request_id,
            "turn_id": entry.turn_id,
            "session_key": entry.session_key,
            "origin": entry.origin.public_label(),
            "destination": entry.destination.public_label(),
            "payload_digest": entry.payload_digest,
            "state": entry.state.value,
            "created_at": entry.created_at,
            "expires_at": entry.expires_at,
        },
        sort_keys=True,
    )


def make_request_id() -> str:
    return f"delivery-{uuid.uuid4().hex[:16]}"


def hash_file(path: str) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def file_media_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
    }.get(ext, "application/octet-stream")


def _forget(request_id: str, ids: Optional[set[str]] = None) -> None:
    with _lock:
        _entries.pop(request_id, None)
        if ids is None:
            for session_key, request_ids in list(_session_index.items()):
                request_ids.discard(request_id)
                if not request_ids:
                    _session_index.pop(session_key, None)
        else:
            for session_id in list(_session_index):
                _session_index[session_id].discard(request_id)
                if not _session_index[session_id]:
                    _session_index.pop(session_id, None)

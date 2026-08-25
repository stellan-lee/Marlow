"""Typed turn-scoped routing context for gateway executions."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .config import Platform
from .session import SessionSource


class ExecutionMode(str, Enum):
    """Broad execution mode for a gateway/tool operation."""

    INTERACTIVE = "interactive"
    SCHEDULED = "scheduled"
    SYSTEM = "system"
    LOCAL = "local"


@dataclass(frozen=True, slots=True)
class ConversationRoute:
    """Identity of a platform conversation.

    Route identity deliberately excludes reply anchors and other delivery hints.
    """

    platform: Platform
    chat_id: str
    thread_id: Optional[str] = None

    @classmethod
    def from_source(cls, source: SessionSource) -> "ConversationRoute":
        return cls(
            platform=source.platform,
            chat_id=str(source.chat_id or ""),
            thread_id=_normalize_optional_id(source.thread_id),
        )

    @classmethod
    def from_parts(
        cls,
        platform: Platform,
        chat_id: str,
        thread_id: Optional[str] = None,
    ) -> "ConversationRoute":
        return cls(
            platform=platform,
            chat_id=str(chat_id or ""),
            thread_id=_normalize_optional_id(thread_id),
        )

    def is_concrete(self) -> bool:
        return bool(self.platform and self.chat_id)

    def to_target(self) -> str:
        if self.thread_id:
            return f"{self.platform.value}:{self.chat_id}:{self.thread_id}"
        return f"{self.platform.value}:{self.chat_id}"

    def public_label(self) -> str:
        if self.thread_id:
            return f"{self.platform.value}:{self.chat_id}:{self.thread_id}"
        return f"{self.platform.value}:{self.chat_id}"


@dataclass(frozen=True, slots=True)
class ActorIdentity:
    """Authenticated actor that initiated a turn."""

    platform: Platform
    user_ids: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_source(cls, source: SessionSource) -> "ActorIdentity":
        values = []
        for value in (source.user_id, source.user_id_alt):
            if value is not None:
                text = str(value).strip()
                if text:
                    values.append(text)
        return cls(platform=source.platform, user_ids=frozenset(values))

    def matches(self, other: "ActorIdentity") -> bool:
        if self.platform != other.platform:
            return False
        if not self.user_ids or not other.user_ids:
            return False
        return bool(self.user_ids & other.user_ids)


@dataclass(frozen=True, slots=True)
class DeliveryHints:
    """Non-identity delivery hints."""

    reply_to_message_id: Optional[str] = None
    notify: bool = False
    chat_type: Optional[str] = None


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Immutable routing context for the current turn."""

    turn_id: str
    mode: ExecutionMode
    origin: Optional[ConversationRoute] = None
    actor: Optional[ActorIdentity] = None
    session_key: Optional[str] = None
    session_id: Optional[str] = None
    hints: DeliveryHints = field(default_factory=DeliveryHints)

    @classmethod
    def from_source(
        cls,
        source: SessionSource,
        *,
        turn_id: str,
        session_key: str,
        session_id: str,
        mode: ExecutionMode = ExecutionMode.INTERACTIVE,
    ) -> "TurnContext":
        origin = ConversationRoute.from_source(source)
        actor = ActorIdentity.from_source(source)
        resolved_session_key = session_key or None
        if mode == ExecutionMode.INTERACTIVE:
            if origin is None or not origin.is_concrete() or actor is None or not resolved_session_key:
                raise ValueError("Interactive turn context requires a concrete origin, actor, and session key")
        return cls(
            turn_id=turn_id,
            mode=mode,
            origin=origin,
            actor=actor,
            session_key=resolved_session_key,
            session_id=session_id or None,
            hints=DeliveryHints(
                reply_to_message_id=(
                    str(source.message_id)
                    if getattr(source, "message_id", None)
                    else None
                ),
                chat_type=getattr(source, "chat_type", None),
            ),
        )

    def is_interactive(self) -> bool:
        return self.mode == ExecutionMode.INTERACTIVE

    def require_interactive_origin(self) -> ConversationRoute:
        if not self.is_interactive() or self.origin is None:
            raise RuntimeError("Interactive turn context is missing an origin route")
        return self.origin


_current_turn: ContextVar[Optional[TurnContext]] = ContextVar(
    "marlow_current_turn",
    default=None,
)


def set_current_turn(turn: TurnContext):
    return _current_turn.set(turn)


def reset_current_turn(token):
    _current_turn.reset(token)


def get_current_turn() -> Optional[TurnContext]:
    return _current_turn.get()


def require_current_turn() -> TurnContext:
    turn = get_current_turn()
    if turn is None:
        raise RuntimeError("Missing current turn context")
    return turn


def _normalize_optional_id(value: Optional[object]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

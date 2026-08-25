"""
Gateway configuration management.

Handles loading and validating configuration for:
- Connected platforms (Telegram, Discord, Slack, Feishu, email, and webhooks)
- Home channels for each platform
- Session reset policies
- Delivery preferences
"""

import logging
import os
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable
from enum import Enum
from urllib.parse import urlparse

from marlow_cli.config import get_marlow_home
from utils import is_truthy_value

logger = logging.getLogger(__name__)


def _coerce_bool(value: Any, default: bool = True) -> bool:
    """Coerce bool-ish config values, preserving a caller-provided default."""
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        return default
    return is_truthy_value(value, default=default)


def _coerce_float(value: Any, default: float) -> float:
    """Coerce numeric config values, falling back on malformed input."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    """Coerce integer config values, falling back on malformed input."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_unauthorized_dm_behavior(value: Any, default: str = "pair") -> str:
    """Normalize unauthorized DM behavior to a supported value."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"pair", "ignore"}:
            return normalized
    return default


def _normalize_notice_delivery(value: Any, default: str = "public") -> str:
    """Normalize notice delivery mode to a supported value."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"public", "private"}:
            return normalized
    return default


def _ensure_platform_extra_dict(platforms_data: dict, name: str) -> tuple[dict, dict]:
    """Get-or-create ``platforms_data[name]`` and its nested ``extra`` dict.

    Both slots are coerced to ``{}`` if a non-dict value is encountered, so
    callers can safely write keys without type-checking.  Returns
    ``(plat_data, extra)`` for in-place mutation.
    """
    plat_data = platforms_data.setdefault(name, {})
    if not isinstance(plat_data, dict):
        plat_data = {}
        platforms_data[name] = plat_data
    extra = plat_data.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        plat_data["extra"] = extra
    return plat_data, extra


# Module-level cache for bundled platform plugin names (lives outside the
# enum so it doesn't become an accidental enum member).
_Platform__bundled_plugin_names: Optional[set] = None


class Platform(Enum):
    """Supported messaging platforms.

    Built-in platforms have explicit members.  Plugin platforms use dynamic
    members created on-demand by ``_missing_()`` so that
    ``Platform("irc")`` works without modifying this enum.  Dynamic members
    are cached in ``_value2member_map_`` for identity-stable comparisons.
    """
    LOCAL = "local"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"
    EMAIL = "email"
    WEBHOOK = "webhook"
    FEISHU = "feishu"

    @classmethod
    def _missing_(cls, value):
        """Accept unknown platform names only for known plugin adapters.

        Creates a pseudo-member cached in ``_value2member_map_`` so that
        ``Platform("irc") is Platform("irc")`` holds True (identity-stable).
        Arbitrary strings are rejected to prevent enum pollution.
        """
        if not isinstance(value, str) or not value.strip():
            return None
        # Normalise to lowercase to avoid case mismatches in config
        value = value.strip().lower()
        # Check cache first (another call may have created it already)
        if value in cls._value2member_map_:
            return cls._value2member_map_[value]

        # Only create pseudo-members for bundled plugin platforms (discovered
        # via filesystem scan) or runtime-registered plugin platforms.
        global _Platform__bundled_plugin_names
        if _Platform__bundled_plugin_names is None:
            _Platform__bundled_plugin_names = cls._scan_bundled_plugin_platforms()
        if value in _Platform__bundled_plugin_names:
            pseudo = object.__new__(cls)
            pseudo._value_ = value
            pseudo._name_ = value.upper().replace("-", "_").replace(" ", "_")
            cls._value2member_map_[value] = pseudo
            cls._member_map_[pseudo._name_] = pseudo
            return pseudo

        # Runtime-registered plugins (e.g. user-installed, discovered after
        # the enum was defined).
        try:
            from gateway.platform_registry import platform_registry
            if platform_registry.is_registered(value):
                pseudo = object.__new__(cls)
                pseudo._value_ = value
                pseudo._name_ = value.upper().replace("-", "_").replace(" ", "_")
                cls._value2member_map_[value] = pseudo
                cls._member_map_[pseudo._name_] = pseudo
                return pseudo
        except Exception:
            pass

        return None

    @classmethod
    def _scan_bundled_plugin_platforms(cls) -> set:
        """Return names of bundled platform plugins under ``plugins/platforms/``."""
        names: set = set()
        try:
            platforms_dir = Path(__file__).parent.parent / "plugins" / "platforms"
            if platforms_dir.is_dir():
                for child in platforms_dir.iterdir():
                    if (
                        child.is_dir()
                        and (child / "__init__.py").exists()
                        and (
                            (child / "plugin.yaml").exists()
                            or (child / "plugin.yml").exists()
                        )
                    ):
                        names.add(child.name.lower())
        except Exception:
            pass
        return names


# Snapshot of built-in platform values before any dynamic _missing_ lookups.
# Used to distinguish real platforms from arbitrary strings.
_BUILTIN_PLATFORM_VALUES = frozenset(m.value for m in Platform.__members__.values())


@dataclass
class HomeChannel:
    """
    Default destination for a platform.
    
    When a cron job specifies deliver="telegram" without a specific chat ID,
    messages are sent to this home channel. Thread-aware platforms may also
    store a thread/topic ID so the bare platform target routes to the exact
    conversation where /sethome was run.
    """
    platform: Platform
    chat_id: str
    name: str  # Human-readable name for display
    thread_id: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "name": self.name,
        }
        if self.thread_id:
            result["thread_id"] = self.thread_id
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HomeChannel":
        return cls(
            platform=Platform(data["platform"]),
            chat_id=str(data["chat_id"]),
            name=data.get("name", "Home"),
            thread_id=str(data["thread_id"]) if data.get("thread_id") else None,
        )


@dataclass
class AdminApprovalConfig:
    """Secure destination and identity for routed gateway approvals.

    ``user_id`` is the authorization boundary. ``chat_id`` and ``thread_id``
    control both delivery and, when ``conversation_mode`` is ``super_admin``,
    the exact conversation where that identity receives elevated authority.
    """

    enabled: bool = False
    platform: Optional[Platform] = None
    user_id: str = ""
    chat_id: str = ""
    thread_id: Optional[str] = None
    chat_type: Optional[str] = None
    conversation_mode: str = "approval_only"
    prefer_authorized_private_origin: bool = True
    allow_shared_fallback: bool = False

    @property
    def is_complete(self) -> bool:
        return bool(
            self.enabled
            and self.platform is not None
            and self.user_id.strip()
            and self.chat_id.strip()
        )

    @property
    def is_super_admin_enabled(self) -> bool:
        return self.is_complete and self.conversation_mode == "super_admin"

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "enabled": self.enabled,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "conversation_mode": self.conversation_mode,
        }
        if self.platform is not None:
            result["platform"] = self.platform.value
        if self.thread_id:
            result["thread_id"] = self.thread_id
        if self.chat_type:
            result["chat_type"] = self.chat_type
        if not self.prefer_authorized_private_origin:
            result["prefer_authorized_private_origin"] = False
        if self.allow_shared_fallback:
            result["allow_shared_fallback"] = True
        return result

    @classmethod
    def from_dict(cls, data: Any) -> "AdminApprovalConfig":
        if not isinstance(data, dict):
            return cls()
        platform = None
        raw_platform = str(data.get("platform") or "").strip().lower()
        if raw_platform:
            try:
                platform = Platform(raw_platform)
            except ValueError:
                logger.warning(
                    "Ignoring invalid approvals.admin.platform=%r",
                    raw_platform,
                )
        conversation_mode = str(
            data.get("conversation_mode") or "approval_only"
        ).strip().lower()
        if conversation_mode not in {"approval_only", "super_admin"}:
            logger.warning(
                "Ignoring invalid approvals.admin.conversation_mode=%r",
                conversation_mode,
            )
            conversation_mode = "approval_only"
        return cls(
            enabled=_coerce_bool(data.get("enabled"), False),
            platform=platform,
            user_id=str(data.get("user_id") or "").strip(),
            chat_id=str(data.get("chat_id") or "").strip(),
            thread_id=(
                str(data.get("thread_id")).strip()
                if data.get("thread_id") not in (None, "")
                else None
            ),
            chat_type=(
                str(data.get("chat_type") or "").strip().lower()
                if data.get("chat_type") not in (None, "")
                else None
            ),
            prefer_authorized_private_origin=_coerce_bool(
                data.get("prefer_authorized_private_origin", True),
                True,
            ),
            allow_shared_fallback=_coerce_bool(
                data.get("allow_shared_fallback"),
                False,
            ),
            conversation_mode=conversation_mode,
        )


@dataclass
class SessionResetPolicy:
    """
    Controls when sessions reset (lose context).
    
    Modes:
    - "daily": Reset at a specific hour each day
    - "idle": Reset after N minutes of inactivity
    - "both": Whichever triggers first (daily boundary OR idle timeout)
    - "none": Never auto-reset (context managed only by compression)
    """
    mode: str = "both"  # "daily", "idle", "both", or "none"
    at_hour: int = 4  # Hour for daily reset (0-23, local time)
    idle_minutes: int = 1440  # Minutes of inactivity before reset (24 hours)
    notify: bool = True  # Send a notification to the user when auto-reset occurs
    notify_exclude_platforms: tuple = ("webhook",)  # Platforms that don't get reset notifications
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "at_hour": self.at_hour,
            "idle_minutes": self.idle_minutes,
            "notify": self.notify,
            "notify_exclude_platforms": list(self.notify_exclude_platforms),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionResetPolicy":
        # Handle both missing keys and explicit null values (YAML null → None)
        mode = data.get("mode")
        at_hour = data.get("at_hour")
        idle_minutes = data.get("idle_minutes")
        notify = data.get("notify")
        exclude = data.get("notify_exclude_platforms")
        return cls(
            mode=mode if mode is not None else "both",
            at_hour=at_hour if at_hour is not None else 4,
            idle_minutes=idle_minutes if idle_minutes is not None else 1440,
            notify=_coerce_bool(notify, True),
            notify_exclude_platforms=tuple(exclude) if exclude is not None else ("webhook",),
        )


@dataclass
class PlatformConfig:
    """Configuration for a single messaging platform."""
    enabled: bool = False
    token: Optional[str] = None  # Bot token (Telegram, Discord)
    api_key: Optional[str] = None  # API key if different from token
    home_channel: Optional[HomeChannel] = None
    
    # Reply threading mode (Telegram/Slack)
    # - "off": Never thread replies to original message
    # - "first": Only first chunk threads to user's message (default)
    # - "all": All chunks in multi-part replies thread to user's message
    reply_to_mode: str = "first"

    # Whether the gateway is allowed to send "♻️ Gateway online" /
    # "♻ Gateway restarted" lifecycle notifications on this platform.
    # Default True preserves prior behavior. Set False on platforms used
    # by end users (e.g. Slack) where operator-flavored restart pings are
    # noise; keep True for back-channels where the operator wants them.
    gateway_restart_notification: bool = True

    # Platform-specific settings
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "enabled": self.enabled,
            "extra": self.extra,
            "reply_to_mode": self.reply_to_mode,
            "gateway_restart_notification": self.gateway_restart_notification,
        }
        if self.token:
            result["token"] = self.token
        if self.api_key:
            result["api_key"] = self.api_key
        if self.home_channel:
            result["home_channel"] = self.home_channel.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlatformConfig":
        home_channel = None
        if "home_channel" in data:
            home_channel = HomeChannel.from_dict(data["home_channel"])

        # gateway_restart_notification may be bridged into extra via the
        # shared-key loop in load_gateway_config(); check both top-level
        # and extra so YAML ``discord: gateway_restart_notification: false``
        # works without needing a separate platforms: block.
        _grn = data.get("gateway_restart_notification")
        if _grn is None:
            _grn = data.get("extra", {}).get("gateway_restart_notification")

        return cls(
            enabled=_coerce_bool(data.get("enabled"), False),
            token=data.get("token"),
            api_key=data.get("api_key"),
            home_channel=home_channel,
            reply_to_mode=data.get("reply_to_mode", "first"),
            gateway_restart_notification=_coerce_bool(_grn, True),
            extra=data.get("extra", {}),
        )


# Streaming defaults — single source of truth so both StreamingConfig and
# StreamConsumerConfig agree on the out-of-the-box edit rhythm.  Tuned for
# Telegram's ~1 edit/s flood envelope: a touch under 1s lets the cadence
# breathe without bumping into rate limits, and a smaller buffer threshold
# makes short replies feel near-instant in DMs.
DEFAULT_STREAMING_EDIT_INTERVAL: float = 0.8
DEFAULT_STREAMING_BUFFER_THRESHOLD: int = 24
DEFAULT_STREAMING_CURSOR: str = " ▉"


@dataclass
class StreamingConfig:
    """Configuration for real-time token streaming to messaging platforms."""
    enabled: bool = False
    # Transport selection:
    #   "auto"  — prefer native streaming-draft updates when the platform
    #             supports them (Telegram sendMessageDraft, Bot API 9.5+);
    #             fall back to edit-based when not.
    #   "draft" — explicitly request native drafts; falls back to edit when
    #             the platform/chat doesn't support them.
    #   "edit"  — progressive editMessageText only (legacy behaviour).
    #   "off"   — disable streaming entirely.
    #
    # Default is "auto": prefer native draft streaming on platforms that
    # support it (Telegram DMs via sendMessageDraft, Bot API 9.5+) and fall
    # back to edit-based streaming everywhere else.  This is safe as a global
    # default because adapters without draft support (Discord, Slack,
    # …) report supports_draft_streaming() == False and transparently use the
    # edit path — so "auto" never regresses non-Telegram platforms, it only
    # upgrades the chats that can render the smoother native preview.
    transport: str = "auto"
    edit_interval: float = DEFAULT_STREAMING_EDIT_INTERVAL
    buffer_threshold: int = DEFAULT_STREAMING_BUFFER_THRESHOLD
    cursor: str = DEFAULT_STREAMING_CURSOR
    # Ported from openclaw/openclaw#72038.  When >0, the final edit for
    # a long-running streamed response is delivered as a fresh message
    # if the original preview has been visible for at least this many
    # seconds, so the platform's visible timestamp reflects completion
    # time instead of the preview creation time.  Currently applied to
    # Telegram only (other platforms ignore the setting).  Default 60s
    # matches the OpenClaw rollout.  Set to 0 to disable.
    fresh_final_after_seconds: float = 60.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "transport": self.transport,
            "edit_interval": self.edit_interval,
            "buffer_threshold": self.buffer_threshold,
            "cursor": self.cursor,
            "fresh_final_after_seconds": self.fresh_final_after_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StreamingConfig":
        if not data:
            return cls()
        return cls(
            enabled=_coerce_bool(data.get("enabled"), False),
            transport=data.get("transport", "auto"),
            edit_interval=_coerce_float(
                data.get("edit_interval"), DEFAULT_STREAMING_EDIT_INTERVAL,
            ),
            buffer_threshold=_coerce_int(
                data.get("buffer_threshold"), DEFAULT_STREAMING_BUFFER_THRESHOLD,
            ),
            cursor=data.get("cursor", DEFAULT_STREAMING_CURSOR),
            fresh_final_after_seconds=_coerce_float(
                data.get("fresh_final_after_seconds"), 60.0
            ),
        )


@dataclass(frozen=True)
class MCPServeConfig:
    """Configuration for Marlow's gateway-managed MCP HTTP endpoint."""

    enabled: bool = False
    transport: str = "streamable_http"
    host: str = "127.0.0.1"
    port: int = 8765
    auth: str = "bearer"
    public_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "transport": self.transport,
            "host": self.host,
            "port": self.port,
            "auth": self.auth,
            "public_url": self.public_url,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "MCPServeConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("mcp_serve must be a mapping")

        enabled = _coerce_bool(data.get("enabled"), False)
        transport = str(data.get("transport", "streamable_http")).strip().lower()
        transport = transport.replace("-", "_")
        host = str(data.get("host", "127.0.0.1")).strip()
        auth = str(data.get("auth", "bearer")).strip().lower()
        public_url_value = data.get("public_url")
        public_url = (
            str(public_url_value).strip().rstrip("/")
            if public_url_value is not None
            else None
        )

        try:
            port = int(data.get("port", 8765))
        except (TypeError, ValueError) as exc:
            raise ValueError("mcp_serve.port must be an integer") from exc

        if enabled and transport != "streamable_http":
            raise ValueError(
                "gateway-managed mcp_serve.transport must be streamable_http"
            )
        if enabled and not host:
            raise ValueError("mcp_serve.host must not be empty")
        if enabled and auth not in {"bearer", "oauth"}:
            raise ValueError("mcp_serve.auth must be bearer or oauth")
        if enabled and auth == "oauth":
            parsed_public_url = urlparse(public_url or "")
            if (
                parsed_public_url.scheme != "https"
                or not parsed_public_url.netloc
                or parsed_public_url.username is not None
                or parsed_public_url.password is not None
                or parsed_public_url.query
                or parsed_public_url.fragment
                or parsed_public_url.path not in {"", "/"}
            ):
                raise ValueError(
                    "mcp_serve.public_url must be an HTTPS origin in oauth mode"
                )
        if not 1 <= port <= 65535:
            raise ValueError("mcp_serve.port must be between 1 and 65535")

        return cls(
            enabled=enabled,
            transport=transport,
            host=host,
            port=port,
            auth=auth,
            public_url=public_url,
        )


# -----------------------------------------------------------------------------
# Built-in platform connection checkers
# -----------------------------------------------------------------------------
# Each callable receives a ``PlatformConfig`` and returns ``True`` when the
# platform is sufficiently configured to be considered "connected".  Platforms
# that rely on the generic ``token or api_key`` check (Telegram, Discord,
# Slack and plugin-provided adapters do not need an entry here.
_PLATFORM_CONNECTED_CHECKERS: dict[Platform, Callable[[PlatformConfig], bool]] = {
    Platform.EMAIL: lambda cfg: bool(cfg.extra.get("address")),
    Platform.WEBHOOK: lambda cfg: True,
    Platform.FEISHU: lambda cfg: bool(cfg.extra.get("app_id")),
}


@dataclass
class GatewayConfig:
    """
    Main gateway configuration.
    
    Manages all platform connections, session policies, and delivery settings.
    """
    # Platform configurations
    platforms: Dict[Platform, PlatformConfig] = field(default_factory=dict)

    # Optional exact-user route for privileged action approvals.
    admin_approval: AdminApprovalConfig = field(default_factory=AdminApprovalConfig)
    
    # Session reset policies by type
    default_reset_policy: SessionResetPolicy = field(default_factory=SessionResetPolicy)
    reset_by_type: Dict[str, SessionResetPolicy] = field(default_factory=dict)
    reset_by_platform: Dict[Platform, SessionResetPolicy] = field(default_factory=dict)
    
    # Reset trigger commands
    reset_triggers: List[str] = field(default_factory=lambda: ["/new", "/reset"])

    # User-defined quick commands (slash commands that bypass the agent loop)
    quick_commands: Dict[str, Any] = field(default_factory=dict)
    
    # Storage paths
    sessions_dir: Path = field(default_factory=lambda: get_marlow_home() / "sessions")
    
    # Delivery settings
    always_log_local: bool = True  # Always save cron outputs to local files
    # Drop outbound "silence narration" messages (e.g. *(silent)*, 🔇, a bare
    # ".") pre-send. These are model hallucinations emitted when a persona has
    # nothing actionable to say; in bot-to-bot channels they mirror back and
    # forth, burning tokens and crashing models. Substrate-level guard that
    # survives SOUL.md/prompt drift across providers. Opt out with False for
    # raw passthrough.
    filter_silence_narration: bool = True

    # STT settings
    stt_enabled: bool = True  # Whether to auto-transcribe inbound voice messages

    # Session isolation in shared chats
    group_sessions_per_user: bool = True  # Isolate group/channel sessions per participant when user IDs are available
    thread_sessions_per_user: bool = False  # When False (default), threads are shared across all participants

    # Unauthorized DM policy
    unauthorized_dm_behavior: str = "pair"  # "pair" or "ignore"

    # Streaming configuration
    streaming: StreamingConfig = field(default_factory=StreamingConfig)

    # Gateway-managed Marlow MCP Streamable HTTP endpoint.
    mcp_serve: MCPServeConfig = field(default_factory=MCPServeConfig)

    # Session store pruning: drop SessionEntry records older than this many
    # days from the in-memory dict and sessions.json.  Keeps the store from
    # growing unbounded in gateways serving many chats/threads/users over
    # months.  Pruning is invisible to users — if they resume, they get a
    # fresh session exactly as if the reset policy had fired.  0 = disabled.
    session_store_max_age_days: int = 90

    def get_connected_platforms(self) -> List[Platform]:
        """Return list of platforms that are enabled and configured."""
        connected = []
        for platform, config in self.platforms.items():
            if not config.enabled:
                continue
            if self._is_platform_connected(platform, config):
                connected.append(platform)
        return connected

    def _is_platform_connected(self, platform: Platform, config: PlatformConfig) -> bool:
        """Check whether a single platform is sufficiently configured."""
        # Generic token/api_key auth covers Telegram, Discord, Slack, etc.
        if config.token or config.api_key:
            return True

        # Platform-specific check
        checker = _PLATFORM_CONNECTED_CHECKERS.get(platform)
        if checker is not None:
            return checker(config)

        # Plugin-registered platforms
        try:
            from gateway.platform_registry import platform_registry
            entry = platform_registry.get(platform.value)
            if entry:
                if entry.is_connected is not None:
                    return entry.is_connected(config)
                if entry.validate_config is not None:
                    return entry.validate_config(config)
                return True
        except Exception:
            pass  # Registry not yet initialised during early import

        return False
    
    def get_home_channel(self, platform: Platform) -> Optional[HomeChannel]:
        """Get the home channel for a platform."""
        config = self.platforms.get(platform)
        if config:
            return config.home_channel
        return None
    
    def get_reset_policy(
        self, 
        platform: Optional[Platform] = None,
        session_type: Optional[str] = None
    ) -> SessionResetPolicy:
        """
        Get the appropriate reset policy for a session.
        
        Priority: platform override > type override > default
        """
        # Platform-specific override takes precedence
        if platform and platform in self.reset_by_platform:
            return self.reset_by_platform[platform]
        
        # Type-specific override (dm, group, thread)
        if session_type and session_type in self.reset_by_type:
            return self.reset_by_type[session_type]
        
        return self.default_reset_policy
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "platforms": {
                p.value: c.to_dict() for p, c in self.platforms.items()
            },
            "admin_approval": self.admin_approval.to_dict(),
            "default_reset_policy": self.default_reset_policy.to_dict(),
            "reset_by_type": {
                k: v.to_dict() for k, v in self.reset_by_type.items()
            },
            "reset_by_platform": {
                p.value: v.to_dict() for p, v in self.reset_by_platform.items()
            },
            "reset_triggers": self.reset_triggers,
            "quick_commands": self.quick_commands,
            "sessions_dir": str(self.sessions_dir),
            "always_log_local": self.always_log_local,
            "filter_silence_narration": self.filter_silence_narration,
            "stt_enabled": self.stt_enabled,
            "group_sessions_per_user": self.group_sessions_per_user,
            "thread_sessions_per_user": self.thread_sessions_per_user,
            "unauthorized_dm_behavior": self.unauthorized_dm_behavior,
            "streaming": self.streaming.to_dict(),
            "mcp_serve": self.mcp_serve.to_dict(),
            "session_store_max_age_days": self.session_store_max_age_days,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GatewayConfig":
        platforms = {}
        for platform_name, platform_data in data.get("platforms", {}).items():
            try:
                platform = Platform(platform_name)
                platforms[platform] = PlatformConfig.from_dict(platform_data)
            except ValueError:
                pass  # Skip unknown platforms
        
        reset_by_type = {}
        for type_name, policy_data in data.get("reset_by_type", {}).items():
            reset_by_type[type_name] = SessionResetPolicy.from_dict(policy_data)
        
        reset_by_platform = {}
        for platform_name, policy_data in data.get("reset_by_platform", {}).items():
            try:
                platform = Platform(platform_name)
                reset_by_platform[platform] = SessionResetPolicy.from_dict(policy_data)
            except ValueError:
                pass
        
        default_policy = SessionResetPolicy()
        if "default_reset_policy" in data:
            default_policy = SessionResetPolicy.from_dict(data["default_reset_policy"])
        
        sessions_dir = get_marlow_home() / "sessions"
        if "sessions_dir" in data:
            sessions_dir = Path(data["sessions_dir"])
        
        quick_commands = data.get("quick_commands", {})
        if not isinstance(quick_commands, dict):
            quick_commands = {}

        stt_enabled = data.get("stt_enabled")
        if stt_enabled is None:
            stt_enabled = data.get("stt", {}).get("enabled") if isinstance(data.get("stt"), dict) else None

        group_sessions_per_user = data.get("group_sessions_per_user")
        thread_sessions_per_user = data.get("thread_sessions_per_user")
        unauthorized_dm_behavior = _normalize_unauthorized_dm_behavior(
            data.get("unauthorized_dm_behavior"),
            "pair",
        )

        try:
            session_store_max_age_days = int(data.get("session_store_max_age_days", 90))
            session_store_max_age_days = max(session_store_max_age_days, 0)
        except (TypeError, ValueError):
            session_store_max_age_days = 90

        return cls(
            platforms=platforms,
            admin_approval=AdminApprovalConfig.from_dict(
                data.get("admin_approval", {})
            ),
            default_reset_policy=default_policy,
            reset_by_type=reset_by_type,
            reset_by_platform=reset_by_platform,
            reset_triggers=data.get("reset_triggers", ["/new", "/reset"]),
            quick_commands=quick_commands,
            sessions_dir=sessions_dir,
            always_log_local=_coerce_bool(data.get("always_log_local"), True),
            filter_silence_narration=_coerce_bool(
                data.get("filter_silence_narration"), True
            ),
            stt_enabled=_coerce_bool(stt_enabled, True),
            group_sessions_per_user=_coerce_bool(group_sessions_per_user, True),
            thread_sessions_per_user=_coerce_bool(thread_sessions_per_user, False),
            unauthorized_dm_behavior=unauthorized_dm_behavior,
            streaming=StreamingConfig.from_dict(data.get("streaming", {})),
            mcp_serve=MCPServeConfig.from_dict(data.get("mcp_serve")),
            session_store_max_age_days=session_store_max_age_days,
        )

    def get_unauthorized_dm_behavior(self, platform: Optional[Platform] = None) -> str:
        """Return the effective unauthorized-DM behavior for a platform."""
        if platform:
            platform_cfg = self.platforms.get(platform)
            if platform_cfg and "unauthorized_dm_behavior" in platform_cfg.extra:
                return _normalize_unauthorized_dm_behavior(
                    platform_cfg.extra.get("unauthorized_dm_behavior"),
                    self.unauthorized_dm_behavior,
                )
        return self.unauthorized_dm_behavior

    def get_notice_delivery(self, platform: Optional[Platform] = None) -> str:
        """Return the effective notice-delivery mode for a platform."""
        if platform:
            platform_cfg = self.platforms.get(platform)
            if platform_cfg and "notice_delivery" in platform_cfg.extra:
                return _normalize_notice_delivery(
                    platform_cfg.extra.get("notice_delivery"),
                    "public",
                )
        return "public"


def load_gateway_config() -> GatewayConfig:
    """
    Load gateway configuration from multiple sources.

    Priority (highest to lowest):
    1. Environment variables
    2. ~/.marlow/config.yaml (primary user-facing config)
    3. ~/.marlow/gateway.json (legacy — provides defaults under config.yaml)
    4. Built-in defaults
    """
    _home = get_marlow_home()
    gw_data: dict = {}

    # Legacy fallback: gateway.json provides the base layer.
    # config.yaml keys always win when both specify the same setting.
    gateway_json_path = _home / "gateway.json"
    if gateway_json_path.exists():
        try:
            with open(gateway_json_path, "r", encoding="utf-8") as f:
                gw_data = json.load(f) or {}
            logger.info(
                "Loaded legacy %s — consider moving settings to config.yaml",
                gateway_json_path,
            )
        except Exception as e:
            logger.warning("Failed to load %s: %s", gateway_json_path, e)

    # Primary source: config.yaml
    try:
        import yaml
        config_yaml_path = _home / "config.yaml"
        if config_yaml_path.exists():
            with open(config_yaml_path, encoding="utf-8") as f:
                yaml_cfg = yaml.safe_load(f) or {}

            # Map config.yaml keys → GatewayConfig.from_dict() schema.
            # Each key overwrites whatever gateway.json may have set.
            sr = yaml_cfg.get("session_reset")
            if sr and isinstance(sr, dict):
                gw_data["default_reset_policy"] = sr

            qc = yaml_cfg.get("quick_commands")
            if qc is not None:
                if isinstance(qc, dict):
                    gw_data["quick_commands"] = qc
                else:
                    logger.warning(
                        "Ignoring invalid quick_commands in config.yaml "
                        "(expected mapping, got %s)",
                        type(qc).__name__,
                    )

            stt_cfg = yaml_cfg.get("stt")
            if isinstance(stt_cfg, dict):
                gw_data["stt"] = stt_cfg

            if "group_sessions_per_user" in yaml_cfg:
                gw_data["group_sessions_per_user"] = yaml_cfg["group_sessions_per_user"]

            if "thread_sessions_per_user" in yaml_cfg:
                gw_data["thread_sessions_per_user"] = yaml_cfg["thread_sessions_per_user"]

            streaming_cfg = yaml_cfg.get("streaming")
            if not isinstance(streaming_cfg, dict):
                # Fall back to nested gateway.streaming written by
                # ``marlow config set gateway.streaming.*``
                streaming_cfg = yaml_cfg.get("gateway", {}).get("streaming")
            if isinstance(streaming_cfg, dict):
                gw_data["streaming"] = streaming_cfg

            if "mcp_serve" in yaml_cfg:
                gw_data["mcp_serve"] = yaml_cfg["mcp_serve"]

            if "reset_triggers" in yaml_cfg:
                gw_data["reset_triggers"] = yaml_cfg["reset_triggers"]

            if "always_log_local" in yaml_cfg:
                gw_data["always_log_local"] = yaml_cfg["always_log_local"]

            if "filter_silence_narration" in yaml_cfg:
                gw_data["filter_silence_narration"] = yaml_cfg[
                    "filter_silence_narration"
                ]

            approvals_cfg = yaml_cfg.get("approvals")
            if isinstance(approvals_cfg, dict):
                admin_cfg = approvals_cfg.get("admin")
                if admin_cfg is not None:
                    gw_data["admin_approval"] = admin_cfg

            if "unauthorized_dm_behavior" in yaml_cfg:
                gw_data["unauthorized_dm_behavior"] = _normalize_unauthorized_dm_behavior(
                    yaml_cfg.get("unauthorized_dm_behavior"),
                    "pair",
                )

            # Merge platform config into gw_data so runtime-only settings under
            # ``gateway.platforms`` are loaded the same way as top-level
            # ``platforms``. Merge nested first so top-level config keeps
            # precedence, matching the existing gateway.streaming fallback.
            gateway_cfg = yaml_cfg.get("gateway")
            gateway_platforms = gateway_cfg.get("platforms") if isinstance(gateway_cfg, dict) else None
            platforms_data = gw_data.setdefault("platforms", {})
            if not isinstance(platforms_data, dict):
                platforms_data = {}
                gw_data["platforms"] = platforms_data

            def _merge_platform_map(source_platforms: Any) -> None:
                if not isinstance(source_platforms, dict):
                    return
                for plat_name, plat_block in source_platforms.items():
                    if not isinstance(plat_block, dict):
                        continue
                    existing = platforms_data.get(plat_name, {})
                    if not isinstance(existing, dict):
                        existing = {}
                    # Deep-merge extra dicts so gateway.json defaults survive
                    merged_extra = {**existing.get("extra", {}), **plat_block.get("extra", {})}
                    if "enabled" in plat_block:
                        merged_extra["_enabled_explicit"] = True
                    merged = {**existing, **plat_block}
                    if merged_extra:
                        merged["extra"] = merged_extra
                    platforms_data[plat_name] = merged

            _merge_platform_map(gateway_platforms)
            _merge_platform_map(yaml_cfg.get("platforms"))
            if platforms_data:
                gw_data["platforms"] = platforms_data
            # Iterate built-in platforms plus any registered plugin platforms
            # so plugin authors get the same shared-key bridging (#24836).
            try:
                from marlow_cli.plugins import discover_plugins
                discover_plugins()  # idempotent
                from gateway.platform_registry import platform_registry as _pr
            except Exception as e:
                logger.debug("plugin discovery skipped: %s", e)
                _pr = None

            _shared_loop_targets: list = list(Platform)
            if _pr is not None:
                for _entry in _pr.plugin_entries():
                    try:
                        _plat = Platform(_entry.name)
                    except (ValueError, KeyError):
                        continue
                    if _plat not in _shared_loop_targets:
                        _shared_loop_targets.append(_plat)

            for plat in _shared_loop_targets:
                if plat == Platform.LOCAL:
                    continue
                platform_cfg = yaml_cfg.get(plat.value)
                if not isinstance(platform_cfg, dict):
                    continue
                # Collect bridgeable keys from this platform section
                bridged = {}
                if "unauthorized_dm_behavior" in platform_cfg:
                    bridged["unauthorized_dm_behavior"] = _normalize_unauthorized_dm_behavior(
                        platform_cfg.get("unauthorized_dm_behavior"),
                        gw_data.get("unauthorized_dm_behavior", "pair"),
                    )
                if "notice_delivery" in platform_cfg:
                    bridged["notice_delivery"] = _normalize_notice_delivery(
                        platform_cfg.get("notice_delivery"),
                        "public",
                    )
                if "reply_prefix" in platform_cfg:
                    bridged["reply_prefix"] = platform_cfg["reply_prefix"]
                if "reply_in_thread" in platform_cfg:
                    bridged["reply_in_thread"] = platform_cfg["reply_in_thread"]
                if "require_mention" in platform_cfg:
                    bridged["require_mention"] = platform_cfg["require_mention"]
                if plat == Platform.TELEGRAM and "allowed_chats" in platform_cfg:
                    bridged["allowed_chats"] = platform_cfg["allowed_chats"]
                if plat == Platform.TELEGRAM and "group_allowed_chats" in platform_cfg:
                    bridged["group_allowed_chats"] = platform_cfg["group_allowed_chats"]
                if plat == Platform.TELEGRAM and "allowed_topics" in platform_cfg:
                    bridged["allowed_topics"] = platform_cfg["allowed_topics"]
                if "free_response_channels" in platform_cfg:
                    bridged["free_response_channels"] = platform_cfg["free_response_channels"]
                if "mention_patterns" in platform_cfg:
                    bridged["mention_patterns"] = platform_cfg["mention_patterns"]
                if "exclusive_bot_mentions" in platform_cfg:
                    bridged["exclusive_bot_mentions"] = platform_cfg["exclusive_bot_mentions"]
                if plat == Platform.TELEGRAM and "observe_unmentioned_group_messages" in platform_cfg:
                    bridged["observe_unmentioned_group_messages"] = platform_cfg["observe_unmentioned_group_messages"]
                if "dm_policy" in platform_cfg:
                    bridged["dm_policy"] = platform_cfg["dm_policy"]
                if "allow_from" in platform_cfg:
                    bridged["allow_from"] = platform_cfg["allow_from"]
                if "allow_admin_from" in platform_cfg:
                    bridged["allow_admin_from"] = platform_cfg["allow_admin_from"]
                if "user_allowed_commands" in platform_cfg:
                    bridged["user_allowed_commands"] = platform_cfg["user_allowed_commands"]
                if "group_policy" in platform_cfg:
                    bridged["group_policy"] = platform_cfg["group_policy"]
                if "group_allow_from" in platform_cfg:
                    bridged["group_allow_from"] = platform_cfg["group_allow_from"]
                if "group_allow_admin_from" in platform_cfg:
                    bridged["group_allow_admin_from"] = platform_cfg["group_allow_admin_from"]
                if "group_user_allowed_commands" in platform_cfg:
                    bridged["group_user_allowed_commands"] = platform_cfg["group_user_allowed_commands"]
                if "channel_prompts" in platform_cfg:
                    channel_prompts = platform_cfg["channel_prompts"]
                    if isinstance(channel_prompts, dict):
                        bridged["channel_prompts"] = {str(k): v for k, v in channel_prompts.items()}
                    else:
                        bridged["channel_prompts"] = channel_prompts
                if "gateway_restart_notification" in platform_cfg:
                    bridged["gateway_restart_notification"] = platform_cfg["gateway_restart_notification"]
                enabled_was_explicit = "enabled" in platform_cfg
                if not bridged and not enabled_was_explicit:
                    continue
                plat_data, extra = _ensure_platform_extra_dict(platforms_data, plat.value)
                if enabled_was_explicit:
                    plat_data["enabled"] = platform_cfg["enabled"]
                if plat == Platform.SLACK and enabled_was_explicit:
                    extra["_enabled_explicit"] = True
                extra.update(bridged)

            # Plugin-owned YAML→env config bridges (#24836).  See
            # ``PlatformEntry.apply_yaml_config_fn`` for the hook contract.
            # Order: shared-key loop (above) → this dispatch → legacy hardcoded
            # blocks (below; no-op when a hook already set their env var) →
            # ``_apply_env_overrides()`` after ``GatewayConfig.from_dict``.
            if _pr is not None:
                for entry in _pr.all_entries():
                    if entry.apply_yaml_config_fn is None:
                        continue
                    platform_cfg = yaml_cfg.get(entry.name)
                    # Fall back to the platform's block under ``platforms`` /
                    # ``gateway.platforms`` so adapter hooks still run when the
                    # user configured the platform only under those nested paths
                    # (e.g. ``platforms.discord.extra.allow_from``) and not via a
                    # top-level ``discord:`` block.
                    if not isinstance(platform_cfg, dict):
                        for _src in (gateway_platforms, yaml_cfg.get("platforms")):
                            if isinstance(_src, dict):
                                _candidate = _src.get(entry.name)
                                if isinstance(_candidate, dict):
                                    platform_cfg = _candidate
                                    break
                    if not isinstance(platform_cfg, dict):
                        continue
                    try:
                        seeded = entry.apply_yaml_config_fn(yaml_cfg, platform_cfg)
                    except Exception as e:
                        logger.debug(
                            "apply_yaml_config_fn for %s raised: %s",
                            entry.name, e,
                        )
                        continue
                    if not isinstance(seeded, dict) or not seeded:
                        continue
                    _, extra = _ensure_platform_extra_dict(platforms_data, entry.name)
                    extra.update(seeded)

            # Slack settings → env vars (env vars take precedence)
            slack_cfg = yaml_cfg.get("slack", {})
            if isinstance(slack_cfg, dict):
                if "require_mention" in slack_cfg and not os.getenv("SLACK_REQUIRE_MENTION"):
                    os.environ["SLACK_REQUIRE_MENTION"] = str(slack_cfg["require_mention"]).lower()
                if "strict_mention" in slack_cfg and not os.getenv("SLACK_STRICT_MENTION"):
                    os.environ["SLACK_STRICT_MENTION"] = str(slack_cfg["strict_mention"]).lower()
                if "allow_bots" in slack_cfg and not os.getenv("SLACK_ALLOW_BOTS"):
                    os.environ["SLACK_ALLOW_BOTS"] = str(slack_cfg["allow_bots"]).lower()
                frc = slack_cfg.get("free_response_channels")
                if frc is not None and not os.getenv("SLACK_FREE_RESPONSE_CHANNELS"):
                    if isinstance(frc, list):
                        frc = ",".join(str(v) for v in frc)
                    os.environ["SLACK_FREE_RESPONSE_CHANNELS"] = str(frc)
                if "reactions" in slack_cfg and not os.getenv("SLACK_REACTIONS"):
                    os.environ["SLACK_REACTIONS"] = str(slack_cfg["reactions"]).lower()
                # allowed_channels: if set, bot ONLY responds in these channels (whitelist)
                ac = slack_cfg.get("allowed_channels")
                if ac is not None and not os.getenv("SLACK_ALLOWED_CHANNELS"):
                    if isinstance(ac, list):
                        ac = ",".join(str(v) for v in ac)
                    os.environ["SLACK_ALLOWED_CHANNELS"] = str(ac)

            # Bridge top-level require_mention to Telegram when the telegram: section
            # does not already provide one.  Users often write "require_mention: true"
            # at the top level alongside group_sessions_per_user, expecting it to work
            # the same way (#3979).
            _tl_require_mention = yaml_cfg.get("require_mention")
            if _tl_require_mention is not None:
                _tg_section = yaml_cfg.get("telegram") or {}
                if "require_mention" not in _tg_section:
                    _tg_plat = platforms_data.setdefault(Platform.TELEGRAM.value, {})
                    _tg_extra = _tg_plat.setdefault("extra", {})
                    _tg_extra.setdefault("require_mention", _tl_require_mention)

            # Telegram settings → env vars (env vars take precedence)
            telegram_cfg = yaml_cfg.get("telegram", {})
            if isinstance(telegram_cfg, dict):
                # Bridge top-level legacy `telegram.disable_topic_auto_rename` into
                # gateway.platforms.telegram.extra so the runtime config sees it.
                # Read as a runtime-config flag, not env-var (no need for env override).
                if "disable_topic_auto_rename" in telegram_cfg:
                    _tg_plat = platforms_data.setdefault(Platform.TELEGRAM.value, {})
                    _tg_extra = _tg_plat.setdefault("extra", {})
                    _tg_extra.setdefault(
                        "disable_topic_auto_rename",
                        telegram_cfg["disable_topic_auto_rename"],
                    )
                # Prefer telegram.require_mention; fall back to the top-level shorthand.
                _effective_rm = telegram_cfg.get("require_mention", yaml_cfg.get("require_mention"))
                if _effective_rm is not None and not os.getenv("TELEGRAM_REQUIRE_MENTION"):
                    os.environ["TELEGRAM_REQUIRE_MENTION"] = str(_effective_rm).lower()
                if "mention_patterns" in telegram_cfg and not os.getenv("TELEGRAM_MENTION_PATTERNS"):
                    os.environ["TELEGRAM_MENTION_PATTERNS"] = json.dumps(telegram_cfg["mention_patterns"])
                if "exclusive_bot_mentions" in telegram_cfg and not os.getenv("TELEGRAM_EXCLUSIVE_BOT_MENTIONS"):
                    os.environ["TELEGRAM_EXCLUSIVE_BOT_MENTIONS"] = str(telegram_cfg["exclusive_bot_mentions"]).lower()
                if "guest_mode" in telegram_cfg and not os.getenv("TELEGRAM_GUEST_MODE"):
                    os.environ["TELEGRAM_GUEST_MODE"] = str(telegram_cfg["guest_mode"]).lower()
                if "observe_unmentioned_group_messages" in telegram_cfg and not os.getenv("TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES"):
                    os.environ["TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES"] = str(telegram_cfg["observe_unmentioned_group_messages"]).lower()
                frc = telegram_cfg.get("free_response_chats")
                if frc is not None and not os.getenv("TELEGRAM_FREE_RESPONSE_CHATS"):
                    if isinstance(frc, list):
                        frc = ",".join(str(v) for v in frc)
                    os.environ["TELEGRAM_FREE_RESPONSE_CHATS"] = str(frc)
                # allowed_chats: if set, bot ONLY responds in these group chats (whitelist)
                ac = telegram_cfg.get("allowed_chats")
                if ac is not None and not os.getenv("TELEGRAM_ALLOWED_CHATS"):
                    if isinstance(ac, list):
                        ac = ",".join(str(v) for v in ac)
                    os.environ["TELEGRAM_ALLOWED_CHATS"] = str(ac)
                allowed_topics = telegram_cfg.get("allowed_topics")
                if allowed_topics is not None and not os.getenv("TELEGRAM_ALLOWED_TOPICS"):
                    if isinstance(allowed_topics, list):
                        allowed_topics = ",".join(str(v) for v in allowed_topics)
                    os.environ["TELEGRAM_ALLOWED_TOPICS"] = str(allowed_topics)
                ignored_threads = telegram_cfg.get("ignored_threads")
                if ignored_threads is not None and not os.getenv("TELEGRAM_IGNORED_THREADS"):
                    if isinstance(ignored_threads, list):
                        ignored_threads = ",".join(str(v) for v in ignored_threads)
                    os.environ["TELEGRAM_IGNORED_THREADS"] = str(ignored_threads)
                if "reactions" in telegram_cfg and not os.getenv("TELEGRAM_REACTIONS"):
                    os.environ["TELEGRAM_REACTIONS"] = str(telegram_cfg["reactions"]).lower()
                if "proxy_url" in telegram_cfg and not os.getenv("TELEGRAM_PROXY"):
                    os.environ["TELEGRAM_PROXY"] = str(telegram_cfg["proxy_url"]).strip()
                # reply_to_mode: top-level preferred, falls back to extra.reply_to_mode
                # YAML 1.1 parses bare 'off' as boolean False — coerce to string "off".
                _telegram_extra = telegram_cfg.get("extra") if isinstance(telegram_cfg.get("extra"), dict) else {}
                _telegram_rtm = (
                    telegram_cfg["reply_to_mode"] if "reply_to_mode" in telegram_cfg
                    else _telegram_extra.get("reply_to_mode")
                )
                if _telegram_rtm is not None and not os.getenv("TELEGRAM_REPLY_TO_MODE"):
                    _rtm_str = "off" if _telegram_rtm is False else str(_telegram_rtm).lower()
                    os.environ["TELEGRAM_REPLY_TO_MODE"] = _rtm_str
                allowed_users = telegram_cfg.get("allow_from")
                if allowed_users is not None and not os.getenv("TELEGRAM_ALLOWED_USERS"):
                    if isinstance(allowed_users, list):
                        allowed_users = ",".join(str(v) for v in allowed_users)
                    os.environ["TELEGRAM_ALLOWED_USERS"] = str(allowed_users)
                group_allowed_users = telegram_cfg.get("group_allow_from")
                if group_allowed_users is not None and not os.getenv("TELEGRAM_GROUP_ALLOWED_USERS"):
                    if isinstance(group_allowed_users, list):
                        group_allowed_users = ",".join(str(v) for v in group_allowed_users)
                    os.environ["TELEGRAM_GROUP_ALLOWED_USERS"] = str(group_allowed_users)
                group_allowed_chats = telegram_cfg.get("group_allowed_chats")
                if group_allowed_chats is not None and not os.getenv("TELEGRAM_GROUP_ALLOWED_CHATS"):
                    if isinstance(group_allowed_chats, list):
                        group_allowed_chats = ",".join(str(v) for v in group_allowed_chats)
                    os.environ["TELEGRAM_GROUP_ALLOWED_CHATS"] = str(group_allowed_chats)
                for _telegram_extra_key in ("guest_mode", "disable_link_previews", "observe_unmentioned_group_messages"):
                    if _telegram_extra_key in telegram_cfg:
                        plat_data = platforms_data.setdefault(Platform.TELEGRAM.value, {})
                        if not isinstance(plat_data, dict):
                            plat_data = {}
                            platforms_data[Platform.TELEGRAM.value] = plat_data
                        extra = plat_data.setdefault("extra", {})
                        if not isinstance(extra, dict):
                            extra = {}
                            plat_data["extra"] = extra
                        extra[_telegram_extra_key] = telegram_cfg[_telegram_extra_key]
                if _telegram_extra:
                    _plat_data, _plat_extra = _ensure_platform_extra_dict(
                        platforms_data, Platform.TELEGRAM.value
                    )
                    for _telegram_extra_key, _telegram_extra_value in _telegram_extra.items():
                        _plat_extra.setdefault(_telegram_extra_key, _telegram_extra_value)

            # Feishu settings → env vars (env vars take precedence)
            feishu_cfg = yaml_cfg.get("feishu", {})
            if isinstance(feishu_cfg, dict):
                if "allow_bots" in feishu_cfg and not os.getenv("FEISHU_ALLOW_BOTS"):
                    os.environ["FEISHU_ALLOW_BOTS"] = str(feishu_cfg["allow_bots"]).lower()

    except Exception as e:
        logger.warning(
            "Failed to process config.yaml — falling back to .env / gateway.json values. "
            "Check %s for syntax errors. Error: %s",
            _home / "config.yaml",
            e,
        )

    config = GatewayConfig.from_dict(gw_data)

    # Override with environment variables
    _apply_env_overrides(config)
    
    # --- Validate loaded values ---
    _validate_gateway_config(config)

    return config


def _validate_gateway_config(config: "GatewayConfig") -> None:
    """Validate and sanitize a loaded GatewayConfig in place.

    Called by ``load_gateway_config()`` after all config sources are merged.
    Extracted as a separate function for testability.
    """
    policy = config.default_reset_policy

    if not (0 <= policy.at_hour <= 23):
        logger.warning(
            "Invalid at_hour=%s (must be 0-23). Using default 4.", policy.at_hour
        )
        policy.at_hour = 4

    if policy.idle_minutes is None or policy.idle_minutes <= 0:
        logger.warning(
            "Invalid idle_minutes=%s (must be positive). Using default 1440.",
            policy.idle_minutes,
        )
        policy.idle_minutes = 1440

    # Warn about empty bot tokens — platforms that loaded an empty string
    # won't connect and the cause can be confusing without a log line.
    _token_env_names = {
        Platform.TELEGRAM: "TELEGRAM_BOT_TOKEN",
        Platform.DISCORD: "DISCORD_BOT_TOKEN",
        Platform.SLACK: "SLACK_BOT_TOKEN",
    }
    for platform, pconfig in config.platforms.items():
        if not pconfig.enabled:
            continue
        env_name = _token_env_names.get(platform)
        if env_name and pconfig.token is not None and not pconfig.token.strip():
            logger.warning(
                "%s is enabled but %s is empty. "
                "The adapter will likely fail to connect.",
                platform.value, env_name,
            )

    # Reject known-weak placeholder tokens.
    # Ported from openclaw/openclaw#64586: users who copy .env.example
    # without changing placeholder values get a clear startup error instead
    # of a confusing "auth failed" from the platform API.
    try:
        from marlow_cli.auth import has_usable_secret
    except ImportError:
        has_usable_secret = None  # type: ignore[assignment]

    if has_usable_secret is not None:
        for platform, pconfig in config.platforms.items():
            if not pconfig.enabled:
                continue
            env_name = _token_env_names.get(platform)
            if not env_name:
                continue
            token = pconfig.token
            if token and token.strip() and not has_usable_secret(token, min_length=4):
                logger.error(
                    "%s is enabled but %s is set to a placeholder value ('%s'). "
                    "Set a real bot token before starting the gateway. "
                    "The adapter will NOT be started.",
                    platform.value, env_name, token.strip()[:6] + "...",
                )
                pconfig.enabled = False


def _apply_env_overrides(config: GatewayConfig) -> None:
    """Apply environment overrides for retained built-ins and platform plugins."""

    def _enable_from_env(platform: Platform) -> PlatformConfig:
        if platform not in config.platforms:
            config.platforms[platform] = PlatformConfig(enabled=True)
            return config.platforms[platform]

        platform_config = config.platforms[platform]
        enabled_was_explicit = bool(
            platform_config.extra.pop("_enabled_explicit", False)
        )
        if not platform_config.enabled and not enabled_was_explicit:
            platform_config.enabled = True
        return platform_config

    def _set_home(
        platform: Platform,
        chat_env: str,
        name_env: str,
        thread_env: str,
        default_name: str = "Home",
    ) -> None:
        chat_id = os.getenv(chat_env)
        if not chat_id or platform not in config.platforms:
            return
        config.platforms[platform].home_channel = HomeChannel(
            platform=platform,
            chat_id=chat_id,
            name=os.getenv(name_env, default_name),
            thread_id=os.getenv(thread_env) or None,
        )

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if telegram_token:
        _enable_from_env(Platform.TELEGRAM).token = telegram_token
    telegram_reply_mode = os.getenv("TELEGRAM_REPLY_TO_MODE", "").lower()
    if telegram_reply_mode in {"off", "first", "all"}:
        config.platforms.setdefault(
            Platform.TELEGRAM, PlatformConfig()
        ).reply_to_mode = telegram_reply_mode
    telegram_fallback_ips = os.getenv("TELEGRAM_FALLBACK_IPS", "")
    if telegram_fallback_ips:
        config.platforms.setdefault(
            Platform.TELEGRAM, PlatformConfig()
        ).extra["fallback_ips"] = [
            value.strip()
            for value in telegram_fallback_ips.split(",")
            if value.strip()
        ]
    _set_home(
        Platform.TELEGRAM,
        "TELEGRAM_HOME_CHANNEL",
        "TELEGRAM_HOME_CHANNEL_NAME",
        "TELEGRAM_HOME_CHANNEL_THREAD_ID",
    )

    discord_token = os.getenv("DISCORD_BOT_TOKEN")
    if discord_token:
        _enable_from_env(Platform.DISCORD).token = discord_token
    discord_reply_mode = os.getenv("DISCORD_REPLY_TO_MODE", "").lower()
    if discord_reply_mode in {"off", "first", "all"}:
        config.platforms.setdefault(
            Platform.DISCORD, PlatformConfig()
        ).reply_to_mode = discord_reply_mode
    _set_home(
        Platform.DISCORD,
        "DISCORD_HOME_CHANNEL",
        "DISCORD_HOME_CHANNEL_NAME",
        "DISCORD_HOME_CHANNEL_THREAD_ID",
    )

    slack_token = os.getenv("SLACK_BOT_TOKEN")
    if slack_token:
        slack_config = _enable_from_env(Platform.SLACK)
        slack_config.token = slack_token
    _set_home(
        Platform.SLACK,
        "SLACK_HOME_CHANNEL",
        "SLACK_HOME_CHANNEL_NAME",
        "SLACK_HOME_CHANNEL_THREAD_ID",
        "",
    )

    email_address = os.getenv("EMAIL_ADDRESS")
    email_password = os.getenv("EMAIL_PASSWORD")
    email_imap = os.getenv("EMAIL_IMAP_HOST")
    email_smtp = os.getenv("EMAIL_SMTP_HOST")
    if all((email_address, email_password, email_imap, email_smtp)):
        email_config = _enable_from_env(Platform.EMAIL)
        email_config.extra.update(
            {
                "address": email_address,
                "imap_host": email_imap,
                "smtp_host": email_smtp,
            }
        )
    _set_home(
        Platform.EMAIL,
        "EMAIL_HOME_ADDRESS",
        "EMAIL_HOME_ADDRESS_NAME",
        "EMAIL_HOME_ADDRESS_THREAD_ID",
    )

    if _coerce_bool(os.getenv("WEBHOOK_ENABLED"), False):
        webhook_config = _enable_from_env(Platform.WEBHOOK)
        webhook_port = os.getenv("WEBHOOK_PORT")
        if webhook_port:
            try:
                webhook_config.extra["port"] = int(webhook_port)
            except ValueError:
                logger.warning("Ignoring invalid WEBHOOK_PORT=%r", webhook_port)
        webhook_secret = os.getenv("WEBHOOK_SECRET")
        if webhook_secret:
            webhook_config.extra["secret"] = webhook_secret

    feishu_app_id = os.getenv("FEISHU_APP_ID")
    feishu_app_secret = os.getenv("FEISHU_APP_SECRET")
    if feishu_app_id and feishu_app_secret:
        feishu_config = _enable_from_env(Platform.FEISHU)
        feishu_config.extra.update(
            {
                "app_id": feishu_app_id,
                "app_secret": feishu_app_secret,
                "domain": os.getenv("FEISHU_DOMAIN", "feishu"),
                "connection_mode": "websocket",
            }
        )
    _set_home(
        Platform.FEISHU,
        "FEISHU_HOME_CHANNEL",
        "FEISHU_HOME_CHANNEL_NAME",
        "FEISHU_HOME_CHANNEL_THREAD_ID",
    )

    idle_minutes = os.getenv("SESSION_IDLE_MINUTES")
    if idle_minutes:
        try:
            config.default_reset_policy.idle_minutes = int(idle_minutes)
        except ValueError:
            logger.warning("Ignoring invalid SESSION_IDLE_MINUTES=%r", idle_minutes)
    reset_hour = os.getenv("SESSION_RESET_HOUR")
    if reset_hour:
        try:
            config.default_reset_policy.at_hour = int(reset_hour)
        except ValueError:
            logger.warning("Ignoring invalid SESSION_RESET_HOUR=%r", reset_hour)

    # Registry-driven enable for plugin platforms.  Built-ins have explicit
    # blocks above; plugins expose check_fn() which is the single source of
    # truth for "are my env vars set?".  When it returns True, ensure the
    # platform is enabled so start() will create its adapter.  Plugins that
    # need to seed ``PlatformConfig.extra`` from env vars
    # project_id / subscription_name) can supply ``env_enablement_fn`` on
    # their PlatformEntry — called here BEFORE adapter construction.
    #
    # Enablement gate (#31116): when a plugin registers ``is_connected``
    # (the "has the user actually configured credentials for this?" check),
    # we MUST consult it before flipping ``enabled = True``.  Otherwise
    # ``check_fn`` alone — which for adapter plugins typically just
    # verifies the SDK is importable / lazy-installs it — silently enables
    # platforms the user never opted into, and the gateway then tries to
    # connect to a plugin platform with no token and emits
    # noisy retry-forever errors.  ``_platform_status`` was already fixed
    # for the same bug class in commit 7849a3d73; this is the runtime
    # counterpart.
    try:
        from marlow_cli.plugins import discover_plugins
        discover_plugins()  # idempotent
        from gateway.platform_registry import platform_registry
        for entry in platform_registry.plugin_entries():
            try:
                if not entry.check_fn():
                    continue
            except Exception as e:
                logger.debug("check_fn for %s raised: %s", entry.name, e)
                continue
            platform = Platform(entry.name)
            existing_cfg = config.platforms.get(platform)
            # Seed candidate extras from ``env_enablement_fn`` so plugins
            # whose ``is_connected`` reads ``config.extra``
            # see the same state they will after enablement. Plugins whose
            # ``is_connected`` reads env vars directly are unaffected.
            seed_for_probe = None
            if entry.env_enablement_fn is not None:
                try:
                    seed_for_probe = entry.env_enablement_fn()
                except Exception as e:
                    logger.debug(
                        "env_enablement_fn for %s raised: %s", entry.name, e
                    )
                    seed_for_probe = None

            # Only consult is_connected for platforms that are NOT already
            # explicitly configured in YAML / env (existing_cfg with
            # enabled=True means the user wrote it themselves or another
            # env-var bridge enabled it — keep that decision).
            if existing_cfg is None or not existing_cfg.enabled:
                if entry.is_connected is not None:
                    try:
                        # Probe with ``enabled=True`` since we're asking
                        # "would this plugin BE configured if we enabled
                        # it?" not "is it currently enabled?". Google
                        # Chat's ``_is_connected`` short-circuits on
                        # ``config.enabled`` being False, which on the
                        # default ``PlatformConfig()`` would fail the
                        # gate even with proper env vars set.
                        if existing_cfg is not None:
                            probe_cfg = existing_cfg
                            if not probe_cfg.enabled:
                                probe_cfg = PlatformConfig(
                                    enabled=True,
                                    extra=dict(probe_cfg.extra or {}),
                                )
                        else:
                            probe_cfg = PlatformConfig(enabled=True)
                        if isinstance(seed_for_probe, dict) and seed_for_probe:
                            # Don't mutate ``existing_cfg``; the probe gets
                            # a transient view with env-seeded extras layered
                            # on top of whatever's already there.
                            probe_extra = dict(getattr(probe_cfg, "extra", {}) or {})
                            for k, v in seed_for_probe.items():
                                if k == "home_channel":
                                    continue
                                probe_extra.setdefault(k, v)
                            probe_cfg = PlatformConfig(
                                enabled=True,
                                extra=probe_extra,
                            )
                        configured = bool(entry.is_connected(probe_cfg))
                    except Exception as exc:
                        logger.debug(
                            "is_connected for %s raised: %s — skipping enablement",
                            entry.name, exc,
                        )
                        configured = False
                    if not configured:
                        logger.debug(
                            "Plugin platform '%s' available but not configured "
                            "(is_connected returned False) — skipping enable",
                            entry.name,
                        )
                        continue
            if platform not in config.platforms:
                config.platforms[platform] = PlatformConfig()
            config.platforms[platform].enabled = True
            # Commit env-seeded extras onto the now-enabled platform.
            # We've already called ``env_enablement_fn`` above (for the
            # probe); reuse that result instead of calling it twice.
            if isinstance(seed_for_probe, dict) and seed_for_probe:
                seed = dict(seed_for_probe)
                # Extract the home_channel dict (if provided) so we wire it
                # up as a proper HomeChannel dataclass.  Everything else is
                # merged into ``extra``.
                home = seed.pop("home_channel", None)
                config.platforms[platform].extra.update(seed)
                if isinstance(home, dict) and home.get("chat_id"):
                    config.platforms[platform].home_channel = HomeChannel(
                        platform=platform,
                        chat_id=str(home["chat_id"]),
                        name=str(home.get("name") or "Home"),
                        thread_id=(
                            str(home["thread_id"])
                            if home.get("thread_id")
                            else None
                        ),
                    )
    except Exception as e:
        logger.debug("Plugin platform enable pass failed: %s", e)

    for platform_config in config.platforms.values():
        platform_config.extra.pop("_enabled_explicit", None)

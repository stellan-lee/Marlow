# Marlow Origin-Bound Messaging and Cross-Conversation Delivery

**Status:** Proposed  
**Document type:** Engineering Design  
**Baseline reviewed:** `4bbfec0bcd98df50e9d4fade9910dc779ebd0541`  
**Primary owners:** Gateway, Messaging Tools, Approval Runtime  
**Last updated:** 2026-08-25

## 1. Executive Summary

Marlow currently has more than one component deciding where a user-facing message should be delivered:

1. normal replies, status messages, clarification prompts, and most background notifications use the inbound `SessionSource`;
2. `send_message` independently resolves a tool-supplied target and treats a bare platform such as `telegram` as that platform's configured home channel;
3. command and action approvals can replace the originating route with the configured administrator route; and
4. successful `send_message` calls independently mirror content into whichever target session can be found.

These routing authorities are individually functional but do not share one policy. An interactive turn that starts in a private conversation can therefore send content to a home group or another channel while the clarification, confirmation, progress, and final response remain in the private conversation. This is not only confusing; it is a potential private-to-shared-channel disclosure.

This design introduces an origin-bound delivery model with one hard rule:

> Content produced by an interactive user turn MUST remain inside the originating conversation unless a concrete cross-conversation destination has been selected and a one-time, request-bound delivery grant has been obtained in the originating conversation.

The design does **not** require a rewrite of platform adapters or normal response delivery. The existing origin-bound paths are retained. The work focuses on the places that are allowed to select or replace a destination: `send_message`, approval presentation, scheduled/system delivery, and session mirroring.

The target design adds:

- an immutable, typed `TurnContext` containing the exact origin route and authenticated actor;
- a typed `ConversationRoute` whose identity includes platform, chat, and thread;
- an `OutboundPolicy` that distinguishes origin-only interaction, cross-conversation delivery, scheduled delivery, and system notices;
- a request-scoped confirmation flow for every interactive cross-conversation send;
- a separation between **who may approve** and **where the approval UI is presented**;
- delivery recording and session mirroring only after authorization and successful delivery; and
- route-isolation, concurrency, callback-authorization, and negative security tests.

## 2. Decisions Requested

This design recommends approving the following product and security decisions:

1. **Every interactive cross-conversation send requires one-time confirmation in the originating conversation.** There is no session-wide or permanent bypass in the first release.
2. **A configured administrator who initiated an action from an authenticated private conversation receives the request-scoped approval in that same private conversation.** The configured administrator route is a fallback for requests initiated by non-administrators; it is no longer the unconditional presentation route.
3. **A shared administrator fallback route is rejected by default.** Operators may explicitly allow a group or channel route, but the unsafe choice must be visible and auditable.
4. **A different thread or topic is a different conversation.** Same platform and same chat are insufficient for route equality.
5. **Bare platform targets are not valid interactive destinations.** `telegram`, `slack`, or `discord` may retain home-channel meaning for scheduled or system execution, but an interactive tool call must use a concrete destination returned by target discovery, including an explicit home selector when intended.

## 3. Problem Statement

### 3.1 User-visible failure

A representative failure is:

```text
User DM
  └─ asks Marlow to change or send something

Marlow agent
  ├─ calls send_message(target="telegram", message="content to review")
  │    └─ current implementation resolves "telegram" to Telegram home
  │         └─ content appears in another group/channel
  └─ asks for clarification or approval
       └─ clarification remains in the original DM
```

The user experiences one logical interaction split across two conversations. More importantly, the target receives content before the user has confirmed that target or content.

### 3.2 Current routing authorities

The reviewed code has four relevant routing authorities.

#### A. Inbound origin routing

`gateway/session.py` defines `SessionSource`, including:

- `platform`;
- `chat_id`;
- `thread_id`;
- `user_id` and `user_id_alt`;
- `message_id`; and
- chat type and display metadata.

`gateway/run.py` and `gateway/platforms/base.py` correctly use this source for most response paths. Final responses, progress, streaming, media, clarify prompts, errors, and background-review messages normally use `source.chat_id` plus thread metadata derived from the source.

#### B. `send_message` target resolution

`tools/send_message_tool.py` defines a separate target grammar. A bare platform explicitly means its configured home channel:

```text
telegram              -> Telegram home channel
telegram:<chat_id>    -> explicit chat
telegram:<chat>:<tid> -> explicit topic/thread
```

When no chat ID is parsed, `_handle_send()` calls `config.get_home_channel(platform)`. This behavior is also stated in the model-facing tool schema. The tool therefore has enough authority to leave the current conversation without consulting the inbound origin.

#### C. Approval presentation routing

`gateway/run.py::_resolve_approval_delivery_route()` combines administrator authorization and presentation. When `approvals.admin.enabled` is true, the function returns the configured administrator adapter, chat, thread, and user regardless of the requester route.

This is appropriate for delegated approval by a different administrator, but incorrect when the authenticated administrator initiated the request in a private conversation and expects the interaction to remain there.

#### D. Session mirroring

After a successful `send_message`, `tools/send_message_tool.py` calls `gateway.mirror.mirror_to_session()`. The mirror module searches for a target session and appends the sent content as an assistant message.

This means a routing mistake can have two effects:

1. the content is delivered to the wrong platform conversation; and
2. the same content may be persisted into that target conversation's model context.

### 3.3 Why this is a design problem, not an adapter bug

The platform adapters receive a chat ID and metadata and generally deliver exactly where requested. Changing only Telegram, Slack, Discord, or Feishu would not remove the conflicting route authorities above the adapters.

The defect is that an interactive turn has no enforced delivery invariant. The model, tool, approval subsystem, and mirror subsystem can each choose a route under different rules.

### 3.4 Current-state validation

The reviewed baseline passes the focused routing suite:

```text
111 passed
```

The passing tests include `send_message`, admin approval routing, delivery, mirroring, session context, and clarify behavior. This confirms that the unsafe behavior is largely encoded as intended legacy semantics rather than being only an intermittent asynchronous race.

## 4. Goals

### 4.1 Functional goals

1. Keep all normal interactive output in the exact conversation that produced the turn.
2. Prevent any cross-conversation delivery before the initiating user confirms the concrete destination and payload.
3. Preserve deliberate cross-platform and cross-channel messaging as a supported feature.
4. Keep cron, scheduled reports, monitoring alerts, and gateway system notices able to use configured non-origin destinations.
5. Allow an administrator to approve actions in the private conversation from which that administrator initiated them.
6. Preserve delegated approval for non-administrator requesters without moving the requester's entire interaction to the administrator route.
7. Persist or mirror only a delivery that was authorized and actually succeeded.
8. Prevent duplicate sends from repeated button clicks, retries, concurrent turns, or stale callbacks.

### 4.2 Security and quality goals

1. Routing ambiguity fails closed.
2. Prompt instructions improve model behavior but are never the security boundary.
3. Route identity is immutable for the lifetime of a turn.
4. Approval and delivery grants are bound to exact arguments, content, attachments, destination, actor, and turn.
5. Different users, DMs, channels, threads, and topics remain isolated under concurrency.
6. Sensitive content is not copied into logs or unrelated approval channels.
7. Existing platform plugins require minimal changes and retain a text fallback when they do not implement rich confirmation UI.
8. The migration can be delivered in independently safe pull requests.

## 5. Non-Goals

This design does not attempt to:

- replace the platform adapter abstraction;
- merge all cron and interactive delivery code into one implementation in the first release;
- redesign platform account authentication or channel discovery;
- create a general organization-wide role-based access-control system;
- make cross-conversation grants durable across gateway restarts;
- guarantee atomic delivery across multiple platforms or multiple attachments;
- change the content, formatting, chunking, media conversion, or retry semantics of platform adapters; or
- treat the configured home channel as the default reply destination for an interactive turn.

## 6. Terminology

### 6.1 Conversation route

The logical conversation boundary to which a message is delivered:

```text
(platform, chat_id, thread_id)
```

`thread_id` is part of the boundary. `telegram:123:topic-a` and `telegram:123:topic-b` are different routes.

A reply anchor such as `message_id` affects presentation but is not part of route identity.

### 6.2 Origin

The immutable `ConversationRoute` derived from the inbound `SessionSource` at the beginning of an interactive turn.

### 6.3 Actor

The authenticated platform identity that initiated the turn. It includes the canonical user ID and any platform-provided stable alternate ID. Actor identity is separate from route identity.

### 6.4 Origin-only interaction

A user-facing message that is part of the current interaction and must not change routes, including:

- final response;
- interim response;
- progress and status;
- clarification;
- ordinary confirmation;
- error reporting;
- completion notification; and
- requester-side approval status.

### 6.5 Cross-conversation delivery

An intentional side effect that sends content to a route different from the interactive origin.

### 6.6 Delivery grant

A one-time capability proving that the initiating actor approved one exact cross-conversation payload and destination. It is not a general permission to send future messages.

### 6.7 Administrator authority

The set of authenticated principals permitted to approve privileged actions.

### 6.8 Approval presentation route

The conversation in which an approval card is displayed. It is selected after authority is evaluated and is not itself the source of authority.

## 7. Hard Invariants

The following invariants are normative and must be encoded in tests.

### 7.1 Interactive origin invariant

Every interactive turn has one immutable origin route. Code must not reconstruct it later from a home channel, mutable environment variables, session index, model arguments, or current adapter state.

### 7.2 Origin-only delivery invariant

For an interactive turn:

```text
kind in {reply, interim, status, clarify, confirmation, error, completion}
    => destination == origin
```

A mismatch is a policy violation and no adapter call occurs.

### 7.3 Cross-conversation authorization invariant

For an interactive turn:

```text
destination != origin
    => kind == cross_conversation
    AND destination is concrete
    AND valid one-time delivery grant exists
```

### 7.4 No implicit home invariant

A bare platform name cannot resolve to a home channel during interactive execution. Home-channel resolution is allowed only for scheduled or system execution, or through an explicit interactive destination such as a discovered `telegram:home` target followed by confirmation.

### 7.5 Exact route invariant

Route equality compares normalized platform, chat ID, and logical thread ID. A missing or different thread is not treated as equal merely because the parent chat matches.

### 7.6 Exact payload invariant

A delivery grant is valid only for the content and attachments shown in the confirmation. Any change to destination, text, media set, media bytes, or delivery-relevant options invalidates the grant.

### 7.7 One-time execution invariant

A grant can transition to sending once. Duplicate button clicks, concurrent retries, or repeated tool calls cannot reuse it.

### 7.8 Authorization-before-side-effect invariant

Before confirmation, Marlow may send the preview only to the origin. It must not send any preview, placeholder, typing status, attachment, or confirmation request to the destination.

### 7.9 Success-before-persistence invariant

A target-session mirror or delivery record that represents a successful send is written only after the adapter reports success. Cancelled, expired, rejected, and failed deliveries are not mirrored as delivered messages.

### 7.10 No guessing invariant

If target resolution, actor identity, callback route, session mapping, or adapter availability is ambiguous, the operation fails closed. It does not fall back to a home channel, another thread, the latest matching session, or a different platform.

## 8. Current Architecture Findings

### 8.1 Existing foundations to retain

The codebase already contains useful foundations:

- `SessionSource` carries the required origin and actor data.
- `gateway/session_context.py` uses `ContextVar` for most gateway session values.
- tool worker threads propagate `ContextVar` state through `tools/thread_context.py`.
- normal response and clarify paths already use the source chat and thread metadata.
- approval requests already use request IDs and request-scoped callback controls.
- adapter implementations already support rich approval and clarify UI on major platforms.
- `gateway.mirror` already avoids some ambiguous multi-user session matches.

The design builds on these rather than creating an unrelated messaging stack.

### 8.2 Gaps to close

1. Session context is represented as independent legacy string variables rather than one typed immutable turn object.
2. `send_message` ignores turn origin and has an interactive home fallback.
3. `gateway.delivery.DeliveryTarget` describes scheduled delivery semantics and must not be reused as the interactive authorization policy without adding execution mode and intent.
4. Administrator configuration combines identity, elevated-conversation scope, and presentation destination.
5. Session mirroring is called directly by the tool rather than consuming an authorized delivery result.
6. Some compatibility code still writes routing-adjacent state to `os.environ`, including `MARLOW_SESSION_KEY`.
7. Rich UI callback authorization is implemented per feature; cross-conversation confirmation needs the same exact-user, exact-route, request-ID validation.

## 9. Target Architecture

### 9.1 Component diagram

```text
Inbound platform event
        │
        ▼
SessionSource
        │ derive once
        ▼
┌──────────────────────────┐
│ Immutable TurnContext    │
│ - turn_id                │
│ - mode                   │
│ - origin route           │
│ - actor identity         │
│ - session key/id         │
│ - reply anchor           │
└─────────────┬────────────┘
              │ ContextVar propagation
              ▼
             Agent
              │
      ┌───────┴─────────┐
      │                 │
normal response    send_message tool
      │                 │ concrete target
      │                 ▼
      │        PendingDeliveryStore
      │                 │
      │        confirmation to origin
      │                 │ approve once
      │                 ▼
      │          DeliveryGrant
      │                 │
      └──────────┬──────┘
                 ▼
          OutboundPolicy
                 │ allow/deny
                 ▼
      OutboundDeliveryService
                 │
                 ▼
          Platform adapter
                 │ success
                 ▼
          DeliveryRecorder
          ├─ target-session mirror, when unambiguous
          └─ privacy-safe audit metadata
```

### 9.2 Ownership boundaries

#### `gateway/turn_context.py` — new

Owns immutable turn, route, actor, and execution-mode types plus the typed `ContextVar` lifecycle.

#### `gateway/outbound.py` — new

Owns outbound envelopes, policy decisions, one authorized adapter invocation, and normalized delivery results.

#### `gateway/pending_delivery.py` — new

Owns request-scoped cross-conversation confirmation state and state transitions.

#### `tools/send_message_tool.py`

Owns model-facing target discovery and argument validation. It no longer owns home fallback, adapter selection, authorization, or mirroring during an interactive turn.

#### `gateway/run.py`

Creates the `TurnContext`, registers the per-turn confirmation bridge, selects approval presentation, and handles platform callbacks.

#### `gateway/platforms/base.py`

Provides a text fallback for delivery confirmation. Rich platform adapters may override it with native buttons.

#### `gateway/mirror.py`

Consumes a successful authorized delivery record. It no longer accepts arbitrary route and content arguments from the tool path.

#### `gateway/delivery.py` and `cron/scheduler.py`

Continue to own scheduled/system delivery. Their home-channel semantics remain valid because these executions do not represent an implicit reply to a live user turn.

## 10. Typed Context and Routing Model

### 10.1 Conversation route

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class ConversationRoute:
    platform: Platform
    chat_id: str
    thread_id: str | None = None

    @classmethod
    def from_source(cls, source: SessionSource) -> "ConversationRoute":
        return cls(
            platform=source.platform,
            chat_id=str(source.chat_id),
            thread_id=normalize_optional_id(source.thread_id),
        )
```

The route contains only identity fields. Delivery hints are separate so a reply anchor cannot accidentally change route equality.

### 10.2 Actor identity

```python
@dataclass(frozen=True, slots=True)
class ActorIdentity:
    platform: Platform
    user_ids: frozenset[str]
```

Both `user_id` and a verified `user_id_alt` may be included. Empty identifiers are excluded.

### 10.3 Delivery hints

```python
@dataclass(frozen=True, slots=True)
class DeliveryHints:
    reply_to_message_id: str | None = None
    notify: bool = False
    chat_type: str | None = None
```

Platform-specific thread metadata remains produced by the existing helper methods. The route object is not polluted with mutable adapter metadata.

### 10.4 Turn context

```python
class ExecutionMode(str, Enum):
    INTERACTIVE = "interactive"
    SCHEDULED = "scheduled"
    SYSTEM = "system"
    LOCAL = "local"

@dataclass(frozen=True, slots=True)
class TurnContext:
    turn_id: str
    mode: ExecutionMode
    origin: ConversationRoute | None
    actor: ActorIdentity | None
    session_key: str | None
    session_id: str | None
    hints: DeliveryHints
```

For `INTERACTIVE`, `origin`, `actor`, and `session_key` are required. For scheduled and system work, origin may be absent.

### 10.5 Context lifecycle

A new typed context variable is added:

```python
_CURRENT_TURN: ContextVar[TurnContext | None]
```

`GatewayRunner` sets it immediately before agent execution and resets it with the exact `ContextVar` token in `finally`.

The new typed context has no `os.environ` fallback. Existing string helpers remain temporarily for compatibility, but all routing and authorization code must use `get_current_turn()`.

Because the tool executor already uses `contextvars.copy_context()`, the typed context is propagated into concurrent tool worker threads without a new thread-local mechanism.

## 11. Outbound Message Model

### 11.1 Outbound kinds

```python
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
```

### 11.2 Outbound envelope

```python
@dataclass(frozen=True, slots=True)
class OutboundEnvelope:
    delivery_id: str
    kind: OutboundKind
    destination: ConversationRoute
    text: str
    attachments: tuple[StagedAttachment, ...]
    hints: DeliveryHints
    turn_id: str | None
    grant_id: str | None = None
```

The envelope is immutable once authorized.

### 11.3 Policy decision

```python
@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason: str
    requires_confirmation: bool = False
```

`OutboundPolicy` is pure and independently testable. It does not send messages or mutate confirmation state.

### 11.4 Policy matrix

| Execution mode | Kind | Destination rule | Authorization |
|---|---|---|---|
| Interactive | Reply/interim/status/clarify/confirmation/error/completion | Exact origin only | Turn context |
| Interactive | Cross-conversation | Concrete non-origin route | One-time delivery grant |
| Interactive | Approval for initiating authorized admin | Exact private origin | Exact actor + request ID |
| Interactive | Delegated admin approval | Configured fallback route | Configured admin principal + request ID |
| Scheduled | Scheduled output | Job-resolved destination | Stored job configuration |
| System | System notice | Explicit configured route/home | Internal system callsite |
| Local | Any platform send | Denied unless explicitly bridged | None |

## 12. `send_message` Redesign

### 12.1 Tool purpose

`send_message` remains the tool for sending to a **different** conversation. It is not a reply tool.

The model-facing description must state:

- do not use this tool to answer the current conversation;
- put current-conversation content in the assistant response;
- discover a concrete destination before sending; and
- cross-conversation delivery will be confirmed in the originating conversation.

### 12.2 Target grammar

For interactive execution, accepted targets are concrete:

```text
telegram:<chat_id>
telegram:<chat_id>:<thread_id>
slack:<conversation_id>
slack:<conversation_id>:<thread_ts>
discord:<channel_or_thread_id>
feishu:<chat_id>[:<thread_id>]
email:<address>
<platform>:home                  # explicit configured home selector
```

A bare platform such as `telegram` is rejected in an interactive turn with a structured `ambiguous_destination` error. It continues to mean home in scheduled/system execution for backward compatibility.

Target discovery should return canonical `target` values that can be copied into the subsequent send call. Human-readable names are presentation only.

### 12.3 Same-origin behavior

If the resolved target equals the interactive origin, `send_message` returns:

```json
{
  "error": "same_origin_reply",
  "message": "Do not use send_message to reply to the current conversation; place the content in the final response."
}
```

It does not send a duplicate message.

### 12.4 Interactive cross-conversation behavior

When the resolved target differs from origin:

1. validate and normalize the target;
2. construct and freeze the exact payload;
3. stage attachments and compute payload digest;
4. register a pending delivery;
5. present a confirmation in origin;
6. block the tool worker until approve, cancel, timeout, interruption, or session cleanup;
7. issue a one-time delivery grant on approval;
8. revalidate the grant and atomically mark it `SENDING`;
9. send through `OutboundDeliveryService`;
10. record the result after adapter success; and
11. return the result to the agent, which provides its final response in origin.

The target receives nothing before step 9.

### 12.5 Structured tool results

Success:

```json
{
  "success": true,
  "delivery_id": "...",
  "target": "telegram:-100123:42",
  "confirmed": true,
  "message_id": "...",
  "mirrored": true
}
```

Cancelled:

```json
{
  "success": false,
  "cancelled": true,
  "delivery_id": "...",
  "message": "The user cancelled this delivery. Do not retry unless the user asks again."
}
```

Timeout:

```json
{
  "success": false,
  "timed_out": true,
  "delivery_id": "...",
  "message": "Delivery confirmation expired. Nothing was sent."
}
```

Policy denial:

```json
{
  "success": false,
  "policy_denied": true,
  "reason": "ambiguous_destination"
}
```

### 12.6 Retry behavior

A cancelled or expired payload digest is marked denied for the remainder of the turn. The model cannot immediately retry the same delivery through another equivalent target string.

A user may initiate a new turn and ask again, producing a new request and confirmation.

## 13. Cross-Conversation Confirmation

### 13.1 Pending delivery model

```python
class PendingDeliveryState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"

@dataclass
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
    state: PendingDeliveryState
    event: threading.Event
```

The store is in-memory and process-local. A restart cancels all pending deliveries; it never resumes or sends one automatically.

### 13.2 State transitions

```text
PENDING ──approve──> APPROVED ──claim──> SENDING ──success──> SENT
   │                    │                    └─failure──> FAILED
   ├─cancel────────> CANCELLED
   ├─timeout───────> EXPIRED
   └─session end───> CANCELLED
```

Transitions are performed under a lock or atomic compare-and-set. Only `APPROVED` may transition to `SENDING`, and only once.

### 13.3 Confirmation presentation

The confirmation is delivered to the exact origin and shows:

- destination platform;
- destination display name and canonical identifier;
- thread/topic when present;
- whether the destination is a DM, group, channel, or unknown;
- a bounded preview of the exact text;
- attachment names, sizes, and digests;
- an explicit warning for shared destinations; and
- `Send once` and `Cancel` actions.

No `Always allow` option is included in the initial release.

### 13.4 Callback authorization

A confirmation callback is accepted only when all of the following match:

- request ID;
- pending state;
- unexpired deadline;
- platform;
- chat ID;
- thread ID;
- authenticated initiating actor; and
- session/turn ownership.

A callback from the destination, another thread, another user, an old card, or a replayed request ID is rejected and produces no send.

### 13.5 Adapter interface

`BasePlatformAdapter` gains a fallback method:

```python
async def send_delivery_confirmation(
    self,
    *,
    chat_id: str,
    request_id: str,
    destination_label: str,
    preview: str,
    authorized_user_id: str,
    metadata: dict | None,
) -> SendResult:
    ...
```

The base implementation sends text instructions tied to request-scoped slash commands. Telegram, Slack, Feishu, and Discord should implement native buttons using the same callback validation contract.

## 14. Attachment Integrity

`send_message` supports local `MEDIA:` paths, so text-only hashing is insufficient.

At confirmation preparation time:

1. validate each path through existing media safety filters;
2. copy the allowed file into a private pending-delivery spool;
3. open with no-follow protections where supported;
4. compute SHA-256, size, and media type from the staged bytes;
5. set restrictive file permissions; and
6. bind the staged artifact metadata into the payload digest.

Delivery uses the staged copy, not the original path. This prevents a file from being replaced after the user previews it but before approval.

Pending files are deleted on send, cancel, expiration, session cleanup, and startup cleanup of stale spool directories.

## 15. Approval Authority and Presentation

### 15.1 Current coupling

The existing `AdminApprovalConfig` uses one object to represent:

- the authorized administrator user;
- the configured administrator chat and thread;
- the route used to present every approval; and
- the exact conversation that receives `super_admin` authority.

These concerns must be evaluated separately.

### 15.2 Internal model

```python
@dataclass(frozen=True)
class AdminPrincipal:
    platform: Platform
    user_ids: frozenset[str]

@dataclass(frozen=True)
class ApprovalPresentationPolicy:
    fallback_route: ConversationRoute | None
    prefer_authorized_private_origin: bool = True
    allow_shared_fallback: bool = False
```

The existing YAML remains readable and maps to one principal plus one fallback route.

### 15.3 Presentation selection

For a request-scoped approval:

1. determine whether the requester actor matches an administrator principal;
2. if it matches and the origin is an authenticated private conversation, present the approval in origin;
3. otherwise present it at the configured fallback route;
4. if no safe fallback exists, fail closed; and
5. independently restrict the callback to the selected administrator user and request ID.

This changes presentation, not authority.

### 15.4 Super-administrator behavior

`super_admin` auto-approval remains scoped to the exact configured privileged conversation. Merely being the same administrator identity in another DM does not silently grant blanket auto-approval.

In a different authenticated private DM, that administrator may receive a request-scoped approval card and approve once. This is safer than broadening super-administrator scope while still preserving a coherent private interaction.

### 15.5 Delegated approval flow

When the requester is not an administrator:

```text
Requester origin
  └─ receives: "Waiting for administrator approval"

Configured administrator fallback
  └─ receives: request-scoped approval card

Administrator decision
  └─ resumes the original agent turn

Final result
  └─ delivered to requester origin
```

The requester-side status contains no sensitive command or payload details beyond what the requester already supplied. The full approval details go only to the explicitly authorized administrator route.

### 15.6 Shared fallback policy

A group/channel fallback is denied by default because approval cards can contain commands, paths, destinations, and other sensitive context.

An operator may explicitly configure:

```yaml
approvals:
  admin:
    allow_shared_fallback: true
```

When enabled, startup logs and `/status` must show a security warning. The request remains bound to the configured administrator user even though other participants can see the card.

### 15.7 Backward-compatible configuration

Existing configuration remains valid:

```yaml
approvals:
  admin:
    enabled: true
    conversation_mode: approval_only
    platform: telegram
    user_id: "123456"
    chat_id: "123456"
    thread_id: null
```

New optional fields:

```yaml
approvals:
  admin:
    prefer_authorized_private_origin: true
    allow_shared_fallback: false
```

The legacy `platform`, `user_id`, `chat_id`, and `thread_id` continue to define the administrator principal and fallback route.

## 16. Outbound Delivery Service

### 16.1 Responsibilities

`OutboundDeliveryService`:

1. accepts an immutable envelope and current turn context;
2. invokes `OutboundPolicy`;
3. validates and consumes any grant;
4. resolves the connected adapter;
5. creates existing platform thread metadata;
6. sends text and attachments using current adapter behavior;
7. normalizes success, partial failure, and failure; and
8. passes only successful results to `DeliveryRecorder`.

It does not infer user intent, choose a target, or ask for confirmation.

### 16.2 Interactive adapter requirement

Interactive cross-conversation sends use the live gateway adapter. If the required adapter is not connected, the operation fails closed.

The standalone HTTP sender remains available to cron and other out-of-process system delivery. An interactive tool must not silently fall back to a separate path that lacks the current turn's policy and callbacks.

### 16.3 Direct adapter calls

Not every current `adapter.send()` call must be migrated immediately.

Direct calls are allowed only for code paths whose destination is mechanically derived from the inbound source or from an internal system configuration and that do not accept a model-selected destination.

The following must use the policy service:

- `send_message`;
- any future tool that accepts a platform/chat/thread destination;
- approval presentation when it can leave origin;
- target-session mirroring; and
- any plugin API that allows the model to choose a message destination.

## 17. Delivery Recording and Session Mirroring

### 17.1 Current risk

The current tool calls `mirror_to_session(platform, chat_id, text, ...)` directly after a send. This API cannot prove that the route was authorized or that the supplied origin metadata belongs to the delivery.

### 17.2 New recorder contract

```python
@dataclass(frozen=True)
class SuccessfulDelivery:
    delivery_id: str
    envelope: OutboundEnvelope
    source_route: ConversationRoute | None
    destination_route: ConversationRoute
    platform_message_ids: tuple[str, ...]
    completed_at: float

class DeliveryRecorder:
    def record_success(self, delivery: SuccessfulDelivery) -> RecordResult:
        ...
```

The recorder receives the already-authorized envelope; callers cannot substitute a different route or content.

### 17.3 Session resolution

The recorder mirrors only when the destination maps to one exact target session.

- Exact platform, chat, and thread must match.
- A DM may additionally use the known destination user identity when available.
- If a group route has multiple per-user sessions, the recorder does not guess which participant's session should receive the message.
- Ambiguous lookup is logged as a privacy-safe `mirror_skipped_ambiguous` event and delivery still succeeds.

A future route-level outbound ledger may provide shared-channel context without writing to an arbitrary user session, but it is not required for this fix.

### 17.4 Mirror semantics

- Same-origin replies are not mirrored through this path because the normal transcript already records them.
- Confirmation previews are not mirrored.
- Cancelled, expired, denied, and failed deliveries are not mirrored as sent content.
- Successful cross-conversation content retains provenance and `delivery_id` in the target transcript.

## 18. Prompt and Tool Contract Changes

### 18.1 Session context prompt

`build_session_context_prompt()` currently labels home channels as default destinations. It should instead label them:

```text
Scheduled and system delivery destinations — never use these as the implicit reply route for the current conversation.
```

The delivery options section must state that `origin` is the only normal interactive response route.

### 18.2 `send_message` schema

Remove the instruction:

```text
If the user just says a platform name, send directly to the home channel.
```

Replace it with:

```text
This tool sends to a different conversation. Never use it to reply to the current conversation. During an interactive turn, use a concrete target returned by action='list'; a bare platform is ambiguous and will be rejected. Marlow will ask the initiating user to confirm the exact destination and payload before sending.
```

### 18.3 Tool result guidance

Cancellation, timeout, and policy-denial results explicitly tell the model not to retry in the current turn. This reduces repeated confirmation cards and tool loops.

### 18.4 Prompt is defense in depth

Even with these changes, the runtime policy remains authoritative. A malformed or adversarial model call cannot bypass route isolation.

## 19. End-to-End Flows

### 19.1 Normal private reply

```text
1. Telegram DM arrives.
2. Gateway derives origin telegram:<dm_chat>:<topic?>.
3. Agent produces final response.
4. Base adapter sends to source.chat_id with source thread metadata.
5. No target selection, home lookup, grant, or mirror path is involved.
```

### 19.2 Cross-channel send requested in DM

```text
1. User in DM asks: "Send this update to #engineering."
2. Agent lists targets and calls send_message with a concrete Slack channel.
3. Tool detects destination != origin.
4. Gateway displays destination and exact preview in the DM.
5. User selects Send once.
6. Request-bound grant is issued and consumed.
7. Slack adapter sends to #engineering.
8. Successful delivery is recorded and mirrored only if the target session is exact.
9. Agent's final acknowledgement remains in the original DM.
```

### 19.3 Accidental `send_message(target="telegram")` in a DM

```text
1. Tool detects interactive mode and bare platform target.
2. Policy returns ambiguous_destination.
3. No home lookup occurs.
4. No message or preview is sent outside origin.
5. Agent is instructed to reply normally or discover a concrete destination.
```

### 19.4 Privileged action initiated by the configured admin in DM

```text
1. Actor matches configured administrator principal.
2. Origin is a private authenticated DM.
3. Approval presentation policy selects origin.
4. Approval card is displayed in the DM and restricted to that actor/request ID.
5. Approval resumes the same turn.
6. Final result remains in the DM.
```

This does not grant super-administrator auto-approval unless the origin is the exact configured super-admin conversation.

### 19.5 Privileged action initiated by a non-admin

```text
1. Requester receives a generic waiting status in origin.
2. Full request-scoped approval is presented at the safe admin fallback route.
3. Only the configured administrator can approve.
4. The agent resumes and replies to the requester origin.
```

### 19.6 Scheduled report

```text
1. Scheduler runs with ExecutionMode.SCHEDULED.
2. Job configuration resolves origin, explicit route, or platform home.
3. Outbound kind is SCHEDULED.
4. Interactive delivery confirmation is not required because destination authorization occurred when the job was created/configured.
5. Existing standalone/live-adapter fallback remains available.
```

## 20. Concurrency and Lifecycle

### 20.1 Turn isolation

Each inbound message receives a unique `turn_id` and typed context. The context is propagated through tool workers using the existing context-copy mechanism.

No routing decision reads a process-global current chat or thread.

### 20.2 Pending request isolation

Pending deliveries are indexed primarily by `request_id`, with secondary indices by turn and session for cleanup. They are never resolved by only “oldest request in session” when using rich controls.

### 20.3 Duplicate callbacks

The first valid approval changes state from `PENDING` to `APPROVED`. Later callbacks receive an expired/already-resolved response. Only one caller can claim `APPROVED -> SENDING`.

### 20.4 Interrupt and session boundary

`/stop`, `/new`, `/reset`, cached-agent eviction, gateway shutdown, and run-generation replacement cancel pending delivery waits and delete staged media. The blocked tool returns cancellation rather than remaining alive past its turn.

### 20.5 Gateway restart

Pending delivery grants are intentionally not persisted. After restart, old buttons are stale and cannot trigger a send. The user must initiate a new request.

### 20.6 Legacy environment cleanup

The new routing path must not use `os.environ["MARLOW_SESSION_*"]`. Existing compatibility writes can remain temporarily for unrelated CLI/cron behavior, but `MARLOW_SESSION_KEY` should be removed from `gateway/run.py::run_sync()` once all gateway tool consumers use the typed context.

## 21. Security and Privacy

### 21.1 Threats addressed

- accidental DM-to-group disclosure;
- model hallucination of a home destination;
- stale or replayed approval buttons;
- approval by a different group participant;
- thread/topic crossover;
- concurrent DM/session crossover;
- attachment replacement after preview;
- mirror contamination after an unauthorized or failed send; and
- fallback to an unguarded standalone sender.

### 21.2 Data minimization

Structured logs contain:

- delivery/turn/request ID;
- hashed origin and destination route keys;
- platform;
- message and attachment digests;
- state transition;
- policy reason; and
- adapter result metadata.

They do not contain full message text, command text, credentials, or attachment bytes.

### 21.3 Redaction

Administrator approval presentation continues to use the existing action-intent redaction and exact-argument digest behavior. Cross-conversation confirmation is shown to the initiating user in origin and therefore may display the exact message they are about to send; logs still store only digests.

### 21.4 Shared destinations

The confirmation card clearly identifies a shared destination. Unknown destination type is treated conservatively and displayed as potentially shared.

### 21.5 Fail-closed conditions

No send occurs when:

- turn context is missing in interactive execution;
- destination is ambiguous;
- origin or actor is unavailable;
- callback identity does not match;
- request is expired or already consumed;
- payload digest changes;
- attachment staging fails;
- target adapter is unavailable; or
- policy cannot classify the operation.

## 22. Observability

Add privacy-safe counters:

```text
marlow_outbound_policy_denied_total{reason,kind,mode}
marlow_cross_delivery_confirmation_total{outcome,platform}
marlow_cross_delivery_send_total{outcome,platform}
marlow_cross_delivery_duplicate_callback_total{platform}
marlow_approval_presentation_total{route_type,platform}
marlow_mirror_total{outcome,platform}
marlow_route_isolation_prevented_total{reason,platform}
```

Structured event names:

```text
outbound_policy_denied
cross_delivery_pending
cross_delivery_approved
cross_delivery_cancelled
cross_delivery_expired
cross_delivery_sent
cross_delivery_failed
approval_presented_at_origin
approval_presented_at_fallback
mirror_skipped_ambiguous
```

`/status` should expose whether shared admin fallback is enabled and whether interactive origin-bound policy is active.

## 23. Testing Strategy

### 23.1 Unit tests: route identity

- same platform/chat/thread is equal;
- same platform/chat with different thread is different;
- `None` and empty optional IDs normalize consistently;
- reply/message anchor does not change route identity;
- platform case and ID string normalization are deterministic.

### 23.2 Unit tests: outbound policy

- every origin-only kind allows exact origin;
- every origin-only kind denies non-origin;
- interactive cross-delivery without grant is denied or requires confirmation;
- exact valid grant permits once;
- changed destination, text, attachment digest, turn, actor, or expiry denies;
- scheduled/system modes retain their allowed routes;
- local mode cannot silently send to a platform.

### 23.3 `send_message` tests

- interactive bare platform never resolves home;
- interactive same-origin call is rejected as a duplicate reply path;
- explicit non-origin target creates a pending confirmation and performs no destination send;
- cancellation, timeout, and interrupt perform no send or mirror;
- approval sends exact content once;
- repeated approval click does not resend;
- changed home configuration after preview cannot change the frozen destination;
- standalone fallback is not used for an interactive send.

### 23.4 Approval tests

- configured admin initiating from private DM receives approval in that DM;
- same admin in a group does not automatically move approval there unless policy allows it;
- non-admin requester gets waiting status only;
- admin fallback receives request and only configured user can act;
- shared fallback is denied by default;
- `super_admin` auto-approval remains exact-conversation scoped;
- request IDs prevent stale or FIFO approval of another action.

### 23.5 Mirror tests

- mirror occurs only after successful authorized delivery;
- no mirror on pending, cancel, timeout, denial, or failure;
- exact thread is required;
- multiple per-user target sessions cause a safe skip;
- mirror uses immutable authorized content and destination;
- same-origin response does not enter the cross-delivery mirror path.

### 23.6 Concurrency tests

Run parallel turns for:

- DM A and DM B on the same platform;
- two topics in the same Telegram chat;
- two Slack threads in the same channel;
- two concurrent cross-delivery confirmations in one session;
- approval and clarify pending at the same time; and
- a stale callback from a previous run generation.

Assert that no content, callback, grant, status, or mirror crosses turn/session/route boundaries.

### 23.7 Adapter contract tests

For Telegram, Slack, Feishu, and Discord:

- confirmation card is sent to origin route and thread;
- callback exposes authenticated user and source route;
- invalid actor is rejected;
- callback data carries request ID only, not sensitive payload;
- text fallback remains functional for adapters without rich UI.

### 23.8 Regression suite

The existing focused suite of 111 routing-related tests remains green after updating assertions that intentionally encode the old unconditional admin-route and bare-platform-home semantics. The full project test suite, formatting, lint, and type checks must pass before merge.

### 23.9 Property test

A property-based test should generate arbitrary origin/destination routes and verify:

```text
interactive AND origin_only_kind AND destination != origin
    => adapter_send_count == 0
```

This test guards future platforms and route formats against accidental exceptions.

## 24. Rollout Plan

### PR 1 — Stop Implicit Route Escapes

**Goal:** Eliminate the disclosure immediately while preserving deliberate cross-send capability.

Changes:

- add typed route comparison helpers;
- change interactive `send_message` to reject bare platform and same-origin targets;
- add minimal request-scoped cross-delivery confirmation in origin;
- ensure destination receives nothing before approval;
- update tool and session prompts;
- add negative route-isolation and concurrency tests;
- keep cron/system bare-platform behavior unchanged.

This PR is independently safe and production-usable.

### PR 2 — Typed Turn Context and Outbound Policy

**Goal:** Replace ad hoc routing inputs with one immutable runtime contract.

Changes:

- add `TurnContext`, `ConversationRoute`, `ActorIdentity`, and `ExecutionMode`;
- set/reset typed context in `GatewayRunner`;
- propagate it through existing worker context copying;
- add pure `OutboundPolicy` and `OutboundDeliveryService`;
- migrate `send_message` and confirmation sends to the service;
- prevent interactive standalone fallback.

Behavior should remain equivalent to PR 1 while the architecture becomes reusable.

### PR 3 — Approval Authority/Presentation Separation

**Goal:** Keep administrator interactions coherent without broadening super-admin authority.

Changes:

- split internal administrator principal and presentation policy;
- prefer exact private origin for an initiating administrator;
- use configured route only as delegated fallback;
- add requester waiting status;
- reject shared fallback by default;
- retain legacy YAML compatibility;
- update approval tests that currently require unconditional configured-route delivery.

### PR 4 — Delivery Recording, Media Integrity, and Cleanup

**Goal:** Close persistence and lifecycle gaps.

Changes:

- stage and hash attachments before confirmation;
- replace direct `mirror_to_session()` arguments with `SuccessfulDelivery`;
- mirror only exact successful routes;
- add structured metrics and audit events;
- clean pending artifacts on all lifecycle boundaries;
- remove routing dependencies on legacy `MARLOW_SESSION_*` environment variables;
- audit future model-selectable destination tools through the same policy.

## 25. Migration and Compatibility

### 25.1 Interactive `send_message`

This is an intentional safety-breaking change for ambiguous calls:

```text
before: send_message(target="telegram") -> home channel immediately
after:  interactive call -> ambiguous_destination, no send
```

The model can call `action="list"` and use the explicit `telegram:home` target when the home destination is genuinely intended. That send is confirmed in origin.

### 25.2 Cron and system delivery

Existing scheduled jobs using `deliver="telegram"` continue to resolve the Telegram home channel. Gateway startup/restart notifications remain system notices and are unaffected.

### 25.3 Platform plugins

Plugins without a rich confirmation UI inherit the base text fallback. Existing `send()` signatures do not change.

### 25.4 Administrator configuration

Existing admin fields continue to load. New safe presentation behavior applies automatically when the configured administrator initiates from a private conversation. Operators relying on a shared fallback must explicitly opt in.

### 25.5 Feature flag and rollback

A temporary operator flag may control rich confirmation presentation, but **must not disable the route policy**. Rollback may switch from native buttons to text confirmation; it may not restore unconfirmed cross-conversation sends.

## 26. Risks and Mitigations

### 26.1 Additional confirmation friction

**Risk:** Every interactive cross-conversation send requires a click.

**Mitigation:** Keep confirmation concise, show exact destination, and perform the send directly after approval without a second agent round trip. Consider scoped destination grants only after real usage data and a separate security review.

### 26.2 Model repeatedly calls `send_message`

**Risk:** Bad model behavior creates repeated cards.

**Mitigation:** Improve the schema, return explicit no-retry results, cache denied payload digests for the turn, and use existing tool-loop guardrails.

### 26.3 Platform callback differences

**Risk:** Adapters expose user/thread data differently.

**Mitigation:** Define a normalized callback contract and retain a text fallback. An adapter that cannot authenticate the callback actor cannot support rich approval and must fail closed or use a safe command path.

### 26.4 Approval fallback becomes unavailable

**Risk:** Shared fallback is rejected and no private admin route exists.

**Mitigation:** Fail closed with a clear operator-facing configuration error and requester status. Do not silently ask the requester to self-approve a privileged action.

### 26.5 Partial media delivery

**Risk:** Text sends but an attachment fails.

**Mitigation:** Normalize a `partial` delivery result, report exact delivered parts to origin, and mirror only what the adapter confirms. Atomic multi-part delivery is outside this design.

### 26.6 Route equality across plugin platforms

**Risk:** A plugin has unusual thread semantics.

**Mitigation:** Default to strict exact IDs. Later allow `PlatformEntry` to provide a route canonicalizer, but never default to coarser equality.

## 27. Rejected Alternatives

### 27.1 Prompt-only instruction

Rejected because a model can ignore, misunderstand, or regress from prompt guidance. Privacy isolation must be runtime-enforced.

### 27.2 Interpret every bare platform as current origin

Rejected because it silently changes the meaning of a cross-platform messaging tool and still leaves target ambiguity. Current-conversation replies should use the normal response path, not a side-effecting tool.

### 27.3 Only patch Telegram

Rejected because the conflicting authorities are platform-independent and affect Slack, Discord, Feishu, email, and future plugins.

### 27.4 Disable `send_message`

Rejected because deliberate cross-channel messaging is a core useful capability. The problem is missing authorization, not the existence of the feature.

### 27.5 Disable session mirroring

Rejected because it would remove useful receiving-side context but would not stop the original disclosure. Mirroring should be downstream of authorization and success.

### 27.6 Always route approvals to one admin channel

Rejected because it conflates authority with presentation, breaks conversation coherence for the initiating administrator, and can expose request details to unrelated channel participants.

## 28. Acceptance Criteria

The design is complete when all of the following are true:

1. A user-originated interactive turn cannot deliver content outside its exact origin without a one-time, exact-payload confirmation.
2. No destination content is sent before that confirmation.
3. A bare platform target in an interactive turn never resolves a home channel.
4. Normal reply, interim, progress, clarify, error, completion, and requester status stay in origin.
5. Different threads/topics are isolated even inside the same chat/channel.
6. The configured administrator can approve a request in the private DM from which that administrator initiated it without receiving blanket super-admin authority there.
7. A non-admin request uses an explicit delegated admin flow and the final result returns to requester origin.
8. Shared admin fallback is off by default and visible when enabled.
9. Cancelled, expired, denied, failed, stale, or replayed requests perform zero destination sends and zero success mirrors.
10. A successful cross-send occurs once, uses the exact confirmed payload, and records the exact destination.
11. Concurrent sessions and requests cannot resolve, deliver, or mirror one another's content.
12. Scheduled/system home-channel delivery continues to work.
13. Existing platform adapters remain compatible through a base confirmation fallback.
14. Focused and full regression suites pass, including explicit negative security assertions.

## 29. Recommendation

Implement the four pull requests in order, with PR 1 treated as a privacy and routing correctness fix rather than a messaging UX enhancement.

The central product rule should be documented in code, tests, and operator documentation:

> Marlow replies where the request came from. Marlow sends somewhere else only after the initiating user has approved the exact destination and content.

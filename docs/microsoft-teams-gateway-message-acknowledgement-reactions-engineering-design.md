# Microsoft Teams Gateway — Message Acknowledgement Reactions

**Status:** Proposal — Ready for Engineering Review  
**Document type:** Engineering Design Amendment  
**Parent design:** `Microsoft Teams Gateway`  
**Related design:** `Microsoft Teams Gateway — Full Channel Thread Context`  
**Date:** 2026-09-01  
**Primary owners:** Teams Platform Plugin, Gateway Runtime  
**Scope:** Personal chats, group chats, and supported standard-channel turns accepted by the existing Microsoft Teams adapter

---

## 1. Executive Summary

Marlow can take a noticeable amount of time to answer a Microsoft Teams message
when a turn includes model latency, tool execution, channel-thread hydration, or
contention from several active sessions. Teams typing indicators are transient
and may not remain visible for the full operation. Users can therefore be unsure
whether Marlow accepted their request or whether the message was lost.

This amendment adds an opt-in, best-effort acknowledgement reaction:

```text
accepted Teams turn starts processing
                ↓
add 👀 to the triggering user message
                ↓
continue the normal Marlow turn unchanged
                ↓
send the existing text response or error message
```

The first release is intentionally acknowledgement-only. It does not remove or
replace the reaction when processing completes. The normal reply remains the
authoritative completion signal.

The implementation uses the reaction operation already available in the pinned
`microsoft-teams-apps==2.0.16` SDK. It does not use Microsoft Graph, delegated
user authentication, new Teams manifest permissions, a worker process, or a
thread pool.

The central invariant is:

> Reaction delivery is optional; normal Teams message processing and delivery
> must remain correct and available when every reaction operation fails.

---

## 2. Decisions Requested

Approval of this design means approving the following decisions:

1. **Ship acknowledgement-only behavior first.** Add one persistent `👀`
   reaction to the triggering user message when application processing starts.
2. **Keep text delivery authoritative.** Do not represent success, failure, or
   cancellation solely through reactions.
3. **Use the Teams SDK conversation reaction API.** Do not use Microsoft Graph
   `setReaction` or delegated user permissions.
4. **Use the inbound activity's service URL.** Build a service-URL-bound API
   client rather than assuming the SDK's default Bot Framework endpoint is
   correct for every tenant or region.
5. **Make reaction work non-blocking and best effort.** Schedule a tracked async
   task; never await the remote operation on the agent's critical path.
6. **Apply an adapter-wide local rate limit.** Permit at most two reaction
   operations per second with a burst of two, matching the documented service
   budget and reducing avoidable 429 responses.
7. **Do not queue stale reactions.** If local capacity is unavailable or a retry
   cannot complete while the acknowledgement is still useful, drop the
   reaction and continue the turn.
8. **Default the feature off.** Roll it out through configuration without a
   manifest update or app reinstall.
9. **Reuse the existing processing lifecycle hook.** Do not add a second
   message-state framework or alter shared gateway semantics for this feature.
10. **Do not broaden current Teams scope.** Private channels, shared channels,
    inbound user reactions, and proactive notifications remain separate work.

---

## 3. Baseline and Design Delta

### 3.1 Current codebase baseline

The current implementation already provides the pieces required for a narrow
adapter-local change:

| Existing capability | Current location | Reused by this design |
| --- | --- | --- |
| Microsoft Teams SDK app and bot credentials | `plugins/platforms/teams/adapter.py` | Yes |
| Authenticated inbound Bot Framework activity | Teams SDK HTTP server integration | Yes |
| Serialized Teams conversation reference | `_build_dispatch_task()` / `SessionSource.metadata` | Yes |
| Stable inbound activity ID | `MessageEvent.message_id` | Yes |
| Asynchronous message-processing lifecycle | `PlatformAdapter.on_processing_start()` | Yes |
| Hook error isolation | `_run_processing_hook()` | Yes |
| Teams telemetry facade | `TeamsTelemetry` | Yes |
| Adapter disconnect lifecycle | `TeamsPlatformAdapter.disconnect()` | Yes |
| Existing reaction patterns on other platforms | Slack, Telegram, Discord, Feishu adapters | Behavioral precedent only |

The gateway defines processing lifecycle hooks at
`gateway/platforms/base.py:3285-3303`. The start hook runs immediately before
the central message handler at `gateway/platforms/base.py:4253-4257`. Hook
exceptions are already isolated from normal message flow.

The Teams adapter creates a `MessageEvent` whose:

```text
message_id              = inbound Teams activity.id
source.metadata         = serialized Teams conversation reference
teams_reference fields  = service URL, conversation, bot, and channel data
```

at `plugins/platforms/teams/adapter.py:1747-1771`.

### 3.2 Important current SDK integration detail

The adapter initializes the official SDK and then restores a custom
`server.on_request` handler. The custom handler preserves the existing Marlow
validation, receipt, dispatch, and fast-HTTP-acknowledgement behavior, but it
means adapter lifecycle methods do not receive the SDK's `ActivityContext`
directly.

Therefore this design does not assume that `ctx.api` is available inside
`on_processing_start()`. Instead, it derives a target from the serialized
conversation reference and creates a public SDK API client bound to that
activity's service URL.

### 3.3 Design delta

This amendment adds only:

- reaction configuration;
- an immutable reaction-target value;
- a service-URL-bound Teams API-client factory/cache;
- a small non-blocking token bucket;
- one best-effort `add_reaction` operation from `on_processing_start()`;
- task tracking and shutdown cleanup;
- telemetry, tests, and documentation.

It does not change:

- JWT validation;
- tenant validation;
- mention gates;
- receipt deduplication;
- session identity;
- full-thread-context hydration;
- agent concurrency;
- pairing or user authorization;
- response generation;
- Bot Framework response routing; or
- any non-Teams adapter.

---

## 4. Problem Statement

A user can currently experience this sequence:

```text
20:00:00  User: @Marlow inspect this failure and explain the root cause
20:00:01  Teams briefly shows typing
20:00:05  Typing indicator disappears or is not visible on another client
20:00:40  Marlow is still processing tools or waiting for the model
20:01:10  User does not know whether the bot received the message
```

This is especially visible when:

- the current Teams thread must first be hydrated through Microsoft Graph;
- the model performs several calls or tool actions;
- another active turn is using shared process capacity;
- the same Teams session serializes subsequent messages;
- the user switches between desktop and mobile clients; or
- the typing activity is delayed, hidden, or expires.

Sending a separate acknowledgement message would solve visibility but would also
pollute the conversation and alter the thread transcript. A reaction is a better
fit for a lightweight transport acknowledgement.

---

## 5. Goals

### 5.1 Functional goals

1. Add a visible acknowledgement to the exact Teams message that triggered a
   supported Marlow turn.
2. Add the reaction when that turn begins processing, not merely when the HTTP
   request arrives or while it is still queued behind the same session.
3. Support all conversation scopes already accepted by the Teams adapter:
   personal chat, group chat, and standard channel thread.
4. Preserve current mention-only behavior for group and channel conversations.
5. Preserve current duplicate suppression and capacity behavior.
6. Make the feature independently configurable and disabled by default.
7. Preserve normal response delivery when acknowledgement cannot be delivered.

### 5.2 Reliability goals

1. Never delay the Bot Framework HTTP acknowledgement.
2. Never delay model execution, tool execution, or final message delivery while
   waiting for the reaction API.
3. Bound network time, retry count, in-flight tasks, rate, and client-cache size.
4. Avoid stale reaction queues during traffic bursts.
5. Leave no orphaned asyncio tasks during clean adapter shutdown.
6. Add no new Python threads, native workers, processes, or executors.
7. Keep idle overhead effectively zero when the feature is disabled.

### 5.3 Security and privacy goals

1. Derive every reaction target only from an already-validated inbound activity.
2. Prevent model text, tool arguments, message content, or external URLs from
   selecting the target conversation or service endpoint.
3. Use the existing bot identity and authentication path.
4. Request no additional Teams or Graph permissions.
5. Log no message body, access token, client secret, or complete conversation
   reference.
6. Ensure a reaction does not alter authorization, actor identity, pairing, or
   approval decisions.

### 5.4 Operational goals

1. Make success, throttling, timeout, invalid-target, and remote failures
   observable.
2. Allow immediate rollback by configuration.
3. Permit live validation without rebuilding or reinstalling the Teams app
   package.
4. Provide enough metrics to decide whether terminal reactions are safe in a
   later release.

---

## 6. Non-Goals

This amendment does not implement:

- inbound `messageReaction` event handling;
- user-reaction-triggered commands or approvals;
- automatic reaction removal;
- `👀 → ✅`, `👀 → ❌`, or another completion state machine;
- a durable reaction ledger;
- guaranteed reaction delivery;
- retry queues that survive process restart;
- proactive notifications;
- reactions on Marlow's own outbound messages;
- reactions on duplicate or unsupported activities;
- reactions for messages rejected before processing;
- Microsoft Graph `setReaction`;
- delegated user sign-in for reactions;
- new RSC or tenant-wide Graph permissions;
- Teams device permissions;
- private-channel or shared-channel enablement;
- a migration from the adapter's custom SDK HTTP handler to the standard
  `ActivityContext` router;
- changes to agent scheduling or global concurrency control; or
- using reactions to communicate tool approval or security state.

---

## 7. User Experience Contract

### 7.1 First-release behavior

```text
User sends a supported Teams message
              │
              ▼
Existing Teams validation and receipt gates pass
              │
              ▼
Turn reaches actual processing start
              │
              ├───────────────┐
              │               │
              ▼               ▼
Schedule best-effort       Start normal
👀 reaction task           Marlow handling
              │               │
              └──── no dependency ────┘
                              │
                              ▼
                    Existing text response
```

The reaction remains on the triggering message after the response is sent.
This is intentional: it records that Marlow acknowledged that user turn. It is
not a live processing indicator.

### 7.2 Reaction semantics

The reaction means:

> Marlow received this Teams turn and began application processing.

It does not mean:

- the turn will succeed;
- the final response was delivered;
- the model agrees with the message;
- an administrator approved an action;
- a command has executed;
- the content is trusted;
- the user is authorized for anything beyond the current gateway result; or
- the reaction itself is a durable workflow record.

### 7.3 Behavior matrix

| Condition | Reaction behavior | Existing turn behavior |
| --- | --- | --- |
| Feature disabled | No operation | Unchanged |
| Supported turn starts | Schedule `👀` add | Continue immediately |
| Same-session message is pending | No reaction yet | Existing queue/merge behavior |
| Pending message later starts | Schedule `👀` add then | Existing processing |
| Successful response | Leave `👀` | Send response normally |
| Handler failure | Leave `👀` if it was added | Existing failure handling |
| Cancellation | Leave `👀` if it was added | Existing cancellation handling |
| Duplicate receipt | No reaction | Existing duplicate acknowledgement |
| Invalid tenant/scope/mention | No reaction | Existing rejection/ignore behavior |
| Dispatch capacity exhausted | No reaction | Existing 503 behavior |
| Empty message after media processing | No reaction through lifecycle hook | Existing receipt rollback |
| Local reaction rate budget unavailable | Drop reaction | Continue turn |
| Remote 429/timeout/404/403/5xx | Best-effort bounded handling, then drop | Continue turn |
| Adapter disconnect | Cancel outstanding reaction tasks | Existing shutdown continues |

---

## 8. Hard Invariants

The following are normative.

### 8.1 Reaction is not on the critical path

```text
reaction latency or failure
    => no additional wait before agent handling
    => no change to final response status
    => no change to receipt state
```

`on_processing_start()` must schedule work and return without awaiting network
I/O.

### 8.2 Exact-message targeting

For an inbound event `E`, the reaction target must be:

```text
service_url     = E.source.metadata.teams_reference.service_url
conversation_id = E.source.metadata.teams_reference.conversation.id
activity_id     = E.message_id
```

The adapter must not react using:

- Marlow's composite `chat_id`;
- the channel ID alone;
- a Graph root-message ID unless it is also the triggering activity ID;
- the generated response activity ID; or
- IDs supplied by the model.

### 8.3 Validated-source-only

A reaction task may be created only for a `MessageEvent` produced by the current
Teams inbound path after its existing JWT, tenant, conversation-scope, mention,
capacity, and receipt gates.

### 8.4 No permission expansion

Enabling reactions must require no change to:

```text
manifest authorization.permissions
webApplicationInfo
Microsoft Graph application permissions
devicePermissions
```

### 8.5 Best-effort fail-open behavior

Every reaction exception must be contained inside the reaction subsystem. It
must not propagate into `_message_handler`, response delivery, receipt handling,
or adapter health.

### 8.6 Bounded resource usage

- No per-message executor or thread pool.
- No unbounded asyncio queue.
- No unbounded task set.
- No unbounded service-URL client cache.
- No unbounded retries.
- No content-size-dependent local CPU work.

### 8.7 Idempotence

Repeated add operations for the same bot, reaction type, and message are treated
as safe. Marlow's receipt store remains the primary duplicate gate; SDK/service
idempotence is defense in depth, not a replacement for receipts.

---

## 9. Microsoft Platform Contract

### 9.1 Supported operation

The Microsoft Teams agent reaction API permits an agent to add or remove its own
reaction on messages in conversations in which the agent participates.

The pinned Python SDK exposes this through the conversation client:

```python
await api.conversations.add_reaction(
    conversation_id,
    activity_id,
    reaction_type,
)
```

For the first release:

```text
reaction_type = 1f440_eyes
rendered form = 👀
```

### 9.2 Why this is not Microsoft Graph

Microsoft Graph message reaction APIs have a different authorization model and
are not required for a bot reacting through its active Teams conversation.
Using Graph here would add delegated-authentication complexity and couple a
transport acknowledgement to unrelated thread-context permissions.

This design therefore keeps the two concerns separate:

```text
Bot Framework / Teams SDK reaction API
    => acknowledge the triggering message

Microsoft Graph + ChannelMessage.Read.Group
    => optionally hydrate standard-channel thread context
```

Either capability may be enabled without relying on the other.

### 9.3 Service URL requirement

Bot Framework activities include a `serviceUrl`. The endpoint can vary by cloud
or region. An API client used for an inbound conversation operation must be
bound to that activity's validated service URL rather than blindly using a
single process-default URL.

### 9.4 Rate limit

Teams documents a reaction budget of approximately two add/remove operations
per second across all conversations in which an agent participates. Remote 429
responses and their `Retry-After` value remain authoritative.

The local limiter is a protection mechanism, not a promise that the service will
never throttle.

### 9.5 Reaction visibility is not guaranteed workflow delivery

A Teams client may delay, collapse, or make reactions less prominent than text
messages. The feature must therefore remain an acknowledgement enhancement, not
a required business-state signal.

---

## 10. Permissions and Manifest Impact

### 10.1 Required manifest changes

None.

The following are not reaction permissions and must not be added for this
feature:

```json
"devicePermissions": ["notifications"]
```

```text
ChannelMessage.Send.Group
ChatMessage.Send.Chat
ChannelMessage.Read.Group
ChatMessage.Read.Chat
```

`ChannelMessage.Read.Group` may still be required independently by the full
channel-thread-context feature, but it is not required by reactions.

### 10.2 Bot manifest state

The existing bot must remain conversational:

```json
"isNotificationOnly": false
```

This design does not convert Marlow into a notification-only bot.

### 10.3 App package version

A code/config-only rollout does not require a Teams manifest version increment
or app reinstall. If the app package is changed for unrelated reasons, normal
package-version rules still apply.

---

## 11. Architecture

### 11.1 Component view

```text
Microsoft Teams
      │
      │ authenticated Bot Framework activity
      ▼
Teams SDK HTTP server
      │
      ▼
TeamsPlatformAdapter._handle_teams_activity
      │
      ├─ tenant/scope/mention checks
      ├─ capacity check
      ├─ receipt claim
      └─ build MessageEvent + teams_reference
      │
      ▼
Teams dispatch supervisor
      │
      ▼
PlatformAdapter.handle_message
      │
      ▼
_process_message_background
      │
      ├─ on_processing_start(event)
      │      │
      │      └─ create tracked async reaction task
      │             ├─ validate target
      │             ├─ local rate-limit check
      │             ├─ get service-URL API client
      │             └─ add 👀 with timeout/bounded retry
      │
      └─ _message_handler(event) immediately continues
             │
             ▼
          Agent/tools
             │
             ▼
      Existing Bot Framework response delivery
```

### 11.2 Ownership boundaries

| Concern | Owner |
| --- | --- |
| JWT and Bot Framework activity validation | Teams SDK / existing adapter integration |
| Tenant, scope, mention, capacity, receipt gates | Existing Teams adapter |
| Session and processing lifecycle | Shared gateway runtime |
| Reaction target extraction | Teams adapter |
| Reaction API client and transport | Teams adapter |
| Reaction throttling and retry policy | Teams adapter |
| Model, tools, approvals, response content | Existing gateway/agent runtime |
| Thread context hydration | Existing separate Teams thread-context subsystem |

### 11.3 Why no shared-gateway change is required

The base adapter already exposes an isolated processing-start hook. Teams can
override that hook in the same way other platform plugins add reactions without
adding Teams concepts to platform-neutral code.

No change to `ProcessingOutcome` or `on_processing_complete()` is required for
the acknowledgement-only first release.

---

## 12. Data Contracts

### 12.1 Configuration

```python
@dataclass(frozen=True, slots=True)
class TeamsReactionConfig:
    enabled: bool = False
```

Operational constants remain code-owned in the first release:

```python
TEAMS_ACK_REACTION = "1f440_eyes"
DEFAULT_REACTION_RATE_PER_SECOND = 2.0
DEFAULT_REACTION_BURST = 2
DEFAULT_REACTION_TIMEOUT_SECONDS = 1.5
DEFAULT_REACTION_FRESHNESS_SECONDS = 3.0
DEFAULT_REACTION_CLIENT_CACHE_SIZE = 8
DEFAULT_REACTION_MAX_RETRIES = 1
```

Avoid exposing reaction IDs, retry loops, queue size, or arbitrary service URLs
as user configuration in the first release.

### 12.2 Reaction target

```python
@dataclass(frozen=True, slots=True)
class TeamsReactionTarget:
    service_url: str
    conversation_id: str
    activity_id: str
```

Validation requirements:

- all fields are non-empty strings;
- `service_url` is HTTPS;
- the URL is normalized only by safe canonicalization such as removing trailing
  slashes;
- no URL query, fragment, credentials, or locally supplied override is accepted;
- `conversation_id` comes from the conversation reference;
- `activity_id` equals `MessageEvent.message_id`.

### 12.3 Operation result

A small internal result value simplifies telemetry and tests:

```python
class TeamsReactionResult(str, Enum):
    SUCCESS = "success"
    DISABLED = "disabled"
    INVALID_TARGET = "invalid_target"
    LOCAL_RATE_LIMITED = "local_rate_limited"
    REMOTE_RATE_LIMITED = "remote_rate_limited"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    TRANSIENT_FAILURE = "transient_failure"
    FAILURE = "failure"
    CANCELLED = "cancelled"
```

This result is operational only. It must not affect the user turn.

### 12.4 Runtime state

```python
self._reaction_config: TeamsReactionConfig
self._reaction_tasks: set[asyncio.Task[Any]]
self._reaction_limiter: TeamsReactionRateLimiter
self._reaction_api_clients: OrderedDict[str, ApiClient]
```

No per-message durable state is required because the first release performs one
add operation and no completion transition.

---

## 13. Target Extraction

### 13.1 Algorithm

```python
def _reaction_target_for_event(event: MessageEvent) -> TeamsReactionTarget | None:
    metadata = event.source.metadata
    if not isinstance(metadata, dict):
        return None

    reference_data = metadata.get("teams_reference")
    if not isinstance(reference_data, dict):
        return None

    # Reuse the adapter's existing SDK-model reconstruction so the helper does
    # not depend on the serialized alias spelling used by a particular SDK
    # version.
    reference = _ref_from_dict(reference_data)
    if reference is None:
        return None

    service_url = normalize_https_service_url(reference.service_url)
    conversation_id = normalized_nonempty(
        getattr(reference.conversation, "id", None)
    )
    activity_id = normalized_nonempty(event.message_id)

    if not service_url or not conversation_id or not activity_id:
        return None

    return TeamsReactionTarget(
        service_url=service_url,
        conversation_id=conversation_id,
        activity_id=activity_id,
    )
```

The exact serialized key name must follow the existing
`_conversation_reference_dict()` output. Tests must use the real adapter helper
rather than an invented reference shape.

### 13.2 Channel-thread correctness

For standard-channel replies:

- `conversation_id` identifies the active Bot Framework thread route;
- `activity_id` identifies the exact triggering reply;
- the reaction is attached to the reply that mentioned Marlow, not to the root
  post and not to Marlow's eventual response.

This target is independent of Graph thread hydration. Hydration may use a root
message locator, while reaction delivery always uses the triggering activity's
Bot Framework conversation reference.

### 13.3 Invalid target behavior

Invalid or incomplete metadata results in:

```text
metric: teams_reaction_operations_total{operation="add",result="invalid_target"}
log: debug/warning without raw reference values
turn: continue normally
```

There is no attempt to reconstruct a target from global adapter state or a
previous message reference.

---

## 14. Service-URL-Bound SDK Client

### 14.1 Requirement

The current adapter does not retain SDK `ActivityContext` objects in
`MessageEvent`, so `ctx.api` is unavailable at lifecycle-hook time. The adapter
must still use public SDK primitives and the current bot credentials.

### 14.2 Recommended factory

Conceptually:

```python
def _reaction_api_for_service_url(self, service_url: str) -> ApiClient:
    normalized = normalize_https_service_url(service_url)

    cached = self._reaction_api_clients.get(normalized)
    if cached is not None:
        self._reaction_api_clients.move_to_end(normalized)
        return cached

    api = ApiClient(
        normalized,
        self._teams_app.api.http,
        self._teams_app.options.api_client_settings,
        cloud=self._teams_app.cloud,
    )

    self._reaction_api_clients[normalized] = api
    self._reaction_api_clients.move_to_end(normalized)
    while len(self._reaction_api_clients) > DEFAULT_REACTION_CLIENT_CACHE_SIZE:
        self._reaction_api_clients.popitem(last=False)

    return api
```

Implementation must verify the exact public constructor and shared/clone HTTP
client behavior against SDK 2.0.16 tests. It must not reach into private token
manager methods.

The operation then uses the non-deprecated conversation API:

```python
await api.conversations.add_reaction(
    target.conversation_id,
    target.activity_id,
    TEAMS_ACK_REACTION,
)
```

### 14.3 Why not `self._teams_app.api` directly

The app-level API client is convenient but is associated with a default service
URL. Using it for every inbound conversation may be incorrect for regional,
sovereign-cloud, or migrated Bot Framework endpoints.

### 14.4 Why not persist API clients or tokens

The cache is process-local and bounded. It stores no message content and does
not create a new credential store. It is cleared during disconnect. Token
refresh remains the SDK's responsibility.

### 14.5 Service URL trust

The URL comes from an authenticated Bot Framework activity. Even so, the
adapter must require HTTPS and reject malformed URLs before constructing a
client. It must not accept a service URL from message text, model output,
configuration, or tool arguments.

---

## 15. Processing Lifecycle Integration

### 15.1 Hook implementation

```python
async def on_processing_start(self, event: MessageEvent) -> None:
    if not self._reaction_config.enabled:
        return

    target = _reaction_target_for_event(event)
    if target is None:
        self._record_reaction_result("invalid_target")
        return

    self._spawn_reaction_task(self._add_acknowledgement_reaction(target))
```

The method contains no remote `await` and returns immediately after task
creation.

### 15.2 Task tracking

```python
def _spawn_reaction_task(self, coroutine: Coroutine[Any, Any, Any]) -> None:
    task = asyncio.create_task(coroutine, name="teams-reaction-add")
    self._reaction_tasks.add(task)

    def _done(completed: asyncio.Task[Any]) -> None:
        self._reaction_tasks.discard(completed)
        try:
            completed.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Unhandled Teams reaction task failure")

    task.add_done_callback(_done)
```

The operation coroutine must normally absorb and classify its own expected
errors. The done callback is final defense against an unobserved task exception.

### 15.3 When the hook runs

The shared lifecycle hook runs when the gateway starts processing a turn. This
has useful queue semantics:

```text
message received while same session active
    => existing pending behavior
    => no immediate processing-start hook
    => no reaction yet

pending turn later begins processing
    => processing-start hook
    => acknowledgement reaction
```

This avoids acknowledging a message as started while it is still waiting behind
another turn.

### 15.4 Authorization semantics

The hook runs after Teams transport validation, mention, capacity, and receipt
gates, but before the central message handler completes all application-level
logic. Therefore the reaction is transport/application acknowledgement, not an
authorization grant.

If future product requirements demand “react only after final central ACL
acceptance,” the shared gateway must expose a new post-authorization lifecycle
hook. That cross-platform change is not required for this release and must not
be approximated with duplicated authorization logic inside the Teams adapter.

### 15.5 Bypass commands

The gateway currently has special same-session command/bypass paths that may not
use the normal processing lifecycle. This design does not change them. Tests
must document which command paths receive acknowledgement under the current
base-adapter semantics rather than silently claiming universal coverage.

---

## 16. Rate Limiting and Freshness

### 16.1 Local token bucket

Use one limiter per Teams adapter instance:

```python
class TeamsReactionRateLimiter:
    def __init__(self, rate_per_second: float = 2.0, burst: int = 2): ...

    async def try_acquire(self) -> bool:
        """Refill by monotonic time and consume one token without waiting."""
```

Properties:

- rate: `2.0` operations/second;
- capacity: `2` operations;
- one asyncio lock protects refill/consume;
- no sleeper and no pending queue;
- cancellation-safe;
- monotonic clock only.

### 16.2 Why drop instead of queue

An acknowledgement received tens of seconds later is misleading and provides
little user value. During a burst, normal replies are more important than
reaction completeness.

Therefore:

```text
no local token available
    => record local_rate_limited
    => drop reaction
    => continue message processing
```

### 16.3 Process and deployment scope

The local limiter covers one adapter instance in one process. Multiple Marlow
processes using the same bot identity can still exceed the service-wide budget.
Remote 429 handling remains required.

The existing Teams credential-lock behavior should prevent accidental duplicate
local instances for the same identity on one profile, but it is not a
distributed rate limiter.

### 16.4 Fairness

The first release uses simple arrival order at task scheduling time. It does not
reserve per-user, per-platform, or per-conversation quotas. Because reactions
are optional and one operation per turn, a more elaborate fairness scheduler is
not justified initially.

---

## 17. Timeout, Retry, and Error Classification

### 17.1 Freshness budget

An add reaction should finish within:

```text
DEFAULT_REACTION_FRESHNESS_SECONDS = 3.0
```

This is an operation deadline, not a delay imposed on the user turn.

### 17.2 Per-attempt timeout

Each remote attempt is bounded by:

```text
DEFAULT_REACTION_TIMEOUT_SECONDS = 1.5
```

Use `asyncio.timeout()` or an equivalent cancellation-safe primitive.

### 17.3 Retry policy

At most one retry is allowed.

Retry only when all conditions are true:

- the failure is explicitly retryable, primarily HTTP 429 or a selected
  transient 5xx/network condition;
- `Retry-After`, when present, is valid and non-negative;
- sleeping and retrying can still complete inside the freshness deadline;
- a local rate-limit token can be acquired for the retry; and
- adapter shutdown has not begun.

Do not retry:

- invalid target;
- 400-class validation errors;
- 401/403 identity or permission errors;
- 404 or deleted message;
- a bot removed from the conversation;
- malformed `Retry-After` beyond the freshness window;
- cancellation during disconnect; or
- any error after the retry limit.

### 17.4 Failure mapping

| Failure | Result | Retry? | User-turn impact |
| --- | --- | --- | --- |
| Local token unavailable | `local_rate_limited` | No | None |
| HTTP 429 | `remote_rate_limited` | Once if fresh | None |
| Timeout | `timeout` | Only if fresh and policy allows | None |
| 404/deleted target | `not_found` | No | None |
| 401/403 | `forbidden` | No | None; operator alert |
| Selected 5xx/network | `transient_failure` | Once if fresh | None |
| Other exception | `failure` | No | None |
| Disconnect cancellation | `cancelled` | No | None |

---

## 18. Configuration

### 18.1 User-facing configuration

```yaml
teams:
  enabled: true

  reactions:
    enabled: false
```

Default:

```text
false
```

### 18.2 Optional environment override

For operational parity with other adapters, the implementation may support:

```dotenv
TEAMS_REACTIONS=true
```

Precedence should follow one documented rule. Recommended:

```text
explicit environment override
    > explicit YAML value
    > default false
```

Do not add environment overrides for reaction ID, service URL, timeout, or rate
in the first release unless an existing project-wide configuration convention
requires them.

### 18.3 Example complete Teams fragment

```yaml
teams:
  enabled: true
  client_id: "<entra-application-client-id>"
  tenant_id: "<entra-directory-tenant-id>"
  host: "127.0.0.1"
  port: 3978

  thread_context:
    enabled: true
    require_complete: true

  reactions:
    enabled: true
```

Thread context and reactions are independent flags.

### 18.4 Configuration validation

- Missing block: feature disabled.
- Non-boolean `enabled`: reject configuration with a clear error, following
  existing Teams config validation conventions.
- Feature enabled without a connected Teams app: no reaction operation; normal
  adapter startup validation remains authoritative.

---

## 19. Shutdown and Lifecycle

### 19.1 Connect

During successful adapter connection:

1. initialize reaction configuration;
2. initialize an empty task set;
3. initialize the token bucket;
4. initialize an empty bounded API-client cache;
5. start no background worker.

### 19.2 Disconnect

Disconnect order:

1. mark reaction subsystem as closing;
2. cancel outstanding reaction tasks;
3. await them with `return_exceptions=True` and a bounded cleanup timeout;
4. clear task state;
5. clear service-URL API clients;
6. continue existing Teams supervisor/server/app shutdown.

Reaction cleanup must not materially extend adapter shutdown.

### 19.3 Partial startup failure

If Teams app initialization fails, reaction state remains inert. Cleanup must be
idempotent even when connection did not reach completion.

### 19.4 Reconnect

A reconnect creates fresh task, limiter, and API-client state. No pending
reaction operation survives the prior connection.

---

## 20. Security and Privacy Analysis

### 20.1 Authentication

The reaction uses the same bot application identity as existing Teams send and
typing operations. This design creates no second identity or delegated user
token.

### 20.2 Authorization

A reaction communicates acknowledgement only. It does not participate in user
allowlisting, pairing, admin approval, command authorization, or tool dispatch.

Historical thread context must not be used to select a reaction target. Only the
current validated inbound activity supplies target IDs.

### 20.3 SSRF boundary

`serviceUrl` is a network destination. Although it originates in a signed Bot
Framework activity, implementation must still:

- require HTTPS;
- parse and normalize it with a URL parser;
- reject credentials, fragments, and malformed hosts;
- use the SDK's authenticated HTTP client;
- never accept overrides from message/model/tool content; and
- preserve any existing sovereign-cloud SDK behavior.

A strict Microsoft host allowlist should only be added if Microsoft publishes a
complete cloud-aware endpoint contract. An incomplete hardcoded public-cloud
allowlist could break legitimate sovereign deployments.

### 20.4 Logs and telemetry

Never log:

- raw message text;
- serialized conversation references;
- access tokens or authorization headers;
- client secrets;
- full service URLs with sensitive query data; or
- complete tenant, conversation, or activity IDs at info level.

Debug correlation may use a one-way hash or an existing request correlation ID.

### 20.5 Data retention

The feature stores no durable message or reaction record. Process-local task and
client state disappears at shutdown.

---

## 21. Failure Handling

### 21.1 Reaction subsystem unavailable

Examples:

- unsupported SDK object shape;
- API client cannot be constructed;
- credential refresh fails;
- remote Teams service unavailable.

Behavior:

```text
record failure
log bounded diagnostic
continue the user turn
```

### 21.2 Target message deleted

A user may delete the triggering message before the operation reaches Teams.
Treat the resulting not-found response as terminal and non-retryable.

### 21.3 Bot removed from conversation

Treat forbidden/not-found responses as terminal. Existing inbound traffic would
normally stop, but in-flight tasks can race with uninstall/removal.

### 21.4 Rate limiting

Local drops and remote 429s are expected under bursts. They are not adapter
health failures unless their sustained rate exceeds an alert threshold.

### 21.5 SDK compatibility regression

Because the design uses public SDK API surfaces but constructs a service-bound
client outside `ActivityContext`, dependency upgrades must run the dedicated
reaction integration tests. Keep the dependency pinned until those tests pass.

### 21.6 Unexpected exception

The task wrapper catches and records it. The base hook's error isolation is an
additional fallback, but the scheduled task itself must not produce an
unretrieved exception.

---

## 22. Observability

### 22.1 Counters

```text
teams_reaction_operations_total{
  operation="add",
  result="success|disabled|invalid_target|local_rate_limited|
          remote_rate_limited|timeout|not_found|forbidden|
          transient_failure|failure|cancelled"
}
```

Do not emit a `disabled` counter per message if that would create unnecessary
noise; it may instead appear only in status/config telemetry.

### 22.2 Latency

```text
teams_reaction_duration_seconds{
  operation="add",
  result="..."
}
```

Measure only the background reaction operation. It is not user-turn latency.

### 22.3 Runtime state

Expose or log at debug/status level:

```text
reaction_enabled
reaction_tasks_inflight
reaction_client_cache_size
reaction_limiter_tokens (optional debug only)
```

### 22.4 Alerts

Initial suggested signals:

- sustained `forbidden` or 401/403 results: likely identity or SDK endpoint
  configuration issue;
- `remote_rate_limited / attempted > 10%` over a meaningful window: reaction
  budget too high or multiple bot instances active;
- sustained timeout/transient-failure rate: Teams service/network degradation;
- in-flight tasks that do not return to zero after traffic stops: task leak.

Reaction success rate alone is not a core availability SLO.

### 22.5 Logging levels

| Event | Level |
| --- | --- |
| Local rate-limit drop | Debug or sampled info |
| Remote 429 | Debug/warning with aggregation |
| One timeout/transient failure | Debug/warning, rate limited |
| Sustained forbidden/auth failure | Warning/error |
| Unhandled task exception | Error |
| Successful reaction | No per-message info log |

---

## 23. Test Strategy

### 23.1 Unit tests — configuration

1. Missing `teams.reactions` defaults to disabled.
2. Explicit `enabled: false` creates no reaction task.
3. Explicit `enabled: true` enables the subsystem.
4. Invalid types fail configuration validation clearly.
5. Environment override precedence is deterministic if implemented.

### 23.2 Unit tests — target extraction

1. Personal-chat reference produces the expected service URL, conversation ID,
   and activity ID.
2. Group-chat reference produces the expected target.
3. Standard-channel-thread reference uses the active conversation/thread ID and
   triggering reply activity ID.
4. Marlow composite `chat_id` is never used as the SDK conversation ID.
5. Missing metadata, reference, conversation, message ID, or service URL returns
   no target.
6. Non-HTTPS or malformed service URL is rejected.
7. Same activity ID in two different conversations remains distinct.

### 23.3 Unit tests — SDK client factory

1. A client is bound to the normalized incoming service URL.
2. Repeated use of one service URL reuses the bounded cache entry.
3. Different service URLs receive distinct clients.
4. Cache eviction follows the configured maximum.
5. Disconnect clears the cache.
6. No private SDK token-manager method is used.
7. `api.conversations.add_reaction()` receives exact conversation, activity,
   and `1f440_eyes` arguments.

### 23.4 Unit tests — non-blocking lifecycle

1. `on_processing_start()` returns before a deliberately blocked fake reaction
   API completes.
2. Slow reaction delivery does not delay `_message_handler` invocation.
3. Reaction failure does not change the handler response.
4. Reaction failure does not change outbound delivery result.
5. The task is tracked until completion and then removed.
6. An unexpected task exception is consumed and logged.

### 23.5 Unit tests — rate and retry policy

1. The first two immediate operations pass with a burst of two.
2. Additional immediate operations are locally dropped without waiting.
3. Tokens refill by monotonic time.
4. Concurrent acquisition never exceeds the budget.
5. HTTP 429 with a short `Retry-After` retries at most once inside freshness.
6. HTTP 429 with a long `Retry-After` is dropped without sleeping past
   freshness.
7. Retry consumes a new local token.
8. 404 and 403 are not retried.
9. Timeout is bounded and classified.
10. Cancellation during shutdown is classified and does not log as failure.

### 23.6 Unit tests — message gates

1. Duplicate receipt produces no processing-start reaction.
2. Unsupported activity type produces no reaction.
3. Invalid tenant produces no reaction.
4. Group/channel message without structured bot mention produces no reaction.
5. Dispatch-capacity rejection produces no reaction.
6. Empty message after media filtering does not reach the processing hook.
7. A queued same-session turn reacts only when its actual processing begins.

### 23.7 Integration tests

Use the existing Teams adapter test harness with a fake SDK API transport:

1. The custom `server.on_request` handler remains installed after SDK
   initialization.
2. The inbound HTTP path returns its current acknowledgement without waiting for
   reaction latency.
3. A complete turn sends the same final response whether reaction add succeeds,
   times out, returns 429, or raises.
4. Full-thread-context hydration behavior is unchanged with reactions enabled.
5. Personal, group, and channel route references target their originating
   conversation.
6. Adapter disconnect with in-flight reaction operations completes cleanly.
7. Reconnect starts with no stale tasks or clients.

### 23.8 Performance tests

1. Enabling reactions creates no new OS thread or executor.
2. Disabled mode adds no per-turn task.
3. In-flight reaction task count remains bounded by currently scheduled work and
   drains after traffic stops.
4. A reaction API that never responds is terminated by timeout.
5. A burst does not create an unbounded queue.
6. Existing HTTP-acknowledgement and agent-start latency remain within test
   noise because remote reaction I/O is not awaited.

### 23.9 Live Teams validation

For each supported scope:

- personal chat;
- group chat with explicit mention;
- standard channel thread with explicit mention.

Validate:

1. `👀` appears on the exact triggering user message under normal rate budget.
2. The normal answer appears in the same conversation/thread.
3. A slow tool-heavy turn is acknowledged while it continues processing.
4. Two quick messages can use the configured burst.
5. A larger burst may omit reactions but still produces normal replies.
6. The feature works whether full thread context is enabled or disabled.
7. No new manifest consent or reinstall is required.
8. Removing the bot or deleting a message does not destabilize the adapter.

---

## 24. Rollout and Backward Compatibility

### 24.1 Compatibility

With the default configuration:

```yaml
teams:
  reactions:
    enabled: false
```

runtime behavior is unchanged.

Existing manifest packages, credentials, thread-context configuration, receipt
databases, session keys, and platform integrations remain compatible.

### 24.2 Rollout phases

#### Phase 0 — automated validation

- land unit and integration tests;
- keep feature disabled by default;
- verify pinned SDK behavior in CI.

#### Phase 1 — local/internal tenant

- enable for one internal bot instance;
- validate all supported scopes;
- monitor reaction result and 429 rates;
- confirm no increase in message loss or processing latency.

#### Phase 2 — broader opt-in

- document `teams.reactions.enabled`;
- allow operators to enable it explicitly;
- continue treating missing reactions as normal degradation.

#### Phase 3 — consider default-on

Only consider default-on after telemetry demonstrates:

- stable SDK behavior;
- low sustained 429 rate;
- no task leak;
- no response-path regression; and
- clear user value.

### 24.3 Rollback

Set:

```yaml
teams:
  reactions:
    enabled: false
```

and restart/reload according to existing Teams configuration semantics. No
manifest rollback is required.

---

## 25. Implementation Plan

### PR 1 — Reaction primitives and tests

Files expected to change:

```text
plugins/platforms/teams/adapter.py
plugins/platforms/teams/README.md
tests/gateway/test_teams_adapter.py
```

Optional test split:

```text
tests/gateway/test_teams_reactions.py
```

Deliverables:

- parse reaction configuration;
- add `TeamsReactionTarget` and result classification;
- add safe target extraction;
- add service-URL API-client factory/cache;
- add token bucket;
- add bounded reaction operation helper;
- unit-test SDK call arguments, throttling, retries, and errors.

### PR 2 — Lifecycle, shutdown, telemetry, and live validation

Deliverables:

- override `on_processing_start()`;
- schedule and track non-blocking tasks;
- cancel/gather on disconnect;
- add counters and latency observations;
- document configuration and semantics;
- add integration tests proving no critical-path dependency;
- complete live Teams test matrix.

The two PRs may be combined if review size remains small and all invariants are
covered.

---

## 26. Acceptance Criteria

The feature is complete when all of the following are true:

1. Reactions are disabled by default.
2. With reactions disabled, existing Teams tests and runtime behavior are
   unchanged.
3. With reactions enabled, a supported turn schedules a `1f440_eyes` reaction on
   the exact triggering message when processing begins.
4. Personal chat, group chat, and supported standard-channel thread targets are
   correct.
5. Duplicate, invalid, unsupported, and capacity-rejected activities do not
   react.
6. A pending same-session message does not react until it begins processing.
7. Reaction network I/O is never awaited by the model/response critical path.
8. A blocked, failed, throttled, or timed-out reaction cannot fail or delay the
   normal user turn.
9. The local adapter-wide limit is at most two operations per second with a
   burst of two.
10. Remote 429 retry behavior is bounded by one retry and the freshness deadline.
11. No new thread pool, worker process, or unbounded queue is introduced.
12. Outstanding tasks are cancelled and consumed on disconnect.
13. No new manifest or Graph permission is required.
14. Logs contain no message content, token, secret, or raw conversation
    reference.
15. Metrics distinguish success, local throttling, remote throttling, timeout,
    invalid target, authorization/permission failure, and generic failure.
16. Live validation proves that a slow turn can be acknowledged while the final
    response continues normally.
17. All repository tests required by the Teams plugin pass.

---

## 27. Definition of Done

- [ ] Design approved.
- [ ] Goal/plan committed under `.plans/`.
- [ ] Reaction config documented and defaulted off.
- [ ] Exact target extraction implemented.
- [ ] Public SDK conversation reaction API used.
- [ ] Regional service URL honored.
- [ ] Non-blocking tracked task wrapper implemented.
- [ ] Local token bucket and freshness deadline implemented.
- [ ] Bounded retry and error classification implemented.
- [ ] Disconnect cleanup implemented.
- [ ] Unit and integration coverage complete.
- [ ] Live personal-chat test complete.
- [ ] Live group-chat test complete.
- [ ] Live standard-channel-thread test complete.
- [ ] 429/failure degradation verified.
- [ ] No manifest permission changes introduced.
- [ ] Rollback verified through configuration.

---

## 28. Alternatives Considered

### 28.1 Continue using only typing indicators

**Rejected as the sole acknowledgement.** Typing is transient and not a durable
signal attached to the triggering message. It remains useful and should not be
removed.

### 28.2 Send an “Acknowledged” chat message

**Rejected.** It adds transcript noise, can create extra channel replies, and
may be more disruptive than the latency it addresses.

### 28.3 Add `👀`, then remove it and add `✅` or `❌`

**Deferred.** This is visually expressive but costs two or three operations per
turn, increases throttling probability, requires ordering/cleanup state, and can
leave misleading intermediate status after a failed delete.

The first release gathers real service telemetry before introducing that state
machine.

### 28.4 Add `👀`, then remove it without a terminal reaction

**Deferred.** This requires two operations per turn and creates cleanup risk.
A persistent acknowledgement has a simpler, stable meaning.

### 28.5 Use Microsoft Graph `setReaction`

**Rejected.** It is the wrong authorization surface for this bot operation,
would introduce delegated-user authentication concerns, and would couple
acknowledgement to unrelated Graph permissions.

### 28.6 Migrate the adapter to SDK `ActivityContext`

**Deferred.** This is the most idiomatic way to access `ctx.api`, but the current
adapter intentionally restores a custom HTTP handler to preserve validation,
receipt, dispatch, and acknowledgement behavior. A migration would be a broader
adapter redesign with substantially greater regression risk.

This design uses public SDK clients without changing the ingress architecture.

### 28.7 Use the app-level default API client for every conversation

**Rejected.** It risks targeting the wrong regional or cloud service endpoint.
The inbound activity's authenticated service URL is the correct route source.

### 28.8 Put reaction calls in `run_in_executor()`

**Rejected.** The SDK operation is asynchronous network I/O. Adding threads
would consume more process capacity and worsen the CPU/concurrency concerns that
motivated careful performance bounds.

### 28.9 Create a durable reaction job queue

**Rejected.** A late acknowledgement is stale, reactions are non-authoritative,
and persistence would add unnecessary delivery and cleanup complexity.

---

## 29. Risks and Mitigations

### 29.1 Risk: service-wide throttling

**Cause:** Several conversations or processes react at once.  
**Mitigation:** one operation per turn, local 2/s token bucket, no queue, bounded
retry, telemetry.

### 29.2 Risk: wrong service endpoint

**Cause:** using a single default Bot Framework URL for regional traffic.  
**Mitigation:** derive and validate `serviceUrl` from the inbound reference and
cache clients by normalized URL.

### 29.3 Risk: acknowledgement appears for a turn later rejected by application logic

**Cause:** the lifecycle hook precedes completion of the central handler.  
**Mitigation:** define the reaction as receipt/processing acknowledgement, not
an authorization or success claim. Add a post-authorization hook only through a
separate cross-platform design if stricter semantics become necessary.

### 29.4 Risk: reaction task accumulation

**Cause:** remote calls hang or task references are not removed.  
**Mitigation:** short timeout, done callback, tracked set, no queue, shutdown
cancellation, task-count observability.

### 29.5 Risk: SDK upgrade breaks client construction

**Cause:** constructor or client-composition changes.  
**Mitigation:** pinned dependency, public API only, dedicated integration tests,
upgrade gate.

### 29.6 Risk: users interpret `👀` as “currently running” forever

**Cause:** familiar cross-platform processing semantics.  
**Mitigation:** product documentation calls it acknowledgement; the text reply
is completion. Product testing should confirm whether a persistent `👀` is
understood. If not, use a single persistent acknowledgement reaction with a
clearer semantic, such as 👍, in a separately reviewed change.

### 29.7 Risk: reaction failure hides a broader identity problem

**Cause:** normal Bot Framework reply and reaction API can fail differently.  
**Mitigation:** classify sustained 401/403 separately and alert operators while
keeping user turns independent.

### 29.8 Risk: multiple Marlow processes bypass local limiter

**Cause:** process-local token bucket.  
**Mitigation:** remote 429 remains authoritative; deployment documentation
should avoid multiple active instances for one bot identity until distributed
coordination exists.

---

## 30. Open Questions

These do not block the first release unless reviewers choose otherwise:

1. Should the acknowledgement reaction remain `👀`, or would persistent `👍`
   communicate “received” more clearly in Teams?
2. Should `TEAMS_REACTIONS` be implemented as an environment override, or should
   YAML remain the only configuration surface?
3. Does the existing configuration reload path safely enable/disable reactions
   without reconnect, or should a restart be required and documented?
4. Should sustained 401/403 reaction failures mark a degraded Teams health
   state, or remain telemetry-only because normal messages may still work?
5. After production measurement, is there enough operation budget to implement
   a terminal reaction lifecycle?
6. Should a future post-authorization gateway hook provide stricter semantics
   across all platforms?

Recommended answers for initial implementation:

```text
reaction:                👀
configuration:           YAML, optional env override only if convention demands
reload:                  restart/reconnect required unless already proven safe
health:                  warning/metric first, no hard unhealthy state
terminal lifecycle:      deferred
post-authorization hook: separate design
```

---

## 31. Final Recommendation

Implement the acknowledgement-only design as a small, opt-in Teams adapter
feature.

The first release should do exactly one visible operation:

```text
when a valid Teams turn begins processing:
    best-effort add 👀 to the triggering message
```

It should not delete or replace that reaction, should not request new
permissions, and should not wait on the remote API before starting the agent.

This delivers the core product value—users can see that a slow request was
received—while keeping the failure domain, rate usage, CPU cost, and code delta
small. More elaborate terminal reactions should be considered only after live
telemetry proves that the service budget and user semantics support them.

---

## 32. References

- Microsoft Learn — Agent reactions in Microsoft Teams:  
  <https://learn.microsoft.com/en-us/microsoftteams/platform/agents-in-teams/agent-reactions>
- Microsoft Teams Python SDK API reference — reactions/conversations:  
  <https://learn.microsoft.com/en-us/python/api/microsoft-teams-api/microsoft_teams.api.reactionclient?view=msteams-sdk-python-latest>
- Microsoft Teams reactions reference:  
  <https://learn.microsoft.com/en-us/microsoftteams/platform/agents-in-teams/teams-reactions-reference>
- Microsoft Teams Python SDK repository:  
  <https://github.com/microsoft/teams.py>
- Marlow Teams adapter:  
  `plugins/platforms/teams/adapter.py`
- Marlow processing lifecycle:  
  `gateway/platforms/base.py`
- Related Marlow design:  
  `docs/microsoft-teams-gateway-full-thread-context-engineering-design.md`

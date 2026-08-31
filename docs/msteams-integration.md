# Microsoft Teams Gateway

**Status:** Implemented locally
**Scope:** Milestone 1
**Target:** Local implementation complete; live Azure tenant and public HTTPS smoke tests remain manual gates

## Summary

Add Microsoft Teams as a first-party Marlow messaging gateway through a bundled `kind: platform` plugin.

The first milestone supports authenticated Microsoft Teams conversations in:

* personal chats;
* group chats;
* standard Teams channels.

The implementation uses the Microsoft Teams SDK and Bot Framework channel transport for inbound and outbound activities. Teams-specific protocol behavior remains inside `plugins/platforms/teams/`. Marlow core remains platform-neutral except for narrowly scoped generic changes required to:

1. expose automatically generated toolsets for registered plugin platforms; and
2. provide a durable, platform-neutral inbound activity receipt primitive if an equivalent primitive does not already exist.

Inbound message processing is split into two phases:

```text
Microsoft Teams
        │
        ▼
POST /api/messages
        │
        ├─ bounded HTTP request handling
        ├─ Teams SDK authentication and parsing
        ├─ tenant and sender identity validation
        ├─ durable activity receipt claim
        ├─ bounded handoff to Marlow dispatch
        │
        └─ prompt HTTP acknowledgement
                 │
                 ▼
        asynchronous Marlow execution
                 │
                 ├─ authorization and pairing
                 ├─ session dispatch
                 ├─ agent and tools
                 └─ Teams response delivery
```

Marlow does not wait for the agent run, media download, tool execution, or final response delivery before acknowledging a normal Teams message. Microsoft bot activities are expected to complete their HTTP handling within approximately 10–15 seconds, and delayed processing may cause retries and duplicate requests.

Adaptive Card approval callbacks use a separate bounded synchronous path because Teams expects an immediate response to an `invoke` activity.

The plugin reuses Marlow’s existing:

* gateway authorization and pairing;
* session identity and dispatch;
* tool policy;
* media validation and caching;
* exact-administrator approval model;
* profile configuration;
* secret handling;
* dependency installation;
* lifecycle supervision.

## Problem and Current Behavior

Current Marlow has no registered `teams` platform. Gateway setup cannot configure Microsoft Teams, and the gateway cannot receive or send Teams activities.

An earlier Hermes-derived Teams plugin was intentionally removed during repository slimming. Its Bot Framework behavior remains useful implementation evidence, but its Hermes imports, branding, meeting-summary pipeline, and previous approval contract are incompatible with current Marlow.

Marlow already supports bundled platform plugins through:

* `register_platform(...)`;
* dynamic platform identities;
* `BasePlatformAdapter`;
* shared gateway authorization;
* shared session dispatch;
* dynamically generated platform toolsets.

However, current toolset validation rejects the generated `marlow-<platform>` name for registered plugin platforms. A connected Teams session would therefore receive no default Marlow toolset unless that generic validation defect is corrected.

A Teams gateway also introduces several production concerns that must be resolved explicitly:

* the public webhook cannot remain blocked on a long-running agent execution;
* Teams may retry activities after request timeout;
* Azure AD object IDs are meaningful only within a tenant boundary;
* inbound Teams images can require authenticated SDK retrieval;
* approval callbacks must be request-bound, user-bound, one-shot, and promptly acknowledged;
* the webhook requires explicit request-size, concurrency, and shutdown behavior;
* standard, private, and shared Teams channels do not have identical bot capabilities.

The desired outcome is that an operator can register one Teams bot, expose one public HTTPS callback, enable the bundled plugin, and use normal Marlow conversations without weakening Marlow’s existing authorization or administrator-approval boundaries.

## Key Decisions

### Teams is a bundled platform plugin

Teams ships in the Marlow repository under:

```text
plugins/platforms/teams/
```

It registers through the existing platform plugin registry and implements `BasePlatformAdapter`.

There must be no Teams-specific branch in:

* the core platform enum;
* the shared gateway dispatcher;
* shared authorization;
* the static toolset catalog;
* session routing;
* approval outcome logic.

### Milestone 1 is single-tenant

`teams.tenant_id` is required.

Every authenticated Teams activity must contain the configured tenant identity. An activity from any other tenant is ignored and fails closed before Marlow dispatch, even if the underlying Microsoft application registration is technically multi-tenant.

The canonical Teams authorization principal is:

```text
(platform="teams", tenant_id, aad_object_id)
```

A display name, UPN, email address, Teams-scoped user ID, or unverified request header must never replace the Azure AD object ID.

### Normal messages are acknowledged before agent execution

The webhook must not await:

* model inference;
* session completion;
* tool execution;
* administrator approval;
* inbound media download;
* outbound message delivery.

Normal message activities are authenticated, validated, claimed, handed off to a bounded asynchronous task, and then acknowledged.

### Duplicate suppression is durable but not a replay queue

Authenticated message activities are claimed through a small durable receipt store before asynchronous dispatch.

The receipt store prevents the same Teams activity from invoking Marlow again after:

* an HTTP retry;
* an adapter restart;
* a gateway restart.

The receipt store does not contain the message body and does not replay unfinished work. Milestone 1 deliberately chooses **at-most-once dispatch** over automatic crash recovery.

Consequently, if the process terminates after claiming an activity but before completing it, that activity may be lost. The user can resend the request as a new Teams message. A durable work queue and exactly-once tool execution are outside this milestone.

### Milestone 1 supports standard channels only

Supported surfaces are:

* personal chat;
* group chat;
* standard Teams channel.

Private and shared channel support is not claimed in Milestone 1. Microsoft documents material restrictions for agent posting and Adaptive Cards in private channels.

The example Teams application manifest must not advertise private/shared channel capabilities.

### Chat messaging does not use Microsoft Graph

Normal Teams chat transport uses:

* the Microsoft Teams SDK;
* Bot Framework activity authentication;
* Bot Framework conversation APIs.

Microsoft Graph is not used for normal inbound or outbound chat.

Meetings, transcripts, calls, recordings, OneDrive uploads, SharePoint uploads, Graph subscriptions, and Graph-based proactive messaging remain outside scope.

## Goals

* Expose Microsoft Teams in Marlow gateway setup and status when the bundled plugin is available.
* Receive authenticated Teams activities at `POST /api/messages`.
* Acknowledge normal message activities promptly without waiting for an agent run.
* Process authorized personal-chat messages.
* Process authorized group-chat and standard-channel messages only when the bot is explicitly mentioned.
* Remove only the bot’s own mention markup while preserving other user mentions.
* Enforce the configured tenant before Marlow dispatch.
* Use the sender’s Azure AD object ID as the authorization identity.
* Preserve stable Teams chat and thread identities.
* Prevent one Teams conversation or channel thread from sharing a Marlow session with another.
* Suppress duplicate Teams activity IDs across gateway restarts for a bounded retention period.
* Support inbound text and inline images.
* Support outbound text, typing indicators, and bounded outbound images.
* Support request-correlated Adaptive Card administrator approvals.
* Give Teams sessions the same Marlow core tools as other registered messaging platforms.
* Respect existing per-platform enabled and disabled tool configuration.
* Isolate Teams startup, ingress, dispatch, and delivery failures from other configured gateways.
* Provide sufficient status, health, logging, and metrics for production operation.
* Preserve default-deny authorization and exact-administrator approval behavior.

## Non-goals

* Teams meetings, calls, recordings, transcripts, or meeting-summary pipelines.
* Microsoft Graph subscriptions or meeting event ingestion.
* Resource-specific consent for receiving all unmentioned channel messages.
* Private or shared Teams channel support.
* Sovereign-cloud validation for GCC High, DoD, or Teams operated by 21Vianet.
* Scheduled, cron, home-channel, or arbitrary proactive delivery.
* Starting a new Teams conversation without a current inbound conversation.
* General file, document, audio, or video attachments.
* OneDrive or SharePoint file upload.
* Message editing or deletion.
* Token-by-token streaming into an existing Teams message.
* Durable replay of incomplete agent runs.
* Exactly-once execution of arbitrary external tools.
* A general-purpose queue, separate worker service, or message broker.
* A built-in public tunnel, reverse proxy, DNS service, certificate manager, or Azure registration service.
* Compatibility with removed Hermes configuration, commands, state, branding, or meeting behavior.
* Silent fallback to a legacy Bot Framework SDK, prerelease SDK, or custom unauthenticated webhook protocol.

## Constraints and Preserved Invariants

* Teams-specific setup and protocol behavior stays under `plugins/platforms/teams/`.
* Inbound activities enter the shared Marlow gateway only after Microsoft SDK authentication and Teams-specific structural validation.
* Shared gateway authorization retains its current order and default-deny behavior.
* Pairing, global allowlists, platform allowlists, explicit allow-all behavior, and exact-administrator routing remain shared gateway responsibilities.
* The plugin must not implement a second authorization framework.
* Administrator approval remains bound to platform, tenant, administrator user, chat, optional thread, request, and one-time action token.
* Unsupported, malformed, expired, duplicate, unauthorized, or mismatched approval actions fail closed.
* `TEAMS_CLIENT_SECRET` remains in the profile’s secret environment storage.
* Non-secret Teams settings remain in `config.yaml`.
* Credentials, bearer tokens, raw authorization headers, approval tokens, and message bodies are never written to logs.
* Connecting with a Teams credential acquires the existing scoped credential lock.
* Disconnect and failed startup release the lock.
* The Microsoft Teams SDK is optional, exactly pinned, represented in `uv.lock`, and installed through the existing allowlisted lazy-dependency path.
* The listener binds to loopback by default.
* A non-loopback listener requires explicit operator configuration.
* Public HTTPS termination remains outside Marlow.
* The local listener must never be described as a TLS endpoint unless TLS is actually configured by an external ingress.
* Teams failure must not terminate another configured gateway platform.

## Proposed Architecture

### Components

```text
┌─────────────────────────────────────────────────────────────┐
│ Microsoft Teams                                            │
└───────────────────────────────┬─────────────────────────────┘
                                │ HTTPS Bot Framework activity
                                ▼
┌─────────────────────────────────────────────────────────────┐
│ Operator-managed HTTPS ingress                             │
│                                                             │
│ - DNS and certificate                                      │
│ - optional external rate limiting                          │
│ - forwards Authorization header and body unchanged         │
└───────────────────────────────┬─────────────────────────────┘
                                │ HTTP
                                ▼
┌─────────────────────────────────────────────────────────────┐
│ Teams platform plugin                                      │
│                                                             │
│ - aiohttp listener                                         │
│ - Teams SDK HttpServer / adapter bridge                    │
│ - JWT authentication and activity parsing                  │
│ - tenant and identity validation                           │
│ - mention normalization                                    │
│ - durable activity receipt claim                           │
│ - bounded dispatch handoff                                 │
│ - conversation reference capture                           │
│ - Teams sends and approval cards                           │
└───────────────────────────────┬─────────────────────────────┘
                                │ MessageEvent / SessionSource
                                ▼
┌─────────────────────────────────────────────────────────────┐
│ Existing Marlow gateway                                    │
│                                                             │
│ - authorization and pairing                                │
│ - session dispatch                                         │
│ - agent execution                                          │
│ - tools and policies                                       │
│ - exact-administrator approval outcome                     │
│ - media cache                                              │
└─────────────────────────────────────────────────────────────┘
```

The Teams SDK separates Teams protocol handling from web-framework integration: its HTTP server handles JWT authentication, activity parsing, and handler routing, while the HTTP adapter translates framework-specific requests and responses.

### Bundled Teams Platform Plugin

The plugin registers:

```text
platform name: teams
plugin kind: platform
adapter: TeamsPlatformAdapter
```

On connection, the adapter performs these steps in order:

1. validate configuration;
2. confirm runtime and optional dependency compatibility;
3. acquire the scoped Teams credential lock;
4. initialize the durable activity receipt store;
5. construct the Teams SDK application and authentication objects;
6. create the `aiohttp` listener;
7. register HTTP routes;
8. bind the configured host and port;
9. start the bounded dispatch supervisor;
10. report `connected`.

The listener registers:

```text
POST /api/messages
GET  /healthz
```

`connected` must not be reported before:

* the credential lock is held;
* the SDK is initialized;
* the receipt store is usable;
* the listener is bound;
* the routes are registered;
* the dispatch supervisor accepts work.

### HTTP Ingress Contract

#### Request limits

`POST /api/messages` must enforce:

* maximum request body size: `1 MiB`;
* JSON content type, allowing a normal charset parameter;
* bounded request read time;
* bounded SDK authentication and parsing time;
* no request-body or authorization-header logging.

An oversized request returns `413`.

Malformed JSON or an unsupported request representation returns `400`.

SDK authentication failure uses the SDK-defined unauthorized response and never reaches Marlow dispatch.

Marlow must not trust `X-Forwarded-*` headers for authentication or principal identity.

#### Normal message response behavior

For an authenticated and structurally valid normal message:

* accepted activity: `200`;
* known duplicate: `200`, without a second dispatch;
* self-authored activity: `200`, ignored;
* empty unsupported activity: `200`, ignored;
* tenant mismatch: `200`, ignored and security-logged;
* unsupported activity type: `200`, ignored;
* no available dispatch capacity: `503`, without creating a receipt.

Under normal load, the adapter should return the accepted-message response within five seconds. It must never intentionally wait for agent completion.

#### Invoke response behavior

Adaptive Card actions arrive through an `invoke`-style activity and use a bounded synchronous handler.

The handler may only:

* authenticate and parse the activity;
* validate tenant and sender;
* validate the callback shape;
* resolve a live pending approval atomically;
* produce the required invoke response;
* schedule any nonessential card update.

It must not execute the approved tool inside the HTTP callback.

Malformed, expired, unauthorized, duplicate, or mismatched approval callbacks return an appropriate non-success card result while leaving the protected operation unapproved.

### Bounded Asynchronous Handoff

Normal Teams messages are handed to a supervised in-process asynchronous task.

This is not a separate worker service and is not a durable work queue.

The supervisor must provide:

* a bounded number of active and pending tasks;
* nonblocking capacity reservation;
* per-task exception capture;
* graceful shutdown;
* cancellation after the existing gateway shutdown grace period;
* no unbounded `asyncio.create_task()` accumulation.

The handoff sequence is:

```text
1. authenticate and validate activity
2. reserve dispatch capacity
3. claim durable activity receipt
4. construct immutable dispatch input
5. commit task to the supervisor
6. return HTTP 200
```

If capacity reservation fails, the adapter returns `503` and creates no receipt.

If the durable receipt already exists, the reservation is released and the adapter returns `200` without dispatch.

If task commitment fails after a new receipt was created, the adapter must make a best-effort removal of that receipt, release capacity, return `503`, and log whether receipt rollback succeeded.

The task owns the captured conversation reference and normalized event after the HTTP request ends.

### Durable Activity Receipts

The activity receipt store exists only to prevent duplicate Marlow dispatch.

A canonical activity key is derived from the structured tuple:

```text
(
  platform = "teams",
  client_id,
  tenant_id,
  conversation_id,
  activity_id
)
```

The tuple must be encoded unambiguously. Plain string concatenation with separators is not sufficient unless each element is length-prefixed or otherwise escaped.

The receipt stores:

```text
version
canonical_key_hash
canonical_payload_hash
claimed_at
expires_at
```

It must not store:

* message text;
* attachment URLs;
* display names;
* credentials;
* bearer tokens;
* raw activity bodies.

The payload hash is calculated from a canonical representation of the authenticated activity. It is used only to detect an unexpected activity-ID collision.

Receipt behavior:

* missing key: atomically create the receipt and accept;
* existing key with the same payload hash: treat as duplicate;
* existing key with a different payload hash: fail closed, do not dispatch, and emit a security event;
* unavailable or corrupt receipt store: Teams startup fails;
* no fallback from durable receipts to in-memory-only receipts.

The claim operation must be atomic across processes sharing the same profile state directory.

Receipts expire after seven days.

Expired receipt cleanup occurs:

* during plugin startup;
* opportunistically after successful claims;
* in bounded batches.

Receipt cleanup failure degrades status and emits an operational warning but does not delete unexpired receipts.

No database migration is required if the receipt store uses Marlow’s existing profile-scoped generic state primitive or an atomic plugin-state representation.

### Inbound Activity Filtering

The adapter accepts normal message activities only when:

* SDK authentication succeeded;
* `channelId` represents Microsoft Teams;
* the verified tenant matches `teams.tenant_id`;
* the sender has a nonempty Azure AD object ID;
* the sender is not the bot itself;
* the conversation type is supported;
* the message contains nonempty text or at least one supported inline image.

Supported conversation types are:

```text
personal
groupChat
channel
```

For `groupChat` and `channel`, the bot must be explicitly identified by a structured mention entity.

The adapter must not trigger merely because:

* the bot’s display name appears as plain text;
* a team or channel was mentioned;
* another user was mentioned;
* an HTML fragment resembles mention markup.

Teams normally delivers group and channel messages to an agent only when directly mentioned unless additional resource-specific permissions are granted. Milestone 1 does not request those permissions.

### Mention Normalization

Mention handling uses structured activity entities.

The adapter:

1. confirms that at least one mention targets the configured bot identity;
2. removes only those exact bot mention entities;
3. preserves mentions of other users and tags;
4. trims leading and trailing whitespace;
5. preserves remaining user text.

An image-only message with a valid bot mention remains dispatchable after the mention text is removed.

A group or channel message that becomes empty and contains no accepted image is acknowledged but does not invoke Marlow.

### Tenant and User Identity

The verified activity tenant must exactly equal the configured `teams.tenant_id`.

The adapter must not use an unverified tenant value from an arbitrary header as its authorization boundary. The parsed authenticated activity is authoritative.

The sender must provide a nonempty Azure AD object ID.

The canonical principal passed to shared gateway authorization is the structured identity:

```text
platform: teams
tenant_id: <configured tenant>
user_id: <aad object id>
```

Where the existing authorization API requires a single string, it must use an unambiguous canonical encoding such as:

```text
<tenant-id>/<aad-object-id>
```

Both values must be normalized to canonical lowercase UUID representation before comparison.

`allowed_users` contains Azure AD object IDs within the configured tenant.

Display names may be retained as informational session metadata but must never be used for:

* authorization;
* administrator matching;
* deduplication;
* approval validation;
* session identity;
* audit identity.

If the Azure AD object ID is absent, the activity fails closed. There is no fallback to a display name, email address, UPN, or Teams-scoped member ID.

### Conversation and Session Identity

Marlow session identity must include:

* platform;
* configured bot client ID;
* tenant ID;
* chat identity;
* optional thread identity.

The structured session source is:

```text
(
  platform = "teams",
  account = client_id,
  tenant = tenant_id,
  chat = chat_id,
  thread = thread_id | null
)
```

#### Personal chat

```text
chat_id   = activity.conversation.id
thread_id = null
```

#### Group chat

```text
chat_id   = activity.conversation.id
thread_id = null
```

#### Standard channel

```text
chat_id = (
  activity.channelData.team.id,
  activity.channelData.channel.id
)

thread_id = activity.conversation.id
```

For channel conversations, Teams carries the channel thread context in the conversation identifier, and sends through that conversation context remain in the same thread.

The full authenticated `conversation.id` must be retained for outbound routing even when a separate Marlow `chat_id` and `thread_id` are generated.

If the required team, channel, conversation, or tenant fields are absent for a channel activity, the activity is rejected rather than merged into a fallback session.

The adapter does not create a Teams-specific session store. It supplies stable source identifiers to the existing Marlow session layer.

### Authorization and Pairing

The plugin does not implement independent user authorization.

After Teams authentication, tenant validation, receipt claim, and asynchronous handoff, the existing gateway applies its normal authorization order, including:

1. exact administrator routing;
2. pairing state;
3. platform-specific allowlist;
4. global allowlist;
5. explicit allow-all configuration;
6. default deny.

A denied activity must not invoke the agent or tools.

Any pairing response is delivered through the captured Teams conversation reference.

`allow_all_users` defaults to `false`.

An empty `allowed_users` list with `allow_all_users: false` preserves default-deny behavior according to the existing gateway contract.

### Inbound Images

Milestone 1 supports inline image content associated with a Teams message.

It does not support arbitrary files.

Teams-specific acquisition remains the plugin’s responsibility because inline image content URLs may require authenticated retrieval. The Teams SDK provides authenticated access to attachment content from the activity context.

The processing boundary is:

```text
Teams attachment descriptor
        │
        ▼
Teams SDK authenticated fetch
        │
        ▼
bounded byte stream
        │
        ▼
existing Marlow media validation
        │
        ▼
existing Marlow media cache
```

The adapter must:

* fetch media only from SDK-recognized attachment descriptors;
* never treat arbitrary URLs in user text as attachments;
* avoid exposing or logging attachment access tokens;
* apply a bounded connection and total download timeout;
* stop reading after the existing Marlow media byte limit;
* validate the declared and detected MIME type;
* accept only image media types supported by the existing media subsystem;
* reject redirect or destination behavior that violates the existing media-fetch policy;
* cache only successfully validated image bytes.

Media fetch occurs after the HTTP activity has been acknowledged.

If text is present and one image fails, Marlow may continue with the text and successfully cached images. The failure is recorded as structured attachment metadata, not silently represented as a valid image.

If a message contains only images and none can be validated, the agent is not invoked. The adapter sends a bounded user-facing failure message when delivery remains possible.

Unsupported attachment kinds are excluded from model context.

### Outbound Conversation Reference

Before acknowledging a normal message, the adapter captures an immutable conversation delivery reference containing the SDK-required routing information, including:

* authenticated service URL;
* conversation ID;
* bot identity;
* tenant identity;
* reply or thread context required by the SDK.

The adapter must not retain a raw inbound bearer token.

The current inbound reference remains owned by the asynchronous dispatch task until its response completes.

Because proactive delivery is outside scope:

* the reference is not stored as a durable address book;
* no message is initiated after the current inbound task has completed;
* an old reference is not reused for cron or unrelated future events.

### Outbound Text

Agent output is converted into ordered Teams message activities.

The Teams platform has an approximate 100 KB message limit and recommends keeping the message body within 80 KB; the limit includes message text and related activity content encoded as UTF-16.

Marlow therefore uses a conservative serialized activity budget:

```text
maximum serialized non-image activity payload: 64 KiB in UTF-16
```

The implementation must measure the actual serialized activity representation rather than relying only on Python character count.

Text chunking prefers boundaries in this order:

1. paragraph;
2. newline;
3. whitespace;
4. Unicode code-point-safe hard split.

Chunking must not split:

* a Unicode surrogate pair;
* a multibyte sequence;
* the activity envelope into an invalid object.

Chunks are sent sequentially.

A later chunk is not sent before the previous chunk succeeds.

Outbound delivery performs bounded transient retries:

* honor `Retry-After` for `429`;
* retry retryable `5xx` failures;
* maximum three attempts per activity;
* no retry for nonretryable `4xx`;
* no unbounded retry loop.

If a later chunk permanently fails, delivery stops and the failure is reported in Teams gateway status and structured logs.

### Outbound Typing Indicators

After shared authorization permits dispatch and before the agent run begins, the adapter sends a best-effort typing indicator.

For long-running requests, the adapter may refresh the indicator at a bounded platform-safe interval while execution remains active.

Typing failures:

* do not fail the agent run;
* do not trigger retries beyond the bounded transient policy;
* are recorded at debug or metric level without message content.

The typing task stops when:

* the response begins;
* the agent run fails;
* the request is cancelled;
* the adapter disconnects.

### Outbound Images

Milestone 1 supports bounded outbound PNG, JPEG, and GIF images already present in Marlow’s validated media subsystem.

Each image is sent as a separate Teams activity to isolate image failures from text delivery.

The preferred Milestone 1 transport is a standard image attachment whose `contentUrl` contains a self-contained data URL generated from validated bytes.

The plugin must not:

* upload content to Graph, OneDrive, or SharePoint;
* create a new public media server;
* expose a local filesystem path;
* forward an unvalidated remote URL from model output.

The outbound image must remain within:

* the existing Marlow outbound media limit;
* the SDK’s accepted activity representation;
* the Teams channel’s actual delivery limit.

A `413` or channel rejection is nonretryable.

If the selected SDK version or live commercial Teams validation cannot deliver the required image representation, outbound image implementation is `BLOCKED`. The implementation must not silently substitute Graph permissions or add an external media-hosting system.

### Adaptive Card Administrator Approvals

The Teams adapter renders the current Marlow exact-administrator approval request as an Adaptive Card.

The card uses a Teams-supported Adaptive Card schema version no later than `1.6`.

Each action contains only:

```json
{
  "kind": "marlow.approval.v1",
  "request_id": "<opaque request id>",
  "decision": "approve | deny",
  "nonce": "<random one-time token>"
}
```

The live pending-approval record is created by the existing approval subsystem and contains:

```text
platform
tenant_id
administrator_aad_object_id
chat_id
optional_thread_id
request_id
nonce_hash
expires_at
resolution_state
```

A callback is valid only when all of the following match:

* platform is `teams`;
* tenant ID;
* authenticated sender Azure AD object ID;
* chat identity;
* thread identity, when present;
* request ID;
* one-time nonce;
* unexpired pending state;
* expected action type.

The nonce must contain at least 128 bits of cryptographically secure randomness.

The callback payload is not trusted to define its own authorization route. Route and administrator identity are read from the live pending request and compared with the authenticated callback activity.

Resolution is atomic and one-shot.

For a valid action:

1. atomically transition the pending request;
2. return the bounded Teams invoke response;
3. resume the existing Marlow approval waiter asynchronously;
4. update or replace the card on a best-effort basis.

Duplicate actions return the already-resolved state but do not resolve the operation again.

The following all fail closed:

* delivery failure;
* timeout;
* adapter shutdown;
* process restart;
* malformed callback;
* missing nonce;
* stale nonce;
* wrong user;
* wrong tenant;
* wrong chat;
* wrong thread;
* unknown request;
* duplicate request resolution.

There is no fallback that allows the original requester to approve an operation merely because administrator card delivery failed.

### Health and Status

The plugin registers:

```text
GET /healthz
```

The endpoint returns:

* `200` only when the plugin has initialized successfully and the listener is ready to accept Teams activities;
* `503` while starting, stopping, or failed.

It contains no:

* client ID;
* tenant ID;
* allowed-user list;
* credentials;
* environment paths;
* activity data.

The endpoint is suitable for reverse-proxy or container readiness checks. Operators may choose not to expose it through the public ingress.

The existing gateway status surface remains authoritative for detailed adapter state.

Teams status should distinguish:

```text
disabled
starting
connected
degraded
failed
stopping
```

A degraded status may include bounded reasons such as:

* receipt cleanup failure;
* recent outbound delivery failures;
* repeated activity collisions;
* media acquisition failures;
* approval delivery failures.

### Observability

Structured logs and metrics must use a request correlation value derived from the activity ID without logging message content.

Required counters include:

```text
teams_http_requests_total{result}
teams_activities_total{type,result}
teams_duplicate_activities_total
teams_dispatch_rejected_total{reason}
teams_agent_dispatch_total{result}
teams_delivery_total{type,result}
teams_media_total{direction,result}
teams_approvals_total{decision,result}
teams_receipt_operations_total{operation,result}
```

Required latency measurements include:

```text
teams_http_ack_duration
teams_agent_duration
teams_delivery_duration
teams_media_fetch_duration
teams_approval_callback_duration
```

Logs must not contain:

* raw authorization headers;
* client secrets;
* access tokens;
* approval nonces;
* message bodies by default;
* raw image bytes;
* attachment authorization URLs.

Azure AD object IDs should be redacted or irreversibly hashed in ordinary operational logs. Full IDs may appear only in an existing protected audit sink whose access model already permits authorization identities.

## Configuration and Setup

Add this top-level configuration:

```yaml
teams:
  enabled: false
  client_id: ""
  tenant_id: ""
  host: "127.0.0.1"
  port: 3978
  allowed_users: []
  allow_all_users: false
```

The required secret is:

```text
TEAMS_CLIENT_SECRET
```

### Validation

When `teams.enabled` is true:

* `client_id` is required and must be a valid UUID;
* `tenant_id` is required and must be a valid UUID;
* `TEAMS_CLIENT_SECRET` is required and nonempty;
* `port` must be within the valid TCP port range;
* every `allowed_users` entry must be a valid Azure AD object ID;
* duplicate user IDs are normalized and removed;
* `allow_all_users` must be explicitly boolean;
* the configured host must parse as a valid bind host;
* a non-loopback host produces a security warning during setup and startup.

The plugin remains disabled unless `enabled` is explicitly true.

### Source of Truth

`config.yaml` is the source of truth for non-secret settings.

The profile’s secret environment file is the source of truth for `TEAMS_CLIENT_SECRET`.

A plugin YAML bridge may mirror values into process-local environment variables only when required by existing registry hooks. Mirrored variables are implementation details and must not become competing configuration sources.

### Gateway Setup

Gateway setup must:

* make Microsoft Teams discoverable as a bundled platform;
* collect the non-secret identifiers;
* write the secret through the existing secret writer;
* never echo the full secret after entry;
* explain that Milestone 1 is single-tenant;
* explain how to find Azure AD object IDs for allowed users;
* display the callback form:

```text
https://<operator-host>/api/messages
```

* explain that the local listener is HTTP;
* explain that a public HTTPS ingress is required;
* explain that Marlow does not provision DNS, TLS, tunnels, or Azure resources;
* explain that personal chat, group chat, and standard channels are supported;
* clearly identify private/shared channels, meetings, files, Graph, and proactive delivery as unsupported.

### Operator-Owned Teams Registration

Operator documentation must cover:

1. creating or selecting a Microsoft Entra application;
2. configuring the bot application identity;
3. creating a client secret;
4. enabling the Microsoft Teams channel;
5. setting the public messaging endpoint;
6. creating a Teams application manifest;
7. enabling `personal`, `groupchat`, and `team` scopes;
8. omitting resource-specific consent for all-message access;
9. omitting private/shared channel feature declarations;
10. installing or publishing the Teams application according to tenant policy;
11. configuring the Marlow allowlist;
12. validating the HTTPS forwarding path.

The public ingress must preserve:

* method;
* path;
* request body;
* content type;
* `Authorization` header.

The ingress should forward only the required paths rather than exposing unrelated Marlow services.

## SDK and Dependency Contract

Use the official Python package:

```text
microsoft-teams-apps==2.0.16
```

As of August 19, 2026, `2.0.16` is the latest stable release and requires Python `>=3.11,<4.0`. Prerelease `2.1.0a*` packages must not be selected.

The implementation must:

* add the exact pin to the optional backend dependency definition;
* regenerate `uv.lock`;
* add it to the existing lazy-dependency allowlist;
* install only the base package;
* not install the optional Graph extra;
* reject unsupported Python runtimes before attempting installation;
* use the same dependency acquisition and verification path as other bundled platform plugins.

If Marlow’s supported runtime includes Python below 3.11, implementation must use an environment marker and report Teams as unsupported on that runtime, or raise the project runtime baseline through a separate approved change.

It must not:

* silently install a prerelease;
* use a version range;
* substitute the archived legacy Bot Framework SDK;
* vendor a copied SDK;
* bypass the repository dependency policy.

A compatibility test must confirm:

* SDK import;
* credential construction;
* HTTP server integration;
* authenticated activity handler registration;
* message send;
* typing send;
* Adaptive Card invoke handling;
* authenticated inline-image retrieval.

Failure of any required capability blocks implementation.

## Platform Toolset Integration

Marlow already generates:

```text
marlow-<platform>
```

toolsets for registered plugin platforms.

Correct generic validation so that `marlow-<name>` is accepted only when:

* `<name>` is currently registered as a platform;
* registration completed through the normal plugin registry;
* the toolset name is the exact generated name.

The change must not accept arbitrary unknown `marlow-*` strings.

Teams then receives the normal generated platform toolset and the existing enabled/disabled overlays.

Do not add:

```text
marlow-teams
```

to a static catalog.

Do not add Teams-specific tool-policy behavior.

## Lifecycle and Failure Isolation

### Startup

Startup failures include:

* missing or invalid configuration;
* unsupported Python runtime;
* missing dependency;
* dependency installation failure;
* invalid SDK construction;
* credential-lock conflict;
* unusable receipt store;
* listener bind failure;
* route registration failure;
* dispatch-supervisor startup failure.

A failed Teams startup:

* reports a bounded fatal Teams error;
* closes partially initialized SDK objects;
* closes the listener if created;
* stops the task supervisor;
* releases the credential lock;
* leaves other configured gateway adapters running.

### Disconnect

Disconnect performs:

1. mark Teams as stopping;
2. stop accepting new ingress;
3. stop health readiness;
4. wait for in-flight tasks using the existing shutdown grace period;
5. cancel remaining tasks after the grace period;
6. fail any live Teams approval waiters closed;
7. close SDK and HTTP resources;
8. release the credential lock;
9. report disconnected.

Disconnect must be idempotent.

### Unexpected Listener Failure

If the listener exits unexpectedly:

* Teams status becomes failed;
* new Teams work is not accepted;
* in-flight tasks may finish if their outbound SDK client remains usable;
* other gateway platforms continue;
* no unbounded automatic restart loop is introduced.

Any restart policy remains owned by the existing process supervisor or deployment system.

## Ownership and Boundaries

### Teams Plugin Owns

* Microsoft Teams SDK integration;
* webhook lifecycle;
* HTTP request limits;
* Teams request authentication integration;
* tenant validation;
* activity structural validation;
* mention detection and removal;
* Teams identity normalization;
* conversation and thread extraction;
* activity receipt keys and claims;
* bounded async handoff;
* Teams-authenticated inline-image acquisition;
* conversation reference capture;
* outbound Teams activities;
* text chunking;
* typing indicators;
* Adaptive Card rendering;
* callback parsing;
* Teams setup metadata;
* Teams health endpoint;
* Teams-specific metrics and status.

### Existing Gateway Owns

* authorization order;
* pairing;
* global and platform allowlists;
* default-deny behavior;
* sessions;
* per-session serialization;
* agent dispatch;
* tool execution;
* tool policy;
* exact-administrator selection;
* approval outcome;
* cancellation and shutdown policy.

### Existing Media Subsystem Owns

* accepted image MIME types;
* byte-size limits;
* image validation;
* cache storage;
* cache retention;
* model-facing media representation.

### Operator Owns

* Microsoft Entra application;
* bot registration;
* Teams channel enablement;
* tenant policy;
* application manifest;
* application publishing and approval;
* public HTTPS ingress;
* DNS;
* TLS certificates;
* firewall policy;
* ingress-level rate limiting;
* callback forwarding;
* secret rotation.

## Persistent and Process-Local State

### Persistent State

The only new persistent state is the bounded activity receipt store.

It contains hashes and timestamps only.

It contains no user content or credentials.

Receipt deletion is safe after expiry because it does not affect sessions or conversation history.

### Process-Local State

The plugin may keep:

* active dispatch reservations;
* in-flight asynchronous tasks;
* current conversation delivery references;
* typing refresh tasks;
* pending Teams approval callbacks;
* bounded SDK clients;
* receipt cleanup bookkeeping.

Process-local state is discarded on restart.

Pending approval requests fail closed on restart.

Conversation references are not restored for proactive use.

## Security Impact

This change adds:

* a publicly forwarded HTTP interface;
* a Microsoft application credential;
* authenticated attachment retrieval;
* interactive administrator callbacks;
* limited profile-scoped receipt state.

Security controls include:

* SDK authentication before activity dispatch;
* strict configured-tenant enforcement;
* Azure AD object ID authorization;
* default-deny gateway authorization;
* bounded request size and duration;
* bounded dispatch concurrency;
* durable duplicate suppression;
* exact-route administrator approvals;
* random one-time approval tokens;
* no secret or bearer-token logging;
* existing media validation;
* loopback listener default;
* operator-owned HTTPS termination;
* no Graph permission expansion;
* no all-channel-message resource-specific consent;
* no arbitrary URL download from message text.

The local listener must not be exposed directly to an untrusted network without an operator-managed security boundary.

The plugin must not infer trust from:

* display names;
* email addresses;
* UPN values;
* forwarded headers;
* message text;
* channel names;
* team names.

### Activity Receipt Trade-off

Durable receipts provide at-most-once dispatch during the seven-day retention window.

They do not provide exactly-once end-to-end execution.

A process crash after receipt claim may cause the message to be lost.

This safety-over-availability choice prevents automatic replay of a request whose external tool side effects may already have started.

A future durable execution queue would require a separate design covering:

* replay state;
* tool idempotency;
* response persistence;
* crash recovery;
* dead-letter handling.

## Compatibility, Migration, Rollout, and Recovery

Teams is disabled by default.

Existing gateway behavior remains unchanged until `teams.enabled` is true.

No existing session migration is required.

The activity receipt store is created lazily when Teams first starts.

### Rollout

Recommended rollout:

1. enable Teams in a nonproduction profile;
2. validate one personal chat;
3. validate one group chat;
4. validate one standard channel thread;
5. validate authorization denial;
6. validate one inline image;
7. validate one long response;
8. validate one administrator approval;
9. validate duplicate suppression across restart;
10. enable the production bot for a restricted allowlist;
11. expand access only after metrics and errors are stable.

### Rollback

Operational rollback is:

```yaml
teams:
  enabled: false
```

followed by gateway restart.

Receipt files may remain until expiry or be removed during explicit cleanup.

Removing the Teams configuration and `TEAMS_CLIENT_SECRET` is optional post-rollback cleanup.

No session rollback is required.

### Secret Rotation

Secret rotation is operator-driven:

1. create the replacement secret in Microsoft Entra;
2. update the profile secret through the existing secret writer;
3. restart Teams gateway;
4. complete a live personal-chat smoke test;
5. revoke the previous secret.

The secret must never be hot-reloaded through an unauthenticated endpoint.

## Alternatives Considered

### Restore the Historical Hermes Plugin Unchanged

Rejected.

It would reintroduce removed imports, branding, meeting behavior, and an obsolete approval contract.

A Marlow-specific port is smaller and preserves current boundaries.

### Add Teams as a Built-in Core Adapter

Rejected.

The platform registry already provides the correct setup, status, authorization, identity, and dispatch boundary.

A built-in adapter would add unnecessary Teams branches to shared code.

### Ship Teams Only as a Separately Installed Plugin

Rejected for this feature.

First-party Teams support should be discoverable through standard gateway setup and released with Marlow.

Bundling remains optional at runtime because the SDK dependency is lazy and Teams is disabled by default.

### Use Microsoft Graph for Normal Chat

Rejected.

Graph would add unnecessary permissions, token flows, subscription behavior, and data-access scope.

Bot Framework activity transport is the normal Teams bot messaging path.

### Await the Agent Run Inside the Webhook

Rejected.

Agent runs may exceed Teams’ activity-processing timeout and cause retries, duplicate requests, and user-visible gateway errors.

### Use Only Process-Local Duplicate Suppression

Rejected.

A restart would erase the deduplication window and could cause a repeated Teams activity to invoke side-effecting Marlow tools again.

### Add a Durable Work Queue

Rejected for Milestone 1.

It would require replay policy, worker lifecycle, completion state, dead-letter handling, response persistence, and tool idempotency.

The activity receipt store addresses duplicate safety without becoming a replay system.

### Use Display Name or Email for Authorization

Rejected.

Those values are mutable, potentially ambiguous, and not sufficient as a tenant-scoped security principal.

### Add a Teams-Only Static Toolset

Rejected.

It would hide a generic registered-platform validation defect and duplicate existing generated toolset behavior.

### Remove HTTP Health Because Gateway Status Exists

Rejected.

Gateway status is useful to operators but cannot replace an HTTP readiness signal for reverse proxies, container probes, and ingress health checks.

## Complexity Introduced

* **Components:** one bundled Teams platform plugin.
* **Generic core changes:** registered-platform toolset validation and, only if absent, a generic durable inbound receipt primitive.
* **Persistent state:** bounded activity receipt hashes and timestamps.
* **Schema migration:** none when using profile-scoped state.
* **Dependencies:** one exact-pinned optional Microsoft Teams SDK.
* **Configuration:** one `teams` block and one secret.
* **HTTP routes:** `/api/messages` and `/healthz`.
* **Infrastructure:** operator-managed Teams registration and public HTTPS ingress.
* **Credentials:** one Microsoft application client secret.
* **Workers:** no separate worker or service.
* **Queues:** no durable execution queue.
* **Graph permissions:** none.
* **New public media service:** none.

## Acceptance Criteria

### Discovery and Configuration

* Gateway setup discovers Microsoft Teams through the bundled plugin registry.
* Discovery does not require a Teams enum or shared dispatcher branch.
* Teams remains disabled by default.
* Invalid client ID, tenant ID, port, allowed user ID, or missing secret produces a clear configuration error.
* A non-loopback host produces a visible security warning.
* Setup writes secrets only through the existing secret writer.
* Status reports unsupported runtime when Python is below 3.11.

### Lifecycle

* Valid startup acquires the scoped credential lock.
* The adapter reports connected only after the receipt store, SDK, listener, routes, and supervisor are ready.
* Disconnect releases the credential lock.
* Failed startup releases every acquired resource.
* Teams startup failure does not stop another configured platform.
* Unexpected listener failure marks Teams failed without terminating another platform.
* `/healthz` returns `200` only while ready.

### HTTP Boundary

* Oversized request bodies receive `413`.
* Malformed request bodies receive `400`.
* Invalid authentication does not reach Marlow.
* A normal accepted message is acknowledged without awaiting agent completion.
* The accepted-message path completes within the defined bounded deadline in integration tests.
* Dispatch saturation returns `503` and creates no receipt.
* Message body, bearer token, and secret values do not appear in logs.

### Identity and Authorization

* An authenticated activity from the configured tenant can proceed.
* An authenticated activity from another tenant does not invoke Marlow.
* Missing Azure AD object ID fails closed.
* Display name changes do not change authorization identity.
* Personal, group, and channel identities remain separate.
* Different tenants cannot share a Marlow principal or session identity.
* Existing pairing and authorization order remains unchanged.
* Unauthorized activities do not invoke the agent or tools.

### Message Routing

* An authorized personal-chat message reaches exactly one Marlow session.
* An authorized group-chat message reaches Marlow only with a structured bot mention.
* An authorized standard-channel message reaches Marlow only with a structured bot mention.
* The bot’s mention is absent from model input.
* Mentions of other users remain in model input.
* A plain-text occurrence of the bot display name does not count as a mention.
* Self-authored and empty unsupported activities do not invoke the agent.
* Distinct channel threads do not share a Marlow session.
* Replies are delivered to the same Teams conversation and thread.

### Duplicate Safety

* Repeated delivery of the same activity in one process invokes Marlow once.
* Repeated delivery after gateway restart invokes Marlow once within the receipt retention period.
* A matching duplicate receives `200`.
* An activity-ID collision with a different payload fails closed.
* The receipt store contains no message content.
* Receipt-store unavailability prevents Teams startup.
* Expired receipts are cleaned in bounded batches.

### Text Delivery

* Teams can send a typing indicator.
* Text exceeding one activity is delivered in ordered chunks.
* Every serialized non-image activity stays within the configured safety budget.
* Unicode text is not corrupted at chunk boundaries.
* A permanent failure stops later chunks and reports a delivery error.
* `429` and retryable `5xx` responses use bounded retry behavior.

### Media

* A supported inline image is retrieved through an authenticated Teams SDK path.
* The image is validated through the existing media subsystem.
* An oversized or invalid image is not injected into model context.
* Arbitrary URLs in user text are not downloaded as Teams attachments.
* A validated outbound image is delivered in a live commercial Teams smoke test.
* General files, audio, and video remain unsupported.
* Outbound image failure does not expose local paths or raw bytes.

### Administrator Approval

* An exact-administrator approval can be delivered as an Adaptive Card.
* The callback is bound to tenant, administrator user, chat, thread, request, and nonce.
* Only the bound Azure AD user can approve or deny.
* The valid callback resolves exactly once.
* Duplicate callback does not resolve again.
* Wrong user, tenant, chat, thread, request, or nonce fails closed.
* Expired approval fails closed.
* Adapter shutdown and restart fail pending approval closed.
* Tool execution does not occur inside the invoke HTTP handler.
* Approval delivery failure never falls back to requester approval.

### Tool Exposure

* A Teams session receives the normal generated Marlow platform toolset.
* Platform-specific enabled and disabled tool overlays work.
* Unknown `marlow-*` toolset names remain invalid.
* No static `marlow-teams` catalog entry is added.

### Rollback

* Disabling Teams restores prior gateway behavior.
* No session or schema rollback is required.
* Remaining expired receipt state can be safely removed.

## Implementation Contract

Implementation must:

* add the plugin under `plugins/platforms/teams/`;
* use Marlow naming throughout;
* register through the current platform registry;
* implement `BasePlatformAdapter`;
* use the exact-pinned stable Microsoft Teams SDK;
* use the SDK for authentication and activity parsing;
* enforce the HTTP request boundary;
* enforce strict configured-tenant matching;
* use Azure AD object IDs for authorization identity;
* implement the bounded asynchronous handoff;
* implement durable activity receipt claims;
* preserve existing authorization and pairing order;
* preserve stable conversation and thread identities;
* implement structured mention handling;
* use authenticated SDK retrieval for inline images;
* reuse existing media validation and cache;
* implement serialized-payload-aware text chunking;
* implement bounded outbound retry;
* implement request-bound Adaptive Card approvals;
* implement `/healthz`;
* expose Teams through generic plugin setup and status hooks;
* correct generic registered-platform toolset validation;
* add operator documentation;
* regenerate the dependency lockfile;
* add focused and regression tests.

Implementation must not:

* add Teams branches to shared dispatch;
* add a core Teams enum;
* add a static Teams toolset;
* use Graph for ordinary chat;
* add Graph permissions;
* use a legacy SDK;
* use a prerelease SDK;
* await agent execution in the message webhook;
* create unbounded tasks;
* create a durable execution queue;
* implement crash replay;
* implement meeting ingestion;
* implement proactive delivery;
* add private/shared channel claims;
* implement general file handling;
* provision ingress infrastructure;
* log message bodies or credentials.

## Validation

Implementation validation must include:

### Static and Dependency Validation

* dependency-policy checks;
* exact SDK pin verification;
* lockfile consistency;
* Python runtime markers;
* plugin manifest validation;
* configuration schema checks;
* secret-redaction checks.

### Focused Automated Tests

* plugin registration;
* setup metadata;
* configuration parsing;
* dependency availability and failure;
* credential locking;
* listener lifecycle;
* health readiness;
* request-size enforcement;
* SDK authentication integration boundary;
* tenant validation;
* sender identity;
* mention matching and stripping;
* session key generation;
* channel thread isolation;
* durable receipt claims;
* duplicate suppression across restart;
* receipt collision handling;
* bounded supervisor saturation;
* async HTTP acknowledgement;
* shared authorization regression;
* text chunking;
* retry behavior;
* typing lifecycle;
* inbound image acquisition;
* media rejection;
* approval callback validation;
* approval one-shot resolution;
* shutdown fail-closed behavior;
* platform tool exposure;
* multi-platform failure isolation.

### Broad Regression Tests

Run the broadest reasonable:

* gateway suite;
* plugin suite;
* authorization suite;
* approval suite;
* media suite;
* session suite;
* dependency-policy suite;
* configuration migration and compatibility suite.

### Manual Live Smoke Test

When Microsoft credentials and public HTTPS ingress are available, validate:

1. one personal-chat text message;
2. one group-chat mention;
3. one standard-channel mention;
4. one threaded standard-channel reply;
5. one unauthorized user;
6. one tenant mismatch where practical;
7. one inline image;
8. one outbound image;
9. one response requiring multiple text chunks;
10. one administrator approval;
11. one duplicate activity or controlled webhook replay;
12. one gateway restart followed by duplicate replay;
13. simultaneous operation with another gateway platform.

Record:

* timestamp;
* tenant;
* Marlow commit;
* SDK version;
* test surface;
* result;
* relevant nonsecret logs.

Any live test that cannot run must be reported as:

```text
BLOCKED
```

It must not be marked passed or silently replaced by a mock.

## Risks and Residual Limitations

### Crash After Receipt Claim

A process crash after receipt claim and before task completion can lose the message.

This is an accepted Milestone 1 trade-off in favor of avoiding duplicate side-effecting tool execution.

### Reply Delivery Failure After Tool Completion

A tool may complete successfully while the final Teams reply fails.

The activity receipt prevents automatic replay of the original request. Operators must use logs and tool-side audit data to determine the external result.

Durable response replay is outside scope.

### Tenant Policy

A tenant administrator may block:

* custom application installation;
* bot access;
* application permissions;
* the selected user;
* the selected channel.

These are operator prerequisites and not Marlow code paths.

### SDK Compatibility

The SDK is pinned, but future Teams platform changes may require an intentional dependency upgrade.

No automatic unbounded upgrade is allowed.

### Outbound Image Compatibility

Self-contained image delivery must be verified against the target commercial Teams tenant.

Failure blocks the image acceptance criterion and does not authorize Graph or public media infrastructure.

### Public Ingress

Incorrect reverse-proxy configuration can:

* remove the authorization header;
* expose unrelated routes;
* break request-body forwarding;
* bypass expected rate controls;
* report a healthy proxy while the adapter is unavailable.

Operator documentation and `/healthz` reduce but do not eliminate this risk.

### Standard Channel Scope

Private and shared channel behavior is intentionally unsupported.

The application manifest and product documentation must not imply broader coverage.

## Scope Review

* **Result:** Passed
* **Bundled platform plugin:** retained
* **Core platform neutrality:** retained
* **Synchronous webhook execution:** removed
* **Tenant boundary:** made explicit
* **Duplicate handling:** changed from process-local suppression to durable at-most-once receipts
* **Durable execution queue:** not introduced
* **Graph integration:** excluded
* **Channel scope:** restricted to standard channels
* **Media ownership:** split between Teams-authenticated acquisition and shared Marlow validation
* **Health endpoint:** restored for ingress readiness
* **SDK version:** resolved to exact stable pin
* **Remaining product decisions:** None
* **Known implementation blockers:** unsupported Python runtime, failed SDK compatibility validation, or failed live outbound-image validation

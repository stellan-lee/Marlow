# Microsoft Teams Gateway — Stateless Thread Context and Canonical Message Identity

**Status:** Proposal — Ready for Engineering Review

**Document type:** Engineering Design Amendment

**Date:** 2026-09-03

**Owners:** Teams Platform Plugin, Gateway Runtime, Agent Runtime

**Scope:** Standard Microsoft Teams channel threads invoked by an explicit bot mention

---

## 1. Executive Summary

For a Microsoft Teams channel thread, Teams—not Marlow—is the authoritative conversation store. Every authorized mention must reconstruct the thread from Microsoft Graph and run the agent from that fresh snapshot. Marlow must not load, replay, summarize, or persist local conversation history for these turns.

This design makes two corrections to the current implementation:

1. **Canonical message identity.** The current implementation derives the Graph root only from the `messageid=` component of the Bot Framework `activity.conversation.id`. It does not model the current message separately, does not use alternate locator evidence, and does not validate that Graph returned the requested root and only its replies. The new resolver keeps `root_message_id` and `current_message_id` distinct, evaluates all available inbound candidates, and proves the selected root against the Graph response topology.
2. **Actually stateless thread history.** The current `REPLACE_VISIBLE_SESSION_HISTORY` mode clears visible history for the model call, but the gateway still creates/reuses a session, loads its transcript before clearing it, caches a mutable agent by session key, and writes the new user/assistant/tool messages to JSONL and SQLite. The new `EXTERNAL_AUTHORITATIVE_STATELESS` mode performs zero local conversation-history reads and zero conversation-history writes.

It also preserves two useful non-conversational context systems behind explicit scope and write policies:

3. **Customer-scoped Memory and governed Work Experience.** Stateless means that Marlow does not replay or duplicate the Teams transcript. It does not mean that the agent forgets durable customer facts or approved reusable lessons. A Teams turn may recall customer-scoped Memory and eligible Work Experience as separately labelled advisory context. Automatic turn ingestion, raw-thread persistence, profile-global `USER.md` injection, and background review over the hydrated Graph snapshot are prohibited.

The resulting invariant is:

> For every authorized mention in a supported Teams channel thread, the model receives one complete, validated Graph snapshot through the triggering message and no local session conversation history. If Marlow cannot establish that snapshot, it does not invoke the agent.

This document supersedes the root-identity, retrieval, and persistence decisions in sections 9.2, 14.1, 15.2–15.4, and 17.3–17.4 of `docs/microsoft-teams-gateway-full-thread-context-engineering-design.md`. Its other security, RSC, channel-support, and failure-handling decisions remain in force.

---

## 2. Decisions Requested

Approval of this design approves the following decisions:

1. Microsoft Graph is the sole source of conversational history for supported Teams channel threads.
2. A Teams channel-thread agent turn does not read or write a Marlow conversation transcript.
3. Marlow may retain a thread-scoped **execution lane** for concurrency, interruption, approvals, and tool lifecycle, but that lane is not a conversation session and supplies no messages to the model.
4. `current_message_id` always comes from the authenticated inbound activity ID.
5. `root_message_id` is selected from transport candidates and must be validated by Graph; no single Bot Framework field is blindly treated as a Graph identifier.
6. A fresh agent instance is used for each stateless Teams turn unless a future cache implementation can prove that all mutable conversational state is reset.
7. The root plus every replies page is loaded on every agent-bearing mention; the triggering message is reconciled exactly once.
8. Session-history commands are not applicable in stateless Teams threads. Execution-control commands remain available.
9. Images and attachment byte retrieval are outside this change. Text and attachment descriptors remain part of normalization.
10. Customer-scoped Memory recall and Work Experience recall remain available when an explicit authorized scope can be resolved.
11. A hydrated Graph thread is never automatically synchronized into Memory, Experience, consolidation evidence, or the skill library.
12. Memory writes from a Teams channel require an explicit current-user instruction, an authorized customer scope, and provenance; Work Experience retains its existing approval and egress governance.

---

## 3. Evidence and Current-Code Findings

### 3.1 Live Graph probe

A live probe against the configured Team and standard channel returned:

- one root message;
- eight replies on one page;
- nine messages total; and
- the supplied current message ID in the reply collection.

This proves the intended Graph read path works with the installed app's `ChannelMessage.Read.Group` resource-specific consent:

```http
GET /v1.0/teams/{team-id}/channels/{channel-id}/messages/{root-message-id}
GET /v1.0/teams/{team-id}/channels/{channel-id}/messages/{root-message-id}/replies?$top=50
```

The probe also showed a real root post whose visible title was in `subject`, while `body.content` contained only an attachment placeholder. Therefore a body-only renderer incorrectly reports the root as empty even though Graph returned the visible text.

### 3.2 Current code path

| Component | Current behavior | Problem |
|---|---|---|
| `plugins/platforms/teams/adapter.py::_parse_root_message_id` | Parses exactly one `messageid=` from `activity.conversation.id` | Single unvalidated locator source; no explicit current/root contract |
| `TeamsAdapter._thread_locator` | Builds a locator containing only `root_message_id` | Cannot carry or validate the trigger ID as part of the retrieval contract |
| `TeamsAdapter._build_source` | Stores the complete Bot Framework conversation ID as `source.thread_id` | Delivery route, execution lane, and Graph thread identity are conflated |
| `TeamsAdapter._load_thread_snapshot` | Fetches root plus replies and deduplicates IDs | Does not validate root `replyToId`, reply parentage, or `channelIdentity` |
| `TeamsAdapter._build_snapshot` | Reconciles the trigger and sets `REPLACE_VISIBLE_SESSION_HISTORY` | Activity timestamp lookup ignores the standard `timestamp` field |
| `GatewayRunner._handle_message_with_agent` | Calls `load_transcript`, then replaces `history` with `[]` | Local history is still touched and session lifecycle still runs |
| Gateway transcript persistence block | Appends `session_meta`, user, assistant, and tool rows | Creates a second conversation record despite Graph being authoritative |
| `GatewayRunner._run_agent` | Caches `AIAgent` by session key and passes `session_id` plus `session_db` | Mutable cross-turn state and agent-side persistence remain possible |
| `AIAgent._persist_session` | Saves a session log and flushes messages to SQLite on many exit paths | Gateway-only write guards cannot guarantee stateless behavior |
| `tests/gateway/test_teams_adapter.py` | Covers pagination and adapter normalization | Fixture omits `replyToId`, uses `createdDateTime` instead of Bot Framework `timestamp`, and has no gateway persistence assertions |

### 3.3 Why clearing `history` is insufficient

The current behavior is approximately:

```python
history = session_store.load_transcript(session_id)
if snapshot.history_mode == REPLACE_VISIBLE_SESSION_HISTORY:
    history = []

agent = AIAgent(session_id=session_id, session_db=session_db)
result = agent.run_conversation(..., conversation_history=history)

session_store.append_to_transcript(session_id, new_messages)
```

This prevents old transcript rows from being visible in one prompt, but it does not make the turn stateless. The transcript is still read, the agent still has a persistent session identity, and the current turn is still written for possible future replay.

---

## 4. Goals

1. Build the model-visible context from the complete Teams thread on every authorized mention.
2. Keep root and current message identity unambiguous and independently observable.
3. Validate that all Graph messages belong to the exact Team, channel, and root requested.
4. Include the current authenticated request exactly once despite Graph consistency lag.
5. Perform no local conversation-history reads or writes for Teams channel-thread turns.
6. Preserve thread-scoped concurrency, interruption, approval, and response routing.
7. Preserve existing behavior for Teams personal chats, Teams group chats, and all other platforms.
8. Fail closed rather than answer with partial, cross-thread, or unverifiable context.
9. Preserve relevant customer-scoped Memory and approved Work Experience without treating either as conversation history.
10. Prevent Graph messages and prior participants' content from becoming durable Memory or Experience through automatic turn ingestion.

## 5. Non-Goals

This change does not add:

- image or file byte retrieval from SharePoint;
- OCR or image understanding for historical messages;
- private or shared channel support;
- Graph subscriptions or ambient ingestion;
- Graph-based outbound message sending;
- tenant-wide `ChannelMessage.Read.All`;
- a local Teams message mirror;
- silent truncation or automatic summarization of oversized threads; or
- stateless behavior for DMs, group chats, Slack, Discord, or other platforms.

---

## 6. Terminology and Identity Model

### 6.1 Four retrieval identifiers

| Field | Source | Meaning | May be used as another field? |
|---|---|---|---|
| `team_id` | `channelData.team.aadGroupId`, a UUID `team.id`, or the existing Teams API resolution fallback | Microsoft Entra group ID used in the Graph URL | Never use `tenant_id` |
| `channel_id` | `channelData.channel.id` | Teams channel ID used in the Graph URL | No |
| `root_message_id` | Resolved candidate, then proven by Graph | Root post under `/messages/{root}` | Never substitute the current reply ID |
| `current_message_id` | Authenticated Bot Framework `activity.id` | The mention that triggered this turn | Never infer from `conversation.id` |

`tenant_id` remains required for token acquisition and isolation, but it is not the Graph `team-id` path parameter.

### 6.2 Transport IDs versus Graph IDs

Two APIs use similarly named fields:

- Bot Framework/Teams Activity has `id`, `replyToId`, and `conversation.id`.
- Microsoft Graph `chatMessage` has `id` and `replyToId`.

They must not be assumed interchangeable solely because their property names match. In particular, a Bot Framework `replyToId` may be absent or may be an activity-routing identifier. A Graph reply's `replyToId`, once retrieved from the intended endpoint, is authoritative evidence that the reply belongs to the selected root.

### 6.3 Separate route, execution, and conversation concepts

| Concept | Canonical value | Purpose | Persisted message history? |
|---|---|---|---|
| Delivery route | Authenticated Teams conversation reference in `source.metadata.teams_reference` | Send typing, progress, and final responses to the origin thread | No |
| Execution lane | `teams:{tenant}:{team}:{channel}:{root}` | Serialize/interrupt runs and scope approvals or live tools | No |
| Conversation context | Fresh validated `ExternalConversationSnapshot` | Build the current model input | No |
| Current request | Sanitized inbound activity text plus `current_message_id` | Sole active user instruction | No |
| Customer Memory | Explicit `customer_id` resolved from deployment configuration and authorized principal | Recall durable customer facts and preferences | Yes, but never as transcript |
| Work Experience | Existing governed profile/project scope plus provider-egress policy | Recall approved reusable decisions and lessons | Yes, as structured experience items |

The word `session` must not be used in user-facing behavior or model-context code for the Teams thread transcript. Existing internal session infrastructure may temporarily host execution metadata during migration, but it must pass the zero-read/zero-write tests in this document.

---

## 7. Target Architecture

```mermaid
sequenceDiagram
    participant T as Teams
    participant P as Teams adapter
    participant G as Microsoft Graph
    participant R as Gateway runtime
    participant A as Fresh agent run
    T->>P: Authenticated channel mention
    P->>R: Event + route + ID candidates
    R->>R: Authorize current sender
    R->>P: Enrich authorized event
    P->>G: Get and validate root
    P->>G: Get every replies page
    P-->>R: Complete snapshot + stateless policy
    R->>A: Snapshot + current request; history=[]
    A-->>R: Result
    R-->>T: Send via original conversation reference
    Note over R,A: No transcript load, replay, or persistence
```

The read and write paths intentionally remain separate:

- Microsoft Graph reads the authoritative thread.
- The existing Teams SDK/Bot Framework conversation reference sends the response.

---

## 8. Data Contracts

### 8.1 Locator

Extend the existing provider-specific locator:

```python
@dataclass(frozen=True, slots=True)
class TeamsThreadLocator:
    tenant_id: str
    team_aad_group_id: str
    channel_id: str
    root_message_id: str
    current_message_id: str
    root_source: str
```

`root_source` is one of:

- `conversation_messageid`;
- `activity_reply_to_id`;
- `activity_id_root`; or
- `graph_fallback`.

It is observability metadata, not model-visible content.

### 8.2 Conversation-history policy

Replace the ambiguous one-value history mode with an explicit policy:

```python
class ExternalHistoryMode(str, Enum):
    REPLACE_VISIBLE_SESSION_HISTORY = "replace_visible_session_history"  # legacy
    EXTERNAL_AUTHORITATIVE_STATELESS = "external_authoritative_stateless"
```

`EXTERNAL_AUTHORITATIVE_STATELESS` has a stronger contract than merely setting `history=[]`:

| Operation | Required behavior |
|---|---|
| `SessionStore.load_transcript` | Must not be called |
| Session compression/summary restore | Must not run |
| Session reset/resume notices | Must not run |
| Agent `conversation_history` | Exactly `[]` |
| Agent log/SQLite persistence | Disabled for all exit paths |
| Gateway JSONL/SQLite append | Must not run |
| Agent cache | Bypassed in the first implementation |
| Graph snapshot persistence | Forbidden |
| Run metrics and approval audit | Allowed without message bodies |

### 8.3 Trigger timestamp

Add one helper that accepts SDK objects and raw dictionaries:

```python
def _extract_activity_timestamp(activity) -> datetime | None:
    # Bot Framework contract first; current compatibility fields second.
    return parse(
        activity.timestamp
        or raw["timestamp"]
        or activity.created_date_time
        or raw["createdDateTime"]
    )
```

Do not use `datetime.now()` as a trigger-boundary substitute. If the trigger is absent from Graph and the authenticated activity has no valid timestamp, completeness cannot be established and the turn must fail closed.

---

## 9. Canonical Root Resolution

### 9.1 Candidate extraction

For an authenticated channel activity:

```python
current_id = nonempty(activity.id)
conversation_root = parse_exact_messageid(activity.conversation.id)
activity_reply = nonempty(activity.reply_to_id or raw.get("replyToId"))

candidates = ordered_unique(
    conversation_root,
    activity_reply,
    current_id,
)
```

The order preserves the Teams thread ID already carried by the conversation context, then tries an explicit reply relationship, then the current ID for a root-post mention. Candidate order is not proof; Graph validation is proof.

Parsing requirements for `conversation.id`:

- parse semicolon parameters, not arbitrary substring matches;
- URL-decode the parameter value once;
- accept exactly one non-empty, valid `messageid` value;
- reject duplicate/conflicting `messageid` parameters;
- never parse the channel ID itself as a message ID.

### 9.2 Candidate validation

For each candidate, with retries bounded by the existing Graph policy:

1. Request `GET /teams/{team}/channels/{channel}/messages/{candidate}`.
2. Require HTTP 200 and a JSON object.
3. Require `root.id == candidate`.
4. Require `root.replyToId` to be empty/null. A reply cannot be accepted as a root.
5. When `root.channelIdentity` is present, require its Team and channel IDs to equal the locator.
6. Fetch every replies page for that candidate.
7. Require every reply's `replyToId == candidate`.
8. When reply `channelIdentity` is present, require the same Team and channel match.
9. Prefer the candidate whose reply set contains `current_message_id`.

Fallback to the next candidate only for a locator-shaped failure: 404, root topology mismatch, or trigger membership mismatch. Do not fall back on 401, 403, 429 exhaustion, 5xx exhaustion, malformed JSON, or a pagination failure; those are retrieval failures, not evidence that another candidate is correct.

### 9.3 Conflict behavior

| Situation | Result |
|---|---|
| Conversation and Activity candidates agree | Use and validate once |
| Candidates differ; one validates and contains current ID | Use the proven candidate; increment fallback/conflict metric |
| Current activity is the validated root | `root_message_id == current_message_id` |
| More than one distinct candidate appears valid | Fail closed; do not guess |
| No candidate validates | Fail closed with a same-thread retry message |
| Trigger is absent after bounded consistency retries but candidates agree | Use validated transport root, apply trigger reconciliation rules |
| Trigger is absent and candidates conflict | Fail closed; membership cannot prove the root |

This algorithm fixes wrong-root requests without issuing a channel-wide message search.

---

## 10. Thread Retrieval and Validation

### 10.1 Normal request count

The normal path performs:

1. one root request; and
2. one or more replies requests, following every `@odata.nextLink`.

Microsoft documents that the replies endpoint returns only replies, so the root must be fetched separately. Microsoft also documents a maximum `$top` of 50. See [Get chatMessage](https://learn.microsoft.com/en-us/graph/api/chatmessage-get?view=graph-rest-1.0), [List replies](https://learn.microsoft.com/en-us/graph/api/chatmessage-list-replies?view=graph-rest-1.0), and the [chatMessage resource](https://learn.microsoft.com/en-us/graph/api/resources/chatmessage?view=graph-rest-1.0).

### 10.2 Pagination invariants

- Start with `replies?$top=50`.
- Follow the exact Graph-provided `@odata.nextLink` until absent.
- Detect a repeated next link and fail closed.
- Reject a page without an array-valued `value`.
- Apply a bounded page/message/byte safety limit and report an explicit oversized-thread error; never label a truncated result complete.

### 10.3 Topology validation

Before normalization, validate:

```text
root.id == locator.root_message_id
root.replyToId is null
every reply.replyToId == locator.root_message_id
every available channelIdentity matches locator team/channel
no conflicting payloads share one message ID
```

`complete_through_trigger` may be set to `true` only after topology, pagination, and trigger reconciliation all pass.

### 10.4 Trigger reconciliation

Graph and Bot Framework can expose the newly sent message at different times. The authenticated inbound activity is therefore the source of truth for the active request.

Algorithm:

1. Normalize root and all replies.
2. If `current_message_id` is present, remove its Graph copy from historical context.
3. Exclude Graph messages that are provably later than the trigger.
4. Append the normalized authenticated inbound activity exactly once as `is_trigger=True`.
5. Render historical messages, then render the current request from `event.text`.

If the trigger is absent from Graph:

- retry the replies read for a short bounded consistency window;
- use the Activity's UTC `timestamp` as the boundary;
- retain messages strictly before that timestamp;
- if a same-timestamp message makes ordering ambiguous, fail closed rather than guess; and
- append the authenticated activity exactly once.

Do not use text, author, or timestamp alone for deduplication. Message ID is the deduplication key.

---

## 11. Message Normalization

### 11.1 Visible text

Normalize Graph text as two independent fields:

```python
subject = normalize_plaintext(message.get("subject"))
body_text = html_to_text(message.get("body"))
```

Rendering rules:

1. Render a non-empty `subject` first.
2. Render non-empty body text second.
3. Suppress duplicate subject/body text.
4. Strip bare `<attachment ...></attachment>` placeholders from display text.
5. If text is empty but attachment descriptors exist, render descriptors; do not claim the message is absent.
6. Preserve author, created time, edited/deleted state, message ID, and parent ID structurally.

The live root-post result that motivated this amendment must render its `subject` even though its body contains only an attachment reference.

### 11.2 Current request

The current request must come from the authenticated inbound activity after removal of the bot's mention. The Graph copy of that message is historical data and must never replace the active request or its authenticated actor.

### 11.3 Attachments

This change keeps only safe metadata such as attachment name, content type, and reference kind. It does not dereference SharePoint URLs, download bytes, run OCR, or attach historical images to the model.

---

## 12. Stateless Gateway Semantics

### 12.1 What “no session history” means

For `EXTERNAL_AUTHORITATIVE_STATELESS`, all of the following are prohibited:

- loading prior user, assistant, system-summary, reasoning, or tool messages;
- restoring compressed session state;
- replaying a prior interrupted tool tail;
- persisting the current user request;
- persisting the Graph snapshot;
- persisting assistant output or tool messages as conversation history;
- auto-generating a session title from the turn;
- auto-compressing or splitting a conversation session; and
- reusing mutable agent state from a prior mention.

The bot's prior Teams response will naturally appear in Graph on the next mention. Persisting it locally adds no conversational value and creates divergence after edits/deletes.

### 12.2 What may remain local

The following are execution state, not conversation history, and may remain:

- one active-run guard per canonical thread;
- interruption and pending-message state;
- run ID and timing;
- approval/clarification state scoped to the live run;
- model/reasoning configuration selected for the thread lane;
- tool sandbox/process handles under an explicit execution-scope policy;
- idempotency receipts; and
- body-free metrics and security audit outcomes.

Normal system instructions, authorization policy, tool policy, customer-scoped Memory, and governed Work Experience remain separate inputs. None may be populated automatically from a local or Graph-backed Teams transcript.

### 12.3 Memory and Work Experience policy

Stateless conversation execution and durable recall are independent concerns. Teams owns the transcript; Marlow may still own curated facts and reusable lessons.

| Context source | Read policy | Write policy for a Teams channel turn |
|---|---|---|
| Local session transcript | Never | Never |
| Graph thread | Every authorized agent-bearing mention | Never copied into a Marlow history store |
| Profile-global `MEMORY.md` | Disabled unless it contains only non-customer operational facts and is explicitly approved for shared-channel disclosure | No automatic write |
| Profile-global `USER.md` | Disabled; a shared channel is not a private user context | Disabled |
| Customer-scoped Memory | Recall when `customer_id` is explicitly resolved and the current actor may use that scope | Explicit instruction only; store a distilled fact with actor, source message, customer scope, and timestamp |
| External conversational Memory provider | May be used only if it supports the same customer/principal boundary | Do not call generic `sync_turn` with the hydrated thread or full `messages` list |
| Work Experience | Recall approved Decisions and lessons in `assist` mode when project/customer scope and provider-egress policy allow it | Use the existing governed candidate/approval lifecycle; never create an item merely because it appeared in a thread |
| Consolidation evidence | Not needed for thread reconstruction | Do not append the Teams turn or hydrated `messages` automatically |
| Background memory/skill review | No review over the hydrated snapshot | Disabled for this mode unless a future implementation supplies a deliberately minimized, authorized input |

Customer Memory is not keyed by `session_id` or `root_message_id`. The deployment must resolve an explicit stable scope, for example:

```text
customer:{customer_id}
```

`customer_id` must come from trusted deployment configuration or an authorized Team-to-customer mapping. It must not be guessed from message text, Team display name, or channel name. If no authorized customer scope can be resolved, customer Memory fails closed while Graph thread execution continues.

Memory and Experience retrieval queries should use the authenticated current request plus a bounded normalized thread topic, not the complete raw Graph transcript. Recalled material is injected separately from the Teams messages and labelled as advisory context. The effective prompt precedence is:

1. system, authorization, and tool policy;
2. customer-scoped Memory and approved Work Experience;
3. untrusted Graph thread content; and
4. the authenticated current request.

The current implementation requires explicit changes to honor this policy:

- split the coarse `skip_memory` switch into independent read, explicit-write, automatic-ingestion, and background-review policies;
- prevent `_sync_external_memory_for_turn()` from calling provider `sync_turn`, structured-card sync, or consolidation evidence append for stateless Teams turns;
- keep the `memory` tool available only for authorized explicit customer-scoped operations, never profile-global writes;
- add `TurnOrigin.TEAMS` and a Teams eligibility/authorization path for Work Experience;
- update `_work_experience_turn_kwargs()` to pass only the raw authenticated mention, not the rendered Graph snapshot; and
- resolve Experience scope explicitly for the customer/project instead of relying only on the gateway process working directory.

Recommended runtime policy:

```python
ContextPolicy(
    conversation_source="teams_graph",
    conversation_persistence="none",
    memory_read_scope="customer",
    memory_write_mode="explicit_only",
    automatic_memory_ingestion=False,
    experience_mode="assist",
    background_review=False,
)
```

### 12.4 Gateway branch

Add one predicate and use it at every lifecycle boundary:

```python
stateless_thread = (
    event.external_conversation_snapshot is not None
    and event.external_conversation_snapshot.history_mode
        == ExternalHistoryMode.EXTERNAL_AUTHORITATIVE_STATELESS
)
```

Required control flow:

```python
if stateless_thread:
    history = []                       # do not call load_transcript
    agent = build_fresh_agent(
        session_db=None,
        conversation_persistence=False,
    )
else:
    history = session_store.load_transcript(session_id)
    agent = get_or_create_session_agent(...)

result = agent.run_conversation(
    message=render_snapshot_and_current_request(event),
    conversation_history=history,
    task_id=execution_scope_id,
    persistence_policy="none" if stateless_thread else "session",
)

if not stateless_thread:
    persist_gateway_transcript(result)
```

### 12.5 Agent-runtime persistence guard

Gateway guards alone are insufficient because `AIAgent._persist_session` is called from many success and error exits. Add a centralized agent persistence policy:

```python
class ConversationPersistencePolicy(str, Enum):
    SESSION = "session"
    NONE = "none"
```

When the policy is `NONE`:

- `_ensure_db_session` does not create a conversation row;
- `_save_session_log` is skipped;
- `_flush_messages_to_session_db` is skipped;
- compression cannot split/create a durable session;
- returned in-memory messages are available only to complete the current run; and
- cleanup discards them after response delivery.

Put the guard in the central persistence functions, not at every caller, and test early-error, tool-error, interrupt, timeout, max-iteration, compression, and success exits.

### 12.6 Agent cache

The first implementation must bypass `_agent_cache` for stateless Teams turns. A cached `AIAgent` contains more than immutable provider configuration, including counters, to-do state, persistence cursors, and other mutable fields. A later optimization may cache only an immutable agent factory/configuration or introduce a fully tested `reset_for_stateless_turn()` contract.

### 12.7 Commands

| Command category | Teams thread behavior |
|---|---|
| `/stop`, approval/deny, live clarification | Retain; these control the active execution lane |
| `/model`, `/reasoning`, display controls | Retain if currently lane-scoped |
| `/new`, `/reset`, `/resume`, `/undo`, `/retry`, `/compress` | Return a clear “Teams owns this thread history; this command does not apply” response |
| Goal/session continuation features | Disable for stateless thread turns unless redesigned against Graph |

Control commands must not create a transcript. Commands that do not need conversation context should bypass Graph hydration after authorization.

---

## 13. Execution Concurrency

Build the lane key from canonical identity, not the opaque Bot Framework conversation string:

```text
teams:{tenant_id}:{team_id}:{channel_id}:{root_message_id}
```

`BasePlatformAdapter.handle_message` currently establishes its active-session guard before the gateway's post-authorization enrichment hook runs. Do not mutate that live guard or `source.thread_id` after Graph resolution. Treat it as an ingress/debounce guard only, and add a post-enrichment `ThreadExecutionCoordinator` keyed by the canonical value above. The coordinator is acquired before agent invocation and owns canonical serialization, interruption, approvals, and pending-turn handoff. This permits a transport locator fallback to prove a different root without moving an already-live adapter guard.

Locator proof may occur before acquiring the canonical coordinator. The final snapshot used by the model must be fetched or refreshed after the coordinator is acquired. Therefore a mention that waited behind another run sees the prior run's Teams reply and any intervening human replies instead of using a snapshot captured before the wait.

Required behavior:

- at most one active agent run per thread lane;
- a new mention in the same lane follows the existing explicit interrupt/queue policy;
- before the successor run starts, it fetches Graph again;
- different root messages execute concurrently;
- the pre-enrichment adapter guard never becomes a conversation-history key;
- IDs in logs are hashed or truncated according to existing privacy policy; and
- no lane state contributes conversation messages to the next run.

The authenticated conversation reference remains the outbound route. Changing the execution key must not alter the reference used by `TeamsAdapter.send`.

---

## 14. Failure Policy

| Failure | Behavior |
|---|---|
| Missing team/channel/current ID | Same-thread safe error; zero Graph calls when no safe locator can be formed |
| No root candidate validates | Same-thread safe error; zero agent invocations |
| Candidate conflict cannot be proven | Fail closed; emit conflict metric without raw IDs |
| 401 | Authentication error; zero agent invocations |
| 403 | Missing RSC consent error; zero agent invocations |
| 404 on one candidate | Try the next locator candidate if available |
| 429 or retryable 5xx | Honor bounded retry; fail closed after exhaustion |
| Broken pagination or topology | Fail closed |
| Trigger absent and no valid timestamp | Fail closed |
| Context exceeds configured budget | Explicit oversized-thread response; no silent truncation |
| Agent failure after a complete snapshot | Report the agent failure; do not persist the request for retry |

No failure path may fall back to local session history.

---

## 15. Security and Privacy

1. Preserve the current authorization-before-enrichment ordering in `gateway/run.py`.
2. Treat historical thread content as untrusted data, not authorization or a change of current actor.
3. The authenticated Activity sender remains the only actor allowed to authorize tools and approvals for the turn.
4. Keep Team-scoped `ChannelMessage.Read.Group` RSC; do not widen to tenant-wide read permissions.
5. Never log message bodies, subjects, attachment URLs, Graph tokens, secrets, or rendered prompts.
6. Do not persist hydrated content to JSONL, SQLite, session summaries, titles, background-review transcripts, or caches.
7. A short-lived in-memory fetch result may be introduced later only as a performance optimization and must be keyed by the full locator plus trigger boundary. It cannot become a history source.
8. Do not inject profile-global `USER.md` into a shared Teams channel response.
9. Customer Memory requires an explicit authorized customer scope and must not be addressed through a thread/session key.
10. Recalled Memory and Experience are advisory and cannot authorize actions or override the authenticated current actor.
11. Explicit Memory writes must record customer scope and provenance without copying the source Thread.

---

## 16. Observability

Add body-free metrics:

- `teams_thread_locator_total{source,result}`;
- `teams_thread_locator_conflict_total{result}`;
- `teams_thread_graph_requests_total{operation,status}`;
- `teams_thread_reply_pages`;
- `teams_thread_messages`;
- `teams_thread_trigger_reconciliation_total{graph_present, result}`;
- `teams_thread_context_build_seconds`;
- `teams_thread_stateless_runs_total{result}`; and
- `teams_thread_history_io_violation_total{operation}`;
- `teams_thread_customer_memory_recall_total{result}`;
- `teams_thread_customer_memory_write_total{result}`; and
- `teams_thread_experience_recall_total{result}`.

For each run, structured debug metadata may include hashed locator components, chosen candidate source, page/message counts, whether the trigger was Graph-present, and completion status. It must not include raw message content or credentials.

The history-I/O violation counter should remain zero and is suitable for an assertion in integration tests.

---

## 17. Code Change Plan

### PR 1 — Canonical identity and timestamps

Files:

- `plugins/platforms/teams/adapter.py`
- `gateway/platforms/base.py` if an explicit execution-lane field is added
- `tests/gateway/test_teams_adapter.py`

Changes:

- add `current_message_id` and `root_source` to `TeamsThreadLocator`;
- replace `_parse_root_message_id` with candidate extraction;
- add `_extract_activity_timestamp` using Bot Framework `timestamp`;
- derive a canonical thread execution key without changing outbound reference metadata;
- validate root IDs, reply parentage, and channel identity; and
- update fixtures to resemble captured modern Activity payloads.

### PR 2 — Complete snapshot and text correctness

Files:

- `plugins/platforms/teams/adapter.py`
- `gateway/platforms/base.py`
- `tests/gateway/test_teams_adapter.py`

Changes:

- implement candidate fallback and bounded consistency retry;
- retain full pagination;
- reconcile the trigger by ID exactly once;
- enforce topology and trigger-boundary invariants;
- render root `subject` plus body correctly; and
- preserve attachment descriptors without fetching bytes.

### PR 3 — Stateless gateway and agent runtime

Files:

- `gateway/platforms/base.py`
- `gateway/run.py`
- `run_agent.py`
- `agent/conversation_loop.py`
- focused gateway and agent-runtime tests

Changes:

- add `EXTERNAL_AUTHORITATIVE_STATELESS`;
- skip transcript load entirely;
- bypass session hygiene, title, resume, compression, and transcript writes;
- add centralized `ConversationPersistencePolicy.NONE`;
- pass `session_db=None` or an equivalent non-persistent dependency;
- bypass mutable agent caching; and
- add the post-enrichment canonical `ThreadExecutionCoordinator`; and
- retain only the canonical execution lane after ingress.

### PR 4 — Scoped Memory and Work Experience

Files:

- `gateway/run.py`
- `run_agent.py`
- `agent/conversation_loop.py`
- `agent/experience/runtime.py`
- the selected customer Memory provider/adapter
- focused Memory and Experience policy tests

Changes:

- introduce independent conversation, Memory read/write, automatic-ingestion, Experience, and background-review policies;
- resolve an authorized `customer_id` independently of the Teams thread identity;
- permit customer-scoped recall and explicit-only customer Memory writes;
- block generic `sync_turn`, consolidation evidence, structured cards, and snapshot-based background review;
- add `TurnOrigin.TEAMS` and an explicit Teams Experience scope/egress path; and
- inject recalled Memory and Experience as separately labelled, wire-only advisory context.

### PR 5 — End-to-end and live verification

Changes:

- add a gateway integration test proving zero local history I/O;
- run the captured-payload fixture suite;
- run the existing Teams and gateway regression suites;
- repeat the live nine-message probe through the actual bot path; and
- confirm the reply lands in the same Teams thread.

Do not combine PR 1's identity changes with PR 3's persistence changes in one unreviewable patch.

---

## 18. Test Strategy

### 18.1 Locator unit tests

1. Root mention: current ID is selected and validated as root.
2. Reply mention: conversation `messageid` resolves the root and activity ID remains current.
3. Activity `replyToId` present and equal to the conversation candidate.
4. Activity `replyToId` absent, as observed in Teams variants.
5. Conversation candidate wrong but alternate candidate validates and contains current.
6. Root/current IDs deliberately swapped: the reply candidate is rejected as a root.
7. Two candidates validate ambiguously: fail closed.
8. Duplicate `messageid` parameters: fail closed.
9. Tenant ID passed as Team ID: channel identity mismatch or 404; no agent run.
10. URL-sensitive channel IDs are encoded exactly once.

### 18.2 Retrieval and normalization tests

1. One root plus zero replies.
2. One root plus more than 50 replies across multiple pages.
3. Repeated `@odata.nextLink` fails closed.
4. Every reply must point to the chosen root.
5. Trigger present in Graph appears once as the active request.
6. Trigger absent during consistency lag is appended from Activity once.
7. Standard Bot Framework `timestamp` controls the trigger boundary.
8. A later concurrent reply is excluded.
9. Same-timestamp ambiguity fails closed.
10. Root text stored only in `subject` is rendered.
11. Attachment-only body does not erase the root subject.
12. Edited/deleted messages and distinct authors are preserved.

### 18.3 Stateless gateway contract tests

For an event marked `EXTERNAL_AUTHORITATIVE_STATELESS`, assert:

- `SessionStore.load_transcript` was not called;
- `SessionStore.append_to_transcript` was not called;
- `SessionDB.append_message` was not called;
- `_save_session_log` was not called;
- `conversation_history == []`;
- a fresh agent was constructed for the second mention;
- no old assistant, tool, summary, reasoning, or to-do state appears in the second prompt;
- the first bot response appears in the second context only when Graph returns it;
- success, transient failure, tool failure, interruption, timeout, and max-iteration exits all perform zero history writes; and
- personal/group chats and other platforms retain existing session behavior.

### 18.4 Memory and Experience boundary tests

1. A Teams thread turn may recall only Memory under its explicitly resolved `customer_id`.
2. No customer mapping means zero Memory access but does not prevent Graph thread execution.
3. Profile-global `USER.md` is absent from a shared-channel prompt.
4. The external provider never receives the full Graph snapshot through `sync_turn(messages=...)`.
5. Consolidation evidence, structured memory cards, and background review receive zero hydrated Thread messages.
6. An ordinary completed turn creates no Memory entry.
7. An authorized explicit “remember” request writes one distilled customer-scoped entry with provenance.
8. A participant without customer-memory write authority cannot create or modify an entry.
9. Teams Experience recall receives only the raw authenticated mention as its query input.
10. Only approved, in-scope, provider-egress-authorized Experience items are injected.
11. Recalled Memory and Experience never enter the persisted conversation transcript because no transcript exists.

### 18.5 End-to-end acceptance fixture

Use a sanitized captured shape equivalent to the verified thread:

- root with visible `subject` and an attachment placeholder body;
- multiple human replies;
- multiple prior bot replies;
- current mention in the replies collection; and
- no image download.

The resulting first model request must contain the root subject and all messages through the trigger in order, with the current request exactly once and no locally stored conversation row.

---

## 19. Rollout and Migration

1. Land identity and validation changes behind the existing `teams.thread_context.enabled` gate.
2. Add `EXTERNAL_AUTHORITATIVE_STATELESS` and make it the only supported history policy when Teams full-thread context is enabled.
3. Existing Teams thread transcripts remain on disk for audit/rollback but become unreachable from this model path. Do not migrate or merge them into Graph context.
4. During canary, alert on locator conflicts, Graph completeness failures, and any nonzero history-I/O violation.
5. Roll back by disabling `teams.thread_context.enabled`; do not silently revert an enabled thread to local session history.

Suggested canary success criteria over at least 50 authorized mentions:

- zero cross-thread context incidents;
- zero history-I/O violations;
- 100% current-message exactly-once reconciliation;
- 100% same-thread outbound delivery;
- no unexplained Graph 403; and
- bounded p95 context-build latency acceptable for the deployment.

---

## 20. Acceptance Criteria

The implementation is complete when all of the following are true:

1. `root_message_id` and `current_message_id` are distinct fields from intake through observability.
2. The selected root is proven by Graph topology, not accepted from an unvalidated transport string.
3. The root and all replies through the trigger are present and ordered deterministically.
4. A root whose visible text exists only in `subject` is not rendered empty.
5. The current authenticated request appears exactly once.
6. No local conversation transcript is read for a Teams channel-thread turn.
7. No user, assistant, reasoning, tool, summary, or snapshot message is persisted by either gateway or agent runtime for that turn.
8. No mutable agent instance carries conversational state between mentions.
9. Same-thread execution controls still work, and different threads remain concurrent.
10. Graph failure, locator ambiguity, partial pagination, or topology mismatch produces zero agent invocations.
11. The response uses the authenticated origin conversation reference and lands in the same Teams thread.
12. Teams personal/group chats and all other platforms pass regression tests unchanged.
13. Customer-scoped Memory and approved Work Experience can be recalled without reading a session transcript.
14. The Graph snapshot is never automatically written to Memory, Experience, consolidation evidence, or skills.
15. Profile-global user memory is not exposed in a shared Teams channel.
16. Explicit customer Memory writes are authorized, scoped, distilled, and provenance-bearing.

---

## 21. Alternatives Rejected

### Keep `REPLACE_VISIBLE_SESSION_HISTORY` as-is

Rejected. It hides old history for one prompt but continues local history reads, writes, agent caching, compression/session lifecycle, and agent-side persistence.

### Prefer `activity.replyToId` without validation

Rejected. Bot Framework and Graph expose similarly named fields with different transport semantics, and Teams may omit the Activity value. A Graph root must be proven as a root.

### Trust only `conversation.id;messageid=`

Rejected as a sole strategy. It is valuable Teams thread-routing evidence and remains the normal first candidate, but format drift, malformed activities, and wrong-ID bugs require validation and bounded fallback.

### Persist only the latest request and response

Rejected. The next mention already obtains both from Teams/Graph. Even partial local persistence creates a competing transcript and enables accidental replay.

### Create a new temporary conversation session per mention

Rejected. It still writes disposable history and leaves garbage rows. A non-persistent agent policy expresses the requirement directly.

### Use Graph channel-list messages and search for the current ID

Rejected. It is slower, broader than necessary, harder to paginate safely, and unnecessary when the activity supplies thread candidates.

---

## 22. Final Recommendation

Implement Teams standard channel threads as externally authoritative, stateless conversation turns:

```text
authenticated mention
  -> resolve and prove canonical root
  -> fetch root + all replies
  -> reconcile current activity exactly once
  -> recall authorized customer Memory and approved Work Experience
  -> run a fresh non-persistent agent with no local conversation history
  -> reply through the original Teams conversation reference
  -> discard turn messages
```

This is a stronger and simpler contract than repairing a Teams-to-local-session mapping. It matches the platform users can see, survives restarts and replicas, reflects edits and deletions, and removes the dual-source behavior that caused the thread-context confusion.

---

## Appendix A — Reviewed Microsoft Contracts

- [Channel and group chat conversations for agents](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/channel-and-group-conversations) — Teams channel thread routing and Activity examples.
- [MessageActivity class](https://learn.microsoft.com/en-us/javascript/api/teams-sdk-typescript/%40microsoft/teams.api/messageactivity?view=msteams-sdk-ts-latest) — Activity `id`, `replyToId`, and `timestamp` fields.
- [Get chatMessage](https://learn.microsoft.com/en-us/graph/api/chatmessage-get?view=graph-rest-1.0) — Graph root and reply request shapes.
- [List channel message replies](https://learn.microsoft.com/en-us/graph/api/chatmessage-list-replies?view=graph-rest-1.0) — replies-only behavior, RSC permission, and `$top=50` limit.
- [chatMessage resource](https://learn.microsoft.com/en-us/graph/api/resources/chatmessage?view=graph-rest-1.0) — Graph `replyToId`, `subject`, body, author, timestamp, and channel identity semantics.
- [Grant resource-specific consent](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/rsc/grant-resource-specific-consent) — Team-scoped RSC configuration and verification.

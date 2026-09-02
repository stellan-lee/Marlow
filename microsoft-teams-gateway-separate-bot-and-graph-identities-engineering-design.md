# Microsoft Teams Gateway — Separate Bot and Graph Application Identities

**Status:** Proposal — Ready for Engineering Review  
**Document type:** Engineering Design Amendment  
**Parent design:** `Microsoft Teams Gateway — Full Channel Thread Context`  
**Date:** 2026-09-02  
**Primary owners:** Teams Platform Plugin, Gateway Runtime, Setup and Configuration  
**Scope:** Single-tenant Microsoft Teams Bot Framework messaging and app-only Microsoft Graph thread hydration

This amendment supersedes the single-identity Graph credential and manifest identity requirements in the parent design.

---

## 1. Executive Summary

Marlow's Teams adapter currently uses one Microsoft Entra application identity for two independent platform paths:

1. **Bot Framework messaging** — authenticate inbound activities and send replies through the Teams/Bot Framework channel.
2. **Microsoft Graph access** — retrieve the root message and replies for a Teams channel thread.

The current implementation constructs `microsoft_teams.apps.App` with `teams.client_id`, `TEAMS_CLIENT_SECRET`, and `teams.tenant_id`. Its Graph client is then obtained from that same `App`, so Graph access always uses the Bot Framework application's identity.

The current deployment demonstrates that this assumption is not always valid:

- the Bot Framework identity is `1dc08c95-26a1-4cda-9efa-f9329e6a7b58`;
- the installed Teams application's effective RSC grants are assigned to `314c1e02-c0e6-4e39-b45b-7e7371b8965f`;
- `ChannelMessage.Read.Group` is granted to the latter identity; and
- a Graph request made with the Bot identity receives `403 Forbidden` with no matching resource-specific consent grants.

This amendment separates the two credential paths while keeping the existing configuration backward compatible:

```yaml
teams:
  client_id: "<BOT_CLIENT_ID>"
  graph_client_id: "<GRAPH_CLIENT_ID>" # optional; defaults to client_id
  tenant_id: "<TENANT_ID>"
```

```dotenv
TEAMS_CLIENT_SECRET=<BOT_CLIENT_SECRET>
TEAMS_GRAPH_CLIENT_SECRET=<GRAPH_CLIENT_SECRET>
```

The Bot SDK continues to use `teams.client_id` and `TEAMS_CLIENT_SECRET`. Thread hydration uses the effective Graph identity:

```text
effective_graph_client_id = teams.graph_client_id or teams.client_id
```

If the effective Graph client ID differs from the Bot client ID, `TEAMS_GRAPH_CLIENT_SECRET` is mandatory. If the IDs are equal, the existing Bot secret remains the default Graph secret, so current one-identity installations require no configuration migration.

The central invariant is:

> Bot Framework operations must use the Bot identity, and Microsoft Graph operations must use the identity to which the target Team's RSC grant was issued. Neither path may silently borrow credentials from the other when the application IDs differ.

---

## 2. Decisions Requested

Approval of this design means approving the following decisions:

1. **Support separate Bot and Graph application identities.** Marlow must not require `bots[].botId` and `webApplicationInfo.id` to be equal.
2. **Preserve `teams.client_id` as the Bot identity.** Existing configuration and session identity remain backward compatible.
3. **Add optional `teams.graph_client_id`.** It identifies the Entra application used for app-only Graph calls and must match `webApplicationInfo.id` and the RSC grant's `clientAppId`.
4. **Add `TEAMS_GRAPH_CLIENT_SECRET`.** It is conditionally required only when the Graph identity differs from the Bot identity.
5. **Build an independent Graph credential provider.** Graph thread hydration must no longer call `self._teams_app.get_app_graph()` or `self._teams_app._get_graph_token()`.
6. **Keep one tenant in the first release.** Both identities must belong to `teams.tenant_id`; cross-tenant Bot/Graph configurations are unsupported.
7. **Keep RSC least privilege.** Thread hydration continues to require `ChannelMessage.Read.Group`; do not add tenant-wide `ChannelMessage.Read.All` as a workaround.
8. **Preserve fail-closed thread hydration.** A Graph authentication or RSC failure must not invoke the agent with partial context.
9. **Do not change conversation routing or session keys.** Graph identity is authorization metadata, not a Marlow conversation identity.
10. **Make the identity split observable.** Runtime status and Graph failure logs must state whether the adapter is in shared-identity or separate-identity mode without logging secrets or access tokens.

---

## 3. Baseline and Observed Failure

### 3.1 Current code path

The current adapter reads one identity and one secret:

```python
self._client_id = config.extra["client_id"]
self._tenant_id = config.extra["tenant_id"]
self._secret = config.extra.get("client_secret") or os.getenv("TEAMS_CLIENT_SECRET")
```

`_build_sdk_app()` passes that credential set into the Teams SDK:

```python
self._teams_app = App(
    client_id=self._client_id,
    client_secret=self._secret,
    tenant_id=self._tenant_id,
    ...
)
```

`_graph_client()` then obtains a Graph client or Graph token from the same SDK `App`:

```python
graph = self._teams_app.get_app_graph(self._tenant_id)
```

or:

```python
token=lambda: self._teams_app._get_graph_token(self._tenant_id)
```

As a result:

```text
Bot client ID == Graph client ID
```

is currently an implementation constraint, even though the Teams application installation can associate Bot behavior and RSC with different Entra application IDs.

### 3.2 Runtime evidence

The inbound Bot path succeeds:

```text
POST /api/messages -> 200
```

The subsequent Graph request fails:

```text
GET /teams/{team}/channels/{channel}/messages/{root} -> 403
```

Graph reports:

```text
Missing role permissions on the request.
API requires one of 'ChannelMessage.Read.All, ChannelMessage.Read.Group'.
Roles on the request ''.
Resource specific consent grants on the request ''.
```

The target Team's `/permissionGrants` result shows application RSC grants for:

```text
clientAppId = 314c1e02-c0e6-4e39-b45b-7e7371b8965f
permission  = ChannelMessage.Read.Group
```

Marlow is configured with:

```text
teams.client_id = 1dc08c95-26a1-4cda-9efa-f9329e6a7b58
```

Therefore the actual request is equivalent to:

```text
Team grant:       314... -> ChannelMessage.Read.Group
Graph caller:     1dc...
Matched RSC:      none
Result:           403 Forbidden
```

### 3.3 Why the Bot path still works

Bot Framework and Microsoft Graph are independent authorization paths:

```text
Teams client
    |
    +--> Bot Framework --> /api/messages --> Marlow
    |
    +--> Microsoft Graph --> Team messages and replies
```

Successful Bot Framework authentication proves that the Bot identity is correct for messaging. It does not prove that the same identity has an RSC grant for Graph.

---

## 4. Problem Statement

Marlow currently conflates three distinct identifiers:

```text
Teams app package ID
Bot Framework Entra application ID
Microsoft Graph/RSC Entra application ID
```

The top-level Teams manifest `id` is already treated separately, but the Bot and Graph Entra identities are assumed to be the same. This causes thread hydration to fail whenever:

```text
bots[].botId != webApplicationInfo.id
```

or whenever the Team's effective RSC grant belongs to a different Entra application than the Bot credential configured in Marlow.

The current failure is difficult to diagnose because:

- Bot ingress remains healthy;
- thread locator parsing is correct;
- Graph token acquisition succeeds;
- the request reaches the correct Team/channel/message endpoint; and
- only Graph's resource-level authorization check fails.

The gateway needs an explicit identity model that represents the platform contract instead of inferring Graph identity from Bot identity.

---

## 5. Goals

### 5.1 Functional goals

- Allow Bot Framework messaging and Microsoft Graph thread hydration to use different Entra application registrations.
- Preserve the existing single-identity configuration as the default.
- Use the correct Graph identity for root-message and reply retrieval.
- Keep Bot replies on the existing Bot Framework path.
- Fail configuration validation when a separate Graph identity lacks its own secret.
- Provide actionable diagnostics for identity mismatch, token failure, and missing RSC grants.
- Keep all existing Teams conversation routing and authorization behavior unchanged.

### 5.2 Security goals

- Keep `ChannelMessage.Read.Group` scoped to Teams where the app is installed and consented.
- Do not request `ChannelMessage.Read.All` to avoid solving an identity mismatch with tenant-wide access.
- Never log access tokens, client secrets, authorization headers, or MSAL token responses containing credentials.
- Ensure the Graph secret is never used by the Bot Framework SDK when the IDs differ.
- Ensure the Bot secret is never used to request a token for a different Graph client ID.
- Require both identities to use the configured tenant in the first release.

### 5.3 Compatibility goals

- Existing deployments using one application ID and one secret must continue to work without configuration changes.
- Existing session keys, receipt keys, locks, allowlists, conversation references, and outbound routing must not change.
- `thread_context.enabled: false` must not require Graph-specific configuration.

### 5.4 Quality goals

- Unit tests must prove credential separation, not merely Graph response normalization.
- Misconfiguration must fail at startup where it can be determined locally.
- Runtime permission failures must remain fail-closed and user-visible.
- The implementation must avoid a second HTTP listener or a second initialized Teams `App` instance.

---

## 6. Non-Goals

This amendment does not add:

- tenant-wide Graph permissions;
- delegated user authentication;
- cross-tenant Graph access;
- certificate, federated identity, or managed identity support for the new Graph credential in the first PR;
- Graph-based outbound message sending;
- group-chat history hydration;
- private/shared channel support;
- proactive notification delivery;
- automatic creation or modification of Entra app registrations;
- automatic installation of the Teams app into a Team;
- automatic grant or revocation of RSC permissions;
- storage of Graph access tokens;
- a second Teams Bot Framework application process; or
- changes to Marlow's global multi-platform concurrency model.

---

## 7. Terminology

### 7.1 Teams app package ID

The top-level Teams manifest `id`, for example:

```text
0f9d72b5-0ac6-4492-a3c1-9260c1538159
```

It identifies the Teams application package. It is not used as an OAuth client ID.

### 7.2 Bot identity

The Entra application used by Azure Bot/Bot Framework messaging:

```text
manifest bots[].botId
== teams.client_id
```

It authenticates inbound activities and outbound Bot Framework operations.

### 7.3 Graph identity

The Entra application used to request app-only Microsoft Graph tokens:

```text
manifest webApplicationInfo.id
== effective teams.graph_client_id
== permissionGrants[].clientAppId
```

It receives Team-scoped RSC grants such as `ChannelMessage.Read.Group`.

### 7.4 Shared-identity mode

The Bot and Graph client IDs are equal. Existing configurations operate in this mode.

### 7.5 Separate-identity mode

The Bot and Graph client IDs differ. Each identity has a separate client secret.

---

## 8. Hard Invariants

The following invariants are normative.

### 8.1 Bot identity isolation

All of the following must use the Bot identity only:

- Teams SDK `App` construction;
- inbound JWT audience validation;
- Bot Framework token acquisition;
- mention-recipient matching;
- outbound message, typing, image, and Adaptive Card operations;
- credential lock ownership;
- receipt-key construction; and
- Marlow conversation/session route construction.

### 8.2 Graph identity isolation

All Microsoft Graph calls used by thread hydration must use the effective Graph identity only.

```text
graph_client_id != bot_client_id
    => Graph token cannot be obtained with bot_client_secret
```

### 8.3 RSC alignment

For a supported Team installation:

```text
manifest.webApplicationInfo.id
    == effective_graph_client_id
    == permission_grant.clientAppId
```

If those values do not align, the adapter must treat the resulting `403` as a configuration/consent failure and must not invoke the agent.

### 8.4 Same-tenant invariant

Both application identities must acquire tokens from:

```text
teams.tenant_id
```

A separate `graph_tenant_id` is not supported in this release.

### 8.5 No silent cross-ID secret fallback

Secret resolution may fall back from `TEAMS_GRAPH_CLIENT_SECRET` to `TEAMS_CLIENT_SECRET` only when the effective Graph and Bot client IDs are equal.

### 8.6 Conversation identity stability

Changing `graph_client_id` must not change:

- `chat_id`;
- `thread_id`;
- session keys;
- Teams receipt keys;
- approval callback routes; or
- outbound conversation references.

### 8.7 Complete-or-no-agent

The parent design's complete-thread invariant remains unchanged:

```text
Graph authentication or authorization failure
    => no ExternalConversationSnapshot
    => no agent invocation for that channel mention
```

### 8.8 Least privilege

The selected design uses:

```text
ChannelMessage.Read.Group
```

It does not require:

```text
ChannelMessage.Read.All
ChannelMessage.Send.Group
Group.Read.All
Group.ReadWrite.All
```

for standard-channel thread hydration.

---

## 9. Target Architecture

### 9.1 Application mapping

```text
Teams App Package
id = <TEAMS_APP_ID>
|
+-- bots[].botId
|       |
|       +--> Bot Entra App
|              client_id     = teams.client_id
|              client_secret = TEAMS_CLIENT_SECRET
|
+-- webApplicationInfo.id
        |
        +--> Graph Entra App
               client_id     = teams.graph_client_id or teams.client_id
               client_secret = TEAMS_GRAPH_CLIENT_SECRET
                               or TEAMS_CLIENT_SECRET in shared mode
               RSC           = ChannelMessage.Read.Group
```

### 9.2 Runtime flow

```text
User explicitly @mentions Marlow
              |
              v
Microsoft Bot Framework
              |
      Bot identity token path
              |
              v
POST /api/messages
              |
              v
Teams SDK validates activity using Bot client ID
              |
              v
Marlow validates tenant, sender, mention, and route
              |
              v
Authorized Teams event enrichment
              |
              v
Independent Graph credential provider
      Graph client ID + Graph secret
              |
              v
Microsoft Graph app-only token
scope = https://graph.microsoft.com/.default
              |
              v
GET root + all replies
              |
              v
ExternalConversationSnapshot
              |
              v
Agent
              |
              v
Bot Framework reply using Bot identity
```

### 9.3 Ownership boundaries

**Teams SDK `App` owns:**

- Bot Framework authentication;
- messaging endpoint processing;
- Bot API token acquisition; and
- outbound activity sending.

**New Graph credential provider owns:**

- Graph client-credential token acquisition;
- token caching through MSAL;
- Graph HTTP authorization headers; and
- independent Graph credential lifecycle.

**Teams adapter owns:**

- selecting the effective Graph identity;
- constructing the Graph client;
- invoking Graph only after authorization;
- normalizing Graph responses; and
- translating Graph failures into Marlow behavior.

---

## 10. Configuration Contract

### 10.1 Proposed configuration

```yaml
teams:
  enabled: true

  # Bot Framework / Azure Bot identity.
  # Must match manifest bots[].botId.
  client_id: "11111111-1111-4111-8111-111111111111"

  # Optional Graph/RSC identity.
  # Defaults to teams.client_id.
  # Must match manifest webApplicationInfo.id and the Team's grant clientAppId.
  graph_client_id: "22222222-2222-4222-8222-222222222222"

  # Shared tenant for both identities.
  tenant_id: "33333333-3333-4333-8333-333333333333"

  host: "127.0.0.1"
  port: 3978

  thread_context:
    enabled: true
    require_complete: true
```

Secrets remain outside YAML:

```dotenv
TEAMS_CLIENT_SECRET=<secret belonging to teams.client_id>
TEAMS_GRAPH_CLIENT_SECRET=<secret belonging to teams.graph_client_id>
```

### 10.2 Resolution matrix

| Configuration | Effective Graph ID | Effective Graph secret | Result |
|---|---|---|---|
| `graph_client_id` absent | `client_id` | `TEAMS_GRAPH_CLIENT_SECRET` if set, otherwise `TEAMS_CLIENT_SECRET` | Shared identity |
| `graph_client_id == client_id` | `client_id` | `TEAMS_GRAPH_CLIENT_SECRET` if set, otherwise `TEAMS_CLIENT_SECRET` | Shared identity with optional separate secret rotation |
| `graph_client_id != client_id` and Graph secret set | `graph_client_id` | `TEAMS_GRAPH_CLIENT_SECRET` | Separate identity |
| `graph_client_id != client_id` and Graph secret absent | none | none | Configuration error when thread context is enabled |

### 10.3 Validation

When `teams.enabled` is true:

- `teams.client_id` remains required and must be a UUID;
- `teams.tenant_id` remains required and must be a UUID;
- `TEAMS_CLIENT_SECRET` remains required;
- when present, `teams.graph_client_id` must be a UUID;
- when `thread_context.enabled` is true and the effective Graph ID differs from the Bot ID, `TEAMS_GRAPH_CLIENT_SECRET` is required;
- when `thread_context.enabled` is true, the resolved Graph secret must be nonempty;
- a Graph secret must never be copied from the Bot secret when the IDs differ.

When `thread_context.enabled` is false:

- no Graph secret is required;
- malformed explicitly supplied `graph_client_id` still fails validation; and
- the Graph credential provider is not initialized.

### 10.4 Current deployment example

Based on the observed Team grant:

```yaml
teams:
  client_id: "1dc08c95-26a1-4cda-9efa-f9329e6a7b58"
  graph_client_id: "314c1e02-c0e6-4e39-b45b-7e7371b8965f"
  tenant_id: "<EDGENESIS_TENANT_ID>"
  thread_context:
    enabled: true
    require_complete: true
```

```dotenv
TEAMS_CLIENT_SECRET=<secret for 1dc08c95...>
TEAMS_GRAPH_CLIENT_SECRET=<secret for 314c1e02...>
```

---

## 11. Manifest Contract

The manifest must represent the same split explicitly:

```json
{
  "bots": [
    {
      "botId": "<BOT_CLIENT_ID>",
      "scopes": ["personal", "team", "groupChat"],
      "isNotificationOnly": false
    }
  ],
  "webApplicationInfo": {
    "id": "<GRAPH_CLIENT_ID>",
    "resource": "https://RscBasedStoreApp"
  },
  "authorization": {
    "permissions": {
      "resourceSpecific": [
        {
          "name": "ChannelMessage.Read.Group",
          "type": "Application"
        }
      ]
    }
  }
}
```

The following relationships replace the parent design's single-identity equality rule:

```text
manifest.bots[].botId
    == teams.client_id

manifest.webApplicationInfo.id
    == effective teams.graph_client_id
    == Team permissionGrants[].clientAppId
```

The manifest's top-level `id` remains independent.

The package should request only permissions backed by implemented product behavior. Calling, video, files, message send through Graph, and member-read permissions should be removed unless separate approved features require them.

---

## 12. Detailed Component Design

### 12.1 Internal naming

Rename the current ambiguous internal fields:

```text
_client_id  -> _bot_client_id
_secret     -> _bot_secret
```

Add:

```text
_graph_client_id
_graph_secret
_graph_identity_mode  # "shared" or "separate"
_graph_token_provider
_graph_http_client
```

The external configuration key `teams.client_id` is not renamed in this release.

### 12.2 Credential resolution

Centralize credential resolution in one method rather than scattering fallback logic:

```python
@dataclass(frozen=True, slots=True)
class TeamsIdentityPlan:
    tenant_id: str
    bot_client_id: str
    bot_secret: str
    graph_client_id: str
    graph_secret: str
    graph_identity_mode: str
```

Conceptual resolver:

```python
def _resolve_identity_plan(config: PlatformConfig) -> TeamsIdentityPlan:
    bot_id = normalize(config.extra.get("client_id"))
    bot_secret = read_bot_secret()

    graph_id = normalize(config.extra.get("graph_client_id")) or bot_id
    explicit_graph_secret = read_graph_secret()

    if graph_id == bot_id:
        graph_secret = explicit_graph_secret or bot_secret
        mode = "shared"
    else:
        graph_secret = explicit_graph_secret
        mode = "separate"

    return TeamsIdentityPlan(...)
```

Validation must run after resolution.

### 12.3 Bot SDK construction

`_build_sdk_app()` must use only Bot fields:

```python
self._teams_app = App(
    client_id=self._bot_client_id,
    client_secret=self._bot_secret,
    tenant_id=self._tenant_id,
    http_server_adapter=self._teams_adapter,
    messaging_endpoint="/api/messages",
)
```

No Graph configuration is passed into this `App`.

### 12.4 Independent Graph token provider

Add a narrow token provider local to the Teams plugin. The implementation should use MSAL client credentials and an in-memory token cache:

```python
class TeamsGraphTokenProvider:
    def __init__(self, *, client_id: str, client_secret: str, tenant_id: str):
        self._client = ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            result = await asyncio.to_thread(
                self._client.acquire_token_for_client,
                scopes=["https://graph.microsoft.com/.default"],
            )

        token = result.get("access_token")
        if not token:
            raise TeamsGraphAuthenticationError(...)
        return token
```

MSAL performs token caching. The local lock prevents concurrent first-use token requests from producing a thundering herd.

Because Marlow imports MSAL directly after this change, add an exact MSAL pin to the `teams` optional dependency and regenerate `uv.lock`.

### 12.5 Independent Graph HTTP client

Do not construct a second Teams SDK `App`. Use a narrow asynchronous Graph client wrapper around the existing `httpx` dependency:

```python
class TeamsGraphClient:
    def __init__(self, token_provider: TeamsGraphTokenProvider):
        self._token_provider = token_provider
        self._http = httpx.AsyncClient(
            base_url="https://graph.microsoft.com/v1.0",
            timeout=GRAPH_CONTEXT_TIMEOUT_SECONDS,
        )

    async def get(self, url: str) -> httpx.Response:
        token = await self._token_provider.get_token()
        response = await self._http.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response
```

The wrapper must support both relative Graph paths and absolute `@odata.nextLink` URLs.

The existing `_graph_request_json()` normalization and error translation remain responsible for decoding responses.

### 12.6 Lifecycle

During `connect()`:

1. validate the resolved identity plan;
2. build the Teams SDK `App` with Bot credentials;
3. if thread context is enabled, build the independent Graph token provider and HTTP client;
4. do not require a live Graph token acquisition to mark the Bot listener connected.

During `_cleanup_partial()` and `disconnect()`:

- close the Graph HTTP client;
- clear the Graph token provider reference;
- clear the Team AAD group cache; and
- stop the Bot SDK as today.

### 12.7 Existing routing fields

All existing uses of `_client_id` for routing must become `_bot_client_id`, including:

- `_conversation_key()`;
- `_canonical_activity_key()`;
- `_recipient_matches_configured_bot()`;
- `_strip_bot_mentions()`;
- `chat_id` construction;
- credential-lock keys; and
- status/account metadata.

`_graph_client_id` must never enter those calculations.

---

## 13. Runtime Flows

### 13.1 Shared identity

```text
teams.client_id = A
teams.graph_client_id absent

Bot token     -> A
Graph token   -> A
RSC grant     -> A
```

This preserves existing behavior.

### 13.2 Separate identity

```text
teams.client_id       = A
teams.graph_client_id = B

Bot token     -> A
Graph token   -> B
RSC grant     -> B
```

The inbound message and outbound reply continue through A. Only thread retrieval uses B.

### 13.3 RSC mismatch

```text
Configured Graph identity = A
Team grant clientAppId     = B
```

Expected behavior:

1. Graph returns `403`;
2. the adapter logs a sanitized permission failure with identity fingerprints and Microsoft request ID;
3. the event enrichment fails;
4. no agent is invoked; and
5. the user receives the existing thread-consent failure message.

Marlow must not retry a non-transient `403`.

---

## 14. Failure Handling

| Failure | Detection point | Behavior |
|---|---|---|
| Invalid Bot client ID | startup validation | Adapter fails to connect |
| Missing Bot secret | startup validation | Adapter fails to connect |
| Invalid Graph client ID | startup validation | Adapter fails to connect when supplied |
| Separate Graph ID without Graph secret | startup validation | Adapter fails to connect when thread context is enabled |
| Invalid Graph secret | first token acquisition | Channel turn fails closed; Bot listener remains connected |
| Wrong Graph tenant | token acquisition or Graph request | Channel turn fails closed |
| Missing/mismatched RSC grant | Graph `403` | No agent invocation; actionable log and user message |
| Graph throttling | Graph `429` | Existing bounded retry using `Retry-After` |
| Graph service error | `5xx` | Existing bounded retry |
| Graph client unavailable | local state | No agent invocation |
| Thread context disabled | configuration | Graph path is not initialized or called |

A Graph failure must not disconnect the entire Teams adapter because personal and group-chat Bot messaging may still be healthy.

---

## 15. Observability

### 15.1 Startup/status fields

Expose:

```text
teams_graph_identity_mode = shared | separate
teams_graph_configured    = true | false
```

Use irreversible fingerprints rather than raw IDs in ordinary logs:

```text
bot_client_id_fingerprint
 graph_client_id_fingerprint
 tenant_id_fingerprint
```

### 15.2 Metrics

Add or extend:

```text
teams_graph_token_requests_total{result}
teams_graph_requests_total{operation,status,result,identity_mode}
teams_graph_request_duration_seconds{operation}
teams_thread_context_failures_total{reason}
```

Suggested failure reasons:

```text
graph_authentication_failed
rsc_missing_or_mismatched
not_found
throttled
service_error
malformed_response
```

### 15.3 Error logging

For a Graph error, log only:

- operation;
- status code;
- Microsoft `request-id` and `client-request-id`;
- sanitized Graph error `code` and `message`;
- identity mode; and
- hashed Team/channel/client identifiers.

Do not log:

- access tokens;
- `Authorization` headers;
- client secrets;
- MSAL token responses;
- Graph message bodies; or
- full thread content.

### 15.4 Diagnostic guidance

When Graph returns a permission failure, operator documentation must state:

```text
manifest bots[].botId             must match teams.client_id
manifest webApplicationInfo.id    must match effective graph_client_id
Team permissionGrants.clientAppId must match effective graph_client_id
```

The `/permissionGrants` result is the runtime source of truth for which Entra application received the RSC grant.

---

## 16. Security and Privacy Analysis

### 16.1 Reduced blast radius

Separate identities allow the Graph/RSC application to have no Bot Framework responsibility and the Bot application to have no channel-history permission.

```text
Bot compromise
    -> Bot messaging capability
    -> no Graph thread-read grant unless identities are shared

Graph credential compromise
    -> RSC-scoped Team data access
    -> no Bot Framework identity unless identities are shared
```

### 16.2 Secret handling

- Both secrets remain in the profile secret environment storage.
- Secrets are independently rotatable.
- The setup flow must not echo either secret.
- A secret for one client ID must not be accepted as an implicit secret for another client ID.

### 16.3 Permission scope

The Graph identity should not be granted tenant-wide `ChannelMessage.Read.All`. Its intended authorization is the Team-scoped RSC grant declared in the Teams manifest.

### 16.4 Authorization-before-read

This amendment does not change the parent design's requirement that Marlow authorize the triggering actor before retrieving thread history.

### 16.5 Historical content

Graph-fetched thread messages remain untrusted historical context, not authenticated active instructions. Separating identities does not change prompt-injection handling.

---

## 17. Compatibility and Migration

### 17.1 Existing shared-identity deployments

No configuration change is required:

```yaml
teams:
  client_id: "A"
  tenant_id: "T"
```

```dotenv
TEAMS_CLIENT_SECRET=<secret for A>
```

Effective result:

```text
graph_client_id = A
graph_secret    = TEAMS_CLIENT_SECRET
```

### 17.2 Existing separate-identity Teams apps

Add:

```yaml
teams:
  graph_client_id: "<webApplicationInfo.id>"
```

and:

```dotenv
TEAMS_GRAPH_CLIENT_SECRET=<secret for graph_client_id>
```

No session or database migration is required.

### 17.3 Current Edgenesis deployment

The observed RSC grant is attached to `314c1e02-...`. The recommended rollout is:

1. confirm that the `314c1e02-...` App Registration belongs to the expected tenant and is operator-controlled;
2. create or rotate a client secret for that registration;
3. configure `teams.graph_client_id=314c1e02-...`;
4. store the matching secret in `TEAMS_GRAPH_CLIENT_SECRET`;
5. ensure `webApplicationInfo.id` remains `314c1e02-...` for the package installed in the target Team;
6. retain `bots[].botId=1dc08c95-...` and `teams.client_id=1dc08c95-...`; and
7. run a live thread-hydration smoke test.

If the `314c1e02-...` registration is not owned or cannot receive a client secret, create a controlled Graph App Registration, update `webApplicationInfo.id`, reinstall the package, and verify that `/permissionGrants` reports the new client ID before changing Marlow.

### 17.4 Rollback

The lowest-risk rollback is:

```yaml
teams:
  thread_context:
    enabled: false
```

Bot messaging continues without Graph hydration.

A second rollback is to return to shared identity after the Team RSC grant has been reissued to the Bot client ID.

---

## 18. Setup and Documentation Changes

Update `_setup_teams()` to ask:

```text
Use a separate Entra application for Microsoft Graph/RSC? [y/N]
```

If no:

- preserve the existing prompts;
- do not ask for a second client ID or secret.

If yes:

- prompt for Graph application client ID;
- prompt for Graph client secret;
- store only `TEAMS_GRAPH_CLIENT_SECRET` in the secret file; and
- instruct the operator that the Graph client ID must match `webApplicationInfo.id` and the Team's `permissionGrants.clientAppId`.

Update:

- `plugins/platforms/teams/README.md`;
- `docs/msteams-integration.md`;
- `docs/microsoft-teams-gateway-full-thread-context-engineering-design.md`;
- `plugins/platforms/teams/plugin.yaml`; and
- any sample configuration.

The parent design sections “Graph credential” and “Manifest identity requirements” must be explicitly superseded by this amendment.

---

## 19. Test Strategy

### 19.1 Configuration tests

Add tests for:

- omitted `graph_client_id` uses Bot ID;
- equal IDs use Bot secret when Graph secret is absent;
- equal IDs prefer explicit Graph secret when present;
- different IDs require explicit Graph secret when thread context is enabled;
- malformed Graph ID is rejected;
- thread context disabled does not require Graph secret;
- `_is_connected()` continues to require only Bot connectivity; and
- setup/plugin environment metadata exposes the optional Graph secret.

### 19.2 Identity-routing tests

Prove that:

- `App(...)` receives Bot client ID and Bot secret;
- Graph token acquisition receives Graph client ID and Graph secret;
- Graph ID is never used for mention matching;
- Graph ID is never included in `chat_id` or `thread_id`;
- receipt keys remain stable when only Graph ID changes; and
- outbound `activity_sender` remains associated with the Bot SDK application.

### 19.3 Graph client tests

Mock MSAL and HTTP responses to verify:

- scope is exactly `https://graph.microsoft.com/.default`;
- authority uses the configured tenant;
- concurrent token requests are coalesced;
- absolute `@odata.nextLink` values work;
- `401` and `403` are not retried;
- `429` respects `Retry-After`;
- `5xx` follows bounded retry behavior; and
- tokens and secrets do not appear in captured logs.

### 19.4 Existing thread-context regression tests

Retain all existing tests for:

- standard-channel validation;
- root-ID parsing;
- pagination;
- trigger reconciliation;
- duplicate/conflicting Graph message IDs;
- edited/deleted messages;
- attachments;
- exact route identity; and
- complete-or-no-agent behavior.

Replace test helpers that inject `self._teams_app.get_app_graph()` with injection of the independent Graph client.

### 19.5 Live integration tests

Run two live scenarios.

**Shared identity:**

```text
bots[].botId = A
webApplicationInfo.id = A
Marlow bot ID = A
Marlow graph ID omitted
Team RSC grant clientAppId = A
```

Expected: root and replies return `200`.

**Separate identity:**

```text
bots[].botId = A
webApplicationInfo.id = B
Marlow bot ID = A
Marlow graph ID = B
Team RSC grant clientAppId = B
```

Expected:

- `/api/messages` succeeds under A;
- Graph root and replies succeed under B; and
- the reply is posted through Bot Framework under A.

### 19.6 Negative live test

Configure Graph ID A while the Team grant belongs to B.

Expected:

- Graph returns `403`;
- logs show separate/shared mode and sanitized identity fingerprints;
- no agent turn occurs; and
- no partial answer is posted.

---

## 20. Acceptance Criteria

This design is complete when all of the following are true:

1. Existing one-identity Teams configurations pass without modification.
2. A separate `graph_client_id` can be configured without affecting inbound Bot authentication.
3. `TEAMS_GRAPH_CLIENT_SECRET` is required only when necessary.
4. The Teams SDK `App` uses Bot credentials exclusively.
5. Graph thread hydration uses Graph credentials exclusively.
6. A live Team whose RSC grant belongs to the Graph identity returns `200` for root and replies.
7. Bot replies continue to arrive in the same Teams thread.
8. Changing only the Graph identity does not change Marlow session keys.
9. Missing or mismatched RSC still prevents agent invocation.
10. No tenant-wide message-read permission is required.
11. No secret or access token appears in logs, runtime status, or errors.
12. The full Teams adapter test suite passes.
13. Operator documentation explains the three IDs separately: Teams package ID, Bot client ID, and Graph client ID.

---

## 21. Detailed Pull Request Plan

### PR 1 — Identity model and configuration

Files:

```text
plugins/platforms/teams/adapter.py
plugins/platforms/teams/plugin.yaml
plugins/platforms/teams/README.md
docs/msteams-integration.md
tests/gateway/test_teams_adapter.py
```

Changes:

- rename internal Bot fields for clarity;
- add `graph_client_id` resolution;
- add conditional Graph secret resolution;
- add validation and tests;
- add optional `TEAMS_GRAPH_CLIENT_SECRET`; and
- preserve all current runtime behavior by default.

This PR does not yet change the Graph caller.

### PR 2 — Independent Graph credential and client

Files:

```text
plugins/platforms/teams/adapter.py
pyproject.toml
uv.lock
tests/gateway/test_teams_adapter.py
```

Changes:

- add local MSAL-backed Graph token provider;
- add independent async Graph HTTP client;
- remove Graph dependency on `self._teams_app.get_app_graph()` and private `_get_graph_token()`;
- wire thread hydration to the independent client;
- add lifecycle cleanup; and
- add identity-separation and security tests.

### PR 3 — Manifest contract, diagnostics, and live validation

Files:

```text
docs/microsoft-teams-gateway-full-thread-context-engineering-design.md
docs/msteams-integration.md
plugins/platforms/teams/README.md
appPackage/manifest.template.json, if added
```

Changes:

- supersede the single-identity invariant in the parent design;
- document shared and separate manifest shapes;
- add safe Graph error diagnostics and metrics;
- remove unsupported manifest permissions from the recommended package;
- document `/permissionGrants` validation; and
- run shared-identity and separate-identity live smoke tests.

---

## 22. Alternatives Considered

### 22.1 Force one identity everywhere

Change `webApplicationInfo.id` to the Bot client ID, reinstall the application, and require the Team grant to move to that identity.

**Rejected as the only supported architecture.** It can work, but it makes Marlow depend on one manifest topology and does not accommodate existing installations where RSC is intentionally assigned to a separate app registration. Supporting an optional Graph ID is small and removes this hidden platform assumption.

### 22.2 Use `ChannelMessage.Read.All`

Grant the Bot identity tenant-wide Graph application permission.

**Rejected.** It expands access from selected installed Teams to all channel messages in the tenant and hides the actual identity/RSC mismatch.

### 22.3 Construct a second Teams SDK `App`

Create one SDK `App` for Bot messaging and another SDK `App` only to obtain Graph tokens.

**Rejected.** It introduces a second application container, HTTP/server-related state, token validator, storage, and lifecycle for a task that only needs client-credential token acquisition.

### 22.4 Continue calling private SDK `_get_graph_token()`

Mutate or duplicate SDK internals to use another credential.

**Rejected.** The private method is bound to the SDK `App`'s credentials. Continuing to rely on it preserves the identity coupling and increases SDK upgrade risk.

### 22.5 Implement OAuth token acquisition manually with raw HTTP

Call the Entra token endpoint directly and implement caching and expiry handling locally.

**Not selected.** It avoids MSAL but creates unnecessary authentication and cache code. A narrow MSAL wrapper is safer and easier to test.

### 22.6 Disable thread context permanently

Continue using only mention activities and Marlow-local session history.

**Rejected.** It restores availability but reintroduces the original incomplete-thread-context problem.

---

## 23. Risks and Mitigations

### 23.1 Operators confuse Bot and Graph IDs

**Mitigation:** use explicit config labels, setup prompts, runtime identity mode, and a three-way validation table in documentation.

### 23.2 Graph App Registration is not operator-controlled

**Mitigation:** require confirmation that the application exists in the configured tenant and has a managed secret. Otherwise provision a new controlled identity and reissue Team grants.

### 23.3 Secret rotation breaks only one path

**Mitigation:** expose Bot and Graph health independently and document independent rotation procedures.

### 23.4 Shared identity regression

**Mitigation:** shared identity remains the default resolution path and receives dedicated regression and live smoke tests.

### 23.5 SDK/MSAL version drift

**Mitigation:** exact-pin direct dependencies, regenerate `uv.lock`, and isolate MSAL usage behind one local provider.

### 23.6 Graph token acquisition blocks the default executor

MSAL client-credential acquisition uses synchronous code under `asyncio.to_thread`.

**Mitigation:** MSAL caches tokens, the provider coalesces concurrent refreshes, and token acquisition occurs far less frequently than Graph message retrieval. Track token request latency separately. A future async credential implementation can replace the provider without changing the adapter contract.

### 23.7 RSC still missing after identity separation

**Mitigation:** treat `/permissionGrants` as the source of truth. Log the effective Graph identity fingerprint and Microsoft request ID. Do not infer grant state from Developer Portal manifest checkboxes alone.

### 23.8 Extra manifest permissions obscure consent

**Mitigation:** recommend a minimal RSC set containing only implemented capabilities, starting with `ChannelMessage.Read.Group` for this feature.

---

## 24. Open Questions

1. Should `graph_client_id` remain flat, or should a future major version introduce a nested `teams.graph` block?
2. Should the first implementation use MSAL directly or a public credential abstraction exposed by the pinned Teams SDK if one becomes stable?
3. Should invalid Graph credentials mark the whole Teams adapter `degraded` while Bot messaging remains connected?
4. Should Marlow add a `gateway doctor teams` command that obtains a Graph token and optionally checks a supplied Team's `/permissionGrants`?
5. Should application IDs be fully shown in a protected diagnostic command while ordinary logs show only fingerprints?
6. Should certificate and managed identity support be added in a follow-up design?
7. Should the repository begin versioning a canonical Teams manifest template to prevent Developer Portal/package drift?

None of these questions block the core identity-separation change.

---

## 25. Final Recommendation

Implement separate logical credentials for Bot Framework and Microsoft Graph while retaining shared identity as the backward-compatible default.

For the currently observed deployment, configure:

```text
Bot identity   = 1dc08c95-26a1-4cda-9efa-f9329e6a7b58
Graph identity = 314c1e02-c0e6-4e39-b45b-7e7371b8965f
```

The installed Team has already shown `ChannelMessage.Read.Group` under the Graph identity. Marlow should therefore obtain its Graph token with the Graph application's own secret while continuing to receive and send Bot Framework activities with the Bot application.

This resolves the present `403`, preserves least privilege, avoids tenant-wide Graph permissions, maintains existing conversation identity, and supports both valid Teams deployment models:

```text
shared Bot/Graph identity
or
separate Bot and Graph identities
```

---

## Appendix A — Required Parent Design Amendments

Replace the parent design statement:

```text
Use an app-only Microsoft Graph client backed by the same Entra application identity configured for the Teams bot.
```

with:

```text
Use an app-only Microsoft Graph client backed by the effective Graph identity. The Graph identity defaults to the Bot identity for backward compatibility but may be configured separately when the Teams application's RSC grant is associated with webApplicationInfo.id rather than bots[].botId.
```

Replace the parent identity equality rule:

```text
bots[].botId
    == webApplicationInfo.id
    == backend CLIENT_ID
```

with:

```text
bots[].botId
    == teams.client_id

webApplicationInfo.id
    == effective teams.graph_client_id
    == permissionGrants[].clientAppId
```

---

## Appendix B — Minimal Current Manifest Shape

```json
{
  "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.30/MicrosoftTeams.schema.json",
  "manifestVersion": "1.30",
  "version": "<incremented-version>",
  "id": "<TEAMS_APP_PACKAGE_ID>",
  "bots": [
    {
      "botId": "<BOT_CLIENT_ID>",
      "scopes": ["personal", "team", "groupChat"],
      "isNotificationOnly": false
    }
  ],
  "webApplicationInfo": {
    "id": "<GRAPH_CLIENT_ID>",
    "resource": "https://RscBasedStoreApp"
  },
  "authorization": {
    "permissions": {
      "resourceSpecific": [
        {
          "name": "ChannelMessage.Read.Group",
          "type": "Application"
        }
      ]
    }
  },
  "supportsChannelFeatures": "tier1"
}
```

---

## Appendix C — Reviewed Platform Contracts

- Microsoft Teams RSC overview:  
  https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/rsc/resource-specific-consent
- Grant RSC permissions to an app:  
  https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/rsc/grant-resource-specific-consent
- Receive/read channel messages with RSC:  
  https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/channel-messages-for-bots-and-agents
- Channel and group-chat Bot behavior:  
  https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/channel-and-group-conversations
- Microsoft Teams Python SDK:  
  https://github.com/microsoft/teams.py

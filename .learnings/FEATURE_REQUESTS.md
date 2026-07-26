# Feature Requests

## [FEAT-20260719-001] execution-bound-admin-action-intents

**Logged**: 2026-07-19T00:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Requested Capability

Administrator approval should authorize semantic actions such as database
mutations or device commands, rather than only high-risk shell commands.

### User Context

The administrator needs to review the intended external effect and target
before an agent changes a database or controls a device. Command-only prompts
expose implementation details and do not cover non-terminal tools.

### Complexity Estimate

complex

### Suggested Implementation

Add structured action-intent metadata to tool registration, enforce it in
central dispatch, bind each one-shot approval to the exact tool arguments, and
map explicit MCP mutation annotations into the same policy.

### Metadata

- Frequency: first_time
- Related Features: administrator-routed approvals, tool registry, MCP tools

### Resolution

- **Resolved**: 2026-07-19T00:00:00+08:00
- **Notes**: Added central execution-bound action intents, plugin registry
  metadata, MCP mutation annotation mapping, semantic admin cards, and focused
  fail-closed tests.

---

## [FEAT-20260720-001] telegram-work-experience-recall

**Logged**: 2026-07-20T08:55:02+08:00
**Priority**: medium
**Status**: resolved
**Area**: backend

### Requested Capability

Support Work Experience recall for turns received through Telegram.

### User Context

The profile owner wants Telegram tasks to benefit from the same approved,
project-scoped lessons available in the classic CLI.

### Complexity Estimate

medium

### Suggested Implementation

Add an explicit Telegram turn origin, preserve the untouched inbound request,
bind direct-message recall to one configured owner user ID, and retain all
existing project and provider-egress checks.

### Metadata

- Frequency: first_time
- Related Features: Work Experience, Telegram gateway, provider egress

### Resolution

- **Resolved**: 2026-07-20T09:22:00+08:00
- **Notes**: Added owner-bound Telegram DM recall with raw-input separation,
  retained project/provider authorization, documented configuration, and
  completed the full gateway and focused Work Experience test suites.

---

## [FEAT-20260722-001] super-admin-gateway-conversation

**Logged**: 2026-07-22T13:33:02Z
**Priority**: high
**Status**: resolved
**Area**: backend

### Requested Capability

Allow one exactly configured gateway administrator conversation to bypass ordinary message and slash-command restrictions and auto-approve approvable actions.

### User Context

The operator needs a trusted admin channel or DM where instructions may be challenged with warnings but are not refused solely because of risk.

### Complexity Estimate

complex

### Suggested Implementation

Reuse the exact `approvals.admin` identity and destination, add an explicit `super_admin` conversation mode, propagate per-turn authority through context variables, and retain non-approvable safety invariants.

### Metadata

- Frequency: first_time
- Related Features: administrator-routed approvals, gateway slash access, dangerous command approvals

### Resolution

- **Resolved**: 2026-07-22T13:50:00Z
- **Notes**: Added exact-source super-admin conversation mode, automatic
  approvable-action authorization, command access, behavioral context,
  background-task propagation, local-only configuration, and focused tests.

---

## [FEAT-20260724-001] telegram-guest-digital-twin

**Logged**: 2026-07-24T09:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Requested Capability

Make every non-super-admin Telegram source a guest-facing digital twin that
combines dedicated system prompts with the administrator's applicable Work
Experience.

### User Context

Guests should receive the administrator's established working knowledge and
style without gaining personal memory, private session history, secrets, or
administrator permissions.

### Complexity Estimate

complex

### Suggested Implementation

Classify authorized Telegram sources against the exact configured super-admin
conversation, compose a global guest prompt with existing channel prompts, add
an explicit guest Work Experience origin with independent runtime
authorization, and strip personal-memory surfaces from guest agents.

### Metadata

- Frequency: first_time
- Related Features: administrator-routed approvals, Telegram channel prompts,
  Work Experience

### Resolution

- **Resolved**: 2026-07-24T09:12:00+08:00
- **Notes**: Added Telegram guest classification, prompt composition, scoped
  experience recall, memory/session-search isolation, documentation, and
  focused regression coverage.

---

## [FEAT-20260726-001] multi-command-intent-approval

**Logged**: 2026-07-26T00:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Requested Capability

Allow one administrator decision to authorize all terminal commands needed for
an approved intent without prompting separately for each command.

### User Context

Multi-step tasks become disruptive when every dangerous-command finding asks
the administrator to approve the same intended outcome again.

### Complexity Estimate

complex

### Suggested Implementation

Use a bounded, request-scoped intent grant that contains an administrator-
reviewed set of exact command digests. Keep the grant session-bound, expiring,
single-use per command, and subject to hardline safety checks. Do not treat a
reused purpose string as authorization for arbitrary future commands.

### Metadata

- Frequency: first_time
- Related Features: administrator-routed approvals, terminal action intents,
  dangerous command approvals

### Resolution

- **Resolved**: 2026-07-26T00:00:00+08:00
- **Notes**: Added administrator-reviewed exact command plans with opaque,
  session-bound, 15-minute grants; atomic one-use command consumption; hardline
  enforcement; intent-first Telegram, Slack, Feishu, and Discord rendering; and
  focused security, concurrency, expiry, schema, and adapter coverage.

---

# Phase 2: Codex-only runtime

## 1. Background and context

Approved by the task request (Marlow Phase 2 — Codex-only Runtime
Consolidation). Native inference and optional app-server execution currently
have different tools, credentials, configuration, history and approval behavior.
The approved change makes Codex own inference, tool reasoning and compaction;
Marlow continues to own business state, authorization and orchestration.

Baseline: origin/main 37d58cdb (clean cloud checkout). Phase 1 is 84df86ea;
37d58cdb adds on-demand shipped skills. The user's local worktree is not mounted.
The user selected remote main as the implementation baseline.

## 2. Goals and non-goals

Goals: one app-server adapter for all AI entry points; durable per-profile
Codex/HOME/workspace isolation; native Codex model configuration and login;
scoped business tools, memory, history and approvals; attributable bounded
execution; actual deletion of the native inference implementation.

Non-goals: redesigning memory, channels or work state; reintroducing Phase 1
features; a new SDK, provider proxy or inference engine; claiming configuration
isolation is an operating-system security sandbox.

## 3. Proposed design

Marlow entry points -> AIAgent compatibility facade -> Codex app-server session
-> Codex-configured provider. Keep the existing synchronous JSON-RPC transport
and event projector. A centralized environment builder owns persistent profile
paths and enforced configuration overrides. Model configuration remains in
that profile's CODEX_HOME/config.toml; credentials use Codex's file backend.

Business tool dispatch remains in the trusted host process, with immutable
profile/session/actor/turn context. The Codex MCP bridge cannot grant authority
from model-supplied arguments. Model-native Codex environment tools are disabled
with `environments=[]` on thread/start and every turn/start. The sole Marlow
execution boundary performs policy/approval checks, then uses that same
app-server’s `command/exec` and named sandbox. Marlow also owns messaging,
memory, experience and task operations. Durable thread
mapping is scoped to profile and conversation. Interrupted/ambiguous operations
are recorded for verification; Marlow does not replay turns automatically.

## 4. Alternatives considered

Keeping Native as fallback was rejected by the approved direction: it doubles
execution and security paths. Replacing the existing Python integration with
another SDK adds maintenance and cannot solve business authorization. Reusing
the user's Codex home fails profile isolation and independent authentication.

## 5. Risks and open questions

Existing app-server mode bypasses parts of Memory/Experience. Its bridge is
stateless and omits them. Native child tools must not bypass business policy.
Codex system/organization configuration must be detected and reported rather
than bypassed. Protocol/configuration fields must be checked against generated
schema and real no-inference smoke tests. macOS and paid provider E2E remain
unverified unless executed in this task.

## 6. Impact

Existing history and profile files remain intact. Legacy runtime/provider
settings need actionable migration diagnostics. No automatic credential import.
Codex CLI becomes required for inference. No task retry after an uncertain write;
Codex handles provider request retries. Independent sessions can run concurrently;
one conversation is serialized. No periodic AI heartbeat or reflection is added.

## 7. Success criteria

The request's acceptance groups A–K apply. Report unit/protocol tests separately
from actual Codex smoke tests; do not represent mocks as real model validation.
Release contents must exclude native inference clients/loops. Tests must use
temporary homes and never send business messages or paid inference by default.

## Tracked migration checklist

| Current capability / location | New owner | Verification |
| --- | --- | --- |
| AIAgent in run_agent.py; init/loop in agent/ | Thin facade + one session adapter | Entry-point and no-native-path checks |
| app-server transport/session/projector | Reuse with bounded lifecycle | Real generated schema, protocol fault tests |
| query.py, oneshot.py, SDK, batch_runner.py | Same facade | CLI/SDK tests; stdout purity |
| gateway/run.py; cron/scheduler.py; webhook AI | Same facade; scripts remain direct | Routing, idle and scheduled-task tests |
| auxiliary_client.py, plugin_llm.py, browser/goal/title helpers | Adapter or deterministic operations | No direct model clients remaining |
| marlow_tools_mcp_server.py; model_tools.py | Trusted scoped business bridge | Spoofing/cross-session/approval tests |
| MemoryStore, MemoryManager, Honcho | Marlow; recall injected per turn | Data/scope and completed-turn sync |
| experience/runtime.py, experience_decision_tool.py | Existing scope/egress/authority checks | Experience and decision regressions |
| native session DB and Codex thread state | Preserve DB; durable scoped mapping | Restart and concurrent-profile tests |
| personal HOME/CODEX_HOME/config/keychain | Persistent isolated profile runtime | Marker files, discovery and process evidence |
| model/provider/fallback configuration | Native profile Codex TOML | Valid custom provider and explicit errors |
| native loop/compressor/provider modules | Delete after migration | Import scan and built-wheel inspection |

Implemented in the working tree. Current validation and remaining limits are
recorded in [phase-2-validation.md](phase-2-validation.md).

### Verified protocol and ownership decision

The executable in this environment reports `codex-cli 0.159.2`; npm publishes
`@openai/codex@0.159.2`. Generated JSON schemas and no-paid-call experiments
were checked before selecting RPC fields. This release rejects other versions.
The actual binary rejects the schema's old `approval_policy=untrusted` value;
Marlow uses `on-request`. Experimental capabilities are enabled to select the
named `marlow` permissions profile.

PreToolUse hooks fail open on several failures, so they cannot enforce mandatory
business checks. Environment-native exec/apply_patch would bypass those checks.
Therefore, tool ownership is a single Marlow MCP execution boundary backed by
the same server's `command/exec`, not a second model client or native loop.
The sole native MCP server auto-delegates its tool approvals to this host
boundary; it never bypasses the host's operation-bound business approvals.
Other native MCP servers, plugins, hooks, skills and environment tools fail
closed or remain disabled.

Default remote conversations use stable private workspace subdirectories;
configured shared threads share a conversation workspace while retaining the
current actor for each business operation. A project allowlist is narrowed to
the explicitly selected task project. Delegates inherit that narrowed workspace
and grants through host-only arguments, with independent task leases.
Cron execution scope is captured separately from delivery origin. Native/local
history is preserved; old dialogue is imported as labelled reference text only.
Stateless inputs have separate threads but stable ownership and cancellation.

Host business extensions are trusted code, not an OS isolation mechanism.
Shell/file execution uses the Codex sandbox; host browser navigation and media
upload additionally enforce file-disclosure boundaries. Upload snapshots use
nofollow descriptors and a protected outbox. No agent-created script Cron or
cross-profile Cron may bypass that boundary.

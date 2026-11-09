# Approved Selective Marlow Cleanup

Historical record of the cleanup approved on 2026-07-18. The later Phase 1 scope reduction supersedes its retention decisions for the retired components.

The user-defined convention is authoritative: checked items are removed;
unchecked items remain supported.

## Intent declaration

### WHY — motivation and problem

Marlow contains many complete optional products and integrations. The earlier
cleanup incorrectly treated all optional systems as disposable and removed
foundational gateway, messaging, and MCP behavior. The user instead wants a
purpose-built Marlow distribution: keep the working core and the integrations
they use, while removing specifically selected providers, connectors, hosted
services, UIs, deployment targets, backends, and maintenance material.

This matters because deleting a whole subsystem based only on size breaks
supported workflows, while leaving every unused integration retains unwanted
dependencies, configuration, tests, and maintenance cost. The checked
inventory provides the missing product decision for each feature family.

### WHAT — scope and outcomes

Restore the complete current `origin/main` baseline first. Then remove the 123
checked items from the approved checklist and preserve the 123 unchecked
items. Removal includes the feature's exclusive implementation, registration,
configuration, dependencies, tests, documentation, assets, and packaging.

Approved removal IDs:

- interfaces: `U3-U8`;
- connectors: `C4-C14`, `C16-C22`, `C24-C25`, `C29-C32`;
- scheduling: `B3`;
- extension features: `X15-X17`;
- model providers: `P1`, `P3`, `P5-P31`;
- execution/tools: `T6-T9`, `T15-T19`;
- web/browser: `W4-W9`, `W11`, `W13-W14`;
- media: `V2`, `V4-V6`, `V8-V11`;
- memory providers: `M5-M6`, `M9-M12`;
- skill packs: `S3-S4`, `S7-S8`, `S11`, `S14-S15`;
- Nous-hosted components: `N1-N8`;
- operations/deployment: `O4-O5`, `O11`, `O14-O17`, `O19`;
- maintenance material: `D5-D9`.

Everything not listed above is explicitly out of removal scope and must remain
working. In particular, gateway foundations, Telegram, Discord, Slack,
Feishu/Lark chat, email, webhooks, cron, goals, delegation, MCP, the local
plugin and skill runtimes, Codex, custom/local model endpoints, local/SSH/
Docker execution, core browser automation, computer use, image generation via
Codex, voice, Holographic and Honcho memory, and retained administration and
deployment surfaces remain supported.

Expected outcome: a smaller Marlow tailored to the approved feature boundary,
without the accidental CLI-only product rewrite.

### HOW — approach and constraints

Use Git history to restore the original implementations rather than rebuilding
them. Reverse the selective Codex follow-up and the broad minimal-core cleanup,
then integrate current `origin/main` so the newer gateway administrator
approval work remains present.

Apply removals in dependency-coherent groups. A checked feature removes its
exclusive behavior and integration points. Shared infrastructure remains when
an unchecked feature needs it. After each group, search registries, manifests,
configuration, lazy-dependency tables, package metadata, docs, and tests for
stale references.

Dependency reconciliation rules:

- `P1` removes first-party OpenAI API support, but the generic OpenAI-wire
  transport remains because retained custom/local endpoints (`P4`) require it.
- `U3` and `U4` remove dashboard/API cron pages and routes; retained cron
  management (`B2`) continues through CLI, tools, and gateway scheduling.
- `U5` removes ACP as a product surface; ACP-only MCP passthrough code has no
  standalone runtime after that removal, while the general MCP system remains.
- `C32` removes connector-specific public HTTP ingress. Retained Feishu chat
  keeps its WebSocket transport, while its webhook mode is removed.
- STT/TTS backends remain even when the similarly named model provider is
  removed; audio integrations are independent retained capabilities.
- self-update remains supported without the removed user-facing backup and
  snapshot systems.
- current configuration migration remains, while checked legacy compatibility
  aliases and retired auth/provider migrations are removed.
- release/publishing remains, while checked contributor/release audit helpers
  are removed.

The accepted trade-off is deliberate feature loss only where selected, plus
some retained shared code that is necessary for unchecked capabilities.

## Engineering design document

### 1. Background & context

The cleanup branch currently contains a broad minimal-core conversion and a
selective Codex restoration. The pull-request base has advanced to
`origin/main` at `68df3f7ae`, which includes gateway administrator approval
routing. The current branch therefore cannot be used as the implementation
baseline; it must first be restored and brought up to the current base.

Marlow relies on dynamic provider, platform, plugin, skill, and MCP discovery.
Static imports alone do not define ownership. The approved checklist defines
the product boundary, and registry/manifest/config consumers define the
implementation boundary for each removal.

### 2. Goals and non-goals

Goals:

- restore all unchecked capabilities from the current working baseline;
- remove every checked capability and its exclusive maintenance surface;
- preserve shared substrate required by retained capabilities;
- remove stale config, dependencies, tests, docs, and registrations;
- keep Git history auditable and avoid reimplementing restored features;
- validate retained gateway, messaging, MCP, Codex, cron, and agent behavior.

Non-goals:

- restoring features marked checked;
- removing unchecked optional features merely because they are disabled;
- replacing dynamic systems that remain in scope with fixed substitutes;
- force-rewriting shared branch history;
- claiming dead-code cleanup for shared code still required at runtime.

### 3. Proposed design

The final system retains four main boundaries:

1. The core agent, sessions, safety, context, models, and local tools.
2. The gateway with retained connectors and automation.
3. MCP, bundled plugins, and local skill execution without the removed remote
   marketplace/provenance/bundle layers.
4. Retained operator surfaces: CLI/TUI, profiles, setup, update, diagnostics,
   Docker, logging, and release tooling.

Each removed family is deleted from its source tree and from all discovery
paths. Shared registries are narrowed rather than removed. Default/sample
configuration documents only retained choices. Tests follow the retained
behavior boundary: checked-feature tests are removed, cross-cutting tests are
adapted, and unchecked-feature coverage remains.

### 4. Alternatives considered

Keeping the minimal branch and adding back unchecked features was rejected
because it would reimplement thousands of coupled files and repeat the earlier
scope error.

Restoring all of `origin/main` without selective cleanup was rejected because
it ignores the user's checked removal decisions.

Deleting every module related to a checked label without dependency analysis
was rejected because overlapping features such as custom endpoints, audio
providers, cron, MCP, and Feishu share implementation with retained behavior.

### 5. Risks and open questions

- The approved boundary crosses large dynamic registries. Every manifest and
  lazy-loading path must be audited after deletion.
- Some tests cover both removed and retained behavior and require careful
  pruning rather than whole-file deletion.
- Removing Windows/WSL/Termux while retaining installers requires narrowing
  documentation and platform branches without breaking Linux/macOS.
- Removing backup/snapshots changes update recovery behavior and must be made
  explicit rather than leaving broken imports.
- Full upstream validation may include environment-dependent integration tests;
  deterministic failures must be distinguished from unavailable credentials
  or external services.

### 6. Impact

The repository returns to the full working architecture before becoming
smaller along the approved boundary. Web/dashboard/API/ACP/LSP/proxy surfaces,
most messaging connectors and model providers, cloud execution/browser/search
backends, selected media and memory plugins, all Nous-hosted integration, and
selected platform/maintenance support are removed.

Gateway core, retained messaging, MCP, cron, skills, memory, Codex, custom/local
models, core tools, voice, CLI/TUI, profiles, Docker, diagnostics, and release
flows remain. Install size and dependency count should fall without changing
those retained capabilities.

### 7. Success criteria

- current `origin/main` is integrated before selective removal;
- every checked ID has no remaining user-facing registration or exclusive
  runtime/config/dependency surface;
- every unchecked feature remains represented by code and its necessary
  registry/config/package integration;
- gateway, Telegram, Discord, Slack, Feishu, email, webhook, cron, MCP, Codex,
  custom/local endpoints, retained tools, and both retained memory providers
  pass applicable tests;
- config, package metadata, lazy dependencies, docs, and tests contain no stale
  checked-feature choices;
- the complete applicable test suite, lint, compile/import checks, and package
  build pass, with external-environment limitations reported separately;
- the final diff receives an independent full review for accidental retained
  feature loss;
- the branch is committed, pushed, and the pull request accurately describes
  the approved selective cleanup.

## Task tracking

- [ ] Restore current working baseline.
- [ ] Remove checked feature families.
- [ ] Clean cross-cutting config/dependencies/tests/docs.
- [ ] Review retained-feature boundaries.
- [ ] Run complete validation and package audit.
- [ ] Commit, push, and revise the pull request.

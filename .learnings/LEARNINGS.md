# Learnings

## [LRN-20260718-004] correction

**Logged**: 2026-07-18T21:30:00+08:00
**Priority**: high
**Status**: resolved
**Area**: infra

### Summary

A project rename must include a working upgrade bridge from legacy launchers
and data directories, not only replacement branding and repository URLs.

### Details

The identity audit changed active Marlow surfaces but did not exercise an
existing installation launched through
`~/.hermes/hermes-agent/venv/bin/hermes`. Its `/update` flow still displayed
Hermes branding and executed an orphaned console script whose import of
`marlow_cli` failed. A clean installer path alone is insufficient because
existing users reach the upgrade through the old executable and need their
configuration and state carried into `~/.marlow`.

### Suggested Action

Add a non-destructive installer migration that copies legacy state while
excluding the old source checkout, retire known legacy launchers after the new
launcher is installed, and validate updater executables before spawning them.
Cover the legacy launcher and data-home transition in regression tests.

### Metadata

- Source: user_feedback
- Related Files: scripts/install.sh, gateway/run.py, marlow_cli/relaunch.py
- Tags: rename, migration, updater, launcher, hermes

### Resolution

- **Resolved**: 2026-07-18T22:00:00+08:00
- **Notes**: Added a non-destructive installer state migration, retired only
  positively identified legacy launchers, made gateway updates prefer the
  current source script, and added regression coverage for all three paths.

---

## [LRN-20260717-001] best_practice

**Logged**: 2026-07-17T00:00:00+08:00
**Priority**: medium
**Status**: promoted
**Area**: backend

### Summary

Validate fallible runtime configuration before opening persistent resources.

### Details

The cleanup's first implementation opened the session database before model
and API-key validation. If agent construction failed, Python never entered the
CLI context manager and the connection remained open. A full-diff lifecycle
review caught this even though behavior tests passed.

### Suggested Action

Construct stateless validated dependencies first. Wrap later resource creation
and selection in cleanup-on-failure logic.

### Metadata

- Source: error
- Related Files: cli.py
- Tags: lifecycle, sqlite, review

**Promoted**: AGENTS.md engineering workflow requires lifecycle and full-diff review.

---

## [LRN-20260717-002] knowledge_gap

**Logged**: 2026-07-17T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary

Modern setuptools expects `project.license` to be an SPDX string.

### Details

The first clean build succeeded but warned that the older TOML table form,
`license = { text = "MIT" }`, will stop being supported after 2027-02-18.

### Suggested Action

Use `license = "MIT"` and declare `license-files = ["LICENSE"]`.

### Metadata

- Source: error
- Related Files: pyproject.toml
- Tags: packaging, setuptools, deprecation

### Resolution

- **Resolved**: 2026-07-17T00:00:00+08:00
- **Notes**: Updated the project metadata before final package validation.

---

## [LRN-20260718-001] correction

**Logged**: 2026-07-18T06:19:39+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary

Interpret “keep existing support” as selectively reverting that capability,
not designing a replacement or reverting the containing cleanup commit.

### Details

After the cleanup removed Codex-specific support, the user asked to keep it.
The initial response proposed a new minimal Codex integration and architecture
document, then interpreted “revert” as potentially reverting the entire
monolithic cleanup commit. The user clarified that the intended change is to
restore only the pre-cleanup Codex-specific changes from Git history, without
reimplementing Codex or undoing the rest of the cleanup.

### Suggested Action

Before proposing replacement architecture or a whole-commit revert for a
recently deleted capability, check whether the user wants only that
capability's original files and integration points restored. Prefer the
narrowest scoped Git restoration when that is the stated intent.

### Metadata

- Source: user_feedback
- Related Files: agent/codex_runtime.py, agent/codex_responses_adapter.py
- Tags: codex, cleanup, revert, scope

### Resolution

- **Resolved**: 2026-07-18T08:00:00+08:00
- **Notes**: Restored the dedicated pre-cleanup Codex components and their
  minimal package dependencies while leaving cron, skills, media, MCP/plugin
  migration, memory hooks, connectors, and unrelated provider branches removed.

---

## [LRN-20260718-002] correction

**Logged**: 2026-07-18T07:28:19+08:00
**Priority**: critical
**Status**: in_progress
**Area**: backend

### Summary

“Clean unused parts” does not authorize removing working Marlow product
families merely because they are optional or make the repository larger.

### Details

The cleanup treated gateways, messaging connectors, MCP, scheduling, memory,
skills, plugins, and other working subsystems as removable product scope. The
user clarified that these are basic Marlow capabilities and must remain. The
intended cleanup is evidence-based dead-code and stale-config removal, not a
conversion of Marlow into a different CLI-only product.

### Suggested Action

Restore the exact pre-cleanup functional baseline before attempting further
cleanup. Preserve every working feature family by default. Remove an item only
when its lack of runtime, dynamic-registry, manifest, compatibility, docs, and
test responsibility is demonstrated and recorded in the cleanup review.

### Metadata

- Source: user_feedback
- Related Files: gateway/, marlow_cli/, plugins/, tests/
- Tags: cleanup, scope, gateway, connectors, mcp, preservation
- See Also: LRN-20260718-001

---

## [LRN-20260718-003] correction

**Logged**: 2026-07-18T00:25:24Z
**Priority**: critical
**Status**: in_progress
**Area**: backend

### Summary

For the approved Marlow feature checklist, checked means remove and unchecked
means keep.

### Details

The user supplied the complete feature inventory and explicitly defined its
decision convention. Checkbox state is the authoritative product boundary;
earlier inferred family-level keep/remove choices must not override it.

### Suggested Action

Implement only the checked IDs, preserve every unchecked ID, and audit shared
code against retained consumers before deleting it.

### Metadata

- Source: user_feedback
- Related Files: .plans/approved-feature-cleanup.md
- Tags: cleanup, checklist, scope, preservation
- See Also: LRN-20260718-002

---

## [LRN-20260719-001] correction

**Logged**: 2026-07-19T08:57:28Z
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary

Distinguish admin routing from the semantic class of an approval.

### Details

The title `Action Approval Required` and exact-user routing prove that an
approval traveled through the configured administrator channel, but do not by
themselves prove that the underlying operation is semantically an
administrator-level intent. A routine terminal command can be promoted to a
structured action intent by central enforcement, creating an admin-routed
decision whose content still represents an ordinary command.

This recurred on 2026-07-25 when a presentation-only fix made the command more
prominent instead of correcting the missing semantic intention at the terminal
tool boundary.

### Suggested Action

When explaining approval behavior, separately identify (1) who is authorized
to decide, (2) what semantic object is being approved, and (3) why that object
was escalated. Treat raw structured-intent rendering as a separate UX concern.
Require terminal requests to carry a user-facing intended outcome before an
administrator approval can be created; bind that intention and the exact tool
arguments in the same request, and render the command only as supporting
evidence.

### Metadata

- Source: user_feedback
- Related Files: tools/registry.py, tools/action_intent.py, tools/approval.py, gateway/run.py
- Tags: approvals, admin-routing, action-intent, semantics, ux

### Resolution

- **Resolved**: 2026-07-25T00:00:00+08:00
- **Notes**: Terminal calls now declare a request-bound purpose; admin-routed
  warnings fail closed without one, and Telegram renders the semantic intention
  before the supporting redacted command.

---

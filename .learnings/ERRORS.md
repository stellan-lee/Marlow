# Errors

## [ERR-20260722-002] skill-usage-test-timestamp-exhaustion

**Logged**: 2026-07-22T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

A strengthened skill-usage test exhausted its mocked timestamp iterator while
the report builder backfilled a record.

### Error

```
StopIteration in tools.skill_usage._empty_record()
```

### Context

- The test added a separate `bump_use()` call to model one real `skill_view`
  load, which increments both view and use counters.
- The iterator covered the four mutations but not the report builder's
  defensive `_empty_record()` call.

### Suggested Fix

When mocking shared time helpers with iterators, include reads performed by
normalization/backfill code as well as explicit mutations.

### Metadata

- Reproducible: yes
- Related Files: tests/tools/test_skill_usage.py, tools/skill_usage.py

### Resolution

- **Resolved**: 2026-07-22T00:00:00+08:00
- **Notes**: Added the fifth timestamp and retained the stronger de-duplication assertion.

---

## [ERR-20260722-001] validation-python-command-recurrence

**Logged**: 2026-07-22T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary

A validation command again assumed a bare `python` executable was available.

### Error

```
zsh:1: command not found: python
```

### Context

- The syntax probe invoked `python -m py_compile` directly.
- This checkout exposes `python3`; the project test wrapper remains preferred.

### Suggested Fix

Default standalone probes to `python3` and use `scripts/run_tests.sh` for tests.

### Metadata

- Reproducible: yes
- Related Files: scripts/run_tests.sh
- See Also: ERR-20260720-001, ERR-20260717-002

### Resolution

- **Resolved**: 2026-07-22T00:00:00+08:00
- **Notes**: Switched subsequent syntax validation to `python3` and the repository test wrapper.

---

## [ERR-20260719-002] marlow-tools-selector-keyword

**Logged**: 2026-07-19T17:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary

The `marlow tools` command passed an unsupported keyword to the shared curses selector.

### Error

```
TypeError: curses_single_select() got an unexpected keyword argument 'default'
```

### Context

- Command attempted: `marlow tools`
- `tools_command()` called `curses_single_select(..., default=0)`.
- The selector contract names that parameter `default_index`.

### Suggested Fix

Use the selector's `default_index` keyword and cover the interactive entry path with a regression test.

### Metadata

- Reproducible: yes
- Related Files: marlow_cli/tools_config.py, tests/marlow_cli/test_tools_config.py

### Resolution

- **Resolved**: 2026-07-19T17:00:00+08:00
- **Notes**: Replaced the invalid `default` keyword with `default_index` and added a regression test for the interactive tools entry path. Focused selector and tools tests pass.

---

## [ERR-20260720-001] validation-python-command-recurrence

**Logged**: 2026-07-20T08:55:02+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

A direct validation command again assumed a `python` executable was available.

### Error

```
zsh:1: command not found: python
```

### Context

- A syntax-only validation command invoked `python -m py_compile` directly.
- This worktree exposes `/opt/homebrew/bin/python3` and no `python` alias.

### Suggested Fix

Use the project test wrapper where possible and `python3` for standalone probes.

### Metadata

- Reproducible: yes
- Related Files: scripts/run_tests.sh
- See Also: ERR-20260717-002

### Resolution

- **Resolved**: 2026-07-20T08:55:02+08:00
- **Notes**: Switched the standalone syntax probe to `python3` and used the
  main checkout's shared virtualenv for pytest.

---

## [ERR-20260720-002] telegram-recall-raw-message-scope

**Logged**: 2026-07-20T09:12:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary

The raw Telegram request was captured in the outer handler but referenced from
a separate inner-handler method without being passed across the boundary.

### Error

```
NameError: name '_raw_experience_user_message' is not defined
```

### Context

- The focused runtime and `_run_agent` tests passed.
- The complete gateway suite exercised the real outer-to-inner dispatch path
  and exposed the missing parameter in 13 tests.

### Suggested Fix

Carry the raw request on the transient typed `MessageEvent`, preserving the
inner handler's established call contract, then pass it to `_run_agent` and
assert propagation in gateway regression tests.

### Metadata

- Reproducible: yes
- Related Files: gateway/run.py, tests/gateway/test_status_command.py

### Resolution

- **Resolved**: 2026-07-20T09:12:00+08:00
- **Notes**: Added `MessageEvent.raw_user_message`, preserved it across plugin
  rewrites, and completed the full gateway suite successfully.

---

## [ERR-20260718-029] missing-repository-rule-files

**Logged**: 2026-07-18T21:30:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: docs

### Summary

The root AGENTS.md references three repository rule files that are absent from
this worktree.

### Error

```
sed: rules/workflow.md: No such file or directory
sed: rules/agent-usage.md: No such file or directory
sed: rules/task-tracking.md: No such file or directory
```

### Resolution

Used the complete root AGENTS.md instructions already supplied for this
worktree and continued without inventing missing rule content.

### Metadata

- Reproducible: yes
- Related Files: AGENTS.md

---

## [ERR-20260718-028] cmd-update-test-process-termination

**Logged**: 2026-07-18T20:43:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary

The broader `test_cmd_update.py` run terminated its parent shell after 14 tests, before pytest or the shell could emit an exit status.

### Error

```
..............
```

No pytest summary or subsequent shell exit marker was emitted.

### Resolution

Kept the completed focused update/identity suite as the validation signal and isolated the broader file rather than reporting an ambiguous run as passing.

### Metadata

- Reproducible: yes
- Related Files: tests/marlow_cli/test_cmd_update.py

---

## [ERR-20260718-027] generic-skill-validator-incompatible

**Logged**: 2026-07-18T20:35:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary

The generic Codex skill validator could not validate this Marlow skill: the first runtime lacked PyYAML, and the project test runtime then rejected Marlow-supported `author`, `platforms`, and `version` fields.

### Error

```
ModuleNotFoundError: No module named 'yaml'

Unexpected key(s) in SKILL.md frontmatter: author, platforms, version.
```

### Resolution

Kept the project-required metadata and validated the skill through Marlow's own loader instead of weakening its schema for an incompatible generic validator.

### Metadata

- Reproducible: yes
- Related Files: skills/autonomous-ai-agents/marlow-agent/SKILL.md

---

## [ERR-20260718-026] documentation-cleanup-patch-context

**Logged**: 2026-07-18T20:20:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: tooling

### Summary

A combined documentation cleanup patch and its first retry missed the exact AGENTS.md companion-plugin paragraph, so both were rejected atomically.

### Error

```
apply_patch verification failed: Failed to find expected lines in AGENTS.md
```

### Resolution

Inspected the exact source wording, including the leading `plug into` text, and applied one-line replacements before the paragraph cleanup.

### Metadata

- Reproducible: yes
- Related Files: AGENTS.md, marlow_cli/setup.py, marlow_cli/main.py

---

## [ERR-20260718-025] mixed-quote-legacy-search

**Logged**: 2026-07-18T19:53:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary

A final compound legacy-path regex mixed shell quote characters and was rejected by zsh.

### Error

```
zsh: unmatched "
```

### Resolution

Replaced compound shell expressions with separate fixed-string Git searches.

### Metadata

- Reproducible: no
- Related Files: none
- See Also: ERR-20260718-021, ERR-20260718-024

---

## [ERR-20260718-024] url-inventory-shell-quoting

**Logged**: 2026-07-18T19:46:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary

A shell regex for extracting Marlow URLs contained an unmatched quote under zsh.

### Error

```
zsh: unmatched "
```

### Resolution

Moved URL extraction into a Python reader over Git-tracked files so shell quoting is no longer involved.

### Metadata

- Reproducible: no
- Related Files: none
- See Also: ERR-20260718-021

---

## [ERR-20260718-023] default-homebrew-python-killed

**Logged**: 2026-07-18T19:37:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary

The default Homebrew `python3` (3.14.6) was terminated with exit code 137 even for a one-line command, so the initial compile validation could not run.

### Error

```
compile_status=137
```

### Resolution

Verified the repository's uv-managed Python 3.11 runtime works and used that interpreter for targeted syntax validation.

### Metadata

- Reproducible: yes
- Related Files: marlow_cli/main.py, marlow_cli/banner.py, marlow_cli/config.py, scripts/install.sh

---

## [ERR-20260718-022] zsh-readonly-status-variable

**Logged**: 2026-07-18T19:35:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary

A validation wrapper assigned the command exit code to `status`, which is a read-only special parameter in zsh.

### Error

```
zsh: read-only variable: status
```

### Resolution

Reran the validation with a task-specific variable name.

### Metadata

- Reproducible: yes
- Related Files: none

---

## [ERR-20260718-021] legacy-audit-shell-regex-quoting

**Logged**: 2026-07-18T19:20:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary

A broad legacy-brand audit regex embedded a single quote inside a single-quoted shell argument, so zsh rejected the read-only scan.

### Error

```
zsh: parse error near `)'
```

### Resolution

Split the audit into multiple simple, independently quoted ripgrep searches.

### Metadata

- Reproducible: no
- Related Files: none

---

## [ERR-20260718-020] broad-legacy-config-patch-context

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary

A combined provider-alias and environment-sanitizer removal patch missed because the sanitizer's current implementation included extra null-byte handling beyond the inspected excerpt.

### Error

```
apply_patch verification failed: Failed to find expected _sanitize_env_lines block
```

### Resolution

Split provider normalizer edits from sanitizer removal and inspected exact function boundaries before retrying.

### Metadata

- Reproducible: no
- Related Files: marlow_cli/config.py, marlow_cli/env_loader.py

---

## [ERR-20260718-019] telegram-provider-group-orphan-block

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: implementation

### Summary

Removing the Telegram provider-group callback branch left its trailing keyboard-render block indented inside the preceding successful model-switch branch.

### Error

Static compilation passed, but review found references to deleted variables (`buttons`, `_label`, `group_id`) on the successful model-switch path.

### Resolution

Removed the complete orphaned group-keyboard fragment and recompiled the adapter.

### Metadata

- Reproducible: yes
- Related Files: gateway/platforms/telegram.py

---

## [ERR-20260718-018] platform-audit-shell-quoting

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary

A combined ripgrep regex used nested shell quotes that zsh interpreted as a glob, so the read-only platform audit did not run.

### Error

```
zsh: bad pattern
```

### Resolution

Replaced the combined shell regex with multiple explicitly quoted ripgrep patterns.

### Metadata

- Reproducible: no
- Related Files: none

---

## [ERR-20260718-017] doctor-platform-guard-indentation

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: implementation

### Summary

Removing the doctor command-installation platform guard left its body over-indented and caused an `IndentationError` during compilation.

### Error

```
IndentationError: unexpected indent (doctor.py, line 723)
```

### Resolution

Replaced the removed native-Windows exclusion with an explicit POSIX scope matching the supported macOS/Linux targets, preserving the existing block structure.

### Metadata

- Reproducible: no
- Related Files: marlow_cli/doctor.py

---

## [ERR-20260718-016] cli-windows-regex-patch-escaping

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary

A combined CLI cleanup patch failed because the expected Python regex string did not preserve the file's exact backslash escaping.

### Error

```
apply_patch verification failed: Failed to find expected Windows path regex block
```

### Resolution

Inspected the exact source and reapplied the regex removal separately from comment-only edits.

### Metadata

- Reproducible: no
- Related Files: cli.py

---

## [ERR-20260718-015] gateway-status-platform-patch-context

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary

A multi-section gateway status patch failed on one stale comment context.

### Error

```
apply_patch verification failed: Failed to find expected Windows normalization lines
```

### Resolution

Applied the import, lock, PID, and comment changes as independent patches and recompiled the module.

### Metadata

- Reproducible: no
- Related Files: gateway/status.py

---

## [ERR-20260718-014] oversized-main-cleanup-patch-context

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary

An oversized patch for Termux startup removal failed because its copied context omitted intervening comments.

### Error

```
apply_patch verification failed: Failed to find expected lines in marlow_cli/main.py
```

### Context

- The patch attempted to remove several distant helper groups in one operation.
- The source contained additional explanatory comments not included in the patch context.

### Resolution

Split the edit into smaller function-level patches and applied them successfully.

### Metadata

- Reproducible: no
- Related Files: marlow_cli/main.py

---

## [ERR-20260718-013] removed-termux-mode-affected-tui-height-test

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

A virtual-height test only distinguished prompt widths under the removed Termux sizing mode.

### Error

```
expected estimatedMsgHeight(..., 26, '❯') to be 3, received 4
```

### Context

- The production TUI still had a Termux-only sizing branch even though Android/Termux support is selected for removal.
- The test's narrow dimensions were clamped to the same desktop minimum width for both prompts.

### Suggested Fix

Remove the checked Termux-specific layout mode and exercise compound prompt sizing above the desktop minimum width.

### Metadata

- Reproducible: yes
- Related Files: ui-tui/src/config/env.ts, ui-tui/src/lib/virtualHeights.ts

### Resolution

Removed Termux-only layout handling and updated the width test to exercise the retained desktop sizing contract. All 913 TUI tests now pass (one skipped).

---

## [ERR-20260717-001] repository-workflow-discovery

**Logged**: 2026-07-17T00:00:00+08:00
**Priority**: medium
**Status**: wont_fix
**Area**: docs

### Summary

The supplied repository instructions reference three workflow rule files that
are not present in the checkout.

### Error

```
wc: rules/workflow.md: open: No such file or directory
wc: rules/agent-usage.md: open: No such file or directory
wc: rules/task-tracking.md: open: No such file or directory
```

### Context

- Attempted to read every rule referenced by the task's AGENTS.md instructions.
- The checked-in `AGENTS.md` does not contain those references and no matching
  files were found under the worktree.

### Suggested fix

Either add the referenced rule files to the repository or remove the stale
references from the external project instructions.

### Metadata

- Reproducible: yes
- Related Files: AGENTS.md

### Resolution

- **Resolved**: 2026-07-17T00:00:00+08:00
- **Notes**: The checked-in development guide is now self-contained. The stale
  references came from external task instructions and are not editable in this
  repository.

---

## [ERR-20260718-012] tui-test-preconditions

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

TUI tests were launched before building `@marlow/ink` and inherited SSH markers.

### Error

```
Cannot find module './dist/entry-exports.js'
Cursor terminal setup must be run on the local machine, not inside an SSH session.
```

### Context

- The TUI test suite imports the built local Ink workspace.
- Terminal-setup tests deliberately reject environments with `SSH_CONNECTION`.

### Suggested Fix

Build `@marlow/ink` first and remove ambient SSH markers only for the test process.

### Metadata

- Reproducible: yes
- Related Files: ui-tui/package.json, ui-tui/packages/marlow-ink/package.json

### Resolution

Built the local Ink workspace and ran the suite with SSH marker variables unset.

---

## [ERR-20260718-011] marlow-ink-node26-child-process-types

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary

Node 26 typings rejected a readonly conditional stdio tuple in `execFileNoThrow`.

### Error

```
Type 'readonly ["pipe", "ignore", "ignore"]' is not assignable to type 'StdioOptions'.
```

### Context

- The error appeared after installing locked TUI dependencies and running `tsc --noEmit`.
- The utility conditionally chooses piped or ignored output for spawned processes.

### Suggested Fix

Type the conditional as mutable `StdioOptions` and the result as `ChildProcess`.

### Metadata

- Reproducible: yes
- Related Files: ui-tui/packages/marlow-ink/src/utils/execFileNoThrow.ts

### Resolution

Typed stdio as mutable `StdioOptions`; the TUI TypeScript check now passes.

---

## [ERR-20260718-010] tui-typecheck-dependencies-missing

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

The TUI type-check could not start because workspace dependencies were absent.

### Error

```
sh: tsc: command not found
```

### Context

- `npm run type-check` was run in `ui-tui` after removing the Skills Hub overlay.
- The worktree did not contain `node_modules`.

### Suggested Fix

Install the repository's locked npm workspace dependencies and rerun type-check.

### Metadata

- Reproducible: yes
- Related Files: package-lock.json, ui-tui/package.json

### Resolution

Installed the locked npm workspace dependencies and reran type-check successfully.

---

## [ERR-20260718-005] delegation-heartbeat-timing-flake

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

The timing-sensitive delegation heartbeat test observed 6 callbacks where it requires more than 6.

### Resolution

The unchanged test passed immediately in isolation; no product code was altered for the scheduler jitter.

---

## [ERR-20260718-009] zsh-reserved-status-variable

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

A pytest collection wrapper assigned to zsh's read-only `status` parameter.

### Error

```
zsh:1: read-only variable: status
```

### Context

- The command captured pytest's exit code after redirecting collection output.
- The failure happened in shell control logic; pytest had already produced its output.

### Suggested Fix

Use a task-specific variable such as `pytest_exit_code` in zsh commands.

### Metadata

- Reproducible: yes
- Related Files: tests/
- See Also: ERR-20260718-008

### Resolution

- **Resolved**: 2026-07-18T00:00:00+08:00
- **Notes**: Renamed the shell variable before rerunning collection.

---

## [ERR-20260718-004] stale-focused-test-paths

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

A focused test command named two runtime-provider test files that no longer exist after checklist pruning.

### Error

```
ERROR: file or directory not found: tests/test_runtime_provider.py
```

### Resolution

Used `rg --files tests` to resolve the remaining transport test path before rerunning.

---

## [ERR-20260718-003] shifted-line-runtime-removal

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary

Sequential line-number deletions shifted `run_agent.py` and clipped retained activity, memory-shutdown, and LM Studio methods.

### Error

```
SyntaxError: '{' was never closed
```

### Context

- Three deletion ranges were calculated before earlier ranges changed line positions.
- A compile check caught the damage immediately.

### Resolution

- Restored retained blocks from the current commit using named function anchors.
- Subsequent mechanical edits must use semantic start/end markers, never mutable line numbers.

---

## [ERR-20260718-002] focused-moa-stale-test

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

The retained MoA test slice still referenced a heavy-skill guard file removed by the approved checklist.

### Error

```
FileNotFoundError: tools/skills_guard.py
```

### Context

- MoA now uses the retained active Codex/custom runtime.
- A shared content-normalization test also revealed two generic reasoning formats that should remain supported.

### Resolution

- Removed only the stale source-file assertion.
- Kept and repaired generic reasoning normalization for compatible endpoints.

---

## [ERR-20260718-007] create-goal-after-resume

**Logged**: 2026-07-18T08:01:12+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary

The goal service rejected a replacement goal after the user resumed a goal
that had previously been marked blocked.

### Error

```
cannot create a new goal because this thread has an unfinished goal;
complete the existing goal first
```

### Context

- The user explicitly asked to set the goal again with an approved checklist.
- The prior goal was blocked only while waiting for that product decision.
- Goal tooling exposes no manual resume operation and forbids falsely marking
  incomplete work complete.

### Suggested Fix

Continue the user-resumed work under the existing goal and use the approved
checklist as its authoritative scope.

### Metadata

- Reproducible: yes
- Related Files: .plans/approved-feature-cleanup.md

### Resolution

- **Resolved**: 2026-07-18T08:01:12+08:00
- **Notes**: Kept the existing goal unfinished and resumed implementation
  without misreporting its status.

---

## [ERR-20260718-008] zsh-special-path-variable

**Logged**: 2026-07-18T00:25:24Z
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

A shell audit loop used zsh's special `path` variable and temporarily broke
command lookup inside the loop.

### Error

```
The probe falsely reported every retained file as missing from HEAD and the
working tree.
```

### Context

- The loop assigned each filename to a variable named `path`.
- In zsh, `path` is tied to `PATH`, so the assignment removed Git and other
  commands from lookup for each iteration.

### Suggested Fix

Use a task-specific variable such as `audit_file` in zsh loops.

### Metadata

- Reproducible: yes
- Related Files: none
- See Also: ERR-20260717-002

### Resolution

- **Resolved**: 2026-07-18T00:25:24Z
- **Notes**: Re-ran the probe with a direct path check and will avoid zsh's
  special `path` variable in subsequent loops.

---

## [ERR-20260717-005] diff-whitespace-audit

**Logged**: 2026-07-17T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: docs

### Summary

The first complete diff check found redundant blank lines at end of file.

### Error

```
new blank line at EOF
```

### Context

- Ten newly replaced text files contained more than one trailing newline.
- Ruff does not report this Git whitespace condition.

### Suggested fix

Normalize changed text files to exactly one trailing newline and rerun
`git diff --check HEAD` independently.

### Metadata

- Reproducible: yes
- Related Files: README.md, AGENTS.md, pyproject.toml, configuration examples

### Resolution

- **Resolved**: 2026-07-17T00:00:00+08:00
- **Notes**: All text files were normalized and independent diff checks pass.

---

## [ERR-20260717-003] agent-request-snapshot

**Logged**: 2026-07-17T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: backend

### Summary

Model requests referenced the live conversation list and changed after the
request returned.

### Error

```
2 failed, 35 passed
Recorded request messages ended with later assistant messages instead of the
user/tool message present when create() was called.
```

### Context

- The agent appended new messages to the same list passed to the model client.
- A synchronous real client serializes immediately, but mocks or alternate
  clients can retain the object and observe later mutations.

### Suggested fix

Pass a deep snapshot of messages for each model request.

### Metadata

- Reproducible: yes
- Related Files: run_agent.py, tests/test_agent.py

### Resolution

- **Resolved**: 2026-07-17T00:00:00+08:00
- **Notes**: Each request now receives `copy.deepcopy(messages)`.

---

## [ERR-20260717-004] initial-ruff-check

**Logged**: 2026-07-17T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

The first Ruff pass reported import ordering, line length, annotation, and test
style issues in the new minimal source.

### Error

```
Found 13 errors.
```

### Context

- First static check after replacing the legacy repository.
- Nine findings are mechanically fixable; four require small formatting edits.

### Suggested fix

Apply Ruff's safe fixes, format the remaining long lines, and rerun from clean
output.

### Metadata

- Reproducible: yes
- Related Files: cli.py, marlow_constants.py, marlow_state.py, tests/, tools/

### Resolution

- **Resolved**: 2026-07-17T00:00:00+08:00
- **Notes**: Applied safe automatic fixes and formatted the four remaining findings.
  A later security-test expansion introduced one import-order recurrence; it
  was fixed immediately and included in the next complete lint run.

---

## [ERR-20260717-002] validation-python-command

**Logged**: 2026-07-17T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

The clean worktree has `python3` but no `python` executable.

### Error

```
zsh:1: command not found: python
```

### Context

- The validation probe and initial test-wrapper fallback used `python`.
- No project virtual environment exists in this worktree yet.

### Suggested fix

Use `python3` as the final no-virtualenv fallback.

### Metadata

- Reproducible: yes
- Related Files: scripts/run_tests.sh

### Resolution

- **Resolved**: 2026-07-17T00:00:00+08:00
- **Notes**: The wrapper now falls back to `python3`.

---
## [ERR-20260718-001] auth-cleanup-patch-context

**Logged**: 2026-07-18T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary

A multi-hunk auth cleanup patch used stale comment context after earlier edits.

### Error

```
apply_patch verification failed: Failed to find expected lines
```

### Context

- The patch combined unrelated documentation and code cleanup hunks.
- Earlier reductions shifted one comment block in `marlow_cli/auth.py`.

### Suggested Fix

Split broad cleanup patches into small hunks anchored to current file content.

### Metadata

- Reproducible: no
- Related Files: marlow_cli/auth.py

### Resolution

- **Resolved**: 2026-07-18T00:00:00+08:00
- **Notes**: Re-read the current file and continued with verified small hunks.

---
## [ERR-20260718-006] Combined stale fast-test patch used a mismatched context

**Logged**: 2026-07-18
**Severity**: low
**Status**: resolved

### Summary
A combined `apply_patch` for stale TUI fast-mode tests failed because one trailing `_load_service_tier` context belonged to a different test file.

### Prevention
Patch each file independently and verify exact `rg` locations before combining unrelated contexts.
## [ERR-20260718-007] Collection found stale tests for removed surfaces

**Logged**: 2026-07-18
**Severity**: medium
**Status**: resolved

### Summary
Full pytest collection failed because retained test modules still imported the removed skill-preloading helper and a removed WSL prompt constant.

### Prevention
After deleting a public helper or constant, immediately search test imports for its symbol and either prune checked-feature tests or restore retained-platform coverage.
## [ERR-20260718-008] Multi-region provider cleanup patch missed context

**Logged**: 2026-07-18
**Severity**: low
**Status**: resolved

### Summary
A multi-region patch for gateway/CLI OpenRouter cleanup failed on one display-label context and therefore applied none of its hunks.

### Prevention
Split large cross-file cleanup patches into small file-local patches after checking exact current text.
## [ERR-20260718-009] Model-metadata cleanup patch was too broad

**Logged**: 2026-07-18
**Severity**: low
**Status**: resolved

### Summary
A broad comment-and-code cleanup patch for model metadata failed context verification before applying.

### Prevention
Use small symbol-focused patches for live metadata code, then make comment-only cleanup separately.

## [ERR-20260718-010] Provider parity tests retained removed integrations

**Logged**: 2026-07-18
**Severity**: low
**Status**: resolved

### Summary
The focused provider-parity run failed because the test module still exercised removed OpenRouter, Gemini, Nous, Kimi, and provider-routing branches.

### Prevention
When deleting provider branches, prune or retarget their parity tests in the same change while preserving Codex and custom-endpoint coverage.

## [ERR-20260718-011] Focused parity process was killed

**Logged**: 2026-07-18
**Severity**: low
**Status**: monitoring

### Summary
The first post-prune provider-parity pytest process was terminated with exit 137 after 34 passing tests, without a Python failure traceback.

### Prevention
Re-run the file through the repository's per-file isolated runner and monitor memory pressure before treating it as a code regression.

## [ERR-20260718-012] Combined vision cleanup patch missed a comment

**Logged**: 2026-07-18
**Severity**: low
**Status**: resolved

### Summary
A combined test-and-comment patch failed because the image-routing comment text differed slightly from the expected context.

### Prevention
Apply behavioral test updates separately from comment-only cleanup and inspect exact lines first.

## [ERR-20260718-013] Collection retained removed platform imports

**Logged**: 2026-07-18
**Severity**: low
**Status**: resolved

### Summary
Full pytest collection found four test modules importing removed native Windows,
WSL, or Termux helpers.

### Prevention
After removing a platform implementation, search the full test tree for every
deleted public symbol and platform-specific fixture before running collection.

## [ERR-20260718-014] Prompt tests expected removed model families

**Logged**: 2026-07-18
**Severity**: low
**Status**: resolved

### Summary
The focused prompt-builder run retained three assertions for removed Grok,
Qwen, and DeepSeek model families after enforcement was narrowed to GPT/Codex.

### Prevention
Audit model-family assertions whenever the retained provider/model matrix is
reduced, including constants shared by prompt construction.

## [ERR-20260718-015] Mechanical test-pruning command was misquoted

**Logged**: 2026-07-18
**Severity**: low
**Status**: resolved

### Summary
A read-only Python command used to generate an `apply_patch` rewrite passed
literal newline escapes to `python -c`, producing a syntax error.

### Prevention
Use a quoted heredoc for multiline read-only transformation scripts, then pass
their output through `apply_patch` for the actual edit.

## [ERR-20260718-016] Full isolated baseline exposed stale cleanup tests

**Logged**: 2026-07-18
**Severity**: medium
**Status**: in progress

### Summary
The first complete isolated run passed 17,865 tests and identified 705 failures
across 123 files, mostly tests for explicitly removed providers, platforms,
connectors, Skills Hub, and legacy compatibility surfaces.

### Prevention
After a checklist-driven deletion pass, run the per-file suite early and use its
failure inventory to prune checked-feature tests and repair retained contracts.

## [ERR-20260718-017] AST pruning left decorators and empty classes

**Logged**: 2026-07-18
**Severity**: low
**Status**: resolved

### Summary
The mechanical test pruning used each AST node's `lineno`, which excludes
decorator lines, and left two classes empty after all selected methods were
removed. Compile checks caught the resulting syntax errors immediately.

### Prevention
When generating removal ranges from AST nodes, begin at the earliest decorator
line and remove parent classes whose bodies become empty; always run compileall
after mechanical pruning.
## ERR-021: Gateway cwd cleanup patch used stale exception text

- **Date:** 2026-07-18
- **Context:** Removing the legacy `MESSAGING_CWD` fallback.
- **Error:** `apply_patch` could not match the warning block because the live code logged `_bootstrap_exc` instead of silently passing.
- **Resolution:** Re-read the exact block and applied a narrower patch against the current text.
- **Status:** Resolved
## ERR-022: Multi-file legacy alias patch had an over-specific setup comment match

- **Date:** 2026-07-18
- **Context:** Removing remaining provider-field aliases.
- **Error:** A multi-file `apply_patch` was rejected while matching an unrelated setup comment, so none of its edits applied.
- **Resolution:** Split behavior changes from comment cleanup and used smaller exact patches.
- **Status:** Resolved
## ERR-023: `apply_patch` delete-file syntax included content lines

- **Date:** 2026-07-18
- **Context:** Removing the test file dedicated to deleted provider grouping.
- **Error:** The custom patch parser rejected content lines after `*** Delete File` as an invalid hunk.
- **Resolution:** Used the delete-file directive without a generated content hunk.
- **Status:** Resolved
## ERR-024: Targeted pytest command referenced a nonexistent runtime-provider test file

- **Date:** 2026-07-18
- **Context:** First focused validation after canonical provider cleanup.
- **Error:** Pytest exited with code 4 because `tests/marlow_cli/test_runtime_provider.py` does not exist.
- **Resolution:** Located matching test files with `rg --files` and reran the valid targeted set.
- **Status:** Resolved
## ERR-025: Canonical config save refactor left a stale local variable

- **Date:** 2026-07-18
- **Context:** Removing root-level config alias normalization.
- **Error:** Focused tests found `save_config()` still referenced deleted `current_normalized`, causing `NameError` after every successful atomic write.
- **Resolution:** Restored an explicitly named current config snapshot without legacy normalization and kept template-preserving output separate.
- **Status:** Resolved
## ERR-026: Test cleanup patch mixed prose and parameter-case edits

- **Date:** 2026-07-18
- **Context:** Updating provider tests to the canonical model-map schema.
- **Error:** A combined patch failed on a docstring match before reaching the behavioral edits.
- **Resolution:** Split the field/case updates from optional prose cleanup.
- **Status:** Resolved

## ERR-027: Referenced project rule files were absent

- **Date:** 2026-07-18
- **Context:** Rechecking the repository-specific workflow instructions before final validation.
- **Error:** The supplied `AGENTS.md` references `rules/workflow.md`, `rules/agent-usage.md`, and `rules/task-tracking.md`, but this checkout has no `rules/` directory.
- **Resolution:** Continued under the complete inline `AGENTS.md` requirements and the approved design document, which already require staged implementation, review, tracking, and validation.
- **Status:** Resolved

## ERR-028: Parallel test runner rejected advertised pytest flags

- **Date:** 2026-07-18
- **Context:** Starting the complete isolated test suite.
- **Error:** `scripts/run_tests_parallel.py` documents arbitrary pytest pass-through arguments, but its current parser rejected `-q --tb=short` as unknown options.
- **Resolution:** Reran the project runner without optional pytest flags; its per-file subprocesses already capture failure details.
- **Status:** Resolved

## ERR-029: Combined stale-test patch used inexact context

- **Date:** 2026-07-18
- **Context:** Adapting retained custom-provider tests and pruning tests for removed credential stores.
- **Error:** A combined `apply_patch` could not match a wrapped file-safety docstring; an immediate follow-up also targeted provider blocks already changed by the first partial application.
- **Resolution:** Re-read the live files, then applied small exact hunks against their current content.
- **Recurrence:** Confirmed on 2026-07-23 while adding the Telegram `/access`
  trigger carve-out; split the adapter and test edits into exact hunks.
- **Status:** Resolved

## ERR-030: Two targeted test commands referenced stale paths

- **Date:** 2026-07-18
- **Context:** Validating retained gateway, compression, browser, and file slices.
- **Error:** Two pytest invocations included test filenames that no longer exist in this checkout, so collection stopped before running valid targets.
- **Resolution:** Located current test paths with `rg --files` and reran the intended slices using the live filenames.
- **Status:** Resolved

## ERR-031: Browser test patch briefly produced a collection error

- **Date:** 2026-07-18
- **Context:** Updating a retained local-browser launch-hint assertion.
- **Error:** The first collection attempt reported an indentation error at the edited assertion.
- **Resolution:** Re-read and compiled the file, confirmed the corrected indentation, then reran the slice successfully.
- **Status:** Resolved

## ERR-032: Platform-skip test initially omitted its import

- **Date:** 2026-07-18
- **Context:** Making Linux-only `systemctl` guard self-tests portable to macOS.
- **Error:** The first patch used `shutil.which()` without importing `shutil`, causing four `NameError` failures.
- **Resolution:** Added the missing standard-library import and reran the slice.
- **Status:** Resolved

## ERR-033: TUI tests inherited SSH state and the wrong Python

- **Date:** 2026-07-18
- **Context:** Final TypeScript TUI validation.
- **Error:** The first Vitest run inherited an SSH marker, so local terminal-setup tests exited through the remote-session guard; its Python parity subprocess also used a Python without the project dependencies. One expensive cursor regression timed out at the default five seconds.
- **Resolution:** Kept the successful TypeScript type-check result and reran tests with SSH markers removed, `MARLOW_PYTHON` pointed at `.venv/bin/python`, and a larger test timeout.
- **Status:** Resolved

## ERR-034: EOF cleanup initially parsed line numbers as filenames

- **Date:** 2026-07-18
- **Context:** Clearing `git diff --check` warnings after mechanical test pruning.
- **Error:** The first pipeline retained `:line` suffixes from `git diff --check`, so Perl could not open the reported paths.
- **Resolution:** Extracted the filename field before applying the mechanical trailing-newline normalization; `git diff --check` is clean.
- **Status:** Resolved

## ERR-035: Dependency lock was stale after cleanup

- **Date:** 2026-07-18
- **Context:** Final package validation.
- **Error:** `uv lock --check` reported that `uv.lock` did not match the cleaned `pyproject.toml`, so the chained package build did not start.
- **Resolution:** Regenerated the lockfile with `uv lock`, rechecked it, then ran the package build separately.
- **Status:** Resolved

## ERR-036: Build frontend was not installed in the project venv

- **Date:** 2026-07-18
- **Context:** Final wheel and source-distribution validation.
- **Error:** `.venv/bin/python -m build` failed because the optional `build` frontend is not installed in the development environment.
- **Resolution:** Used the available `uv build` frontend instead, without adding a runtime dependency solely for packaging validation.
- **Status:** Resolved
## ERR-033: README cleanup patch used an inexact provider URL

- **Date:** 2026-07-18
- **Context:** Rewriting the project overview for the retained Codex/custom-only provider surface.
- **Error:** The first combined patch used an inexact Xiaomi URL in its context and did not apply.
- **Resolution:** Re-read the live README and applied the cleanup using exact current text.
- **Status:** Resolved

## ERR-034: Combined installer cleanup patch used inexact package-data context

- **Date:** 2026-07-18
- **Context:** Removing the legacy bootstrap and dashboard package-data entries.
- **Error:** A combined patch failed because the live `pyproject.toml` comments differed from the assumed context.
- **Resolution:** Re-read the live section and applied smaller exact hunks.
- **Status:** Resolved

## ERR-035: Error-log update included an empty patch hunk

- **Date:** 2026-07-18
- **Context:** Updating stale file-safety assertions while recording an earlier patch mismatch.
- **Error:** `apply_patch` rejected an empty `Update File` hunk for this log.
- **Resolution:** Combined the assertion edits with a complete, valid append hunk.
- **Status:** Resolved

## ERR-036: Multi-file stale-reference patch used mismatched browser text

- **Date:** 2026-07-18
- **Context:** Removing references to deleted execution and browser backends.
- **Error:** A broad multi-file patch failed while matching a browser CDP message.
- **Resolution:** Split the cleanup into smaller exact file-level patches.
- **Status:** Resolved

## ERR-037: Removed-backend prose patch used an incomplete paragraph

- **Date:** 2026-07-18
- **Context:** Removing the last `_stdin_mode` documentation after deleting SDK backends.
- **Error:** The patch omitted two live continuation lines and did not match.
- **Resolution:** Re-read the exact paragraph and replaced it with a retained-backend description.
- **Status:** Resolved

## ERR-038: OpenClaw cleanup missed two live comment suffixes

- **Date:** 2026-07-18
- **Context:** Removing the retired OpenClaw migration onboarding surface.
- **Error:** The combined patch omitted the suffixes on two `except` comments in the live CLI block.
- **Resolution:** Re-read the exact block and split onboarding, CLI, and Honcho edits.
- **Status:** Resolved

## ERR-039: Legacy-fallback patch used mismatched combined context

- **Date:** 2026-07-18
- **Context:** Removing the root-level fallback alias and terminal config aliases.
- **Error:** Two broad `apply_patch` calls failed because one hunk in each used inexact live context.
- **Resolution:** Re-read the affected sections and applied smaller exact hunks.
- **Status:** Resolved

## ERR-040: Retained-provider fixtures changed assertion semantics

- **Date:** 2026-07-18
- **Context:** Replacing removed provider fixtures with Codex/custom equivalents.
- **Error:** One cooldown test initially reused the primary custom provider, and one CLI assertion still required a slash-form remote model name.
- **Resolution:** Used two Codex fallback models for the cooldown chain and asserted the retained local model directly.
- **Status:** Resolved

## ERR-041: TUI parity helper received the wrong venv path

- **Date:** 2026-07-18
- **Context:** Final retained TUI validation from the `ui-tui` working directory.
- **Error:** The command-registry parity test reads `PYTHON`, not the gateway client's `MARLOW_PYTHON`, so it used a system Python without PyYAML even though Vitest passed.
- **Resolution:** Reran with `PYTHON` set to the repository venv's absolute interpreter path.
- **Status:** Resolved

## ERR-042: Monolithic suite was contaminated by Docker and leaked threads

- **Date:** 2026-07-18
- **Context:** Final repository-wide validation.
- **Error:** The unrestricted pytest run entered `tests/docker`, timed out while building the image, then accumulated Honcho/TUI/gateway background threads and cascaded timeout failures across otherwise unrelated modules.
- **Resolution:** Stopped the contaminated run and validated Docker-independent tests plus each reported module in fresh processes; Docker/package behavior is checked separately.
- **Status:** Resolved

## ERR-043: CLI reload test leaked a mocked renderer into resume output

- **Date:** 2026-07-18
- **Context:** Fail-fast Docker-independent suite validation.
- **Error:** `test_cli_init` reloads `cli` under prompt-toolkit stubs, so a later resume test's `_cprint` call produced no capturable stdout despite passing alone.
- **Resolution:** Made the resume stdout test bind `_cprint` to `print`, isolating its output-channel assertion from prior module reload state.
- **Status:** Resolved

## ERR-044: Discord import tests changed the DMChannel class identity

- **Date:** 2026-07-18
- **Context:** Ordered Discord gateway test validation.
- **Error:** Earlier lazy-import tests replaced the adapter module's `discord.DMChannel`, so reply extraction fixtures were no longer recognized as DMs and returned before dispatch.
- **Resolution:** Bound the adapter module's `DMChannel` to the fixture class for each reply extraction test.
- **Status:** Resolved

## ERR-045: Telegram module globals outlived fake-module fixtures

- **Date:** 2026-07-18
- **Context:** Ordered Telegram gateway test validation.
- **Error:** Tests replaced `sys.modules["telegram"]`, but an already-imported adapter retained older `ChatType` and rendering classes, causing group messages to be classified as DMs.
- **Resolution:** The fake-Telegram fixture now also patches the live adapter module globals for each test.
- **Status:** Resolved

## ERR-046: Skin assertion depended on prior global state

- **Date:** 2026-07-18
- **Context:** Fresh-process agent test-area validation.
- **Error:** A todo-display test asserted the default prefix after earlier tests had intentionally selected another global skin.
- **Resolution:** The test now selects the default skin for its assertion and restores the prior skin afterward.
- **Status:** Resolved

## ERR-047: Update stream assertion held a stale class after reload

- **Date:** 2026-07-18
- **Context:** Fresh-process `marlow_cli` test-area validation.
- **Error:** Earlier tests reloaded `marlow_cli.main`; the imported `_UpdateOutputStream` class no longer matched the current class used by `_install_hangup_protection`.
- **Resolution:** The integration assertion now checks against the installer function's live module-global class.
- **Status:** Resolved

## ERR-048: Deselected integration area stopped the test loop

- **Date:** 2026-07-18
- **Context:** Fresh-process validation by top-level test area.
- **Error:** `tests/integration` is excluded by the default marker configuration, so pytest returned its no-tests-selected status and stopped a `set -e` loop.
- **Resolution:** Recorded the 37 intentional deselections and resumed the remaining areas explicitly.
- **Status:** Resolved

## ERR-049: Tools tests inherited live task and session context

- **Date:** 2026-07-18
- **Context:** Fresh-process tools-area validation.
- **Error:** CWD and sudo-cache assertions inherited live terminal/session context from earlier tools tests; a config-sync assertion also still encoded the removed `env_type` alias.
- **Resolution:** Scoped the CWD test to its env fallback, explicitly set/reset the session ContextVar, and updated the config-map assertion to canonical `backend` parity.
- **Status:** Resolved

## ERR-050: Telegram parse-mode tests asserted enum repr internals

- **Date:** 2026-07-18
- **Context:** Full messaging-gateway area validation.
- **Error:** Six MarkdownV2 tests expected the fake enum name inside `repr()`, while the real retained Telegram library correctly supplies the value `'MarkdownV2'`.
- **Resolution:** Assertions now compare against the adapter method's live `ParseMode.MARKDOWN_V2` value.
- **Status:** Resolved

## ERR-051: Empty skill directories produced an empty deletion patch

- **Date:** 2026-07-18
- **Context:** Final checklist audit of removed bundled skill categories.
- **Error:** `find` previously listed the category directories, which looked like retained content, but a generated deletion patch was empty because the directories contained no tracked files.
- **Resolution:** Verified both the filesystem and Git index report zero files in those checked categories, then limited the cleanup to stale references.
- **Status:** Resolved

## ERR-052: TUI tests still expected the removed root max-turns alias

- **Date:** 2026-07-18
- **Context:** Focused validation after removing the last root-level `max_turns` compatibility reads.
- **Error:** Three TUI gateway tests expected root `max_turns` to override the canonical `agent.max_turns` defaults.
- **Resolution:** Updated the tests to verify that a missing or null `agent` section uses the documented foreground/background defaults and ignores the legacy root key.
- **Status:** Resolved

## ERR-053: Zsh did not split the changed-file lint list

- **Date:** 2026-07-18
- **Context:** Final lint of all changed Python files.
- **Error:** Passing a newline-delimited shell variable to Ruff under zsh produced one overlong filename because zsh does not perform sh-style implicit word splitting.
- **Resolution:** Switched to a newline-safe `xargs` pipeline so each changed Python path is passed as a separate argument.
- **Status:** Resolved

## ERR-054: Combined suite leaked gateway session context into a skills test

- **Date:** 2026-07-18
- **Context:** Full combined Cron, Marlow CLI, and TUI-gateway validation.
- **Error:** A prior gateway test left `MARLOW_SESSION_PLATFORM` explicitly cleared in its ContextVar, which correctly suppresses the process-environment fallback and made one skills-config test order-dependent.
- **Resolution:** Ran the environment-fallback assertion in a fresh `contextvars.Context`, matching a normal non-gateway caller without mutating production behavior.
- **Status:** Resolved

## ERR-055: A quoted audit search was malformed

- **Date:** 2026-07-18
- **Context:** Final fallback-schema audit.
- **Error:** A mixed single/double-quoted `rg` command left an unmatched quote in zsh.
- **Resolution:** Reissued the searches with simple single-quoted patterns and kept later audit commands similarly scoped.
- **Status:** Resolved

## ERR-056: A zsh loop variable overwrote the command search path

- **Date:** 2026-07-18
- **Context:** Preservation audit of retained feature files.
- **Error:** Naming a zsh loop variable `path` overwrote zsh's special `$path` array, so subsequent `git`, `rg`, and `head` commands were no longer discoverable.
- **Resolution:** Renamed the loop variable and reran the remaining audit command separately.
- **Status:** Resolved

## ERR-057: Vision tests inherited Codex native-routing state

- **Date:** 2026-07-18
- **Context:** Final mixed validation across fallback, Cron, delegation, Codex, and vision areas.
- **Error:** Three tests of the auxiliary vision handler inherited native-vision routing enabled by earlier retained Codex tests, so their mocked auxiliary client was intentionally bypassed.
- **Resolution:** Scoped the auxiliary-handler test class to the auxiliary route; native fast-path behavior remains covered in its dedicated test module.
- **Status:** Resolved

## ERR-058: Platform registry audit used a nonexistent accessor

- **Date:** 2026-07-18
- **Context:** Requirement-by-requirement completion audit of retained connectors.
- **Error:** The audit imported `get_platform_registry`, but the module exposes the registry through a different public surface.
- **Resolution:** Inspected `gateway/platform_registry.py` and reran discovery using its actual API.
- **Status:** Resolved

## ERR-059: Broad audit command used an unsafe zsh glob and missing interpreter alias

- **Date:** 2026-07-18
- **Context:** Residual checked-feature audit.
- **Error:** A `tools/model*` glob expanded unexpectedly and the checkout has no `python` alias.
- **Resolution:** Replaced the glob with explicit `rg` paths and used `.venv/bin/python` for repository checks.
- **Status:** Resolved

## ERR-060: Multi-file patches failed atomically on stale context

- **Date:** 2026-07-18
- **Context:** Removing dashboard, connector, and preload remnants.
- **Error:** Several large patches contained one stale hunk, so `apply_patch` correctly rejected the entire edit.
- **Resolution:** Re-read the affected regions and split changes into smaller, independently verifiable patches.
- **Status:** Resolved

## ERR-061: Dynamic patch helper had an invalid JavaScript replacement expression

- **Date:** 2026-07-18
- **Context:** Deleting obsolete cron skill-scanner functions through `apply_patch`.
- **Error:** The first helper expression was parsed incorrectly and raised `ReferenceError: g is not defined` before editing files.
- **Resolution:** Replaced it with explicit function-boundary pairs and generated each deletion patch separately.
- **Status:** Resolved

## ERR-062: Focused tests imported a removed preload-only scanner

- **Date:** 2026-07-18
- **Context:** Validation after removing cron skill preloading.
- **Error:** Test collection still imported `_scan_cron_skill_assembled`, which was intentionally removed with the preload path.
- **Resolution:** Removed the scanner-specific tests and updated the retained strict scanner test; the focused suite then passed 262 tests.
- **Status:** Resolved

## ERR-063: Backend guard tests expected removed remote environments

- **Date:** 2026-07-18
- **Context:** Focused validation after limiting terminal backends to local, Docker, and SSH.
- **Error:** Five tests still expected approval bypasses for removed Singularity, Modal, and Daytona environments.
- **Resolution:** Reduced those test matrices to the retained Docker backend; local and SSH behavior remain covered separately.
- **Status:** Resolved

## ERR-064: Shared ambient declarations were mistaken for a removed dependency shim

- **Date:** 2026-07-18
- **Context:** Removing the `bidi-js` dependency from the TUI renderer.
- **Error:** The first cleanup deleted the entire ambient declaration file, which also defines the forked Ink package's React compiler, reconciler, semver, and intrinsic element types.
- **Resolution:** Restored the shared declarations, removed only the `bidi-js` module stanza, and restored the declaration reference at the top of the package entrypoint.
- **Status:** Resolved

## ERR-065: AGENTS.md references unavailable rule files

- **Date:** 2026-07-18
- **Context:** Reloading repository instructions after an updated `AGENTS.md` was supplied.
- **Error:** `rules/workflow.md`, `rules/agent-usage.md`, and `rules/task-tracking.md` do not exist in the worktree or the Codex configuration tree.
- **Resolution:** Continued under the complete workflow and quality requirements stated directly in `AGENTS.md`; no referenced rule content could be loaded.
- **Recurrence:** Confirmed again on 2026-07-23 while preparing the usage-metrics PR; the inline project instructions remain authoritative.
- **Status:** Resolved

## ERR-066: Parallel test wrapper does not accept pytest verbosity flags

- **Date:** 2026-07-18
- **Context:** Starting the full repository validation suite.
- **Error:** `scripts/run_tests.sh -q` passed `-q` to `run_tests_parallel.py`, whose CLI does not accept pytest verbosity flags.
- **Resolution:** Restarted `scripts/run_tests.sh` without `-q`.
- **Recurrence:** Confirmed on 2026-07-23 while validating Telegram group access; use the wrapper without pytest verbosity flags.
- **Status:** Resolved

## ERR-067: Full TUI lint has unrelated baseline violations

- **Date:** 2026-07-18
- **Context:** Final static validation after the selective cleanup.
- **Error:** `npm --prefix ui-tui run lint` reports 33 errors and 103 warnings across pre-existing renderer, markdown, config-sync, import-order, and React-compiler code. Most affected files are outside this cleanup, and the same rules flag unchanged lines in touched files.
- **Resolution:** Kept the cleanup scoped; validated changed TUI behavior with a passing TypeScript type-check and targeted/full Vitest runs, and restored the package declaration entrypoint to its original formatting.
- **Status:** Resolved

## [ERR-20260719-001] scripts-run-tests-venv-discovery

**Logged**: 2026-07-19T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary

The canonical test wrapper cannot run in a worktree without a local or shared
Marlow virtual environment.

### Error

```
error: no virtualenv found in <worktree>/.venv or <worktree>/venv
```

### Context

- Attempted `scripts/run_tests.sh` with four focused test files.
- Neither worktree-local candidate exists and
  `~/.marlow/marlow-agent/venv` is also absent.
- System `python3` can compile the changed modules but has no pytest install.

### Suggested Fix

Create the documented shared virtual environment, or make the wrapper's error
message include the shared path it probes. Do not silently run the suite with
an environment that lacks the locked project dependencies.

### Metadata

- Reproducible: yes
- Related Files: scripts/run_tests.sh
- See Also: ERR-20260717-002

### Resolution

- **Resolved**: 2026-07-19T00:00:00+08:00
- **Notes**: Created the worktree-local `.venv` from the pinned lock with
  `uv sync --extra dev`; the canonical test wrapper can now run.

---

## [ERR-20260719-002] apply-patch-formatting

**Logged**: 2026-07-19T08:57:28Z
**Priority**: low
**Status**: resolved
**Area**: docs

### Summary

A correction-log patch initially used malformed added lines.

### Error

```
apply_patch verification failed: invalid hunk
```

### Context

- Wrapped prose lines in an added block were missing the required `+` prefix.

### Suggested Fix

Prefix every added line in an update hunk and retry with stable context.

### Metadata

- Reproducible: yes
- Related Files: .learnings/LEARNINGS.md

### Resolution

- **Resolved**: 2026-07-19T08:57:28Z
- **Notes**: Reissued the patch with valid added-line prefixes.

---

## [ERR-20260719-003] pytest-node-id-mismatch

**Logged**: 2026-07-19T09:01:44Z
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

A targeted pytest node ID used the wrong test class and method names.

### Error

```
ERROR: not found: tests/gateway/test_admin_approval_feature.py::TestAdminApprovalFeature::test_admin_route_overrides_yolo_mode_and_prior_grants
```

### Context

- The actual class is `TestAdminApprovalQueue`.
- The actual method is `test_admin_route_overrides_mode_off_yolo_and_prior_grants`.

### Suggested Fix

Resolve exact pytest node IDs with `rg` before invoking targeted tests.

### Metadata

- Reproducible: yes
- Related Files: tests/gateway/test_admin_approval_feature.py

### Resolution

- **Resolved**: 2026-07-19T09:01:44Z
- **Notes**: Corrected the node ID; the focused and broader suites passed.

---

## [ERR-20260719-004] hidden-error-log-replacement

**Logged**: 2026-07-19T09:06:00Z
**Priority**: high
**Status**: resolved
**Area**: docs

### Summary

Creating an absent hidden log file temporarily replaced a larger tracked file.

### Error

```
git diff --stat reported 1,604 deleted lines from .learnings/ERRORS.md
```

### Context

- `rg --files` omitted the hidden `.learnings` path.
- The tracked file was absent from the worktree but present in `HEAD`.
- An `Add File` patch therefore created a short replacement.
- Final full-diff review caught the history loss before handoff.

### Suggested Fix

Before adding a hidden repository file, check both the worktree and `git ls-files`
or `git show HEAD:<path>`. Always inspect the full diff before completion.

### Metadata

- Reproducible: yes
- Related Files: .learnings/ERRORS.md

### Resolution

- **Resolved**: 2026-07-19T09:06:00Z
- **Notes**: Restored the complete tracked log from `HEAD` and appended only
  the unique new entries.

---

## [ERR-20260722-001] scripts/run_tests.sh

**Logged**: 2026-07-22T13:33:02Z
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary

The canonical test runner could not find a supported virtualenv from this worktree.

### Error

```
error: no virtualenv found in /Users/liyuqi/.codex/worktrees/fcc5/hermes-lite/.venv or /Users/liyuqi/.codex/worktrees/fcc5/hermes-lite/venv
```

### Context

- The documented shared fallback at `~/.marlow/marlow-agent/venv` was absent.
- System Python 3.14 did not have pytest installed.
- The main checkout at `/Volumes/Dev/project/hermes-lite` had a reusable `.venv`.

### Suggested Fix

Document the main-checkout virtualenv as a worktree fallback or provide the documented shared environment.

### Metadata

- Reproducible: yes
- Related Files: scripts/run_tests.sh, AGENTS.md

### Resolution

- **Resolved**: 2026-07-22T13:36:00Z
- **Notes**: Used the existing main-checkout virtualenv without changing dependencies.

---

## [ERR-20260722-002] tracked-hidden-log-replacement

**Logged**: 2026-07-22T13:36:00Z
**Priority**: high
**Status**: resolved
**Area**: docs

### Summary

Adding apparently absent hidden learning logs replaced tracked history already present in `HEAD`.

### Error

```
git diff --numstat reported 1,896 deleted ERROR lines and 64 deleted FEATURE_REQUEST lines.
```

### Context

- The worktree omitted the tracked hidden files while `HEAD` retained them.
- This repeated the exact failure already documented in ERR-20260719-004.
- A focused status and full line-count comparison detected it before validation.

### Suggested Fix

Always run `git ls-files <hidden-path>` before creating hidden repository files, even when the worktree path is absent.

### Metadata

- Reproducible: yes
- Related Files: .learnings/ERRORS.md, .learnings/FEATURE_REQUESTS.md
- See Also: ERR-20260719-004

### Resolution

- **Resolved**: 2026-07-22T13:37:00Z
- **Notes**: Mechanically restored both files from `HEAD`, then appended only the new entries.

---

## [ERR-20260722-003] git-rev-parse-multiple-short-revisions

**Logged**: 2026-07-22T14:02:00Z
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

One Git inspection command passed multiple revisions to `git rev-parse --short`, which accepts a single revision in this form.

### Error

```
fatal: Needed a single revision
```

### Context

- The command attempted to print `HEAD`, `origin/main`, and `upstream/main` together.
- Fetching both remotes completed successfully before the display command failed.

### Suggested Fix

Invoke `git rev-parse --short` once per revision when comparing several refs.

### Metadata

- Reproducible: yes
- Related Files: .git/config

### Resolution

- **Resolved**: 2026-07-22T14:03:00Z
- **Notes**: Queried each revision separately and confirmed the PR should target the fork's `main` branch.

---

## [ERR-20260723-001] telegram-adapter-test-fixture

**Logged**: 2026-07-23T11:20:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

A lightweight Telegram adapter fixture omitted the base `platform` attribute.

### Error

```
AttributeError: 'TelegramAdapter' object has no attribute 'platform'
```

### Context

- The test constructed `TelegramAdapter` with `object.__new__`.
- The fail-closed administrator-verification log reads `self.name`, which derives
  from `self.platform`.

### Suggested Fix

Set required base attributes when tests bypass adapter initialization.

### Metadata

- Reproducible: yes
- Related Files: tests/gateway/test_telegram_group_access.py

### Resolution

- **Resolved**: 2026-07-23T11:20:00+08:00
- **Notes**: Set `adapter.platform = Platform.TELEGRAM` in the fixture.

---

## [ERR-20260723-002] gateway-feishu-baseline

**Logged**: 2026-07-23T11:30:00+08:00
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary

The full gateway suite has 12 unrelated Feishu webhook baseline failures.

### Error

```
12 failed, 189 passed in tests/gateway/test_feishu.py
AttributeError/ImportError for Feishu webhook methods and constants absent from gateway/platforms/feishu.py
```

### Context

- Reproduced by running `tests/gateway/test_feishu.py` alone.
- The Telegram group-access change does not modify the Feishu adapter or tests.
- The remaining 4,262 gateway tests passed or skipped.

### Suggested Fix

Reconcile the Feishu webhook security tests with the retained Feishu adapter in
a separate scoped change.

### Metadata

- Reproducible: yes
- Related Files: tests/gateway/test_feishu.py, gateway/platforms/feishu.py

---

## [ERR-20260725-001] telegram-approval-html-test

**Logged**: 2026-07-25T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

The intent-first Telegram approval test expected raw command quotes even though
the adapter correctly HTML-escapes all dynamic content.

### Error

```
AssertionError: raw command was not found in the HTML approval message
```

### Context

- The rendered command contained `&#x27;` instead of a literal single quote.
- The escaping is required to prevent Telegram HTML markup injection.

### Suggested Fix

Compare against the standard-library HTML-escaped command.

### Metadata

- Reproducible: yes
- Related Files: tests/gateway/test_telegram_approval_buttons.py

### Resolution

- **Resolved**: 2026-07-25T00:00:00+08:00
- **Notes**: Updated the assertion to use `html.escape()`.

---

## [ERR-20260725-002] mcp-tool-collection-baseline

**Logged**: 2026-07-25T00:00:00+08:00
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary

The MCP tool suite does not collect because its compatibility type is not
exported by the current `tools.mcp_tool` module.

### Error

```
ImportError: cannot import name 'CreateMessageResultWithTools' from 'tools.mcp_tool'
```

### Context

- Encountered while running broader tool-schema compatibility validation.
- The terminal-intent change does not modify MCP code or its tests.
- The remaining model-tool, toolset, registry, and transport suites passed.

### Suggested Fix

Reconcile the MCP SDK compatibility import and module export in a separately
scoped change.

### Metadata

- Reproducible: yes
- Related Files: tools/mcp_tool.py, tests/tools/test_mcp_tool.py

---

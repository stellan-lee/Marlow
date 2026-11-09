# Marlow Agent - Development Guide

Instructions for AI coding assistants and developers working on the marlow-agent codebase.

**Never give up on the right solution.**

## Development Environment

```bash
# Prefer .venv; fall back to venv if that's what your checkout has.
source .venv/bin/activate   # or: source venv/bin/activate
```

`scripts/run_tests.sh` probes `.venv` first, then `venv`, then
`$HOME/.marlow/marlow-agent/venv` (for worktrees that share a venv with the
main checkout).

## Project Structure

File counts shift constantly — don't treat the tree below as exhaustive.
The canonical source is the filesystem. The notes call out the load-bearing
entry points you'll actually edit.

```
marlow-agent/
├── run_agent.py          # Thin SDK facade — sole Codex app-server runtime
├── model_tools.py        # Tool orchestration, discover_builtin_tools(), handle_function_call()
├── toolsets.py           # Toolset definitions, _MARLOW_CORE_TOOLS list
├── marlow_state.py       # SessionDB — SQLite session store (FTS5 search)
├── marlow_constants.py   # get_marlow_home(), display_marlow_home() — profile-aware paths
├── marlow_logging.py     # setup_logging() — agent.log / errors.log / gateway.log (profile-aware)
├── batch_runner.py       # Parallel batch processing
├── agent/                # Codex adapter, isolated environment, business memory and policy
├── marlow_cli/           # CLI subcommands, setup wizard, plugin loader and single-query entry points
├── tools/                # Tool implementations — auto-discovered via tools/registry.py
│   └── environments/     # Terminal backends (local, docker, ssh)
├── gateway/              # Messaging gateway — run.py + session.py + platforms/
│   ├── platforms/        # Built-in adapters: telegram, slack, feishu, email,
│   │                     #   and signed webhook. Discord ships as a platform plugin.
│   └── builtin_hooks/    # Extension point for always-registered gateway hooks (none shipped)
├── plugins/              # Plugin system (see "Plugins" section below)
│   ├── memory/           # Memory-provider plugins (honcho)
│   ├── observability/    # Metrics / traces / logs plugin
│   └── <others>/         # observability, browser/web, platforms, security
├── .agents/skills/       # Developer skills for the source checkout
├── optional-skills/      # Shipped skills for manual installation
├── skills/               # Shipped skills for on-demand lookup
├── cron/                 # Scheduler — jobs.py, scheduler.py
├── scripts/              # run_tests.sh, release.py, auxiliary scripts
└── tests/                # Pytest suite (~17k tests across ~900 files as of May 2026)
```

**User config:** `~/.marlow/config.yaml` (settings), `~/.marlow/.env` (API keys only).
**Logs:** `~/.marlow/logs/` — `agent.log` (INFO+), `errors.log` (WARNING+),
`gateway.log` when running the gateway. Profile-aware via `get_marlow_home()`.
Browse with `marlow logs [--follow] [--level ...] [--session ...]`.


## Codex-only execution

Codex CLI 0.159.2 is the only supported inference runtime. Read
`docs/architecture/phase-2-codex-runtime.md` and
`docs/architecture/phase-2-validation.md` for configuration, migration and
validation evidence. Never reintroduce an inference client, provider router,
Native loop, fallback chain, model-driven approval judge or Marlow compressor.

`agent/codex_environment.py` owns discovery, HOME/CODEX_HOME and sandbox policy.
`agent/transports/codex_app_server_session.py` owns the protocol event pump.
Marlow's scoped MCP boundary owns business authorization. Model-native execution
tools are disabled; shell/files use the same server's sandboxed `command/exec`
only after host checks. Host browser and media operations must preserve the
trusted file-disclosure scope. Delegation inherits its parent's workspace and
grants. Profile globals must not retain another profile's business paths.

Model settings and authentication belong in the profile's native Codex TOML and
file credential store. Historical YAML inference settings are preserved and
rejected with migration guidance. A stopped or uncertain mutation must never be
replayed automatically. A new runtime needs real-schema, real-subprocess and
independent read-only verification; mock tests alone do not establish sandbox
or provider compatibility.

## TypeScript Style

Applies to TypeScript across Marlow: future TypeScript packages.

- Prefer small nanostores over component state when state is shared, reused, or read by distant UI.
- Let each feature own its atoms. Chat state belongs near chat, shell state near shell, shared state in `src/store`.
- Components that render from an atom should use `useStore`. Non-rendering actions should read with `$atom.get()`.
- Do not pass state through three components when the leaf can subscribe to the atom.
- Keep persistence beside the atom that owns it.
- Keep route roots thin. They compose routes and shell; they should not become controllers.
- No monolithic hooks. A hook should own one narrow job.
- Prefer colocated action modules over hidden god hooks.
- If a callback is pure side effect, use the terse void form:
  `onState={st => void setGatewayState(st)}`.
- Async UI handlers should make intent explicit:
  `onClick={() => void save()}`.
- Prefer interfaces for public props and shared object shapes. Avoid `type X = { ... }` for object props.
- Extend React primitives for props: `React.ComponentProps<'button'>`, `React.ComponentProps<typeof Dialog>`, `Omit<...>`, `Pick<...>`.
- Table-driven beats condition ladders when mapping ids, routes, or views.
- `src/app` owns routes, pages, and page-specific components.
- `src/store` owns shared atoms.
- `src/lib` owns shared pure helpers.

## File Dependency Chain

```
tools/registry.py  (no deps — imported by all tool files)
       ↑
tools/*.py  (each calls registry.register() at import time)
       ↑
model_tools.py  (imports tools/registry + triggers tool discovery)
       ↑
run_agent.py, cli.py, batch_runner.py, environments/
```

---

## AIAgent Class (run_agent.py)

`AIAgent` is a synchronous SDK facade over the single Codex app-server adapter.
`chat()` returns final text; `run_conversation()` returns final text, history,
usage and durable completion/error state. Read the actual constructor for
callbacks and scope fields. Legacy credentials, provider routing and runtime
switches are rejected with migration guidance.

Codex owns the inference/tool loop and compaction. Marlow projects business
context before a turn, authorizes each MCP operation and records its outcome.
Never restore a client.chat.completions loop or an auxiliary inference client.

---

## Command-line and Gateway Entry Points

`marlow_cli/main.py` owns CLI routing. `marlow_cli/query.py` implements
`marlow chat -q`, including image input and session continuation.
`marlow_cli/oneshot.py` implements the stdout-only `marlow -z` interface.
Both use the core `AIAgent`. Operator setup and model selection use their
own menus.

Gateway slash commands are registered in `marlow_cli/commands.py` and
dispatched by `gateway/run.py`. Telegram and Slack build their menus from
the same registry. Use `marlow_cli.config.save_config_value()` for atomic,
typed profile setting changes. Explicit `/learn` requests can create a
skill. Background memory review uses memory tools.

---

## Adding New Tools

For most custom or local-only tools, do **not** edit Marlow core. Use the plugin
route instead: create `~/.marlow/plugins/<name>/plugin.yaml` and
`~/.marlow/plugins/<name>/__init__.py`, then register tools with
`ctx.register_tool(...)`. Plugin toolsets are discovered automatically and can be
enabled or disabled without touching `tools/` or `toolsets.py`.

Use the built-in route below only when the user is explicitly contributing a new
core Marlow tool that should ship in the base system.

Built-in/core tools require changes in **2 files**:

**1. Create `tools/your_tool.py`:**
```python
import json, os
from tools.registry import registry

def check_requirements() -> bool:
    return bool(os.getenv("EXAMPLE_API_KEY"))

def example_tool(param: str, task_id: str = None) -> str:
    return json.dumps({"success": True, "data": "..."})

registry.register(
    name="example_tool",
    toolset="example",
    schema={"name": "example_tool", "description": "...", "parameters": {...}},
    handler=lambda args, **kw: example_tool(param=args.get("param", ""), task_id=kw.get("task_id")),
    check_fn=check_requirements,
    requires_env=["EXAMPLE_API_KEY"],
)
```

**2. Add to `toolsets.py`** — either `_MARLOW_CORE_TOOLS` (all platforms) or a new toolset. **This step is required:** auto-discovery imports the tool and registers its schema, but the tool is only *exposed to an agent* if its name appears in a toolset. `_MARLOW_CORE_TOOLS` is not dead code — it's the default bundle every platform's base toolset inherits from.

Auto-discovery: any `tools/*.py` file with a top-level `registry.register()` call is imported automatically — no manual import list to maintain. Wiring into a toolset is still a deliberate, manual step.

The registry handles schema collection, dispatch, availability checking, and error wrapping. All handlers MUST return a JSON string.

**Path references in tool schemas**: If the schema description mentions file paths (e.g. default output directories), use `display_marlow_home()` to make them profile-aware. The schema is generated at import time, which is after `_apply_profile_override()` sets `MARLOW_HOME`.

**State files**: If a tool stores persistent state (caches, logs, checkpoints), use `get_marlow_home()` for the base directory — never `Path.home() / ".marlow"`. This ensures each profile gets its own state.

**Agent-level tools** (todo, memory): intercepted by `run_agent.py` before `handle_function_call()`. See `tools/todo_tool.py` for the pattern.

---

## Dependency Pinning Policy

All dependencies must have upper bounds to limit supply-chain attack surface.
This policy was established after the litellm compromise (PR #2796, #2810) and
reinforced after the Mini Shai-Hulud worm campaign (May 2026).

| Source type | Treatment | Example |
|---|---|---|
| PyPI package | `>=floor,<next_major` | `"httpx>=0.28.1,<1"` |
| Git URL | Commit SHA | `git+https://...@<40-char-sha>` |
| GitHub Actions | Commit SHA + comment | `uses: actions/checkout@<sha>  # v4` |
| CI-only pip | `==exact` | `pyyaml==6.0.2` |

**When adding a new dependency to `pyproject.toml`:**
1. Pin to `>=current_version,<next_major` for post-1.0 (e.g. `>=1.5.0,<2`).
2. For pre-1.0 packages, use `<0.(current_minor + 2)` (e.g. `>=0.29,<0.32`).
3. Never commit a bare `>=X.Y.Z` without a ceiling — CI and reviewers will reject it.
4. Run `uv lock` to regenerate `uv.lock` with hashes.

Reference: #2810 (bounds pass), #9801 (SHA pinning + audit CI).

---

## Adding Configuration

### config.yaml options:
1. Add to `DEFAULT_CONFIG` in `marlow_cli/config.py`
2. Bump `_config_version` (check the current value at the top of `DEFAULT_CONFIG`)
   ONLY if you need to actively migrate/transform existing user config
   (renaming keys, changing structure). Adding a new key to an existing
   section is handled automatically by the deep-merge and does NOT require
   a version bump.

### Runtime and business settings

`runtime.codex` configures the managed binary, timeouts, bounded concurrency,
explicit environment variables and project/skill allowlists. Model and provider
settings belong exclusively in the profile's `runtime/codex/config.toml`.
Retired YAML inference fields fail with migration guidance and are not silently
translated. Memory, tools, channels, delegation and Cron retain their existing
business configuration.

### .env variables (SECRETS ONLY — API keys, tokens, passwords):
1. Add to `OPTIONAL_ENV_VARS` in `marlow_cli/config.py` with metadata:
```python
"NEW_API_KEY": {
    "description": "What it's for",
    "prompt": "Display name",
    "url": "https://...",
    "password": True,
    "category": "tool",  # provider, tool, messaging, setting
},
```

Non-secret settings (timeouts, thresholds, feature flags, paths, display
preferences) belong in `config.yaml`, not `.env`. If internal code needs an
env var mirror for backward compatibility, bridge it from `config.yaml` to
the env var in code (see `gateway_timeout`, `terminal.cwd` → `TERMINAL_CWD`).

### Config loaders (three paths — know which one you're in):

| Loader | Used by | Location |
|--------|---------|----------|
| `load_config()` | `marlow tools`, `marlow setup`, most CLI subcommands | `marlow_cli/config.py` — merges `DEFAULT_CONFIG` + user YAML |
| Direct YAML load | Gateway runtime | `gateway/run.py` + `gateway/config.py` — reads user YAML raw |

If you add a new key and the CLI sees it but the gateway doesn't (or vice
versa), you're on the wrong loader. Check `DEFAULT_CONFIG` coverage.

### Working directory:
- **CLI** — uses the process's current directory (`os.getcwd()`).
- **Messaging** — uses `terminal.cwd` from `config.yaml`. The gateway bridges this
  to the `TERMINAL_CWD` env var for child tools. **`MESSAGING_CWD` has been
  removed** — the config loader prints a deprecation warning if it's set in
  `.env`. Same for `TERMINAL_CWD` in `.env`; the canonical setting is
  `terminal.cwd` in `config.yaml`.

---

## Plugins

Marlow has two plugin surfaces. Both live under `plugins/` in the repo so
repo-shipped plugins can be discovered alongside user-installed ones in
`~/.marlow/plugins/` and pip-installed entry points.

### General plugins (`marlow_cli/plugins.py` + `plugins/<name>/`)

`PluginManager` discovers plugins from `~/.marlow/plugins/`, `./.marlow/plugins/`,
and pip entry points. Each plugin exposes a `register(ctx)` function that
can:

- Register Python-callback lifecycle hooks:
  `pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`,
  `on_session_start`, `on_session_end`
- Register new tools via `ctx.register_tool(...)`
- Register CLI subcommands via `ctx.register_cli_command(...)` — the
  plugin's argparse tree is wired into `marlow` at startup so
  `marlow <pluginname> <subcmd>` works with no change to `main.py`

Hooks are invoked from `model_tools.py` (pre/post tool) and `run_agent.py`
(lifecycle). **Discovery timing pitfall:** `discover_plugins()` only runs
as a side effect of importing `model_tools.py`. Code paths that read plugin
state without importing `model_tools.py` first must call `discover_plugins()`
explicitly (it's idempotent).

### Memory-provider plugins (`plugins/memory/<name>/`)

Separate discovery system for pluggable memory backends. The built-in
provider is **honcho**.

Each provider implements the `MemoryProvider` ABC (see `agent/memory_provider.py`)
and is orchestrated by `agent/memory_manager.py`. Lifecycle hooks include
`sync_turn(turn_messages)`, `prefetch(query)`, `shutdown()`, and optional
`post_setup(marlow_home, config)` for setup-wizard integration.

**CLI commands via `plugins/memory/<name>/cli.py`:** if a memory plugin
defines `register_cli(subparser)`, `discover_plugin_cli_commands()` finds
it at argparse setup time and wires it into `marlow <plugin>`. The
framework only exposes CLI commands for the **currently active** memory
provider (read from `memory.provider` in config.yaml), so disabled
providers don't clutter `marlow --help`.

**Rule (Teknium, May 2026):** plugins MUST NOT modify core files
(`run_agent.py`, `cli.py`, `gateway/run.py`, `marlow_cli/main.py`, etc.).
If a plugin needs a capability the framework doesn't expose, expand the
generic plugin surface (new hook, new ctx method) — never hardcode
plugin-specific logic into core. PR #5295 removed 95 lines of hardcoded
honcho argparse from `main.py` for exactly this reason.

**No new in-tree memory providers (policy, May 2026):** the set of
built-in memory providers under `plugins/memory/` is closed. New memory
backends must ship as **standalone plugin repos** that users install
into `~/.marlow/plugins/` (or via pip entry points) — they implement
the same `MemoryProvider` ABC, register through the same discovery
path, and integrate via `marlow memory setup` / `post_setup()` without
landing in this tree. PRs that add a new directory under
`plugins/memory/` will be closed with a pointer to publish the
provider as its own repo. Existing in-tree providers stay; bug fixes
to them are welcome.

### Model configuration

Model-provider and context-engine plugins are retired. Custom inference
providers use Codex native Responses configuration only. Business plugins and
memory providers remain supported inside their trusted profile scope.

## Skills

New profiles start with an empty skill directory. The source tree has three skill locations.

- **`skills/`** ships `native-mcp` and `webhook-subscriptions` for explicit
  lookup. Use `skills_list`, `skill_view`, or the skill's named Gateway slash
  command. These assets stay in the installation directory.
- **`optional-skills/`** ships skills for manual installation. Copy the desired
  directory into `$MARLOW_HOME/skills/` or a configured external skill directory.
- **`.agents/skills/`** contains developer skills for the source checkout.
  The `marlow-agent` developer skill stays outside the product packages.

When reviewing skill PRs, check which directory they target — heavy-dep or
niche skills belong in `optional-skills/`.

### SKILL.md frontmatter

Standard fields: `name`, `description`, `version`, `author`, `license`,
`platforms` (OS-gating list: `[macos]`, `[linux, macos]`, ...),
`metadata.marlow.tags`, `metadata.marlow.category`,
`metadata.marlow.related_skills`, `metadata.marlow.config` (config.yaml
settings the skill needs — stored under `skills.config.<key>`, prompted
during setup, injected at load time).

Top-level `tags:` and `category:` are also accepted and mirrored from
`metadata.marlow.*` by the loader.

### Skill authoring standards (HARDLINE)

Every new or modernized skill — bundled, optional, or contributed —
must meet these standards before merge. Reviewers reject PRs that
violate them.

1. **`description` ≤ 60 characters, one sentence, ends with a period.**
   Long descriptions bloat skill listings and dilute the model's
   attention when many skills are loaded. State the capability, not
   the implementation. No marketing words ("powerful",
   "comprehensive", "seamless", "advanced"). Don't repeat the skill
   name. Verify with:
   ```python
   import re, pathlib
   m = re.search(r'^description: (.*)$',
                 pathlib.Path('skills/<cat>/<name>/SKILL.md').read_text(),
                 re.MULTILINE)
   assert len(m.group(1)) <= 60, len(m.group(1))
   ```

2. **Tools referenced in SKILL.md prose must be native Marlow tools or
   MCP servers the skill explicitly expects.** When the skill needs a
   capability, point at the proper tool by name in backticks
   (`` `terminal` ``, `` `web_extract` ``, `` `read_file` ``,
   `` `patch` ``, `` `search_files` ``, `` `vision_analyze` ``,
   `` `browser_navigate` ``, `` `delegate_task` ``, etc.). Do NOT
   name shell utilities the agent already has wrapped — `grep` →
   `search_files`, `cat`/`head`/`tail` → `read_file`, `sed`/`awk` →
   `patch`, `find`/`ls` → `search_files target='files'`. If the skill
   depends on an MCP server, name the MCP server and document the
   expected setup in `## Prerequisites`. Anything else (third-party
   CLIs, shell pipelines, etc.) is fair game inside script files but
   should not be the headline interaction surface in the prose.

3. **`platforms:` gating audited against actual script imports.**
   Skills that use POSIX-only primitives (`fcntl`, `termios`,
   `os.setsid`, `os.kill(pid, 0)` for liveness, `/proc`, `/tmp`
   hardcoded, `signal.SIGKILL`, bash heredocs, `osascript`, `apt`,
   `systemctl`) must declare their supported platforms. Default
   posture: try to fix it cross-platform first — `tempfile.gettempdir`,
   `pathlib.Path`, `psutil.pid_exists`, Python-level filtering instead
   of `grep`. Gate to a narrower set only when the dependency is
   genuinely platform-bound.

4. **`author` credits the human contributor first.** For external
   contributions, the contributor's real name + GitHub handle goes
   first; "Marlow Agent" is the secondary collaborator. If the
   contributor's commit shows "Marlow Agent" as author (because they
   used Marlow to draft the skill), replace it with their actual name
   — credit the human, not the tool.

5. **SKILL.md body uses the modern section order.** `# <Skill> Skill`
   title, 2-3 sentence intro stating what it does and doesn't do,
   `## When to Use`, `## Prerequisites`, `## How to Run`,
   `## Quick Reference`, `## Procedure`, `## Pitfalls`,
   `## Verification`. Target ~200 lines for a complex skill,
   ~100 lines for a simple one. Cut redundant intro fluff, marketing
   prose, and re-explanations of env vars already in
   `## Prerequisites`.

6. **Scripts go in `scripts/`, references in `references/`,
   templates in `templates/`.** Don't expect the model to inline-write
   parsers, XML walkers, or non-trivial logic every call — ship a
   helper script. Reference it from SKILL.md by path relative to the
   skill directory.

7. **Tests live at `tests/skills/test_<skill>_skill.py`** and use only
   stdlib + pytest + `unittest.mock`. No live network calls. Run via
   `scripts/run_tests.sh tests/skills/test_<skill>_skill.py -q`.

8. **`.env.example` additions are isolated to a clearly delimited
   block.** Don't touch the surrounding file — contributor-supplied
   `.env.example` versions are usually stale and edits outside the
   skill's own block must be dropped during salvage.

For the project development reference, see
[the Marlow developer skill](.agents/skills/marlow-agent/SKILL.md).

---

## Toolsets

All toolsets are defined in `toolsets.py` as a single `TOOLSETS` dict.
Each platform's adapter picks a base toolset (e.g. Telegram uses
`"messaging"`); `_MARLOW_CORE_TOOLS` is the default bundle most
platforms inherit from.

Current toolset keys: `admin_approval`, `browser`, `clarify`,
`code_execution`, `context_engine`, `cronjob`, `debugging`,
`delegation`, `file`, `memory`, `messaging`, `moa`, `safe`,
`search`, `session_search`, `skills`, `terminal`, `todo`, `vision`,
and `web`.

Enable/disable per platform via `marlow tools` (the curses UI) or the
`tools.<platform>.enabled` / `tools.<platform>.disabled` lists in
`config.yaml`.

---

## Delegation (`delegate_task`)

`tools/delegate_tool.py` spawns a subagent with an isolated
context + terminal session. Synchronous: the parent waits for the
child's summary before continuing its own loop — if the parent is
interrupted, the child is cancelled.

Two shapes:

- **Single:** pass `goal` (+ optional `context`, `toolsets`).
- **Batch (parallel):** pass `tasks: [...]` — each gets its own subagent
  running concurrently. Concurrency is capped by
  `delegation.max_concurrent_children` (default 3).

Roles:

- `role="leaf"` (default) — focused worker. Cannot call `delegate_task`,
  `clarify`, `memory`, `send_message`, `execute_code`.
- `role="orchestrator"` — retains `delegate_task` so it can spawn its
  own workers. Gated by `delegation.orchestrator_enabled` (default true)
  and bounded by `delegation.max_spawn_depth` (default 2).

Key config knobs (under `delegation:` in `config.yaml`):
`max_concurrent_children`, `max_spawn_depth`, `child_timeout_seconds`,
`orchestrator_enabled`, `inherit_mcp_toolsets`,
`max_iterations`.

Synchronicity rule: delegate_task is **not** durable. For long-running
work that must outlive the current turn, use `cronjob` instead.

---

## Cron (scheduled jobs)

`cron/jobs.py` (job store) + `cron/scheduler.py` (tick loop). Agents
schedule jobs via the `cronjob` tool; users via `marlow cron <verb>`
(`list`, `add`, `edit`, `pause`, `resume`, `run`, `remove`).

Supported schedule formats:
- Duration: `"30m"`, `"2h"`, `"1d"`
- "every" phrase: `"every 2h"`, `"every monday 9am"`
- 5-field cron expression: `"0 9 * * *"`
- ISO timestamp (one-shot): `"2026-06-01T09:00:00Z"`

Per-job fields include `skills` (load specific skills), `model` /
`provider` overrides, `script` (pre-run data-collection script whose
stdout is injected into the prompt; `no_agent=True` turns the script
into the entire job), `context_from` (chain job A's last output into
job B's prompt), `workdir` (run in a specific directory with its
`AGENTS.md`/`CLAUDE.md` loaded), and multi-platform delivery.

Hardening invariants:
- **3-minute hard interrupt** on cron sessions — runaway agent loops
  cannot monopolize the scheduler.
- Catchup window: half the job's period, clamped to 120s–2h.
- Grace window: 120s for one-shot jobs whose fire time was missed.
- File lock at `~/.marlow/cron/.tick.lock` prevents duplicate ticks
  across processes.
- Cron sessions pass `skip_memory=True` by default; memory providers
  intentionally do not run during cron.

Cron deliveries are **not** mirrored into the target gateway session —
they land in their own cron session with a header/footer frame so the
main conversation's message-role alternation stays intact.

---

## Important Policies

### Context and triggered work

Codex owns context compaction. Preserve raw Marlow history; never rewrite it to
simulate native Thread rollback. Context, tool and memory changes must preserve
the current profile and conversation scope.

AI work requires an explicit input, due AI job or authorized follow-up. Do not
add idle AI heartbeats. `terminal(background=True)` is currently unsupported;
use scoped Cron/delegation for supported triggered work.

---

## Profiles: Multi-Instance Support

Marlow supports **profiles** — multiple fully isolated instances, each with its own
`MARLOW_HOME` directory (config, API keys, memory, sessions, skills, gateway, etc.).

The core mechanism: `_apply_profile_override()` in `marlow_cli/main.py` sets
`MARLOW_HOME` before any module imports. All `get_marlow_home()` references
automatically scope to the active profile.

### Rules for profile-safe code

1. **Use `get_marlow_home()` for all MARLOW_HOME paths.** Import from `marlow_constants`.
   NEVER hardcode `~/.marlow` or `Path.home() / ".marlow"` in code that reads/writes state.
   ```python
   # GOOD
   from marlow_constants import get_marlow_home
   config_path = get_marlow_home() / "config.yaml"

   # BAD — breaks profiles
   config_path = Path.home() / ".marlow" / "config.yaml"
   ```

2. **Use `display_marlow_home()` for user-facing messages.** Import from `marlow_constants`.
   This returns `~/.marlow` for default or `~/.marlow/profiles/<name>` for profiles.
   ```python
   # GOOD
   from marlow_constants import display_marlow_home
   print(f"Config saved to {display_marlow_home()}/config.yaml")

   # BAD — shows wrong path for profiles
   print("Config saved to ~/.marlow/config.yaml")
   ```

3. **Module-level constants are fine** — they cache `get_marlow_home()` at import time,
   which is AFTER `_apply_profile_override()` sets the env var. Just use `get_marlow_home()`,
   not `Path.home() / ".marlow"`.

4. **Tests that mock `Path.home()` must also set `MARLOW_HOME`** — since code now uses
   `get_marlow_home()` (reads env var), not `Path.home() / ".marlow"`:
   ```python
   with patch.object(Path, "home", return_value=tmp_path), \
        patch.dict(os.environ, {"MARLOW_HOME": str(tmp_path / ".marlow")}):
       ...
   ```

5. **Gateway platform adapters should use token locks** — if the adapter connects with
   a unique credential (bot token, API key), call `acquire_scoped_lock()` from
   `gateway.status` in the `connect()`/`start()` method and `release_scoped_lock()` in
   `disconnect()`/`stop()`. This prevents two profiles from using the same credential.
   See `gateway/platforms/telegram.py` for the canonical pattern.

6. **Profile operations are HOME-anchored, not MARLOW_HOME-anchored** — `_get_profiles_root()`
   returns `Path.home() / ".marlow" / "profiles"`, NOT `get_marlow_home() / "profiles"`.
   This is intentional — it lets `marlow -p coder profile list` see all profiles regardless
   of which one is active.

## Known Pitfalls

### DO NOT hardcode `~/.marlow` paths
Use `get_marlow_home()` from `marlow_constants` for code paths. Use `display_marlow_home()`
for user-facing print/log messages. Hardcoding `~/.marlow` breaks profiles — each profile
has its own `MARLOW_HOME` directory. This was the source of 5 bugs fixed in PR #3575.

### DO NOT introduce new `simple_term_menu` usage
Existing call sites in `marlow_cli/main.py` remain for legacy fallback only;
the preferred UI is curses (stdlib) because `simple_term_menu` has
ghost-duplication rendering bugs in tmux/iTerm2 with arrow keys. New
interactive menus must use `marlow_cli/curses_ui.py` — see
`marlow_cli/tools_config.py` for the canonical pattern.

### DO NOT use `\033[K` (ANSI erase-to-EOL) in spinner/display code
Leaks as literal `?[K` text under `prompt_toolkit`'s `patch_stdout`. Use space-padding: `f"\r{line}{' ' * pad}"`.

### `_last_resolved_tool_names` is a process-global in `model_tools.py`
`_run_single_child()` in `delegate_tool.py` saves and restores this global around subagent execution. If you add new code that reads this global, be aware it may be temporarily stale during child agent runs.

### DO NOT hardcode cross-tool references in schema descriptions
Tool schema descriptions must not mention tools from other toolsets by name (e.g., `browser_navigate` saying "prefer web_search"). Those tools may be unavailable (missing API keys, disabled toolset), causing the model to hallucinate calls to non-existent tools. If a cross-reference is needed, add it dynamically in `get_tool_definitions()` in `model_tools.py` — see the `browser_navigate` / `execute_code` post-processing blocks for the pattern.

### The gateway has TWO message guards — both must bypass approval/control commands
When an agent is running, messages pass through two sequential guards:
(1) **base adapter** (`gateway/platforms/base.py`) queues messages in
`_pending_messages` when `session_key in self._active_sessions`, and
(2) **gateway runner** (`gateway/run.py`) intercepts `/stop`, `/new`,
`/queue`, `/status`, `/approve`, `/deny` before they reach
`running_agent.interrupt()`. Any new command that must reach the runner
while the agent is blocked (e.g. approval prompts) MUST bypass BOTH
guards and be dispatched inline, not via `_process_message_background()`
(which races session lifecycle).

### Squash merges from stale branches silently revert recent fixes
Before squash-merging a PR, ensure the branch is up to date with `main`
(`git fetch origin main && git reset --hard origin/main` in the worktree,
then re-apply the PR's commits). A stale branch's version of an unrelated
file will silently overwrite recent fixes on main when squashed. Verify
with `git diff HEAD~1..HEAD` after merging — unexpected deletions are a
red flag.

### Don't wire in dead code without E2E validation
Unused code that was never shipped was dead for a reason. Before wiring an
unused module into a live code path, E2E test the real resolution chain
with actual imports (not mocks) against a temp `MARLOW_HOME`.

### Tests must not write to `~/.marlow/`
The `_isolate_marlow_home` autouse fixture in `tests/conftest.py` redirects `MARLOW_HOME` to a temp dir. Never hardcode `~/.marlow/` paths in tests.

**Profile tests**: When testing profile features, also mock `Path.home()` so that
`_get_profiles_root()` and `_get_default_marlow_home()` resolve within the temp dir.
Use the pattern from `tests/marlow_cli/test_profiles.py`:
```python
@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".marlow"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("MARLOW_HOME", str(home))
    return home
```

---

## Testing

**ALWAYS use `scripts/run_tests.sh`** — do not call `pytest` directly. The script enforces
hermetic environment parity with CI (unset credential vars, TZ=UTC, LANG=C.UTF-8,
`-n auto` xdist workers, in-tree subprocess-isolation plugin). Direct `pytest`
on a 16+ core developer machine with API keys set diverges from CI in ways
that have caused multiple "works locally, fails in CI" incidents (and the reverse).

```bash
scripts/run_tests.sh                                  # full suite, CI-parity
scripts/run_tests.sh tests/gateway/                   # one directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # one test
scripts/run_tests.sh -v --tb=long                     # pass-through pytest flags
scripts/run_tests.sh --no-isolate tests/foo/          # disable subprocess isolation (faster, for debugging)
```

### Subprocess-per-test isolation

Every test runs in a freshly-spawned Python subprocess via the in-tree plugin
at `tests/_isolate_plugin.py`. This means module-level dicts/sets and
ContextVars from one test cannot leak into the next — the historic
`_reset_module_state` autouse fixture is gone.

Implementation notes:

- The plugin uses `multiprocessing.get_context("spawn")` on supported Linux
  and macOS hosts (POSIX `fork` is not used).
- Per-test overhead is ~0.5–1.0s (Python startup + pytest collection). xdist
  parallelism amortizes this across cores; on a 20-core box the full suite
  finishes in roughly the same wall time as before, but flake-free.
- `isolate_timeout` (configured in `pyproject.toml`) caps each test at 30s.
  Hangs are killed and surfaced as a failure report.
- Pass `--no-isolate` to disable isolation — useful when debugging a single
  test interactively, or when you specifically want to verify state leakage.
- The plugin disables itself in child processes (sentinel envvar
  `MARLOW_ISOLATE_CHILD=1`), so there's no fork-bomb risk.

### Why the wrapper (and why the old "just call pytest" doesn't work)

Five real sources of local-vs-CI drift the script closes:

| | Without wrapper | With wrapper |
|---|---|---|
| Provider API keys | Whatever is in your env (auto-detects pool) | All `*_API_KEY`/`*_TOKEN`/etc. unset |
| HOME / `~/.marlow/` | Your real config+auth.json | Temp dir per test |
| Timezone | Local TZ (PDT etc.) | UTC |
| Locale | Whatever is set | C.UTF-8 |
| xdist workers | `-n auto` = all cores | `-n auto` (safe — subprocess isolation prevents cross-worker flakes) |

`tests/conftest.py` also enforces points 1-4 as an autouse fixture so ANY pytest
invocation (including IDE integrations) gets hermetic behavior — but the wrapper
is belt-and-suspenders.

### Running without the wrapper (only if you must)

If you can't use the wrapper (e.g. inside an IDE that shells pytest directly),
at minimum activate the venv. The isolation plugin loads automatically from
`addopts` in `pyproject.toml`, so you get the same per-test process isolation
either way.

```bash
source .venv/bin/activate   # or: source venv/bin/activate
python -m pytest tests/ -q
```

If you need to bypass isolation for fast feedback while debugging:

```bash
python -m pytest tests/agent/test_foo.py -q --no-isolate
```

Always run the full suite before pushing changes.

### Don't write change-detector tests

A test is a **change-detector** if it fails whenever data that is **expected
to change** gets updated — model catalogs, config version numbers,
enumeration counts, hardcoded lists of provider models. These tests add no
behavioral coverage; they just guarantee that routine source updates break
CI and cost engineering time to "fix."

**Do not write:**

```python
# catalog snapshot — breaks every model release
assert "gemini-2.5-pro" in _PROVIDER_MODELS["gemini"]
assert "MiniMax-M2.7" in models

# config version literal — breaks every schema bump
assert DEFAULT_CONFIG["_config_version"] == 21

# enumeration count — breaks every time a skill/provider is added
assert len(_PROVIDER_MODELS["huggingface"]) == 8
```

**Do write:**

```python
# behavior: does the catalog plumbing work at all?
assert "gemini" in _PROVIDER_MODELS
assert len(_PROVIDER_MODELS["gemini"]) >= 1

# behavior: does migration bump the user's version to current latest?
assert raw["_config_version"] == DEFAULT_CONFIG["_config_version"]

# invariant: no plan-only model leaks into the legacy list
assert not (set(moonshot_models) & coding_plan_only_models)

# invariant: every model in the catalog has a context-length entry
for m in _PROVIDER_MODELS["huggingface"]:
    assert m.lower() in DEFAULT_CONTEXT_LENGTHS_LOWER
```

The rule: if the test reads like a snapshot of current data, delete it. If
it reads like a contract about how two pieces of data must relate, keep it.
When a PR adds a new provider/model and you want a test, make the test
assert the relationship (e.g. "catalog entries all have context lengths"),
not the specific names.

Reviewers should reject new change-detector tests; authors should convert
them into invariants before re-requesting review.

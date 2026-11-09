---
name: marlow-agent
description: "Develop and troubleshoot the Marlow Agent checkout."
license: MIT
metadata:
  author: Teknium and Marlow Agent
  version: "2.1.0"
---

# Marlow Agent

This skill is for developers working in the Marlow source checkout.

Marlow Agent is an open-source AI agent framework that runs in your terminal and messaging platforms. It provides autonomous coding and task execution through tool calling, using the sole managed Codex app-server adapter on Linux and macOS. The current supported CLI version is 0.159.2.

Runtime implementation and limits: see `docs/architecture/phase-2-codex-runtime.md`
and `docs/architecture/phase-2-validation.md`. Marlow owns business authorization,
identity, memory and history; Codex owns reasoning, model retries and compaction.
Do not add auxiliary clients, provider fallback or a second tool reasoning loop.

What makes Marlow different:

- **Reusable skills** — request `/learn` to save a procedure for future sessions. Existing skills can be loaded and edited on request.
- **Persistent memory across sessions** — remembers who you are, your preferences, environment details, and lessons learned through local memory or Honcho.
- **Multi-platform gateway** — the same agent runs on Telegram, Discord, Slack, Feishu/Lark, Email, and signed webhooks with full tool access.
- **Flexible inference** — configure a Responses provider with tools in the profile’s native Codex TOML. Chat Completions-only endpoints are unsupported.
- **Profiles** — run multiple independent Marlow instances with isolated configs, sessions, skills, and memory.
- **Extensible** — plugins, MCP servers, custom tools, webhook triggers, cron scheduling, and the full Python ecosystem.

People use Marlow for software development, research, system administration, data analysis, content creation, home automation, and anything else that benefits from an AI agent with persistent context and full system access.

**This skill helps you work with Marlow Agent effectively** — setting it up, configuring features, spawning additional agent instances, troubleshooting issues, finding the right commands and settings, and understanding how the system works when you need to extend or contribute to it.

**Project guide:** https://github.com/stellan-lee/Marlow#readme

## Quick Start

```bash
# Install
curl -fsSL https://raw.githubusercontent.com/stellan-lee/Marlow/main/scripts/install.sh | bash

# Single query
marlow chat -q "What is the capital of France?"

# Setup wizard
marlow setup

# Change model/provider
marlow model

# Check health
marlow doctor
```

---

## CLI Reference

### Global Flags

```
marlow [flags] [command]

  --version, -V             Show version
  --resume, -r SESSION      Resume session by ID or title
  --continue, -c [NAME]     Resume by name, or most recent session
  --worktree, -w            Isolated git worktree mode (parallel agents)
  --profile, -p NAME        Use a named profile
  --yolo                    Skip dangerous command approval
  --pass-session-id         Include session ID in system prompt
```

With no arguments, Marlow shows command help.

### Chat

```
marlow chat [flags]
  -q, --query TEXT          Single query, non-interactive
  -m, --model MODEL         Codex or custom endpoint model id
  -t, --toolsets LIST       Comma-separated toolsets
  --provider PROVIDER       Force provider (openai-codex or custom)
  -v, --verbose             Verbose output
  -Q, --quiet               Suppress banner, spinner, tool previews
  --checkpoints             Enable filesystem checkpoints (/rollback)
  --source TAG              Session source tag (default: cli)
```

### Configuration

```
marlow setup [section]      Interactive wizard (model|terminal|gateway|tools|agent)
marlow model                Interactive model/provider picker
marlow config               View current config
marlow config edit          Open config.yaml in $EDITOR
marlow config set KEY VAL   Set a config value
marlow config path          Print config.yaml path
marlow config env-path      Print .env path
marlow config check         Check for missing/outdated config
marlow config migrate       Update config with new options
marlow login                Authenticate with OpenAI Codex
marlow logout               Clear Codex authentication
marlow doctor [--fix]       Check dependencies and config
marlow status [--all]       Show component status
```

### Tools & Skills

```
marlow tools                Interactive tool enable/disable (curses UI)
marlow tools list           Show all tools and status
marlow tools enable NAME    Enable a toolset
marlow tools disable NAME   Disable a toolset

marlow skills               Enable or disable installed local skills
# Add skills by placing SKILL.md packages under ~/.marlow/skills/
```

### MCP Servers

```
marlow mcp serve            Run Marlow as an MCP server
marlow mcp add NAME         Add an MCP server (--url or --command)
marlow mcp remove NAME      Remove an MCP server
marlow mcp list             List configured servers
marlow mcp test NAME        Test connection
marlow mcp configure NAME   Toggle tool selection
```

### Gateway (Messaging Platforms)

```
marlow gateway run          Start gateway foreground
marlow gateway install      Install as background service
marlow gateway start/stop   Control the service
marlow gateway restart      Restart the service
marlow gateway status       Check status
marlow gateway setup        Configure platforms
```

Supported platforms: Telegram, Discord, Slack, Feishu/Lark, Email, and signed webhooks.

Run `marlow gateway --help` for platform commands.

### Sessions

```
marlow sessions list        List recent sessions
marlow sessions browse      Interactive picker
marlow sessions export OUT  Export to JSONL
marlow sessions rename ID T Rename a session
marlow sessions delete ID   Delete a session
marlow sessions prune       Clean up old sessions (--older-than N days)
marlow sessions stats       Session store statistics
```

### Cron Jobs

```
marlow cron list            List jobs (--all for disabled)
marlow cron create SCHED    Create: '30m', 'every 2h', '0 9 * * *'
marlow cron edit ID         Edit schedule, prompt, delivery
marlow cron pause/resume ID Control job state
marlow cron run ID          Trigger on next tick
marlow cron remove ID       Delete a job
marlow cron status          Scheduler status
```

### Webhooks

```
marlow webhook subscribe N  Create route at /webhooks/<name>
marlow webhook list         List subscriptions
marlow webhook remove NAME  Remove a subscription
marlow webhook test NAME    Send a test POST
```

### Profiles

```
marlow profile list         List all profiles
marlow profile create NAME  Create (--clone, --clone-all, --clone-from)
marlow profile use NAME     Set sticky default
marlow profile delete NAME  Delete a profile
marlow profile show NAME    Show details
marlow profile alias NAME   Manage wrapper scripts
marlow profile rename A B   Rename a profile
marlow profile export NAME  Export to tar.gz
marlow profile import FILE  Import from archive
```

### Other

```
marlow insights [--days N]  Usage analytics
marlow update               Update to latest version
marlow pairing list/approve/revoke  DM authorization
marlow plugins list/install/remove  Plugin management
marlow honcho setup/status  Honcho memory integration (requires honcho plugin)
marlow memory setup/status/off  Memory provider config
marlow completion bash|zsh  Shell completions
marlow uninstall            Uninstall Marlow
```

---

## Slash Commands (In-Session)

Use these in a Gateway conversation. New commands land fairly
often; if something below looks stale, run `/help` in-session for the
authoritative list.
The registry of record is `marlow_cli/commands.py` — every consumer
(Telegram menu, Slack mapping, `/help`) derives from it.

### Session Control
```
/new (/reset)        Fresh session
/retry               Resend last message
/undo                Remove last exchange
/title [name]        Name the session
/compress            Manually compress context
/stop                Kill background processes
/rollback [N]        Restore filesystem checkpoint
/background <prompt> Run prompt in background
/queue <prompt>      Queue for next turn
/steer <prompt>      Inject a message after the next tool call without interrupting
/agents (/tasks)     Show active agents and running tasks
/resume [name]       Resume a named session
/goal [text|sub]     Set a standing goal Marlow works on across turns until achieved
                     (subcommands: status, pause, resume, clear)
```

### Configuration
```
/model [name]        Show or change model
/reasoning [level]   Set reasoning (none|minimal|low|medium|high|xhigh|show|hide)
/verbose             Cycle: off → new → all → verbose
/yolo                Toggle approval bypass
/footer [on|off]     Toggle gateway runtime-metadata footer on final replies
```

### Tools & Skills
```
/<skill-name>       Load a skill into session
/learn              Request creation of a skill
/reload-skills       Re-scan ~/.marlow/skills/ for added/removed skills
/reload-mcp          Reload MCP servers
```

### Gateway
```
/approve             Approve a pending command (gateway)
/deny                Deny a pending command (gateway)
/restart             Restart gateway (gateway)
/sethome             Set current chat as home channel (gateway)
/update              Update Marlow to latest (gateway)
/topic [sub]         Enable or inspect Telegram DM topic sessions (gateway)
/platforms (/gateway) Show platform connection status (gateway)
```

### Utility
```
/branch (/fork)      Branch the current session
/browser             Open CDP browser connection
```

### Info
```
/help                Show commands
/commands [page]     Browse all commands (gateway)
/usage               Token usage
/insights [days]     Usage analytics
/status              Session info (gateway)
/profile             Active profile info
/debug               Upload debug report (system info + logs) and get shareable links
```

### Exit
```
/quit (/exit, /q)    Exit CLI
```

---

## Key Paths & Config

```
~/.marlow/config.yaml       Main configuration
~/.marlow/.env              API keys and secrets
$MARLOW_HOME/skills/        Installed skills
~/.marlow/sessions/         Gateway routing index, request dumps, *.jsonl transcripts (and optional per-session JSON snapshots when sessions.write_json_snapshots: true)
~/.marlow/state.db          Canonical session store (SQLite + FTS5)
~/.marlow/logs/             Gateway and error logs
~/.marlow/auth.json         OAuth tokens and credential pools
~/.marlow/marlow-agent/     Source code (if git-installed)
```

Profiles use `~/.marlow/profiles/<name>/` with the same layout.

### Config Sections

Edit with `marlow config edit` or `marlow config set section.key value`.

| Section | Key options |
|---------|-------------|
| `model` | `default`, `provider`, `base_url`, `api_key`, `context_length` |
| `agent` | `max_turns` (90), `tool_use_enforcement` |
| `terminal` | `backend` (local/docker/ssh), `cwd`, `timeout` (180) |
| `compression` | `enabled`, `threshold` (0.50), `target_ratio` (0.20) |
| `display` | `tool_progress`, `show_reasoning`, `show_cost` |
| `memory` | `memory_enabled`, `user_profile_enabled`, `provider` |
| `security` | `tirith_enabled`, `website_blocklist` |
| `delegation` | `model`, `provider`, `base_url`, `api_key`, `max_iterations` (50), `reasoning_effort` |
| `checkpoints` | `enabled`, `max_snapshots` (50) |

Use `marlow config --help` and `marlow config edit` for the active configuration.

### Providers

Choose Codex or a custom/local endpoint with `marlow model` or `marlow setup`.

| Provider | Auth | Key env var |
|----------|------|-------------|
| OpenAI Codex | OAuth | `marlow login` |
| Custom/local endpoint | Config | `model.base_url` + `model.api_key` in config.yaml |

Use `marlow model` or `marlow setup` to inspect and configure providers.

### Toolsets

Enable/disable via `marlow tools` (interactive) or `marlow tools enable/disable NAME`.

| Toolset | What it provides |
|---------|-----------------|
| `web` | Web search and content extraction |
| `search` | Web search only (subset of `web`) |
| `browser` | Local Chromium automation or an existing CDP browser |
| `terminal` | Shell commands and process management |
| `file` | File read/write/search/patch |
| `code_execution` | Sandboxed Python execution |
| `vision` | Image analysis |
| `skills` | Skill browsing and management |
| `memory` | Persistent cross-session memory |
| `session_search` | Search past conversations |
| `delegation` | Subagent task delegation |
| `cronjob` | Scheduled task management |
| `clarify` | Ask user clarifying questions |
| `messaging` | Cross-platform message sending |
| `todo` | In-session task planning and tracking |
| `debugging` | Extra introspection/debug tools (off by default) |
| `safe` | Minimal, low-risk toolset for locked-down sessions |
| `discord` | Discord integration tools |
| `discord_admin` | Discord admin/moderation tools |
| `feishu_doc` | Feishu (Lark) document tools |
| `feishu_drive` | Feishu (Lark) drive tools |
| `rl` | Reinforcement learning tools (off by default) |
| `moa` | Mixture of Agents (off by default) |

Full enumeration lives in `toolsets.py` as the `TOOLSETS` dict; `_MARLOW_CORE_TOOLS` is the default bundle most platforms inherit from.

Tool changes take effect on `/reset` (new session). They do NOT apply mid-conversation to preserve prompt caching.

---

## Security & Privacy Toggles

Common "why is Marlow doing X to my output / tool calls / commands?" toggles — and the exact commands to change them. Most of these need a fresh session (`/reset` in chat, or start a new `marlow` invocation) because they're read once at startup.

### Secret redaction in tool output

Secret redaction is **on by default** — tool output (terminal stdout, `read_file`, web content, subagent summaries, etc.) is scanned for strings that look like API keys, tokens, and secrets before it enters the conversation context and logs. Leave it enabled for normal use:

```bash
marlow config set security.redact_secrets true       # keep enabled globally
```

**Restart required.** `security.redact_secrets` is snapshotted at import time — toggling it mid-session (e.g. via `export MARLOW_REDACT_SECRETS=false` from a tool call) will NOT take effect for the running process. Tell the user to change it in config from a terminal, then start a new session. This is deliberate — it prevents an LLM from flipping the toggle on itself mid-task.

Disable only when you deliberately need raw credential-like strings for debugging or redactor development:
```bash
marlow config set security.redact_secrets false
```

### PII redaction in gateway messages

Separate from secret redaction. When enabled, the gateway hashes user IDs and strips phone numbers from the session context before it reaches the model:

```bash
marlow config set privacy.redact_pii true    # enable
marlow config set privacy.redact_pii false   # disable (default)
```

### Command approval prompts

By default (`approvals.mode: manual`), Marlow prompts the user before running shell commands flagged as destructive (`rm -rf`, `git reset --hard`, etc.). The modes are:

- `manual` — always prompt (default)
- `smart` — use an auxiliary LLM to auto-approve low-risk commands, prompt on high-risk
- `off` — skip all approval prompts (equivalent to `--yolo`)

```bash
marlow config set approvals.mode smart       # recommended middle ground
marlow config set approvals.mode off         # bypass everything (not recommended)
```

Per-invocation bypass without changing config:
- `marlow --yolo …`
- `export MARLOW_YOLO_MODE=1`

Note: YOLO / `approvals.mode: off` does NOT turn off secret redaction. They are independent.

### Shell hooks allowlist

Some shell-hook integrations require explicit allowlisting before they fire. Managed via `~/.marlow/shell-hooks-allowlist.json` — prompted interactively the first time a hook wants to run.

### Disabling the web and browser tools

To keep the model away from network or media tools entirely, open `marlow tools` and toggle per-platform. Takes effect on next session (`/reset`). See the Tools & Skills section above.

---

## Spawning Additional Marlow Instances

Run additional Marlow processes as fully independent subprocesses — separate sessions, tools, and environments.

### When to Use This vs delegate_task

| | `delegate_task` | Spawning `marlow` process |
|-|-----------------|--------------------------|
| Isolation | Separate conversation, shared process | Fully independent process |
| Duration | Minutes (bounded by parent loop) | Hours/days |
| Tool access | Subset of parent's tools | Full tool access |
| Use case | Quick parallel subtasks | Long autonomous missions |

### One-Shot Mode

```
terminal(command="marlow chat -q 'Research GRPO papers and write summary to ~/research/grpo.md'", timeout=300)

# Background for long tasks:
terminal(command="marlow chat -q 'Set up CI/CD for ~/myapp'", background=true)
```

### Session Resume

```
# Resume most recent session
terminal(command="marlow chat --continue -q 'Continue with the next step'", timeout=120)

# Resume specific session
terminal(command="marlow chat --resume 20260225_143052_a1b2c3 -q 'Summarize progress'", timeout=120)
```

### Tips

- **Prefer `delegate_task` for quick subtasks** — less overhead than spawning a full process
- **Use `-w` (worktree mode)** when spawning agents that edit code — prevents git conflicts
- **Set timeouts** for one-shot mode — complex tasks can take 5-10 minutes
- **Use `marlow chat -q` for fire-and-forget** — no PTY needed
- **For scheduled tasks**, use the `cronjob` tool instead of spawning — handles delivery and retry

---

## Durable & Background Systems

Four systems run alongside the main conversation loop. Quick reference
here; full developer notes live in `AGENTS.md` and command-specific help.

### Delegation (`delegate_task`)

Synchronous subagent spawn — the parent waits for the child's summary
before continuing its own loop. Isolated context + terminal session.

- **Single:** `delegate_task(goal, context, toolsets)`.
- **Batch:** `delegate_task(tasks=[{goal, ...}, ...])` runs children in
  parallel, capped by `delegation.max_concurrent_children` (default 3).
- **Roles:** `leaf` (default; cannot re-delegate) vs `orchestrator`
  (can spawn its own workers, bounded by `delegation.max_spawn_depth`).
- **Not durable.** If the parent is interrupted, the child is
  cancelled. For work that must outlive the turn, use `cronjob` or
  `terminal(background=True, notify_on_complete=True)`.

Config: `delegation.*` in `config.yaml`.

### Cron (scheduled jobs)

Durable scheduler — `cron/jobs.py` + `cron/scheduler.py`. Drive it via
the `cronjob` tool, the `marlow cron` CLI (`list`, `add`, `edit`,
`pause`, `resume`, `run`, `remove`), or the `/cron` slash command.

- **Schedules:** duration (`"30m"`, `"2h"`), "every" phrase
  (`"every monday 9am"`), 5-field cron (`"0 9 * * *"`), or ISO timestamp.
- **Per-job knobs:** `skills`, `model`/`provider` override, `script`
  (pre-run data collection; `no_agent=True` makes the script the whole
  job), `context_from` (chain job A's output into job B), `workdir`
  (run in a specific dir with its `AGENTS.md` / `CLAUDE.md` loaded),
  multi-platform delivery.
- **Invariants:** 3-minute hard interrupt per run, `.tick.lock` file
  prevents duplicate ticks across processes, cron sessions pass
  `skip_memory=True` by default, and cron deliveries are framed with a
  header/footer instead of being mirrored into the target gateway
  session (keeps role alternation intact).

Run `marlow cron --help` for current usage.

## Troubleshooting

### Tool not available
1. `marlow tools` — check if toolset is enabled for your platform
2. Some tools need env vars (check `.env`)
3. `/reset` after enabling tools

### Model/provider issues
1. `marlow doctor` — check config and dependencies
2. `marlow login` — re-authenticate Codex OAuth
3. Check `.env` has the right API key

### Changes not taking effect
- **Tools/skills:** `/reset` starts a new session with updated toolset
- **Config changes:** In gateway: `/restart`. In CLI: exit and relaunch.
- **Code changes:** Restart the CLI or gateway process

### Skills not showing
1. `marlow skills` — verify local skill configuration
2. `marlow skills config` — check platform enablement
3. Load explicitly by name with `skill_view` or a Gateway `/<skill-name>` command

### Gateway issues
Check logs first:
```bash
grep -i "failed to send\|error" ~/.marlow/logs/gateway.log | tail -20
```

Common gateway problems:
- **Gateway dies on SSH logout**: Enable linger: `sudo loginctl enable-linger $USER`
- **Gateway crash loop**: Reset the failed state: `systemctl --user reset-failed marlow-gateway`

### Platform-specific issues
- **Discord bot silent**: Must enable **Message Content Intent** in Bot → Privileged Gateway Intents.
- **Slack bot only works in DMs**: Must subscribe to `message.channels` event. Without it, the bot ignores public channels.

### Auxiliary models not working
If `auxiliary` tasks (vision, compression, session_search) fail silently, the `auto` provider can't find a backend. Either set `OPENROUTER_API_KEY` or `GOOGLE_API_KEY`, or explicitly configure each auxiliary task's provider:
```bash
marlow config set auxiliary.vision.provider <your_provider>
marlow config set auxiliary.vision.model <model_name>
```

---

## Where to Find Things

| Looking for... | Location |
|----------------|----------|
| Config options | `marlow config edit` or `marlow config --help` |
| Available tools | `marlow tools list` |
| Slash commands | `/help` in session |
| Installed skills | `marlow skills` or `~/.marlow/skills/` |
| Provider setup | `marlow model` |
| Platform setup | `marlow gateway setup` |
| MCP servers | `marlow mcp list` |
| Profiles | `marlow profile list` |
| Cron jobs | `marlow cron list` |
| Memory | `marlow memory status` |
| Env variables | `marlow config env-path` |
| CLI commands | `marlow --help` |
| Gateway logs | `~/.marlow/logs/gateway.log` |
| Session files | `marlow sessions browse` (reads state.db) |
| Source code | `~/.marlow/marlow-agent/` |

---

## Contributor Quick Reference

For occasional contributors and PR authors. See `CONTRIBUTING.md` in the repository.

### Project Layout

```
marlow-agent/
├── run_agent.py          # AIAgent — core conversation loop
├── model_tools.py        # Tool discovery and dispatch
├── toolsets.py           # Toolset definitions
├── marlow_state.py       # SQLite session store
├── agent/                # Prompt builder, context compression, memory, model routing, credential pooling, skill dispatch
├── marlow_cli/           # CLI subcommands, config, setup, commands
│   ├── commands.py       # Slash command registry (CommandDef)
│   ├── config.py         # DEFAULT_CONFIG, env var definitions
│   └── main.py           # CLI entry point and argparse
├── tools/                # One file per tool
│   └── registry.py       # Central tool registry
├── gateway/              # Messaging gateway
│   └── platforms/        # Platform adapters (telegram, discord, etc.)
├── cron/                 # Job scheduler
└── tests/                # ~3000 pytest tests
```

Config: `~/.marlow/config.yaml` (settings), `~/.marlow/.env` (API keys).

### Adding a Tool (3 files)

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
    handler=lambda args, **kw: example_tool(
        param=args.get("param", ""), task_id=kw.get("task_id")),
    check_fn=check_requirements,
    requires_env=["EXAMPLE_API_KEY"],
)
```

**2. Add to `toolsets.py`** → `_MARLOW_CORE_TOOLS` list.

Auto-discovery: any `tools/*.py` file with a top-level `registry.register()` call is imported automatically — no manual list needed.

All handlers must return JSON strings. Use `get_marlow_home()` for paths, never hardcode `~/.marlow`.

### Adding a Slash Command

1. Add `CommandDef` to `COMMAND_REGISTRY` in `marlow_cli/commands.py`
2. Add the Gateway handler in `gateway/run.py`

All consumers (help text, Telegram menu, Slack mapping) derive from the central registry automatically.

### Agent Loop (High Level)

```
run_conversation():
  1. Build system prompt
  2. Loop while iterations < max:
     a. Call LLM (OpenAI-format messages + tool schemas)
     b. If tool_calls → dispatch each via handle_function_call() → append results → continue
     c. If text response → return
  3. Context compression triggers automatically near token limit
```

### Testing

```bash
python -m pytest tests/ -o 'addopts=' -q   # Full suite
python -m pytest tests/tools/ -q            # Specific area
```

- Tests auto-redirect `MARLOW_HOME` to temp dirs — never touch real `~/.marlow/`
- Run full suite before pushing any change
- Use `-o 'addopts='` to clear any baked-in pytest flags

### Extending the system prompt's execution-environment block

Factual guidance about the host OS, user home, cwd, terminal backend, and shell is emitted from `agent/prompt_builder.py::build_environment_hints()`. The convention:

- **Local terminal backend** → emit host info (OS, `$HOME`, cwd).
- **Remote terminal backend** (`docker` or `ssh`) → **suppress** host info entirely and describe only the backend. A live `uname`/`whoami`/`pwd` probe runs inside the backend via `tools.environments.get_environment(...).execute(...)`, cached per process in `_BACKEND_PROBE_CACHE`, with a static fallback if the probe times out.
- **Key fact for prompt authoring:** when `TERMINAL_ENV != "local"`, *every* file tool (`read_file`, `write_file`, `patch`, `search_files`) runs inside the backend container, not on the host. The system prompt must never describe the host in that case — the agent can't touch it.

### Commit Conventions

```
type: concise subject line

Optional body.
```

Types: `fix:`, `feat:`, `refactor:`, `docs:`, `chore:`

### Key Rules

- **Never break prompt caching** — don't change context, tools, or system prompt mid-conversation
- **Message role alternation** — never two assistant or two user messages in a row
- Use `get_marlow_home()` from `marlow_constants` for all paths (profile-safe)
- Config values go in `config.yaml`, secrets go in `.env`
- New tools need a `check_fn` so they only appear when requirements are met

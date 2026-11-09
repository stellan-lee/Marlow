# Contributing to Marlow Agent

Thank you for contributing to Marlow Agent! This guide covers everything you need: setting up your dev environment, understanding the architecture, deciding what to build, and getting your PR merged.

---

## Contribution Priorities

We value contributions in this order:

1. **Bug fixes** — crashes, incorrect behavior, data loss. Always top priority.
2. **Platform compatibility** — macOS and supported Linux distributions.
3. **Security hardening** — shell injection, prompt injection, path traversal, privilege escalation. See [Security](#security-considerations).
4. **Performance and robustness** — retry logic, error handling, graceful degradation.
5. **New skills** — but only broadly useful ones. See [Should it be a Skill or a Tool?](#should-it-be-a-skill-or-a-tool)
6. **New tools** — rarely needed. Most capabilities should be skills. See below.
7. **Documentation** — fixes, clarifications, new examples.

---

## Should it be a Skill or a Tool?

This is the most common question for new contributors. The answer is almost always **skill**.

### Make it a Skill when:

- The capability can be expressed as instructions + shell commands + existing tools
- It wraps an external CLI or API that the agent can call via `terminal` or `web_extract`
- It doesn't need custom Python integration or API key management baked into the agent
- Examples: arXiv search, git workflows, Docker management, PDF processing, email via CLI tools

### Make it a Tool when:

- It requires end-to-end integration with API keys, auth flows, or multi-component configuration managed by the agent harness
- It needs custom processing logic that must execute precisely every time (not "best effort" from LLM interpretation)
- It handles binary data, streaming, or real-time events that can't go through the terminal
- Examples: local browser automation, vision analysis (base64 image handling)

### Should the Skill be bundled?

New profiles start with an empty skill directory. `skills/` contains the
shipped reference skills for native MCP and webhook subscriptions. Users can
find and load them on request from the installation directory.

Put official skills that require manual installation in **`optional-skills/`**.
The Marlow contributor skill lives in **`.agents/skills/marlow-agent/`** for
developers working in the source checkout.

Specialized community skills should ship independently and can be copied into `~/.marlow/skills/` or a project-local skills directory.

---

## Memory Providers: Ship as a Standalone Plugin

**We are no longer accepting new memory providers into this repo.** Honcho is the bundled provider under `plugins/memory/`. If you want to add a new memory backend, publish it as a **standalone plugin repo** that users install into `~/.marlow/plugins/` (or via a pip entry point).

Standalone memory plugins:

- Implement the same `MemoryProvider` ABC (`agent/memory_provider.py`) — `sync_turn`, `prefetch`, `shutdown`, and optionally `post_setup(marlow_home, config)` for setup-wizard integration
- Use the same discovery system — `discover_memory_providers()` picks them up from user/project plugin directories and pip entry points
- Integrate with `marlow memory setup` via `post_setup()` — no need to touch core code
- Can register their own CLI subcommands via `register_cli(subparser)` in a `cli.py` file
- Get all the same lifecycle hooks and config plumbing as in-tree providers

PRs that add a new directory under `plugins/memory/` will be closed with a pointer to publish the provider as its own repo. Existing in-tree providers stay; bug fixes to them are welcome.

This isn't a quality bar — it's a coupling-and-maintenance decision. Memory providers are the most common plugin type and they shouldn't all live in this tree.

---

## Development Setup

### Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Git** | With `--recurse-submodules` support, and the `git-lfs` extension installed |
| **Python 3.11+** | uv will install it if missing |
| **uv** | Fast Python package manager ([install](https://docs.astral.sh/uv/)) |
| **Node.js 20+** | Needed for local browser tooling |

### Clone and install

```bash
git clone --recurse-submodules https://github.com/stellan-lee/Marlow.git
cd Marlow

# Create venv with Python 3.11
uv venv venv --python 3.11
export VIRTUAL_ENV="$(pwd)/venv"

# Install with all extras (messaging, cron, CLI menus, dev tools)
uv pip install -e ".[all,dev]"

# Optional: browser tools
npm install
```

### Configure for development

```bash
mkdir -p ~/.marlow/{cron,sessions,logs,memories,skills}
cp cli-config.yaml.example ~/.marlow/config.yaml
touch ~/.marlow/.env

# Add at minimum an LLM provider key:
echo "OPENROUTER_API_KEY=***" >> ~/.marlow/.env
```

### Run

```bash
# Symlink for global access
mkdir -p ~/.local/bin
ln -sf "$(pwd)/venv/bin/marlow" ~/.local/bin/marlow

# Verify
marlow doctor
marlow chat -q "Hello"
```

### Run tests

```bash
# Preferred — matches CI (hermetic env, 4 xdist workers); see AGENTS.md
scripts/run_tests.sh

# Alternative (activate the venv first). The wrapper is still recommended
# for parity with GitHub Actions before you open a PR:
pytest tests/ -v
```

---

## Project Structure

```
marlow-agent/
├── run_agent.py              # AIAgent class — core conversation loop, tool dispatch, session persistence
├── model_tools.py            # Tool orchestration (thin layer over tools/registry.py)
├── toolsets.py               # Tool groupings and presets (marlow-cli, marlow-telegram, etc.)
├── marlow_state.py           # SQLite session database with FTS5 full-text search, session titles
├── batch_runner.py           # Parallel batch processing for trajectory generation
│
├── agent/                    # Agent internals (extracted modules)
│   ├── prompt_builder.py         # System prompt assembly (identity, skills, context files, memory)
│   ├── context_compressor.py     # Auto-summarization when approaching context limits
│   ├── auxiliary_client.py       # Resolves auxiliary OpenAI clients (summarization, vision)
│   ├── display.py                # KawaiiSpinner, tool progress formatting
│   ├── model_metadata.py         # Model context lengths, token estimation
│   └── trajectory.py             # Trajectory saving helpers
│
├── marlow_cli/               # CLI command implementations
│   ├── main.py                   # Entry point, argument parsing, command dispatch
│   ├── config.py                 # Config management, migration, env var definitions
│   ├── setup.py                  # Interactive setup wizard
│   ├── auth.py                   # Provider resolution and Codex OAuth
│   ├── models.py                 # Retained model selection helpers
│   ├── banner.py                 # Welcome banner, ASCII art
│   ├── commands.py               # Central slash command registry (CommandDef), autocomplete, gateway helpers
│   ├── callbacks.py              # Interactive callbacks (clarify, sudo, approval)
│   ├── doctor.py                 # Diagnostics
│   ├── skills_config.py          # Local skills configuration helpers
│
├── tools/                    # Tool implementations (self-registering)
│   ├── registry.py               # Central tool registry (schemas, handlers, dispatch)
│   ├── approval.py               # Dangerous command detection + per-session approval
│   ├── terminal_tool.py          # Terminal orchestration (sudo, env lifecycle, backends)
│   ├── file_operations.py        # read_file, write_file, search, patch, etc.
│   ├── web_tools.py              # web_search and web_extract plugin dispatch
│   ├── vision_tools.py           # Image analysis via multimodal models
│   ├── delegate_tool.py          # Subagent spawning and parallel task execution
│   ├── code_execution_tool.py    # Sandboxed Python with RPC tool access
│   ├── session_search_tool.py    # Search past conversations with FTS5 + anchored windows
│   ├── cronjob_tools.py          # Scheduled task management
│   ├── skill_tools.py            # Skill search, load, manage
│   └── environments/             # Terminal execution backends
│       ├── base.py                   # BaseEnvironment ABC
│       ├── local.py, docker.py, ssh.py
│
├── gateway/                  # Messaging gateway
│   ├── run.py                    # GatewayRunner — platform lifecycle, message routing, cron
│   ├── config.py                 # Platform configuration resolution
│   ├── session.py                # Session store, context prompts, reset policies
│   └── platforms/                # Built-in platform adapters
│       ├── telegram.py, slack.py, feishu.py, email.py, webhook.py
│
├── scripts/                  # Install, release, and test helpers
│   └── install.sh                # Linux/macOS installer
│
├── .agents/skills/            # Skills for developers working in the checkout
├── skills/                   # Shipped skills for on-demand lookup
├── optional-skills/          # Shipped skills for manual installation
├── tests/                    # Test suite
│
├── cli-config.yaml.example   # Example configuration (copied to ~/.marlow/config.yaml)
└── AGENTS.md                 # Development guide for AI coding assistants
```

### User configuration (stored in `~/.marlow/`)

| Path | Purpose |
|------|---------|
| `~/.marlow/config.yaml` | Settings (model, terminal, toolsets, compression, etc.) |
| `~/.marlow/.env` | API keys and secrets |
| `~/.marlow/auth.json` | Codex OAuth credentials |
| `~/.marlow/skills/` | Installed local and user-requested skills |
| `~/.marlow/memories/` | Persistent memory (MEMORY.md, USER.md) |
| `~/.marlow/state.db` | SQLite session database |
| `~/.marlow/sessions/` | Gateway routing index (`sessions.json`), request-dump breadcrumbs, gateway `*.jsonl` transcripts, and (optionally) per-session JSON snapshots when `sessions.write_json_snapshots: true` is set. The per-session snapshots are off by default; state.db is canonical. |
| `~/.marlow/cron/` | Scheduled job data |

---

## Architecture Overview

### Core Loop

```
User message → AIAgent._run_agent_loop()
  ├── Build system prompt (prompt_builder.py)
  ├── Build API kwargs (model, messages, tools, reasoning config)
  ├── Call LLM (OpenAI-compatible API)
  ├── If tool_calls in response:
  │     ├── Execute each tool via registry dispatch
  │     ├── Add tool results to conversation
  │     └── Loop back to LLM call
  ├── If text response:
  │     ├── Persist session to DB
  │     └── Return final_response
  └── Context compression if approaching token limit
```

### Key Design Patterns

- **Self-registering tools**: Each tool file calls `registry.register()` at import time. `model_tools.py` triggers discovery by importing all tool modules.
- **Toolset grouping**: Tools are grouped into toolsets (`web`, `terminal`, `file`, `browser`, etc.) that can be enabled/disabled per platform.
- **Session persistence**: All conversations are stored in SQLite (`marlow_state.py`) with full-text search and unique session titles. Per-session JSON snapshots in `~/.marlow/sessions/` were superseded by the SQLite store and are off by default; opt back in with `sessions.write_json_snapshots: true` if you have external tooling that consumes the JSON files directly.
- **Ephemeral injection**: System prompts and prefill messages are injected at API call time, never persisted to the database or logs.
- **Provider abstraction**: The agent supports Codex OAuth and custom/local OpenAI-compatible endpoints.

---

## Code Style

- **PEP 8** with practical exceptions (we don't enforce strict line length)
- **Comments**: Only when explaining non-obvious intent, trade-offs, or API quirks. Don't narrate what the code does — `# increment counter` adds nothing
- **Error handling**: Catch specific exceptions. Log with `logger.warning()`/`logger.error()` — use `exc_info=True` for unexpected errors so stack traces appear in logs
- **Cross-platform**: Never assume Unix. See [Cross-Platform Compatibility](#cross-platform-compatibility)

---

## Adding a New Tool

Before writing a tool, ask: [should this be a skill instead?](#should-it-be-a-skill-or-a-tool)

Tools self-register with the central registry. Each tool file co-locates its schema, handler, and registration:

```python
"""my_tool — Brief description of what this tool does."""

import json
from tools.registry import registry


def my_tool(param1: str, param2: int = 10, **kwargs) -> str:
    """Handler. Returns a string result (often JSON)."""
    result = do_work(param1, param2)
    return json.dumps(result)


MY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "my_tool",
        "description": "What this tool does and when the agent should use it.",
        "parameters": {
            "type": "object",
            "properties": {
                "param1": {"type": "string", "description": "What param1 is"},
                "param2": {"type": "integer", "description": "What param2 is", "default": 10},
            },
            "required": ["param1"],
        },
    },
}


def _check_requirements() -> bool:
    """Return True if this tool's dependencies are available."""
    return True


registry.register(
    name="my_tool",
    toolset="my_toolset",
    schema=MY_TOOL_SCHEMA,
    handler=lambda args, **kw: my_tool(**args, **kw),
    check_fn=_check_requirements,
)
```

**Wire into a toolset (required):** Built-in tools are auto-discovered: any
`tools/*.py` file that contains a top-level `registry.register(...)` call is
imported by `discover_builtin_tools()` in `tools/registry.py` when `model_tools`
loads. There is **no** manual import list in `model_tools.py` to maintain.

You must still add the tool name to the appropriate list in `toolsets.py`
(for example `_MARLOW_CORE_TOOLS` or a dedicated toolset); otherwise the tool
registers but is never exposed to the agent. If you introduce a new toolset,
add it in `toolsets.py` and wire it into the relevant platform presets.

See `AGENTS.md` (section **Adding New Tools**) for profile-aware paths and
plugin vs core guidance.

---

## Adding a Skill

Shipped skills are organized by category under `skills/` and `optional-skills/`.

```
optional-skills/
├── research/
│   └── arxiv/
│       ├── SKILL.md              # Required: main instructions
│       └── scripts/              # Optional: helper scripts
│           └── search_arxiv.py
├── productivity/
│   └── ocr-and-documents/
│       ├── SKILL.md
│       ├── scripts/
│       └── references/
└── ...
```

### SKILL.md format

```markdown
---
name: my-skill
description: Brief description (shown in skill search results)
version: 1.0.0
author: Your Name
license: MIT
platforms: [macos, linux]          # Optional — restrict to specific OS platforms
                                   #   Valid: macos, linux, windows
                                   #   Omit to load on all platforms (default)
required_environment_variables:    # Optional — secure setup-on-load metadata
  - name: MY_API_KEY
    prompt: API key
    help: Where to get it
    required_for: full functionality
prerequisites:                     # Optional legacy runtime requirements
  env_vars: [MY_API_KEY]           #   Backward-compatible alias for required env vars
  commands: [curl, jq]             #   Advisory only; does not hide the skill
metadata:
  marlow:
    tags: [Category, Subcategory, Keywords]
    related_skills: [other-skill-name]
    fallback_for_toolsets: [web]       # Optional — show only when toolset is unavailable
    requires_toolsets: [terminal]      # Optional — show only when toolset is available
---

# Skill Title

Brief intro.

## When to Use
Trigger conditions — when should the agent load this skill?

## Quick Reference
Table of common commands or API calls.

## Procedure
Step-by-step instructions the agent follows.

## Pitfalls
Known failure modes and how to handle them.

## Verification
How the agent confirms it worked.
```

### Platform-specific skills

Skills can declare which OS platforms they support via the `platforms` frontmatter field. Skills with this field are automatically hidden from the system prompt, `skills_list()`, and slash commands on incompatible platforms.

```yaml
platforms: [macos]            # macOS only
platforms: [macos, linux]     # macOS and Linux
platforms: [windows]          # Windows only
```

If the field is omitted or empty, the skill loads on all platforms.

### Conditional skill activation

Skills can declare conditions that control when they appear in the system prompt, based on which tools and toolsets are available in the current session. This is primarily used for **fallback skills** — alternatives that should only be shown when a primary tool is unavailable.

Four fields are supported under `metadata.marlow`:

```yaml
metadata:
  marlow:
    fallback_for_toolsets: [web]      # Show ONLY when these toolsets are unavailable
    requires_toolsets: [terminal]     # Show ONLY when these toolsets are available
    fallback_for_tools: [web_search]  # Show ONLY when these specific tools are unavailable
    requires_tools: [terminal]        # Show ONLY when these specific tools are available
```

**Semantics:**
- `fallback_for_*`: The skill is a backup. It is **hidden** when the listed tools/toolsets are available, and **shown** when they are unavailable. Use this for free alternatives to premium tools.
- `requires_*`: The skill needs certain tools to function. It is **hidden** when the listed tools/toolsets are unavailable. Use this for skills that depend on specific capabilities (e.g., a skill that only makes sense with terminal access).
- If both are specified, both conditions must be satisfied for the skill to appear.
- If neither is specified, the skill is always shown (backward compatible).

**Examples:**

```yaml
# DuckDuckGo search — shown when the web toolset is unavailable
metadata:
  marlow:
    fallback_for_toolsets: [web]

# Smart home skill — only useful when terminal is available
metadata:
  marlow:
    requires_toolsets: [terminal]

# Browser helper — shown when the browser toolset is unavailable
metadata:
  marlow:
    fallback_for_toolsets: [browser]
```

The filtering happens at prompt build time in `agent/prompt_builder.py`. The `build_skills_system_prompt()` function receives the set of available tools and toolsets from the agent and uses `_skill_should_show()` to evaluate each skill's conditions.

### Skill setup metadata

Skills can declare secure setup-on-load metadata via the `required_environment_variables` frontmatter field. Missing values do not hide the skill from discovery; they trigger a CLI-only secure prompt when the skill is actually loaded.

```yaml
required_environment_variables:
  - name: TENOR_API_KEY
    prompt: Tenor API key
    help: Get a key from https://developers.google.com/tenor
    required_for: full functionality
```

The user may skip setup and keep loading the skill. Marlow only exposes metadata (`stored_as`, `skipped`, `validated`) to the model — never the secret value.

Legacy `prerequisites.env_vars` remains supported and is normalized into the new representation.

```yaml
prerequisites:
  env_vars: [TENOR_API_KEY]       # Legacy alias for required_environment_variables
  commands: [curl, jq]            # Advisory CLI checks
```

Gateway and messaging sessions never collect secrets in-band; they instruct the user to run `marlow setup` or update `~/.marlow/.env` locally.

**When to declare required environment variables:**
- The skill uses an API key or token that should be collected securely at load time
- The skill can still be useful if the user skips setup, but may degrade gracefully

**When to declare command prerequisites:**
- The skill relies on a CLI tool that may need separate installation
- Treat command checks as guidance, not discovery-time hiding

### Skill authoring standards (HARDLINE)

Every new or modernized skill — bundled, optional, or contributed — must meet these standards before merge. Reviewers reject PRs that violate them.

1. **`description` ≤ 60 characters, one sentence, ends with a period.** Long descriptions bloat the skill listing UI and dilute the model's attention when many skills are loaded. State the capability, not the implementation. No marketing words ("powerful", "comprehensive", "seamless", "advanced"). Don't repeat the skill name. Verify with:
   ```python
   import re, pathlib
   m = re.search(r'^description: (.*)$',
                 pathlib.Path('skills/<cat>/<name>/SKILL.md').read_text(),
                 re.MULTILINE)
   assert len(m.group(1)) <= 60, len(m.group(1))
   ```

   Good: `Search arXiv papers by keyword, author, category, or ID.`
   Bad: `A powerful and comprehensive skill that allows the agent to search arXiv for relevant academic papers using various criteria including keywords, authors, and categories.`

2. **Tools referenced in SKILL.md prose must be native Marlow tools or MCP servers the skill explicitly expects.** When the skill needs a capability, point at the proper tool by name in backticks: `` `terminal` ``, `` `web_extract` ``, `` `web_search` ``, `` `read_file` ``, `` `write_file` ``, `` `patch` ``, `` `search_files` ``, `` `vision_analyze` ``, `` `browser_navigate` ``, `` `delegate_task` ``, `` `cronjob` ``, `` `memory` ``, `` `skill_view` ``, `` `todo` ``, `` `execute_code` ``.

   Do NOT name shell utilities the agent already has wrapped:

   | Don't say | Say |
   |---|---|
   | `grep`, `rg` | `search_files` |
   | `cat`, `head`, `tail` | `read_file` |
   | `sed`, `awk` | `patch` |
   | `find`, `ls` | `search_files` (with `target='files'`) |
   | `curl` for content extraction | `web_extract` |
   | `echo > file`, `cat <<EOF` | `write_file` |

   If the skill depends on an MCP server, name the MCP server and document its setup in `## Prerequisites`. Third-party CLIs (e.g. `ffmpeg`, `gh`, a specific SDK) are fine to invoke from inside script files, but the prose should frame the interaction as "invoke through the `terminal` tool", not as a manual shell session.

3. **`platforms:` gating audited against actual script imports.** Skills that use POSIX-only primitives (`fcntl`, `termios`, `os.setsid`, `os.kill(pid, 0)` for liveness, `/proc`, hardcoded `/tmp` paths, `signal.SIGKILL`, bash heredocs, `osascript`, `apt`, `systemctl`) must declare their supported platforms via the `platforms:` frontmatter. Default posture is to fix it cross-platform first — `tempfile.gettempdir()`, `pathlib.Path`, `psutil.pid_exists()`, Python-level filtering instead of `grep`. Gate to a narrower set only when the dependency is genuinely platform-bound (e.g. `osascript` is macOS-only, `/proc` is Linux-only).

4. **`author` credits the human contributor first.** For external contributions, the contributor's real name + GitHub handle goes first (`Jane Doe (jane-doe)`); "Marlow Agent" is the secondary collaborator. If the contributor's commit shows "Marlow Agent" as author because they used Marlow to draft the skill, replace it with their actual name — credit the human, not the tool.

5. **SKILL.md body uses the modern section order.** `# <Skill> Skill` title, 2-3 sentence intro stating what it does and what it doesn't do, then:
   - `## When to Use` — trigger conditions
   - `## Prerequisites` — env vars, install steps, MCP setup, API key sourcing
   - `## How to Run` — canonical invocation through the `terminal` tool
   - `## Quick Reference` — flat command/API reference
   - `## Procedure` — numbered steps with copy-paste commands
   - `## Pitfalls` — known limits, rate limits, things that look broken but aren't
   - `## Verification` — single command that proves the skill works

   Target ~200 lines for a complex skill, ~100 lines for a simple one. Cut redundant intro fluff, marketing prose, and re-explanations of env vars already documented in `## Prerequisites`.

6. **Scripts go in `scripts/`, references in `references/`, templates in `templates/`.** Don't expect the model to inline-write parsers, XML walkers, or non-trivial logic every call — ship a helper script. Reference scripts from SKILL.md by path relative to the skill directory.

7. **Tests live at `tests/skills/test_<skill>_skill.py`** and use only stdlib + pytest + `unittest.mock`. No live network calls. Run via `scripts/run_tests.sh tests/skills/test_<skill>_skill.py -q`. Must pass under the hermetic CI env (no API keys leaking through). Use `monkeypatch` and `tmp_path` for any env-var or filesystem dependencies.

8. **`.env.example` additions are isolated to a clearly delimited block.** Don't touch the surrounding file — contributor-supplied `.env.example` versions are usually stale, and edits outside the skill's own block will be dropped during salvage. Comment all values with `#` (it's documentation, not live config).

### Skill guidelines

- **No external dependencies unless absolutely necessary.** Prefer stdlib Python, curl, and existing Marlow tools (`web_extract`, `terminal`, `read_file`).
- **Progressive disclosure.** Put the most common workflow first. Edge cases and advanced usage go at the bottom.
- **Include helper scripts** for XML/JSON parsing or complex logic — don't expect the LLM to write parsers inline every time.
- **Test it.** Run `marlow --toolsets skills -q "Use the X skill to do Y"` and verify the agent follows the instructions correctly.

---

## Security Considerations

Marlow has terminal access. Security matters.

### Existing protections

| Layer | Implementation |
|-------|---------------|
| **Sudo password piping** | Uses `shlex.quote()` to prevent shell injection |
| **Dangerous command detection** | Regex patterns in `tools/approval.py` with user approval flow |
| **Cron prompt injection** | Scanner in `tools/cronjob_tools.py` blocks instruction-override patterns |
| **Write deny list** | Protected paths (`~/.ssh/authorized_keys`, `/etc/shadow`) resolved via `os.path.realpath()` to prevent symlink bypass |
| **Code execution sandbox** | `execute_code` child process runs with API keys stripped from environment |
| **Container hardening** | Docker: all capabilities dropped, no privilege escalation, PID limits, size-limited tmpfs |

### When contributing security-sensitive code

- **Always use `shlex.quote()`** when interpolating user input into shell commands
- **Resolve symlinks** with `os.path.realpath()` before path-based access control checks
- **Don't log secrets.** API keys, tokens, and passwords should never appear in log output
- **Catch broad exceptions** around tool execution so a single failure doesn't crash the agent loop
- **Test on supported platforms** if your change touches file paths, process management, or shell commands

If your PR affects security, note it explicitly in the description.

### Dependency pinning policy (supply chain hardening)

After the [litellm supply chain compromise](https://github.com/BerriAI/litellm/issues/24512) in March 2026 and the [Mini Shai-Hulud worm campaign](https://socket.dev/blog/tanstack-npm-packages-compromised-mini-shai-hulud-supply-chain-attack) in May 2026, all dependencies must follow these rules:

| Source type | Required treatment | Rationale |
|---|---|---|
| **PyPI package** | `>=floor,<next_major` | PyPI versions are immutable once published, but new versions can be pushed into your range. A `<next_major` ceiling stops a 1.x install from upgrading to a malicious 2.0.0. |
| **Git URL** | Full commit SHA | Branches and tags are mutable refs; SHA is content-addressed. |
| **GitHub Actions** | Full commit SHA + version comment | Action tags are mutable refs (e.g. tj-actions/changed-files March 2025). Pin as `uses: owner/action@<sha>  # vX.Y.Z` |
| **CI-only pip installs** | `==exact` | Hermetic CI builds; churn is acceptable. |

**Every new PyPI dependency in a PR must have a `<next_major` upper bound.** PRs adding unbounded `>=X.Y.Z` specs will be rejected by reviewers. Run `uv lock --check` after dependency changes.

**How to determine the ceiling:**
- If the package is at version `1.x.y`, use `<2`.
- If the package is at version `0.x.y` (pre-1.0), use `<0.(current_minor + 2)` — e.g. if current is `0.29.x`, use `<0.32`. This gives ~2 minor versions of headroom while keeping the window small enough that a hostile takeover version is unlikely to land inside it.
- Exception: packages with very stable APIs (e.g. `aiohttp-socks`) can use `<1` at reviewer discretion.

**Examples:**
```toml
# ✅ Correct — post-1.0
"openai>=2.21.0,<3"
"pydantic>=2.12.5,<3"

# ✅ Correct — pre-1.0 (tight minor window)
"asyncpg>=0.29,<0.32"
"aiosqlite>=0.20,<0.23"
"honcho-ai>=2.0,<3"

# ❌ Rejected — no upper bound
"some-package>=1.2.3"

# ❌ Rejected — too tight (blocks legitimate patches)
"some-package==1.2.3"

# ❌ Rejected — too loose for pre-1.0 (allows 80 minor versions)
"some-package>=0.20,<1"
```

**Reference PRs:** #2796 (litellm removal), #2810 (upper bounds pass), #9801 (SHA pinning + supply-chain-audit CI).

---

## Pull Request Process

### Branch naming

```
fix/description        # Bug fixes
feat/description       # New features
docs/description       # Documentation
test/description       # Tests
refactor/description   # Code restructuring
```

### Before submitting

1. **Run tests**: `scripts/run_tests.sh` (recommended; same as CI) or `pytest tests/ -v` with the project venv activated
2. **Test manually**: Run `marlow` and exercise the code path you changed
3. **Check platform impact**: If you touch file I/O, process management, or terminal handling, consider macOS and Linux
4. **Keep PRs focused**: One logical change per PR. Don't mix a bug fix with a refactor with a new feature.

### PR description

Include:
- **What** changed and **why**
- **How to test** it (reproduction steps for bugs, usage examples for features)
- **What platforms** you tested on
- Reference any related issues

### Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>
```

| Type | Use for |
|------|---------|
| `fix` | Bug fixes |
| `feat` | New features |
| `docs` | Documentation |
| `test` | Tests |
| `refactor` | Code restructuring (no behavior change) |
| `chore` | Build, CI, dependency updates |

Scopes: `cli`, `gateway`, `tools`, `skills`, `agent`, `install`, `security`, etc.

Examples:
```
fix(cli): prevent crash in save_config_value when model is a string
feat(gateway): improve Feishu topic routing
fix(security): prevent shell injection in sudo password piping
test(tools): add unit tests for file_operations
```

---

## Reporting Issues

- Use [GitHub Issues](https://github.com/stellan-lee/Marlow/issues)
- Include: OS, Python version, Marlow version (`marlow version`), full error traceback
- Include steps to reproduce
- Check existing issues before creating duplicates
- For security vulnerabilities, please report privately

---

## Community

- **GitHub Discussions**: For design proposals and architecture discussions
- **Skills**: Share standalone `SKILL.md` packages with the community

---

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).

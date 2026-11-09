<p align="center">
  <img src="assets/banner.png" alt="Marlow Agent" width="100%">
</p>

# Marlow Agent ☤

<p align="center">
  <a href="https://github.com/stellan-lee/Marlow"><img src="https://img.shields.io/badge/Source-GitHub-181717?style=for-the-badge&logo=github" alt="Source repository"></a>
  <a href="https://github.com/stellan-lee/Marlow/issues"><img src="https://img.shields.io/badge/Issues-GitHub-blue?style=for-the-badge&logo=github" alt="Issue tracker"></a>
  <a href="https://github.com/stellan-lee/Marlow/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
</p>

**An AI agent with persistent memory, local tools, and messaging integrations.** Marlow searches past conversations, uses reusable skills, and can create skills at your request. Run it locally, on a cloud VM, or through a messaging gateway.

Use a Codex subscription or any local/custom OpenAI-compatible endpoint. Switch with `marlow model`.

<table>
<tr><td><b>Scriptable command line</b></td><td>Run a single query with image input and session history, or use a stdout-only invocation in a script.</td></tr>
<tr><td><b>Lives where you do</b></td><td>Telegram, Discord, Slack, Feishu, Teams, email, and signed webhooks through one Gateway.</td></tr>
<tr><td><b>Persistent memory and skills</b></td><td>Local memory, project-scoped work experience and decisions, FTS5 session search, and user-initiated skill creation. <a href="https://github.com/plastic-labs/honcho">Honcho</a> dialectic user modeling. Compatible with the <a href="https://agentskills.io">agentskills.io</a> open standard.</td></tr>
<tr><td><b>Scheduled automations</b></td><td>Built-in cron scheduler with delivery to any platform. Daily reports, nightly backups, weekly audits — all in natural language, running unattended.</td></tr>
<tr><td><b>Delegates and parallelizes</b></td><td>Spawn isolated subagents for parallel workstreams. Write Python scripts that call tools via RPC, collapsing multi-step pipelines into zero-context-cost turns.</td></tr>
<tr><td><b>Runs where you work</b></td><td>Local, Docker, and SSH terminal backends cover direct development, isolated containers, and remote hosts.</td></tr>
<tr><td><b>Research-ready</b></td><td>Batch trajectory generation, trajectory compression for training the next generation of tool-calling models.</td></tr>
</table>

---

## Quick Install

### Linux and macOS

```bash
curl -fsSL https://raw.githubusercontent.com/stellan-lee/Marlow/main/scripts/install.sh | bash
```

If you are upgrading a pre-Marlow installation, run the installer directly
instead of using the old `hermes update` command. It copies missing user state
from `~/.hermes` into `~/.marlow`, excludes the obsolete source checkout, and
keeps the original directory available for rollback. Use `marlow update` after
that one-time migration.

After installation:

```bash
source ~/.bashrc    # reload shell (or: source ~/.zshrc)
marlow chat -q "Hello"
```

---

## Getting Started

```bash
marlow chat -q "Hello" # Run one query and save the session
marlow -z "Hello"      # Return final text on stdout for scripts
marlow model        # Choose your LLM provider and model
marlow tools        # Configure which tools are enabled
marlow config set   # Set individual config values
marlow gateway      # Start the messaging gateway (Telegram, Discord, etc.)
marlow setup        # Run the full setup wizard (configures everything at once)
marlow update       # Update to the latest version
marlow doctor       # Diagnose any issues
```

Use `marlow --help`, `marlow setup`, and the guides in this repository for configuration details.

---

## Querying Marlow

Use `marlow chat -q "Describe this" --image ./photo.png` for a single query.
Pass `--resume` or `--continue` with a query to pick up an existing session.
A terminal can prompt for approval during a query. A non-interactive query
denies actions that require an interactive decision.

Use `marlow -z "Summarize the current repository"` for stdout-only output in
scripts. It honors explicit authorization and denies actions that require
interactive approval. Use `marlow gateway setup` and `marlow gateway start`
for ongoing messaging conversations. Run `marlow --help` for operator commands.

---

## Documentation

- [README](README.md) — install, quick start, and command overview
- [Contributing guide](CONTRIBUTING.md) — development setup and project conventions
- [Security policy](SECURITY.md) — trust model and vulnerability reporting
- [Admin approvals](docs/admin-approvals.md) — privileged approval routing
- [Telegram guest digital twin](docs/telegram-digital-twin.md) — guest prompts and scoped owner experience
- [Network isolation](docs/security/network-egress-isolation.md) — container egress controls

---

## Contributing

We welcome contributions! See the [Contributing Guide](CONTRIBUTING.md) for development setup, code style, and PR process.

Quick start for contributors:

```bash
git clone https://github.com/stellan-lee/Marlow.git
cd Marlow
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[all,dev]"
scripts/run_tests.sh
```

---

## Community

- 🐛 [Issues](https://github.com/stellan-lee/Marlow/issues)
- 🔌 [computer-use-linux](https://github.com/avifenesh/computer-use-linux) — Linux desktop-control MCP server for Marlow and other MCP hosts, with AT-SPI accessibility trees, Wayland/X11 input, screenshots, and compositor window targeting.

---

## License

MIT — see [LICENSE](LICENSE).

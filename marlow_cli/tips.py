"""Random CLI tips for retained Marlow features."""

import random


TIPS = [
    "/background <prompt> runs a task in a separate session.",
    "/branch forks the current session without losing the original.",
    "/compress manually summarizes long conversation context.",
    "/rollback lists filesystem checkpoints created before file changes.",
    "/queue <prompt> queues a message without interrupting the current turn.",
    "/undo removes the last user and assistant exchange.",
    "/reasoning changes reasoning effort; /reasoning show displays it.",
    "/model switches between Codex and configured custom/local endpoints.",
    "/learn creates a reusable local skill from your description.",
    "/goal sets a persistent objective that can continue automatically.",
    "/reload-mcp refreshes MCP servers without restarting Marlow.",
    "/reload-skills rescans local SKILL.md files.",
    "@file:path/to/file.py injects a file directly into your message.",
    "@folder:src/ injects a directory tree listing.",
    "@diff and @staged inject current Git changes.",
    "Ctrl+C interrupts the current agent turn.",
    "marlow chat --continue -q resumes the most recent CLI session.",
    "marlow chat -w -q runs a query in an isolated Git worktree.",
    "marlow chat -q runs one non-interactive query and exits.",
    "marlow doctor --fix diagnoses and repairs local configuration issues.",
    "marlow logs -f follows the agent log in real time.",
    "marlow profile create work creates an isolated Marlow profile.",
    "marlow gateway install configures the gateway as a system service.",
    "marlow webhook subscribe creates signed event-driven webhook routes.",
    "marlow mcp add configures stdio or HTTP MCP servers.",
    "MCP servers can use OAuth with PKCE and per-server tool filtering.",
    "Custom providers live under model.providers in config.yaml.",
    "LM Studio, Ollama, and vLLM work through custom OpenAI-compatible endpoints.",
    "Codex OAuth supports the Responses API and Codex app-server runtime.",
    "Local, Docker, and SSH terminal backends are available.",
    "execute_code runs Python that can call selected Marlow tools.",
    "delegate_task supports foreground and background subagents.",
    "mixture_of_agents can aggregate several retained model routes.",
    "web_search supports Brave Search and DuckDuckGo/DDGS.",
    "browser_navigate uses local Chromium or a configured CDP connection.",
    "Telegram, Discord, Slack, Feishu, email, and signed webhooks are retained gateway connectors.",
    "send_message can reach connected gateway conversations.",
    "Honcho adds an optional external memory provider.",
    "Local MEMORY.md and USER.md files provide lightweight persistent memory.",
    "Plugins can register tools, commands, prompts, and lifecycle hooks.",
    "User and project plugin directories remain supported.",
    "skills.external_dirs loads SKILL.md files from additional local directories.",
    "Set compression.threshold to control automatic context compression.",
    "Set security.website_blocklist.enabled to block domains from web tools.",
    "Set display.platforms for per-gateway presentation overrides.",
    "Configure the profile Codex model provider in runtime/codex/config.toml.",
    "The gateway supports typing indicators, streaming edits, and progress updates.",
    "Gateway profiles can run as independent services.",
    "Filesystem checkpoints use shadow repositories and never modify project Git history.",
    "Session search uses the local SQLite full-text index.",
    "Trajectory saving captures tool-use traces for optional training data.",
]


def get_random_tip(exclude_recent: int = 0) -> str:
    """Return a random tip; ``exclude_recent`` is reserved for future use."""
    del exclude_recent
    return random.choice(TIPS)

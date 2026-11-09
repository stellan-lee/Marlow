"""Marlow's public SDK facade over the sole Codex app-server runtime.

Marlow retains business state and authorization. Codex owns reasoning, model
requests, tools' reasoning loop and compaction. There is no native fallback.
"""

from __future__ import annotations
import json
import logging
from pathlib import Path
import shutil
import threading
import time
import uuid
from contextlib import contextmanager

from agent.codex_environment import (
    CodexEnvironment,
    CodexConfigurationError,
    CodexAuthenticationError,
    SUPPORTED_CODEX_VERSION,
)
from agent.codex_memory import MemoryLifecycle
from agent.codex_state import RuntimeState
from agent.memory_manager import (
    StreamingContextScrubber,
    build_memory_context_block,
    sanitize_context,
)
from marlow_constants import (
    get_marlow_home,
    set_marlow_home_override,
    reset_marlow_home_override,
)

logger = logging.getLogger(__name__)
_CAPACITY_LOCK = threading.Lock()
_CAPACITY = {}


class AIAgent(MemoryLifecycle):
    """Compatibility interface for Gateway, SDK, CLI, Cron and task workers."""

    def __init__(
        self,
        model="",
        *,
        session_id=None,
        platform=None,
        session_db=None,
        enabled_toolsets=None,
        disabled_toolsets=None,
        **options,
    ):
        from marlow_cli.config import load_config

        self.profile = get_marlow_home().absolute()
        self.config = load_config()
        self.runtime_config = (self.config.get("runtime") or {}).get("codex") or {}
        import os

        backend = os.environ.get("TERMINAL_ENV") or (
            self.config.get("terminal") or {}
        ).get("backend", "local")
        if backend != "local":
            raise CodexConfigurationError(
                "Codex runtime currently supports local sandbox execution only. "
                "Docker/SSH execution is not migrated; it will never silently run on the host. "
                "Use an explicitly authorized local project or retain your current release for remote execution."
            )
        settings = dict(self.runtime_config)
        if options.get("authorized_projects"):
            settings["projects"] = list(settings.get("projects", [])) + list(
                options["authorized_projects"]
            )
        from agent.codex_environment import conversation_workspace_scope

        self._conversation_shared = options.get("conversation_shared", False)
        scope_user = None if self._conversation_shared else options.get("user_id")
        if options.get("runtime_task_id") and not any(
            options.get(key) is not None for key in ("user_id", "chat_id", "thread_id")
        ):
            scope_user = "task:" + options["runtime_task_id"]
        workspace_scope = conversation_workspace_scope(
            channel=platform or "sdk",
            user=scope_user,
            chat=options.get("chat_id"),
            thread=options.get("thread_id"),
        )
        if options.get("workspace_scope") is not None:
            workspace_scope = options["workspace_scope"]
        self._workspace_scope = workspace_scope
        self.environment = CodexEnvironment.build(
            self.profile, settings=settings, workspace_scope=workspace_scope
        )
        # Model/provider truth is Codex TOML. Old credentials and provider URL
        # arguments must not silently select a different backend.
        for key in ("api_key", "base_url"):
            if options.get(key):
                raise CodexConfigurationError(
                    f"{key} is retired; configure the profile Codex provider"
                )
        if options.get("api_mode") not in (None, "", "codex_app_server"):
            raise CodexConfigurationError("Only codex_app_server is supported")
        if options.get("fallback_providers"):
            raise CodexConfigurationError(
                "Fallback providers are retired; configure Codex"
            )
        from marlow_cli.runtime_provider import resolve_runtime_provider

        resolve_runtime_provider()
        self.model = model or self.environment.config.get("model", "")
        self.provider = self.environment.config.get("model_provider", "openai")
        if options.get("provider") not in (
            None,
            "",
            "auto",
            "codex",
            "openai-codex",
            self.provider,
        ):
            raise CodexConfigurationError(
                "Provider selection belongs in the profile Codex config.toml"
            )
        provider_config = self.environment.config.get("model_providers", {}).get(
            self.provider, {}
        )
        self.base_url = provider_config.get(
            "base_url", "https://api.openai.com/v1" if self.provider == "openai" else ""
        )
        self.api_key = None
        self.api_mode = "codex_app_server"
        self.session_id = session_id or "marlow_" + uuid.uuid4().hex
        self.platform = platform or "sdk"
        from agent.runtime_cwd import resolve_agent_cwd

        selected = options.get("project_path") or self.runtime_config.get("project")
        current_scope = resolve_agent_cwd().resolve()
        if not selected and any(
            current_scope == Path(p) or current_scope.is_relative_to(Path(p))
            for p, _ in self.environment.projects
        ):
            selected = str(current_scope)
        self.session_cwd = str(self.environment.workspace)
        if selected:
            candidate = Path(selected).resolve()
            if not any(
                candidate == Path(p) or candidate.is_relative_to(Path(p))
                for p, _ in self.environment.projects
            ):
                raise PermissionError(
                    "Selected project is not in the explicit project grants"
                )
            self.session_cwd = str(candidate)
        from agent.codex_environment import select_project_grants

        self.environment = select_project_grants(
            self.environment, selected, options.get("authorized_projects") or []
        )
        self.enabled_toolsets = enabled_toolsets
        self.disabled_toolsets = disabled_toolsets
        self.quiet_mode = options.get("quiet_mode", False)
        self.ephemeral_system_prompt = options.get("ephemeral_system_prompt")
        self.prefill_messages = options.get("prefill_messages") or []
        self.reasoning_config = options.get("reasoning_config")
        self.request_overrides = options.get("request_overrides") or {}
        self.max_tokens = options.get("max_tokens")
        self.output_schema = options.get("output_schema")
        self.max_iterations = max(1, min(int(options.get("max_iterations", 90)), 1000))
        self.turn_timeout = min(
            float(options.get("timeout") or 3600),
            float(self.runtime_config.get("turn_timeout", 600)),
            3600,
        )
        self._session_db = session_db
        self._owns_db = session_db is None
        self._conversation_persistence_policy = options.get(
            "conversation_persistence_policy", "session"
        )
        self._parent_session_id = options.get("parent_session_id")
        self._cron_execution_owner = options.get("cron_execution_owner")
        self._session_messages = []
        self._codex_session = None
        self._bridge = None
        self._closed = False
        self._cancel_event = options.get("cancel_event")
        self._sampling_budget_lock = threading.Lock()
        self._sampling_remaining = 0
        self._interrupt_requested = False
        self._cancel_generation = 0
        self._active_children = []
        self._active_children_lock = threading.RLock()
        self._delegate_depth = 0
        self._business_uncertain = False
        self._dispatched_write = False
        self._failed_mutations = {}
        self._decision_turn = None
        self._experience_turn = None
        self._current_task_id = None
        self._current_turn_id = None
        self._stream_callback = None
        self._print_fn = None
        self._stream_scrubber = StreamingContextScrubber()
        self.background_review_callback = None
        self._skip_memory = options.get("skip_memory", False)
        self._memory_post_turn_prefetch_enabled = False
        self._activity_ts = time.monotonic()
        self._activity_description = "idle"
        self._turn_lock = threading.Lock()
        self._business_threads = set()
        self._business_threads_lock = threading.RLock()
        from agent.tool_guardrails import (
            ToolCallGuardrailController,
            ToolCallGuardrailConfig,
        )

        self._tool_guardrails = ToolCallGuardrailController(
            ToolCallGuardrailConfig.from_mapping(
                self.config.get("tool_loop_guardrails")
            )
        )
        self._user_turn_count = 0
        self.suppress_status_output = False
        for name in (
            "user_id",
            "user_id_alt",
            "user_name",
            "chat_id",
            "chat_name",
            "chat_type",
            "thread_id",
            "gateway_session_key",
        ):
            setattr(self, "_" + name, options.get(name))
        for name in (
            "tool_progress_callback",
            "tool_start_callback",
            "tool_complete_callback",
            "thinking_callback",
            "reasoning_callback",
            "clarify_callback",
            "step_callback",
            "stream_delta_callback",
            "interim_assistant_callback",
            "status_callback",
            "tool_gen_callback",
        ):
            setattr(self, name, options.get(name))
        self.scope = {
            "profile": str(self.profile),
            "user": None if self._conversation_shared else self._user_id,
            "channel": self.platform,
            "chat": self._chat_id,
            "thread": self._thread_id,
            "conversation": self._gateway_session_key,
            "session": self.session_id,
            "project": self.session_cwd,
            "task": options.get("runtime_task_id"),
        }
        self.state = RuntimeState(self.profile, self.scope)
        for name in (
            "api_calls",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
        ):
            setattr(self, "session_" + name, 0)
        self.session_estimated_cost_usd = None
        self.session_cost_source = "codex"
        self.session_cost_status = "unknown"
        self.usage_available = False
        self.context_length = None
        self.compression_count = 0
        from model_tools import get_tool_definitions
        from agent.codex_bridge import BUSINESS_TOOLS
        from tools.registry import registry
        from marlow_cli.plugins import get_plugin_manager

        with self._profile_context():
            configured_mcp = self.config.get("mcp_servers") or {}
            if configured_mcp and enabled_toolsets != []:
                from tools.mcp_tool import discover_mcp_tools

                discover_mcp_tools()
            get_plugin_manager().discover_and_load()
            definitions = (
                get_tool_definitions(
                    enabled_toolsets=enabled_toolsets,
                    disabled_toolsets=disabled_toolsets,
                    quiet_mode=True,
                    skip_tool_search_assembly=True,
                )
                or []
            )
            manager = get_plugin_manager()
            manager.discover_and_load()
            plugin_names = manager._plugin_tool_names
            allowed_mcp_toolsets = {
                "mcp-" + str(name)
                for name, entry in configured_mcp.items()
                if isinstance(entry, dict) and entry.get("enabled", True) is not False
            }
            self.tools = []
            for tool in definitions:
                name = tool.get("function", {}).get("name")
                entry = registry.get_entry(name)
                if (
                    name in BUSINESS_TOOLS
                    or (entry and entry.toolset in allowed_mcp_toolsets)
                    or name in plugin_names
                ):
                    self.tools.append(tool)
        self.valid_tool_names = {t["function"]["name"] for t in self.tools}
        from tools.todo_tool import TodoStore

        self._todo_store = TodoStore()
        from agent.business_memory import initialize_memory

        with self._profile_context():
            initialize_memory(self, self.config, options)
        self._automatic_memory_ingestion_enabled = options.get(
            "automatic_memory_ingestion_enabled",
            self._automatic_memory_ingestion_enabled,
        )
        self._memory_write_mode = options.get(
            "memory_write_mode", self._memory_write_mode
        )
        # Automatic background review belongs to neither startup nor idle.
        # Explicit background jobs still use this same facade.
        self._background_review_enabled = False

    @contextmanager
    def _profile_context(self):
        token = set_marlow_home_override(self.profile)
        from agent.runtime_cwd import set_session_cwd, _SESSION_CWD

        cwd_token = set_session_cwd(self.session_cwd)
        try:
            from agent.codex_file_scope import file_scope

            with file_scope([
                self.environment.workspace,
                *(path for path, _ in self.environment.projects),
            ]):
                yield
        finally:
            _SESSION_CWD.reset(cwd_token)
            reset_marlow_home_override(token)

    def business_schemas(self):
        return [t["function"] for t in self.tools]

    def _dispatch_business_tool(self, name, args):
        if self._interrupt_requested or self._closed:
            raise PermissionError("Turn cancelled")
        if self._business_uncertain and self._tool_can_write(name, args):
            raise PermissionError(
                "A prior business operation needs verification; further writes are blocked"
            )
        self._touch_activity("business tool: " + name)
        with self._profile_context():
            if name == "vision_analyze":
                raw = str(args.get("image_url") or "")
                if raw.startswith(("https://", "http://")):
                    from model_tools import _run_async
                    from tools.vision_tools import _vision_analyze_native

                    return _run_async(
                        _vision_analyze_native(raw, args.get("question", ""))
                    )
                from agent.codex_execution import execute

                return execute(self, name, dict(args, path=raw.removeprefix("file://")))
            if name == "browser_navigate":
                from urllib.parse import urlsplit

                if urlsplit(str(args.get("url") or "")).scheme.lower() not in {
                    "http",
                    "https",
                }:
                    raise PermissionError(
                        "Browser navigation supports HTTP/HTTPS only in this runtime"
                    )
            if name == "browser_console" and args.get("expression") is not None:
                raise PermissionError(
                    "Host browser JavaScript evaluation is not available in the isolated runtime"
                )
            if name == "cronjob":
                from agent.codex_cron_scope import dispatch_cron

                return dispatch_cron(self, args)
            if name in {"terminal", "read_file", "write_file", "patch", "search_files"}:
                from agent.codex_execution import execute

                return execute(self, name, args)
            if name == "memory":
                from tools.memory_tool import memory_tool

                if (
                    self._memory_write_mode in {"off", "none", "read-only"}
                    and args.get("action") != "read"
                ):
                    raise PermissionError("Memory writes disabled for this scope")
                target = args.get("target", "memory")
                if (target == "memory" and not self._memory_enabled) or (
                    target == "user" and not self._user_profile_enabled
                ):
                    raise PermissionError("This memory target is disabled")
                if self._memory_store is None:
                    raise PermissionError("Profile memory not enabled in this scope")
                result = memory_tool(
                    action=args.get("action"),
                    target=args.get("target", "memory"),
                    content=args.get("content"),
                    old_text=args.get("old_text"),
                    store=self._memory_store,
                )
                if self._memory_manager and args.get("action") in {"add", "replace"}:
                    self._memory_manager.on_memory_write(
                        args["action"],
                        args.get("target", "memory"),
                        args.get("content", ""),
                    )
                return result
            if name == "todo":
                from tools.todo_tool import todo_tool

                return todo_tool(
                    todos=args.get("todos"),
                    merge=args.get("merge", False),
                    store=self._todo_store,
                )
            if name == "experience_decision":
                from tools.experience_decision_tool import experience_decision_tool

                return experience_decision_tool(args, runtime=self._decision_turn)
            if name == "session_search":
                return self._search_history(args)
            if name in {"skills_list", "skill_view"}:
                return self._skill_tool(name, args)
            if name == "clarify":
                if not self.clarify_callback:
                    raise PermissionError(
                        "Clarification requires an interactive channel"
                    )
                from tools.clarify_tool import clarify_tool

                return clarify_tool(
                    question=args.get("question", ""),
                    choices=args.get("choices"),
                    callback=self.clarify_callback,
                )
            if name == "delegate_task":
                from tools.delegate_tool import delegate_task

                return delegate_task(**args, parent_agent=self)
            if self._memory_manager and self._memory_manager.has_tool(name):
                return self._memory_manager.handle_tool_call(name, args)
            if name == "send_message":
                from gateway.turn_context import get_current_turn

                if get_current_turn() is None:
                    raise PermissionError(
                        "Messaging requires trusted routing and approval context"
                    )
            from tools.mcp_tool import authorize_sampling
            from model_tools import handle_function_call

            with authorize_sampling(self, name):
                return handle_function_call(
                    name,
                    args,
                    task_id=self._current_task_id,
                    enabled_tools=self.valid_tool_names,
                    enabled_toolsets=self.enabled_toolsets,
                    disabled_toolsets=self.disabled_toolsets,
                )

    @staticmethod
    def _tool_can_write(name, args):
        if name in {"memory", "cronjob", "experience_decision"}:
            return args.get("action") not in {
                "read",
                "list",
                "search",
                "show",
                "related",
                "status",
            }
        return name not in {
            "web_search",
            "web_extract",
            "browser_snapshot",
            "browser_get_images",
            "vision_analyze",
            "browser_vision",
            "browser_console",
            "read_file",
            "search_files",
            "session_search",
            "skills_list",
            "skill_view",
            "clarify",
        }

    def _skill_tool(self, name, args):
        # Skills are data exposed on explicit lookup; never an executable
        # auto-discovery source. Shipped Phase 1 survivors remain on demand.
        roots = [self.profile / "skills"]
        from agent.bundled_skills import (
            iter_on_demand_skills,
            is_unchanged_legacy_skill,
        )

        shipped = {
            p.parent.name: p for p in iter_on_demand_skills(trusted_runtime=True)
        }
        roots += [Path(p) for p in self.runtime_config.get("skills", [])]
        from agent.skill_utils import get_external_skills_dirs

        roots += get_external_skills_dirs()
        skills = dict(shipped)
        for root in roots:
            if root.is_symlink():
                raise PermissionError("Skill source symlinks are not allowed")
            for path in root.rglob("SKILL.md") if root.is_dir() else []:
                if any(p.is_symlink() for p in (path, *path.parents)):
                    continue
                if root == self.profile / "skills" and is_unchanged_legacy_skill(
                    path, root
                ):
                    continue
                skills[path.parent.name] = path
        from marlow_cli.plugins import get_plugin_manager

        for qualified, entry in get_plugin_manager()._plugin_skills.items():
            path = Path(entry["path"])
            if path.is_file() and not any(
                part.is_symlink() for part in (path, *path.parents)
            ):
                skills[qualified] = path
        if name == "skills_list":
            return json.dumps({"skills": sorted(skills)})
        selected = args.get("name") or args.get("skill")
        if selected not in skills:
            raise PermissionError("Skill not explicitly authorized")
        return skills[selected].read_text(encoding="utf-8")

    def _search_history(self, args):
        db = self._get_session_db_for_recall()
        if db is None:
            raise RuntimeError("Session database unavailable")
        # Local profile owner may search owned history. Remote callers only
        # access this conversation; model-supplied session/user IDs grant none.
        allowed = (
            None
            if not self._user_id and self.platform in {"cli", "sdk"}
            else {self.session_id}
        )
        sid = args.get("session_id")
        if sid and allowed is not None and sid not in allowed:
            raise PermissionError("History outside this conversation is not authorized")
        if sid:
            rows = db.get_messages(sid)
        elif args.get("query"):
            rows = db.search_messages(
                args["query"], limit=min(int(args.get("limit", 3)), 20) * 10
            )
            if allowed is not None:
                rows = [r for r in rows if r.get("session_id") in allowed]
        else:
            rows = db.get_messages(self.session_id)
        return json.dumps(
            {
                "success": True,
                "results": rows[: min(max(int(args.get("limit", 3)), 1), 20)],
            },
            default=str,
        )

    def _hydrate_todo_store(self, history: list[dict]) -> None:
        """
        Recover todo state from conversation history.

        The gateway creates a fresh AIAgent per message, so the in-memory
        TodoStore is empty. We scan the history for the most recent todo
        tool response and replay it to reconstruct the state.
        """
        # Walk history backwards to find the most recent todo tool response
        last_todo_response = None
        for msg in reversed(history):
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            # Quick check: todo responses contain "todos" key
            if '"todos"' not in content:
                continue
            try:
                data = json.loads(content)
                if "todos" in data and isinstance(data["todos"], list):
                    last_todo_response = data["todos"]
                    break
            except (json.JSONDecodeError, TypeError):
                continue

        if last_todo_response:
            # Replay the items into the store (replace mode)
            self._todo_store.write(last_todo_response, merge=False)
            logger.debug("Restored todo state: items=%d", len(last_todo_response))

    def _get_session_db_for_recall(self):
        if self._session_db is None and self._conversation_persistence_policy != "none":
            from marlow_state import SessionDB

            self._session_db = SessionDB(self.profile / "state.db")
        return self._session_db

    def _memory_context(self, user_message, raw_user_message, turn_origin):
        context = []
        if self._memory_store:
            targets = ([] if not self._memory_enabled else ["memory"]) + (
                [] if not self._user_profile_enabled else ["user"]
            )
            context += [
                self._memory_store.format_for_system_prompt(k) or "" for k in targets
            ]
        if self._memory_manager:
            provider_context = self._memory_manager.build_system_prompt()
            if provider_context:
                context.append(build_memory_context_block(provider_context))
            from agent.memory_recall import recall_external

            recalled = recall_external(self, user_message, self._session_messages)
            if recalled:
                context.append(build_memory_context_block(recalled))
        consolidation = (self.config.get("memory") or {}).get("consolidation") or {}
        if (
            not self._skip_memory
            and consolidation.get("enabled")
            and consolidation.get("legacy_recall_enabled") is True
        ):
            try:
                from agent.memory_consolidation_runner import (
                    retrieve_consolidated_memory,
                )

                recalled = retrieve_consolidated_memory(
                    scope_id=self._user_id or "local-owner",
                    scope_type="user" if self._user_id else "profile",
                    query=user_message,
                    limit=int(consolidation.get("retrieval_limit", 8)),
                )
                if recalled:
                    context.append(build_memory_context_block(recalled))
            except Exception as exc:
                logger.debug("Consolidated recall failed: %s", type(exc).__name__)
        from agent.experience.runtime import (
            prepare_experience_turn,
            prepare_decision_capture_turn,
        )

        self._experience_turn = prepare_experience_turn(
            self, raw_user_message=raw_user_message, turn_origin=turn_origin
        )
        self._decision_turn = prepare_decision_capture_turn(
            self, raw_user_message=raw_user_message, turn_origin=turn_origin
        )
        if self._experience_turn:
            context.append(
                self._experience_turn.context_for_request(
                    provider=self.provider, base_url=self.base_url
                )
            )
        return "\n\n".join(x for x in context if x)

    def _new_session(self, thread_id):
        from agent.codex_bridge import BusinessBridge
        from agent.transports.codex_app_server import check_codex_binary
        from agent.transports.codex_app_server_session import CodexAppServerSession

        binary = self.runtime_config.get("binary") or shutil.which("codex")
        if not binary:
            raise RuntimeError("Codex CLI required; install supported Codex 0.159.2")
        ok, version = check_codex_binary(binary)
        if not ok or version != ".".join(map(str, SUPPORTED_CODEX_VERSION)):
            raise RuntimeError(
                "Unsupported Codex CLI; this release supports 0.159.2 only"
            )
        self._bridge = BusinessBridge(self)
        from tools.terminal_tool import _get_approval_callback

        self._codex_session = CodexAppServerSession(
            environment=self.environment,
            codex_bin=binary,
            thread_id=thread_id,
            bridge=self._bridge,
            model=self.model or None,
            instructions=self.ephemeral_system_prompt,
            approval_callback=_get_approval_callback(),
            clarify_callback=self.clarify_callback,
            on_event=self._on_codex_event,
            max_tool_calls=self.max_iterations,
            reasoning=self.reasoning_config,
            output_schema=self.output_schema,
            on_thread=self.state.thread,
            on_turn=self._on_turn_started,
        )

    def _on_turn_started(self, turn_id):
        self._current_turn_id = turn_id
        self.state.turn(turn_id)

    def run_conversation(
        self,
        user_message,
        system_message=None,
        conversation_history=None,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
        raw_user_message=None,
        turn_origin=None,
        runtime_action=None,
    ):
        if runtime_action not in (None, "compact"):
            raise ValueError("Unsupported runtime action")
        if self._closed:
            raise RuntimeError("Agent is closed")
        if not user_message:
            raise ValueError("An explicit input trigger is required")
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise InterruptedError("Business task cancelled before dispatch")
        cancellation_generation = self._cancel_generation
        # One process per active session; bounded profile concurrency. No model
        # requests occur during facade initialization or while waiting for work.
        with _CAPACITY_LOCK:
            gate = _CAPACITY.setdefault(
                str(self.profile),
                threading.BoundedSemaphore(
                    max(1, min(int(self.runtime_config.get("max_concurrent", 4)), 16))
                ),
            )
        if not gate.acquire(timeout=30):
            raise TimeoutError("Profile runtime concurrency limit reached")
        run_id = None
        turn = None
        try:
            with self._turn_lock, self._profile_context(), self.state.lease():
                if cancellation_generation != self._cancel_generation:
                    raise InterruptedError(
                        "Input cancelled while waiting for this conversation"
                    )
                self._stream_callback = stream_callback
                self._stream_scrubber.reset()
                self._tool_guardrails.reset_for_turn()
                self._interrupt_requested = False
                self._business_uncertain = False
                self._dispatched_write = False
                self._failed_mutations = {}
                self._decision_turn = None
                self._experience_turn = None
                with self._sampling_budget_lock:
                    self._sampling_remaining = max(
                        0,
                        min(
                            int(
                                self.runtime_config.get(
                                    "max_sampling_calls_per_turn", 3
                                )
                            ),
                            10,
                        ),
                    )
                if self._conversation_persistence_policy == "none":
                    self.release_clients()
                    self.state = RuntimeState(
                        self.profile, dict(self.scope, ephemeral_turn=uuid.uuid4().hex)
                    )
                thread_id, _, status = self.state.mapping()
                if status in {"running", "needs_verification"}:
                    raise RuntimeError(
                        "Previous turn needs verification; automatic replay is disabled"
                    )
                from tools.terminal_tool import _task_env_overrides

                if _task_env_overrides.get(task_id):
                    raise CodexConfigurationError(
                        "Legacy per-task execution backends are not supported by this Codex adapter"
                    )
                self.state.claim_scope()
                if self._codex_session is None:
                    self._new_session(thread_id)
                db = self._get_session_db_for_recall()
                if db:
                    previous = db.get_session(self.session_id)
                    if (
                        previous
                        and thread_id
                        and previous.get("cwd")
                        and Path(previous["cwd"]).resolve()
                        != Path(self.session_cwd).resolve()
                    ):
                        raise PermissionError(
                            "Session belongs to a different project; start a new session with explicit project authorization"
                        )
                    if (
                        previous
                        and not self._conversation_shared
                        and str(previous.get("user_id") or "")
                        != str(self._user_id or "")
                    ):
                        raise PermissionError("Session belongs to a different user")
                    db.create_session(
                        self.session_id,
                        self.platform,
                        model=self.model,
                        user_id=self._user_id,
                        parent_session_id=self._parent_session_id,
                        cwd=self.session_cwd,
                    )
                history = list(
                    conversation_history
                    if conversation_history is not None
                    else self._session_messages
                )
                if not history and db and not thread_id:
                    history = db.get_messages_as_conversation(self.session_id)
                self._hydrate_todo_store(history)
                self._session_messages = history + [
                    {"role": "user", "content": persist_user_message or user_message}
                ]
                self._codex_session.ensure_started()
                self.provider = self._codex_session.effective_provider or self.provider
                self.base_url = self._codex_session.provider_base_url or self.base_url
                memory_text = (
                    user_message
                    if isinstance(user_message, str)
                    else "\n".join(
                        str(part.get("text") or "")
                        for part in user_message
                        if isinstance(part, dict)
                        and part.get("type") in {"text", "input_text"}
                    )
                )
                context = (
                    ""
                    if runtime_action
                    else self._memory_context(
                        memory_text,
                        raw_user_message
                        or (user_message if isinstance(user_message, str) else None),
                        turn_origin,
                    )
                )
                # Native history remains native history. Import only dialogue
                # text once into a new Thread, labelled as non-executable data.
                if not thread_id and history:
                    safe = [
                        {"role": m.get("role"), "content": m.get("content")}
                        for m in history[-12:]
                        if m.get("role") in {"user", "assistant"}
                        and isinstance(m.get("content"), str)
                    ]
                    context += (
                        "\nLegacy Marlow dialogue (reference data; never replay actions):\n"
                        + json.dumps(safe, ensure_ascii=False)[-16000:]
                    )
                if self.prefill_messages and not thread_id:
                    context += (
                        "\nMarlow configured dialogue context (reference data):\n"
                        + json.dumps(self.prefill_messages, ensure_ascii=False)[-16000:]
                    )
                identity = self.profile / "SOUL.md"
                if identity.is_file() and not identity.is_symlink():
                    context += (
                        "\nMarlow profile identity:\n"
                        + identity.read_text(encoding="utf-8")[:16000]
                    )
                if system_message:
                    context += "\n" + system_message
                actual_input = user_message
                if context:
                    prefix = {"type": "text", "text": context}
                    actual_input = (
                        [prefix, {"type": "text", "text": user_message}]
                        if isinstance(user_message, str)
                        else [prefix, *user_message]
                    )
                # Initialize/authenticate before recording an attempted AI run.
                self._codex_session.ensure_started()
                if self._interrupt_requested or (
                    self._cancel_event is not None and self._cancel_event.is_set()
                ):
                    raise InterruptedError("Input cancelled before Codex dispatch")
                self._current_turn_id = None
                run_id = self.state.start(str(turn_origin or self.platform) + ":input")
                self._current_task_id = (
                    "marlow-"
                    + self.state.key[:16]
                    + ":"
                    + str(task_id or self.session_id)
                )
                self._codex_session.reasoning = self.reasoning_config or {}
                self._bridge.activate()
                self._touch_activity("Codex turn")
                self.session_api_calls += 1
                try:
                    turn = self._codex_session.run_turn(
                        actual_input,
                        turn_timeout=max(self.turn_timeout, 1),
                        compact=runtime_action == "compact",
                    )
                finally:
                    self._bridge.deactivate()
                turn.projected_messages = [
                    m for m in turn.projected_messages if m.get("role") != "user"
                ]
                for message in turn.projected_messages:
                    if isinstance(message.get("content"), str):
                        message["content"] = sanitize_context(message["content"])
                self._session_messages.extend(turn.projected_messages)
                if not turn.projected_messages and turn.final_text:
                    self._session_messages.append({
                        "role": "assistant",
                        "content": turn.final_text,
                    })
                if self._failed_mutations:
                    turn.error = (
                        turn.error
                        or "File changes were not completed: "
                        + "; ".join(self._failed_mutations.values())[:1000]
                    )
                if self._business_uncertain:
                    turn.error = (
                        turn.error
                        or "A business operation needs verification; further writes were stopped"
                    )
                completed = not turn.interrupted and not turn.error
                final = sanitize_context(turn.final_text)
                if turn.should_retire or turn.interrupted:
                    self.interrupt("Parent turn stopped")
                    self.release_clients()
                if (
                    completed
                    and not runtime_action
                    and self._automatic_memory_ingestion_enabled
                ):
                    self._sync_external_memory_for_turn(
                        original_user_message=memory_text,
                        final_response=final,
                        interrupted=False,
                        messages=self._session_messages,
                    )
                if db:
                    # Canonical raw history excludes recalled context and secrets.
                    from agent.redact import redact_sensitive_text

                    tool_names = {
                        call.get("id"): call.get("function", {}).get("name")
                        for message in self._session_messages
                        for call in message.get("tool_calls") or []
                    }
                    for message in self._session_messages[len(history) :]:
                        db.append_message(
                            self.session_id,
                            message["role"],
                            redact_sensitive_text(
                                message.get("content") or "", force=True
                            )
                            if isinstance(message.get("content"), str)
                            else json.dumps(message.get("content"), ensure_ascii=False),
                            tool_calls=message.get("tool_calls"),
                            tool_call_id=message.get("tool_call_id"),
                            tool_name=message.get("tool_name")
                            or tool_names.get(message.get("tool_call_id")),
                        )
                if db and turn.usage:
                    last = turn.usage.get("last") or {}
                    db.update_token_counts(
                        self.session_id,
                        input_tokens=max(
                            0,
                            last.get("inputTokens", 0)
                            - last.get("cachedInputTokens", 0),
                        ),
                        output_tokens=last.get("outputTokens", 0),
                        cache_read_tokens=last.get("cachedInputTokens", 0),
                        reasoning_tokens=last.get("reasoningOutputTokens", 0),
                        model=self.model,
                        api_call_count=1,
                        cost_status="unknown",
                        cost_source="none",
                    )
                self.state.finish(
                    run_id,
                    thread_id=turn.thread_id,
                    turn_id=turn.turn_id,
                    status="completed"
                    if completed
                    else (
                        "needs_verification"
                        if self._dispatched_write or self._business_uncertain
                        else "stopped"
                    ),
                    error_type="runtime_error" if turn.error else None,
                    usage=turn.usage,
                )
                run_id = None
                self._user_turn_count += 1
                if self._conversation_persistence_policy == "none":
                    self.release_clients()
                return {
                    "final_response": final,
                    "messages": self._session_messages,
                    "completed": completed,
                    "partial": not completed,
                    "error": turn.error,
                    "api_calls": 1,
                    "codex_thread_id": turn.thread_id,
                    "codex_turn_id": turn.turn_id,
                    "usage": turn.usage,
                    "usage_available": turn.usage is not None,
                    "status": "completed"
                    if completed
                    else (
                        "needs_verification"
                        if self._dispatched_write or self._business_uncertain
                        else ("auth_required" if turn.auth_required else "stopped")
                    ),
                }
        except Exception as exc:
            if run_id is not None:
                self.state.finish(
                    run_id,
                    thread_id=self.state.mapping()[0],
                    turn_id=self._current_turn_id,
                    status="needs_verification"
                    if self._dispatched_write or self._business_uncertain
                    else "stopped",
                    error_type=type(exc).__name__,
                )
            self.release_clients()
            from agent.redact import redact_sensitive_text

            return {
                "final_response": "",
                "messages": self._session_messages,
                "completed": False,
                "partial": True,
                "api_calls": 0 if run_id is None else 1,
                "error": self.environment.redact(str(exc)),
                "status": "auth_required"
                if isinstance(exc, CodexAuthenticationError)
                else ("cancelled" if isinstance(exc, InterruptedError) else "failed"),
            }
        finally:
            gate.release()

    def _on_codex_event(self, event):
        self._touch_activity(event.get("method", "Codex event"))
        params = event.get("params") or {}
        if event.get("method") == "item/agentMessage/delta":
            callback = self._stream_callback or self.stream_delta_callback
            if callback:
                visible = self._stream_scrubber.feed(params.get("delta", ""))
                if visible:
                    callback(visible)
        if event.get("method") == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            totals = usage.get("total") or {}
            for field, key in [
                ("input_tokens", "inputTokens"),
                ("output_tokens", "outputTokens"),
                ("total_tokens", "totalTokens"),
                ("cache_read_tokens", "cachedInputTokens"),
                ("reasoning_tokens", "reasoningOutputTokens"),
            ]:
                if key in totals:
                    setattr(self, "session_" + field, totals[key])
            self.session_input_tokens = max(
                0, totals.get("inputTokens", 0) - totals.get("cachedInputTokens", 0)
            )
            self.session_prompt_tokens = totals.get("inputTokens", 0)
            self.session_completion_tokens = self.session_output_tokens
            self.usage_available = True
            self.context_length = usage.get("modelContextWindow")

    def chat(self, message, stream_callback=None):
        result = self.run_conversation(message, stream_callback=stream_callback)
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result["final_response"]

    def interrupt(self, message=None):
        self._cancel_generation += 1
        self._interrupt_requested = True
        self._interrupt_message = message
        with self._business_threads_lock:
            from tools.interrupt import set_interrupt

            for thread_id in self._business_threads:
                set_interrupt(True, thread_id)
        if self._gateway_session_key:
            from tools.approval import clear_session
            from gateway.pending_delivery import clear_session as clear_deliveries

            clear_session(self._gateway_session_key)
            clear_deliveries(self._gateway_session_key)
        if self._codex_session:
            self._codex_session.request_interrupt()
        with self._active_children_lock:
            for child in list(self._active_children):
                child.interrupt(message)

    def clear_interrupt(self):
        self._interrupt_requested = False

    @property
    def is_interrupted(self):
        return self._interrupt_requested

    def steer(self, text):
        # New input queues at the conversation lease; it cannot silently
        # change a pending operation's authorization.
        return False

    def reset_session_state(self, session_id=None, **kwargs):
        if self._turn_lock.locked():
            raise RuntimeError("Stop the active turn before resetting the session")
        self.commit_memory_session(self._session_messages)
        self.release_clients()
        self.session_id = session_id or "marlow_" + uuid.uuid4().hex
        self.scope = dict(self.scope, session=self.session_id)
        self.state = RuntimeState(self.profile, self.scope)
        self._session_messages = []
        self._user_turn_count = 0
        self._current_turn_id = None
        self._business_uncertain = False
        self._dispatched_write = False
        self._interrupt_requested = False
        from tools.todo_tool import TodoStore

        self._todo_store = TodoStore()
        for name in (
            "api_calls",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
        ):
            setattr(self, "session_" + name, 0)
        self.usage_available = False
        # Providers receive the real new session identity, rather than retaining
        # another conversation's cached memory state.
        self.shutdown_memory_provider()
        from agent.business_memory import initialize_memory

        with self._profile_context():
            initialize_memory(self, self.config, {"skip_memory": self._skip_memory})

    def switch_model(self, new_model, new_provider=None, **kwargs):
        if self._turn_lock.locked():
            raise RuntimeError("Stop the active turn before changing models")
        if new_provider and new_provider != self.provider:
            raise ValueError("Select the provider in the profile Codex config.toml")
        if kwargs.get("api_key") or kwargs.get("base_url"):
            raise ValueError(
                "Model credentials belong in the profile Codex config.toml"
            )
        self.model = str(new_model).strip()
        if not self.model:
            raise ValueError("Model identifier is required")
        self.release_clients()
        if self._session_db:
            self._session_db.update_session_model(self.session_id, self.model)

    def _touch_activity(self, description):
        self._activity_ts = time.monotonic()
        self._activity_description = description

    def get_activity_summary(self):
        idle = time.monotonic() - self._activity_ts
        return {
            "idle_seconds": idle,
            "seconds_since_activity": idle,
            "description": self._activity_description,
            "last_activity": self._activity_description,
            "last_activity_desc": self._activity_description,
            "api_call_count": self.session_api_calls,
            "max_iterations": self.max_iterations,
            "executing_tool": self._activity_description.startswith("business tool:"),
            "current_tool": self._activity_description,
        }

    def get_rate_limit_state(self):
        return None

    def compact(self):
        result = self.run_conversation(
            "Operator requested Codex context compaction.",
            turn_origin="operator_compaction",
            runtime_action="compact",
        )
        if result.get("completed"):
            self.compression_count += 1
            return True
        return False

    def release_clients(self):
        if self._codex_session:
            self._codex_session.close()
            self._codex_session = None
        if self._bridge:
            self._bridge.close()
            self._bridge = None

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.interrupt()
        self.release_clients()
        self.shutdown_memory_provider(self._session_messages)
        if self._current_task_id:
            from tools.browser_tool import cleanup_browser

            cleanup_browser(self._current_task_id)
        if self._owns_db and self._session_db:
            self._session_db.close()

    def _convert_to_trajectory_format(self, messages, user_query, completed):
        return list(messages)


def main(user_query="", **kwargs):
    agent = AIAgent(**kwargs)
    try:
        print(agent.chat(user_query))
    finally:
        agent.close()


if __name__ == "__main__":
    import fire

    fire.Fire(main)

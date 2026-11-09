"""Existing memory initialization, independent of model clients."""

import logging
from marlow_constants import get_marlow_home

logger = logging.getLogger(__name__)


def initialize_memory(agent, config, options):
    _agent_cfg = config
    skip_memory = options.get("skip_memory", False)
    skip_profile_memory = options.get("skip_profile_memory", False)
    platform = agent.platform
    agent._memory_store = None
    agent._memory_enabled = False
    agent._user_profile_enabled = False
    agent._memory_recall_enabled = True
    agent._memory_read_scope = "session"
    agent._memory_write_mode = "auto"
    agent._automatic_memory_ingestion_enabled = True
    agent._background_review_enabled = True
    agent._memory_post_turn_prefetch_enabled = False
    agent._memory_recall_query_builder_enabled = False
    agent._memory_recall_query_recent_turns = 6
    agent._memory_recall_query_max_recent_chars = 1200
    agent._memory_recall_query_max_chars = 1800
    agent._memory_multi_query_recall_enabled = False
    agent._memory_multi_query_recall_max_queries = 4
    agent._memory_multi_query_recall_max_total_chars = 6000
    agent._memory_multi_query_recall_per_query_timeout_ms = 3000
    agent._memory_structured_cards_enabled = False
    agent._memory_structured_cards_max_per_turn = 5
    agent._memory_structured_cards_max_chars = 2500
    agent._memory_structured_cards_fallback_sync_turn_enabled = True
    agent._memory_structured_conflict_resolution_enabled = False
    agent._memory_structured_conflict_filter_enabled = False
    agent._memory_structured_conflict_max_candidates = 8
    agent._memory_structured_conflict_min_entity_overlap = 1
    agent._memory_structured_conflict_require_explicit_override = True
    agent._memory_nudge_interval = 10
    agent._turns_since_memory = 0
    mem_config = {}
    if not skip_memory and not skip_profile_memory:
        try:
            mem_config = _agent_cfg.get("memory", {})
            experience_config = _agent_cfg.get("experience", {})
            canonical_decision_capture_enabled = bool(
                experience_config.get("decisions", {}).get("enabled", True)
            )
            if canonical_decision_capture_enabled:
                agent._memory_structured_cards_enabled = False
            agent._memory_enabled = mem_config.get("memory_enabled", False)
            agent._user_profile_enabled = mem_config.get("user_profile_enabled", False)
            agent._memory_read_scope = str(
                mem_config.get("memory_read_scope", "session") or "session"
            )
            agent._memory_write_mode = str(
                mem_config.get("memory_write_mode", "auto") or "auto"
            )
            agent._automatic_memory_ingestion_enabled = bool(
                mem_config.get("automatic_memory_ingestion_enabled", True)
            )
            agent._background_review_enabled = bool(
                mem_config.get("background_review_enabled", True)
            )
            agent._memory_post_turn_prefetch_enabled = bool(
                mem_config.get("post_turn_prefetch_enabled", False)
            )
            agent._memory_recall_query_builder_enabled = bool(
                mem_config.get("recall_query_builder_enabled", False)
            )
            agent._memory_recall_query_recent_turns = int(
                mem_config.get("recall_query_recent_turns", 6) or 6
            )
            agent._memory_recall_query_max_recent_chars = int(
                mem_config.get("recall_query_max_recent_chars", 1200) or 1200
            )
            agent._memory_recall_query_max_chars = int(
                mem_config.get("recall_query_max_chars", 1800) or 1800
            )
            agent._memory_multi_query_recall_enabled = bool(
                mem_config.get("multi_query_recall_enabled", False)
            )
            agent._memory_multi_query_recall_max_queries = int(
                mem_config.get("multi_query_recall_max_queries", 4) or 4
            )
            agent._memory_multi_query_recall_max_total_chars = int(
                mem_config.get("multi_query_recall_max_total_chars", 6000) or 6000
            )
            agent._memory_multi_query_recall_per_query_timeout_ms = int(
                mem_config.get("multi_query_recall_per_query_timeout_ms", 3000) or 3000
            )
            agent._memory_structured_cards_enabled = bool(
                mem_config.get("structured_cards_enabled", False)
            )
            if canonical_decision_capture_enabled:
                agent._memory_structured_cards_enabled = False
            agent._memory_structured_cards_max_per_turn = int(
                mem_config.get("structured_cards_max_per_turn", 5) or 5
            )
            agent._memory_structured_cards_max_chars = int(
                mem_config.get("structured_cards_max_chars", 2500) or 2500
            )
            agent._memory_structured_cards_fallback_sync_turn_enabled = bool(
                mem_config.get("structured_cards_fallback_sync_turn_enabled", True)
            )
            agent._memory_structured_conflict_resolution_enabled = bool(
                mem_config.get("structured_conflict_resolution_enabled", False)
            )
            agent._memory_structured_conflict_filter_enabled = bool(
                mem_config.get("structured_conflict_filter_enabled", False)
            )
            agent._memory_structured_conflict_max_candidates = int(
                mem_config.get("structured_conflict_max_candidates", 8) or 8
            )
            agent._memory_structured_conflict_min_entity_overlap = int(
                mem_config.get("structured_conflict_min_entity_overlap", 1) or 1
            )
            agent._memory_structured_conflict_require_explicit_override = bool(
                mem_config.get("structured_conflict_require_explicit_override", True)
            )
            agent._memory_nudge_interval = int(mem_config.get("nudge_interval", 10))
            if agent._memory_enabled or agent._user_profile_enabled:
                from tools.memory_tool import MemoryStore

                agent._memory_store = MemoryStore(
                    memory_char_limit=mem_config.get("memory_char_limit", 2200),
                    user_char_limit=mem_config.get("user_char_limit", 1375),
                )
                agent._memory_store.load_from_disk()
        except Exception:
            pass  # Memory is optional -- don't break agent init

    # Memory provider plugin (external — one at a time, alongside built-in)
    # Reads memory.provider from config to select which plugin to activate.
    agent._memory_manager = None
    if not skip_memory:
        try:
            _mem_provider_name = mem_config.get("provider", "") if mem_config else ""

            if _mem_provider_name and _mem_provider_name.strip():
                from agent.memory_manager import MemoryManager as _MemoryManager
                from plugins.memory import load_memory_provider as _load_mem

                agent._memory_manager = _MemoryManager()
                _mp = _load_mem(_mem_provider_name)
                if _mp and _mp.is_available():
                    agent._memory_manager.add_provider(_mp)
                if agent._memory_manager.providers:
                    _init_kwargs = {
                        "session_id": agent.session_id,
                        "platform": platform or "cli",
                        "marlow_home": str(get_marlow_home()),
                        "agent_context": "primary",
                    }
                    # Thread session title for memory provider scoping
                    # (e.g. honcho uses this to derive chat-scoped session keys)
                    if agent._session_db:
                        try:
                            _st = agent._session_db.get_session_title(agent.session_id)
                            if _st:
                                _init_kwargs["session_title"] = _st
                        except Exception:
                            pass
                    # Thread gateway user identity for per-user memory scoping
                    if agent._user_id:
                        _init_kwargs["user_id"] = agent._user_id
                    if agent._user_id_alt:
                        _init_kwargs["user_id_alt"] = agent._user_id_alt
                    if agent._user_name:
                        _init_kwargs["user_name"] = agent._user_name
                    if agent._chat_id:
                        _init_kwargs["chat_id"] = agent._chat_id
                    if agent._chat_name:
                        _init_kwargs["chat_name"] = agent._chat_name
                    if agent._chat_type:
                        _init_kwargs["chat_type"] = agent._chat_type
                    if agent._thread_id:
                        _init_kwargs["thread_id"] = agent._thread_id
                    # Thread gateway session key for stable per-chat Honcho session isolation
                    if agent._gateway_session_key:
                        _init_kwargs["gateway_session_key"] = agent._gateway_session_key
                    # Profile identity for per-profile provider scoping
                    try:
                        from marlow_cli.profiles import get_active_profile_name

                        _profile = get_active_profile_name()
                        _init_kwargs["agent_identity"] = _profile
                        _init_kwargs["agent_workspace"] = "marlow"
                    except Exception:
                        pass
                    agent._memory_manager.initialize_all(**_init_kwargs)
                    logger.info("Memory provider '%s' activated", _mem_provider_name)
                else:
                    logger.debug(
                        "Memory provider '%s' not found or not available",
                        _mem_provider_name,
                    )
                    agent._memory_manager = None
        except Exception as _mpe:
            logger.warning("Memory provider plugin init failed: %s", _mpe)
            agent._memory_manager = None

    # Inject memory provider tool schemas into the tool surface.
    # Skip tools whose names already exist (plugins may register the
    # same tools via ctx.register_tool(), which lands in agent.tools
    # through _ra().get_tool_definitions()).  Duplicate function names cause
    # 400 errors on compatible endpoints that enforce unique names.
    #
    # Respect the platform's enabled_toolsets configuration (#5544):
    #   enabled_toolsets is None        → no filter, inject (backward compat)
    #   "memory" in enabled_toolsets    → user opted in, inject
    #   otherwise (incl. [])            → user excluded memory, skip injection
    #
    # Without this gate, `platform_toolsets: telegram: []` still leaks memory
    # provider tools into the tool surface — a 10x latency
    # penalty on local models and a frequent trigger of tool-call loops.
    if (
        agent._memory_manager
        and agent.tools is not None
        and (agent.enabled_toolsets is None or "memory" in agent.enabled_toolsets)
    ):
        _existing_tool_names = {
            t.get("function", {}).get("name")
            for t in agent.tools
            if isinstance(t, dict)
        }
        for _schema in agent._memory_manager.get_all_tool_schemas():
            _tname = _schema.get("name", "")
            if _tname and _tname in _existing_tool_names:
                continue  # already registered via plugin path
            _wrapped = {"type": "function", "function": _schema}
            agent.tools.append(_wrapped)
            if _tname:
                agent.valid_tool_names.add(_tname)
                _existing_tool_names.add(_tname)

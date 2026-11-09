"""Run a local query without creating a terminal chat interface."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext, redirect_stdout
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time
from urllib.parse import unquote, urlparse
import uuid

logger = logging.getLogger(__name__)

_TERMINAL_ENV = {
    "backend": "TERMINAL_ENV",
    "cwd": "TERMINAL_CWD",
    "timeout": "TERMINAL_TIMEOUT",
    "lifetime_seconds": "TERMINAL_LIFETIME_SECONDS",
    "ssh_host": "TERMINAL_SSH_HOST",
    "ssh_user": "TERMINAL_SSH_USER",
    "ssh_port": "TERMINAL_SSH_PORT",
    "ssh_key": "TERMINAL_SSH_KEY",
    "docker_image": "TERMINAL_DOCKER_IMAGE",
    "docker_forward_env": "TERMINAL_DOCKER_FORWARD_ENV",
    "docker_volumes": "TERMINAL_DOCKER_VOLUMES",
    "docker_env": "TERMINAL_DOCKER_ENV",
    "docker_mount_cwd_to_workspace": "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE",
    "docker_run_as_host_user": "TERMINAL_DOCKER_RUN_AS_HOST_USER",
    "docker_persist_across_processes": "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES",
    "docker_orphan_reaper": "TERMINAL_DOCKER_ORPHAN_REAPER",
    "container_cpu": "TERMINAL_CONTAINER_CPU",
    "container_memory": "TERMINAL_CONTAINER_MEMORY",
    "container_disk": "TERMINAL_CONTAINER_DISK",
    "container_persistent": "TERMINAL_CONTAINER_PERSISTENT",
    "sandbox_dir": "TERMINAL_SANDBOX_DIR",
    "persistent_shell": "TERMINAL_PERSISTENT_SHELL",
    "sudo_password": "SUDO_PASSWORD",
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg", ".ico"}


@contextmanager
def _invocation_environment(updates: dict[str, str | None]):
    """Apply CLI-owned environment values before tool initialization."""
    original = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def local_query_approvals(*, can_prompt: bool, yolo: bool = False):
    """Route protected actions through an approval guard even without a TTY."""
    from tools.approval import (
        clear_session, enable_session_yolo, forbid_interactive_approval,
        reset_current_session_key, set_current_session_key,
    )
    from tools.terminal_tool import _get_approval_callback, set_approval_callback

    session_key = f"cli-query:{uuid.uuid4().hex}"
    token = set_current_session_key(session_key)
    previous_callback = _get_approval_callback()

    def approve(command, description, *, allow_permanent=True):
        if yolo:
            return "once"
        if not can_prompt:
            return "deny"
        from marlow_cli.config import is_user_config_ignored
        from tools.approval import prompt_dangerous_approval

        with redirect_stdout(sys.stderr):
            return prompt_dangerous_approval(
                command, description, allow_permanent=allow_permanent and not is_user_config_ignored(),
            )

    if yolo:
        enable_session_yolo(session_key)
    set_approval_callback(approve)
    try:
        policy = nullcontext() if can_prompt else forbid_interactive_approval()
        with _invocation_environment({"MARLOW_INTERACTIVE": "1" if can_prompt else None}), policy:
            yield
    finally:
        set_approval_callback(previous_callback)
        clear_session(session_key)
        reset_current_session_key(token)


def _terminal_environment(cfg: dict, raw: dict, worktree: str | None) -> dict[str, str]:
    terminal = cfg.get("terminal") or {}
    updates = {}
    explicit_terminal = "terminal" in raw
    for name, var in _TERMINAL_ENV.items():
        if name in terminal and name != "cwd" and (explicit_terminal or var not in os.environ):
            value = terminal[name]
            updates[var] = json.dumps(value) if isinstance(value, (list, dict)) else str(value)

    if worktree:
        updates["TERMINAL_CWD"] = worktree
    elif updates.get("TERMINAL_ENV", os.environ.get("TERMINAL_ENV", "local")) == "local":
        updates["TERMINAL_CWD"] = os.getcwd()
    elif terminal.get("cwd") not in (None, "", ".", "auto", "cwd"):
        updates["TERMINAL_CWD"] = str(terminal["cwd"])
    return updates


def _resume_query(args, db):
    session_id = None
    if args.resume or isinstance(args.continue_last, str):
        name = args.resume or args.continue_last
        session_id = db.resolve_session_id(name) or db.resolve_session_by_title(name)
        if not session_id:
            raise ValueError(f"No session found matching {name!r}. Use 'marlow sessions list'.")
    elif args.continue_last:
        sessions = db.search_sessions(source="cli", limit=1)
        if not sessions:
            raise ValueError("No previous CLI session found. Use 'marlow sessions list'.")
        session_id = sessions[0]["id"]

    if not session_id:
        return None, []

    session_id = db.resolve_resume_session_id(session_id) or session_id
    session = db.get_session(session_id)
    if not session:
        raise ValueError(f"Session not found: {session_id}")
    history = [
        message for message in db.get_messages_as_conversation(session_id)
        if message.get("role") != "session_meta"
    ]
    if not args.no_restore_cwd and not args.worktree:
        saved_cwd = session.get("cwd")
        if saved_cwd and Path(saved_cwd).is_dir():
            os.chdir(saved_cwd)
        elif saved_cwd:
            print(f"Session directory is unavailable: {saved_cwd}", file=sys.stderr)
    return session_id, history


def _split_path_input(raw: str) -> tuple[str, str]:
    r"""Split a leading file path token from trailing free-form text.

    Supports quoted paths and backslash-escaped spaces so callers can accept
    inputs like:
      /tmp/pic.png describe this
      ~/storage/shared/My\ Photos/cat.png what is this?
      "/storage/emulated/0/DCIM/Camera/cat 1.png" summarize
    """
    raw = str(raw or "").strip()
    if not raw:
        return "", ""

    if raw[0] in {'"', "'"}:
        quote = raw[0]
        pos = 1
        while pos < len(raw):
            ch = raw[pos]
            if ch == '\\' and pos + 1 < len(raw):
                pos += 2
                continue
            if ch == quote:
                token = raw[1:pos]
                remainder = raw[pos + 1 :].strip()
                return token, remainder
            pos += 1
        return raw[1:], ""

    pos = 0
    while pos < len(raw):
        ch = raw[pos]
        if ch == '\\' and pos + 1 < len(raw) and raw[pos + 1] == ' ':
            pos += 2
        elif ch == ' ':
            break
        else:
            pos += 1

    token = raw[:pos].replace('\\ ', ' ')
    remainder = raw[pos:].strip()
    return token, remainder


def _resolve_attachment_path(raw_path: str) -> Path | None:
    token = raw_path.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        token = token[1:-1].strip()
    token = token.replace("\\ ", " ")
    if token.startswith("file://"):
        token = unquote(urlparse(token).path)
    try:
        path = Path(os.path.expandvars(token)).expanduser()
        if not path.is_absolute():
            path = Path(os.environ.get("TERMINAL_CWD", os.getcwd())) / path
        path = path.resolve()
        return path if path.is_file() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _collect_query_images(query: str | None, image: str | None) -> tuple[str, list[Path]]:
    """Retain both leading image paths in queries and explicit attachments."""
    message = query or ""
    stripped = message.strip()
    images = []
    path_prefix = stripped.lstrip("\"'")
    if path_prefix.startswith(("/", "~", "./", "../", "file://")) or (
        len(path_prefix) >= 3
        and path_prefix[0].isalpha()
        and path_prefix[1:3] in {":/", ":\\"}
    ):
        path = _resolve_attachment_path(stripped)
        remainder = ""
        if path is None:
            token, remainder = _split_path_input(stripped)
            path = _resolve_attachment_path(token)
        if path is None and stripped[0] not in {'"', "'"}:
            # An unquoted dropped path may itself contain spaces. Try
            # candidate splits only within the filesystem path-length limit.
            for pos in reversed(range(min(len(stripped), 4096))):
                if stripped[pos] != " ":
                    continue
                path = _resolve_attachment_path(stripped[:pos])
                if path is not None:
                    remainder = stripped[pos + 1:].strip()
                    break
        if path is not None and path.suffix.lower() in _IMAGE_SUFFIXES:
            images.append(path)
            message = remainder or f"[User attached image: {path.name}]"

    if image:
        explicit_path = _resolve_attachment_path(image)
        if explicit_path is None:
            raise ValueError(f"Image file not found: {image}")
        if explicit_path.suffix.lower() not in _IMAGE_SUFFIXES:
            raise ValueError(f"Not a supported image file: {explicit_path}")
        if explicit_path not in images:
            images.append(explicit_path)
    return message or "Describe this image.", images


def _image_input(query: str, images: list[Path], model: str, provider: str, cfg: dict):
    if not images:
        return query

    from agent.image_routing import build_native_content_parts, decide_image_input_mode
    from tools.vision_tools import vision_analyze_tool

    if decide_image_input_mode(provider, model, cfg) == "native":
        parts, _skipped = build_native_content_parts(query, [str(path) for path in images])
        if any(part.get("type") == "image_url" for part in parts):
            return parts

    annotations = []
    for path in images:
        try:
            result = json.loads(asyncio.run(vision_analyze_tool(
                image_url=str(path),
                user_prompt="Describe the image in detail, including any readable text.",
            )))
        except Exception:
            logger.debug("Attached image analysis failed", exc_info=True)
            result = {}
        if result.get("success") and result.get("analysis"):
            annotations.append(f"[Attached image description for {path}: {result['analysis']}]")
        else:
            annotations.append(f"[Attached image at {path}. Use vision_analyze to inspect it.]")
    return "\n\n".join([*annotations, query])


def _expand_context_references(text: str, agent) -> str:
    if "@" not in text:
        return text
    from agent.context_references import preprocess_context_references

    result = preprocess_context_references(
        text,
        cwd=Path.cwd(),
        context_length=agent.context_compressor.context_length,
    )
    for warning in result.warnings:
        print(warning, file=sys.stderr)
    if result.blocked:
        raise ValueError("Context references exceeded the model's input budget.")
    return result.message


@contextmanager
def _interruptible_query(agent):
    handlers = {}

    def interrupt(signum, _frame):
        agent.interrupt(f"received signal {signum}")
        try:
            grace = float(os.environ.get("MARLOW_SIGTERM_GRACE", "1.5"))
        except ValueError:
            grace = 1.5
        if grace > 0:
            time.sleep(grace)
        raise KeyboardInterrupt

    try:
        for name in ("SIGTERM", "SIGHUP"):
            if hasattr(signal, name):
                sig = getattr(signal, name)
                handlers[sig] = signal.signal(sig, interrupt)
        yield
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def _finalize_query(agent, db, result, source):
    if agent is None:
        return
    from marlow_cli.plugins import invoke_hook
    from tools.async_delegation import interrupt_for_session

    interrupt_for_session(parent_session_id=agent.session_id, reason="CLI query completed")

    try:
        invoke_hook("on_session_finalize", session_id=agent.session_id, platform=source)
    finally:
        try:
            agent.shutdown_memory_provider((result or {}).get("messages") or None)
        finally:
            agent.close()
    if agent.session_id and db.get_session(agent.session_id):
        db.end_session(agent.session_id, "cli_close")


def run_query(args) -> int:
    """Execute a single query, retaining session history and approval policy."""
    from marlow_cli.config import load_config, read_raw_config
    from marlow_cli.fallback_config import get_fallback_chain
    from tools.skills_sync import sync_skills
    from marlow_cli.mcp_startup import wait_for_mcp_discovery
    from marlow_cli.oneshot import _normalize_toolsets, _oneshot_clarify_callback, _resolve_query_runtime
    from marlow_cli.tools_config import _get_platform_tools
    from marlow_constants import parse_reasoning_effort
    from marlow_state import SessionDB

    settings = {}
    if args.ignore_user_config:
        settings["MARLOW_IGNORE_USER_CONFIG"] = "1"
    if args.source:
        settings["MARLOW_SESSION_SOURCE"] = args.source

    with _invocation_environment(settings), local_query_approvals(can_prompt=sys.stdin.isatty(), yolo=args.yolo):
        cfg = load_config()
        raw_cfg = read_raw_config() if not args.ignore_user_config else {}
        agent_config = cfg.get("agent") or {}
        source = os.environ.get("MARLOW_SESSION_SOURCE") or "cli"
        checkpoint = cfg.get("checkpoints") or {}
        if isinstance(checkpoint, bool):
            checkpoint = {"enabled": checkpoint}

        try:
            sync_skills(quiet=True)
        except Exception:
            logger.debug("Bundled skill sync failed", exc_info=True)
        wait_for_mcp_discovery()

        db = None
        agent = None
        worktree = None
        result = None
        original_cwd = Path.cwd()
        try:
            db = SessionDB()
            session_id, history = _resume_query(args, db)
            if args.worktree or cfg.get("worktree"):
                from marlow_cli.worktrees import _setup_worktree

                worktree = _setup_worktree()
                if not worktree:
                    return 1
                os.chdir(worktree["path"])

            with _invocation_environment(_terminal_environment(cfg, raw_cfg, (worktree or {}).get("path"))):
                from run_agent import AIAgent

                model, runtime = _resolve_query_runtime(cfg, args.model, args.provider)
                system_prompt = os.environ.get("MARLOW_EPHEMERAL_SYSTEM_PROMPT") or agent_config.get("system_prompt")
                if worktree:
                    note = f"You are working in an isolated Git worktree at {worktree['path']}."
                    system_prompt = f"{system_prompt}\n\n{note}" if system_prompt else note
                ignore_rules = args.ignore_rules or os.environ.get("MARLOW_IGNORE_RULES") == "1"
                agent = AIAgent(
                    model=model,
                    api_key=runtime.get("api_key"),
                    base_url=runtime.get("base_url"),
                    provider=runtime.get("provider"),
                    api_mode=runtime.get("api_mode"),
                    max_iterations=args.max_turns if args.max_turns is not None else agent_config.get("max_turns", 90),
                    enabled_toolsets=_normalize_toolsets(args.toolsets) or sorted(_get_platform_tools(cfg, "cli")),
                    disabled_toolsets=agent_config.get("disabled_toolsets") or None,
                    verbose_logging=bool(getattr(args, "verbose", agent_config.get("verbose"))),
                    quiet_mode=True,
                    ephemeral_system_prompt=system_prompt,
                    reasoning_config=parse_reasoning_effort(agent_config.get("reasoning_effort") or ""),
                    session_id=session_id,
                    platform=source,
                    session_db=db,
                    clarify_callback=_oneshot_clarify_callback,
                    fallback_providers=get_fallback_chain(cfg) or None,
                    checkpoints_enabled=args.checkpoints or bool(checkpoint.get("enabled")),
                    checkpoint_max_snapshots=checkpoint.get("max_snapshots", 20),
                    checkpoint_max_total_size_mb=checkpoint.get("max_total_size_mb", 500),
                    checkpoint_max_file_size_mb=checkpoint.get("max_file_size_mb", 10),
                    pass_session_id=args.pass_session_id,
                    skip_context_files=ignore_rules,
                    skip_memory=ignore_rules,
                )
                agent.suppress_status_output = True
                if session_id:
                    db.reopen_session(session_id)
                with _interruptible_query(agent):
                    text, images = _collect_query_images(args.query, args.image)
                    expanded_query = _expand_context_references(text, agent)
                    user_message = _image_input(expanded_query, images, model, runtime.get("provider", ""), cfg)
                    result = agent.run_conversation(
                        user_message=user_message,
                        conversation_history=history,
                        raw_user_message=args.query,
                        turn_origin="classic_cli",
                    )
        except KeyboardInterrupt:
            if agent is not None:
                agent.interrupt("CLI interrupted")
            return 130
        except Exception as exc:
            from agent.redact import redact_sensitive_text

            print(f"Error: {redact_sensitive_text(str(exc), force=True)}", file=sys.stderr)
            return 1
        finally:
            try:
                if db is not None:
                    try:
                        _finalize_query(agent, db, result, source)
                    finally:
                        db.close()
            except Exception:
                logger.debug("Query cleanup failed", exc_info=True)
            finally:
                os.chdir(original_cwd)
                if worktree:
                    from marlow_cli.worktrees import _cleanup_worktree

                    _cleanup_worktree(worktree)

    answer = result.get("final_response")
    if answer:
        print(answer)
    elif result.get("error"):
        from agent.redact import redact_sensitive_text

        print(f"Error: {redact_sensitive_text(str(result['error']), force=True)}", file=sys.stderr)
    if agent.session_id:
        print(f"session_id: {agent.session_id}", file=sys.stderr)
    return 1 if result.get("failed") or not answer else 0

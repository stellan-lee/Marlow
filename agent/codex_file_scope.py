"""Trusted file-disclosure scope shared by tools and Gateway media delivery."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import functools

_CURRENT = ContextVar("marlow_file_disclosure", default=None)


@dataclass
class FileScope:
    roots: tuple[Path, ...]
    generated: set[Path] = field(default_factory=set)

    def permits(self, path):
        return path in self.generated or any(
            path == root or path.is_relative_to(root) for root in self.roots
        )


@contextmanager
def file_scope(roots):
    roots = tuple(Path(root).resolve() for root in roots)
    current = _CURRENT.get()
    token = _CURRENT.set(
        current if current and current.roots == roots else FileScope(roots)
    )
    try:
        yield _CURRENT.get()
    finally:
        _CURRENT.reset(token)


def register_generated(path):
    scope = _CURRENT.get()
    if scope is not None:
        scope.generated.add(Path(path).resolve())


def permits_delivery(path):
    from marlow_constants import get_marlow_home

    profile = get_marlow_home().absolute()
    if path.is_relative_to(profile / "runtime"):
        return False
    # Credential stores are private even outside the current profile layout.
    if any(part in {".codex", ".ssh", ".aws", ".gnupg"} for part in path.parts):
        return False
    if "runtime" in path.parts and "codex" in path.parts:
        return False
    scope = _CURRENT.get()
    if scope is not None:
        return scope.permits(path)
    # No trusted conversation may infer access to another conversation's files.
    if "conversations" in path.parts and path.is_relative_to(profile / "workspace"):
        return False
    return None  # deterministic operator paths still apply their normal policy


def gateway_delivery_scope(function):
    @functools.wraps(function)
    async def scoped(self, event, *args, **kwargs):
        from agent.codex_environment import gateway_environment

        environment = gateway_environment(event.source)
        with file_scope([
            environment.workspace,
            *(path for path, _ in environment.projects),
        ]):
            return await function(self, event, *args, **kwargs)

    return scoped


def snapshot_media(path):
    """Freeze permitted bytes before async approval/upload; never follow links."""
    import os
    import shutil
    import stat
    import uuid
    from agent.codex_environment import _private_directory
    from marlow_constants import get_marlow_home

    scope = _CURRENT.get()
    if scope is None:
        return path  # deterministic operator path; no model-controlled writer
    source = Path(path)
    outbox = get_marlow_home() / ".delivery-outbox"
    if source.is_relative_to(outbox) and source in scope.generated:
        return str(source)
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    descriptor = None
    try:
        for part in source.parts[1:-1]:
            following = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = following
        descriptor = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 100 * 1024 * 1024:
            raise PermissionError("Media must be a regular file of at most 100 MiB")
        target_dir = _private_directory(outbox / uuid.uuid4().hex)
        target = target_dir / source.name
        with os.fdopen(descriptor, "rb") as incoming, target.open("xb") as outgoing:
            descriptor = None
            shutil.copyfileobj(incoming, outgoing)
        target.chmod(0o600)
        register_generated(target)
        return str(target)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def cron_delivery_scope(function):
    @functools.wraps(function)
    def scoped(job, *args, **kwargs):
        from agent.codex_environment import (
            managed_environment,
            conversation_workspace_scope,
            CodexEnvironment,
        )
        from marlow_cli.config import load_config

        captured = job.get("execution_scope") or {}
        user = captured.get("user_id")
        if not any(
            captured.get(key) is not None for key in ("user_id", "chat_id", "thread_id")
        ):
            user = "task:cron:" + str(job["id"])
        key = conversation_workspace_scope(
            channel="cron",
            user=user,
            chat=captured.get("chat_id"),
            thread=captured.get("thread_id"),
        )
        environment = CodexEnvironment.build(
            settings=(load_config().get("runtime") or {}).get("codex") or {},
            workspace_scope=key,
        )
        roots = [
            environment.workspace,
            *[entry["path"] for entry in captured.get("projects") or []],
        ]
        if captured.get("workspace"):
            roots.append(captured["workspace"])
        with file_scope(roots):
            return function(job, *args, **kwargs)

    return scoped


def source_delivery_scope(function):
    @functools.wraps(function)
    async def scoped(*args, **kwargs):
        import inspect
        from agent.codex_environment import gateway_environment

        arguments = inspect.signature(function).bind_partial(*args, **kwargs).arguments
        source = arguments.get("source")
        if source is None:
            source = arguments["event"].source
        environment = gateway_environment(source)
        with file_scope([
            environment.workspace,
            *(path for path, _ in environment.projects),
        ]):
            return await function(*args, **kwargs)

    return scoped

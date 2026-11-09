"""Durable scope/thread/turn attribution and cross-process conversation leases."""

from __future__ import annotations
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import time


class RuntimeState:
    def __init__(self, profile: Path, scope: dict):
        from agent.codex_environment import _private_directory

        self.root = _private_directory(profile / "runtime")
        self.scope = json.dumps(scope, sort_keys=True, separators=(",", ":"))
        self.key = hashlib.sha256(self.scope.encode()).hexdigest()
        stable = {key: value for key, value in scope.items() if key != "ephemeral_turn"}
        self.stable_scope = json.dumps(stable, sort_keys=True, separators=(",", ":"))
        lease_scope = (
            json.dumps({"task": stable["task"]}, sort_keys=True)
            if stable.get("task")
            else self.stable_scope
        )
        self.lease_key = hashlib.sha256(lease_scope.encode()).hexdigest()
        self.path = self.root / "codex-runs.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Runtime mapping database must not be a symlink")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS session_scopes (session TEXT PRIMARY KEY, scope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS threads (
                    scope TEXT PRIMARY KEY, thread_id TEXT, turn_id TEXT,
                    status TEXT NOT NULL DEFAULT 'idle', updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, scope TEXT NOT NULL, trigger TEXT NOT NULL,
                    thread_id TEXT, turn_id TEXT, started REAL NOT NULL, ended REAL,
                    status TEXT NOT NULL, error_type TEXT, usage TEXT);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def lease(self, timeout=60):
        path = self.root / (self.lease_key + ".lock")
        if path.is_symlink():
            raise ValueError("Runtime lease file must not be a symlink")
        with path.open("a", encoding="utf-8") as stream:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "This conversation is busy; input was not dispatched"
                        )
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def claim_scope(self):
        session = json.loads(self.scope).get("session")
        if not session:
            return
        with self.connect() as db:
            if json.loads(self.scope).get("ephemeral_turn"):
                pending = db.execute(
                    "SELECT scope FROM threads WHERE status IN ('running','needs_verification')"
                ).fetchall()
                for (raw,) in pending:
                    stable = {
                        key: value
                        for key, value in json.loads(raw).items()
                        if key != "ephemeral_turn"
                    }
                    if (
                        raw != self.scope
                        and json.dumps(stable, sort_keys=True, separators=(",", ":"))
                        == self.stable_scope
                    ):
                        raise RuntimeError(
                            "Previous stateless turn needs verification; replay is disabled"
                        )
            task = json.loads(self.scope).get("task")
            if task:
                pending = db.execute(
                    "SELECT scope FROM threads WHERE status IN ('running','needs_verification')"
                ).fetchall()
                if any(
                    json.loads(row[0]).get("task") == task and row[0] != self.scope
                    for row in pending
                ):
                    raise RuntimeError(
                        "A previous execution of this task needs verification; automatic task replay is disabled"
                    )
            db.execute(
                "INSERT OR IGNORE INTO session_scopes(session,scope) VALUES(?,?)",
                (session, self.stable_scope),
            )
            owner = db.execute(
                "SELECT scope FROM session_scopes WHERE session=?", (session,)
            ).fetchone()
            if owner[0] != self.stable_scope:
                raise PermissionError(
                    "Session belongs to a different user, channel, conversation or project"
                )

    def mapping(self):
        with self.connect() as db:
            row = db.execute(
                "SELECT thread_id,turn_id,status FROM threads WHERE scope=?",
                (self.scope,),
            ).fetchone()
        return row or (None, None, "idle")

    def thread(self, thread_id):
        with self.connect() as db:
            db.execute(
                "INSERT INTO threads(scope,thread_id,status,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET thread_id=excluded.thread_id,updated=excluded.updated",
                (self.scope, thread_id, "idle", time.time()),
            )

    def start(self, trigger):
        with self.connect() as db:
            thread_id, _, status = self.mapping()
            if status in {"running", "needs_verification"}:
                raise RuntimeError(
                    "Previous turn needs verification before dispatch; do not replay writes"
                )
            cursor = db.execute(
                "INSERT INTO runs(scope,trigger,thread_id,started,status) VALUES(?,?,?,?,?)",
                (self.scope, trigger, thread_id, time.time(), "running"),
            )
            db.execute(
                "UPDATE threads SET turn_id=NULL,status=?,updated=? WHERE scope=?",
                ("running", time.time(), self.scope),
            )
            return cursor.lastrowid

    def turn(self, turn_id):
        with self.connect() as db:
            db.execute(
                "UPDATE threads SET turn_id=?,updated=? WHERE scope=?",
                (turn_id, time.time(), self.scope),
            )
            db.execute(
                "UPDATE runs SET turn_id=? WHERE scope=? AND status=?",
                (turn_id, self.scope, "running"),
            )

    def finish(
        self, run_id, *, thread_id, turn_id, status, error_type=None, usage=None
    ):
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET thread_id=?,turn_id=?,ended=?,status=?,error_type=?,usage=? WHERE id=?",
                (
                    thread_id,
                    turn_id,
                    time.time(),
                    status,
                    error_type,
                    json.dumps(usage) if usage is not None else None,
                    run_id,
                ),
            )
            db.execute(
                "UPDATE threads SET turn_id=?,status=?,updated=? WHERE scope=?",
                (turn_id, status, time.time(), self.scope),
            )

    def acknowledge(self, *, thread_id, turn_id):
        """Explicit operator verification, never task completion or replay."""
        with self.lease(), self.connect() as db:
            current = db.execute(
                "SELECT thread_id,turn_id,status FROM threads WHERE scope=?",
                (self.scope,),
            ).fetchone()
            if (
                not current
                or current[:2] != (thread_id, turn_id)
                or current[2] not in {"running", "needs_verification"}
            ):
                raise ValueError(
                    "Verification does not match the current pending operation"
                )
            db.execute(
                "UPDATE threads SET status=?,updated=? WHERE scope=?",
                ("verified_by_operator", time.time(), self.scope),
            )

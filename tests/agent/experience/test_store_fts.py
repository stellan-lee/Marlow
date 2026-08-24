from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import agent.experience.store as store_module
from agent.experience.store import (
    ExperienceSchemaNotCurrentError,
    ExperienceStore,
)


class _NoFtsCursor(sqlite3.Cursor):
    def execute(self, sql: str, parameters=(), /):  # type: ignore[no-untyped-def]
        if "using fts5" in sql.lower() or "experience_search" in sql.lower() and "match" in sql.lower():
            raise sqlite3.OperationalError("no such module: fts5")
        return super().execute(sql, parameters)


class _NoFtsConnection(sqlite3.Connection):
    def cursor(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["factory"] = _NoFtsCursor
        return super().cursor(*args, **kwargs)

    def execute(self, sql: str, parameters=(), /):  # type: ignore[no-untyped-def]
        if "using fts5" in sql.lower() or "experience_search" in sql.lower() and "match" in sql.lower():
            raise sqlite3.OperationalError("no such module: fts5")
        return super().execute(sql, parameters)


def _disable_fts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["factory"] = _NoFtsConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)


def _seed(store: ExperienceStore) -> None:
    store.upsert_scope_policy(
        principal_id="local-owner",
        repository_id="repo",
        project_id="project",
        project_root_rel=".",
        recall_allowed=True,
        injection_allowed=True,
        max_egress_policy="explicit_any_provider",
    )
    store.create_lesson(
        item_id="lesson",
        principal_id="local-owner",
        scope_type="project",
        scope_id="project",
        repository_id="repo",
        project_id="project",
        title="SQLite lock handling",
        summary="Use bounded contention retries.",
        body={
            "applies_when": "SQLite reports a busy writer",
            "does_not_apply_when": None,
            "guidance": "Use BEGIN IMMEDIATE with bounded jitter.",
            "rationale": "It avoids synchronized writer convoys.",
        },
        tags={"technology": ["sqlite"], "failure": ["database is locked"]},
        sensitivity="normal",
        egress_policy="explicit_any_provider",
        producer_trust_domain="provider:a",
    )
    store.approve_lesson("lesson")


def _search(store: ExperienceStore, *, query: str = "", tags=None):  # type: ignore[no-untyped-def]
    return store.search_lessons(
        principal_id="local-owner",
        scope_type="project",
        scope_id="project",
        repository_id="repo",
        project_id="project",
        provider_trust_domain="provider:a",
        query=query,
        tags=tags,
    )




def _seed_cjk_lessons(store: ExperienceStore) -> None:
    store.upsert_scope_policy(
        principal_id="local-owner",
        repository_id="repo",
        project_id="project",
        project_root_rel=".",
        recall_allowed=True,
        injection_allowed=True,
        max_egress_policy="explicit_any_provider",
    )
    lessons = [
        ("lesson_cjk_phrase", "审批卡片", "审批卡片需要紧凑的内联流程。"),
        ("lesson_cjk_substring", "队列重试", "SQLite 队列重试需要抖动。"),
        ("lesson_cjk_two", "工单", "工单处理要保留来源。"),
        ("lesson_mixed", "Redis缓存", "Redis缓存用于 API 限流。"),
        ("lesson_irrelevant", "账单导出", "账单导出走异步任务。"),
    ]
    for item_id, title, summary in lessons:
        store.create_lesson(
            item_id=item_id,
            principal_id="local-owner",
            scope_type="project",
            scope_id="project",
            repository_id="repo",
            project_id="project",
            title=title,
            summary=summary,
            body={
                "applies_when": "Chinese retrieval test",
                "does_not_apply_when": None,
                "guidance": summary,
                "rationale": "Deterministic CJK test data.",
            },
            tags={"technology": ["sqlite"]},
            sensitivity="normal",
            egress_policy="explicit_any_provider",
            producer_trust_domain="provider:a",
        )
        store.approve_lesson(item_id)


def test_cjk_phrase_substring_two_char_and_mixed_retrieval(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _seed_cjk_lessons(store)
        phrase = [item["id"] for item in _search(store, query="审批卡片")]
        substring = [item["id"] for item in _search(store, query="队列重试")]
        two_char = [item["id"] for item in _search(store, query="工单")]
        mixed = [item["id"] for item in _search(store, query="Redis API")]
        irrelevant = [item["id"] for item in _search(store, query="账单")]
        assert phrase == ["lesson_cjk_phrase"]
        assert substring == ["lesson_cjk_substring"]
        assert two_char == ["lesson_cjk_two"]
        assert mixed == ["lesson_mixed"]
        assert irrelevant == []


def test_retrieval_is_deterministic_and_bounded(tmp_path: Path) -> None:
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        _seed_cjk_lessons(store)
        first = [item["id"] for item in _search(store, query="审批 队列 Redis API 账单")]
        second = [item["id"] for item in _search(store, query="审批 队列 Redis API 账单")]
        assert first == second
        assert len(first) <= 5


def test_no_fts_runtime_uses_only_authorized_metadata(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _disable_fts(monkeypatch)
    with ExperienceStore((tmp_path / "state.db").resolve()) as store:
        assert store.fts_enabled is False
        _seed(store)
        assert _search(store, query="SQLite contention") == []
        results = _search(store, tags={"failure": ["database is locked"]})
        assert [item["id"] for item in results] == ["lesson"]


def test_fts_database_reopens_safely_when_runtime_loses_fts(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    path = (tmp_path / "state.db").resolve()
    with ExperienceStore(path) as store:
        _seed(store)

    _disable_fts(monkeypatch)
    with ExperienceStore(path) as degraded:
        assert degraded.fts_enabled is False
        assert [
            item["id"]
            for item in _search(degraded, tags={"technology": ["sqlite"]})
        ] == ["lesson"]


def test_schema_initialization_is_idempotent(tmp_path: Path) -> None:
    path = (tmp_path / "state.db").resolve()
    with ExperienceStore(path) as first:
        _seed(first)
    with ExperienceStore(path) as second:
        assert second.get_item("lesson") is not None


def test_normal_reopen_does_not_rebuild_fts_index(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    statements: list[str] = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    path = (tmp_path / "state.db").resolve()
    with ExperienceStore(path) as first:
        _seed(first)
    first_rebuilds = sum(
        "VALUES('rebuild')" in statement for statement in statements
    )
    assert first_rebuilds == 1

    with ExperienceStore(path):
        pass
    total_rebuilds = sum(
        "VALUES('rebuild')" in statement for statement in statements
    )
    assert total_rebuilds == first_rebuilds


def test_open_current_validates_without_schema_writes_and_keeps_fts(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    path = (tmp_path / "state.db").resolve()
    with ExperienceStore(path) as initialized:
        _seed(initialized)

    statements: list[str] = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    with ExperienceStore.open_current(path) as current:
        assert current.fts_enabled is True
        assert [item["id"] for item in _search(current, query="SQLite contention")] == [
            "lesson"
        ]

    schema_write_prefixes = (
        "ALTER ",
        "BEGIN",
        "CREATE ",
        "DELETE ",
        "DROP ",
        "INSERT ",
        "REPLACE ",
        "UPDATE ",
        "VACUUM",
    )
    assert not any(
        statement.lstrip().upper().startswith(schema_write_prefixes)
        for statement in statements
    )
    assert not any("JOURNAL_MODE" in statement.upper() for statement in statements)


def test_open_current_rejects_missing_schema_without_creating_it(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "state.db").resolve()

    with pytest.raises(ExperienceSchemaNotCurrentError):
        ExperienceStore.open_current(path)

    assert not path.exists()

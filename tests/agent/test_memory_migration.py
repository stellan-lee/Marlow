import json
import sqlite3
from pathlib import Path

import pytest

from agent import memory_migration


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(memory_migration, "_profile_home", lambda _profile: home)
    return home


def test_migrates_legacy_files_to_private_owner_workspace_and_keeps_sources(
    profile_home,
):
    legacy = profile_home / "memories"
    legacy.mkdir()
    source = legacy / "MEMORY.md"
    source_bytes = b"Remember this\n\xc2\xa7\nSecond note\n"
    source.write_bytes(source_bytes)
    (legacy / "USER.md").write_text("User prefers concise replies\n", encoding="utf-8")
    drift_backup = legacy / "MEMORY.md.bak.123"
    drift_backup.write_bytes(b"unparsed historical bytes\x00\xff")

    result = memory_migration.migrate_memory()

    target = (
        profile_home
        / "memory-workspaces"
        / "local-owner"
        / "memory"
        / "legacy-memory.md"
    )
    assert result["migrated"]["local_files"] == 2
    assert "Remember this" in target.read_text(encoding="utf-8")
    assert "Second note" in target.read_text(encoding="utf-8")
    assert source.read_bytes() == source_bytes
    active_backup = (
        profile_home / "memory-migration" / "backups" / "legacy-memory" / source.name
    )
    assert active_backup.read_bytes() == source_bytes
    assert str(active_backup) in result["backup_paths"]
    archived_backup = (
        profile_home
        / "memory-migration"
        / "backups"
        / "legacy-memory"
        / drift_backup.name
    )
    assert archived_backup.read_bytes() == drift_backup.read_bytes()
    assert str(archived_backup) in result["backup_paths"]
    assert target.stat().st_mode & 0o777 == 0o600
    assert (target.parent.parent).stat().st_mode & 0o777 == 0o700


def test_retries_use_original_snapshot_and_do_not_resurrect_deleted_memory(
    profile_home,
):
    legacy = profile_home / "memories"
    legacy.mkdir()
    source = legacy / "MEMORY.md"
    source.write_text("Original\n", encoding="utf-8")
    result = memory_migration.migrate_memory()
    target = (
        profile_home
        / "memory-workspaces"
        / "local-owner"
        / "memory"
        / "legacy-memory.md"
    )

    source.write_text("Changed later\n", encoding="utf-8")
    target.unlink()
    second = memory_migration.migrate_memory()

    assert not target.exists()
    assert second["migrated"]["local_files"] == 0
    assert result["status"] == "complete"


def test_database_migration_keeps_consistent_backup_and_reads_first_snapshot(
    profile_home,
):
    source = profile_home / "memory_consolidation.db"
    connection = sqlite3.connect(source)
    connection.execute(
        "CREATE TABLE memory_events (event_id TEXT, scope_id TEXT, content TEXT)"
    )
    connection.execute("INSERT INTO memory_events VALUES ('e1', 'scope-a', 'first')")
    connection.commit()
    connection.close()

    first = memory_migration.migrate_memory()
    backup = profile_home / "memory-migration" / "backups" / "memory_consolidation.db"
    assert backup in [Path(path) for path in first["backup_paths"]]
    assert backup.stat().st_mode & 0o777 == 0o600

    connection = sqlite3.connect(source)
    connection.execute("INSERT INTO memory_events VALUES ('e2', 'scope-b', 'later')")
    connection.commit()
    connection.close()

    second = memory_migration.migrate_memory()
    assert first["migrated"]["consolidation_records"] == 1
    assert second["migrated"]["consolidation_records"] == 0
    migration_files = list(
        (profile_home / "memory-migration" / "consolidation").glob("*.md")
    )
    assert len(migration_files) == 1
    document = migration_files[0].read_text(encoding="utf-8")
    assert "first" in document
    assert "later" not in document
    backup_connection = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        assert (
            backup_connection.execute("SELECT count(*) FROM memory_events").fetchone()[
                0
            ]
            == 1
        )
    finally:
        backup_connection.close()


def test_symlinked_legacy_memory_source_fails_closed(profile_home):
    outside = profile_home.parent / "outside.md"
    outside.write_text("private", encoding="utf-8")
    legacy = profile_home / "memories"
    legacy.mkdir()
    (legacy / "MEMORY.md").symlink_to(outside)

    result = memory_migration.migrate_memory()
    assert result["status"] == "blocked"
    assert result["errors"][0]["type"] == "ValueError"


def test_configured_external_provider_is_reported_without_being_opened(profile_home):
    (profile_home / "config.yaml").write_text(
        "memory:\n  provider: honcho\n", encoding="utf-8"
    )

    result = memory_migration.migrate_memory()

    assert result["status"] == "partial"
    assert result["external"] == [
        {
            "provider": "honcho",
            "status": "not_accessed",
            "reason": "External provider data was not queried or exported.",
        }
    ]


def test_honcho_config_presence_is_reported_without_reading_credentials(profile_home):
    (profile_home / "honcho.json").write_text(
        "this is intentionally not parsed", encoding="utf-8"
    )

    result = memory_migration.migrate_memory()

    assert result["status"] == "partial"
    assert result["external"][0]["provider"] == "honcho"
    assert result["external"][0]["status"] == "not_accessed"


def test_state_database_without_experience_tables_is_not_backed_up(profile_home):
    source = profile_home / "state.db"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE sessions (session_id TEXT)")
    connection.execute("INSERT INTO sessions VALUES ('history')")
    connection.commit()
    connection.close()

    result = memory_migration.migrate_memory()

    assert result["migrated"]["experience_records"] == 0
    assert (
        str(profile_home / "memory-migration" / "backups" / "state.db")
        not in result["backup_paths"]
    )
    assert not (profile_home / "memory-migration" / "backups" / "state.db").exists()


def _create_experience_database(path, records):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE experience_items (
            id TEXT, family_id TEXT, kind TEXT, current_status TEXT,
            current_revision INTEGER, principal_id TEXT, scope_type TEXT,
            scope_id TEXT, repository_id TEXT, project_id TEXT,
            sensitivity TEXT, egress_policy TEXT, created_by TEXT,
            created_at REAL, updated_at REAL, deleted_at REAL
        );
        CREATE TABLE experience_item_revisions (
            item_id TEXT, revision INTEGER, title TEXT, summary TEXT,
            body_json TEXT, source_session_id TEXT, source_turn_id TEXT,
            source_work_id TEXT, source_hash TEXT, editor TEXT, edit_reason TEXT,
            created_at REAL, last_validated_at REAL, review_after REAL,
            producer_json TEXT, content_hash TEXT, confidence REAL
        );
        """
    )
    for item in records:
        item = dict(item)
        has_revision = item.pop("has_revision", True)
        item_row = {
            "family_id": f"family-{item['id']}",
            "kind": "lesson",
            "current_status": "active",
            "current_revision": 1,
            "principal_id": "local-owner",
            "scope_type": "profile",
            "scope_id": "profile-owner",
            "repository_id": None,
            "project_id": None,
            "sensitivity": "normal",
            "egress_policy": "explicit_any_provider",
            "created_by": "user",
            "created_at": 10.0,
            "updated_at": 20.0,
            "deleted_at": None,
            **item,
        }
        columns = list(item_row)
        connection.execute(
            f"INSERT INTO experience_items ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [item_row[column] for column in columns],
        )
        if has_revision:
            revision = {
                "item_id": item["id"],
                "revision": 1,
                "title": f"Title {item['id']}",
                "summary": f"Summary {item['id']}",
                "body_json": json.dumps({"statement": f"Body {item['id']}"}),
                "source_session_id": "session-source",
                "source_turn_id": "turn-source",
                "source_work_id": "work-source",
                "source_hash": "source-hash",
                "editor": "operator",
                "edit_reason": "verified historical lesson",
                "created_at": 10.0,
                "last_validated_at": 15.0,
                "review_after": None,
                "producer_json": "{}",
                "content_hash": "content-hash",
                "confidence": 0.9,
            }
            columns = list(revision)
            connection.execute(
                f"INSERT INTO experience_item_revisions ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                [revision[column] for column in columns],
            )
    connection.commit()
    connection.close()


def test_active_explicit_any_provider_experience_remains_operator_only(profile_home):
    source = profile_home / "state.db"
    records = [
        {"id": "eligible"},
        {"id": "other-principal", "principal_id": "gateway-user"},
        {
            "id": "project-scope",
            "scope_type": "project",
            "repository_id": "repo",
            "project_id": "project",
        },
        {"id": "private", "sensitivity": "local_only"},
        {"id": "egress", "egress_policy": "local_only"},
        {"id": "inactive", "current_status": "candidate"},
        {"id": "deleted", "deleted_at": 30.0},
        {"id": "missing-revision", "has_revision": False},
    ]
    _create_experience_database(source, records)

    result = memory_migration.migrate_memory()

    memory_dir = profile_home / "memory-workspaces" / "local-owner" / "memory"
    assert result["migrated"]["experience_records"] > 0
    assert list(memory_dir.glob("legacy-experience-*.md")) == []
    archive_docs = list(
        (profile_home / "memory-migration" / "experience").glob("*.md")
    )
    archive_text = "\n".join(path.read_text(encoding="utf-8") for path in archive_docs)
    assert "Body eligible" in archive_text
    assert "session-source" in archive_text
    assert "verified historical lesson" in archive_text
    assert any(
        "no matching project scope policy" in restriction
        and "explicit-any-provider" in restriction
        for restriction in result["restrictions"]
    )
    assert (profile_home / "memory-migration" / "backups" / "state.db").is_file()


def test_legacy_memory_flags_control_each_file_promotion(profile_home):
    legacy = profile_home / "memories"
    legacy.mkdir()
    (legacy / "MEMORY.md").write_text("Agent memory\n", encoding="utf-8")
    (legacy / "USER.md").write_text("User memory\n", encoding="utf-8")
    (profile_home / "config.yaml").write_text(
        "memory:\n  memory_enabled: false\n  user_profile_enabled: true\n",
        encoding="utf-8",
    )

    result = memory_migration.migrate_memory()

    memory_dir = profile_home / "memory-workspaces" / "local-owner" / "memory"
    assert not (memory_dir / "legacy-memory.md").exists()
    assert (memory_dir / "legacy-user.md").is_file()
    assert result["migrated"]["local_files"] == 1
    archived_memory = (
        profile_home / "memory-migration" / "backups" / "legacy-memory" / "MEMORY.md"
    )
    assert archived_memory.read_text(encoding="utf-8") == "Agent memory\n"
    assert any(
        "MEMORY.md" in restriction and "flag is false" in restriction
        for restriction in result["restrictions"]
    )


def test_both_legacy_memory_flags_false_keep_both_files_in_backup_only(profile_home):
    legacy = profile_home / "memories"
    legacy.mkdir()
    (legacy / "MEMORY.md").write_text("Agent memory\n", encoding="utf-8")
    (legacy / "USER.md").write_text("User memory\n", encoding="utf-8")
    (profile_home / "config.yaml").write_text(
        "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n",
        encoding="utf-8",
    )

    result = memory_migration.migrate_memory()

    memory_dir = profile_home / "memory-workspaces" / "local-owner" / "memory"
    assert result["migrated"]["local_files"] == 0
    assert list(memory_dir.glob("legacy-*.md")) == []
    backup_dir = profile_home / "memory-migration" / "backups" / "legacy-memory"
    assert (backup_dir / "MEMORY.md").read_text(encoding="utf-8") == "Agent memory\n"
    assert (backup_dir / "USER.md").read_text(encoding="utf-8") == "User memory\n"


def test_database_decode_failure_keeps_completed_local_file_journal(profile_home):
    legacy = profile_home / "memories"
    legacy.mkdir()
    (legacy / "MEMORY.md").write_text("Already saved\n", encoding="utf-8")
    source = profile_home / "state.db"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE experience_items (unexpected TEXT)")
    connection.execute("INSERT INTO experience_items VALUES ('bad row')")
    connection.commit()
    connection.close()

    result = memory_migration.migrate_memory()

    manifest_path = profile_home / "memory-migration" / ".migration-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["migrated"]["local_files"] == 1
    assert manifest["completed"]["local:MEMORY.md"]["status"] == "written"


@pytest.mark.parametrize("target_kind", ["dangling_symlink", "directory"])
def test_preexisting_unsafe_memory_destination_is_rejected(profile_home, target_kind):
    legacy = profile_home / "memories"
    legacy.mkdir()
    (legacy / "MEMORY.md").write_text(
        "Do not overwrite destination\n", encoding="utf-8"
    )
    target = (
        profile_home
        / "memory-workspaces"
        / "local-owner"
        / "memory"
        / "legacy-memory.md"
    )
    target.parent.mkdir(parents=True)
    if target_kind == "dangling_symlink":
        target.symlink_to(profile_home / "missing-target")
    else:
        target.mkdir()

    result = memory_migration.migrate_memory()

    assert result["status"] == "blocked"
    assert result["errors"]


def test_migration_manifest_is_json_safe(profile_home):
    migration_dir = profile_home / "memory-migration"
    migration_dir.mkdir()
    manifest = {"version": 1, "completed": {"x": {"status": "written"}}}
    memory_migration._write_manifest(
        migration_dir / ".migration-manifest.json", manifest
    )
    assert (
        json.loads((migration_dir / ".migration-manifest.json").read_text()) == manifest
    )

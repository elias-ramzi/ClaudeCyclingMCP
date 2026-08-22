"""The database: where it lives, and that a migration walk actually walks.

A migration bug is the worst kind this repo can ship, because it is discovered
on someone else's data. These cover the two starting points that exist in the
world — a machine with no database, and a machine holding one from an earlier
release — plus the rule that makes both safe: applying migrations twice must be
a no-op.
"""

from __future__ import annotations

import sqlite3

import pytest

from cycling_mcp import store


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "coach.db"
    monkeypatch.setenv(store.ENV_DB_PATH, str(path))
    return path


def test_env_var_overrides_the_default_location(db):
    assert store.db_path() == db


def test_default_location_is_under_the_home_directory(monkeypatch):
    monkeypatch.delenv(store.ENV_DB_PATH, raising=False)
    assert store.db_path() == store.DEFAULT_DB_PATH
    assert store.db_path().name == "coach.db"


def test_db_path_does_not_create_anything(db):
    """It is called for reporting, including by server_info."""
    assert store.db_path() == db
    assert not db.exists()
    assert not db.parent.exists()


def test_an_empty_database_migrates_to_current(db):
    with store.open_db() as conn:
        assert store.schema_version(conn) == store.CURRENT_SCHEMA_VERSION
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert {"athlete", "ftp_history", "activities", "events", "planned_workouts"} <= tables
    assert db.exists(), "the parent directory should have been created too"


def test_migrating_an_already_current_database_changes_nothing(db):
    with store.open_db():
        pass
    with store.open_db() as conn:
        assert store.migrate(conn) == []
        assert store.schema_version(conn) == store.CURRENT_SCHEMA_VERSION


def _build_v1(path):
    """A database as it stands after the first migration and nothing else."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    store.schema_version(conn)
    for statement in store.MIGRATIONS[0][1]():
        conn.execute(statement)
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (1, '2026-01-01T00:00:00Z')"
    )
    conn.close()


def test_a_v1_database_upgrades_in_place_without_losing_rows(db):
    """The realistic case: an athlete's file from an earlier release.

    v2 adds tables and ALTERs `activities`, so this is the path that would
    silently drop a training log if a migration were rewritten rather than
    appended.
    """
    _build_v1(db)
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute(
        "INSERT INTO activities (athlete_id, garmin_activity_id, local_date, imported_at) "
        "VALUES (1, '999', '2026-03-01', '2026-03-01T00:00:00Z')"
    )
    conn.close()

    with store.open_db() as conn:
        assert store.schema_version(conn) == store.CURRENT_SCHEMA_VERSION
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(activities)")}
        rows = conn.execute("SELECT * FROM activities").fetchall()
    assert {"rpe", "feel", "note", "annotated_at"} <= columns
    assert len(rows) == 1 and rows[0]["garmin_activity_id"] == "999"


def test_the_v1_walk_reports_which_migrations_it_applied(db):
    _build_v1(db)
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    assert store.migrate(conn) == [2]
    conn.execute("COMMIT")
    conn.close()


def test_migration_versions_are_ordered_and_unique():
    versions = [version for version, _ in store.MIGRATIONS]
    assert versions == sorted(set(versions))
    assert versions[-1] == store.CURRENT_SCHEMA_VERSION


def test_foreign_keys_are_enforced(db):
    """SQLite defaults them off, and a default-off foreign key does nothing."""
    with pytest.raises(sqlite3.IntegrityError), store.open_db() as conn:
        conn.execute("INSERT INTO activity_laps (activity_id, lap_index) VALUES (4242, 1)")


def test_a_failed_write_rolls_back(db):
    with store.open_db() as conn:
        conn.execute(
            "INSERT INTO athlete (athlete_id, created_at, updated_at) VALUES (1, 'x', 'x')"
        )
    with pytest.raises(RuntimeError), store.open_db() as conn:
        conn.execute("UPDATE athlete SET display_name = 'partial'")
        raise RuntimeError("something went wrong half-way")
    with store.open_db() as conn:
        assert conn.execute("SELECT display_name FROM athlete").fetchone()[0] is None


def test_db_status_reports_a_missing_database_without_creating_it(db):
    status = store.db_status()
    assert status["exists"] is False
    assert status["schema_version"] is None
    assert not db.exists()


def test_db_status_reports_the_version_of_a_real_database(db):
    with store.open_db():
        pass
    status = store.db_status()
    assert status["exists"] is True
    assert status["schema_version"] == store.CURRENT_SCHEMA_VERSION
    assert status["expected_schema_version"] == store.CURRENT_SCHEMA_VERSION


def test_a_file_that_is_not_a_database_is_refused_with_the_real_reason(db):
    """sqlite3.Error is not an OSError, so the StoreError was unreachable — and
    the rollback in the cleanup path raised over the top of the real cause."""
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("this is not a database", encoding="utf-8")
    with pytest.raises(store.StoreError) as exc, store.open_db():
        pass
    assert "file is not a database" in str(exc.value)
    assert str(db) in str(exc.value)
    assert "cannot rollback" not in str(exc.value)


def test_an_unwritable_location_is_refused_with_the_path_and_the_env_var(tmp_path, monkeypatch):
    blocked = tmp_path / "readonly"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv(store.ENV_DB_PATH, str(blocked / "coach.db"))
    try:
        with pytest.raises(store.StoreError) as exc, store.open_db():
            pass
        assert store.ENV_DB_PATH in str(exc.value)
    finally:
        blocked.chmod(0o700)

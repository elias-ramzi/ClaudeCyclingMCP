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
    assert store.migrate(conn) == [version for version, _ in store.MIGRATIONS if version > 1]
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


def _build_v4(path):
    """A database as it stands after the first four migrations and nothing
    else — the pinned starting point for the sequence-floor tests below, since
    migration 5 (the rebuild that drops the mark) is the very next one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Called for its side effect (creating the `schema_version` table), same
    # as `_build_v1` does elsewhere in this file — not a dead call.
    store.schema_version(conn)
    for version, statements in store.MIGRATIONS[:4]:
        for statement in statements():
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-01-01T00:00:00Z')",
            (version,),
        )
    conn.close()


_NOW = "2026-01-01T00:00:00Z"


def _insert_food_log(conn, label):
    return conn.execute(
        "INSERT INTO food_log (log_date, slot, label, kcal, protein_g, fiber_g, "
        "logged_at, updated_at) VALUES ('2026-01-01', 'breakfast', ?, 100, 5, 2, ?, ?)",
        (label, _NOW, _NOW),
    ).lastrowid


def test_a_v4_database_with_a_deleted_food_log_tail_does_not_reuse_the_id(db):
    """The pinned scenario (review round 8, finding 4): migration 5's rebuild
    does not carry `sqlite_sequence` forward on its own — `DROP TABLE
    food_log` deletes food_log's own high-water mark, and the RENAME hands
    the rebuilt table only `MAX(id)` of the rows that survived the copy. A
    stale pre-migration id — a queued `edit_log_entry(2, ...)` — would then
    silently edit a different, newer entry instead of erroring. Migration 5
    itself must not change to fix this (append-only); the `migrate()`
    framework guard is what protects this v4-or-earlier path."""
    _build_v4(db)
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _insert_food_log(conn, "item 1")
    _insert_food_log(conn, "item 2")
    assert (
        conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'food_log'").fetchone()["seq"]
        == 2
    )
    conn.execute("DELETE FROM food_log WHERE id = 2")
    conn.close()

    with store.open_db() as conn:
        assert store.schema_version(conn) == store.CURRENT_SCHEMA_VERSION
        new_id = _insert_food_log(conn, "new item")
    assert new_id == 3


def test_a_fresh_database_still_assigns_sequential_food_log_ids(db):
    """No regression: a database with nothing to restore (migrated 0 ->
    current in one call, before anything is ever inserted) must still hand
    out plain sequential ids."""
    with store.open_db() as conn:
        ids = [_insert_food_log(conn, f"item {i}") for i in (1, 2, 3)]
    assert ids == [1, 2, 3]


def test_a_v4_database_without_deletions_does_not_inflate_the_next_id(db):
    """Just outside the guard: when the pre-migration mark already equals
    MAX(id) (nothing was ever deleted), restoring it must not push the next
    id past max + 1."""
    _build_v4(db)
    conn = sqlite3.connect(db, isolation_level=None)
    _insert_food_log(conn, "item 1")
    _insert_food_log(conn, "item 2")
    conn.close()

    with store.open_db() as conn:
        new_id = _insert_food_log(conn, "item 3")
    assert new_id == 3


def test_a_v4_database_with_every_food_log_row_deleted_still_restores_the_floor(db):
    """The INSERT branch of `_restore_sqlite_sequence_floor`: when every
    `food_log` row is deleted before migrating, migration 5's rebuild copies
    zero rows, and the RENAME hands the rebuilt table no `sqlite_sequence` row
    at all (not merely a low one) — the guard's `row is None` branch is what
    reinstates it, rather than the `UPDATE` branch exercised above."""
    _build_v4(db)
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _insert_food_log(conn, "item 1")
    _insert_food_log(conn, "item 2")
    _insert_food_log(conn, "item 3")
    conn.execute("DELETE FROM food_log")
    assert (
        conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'food_log'").fetchone()["seq"]
        == 3
    )
    conn.close()

    with store.open_db() as conn:
        new_id = _insert_food_log(conn, "new item")
    assert new_id == 4


def test_the_sequence_guard_leaves_untouched_tables_alone(db):
    """The guard is generic, not food_log-specific — it snapshots and
    restores every AUTOINCREMENT table uniformly. Only food_log is rebuilt by
    a migration today, so this pins the other half of that: a table nothing
    rebuilds must come out of migrate() with its sqlite_sequence unchanged,
    deletions and all."""
    _build_v4(db)
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO ftp_history (athlete_id, value_watts, effective_date, method, recorded_at) "
        "VALUES (1, 250, '2026-01-01', 'ramp_test', ?)",
        (_NOW,),
    )
    conn.execute("DELETE FROM ftp_history WHERE id = 1")
    conn.execute(
        "INSERT INTO ftp_history (athlete_id, value_watts, effective_date, method, recorded_at) "
        "VALUES (1, 260, '2026-02-01', 'ramp_test', ?)",
        (_NOW,),
    )
    before_seq = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'ftp_history'"
    ).fetchone()["seq"]
    conn.close()

    with store.open_db() as conn:
        after_seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'ftp_history'"
        ).fetchone()["seq"]
    assert after_seq == before_seq == 2


def test_migration_5_statements_are_byte_identical():
    """Migration 5 is append-only and has run: this pins its exact text so an
    edit to fix the sequence issue in-place (rather than via migration 6 and
    the migrate() guard) fails loudly instead of shipping silently."""
    assert store._migrate_5_food_log_nullable_macros() == [
        """
        CREATE TABLE food_log_v5 (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id            INTEGER NOT NULL DEFAULT 1,
            log_date              TEXT NOT NULL,
            slot                  TEXT NOT NULL,
            ingredient_id         INTEGER REFERENCES ingredients(id) ON DELETE SET NULL,
            label                 TEXT NOT NULL,
            grams                 REAL,
            kcal                  REAL NOT NULL,
            protein_g             REAL,
            fiber_g               REAL,
            carbs_g               REAL,
            fat_g                 REAL,
            cost                  REAL,
            counts_toward_protein INTEGER NOT NULL DEFAULT 1,
            is_estimate           INTEGER NOT NULL DEFAULT 0,
            meal_id               INTEGER REFERENCES meals(id) ON DELETE SET NULL,
            meal_name             TEXT,
            note                  TEXT,
            logged_at             TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        )
        """,
        "INSERT INTO food_log_v5 (id, athlete_id, log_date, slot, ingredient_id, label, grams, "
        "kcal, protein_g, fiber_g, carbs_g, fat_g, cost, counts_toward_protein, is_estimate, "
        "meal_id, meal_name, note, logged_at, updated_at) "
        "SELECT id, athlete_id, log_date, slot, ingredient_id, label, grams, kcal, protein_g, "
        "fiber_g, carbs_g, fat_g, cost, counts_toward_protein, is_estimate, meal_id, meal_name, "
        "note, logged_at, updated_at FROM food_log",
        "DROP TABLE food_log",
        "ALTER TABLE food_log_v5 RENAME TO food_log",
        "CREATE INDEX ix_food_log_date ON food_log(athlete_id, log_date, slot)",
    ]


def test_migration_6_is_registered_and_current_version_follows():
    assert store.MIGRATIONS[-1][0] == 6
    assert store.CURRENT_SCHEMA_VERSION == 6


def test_migration_6_is_a_no_op_on_a_database_already_correct(db):
    """Idempotency: running migration 6's own statements twice against a
    database whose sequence is already right must not change anything."""
    with store.open_db() as conn:
        _insert_food_log(conn, "item 1")
        for statement in store._migrate_6_food_log_sequence():
            conn.execute(statement)
        seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'food_log'").fetchone()[
            "seq"
        ]
    assert seq == 1


def test_an_unreachable_location_is_refused_with_the_path_and_the_env_var(tmp_path, monkeypatch):
    """The parent is a file, so the directory cannot be created.

    Not a chmod: POSIX permission bits do not make a directory unwritable on
    Windows, so that version of this test passed everywhere except the one
    place it ran and quietly asserted nothing. A file in the way fails on every
    platform, and reaches the same code path.
    """
    wall = tmp_path / "wall"
    wall.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(store.ENV_DB_PATH, str(wall / "sub" / "coach.db"))

    with pytest.raises(store.StoreError) as exc, store.open_db():
        pass
    assert store.ENV_DB_PATH in str(exc.value)
    assert "coach.db" in str(exc.value)

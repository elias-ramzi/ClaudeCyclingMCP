"""SQLite persistence for the coaching layer: where it lives, and its schema.

This is the one module in the package that touches state. Everything above it
— renderers, metrics, verification — stays pure, and the coaching tools stay
pure functions of what is stored plus what is passed in.

The database holds an athlete profile, dated FTP/weight/HR history, objectives,
a normalised cache of imported Garmin activities, and planned sessions. It is
the athlete's file, not a mirror of Garmin: anything not stored here is still
Garmin's to answer for.

Location, in order: the ``CLAUDE_CYCLING_DB`` environment variable, else
``~/.claude-cycling/coach.db``. Parent directories are created on first write.
WAL is enabled so a long read cannot block a write, and foreign keys are
enabled per connection — SQLite defaults them off, and a default-off foreign
key is a foreign key that does nothing.

Migrations are ordered functions with a version number, applied in sequence and
recorded in ``schema_version``. They are append-only: **never edit a migration
that has run on a real database**, because the only thing that reruns is a
version that has not been applied. Change the schema by adding the next one.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ENV_DB_PATH = "CLAUDE_CYCLING_DB"
DEFAULT_DB_PATH = Path.home() / ".claude-cycling" / "coach.db"

#: The athlete every table defaults to. One athlete is the whole use case
#: today; the column exists so adding a second one is a schema no-op.
DEFAULT_ATHLETE_ID = 1


def db_path() -> Path:
    """Where the coaching database lives, without touching the filesystem.

    Safe to call for reporting — it neither creates the file nor its parent.
    """
    override = os.environ.get(ENV_DB_PATH, "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_DB_PATH


def now_utc() -> str:
    """A UTC timestamp for provenance columns, to the second."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------


def _migrate_1_training_log() -> list[str]:
    """The athlete and what they actually did: profile, dated history, rides.

    Every history table is append-only and dated, because a single "current
    FTP" column silently rewrites the past: a ride from March scored against a
    July FTP is scored against an athlete who did not exist yet.
    """
    return [
        """
        CREATE TABLE athlete (
            athlete_id   INTEGER PRIMARY KEY,
            display_name TEXT,
            height_cm    REAL,
            birth_year   INTEGER,
            availability TEXT,
            equipment    TEXT,
            constraints  TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE ftp_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id     INTEGER NOT NULL DEFAULT 1,
            value_watts    INTEGER NOT NULL,
            effective_date TEXT NOT NULL,
            method         TEXT,
            note           TEXT,
            recorded_at    TEXT NOT NULL
        )
        """,
        "CREATE INDEX ix_ftp_history_date ON ftp_history(athlete_id, effective_date)",
        """
        CREATE TABLE weight_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id     INTEGER NOT NULL DEFAULT 1,
            value_kg       REAL NOT NULL,
            effective_date TEXT NOT NULL,
            note           TEXT,
            recorded_at    TEXT NOT NULL
        )
        """,
        "CREATE INDEX ix_weight_history_date ON weight_history(athlete_id, effective_date)",
        """
        CREATE TABLE hr_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id     INTEGER NOT NULL DEFAULT 1,
            threshold_hr   INTEGER,
            max_hr         INTEGER,
            resting_hr     INTEGER,
            effective_date TEXT NOT NULL,
            method         TEXT,
            note           TEXT,
            recorded_at    TEXT NOT NULL
        )
        """,
        "CREATE INDEX ix_hr_history_date ON hr_history(athlete_id, effective_date)",
        """
        CREATE TABLE activities (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id         INTEGER NOT NULL DEFAULT 1,
            garmin_activity_id TEXT,
            name               TEXT,
            sport              TEXT,
            sub_sport          TEXT,
            start_time_utc     TEXT,
            start_time_local   TEXT,
            local_date         TEXT NOT NULL,
            duration_s         REAL,
            moving_duration_s  REAL,
            distance_m         REAL,
            elevation_gain_m   REAL,
            avg_hr             INTEGER,
            max_hr             INTEGER,
            avg_power          REAL,
            max_power          REAL,
            normalized_power   REAL,
            calories           REAL,
            source             TEXT NOT NULL DEFAULT 'garmin',
            raw_json           TEXT,
            imported_at        TEXT NOT NULL
        )
        """,
        # Partial, so manually entered rides (no Garmin id) are not all
        # duplicates of one another under a single NULL key.
        """
        CREATE UNIQUE INDEX ux_activities_garmin
            ON activities(athlete_id, garmin_activity_id)
            WHERE garmin_activity_id IS NOT NULL
        """,
        "CREATE INDEX ix_activities_date ON activities(athlete_id, local_date)",
        """
        CREATE TABLE activity_laps (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id       INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
            lap_index         INTEGER NOT NULL,
            duration_s        REAL,
            moving_duration_s REAL,
            distance_m        REAL,
            avg_power         REAL,
            max_power         REAL,
            normalized_power  REAL,
            avg_hr            INTEGER,
            max_hr            INTEGER,
            avg_cadence       REAL,
            elevation_gain_m  REAL,
            raw_json          TEXT
        )
        """,
        "CREATE UNIQUE INDEX ux_activity_laps ON activity_laps(activity_id, lap_index)",
    ]


def _migrate_2_plan_and_debrief() -> list[str]:
    """The coaching layer over the log: objectives, sessions, and how it felt.

    Separate from v1 because the log stands on its own — an athlete can import
    rides and read their form without ever declaring an objective. This adds
    what the training is *for* (events), what was prescribed
    (planned_workouts), and the subjective read on a ride that no device
    records.
    """
    return [
        """
        CREATE TABLE events (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id         INTEGER NOT NULL DEFAULT 1,
            name               TEXT NOT NULL,
            event_date         TEXT NOT NULL,
            distance_km        REAL,
            elevation_m        REAL,
            priority           TEXT,
            status             TEXT NOT NULL DEFAULT 'upcoming',
            note               TEXT,
            linked_activity_id INTEGER REFERENCES activities(id) ON DELETE SET NULL,
            finish_time_s      INTEGER,
            debrief            TEXT,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        )
        """,
        "CREATE INDEX ix_events_date ON events(athlete_id, event_date)",
        """
        CREATE TABLE planned_workouts (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id         INTEGER NOT NULL DEFAULT 1,
            spec_json          TEXT NOT NULL,
            scheduled_date     TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'planned',
            linked_activity_id INTEGER REFERENCES activities(id) ON DELETE SET NULL,
            pushed_to          TEXT,
            note               TEXT,
            warnings_json      TEXT,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        )
        """,
        "CREATE INDEX ix_planned_date ON planned_workouts(athlete_id, scheduled_date)",
        "ALTER TABLE activities ADD COLUMN rpe INTEGER",
        "ALTER TABLE activities ADD COLUMN feel TEXT",
        "ALTER TABLE activities ADD COLUMN note TEXT",
        "ALTER TABLE activities ADD COLUMN annotated_at TEXT",
    ]


MIGRATIONS: list[tuple[int, Callable[[], list[str]]]] = [
    (1, _migrate_1_training_log),
    (2, _migrate_2_plan_and_debrief),
]

CURRENT_SCHEMA_VERSION = MIGRATIONS[-1][0]


def schema_version(conn: sqlite3.Connection) -> int:
    """The highest migration applied, or 0 on a database with no schema yet."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  version INTEGER PRIMARY KEY,"
        "  applied_at TEXT NOT NULL"
        ")"
    )
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"] or 0)


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Bring `conn` up to CURRENT_SCHEMA_VERSION. Returns what was applied.

    Idempotent: on an already-current database this is one SELECT and an empty
    list. Statements are executed one at a time rather than through
    `executescript`, which issues an implicit COMMIT — that would drop the
    caller's transaction and leave a failed migration half-applied on disk.
    """
    current = schema_version(conn)
    applied: list[int] = []
    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        for statement in statements():
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, now_utc()),
        )
        applied.append(version)
    return applied


# --------------------------------------------------------------------------
# connections
# --------------------------------------------------------------------------


class StoreError(RuntimeError):
    """Raised when the database cannot be opened or written."""


@contextmanager
def open_db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open the coaching database, migrating it, and commit on clean exit.

    Creates the file and its parent directory if they are missing, so the first
    coaching tool call is also the install step — there is nothing to set up.
    That means any tool using this **creates state on the server's filesystem**;
    `server_info` reports the path so it is never a surprise.

    Rolls back and re-raises on any exception, so a tool that fails half-way
    leaves no partial write behind.
    """
    target = Path(path) if path is not None else db_path()

    # sqlite3.Error is not an OSError, so "the directory is read-only" and
    # "this file is not a database" arrive as different exception families for
    # the same user-visible problem. Both are the same answer: the file could
    # not be opened, and here is where it was looked for.
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, isolation_level=None)
    except (OSError, sqlite3.Error) as exc:
        raise StoreError(_open_failure(target, exc)) from exc

    conn.row_factory = sqlite3.Row
    try:
        # WAL survives the connection, so this is a one-off in practice. It is
        # set every time because a database restored from an export, or copied
        # between machines, arrives in the default rollback-journal mode.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN")
    except (OSError, sqlite3.Error) as exc:
        # Nothing has begun, so there is nothing to roll back — and attempting
        # it here raises "cannot rollback - no transaction is active", which
        # would replace the real cause with a message about the cleanup.
        conn.close()
        raise StoreError(_open_failure(target, exc)) from exc

    try:
        migrate(conn)
        yield conn
    except BaseException:
        _rollback(conn)
        conn.close()
        raise
    conn.execute("COMMIT")
    conn.close()


def _open_failure(target: Path, exc: BaseException) -> str:
    detail = getattr(exc, "strerror", None) or str(exc)
    return (
        f"Could not open the coaching database at {target}: {detail}. It is resolved on the "
        f"machine running this MCP server; set the {ENV_DB_PATH} environment variable to move "
        f"it. If the file exists but is not a database, move it aside — this server will not "
        f"overwrite it."
    )


def _rollback(conn: sqlite3.Connection) -> None:
    """Undo the open transaction, without letting the cleanup become the error.

    A failure during rollback is not the failure worth reporting: the caller is
    already unwinding one, and raising here would replace a real cause with a
    message about tidying up.
    """
    with contextlib.suppress(sqlite3.Error):
        conn.execute("ROLLBACK")


def db_status() -> dict:
    """Describe the database without creating it.

    `server_info` calls this, and a tool whose job is to report the state of
    the world must not change it — opening the file would create it, and then
    "exists: true" would only ever mean "you asked".
    """
    path = db_path()
    status: dict = {
        "path": str(path),
        "exists": path.exists(),
        "env_var": ENV_DB_PATH,
        "expected_schema_version": CURRENT_SCHEMA_VERSION,
    }
    if not path.exists():
        status["schema_version"] = None
        status["note"] = "Created on the first coaching tool call; nothing to set up."
        return status

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            status["schema_version"] = int(row["v"] or 0)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        status["schema_version"] = None
        status["error"] = f"{path} exists but could not be read: {exc}"
    return status

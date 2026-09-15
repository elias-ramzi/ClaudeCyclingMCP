"""SQLite persistence for the coaching layer: where it lives, and its schema.

This is the one module in the package that touches state. Everything above it
— renderers, metrics, verification — stays pure, and the coaching tools stay
pure functions of what is stored plus what is passed in.

The database holds an athlete profile, dated FTP/weight/HR history, objectives,
a normalised cache of imported Garmin activities, planned sessions, and the
nutrition layer over the same athlete: ingredients, standard meals, the food
log and its dated targets. It is the athlete's file, not a mirror of Garmin:
anything not stored here is still Garmin's to answer for.

Training and nutrition share one file deliberately. A calorie target computed
without the day's ride is a target for somebody else, and that join is only
free while both live here.

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


def _migrate_3_import_flags() -> list[str]:
    """Keep what the import noticed about a ride's data quality.

    `local_date_from_utc`, `no_power`, `no_sport_type` were computed at import
    and reported once, in the response to that call. Every later read — the
    week, the load, the compliance report — then saw a row with no indication
    that its date might be a day out or its sport unknown. A caveat that exists
    only in a transcript is a caveat nobody has.
    """
    return ["ALTER TABLE activities ADD COLUMN flags_json TEXT"]


def _migrate_4_nutrition() -> list[str]:
    """The nutrition layer: what is eaten, what it costs, and what to aim at.

    Kept in the same database as the training log on purpose. A calorie target
    that ignores what the athlete rode that day is a target for somebody else,
    and joining across two files would mean either a second store to keep in
    step or a target computed from numbers passed in by hand.

    `gender` goes on the athlete because Mifflin-St Jeor needs it and nothing
    before this did. Height and birth year are already there; weight is not,
    and deliberately stays in `weight_history` — a BMR is computed against the
    weight in effect on the day being planned, not against a column somebody
    last updated in March.
    """
    return [
        # NULL means "not asked yet", which `suggest_targets` reports as a gap
        # rather than guessing. A guessed gender moves BMR by 166 kcal/day.
        "ALTER TABLE athlete ADD COLUMN gender TEXT",
        # Append-only and dated, like ftp_history: what the athlete was aiming
        # at in March explains a March deficit, and overwriting it makes every
        # past target unreadable.
        """
        CREATE TABLE nutrition_goals (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id        INTEGER NOT NULL DEFAULT 1,
            goal_type         TEXT NOT NULL,
            target_weight_kg  REAL,
            milestone_weight_kg REAL,
            rate_kg_per_week  REAL,
            status            TEXT NOT NULL DEFAULT 'active',
            note              TEXT,
            effective_date    TEXT NOT NULL,
            closed_date       TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        )
        """,
        "CREATE INDEX ix_nutrition_goals ON nutrition_goals(athlete_id, status, effective_date)",
        # `name_key` is the accent- and case-folded name. Stored rather than
        # folded per query so the uniqueness constraint sees what the resolver
        # sees: without it "Skyr" and "skyr" are two rows, and logging one of
        # them is a coin toss.
        #
        # kcal/protein/fibre are NOT NULL because every target is computed from
        # them and a NULL would silently read as zero — an ingredient with no
        # calories is a rounding error that eats a day's deficit. The rest are
        # nullable: forcing a full macro breakdown out of a packet that only
        # prints four numbers is how a food base stops being filled in.
        """
        CREATE TABLE ingredients (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id            INTEGER NOT NULL DEFAULT 1,
            name                  TEXT NOT NULL,
            name_key              TEXT NOT NULL,
            aliases_json          TEXT,
            kcal_100g             REAL NOT NULL,
            protein_100g          REAL NOT NULL,
            fiber_100g            REAL NOT NULL,
            carbs_100g            REAL,
            fat_100g              REAL,
            sat_fat_100g          REAL,
            sugar_100g            REAL,
            salt_100g             REAL,
            state                 TEXT NOT NULL DEFAULT 'as_sold',
            default_portion_g     REAL,
            portion_label         TEXT,
            package_price         REAL,
            package_weight_g      REAL,
            counts_toward_protein INTEGER NOT NULL DEFAULT 1,
            note                  TEXT,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX ux_ingredients_name ON ingredients(athlete_id, name_key)",
        """
        CREATE TABLE meals (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id       INTEGER NOT NULL DEFAULT 1,
            name             TEXT NOT NULL,
            name_key         TEXT NOT NULL,
            default_for_slot TEXT,
            note             TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX ux_meals_name ON meals(athlete_id, name_key)",
        # No macros here, by design. A meal is a list of ingredients and their
        # grams; its calories are computed from the ingredient rows every time
        # it is read. Denormalising them would freeze a breakfast's protein at
        # whatever the skyr pot said the day the meal was saved.
        """
        CREATE TABLE meal_items (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            meal_id       INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
            position      INTEGER NOT NULL,
            ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
            grams         REAL NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX ux_meal_items ON meal_items(meal_id, position)",
        # The opposite rule to `meals`, for the opposite reason. A log entry's
        # macros and cost are frozen at log time: the athlete ate what the
        # ingredient said *then*, and correcting a mistyped protein figure
        # today must not rewrite last month's days into ones that never
        # happened. `label` is likewise the name as it stood, so an entry stays
        # readable after a rename — and readable at all for a free-form
        # estimate, which has no ingredient row behind it.
        """
        CREATE TABLE food_log (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id            INTEGER NOT NULL DEFAULT 1,
            log_date              TEXT NOT NULL,
            slot                  TEXT NOT NULL,
            ingredient_id         INTEGER REFERENCES ingredients(id) ON DELETE SET NULL,
            label                 TEXT NOT NULL,
            grams                 REAL,
            kcal                  REAL NOT NULL,
            protein_g             REAL NOT NULL,
            fiber_g               REAL NOT NULL,
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
        "CREATE INDEX ix_food_log_date ON food_log(athlete_id, log_date, slot)",
        # One row per date: a day has one set of targets, and two would leave
        # every remainder depending on which was read.
        """
        CREATE TABLE daily_targets (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id     INTEGER NOT NULL DEFAULT 1,
            target_date    TEXT NOT NULL,
            kcal           REAL NOT NULL,
            protein_g      REAL NOT NULL,
            fiber_g        REAL NOT NULL,
            day_type       TEXT NOT NULL,
            source         TEXT NOT NULL,
            rationale_json TEXT,
            note           TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX ux_daily_targets ON daily_targets(athlete_id, target_date)",
    ]


def _migrate_5_food_log_nullable_macros() -> list[str]:
    """`food_log.protein_g` / `.fiber_g` become nullable: unknown is not zero.

    A free-form estimate — canteen food, a restaurant plate — states what it
    states. `kcal` is always given; `protein_g`/`fiber_g` are not always known,
    and storing an unstated figure as `0.0` is a stated zero wearing an unknown
    one's clothes: `day_summary`'s remainder then overstates what is left by
    the whole meal, silently.

    SQLite has no `ALTER COLUMN`, so this is the standard rebuild: a new table
    with the two columns nullable, copy every row across by name (nothing else
    changes), drop the old table, rename, and recreate the index that named the
    old one. Every other column and constraint is byte-identical to migration 4.
    """
    return [
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


def _migrate_6_food_log_sequence() -> list[str]:
    """Tombstone for the pre-guard loss window. Changes no id assignment on
    any database this migration actually runs against.

    Migration 5's rebuild (see its own docstring — it is append-only and
    stays exactly as it ran) leaves `sqlite_sequence` exactly where it needs
    to be on every real v5 database: `INSERT ... SELECT` into `food_log_v5`
    writes that table's own `sqlite_sequence` row as it runs (even when it
    copies zero rows), so after the copy `seq == MAX(id)` of `food_log_v5`
    *by construction* — whether or not the pre-DROP mark on the old
    `food_log` was higher, which is exactly the loss the next paragraph
    covers. `ALTER TABLE ... RENAME` then carries that row across untouched
    — only `DROP TABLE` deletes a `sqlite_sequence` row, and the DROP here
    targets the old `food_log`, not the renamed `food_log_v5`. `seq ==
    MAX(id)` is precisely the condition both statements below test for
    (`NOT EXISTS` a row, or `seq < MAX(id)`), so on any database that
    reaches this migration, neither one fires.

    They would still change no id assignment even in a database where the
    row somehow read absent or low, because SQLite's own AUTOINCREMENT
    allocation is `max(sqlite_sequence.seq, MAX(rowid)) + 1` regardless of
    what the table says — the next id handed out is identical whether or not
    this migration runs. What this migration cannot do, and could never have
    done, is recover an id from the actual loss window: a row deleted from
    `food_log` before `migrate()` grew its own snapshot/restore guard had its
    id handed out again the moment migration 5's rebuild ran, and that
    reassignment is long since committed by the time any later migration
    could look. It is `migrate()`'s snapshot/restore guard — which captures
    `sqlite_sequence` *before* migration 5's DROP — that protects a database
    migrating from v4 or earlier; this migration runs after the fact and has
    nothing left to catch.

    Pure SQL and idempotent regardless (a migration only ever runs once per
    database anyway). Its INSERT arm is dead code against every state above —
    kept only as a guard for a hypothetical future rebuild that drops a
    table's `sqlite_sequence` row entirely outside `migrate()`'s own
    bracketing guard.
    """
    return [
        "INSERT INTO sqlite_sequence (name, seq) "
        "SELECT 'food_log', IFNULL((SELECT MAX(id) FROM food_log), 0) "
        "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'food_log')",
        "UPDATE sqlite_sequence SET seq = (SELECT IFNULL(MAX(id), 0) FROM food_log) "
        "WHERE name = 'food_log' "
        "AND seq < (SELECT IFNULL(MAX(id), 0) FROM food_log)",
    ]


def _migrate_7_pre_link_status() -> list[str]:
    """`planned_workouts.pre_link_status`: the status `link_activity` auto-
    completed FROM, so unlinking can revert to it directly instead of
    re-deriving a guess from `pushed_to`.

    `pushed_to` lingering after a session is walked back to `planned`, or
    left NULL on a session that was genuinely `pushed`, made the old
    inference wrong in both directions — and a session pushed *after* being
    linked read the pre-update NULL and reverted to `planned` under a note
    claiming the workout was never sent anywhere. Recording the real value at
    link time removes the guess entirely; a row linked before this migration
    has NULL here and the caller falls back to the old inference, softening
    the claim it can no longer back up.
    """
    return ["ALTER TABLE planned_workouts ADD COLUMN pre_link_status TEXT"]


MIGRATIONS: list[tuple[int, Callable[[], list[str]]]] = [
    (1, _migrate_1_training_log),
    (2, _migrate_2_plan_and_debrief),
    (3, _migrate_3_import_flags),
    (4, _migrate_4_nutrition),
    (5, _migrate_5_food_log_nullable_macros),
    (6, _migrate_6_food_log_sequence),
    (7, _migrate_7_pre_link_status),
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


def _snapshot_sqlite_sequence(conn: sqlite3.Connection) -> dict[str, int]:
    """`{table_name: seq}` for every AUTOINCREMENT table, or `{}` on a brand
    new database that has no tables — and so no `sqlite_sequence` — yet."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
    ).fetchone()
    if not exists:
        return {}
    return {
        row["name"]: row["seq"] for row in conn.execute("SELECT name, seq FROM sqlite_sequence")
    }


def _restore_sqlite_sequence_floor(conn: sqlite3.Connection, before: dict[str, int]) -> None:
    """Raise `sqlite_sequence` back to its pre-migration mark wherever a
    migration just applied left it lower — never lowers or invents a mark.

    A rebuild migration (SQLite has no `ALTER COLUMN`, so relaxing a
    constraint is CREATE new table + `INSERT ... SELECT` + DROP old + RENAME)
    only carries forward the AUTOINCREMENT high-water mark of the rows that
    survived the copy: `DROP TABLE` deletes that table's own
    `sqlite_sequence` row, and the rename hands the rebuilt table just
    `MAX(id)` of what got copied. A row deleted before the rebuild — its id
    already past `MAX(id)` of what remains — gets handed out again to
    whatever is inserted next, silently.
    """
    for name, seq in before.items():
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        if not table_exists:
            continue
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (name,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (name, seq))
        elif row["seq"] < seq:
            conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (seq, name))


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Bring `conn` up to CURRENT_SCHEMA_VERSION. Returns what was applied.

    Idempotent: on an already-current database this is one SELECT and an empty
    list. Statements are executed one at a time rather than through
    `executescript`, which issues an implicit COMMIT — that would drop the
    caller's transaction and leave a failed migration half-applied on disk.

    Brackets the whole walk with an AUTOINCREMENT high-water-mark guard: the
    `sqlite_sequence` table is snapshotted before any pending migration runs,
    and restored afterward wherever a migration left a table's mark lower
    than it stood before (see `_restore_sqlite_sequence_floor`). This lives
    here, in the framework, rather than in any one migration, because a
    rebuild migration cannot see its own pre-DROP `sqlite_sequence` state
    from a *later* migration — by the time anything downstream could read it,
    the DROP already deleted it. Only code that brackets the entire walk has
    both the before and the after in hand. It also means every future rebuild
    migration is covered by construction, not by remembering to add this
    again.
    """
    current = schema_version(conn)
    pending = [(version, statements) for version, statements in MIGRATIONS if version > current]
    if not pending:
        return []

    before_sequence = _snapshot_sqlite_sequence(conn)
    applied: list[int] = []
    for version, statements in pending:
        for statement in statements():
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, now_utc()),
        )
        applied.append(version)
    _restore_sqlite_sequence_floor(conn, before_sequence)
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

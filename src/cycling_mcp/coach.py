"""The coaching operations: read and write the athlete's file, compute from it.

`server.py` wraps each of these in a tool; the judgement about what to do with
the answers lives in the bundled `coaching` skill, which is instructions for a
model rather than code. The split is deliberate. Storing, resolving a dated FTP,
and running 40 days of exponential smoothing are things code does reliably and a
model does not; deciding whether a missed Tuesday matters is the other way round.

Everything here returns plain JSON-shaped dicts. Every write returns what was
actually stored rather than an acknowledgement, so the caller can check what it
believes against what is on disk without a second round trip.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from .garmin_import import (
    as_activity_list,
    as_lap_list,
    normalize_activity,
    normalize_lap,
    row_flags,
)
from .metrics import compute_metrics, describe
from .spec import (
    FTP_PLAUSIBLE_W,
    FTP_USUAL_W,
    SpecError,
    format_duration,
    load_spec,
    parse_duration,
)
from .spec import validate_spec as _validate_spec
from .store import CURRENT_SCHEMA_VERSION, DEFAULT_ATHLETE_ID, now_utc, open_db
from .training import (
    COMPLIANT_VERDICTS,
    LTHR_FROM_MAX_HR,
    TWENTY_MINUTE_FACTOR,
    UNVERIFIABLE_VERDICTS,
    Load,
    compare_block,
    compute_activity_load,
    form_series,
    hr_zones,
    parse_date,
    power_zones,
)
from .verify import payload_digest

PLANNED_STATUSES = ("planned", "pushed", "completed", "missed", "skipped")
EVENT_STATUSES = ("upcoming", "completed", "abandoned", "dns")
EVENT_PRIORITIES = ("A", "B", "C")
PUSH_TARGETS = ("garmin", "mywhoosh")
FTP_METHODS = (
    "stated",
    "20min_test",
    "ramp_test",
    "8min_test",
    "garmin_profile",
    "estimated",
    "race",
    "other",
)
HR_METHODS = ("stated", "lab_test", "field_test", "garmin_profile", "estimated", "other")
SPORTS = ("cycling", "running", "swimming", "other")

# Plausibility limits. Outside these a value is far more likely to be the wrong
# unit or the wrong field than a real measurement, and storing it would poison
# every zone and every load figure computed afterwards.
FTP_LIMITS = FTP_PLAUSIBLE_W
FTP_USUAL = FTP_USUAL_W
WEIGHT_LIMITS_KG = (25.0, 300.0)
# Above this a weight is worth querying as a possible pounds figure. It is a
# question, not a limit: the two ranges genuinely overlap for adults.
POUNDS_SUSPICION_KG = 120.0
HR_LIMITS = (25, 240)

_ACTIVITY_OUT_FIELDS = (
    "id",
    "garmin_activity_id",
    "name",
    "sport",
    "sub_sport",
    "start_time_utc",
    "start_time_local",
    "local_date",
    "duration_s",
    "moving_duration_s",
    "distance_m",
    "elevation_gain_m",
    "avg_hr",
    "max_hr",
    "avg_power",
    "max_power",
    "normalized_power",
    "calories",
    "source",
    "rpe",
    "feel",
    "note",
    "flags_json",
    "imported_at",
)

# The fields an import writes. Everything else on the row — the subjective
# layer, the local id — belongs to this server, not to Garmin.
_IMPORT_FIELDS = (
    "name",
    "sport",
    "sub_sport",
    "start_time_utc",
    "start_time_local",
    "local_date",
    "duration_s",
    "moving_duration_s",
    "distance_m",
    "elevation_gain_m",
    "avg_hr",
    "max_hr",
    "avg_power",
    "max_power",
    "normalized_power",
    "calories",
    "source",
    "flags_json",
)

#: The activity columns every read needs. Naming them keeps `raw_json` — the
#: whole Garmin payload, by far the largest column — out of queries that return
#: hundreds of rows and never look at it.
_ACTIVITY_SELECT = ", ".join(_ACTIVITY_OUT_FIELDS)

_LAP_OUT_FIELDS = (
    "lap_index",
    "duration_s",
    "moving_duration_s",
    "distance_m",
    "avg_power",
    "max_power",
    "normalized_power",
    "avg_hr",
    "max_hr",
    "avg_cadence",
    "elevation_gain_m",
)


class CoachError(ValueError):
    """A refusal with a reason the caller can act on."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _dict(row: sqlite3.Row | None) -> dict | None:
    return None if row is None else dict(zip(row.keys(), tuple(row), strict=True))


def _project(row: sqlite3.Row | dict, fields: tuple[str, ...]) -> dict:
    source = row if isinstance(row, dict) else _dict(row) or {}
    projected = {field: source.get(field) for field in fields}
    if "flags_json" in projected:
        # Handed back parsed. A caller comparing against a list should not have
        # to know this column holds JSON in a text field.
        raw = projected.pop("flags_json")
        projected["flags"] = json.loads(raw) if raw else []
    return projected


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _today(value: str | None = None) -> date:
    """The reference "today", explicit when given.

    Every tool that needs one takes it as an argument. A plan is read and
    written across timezones and after midnight, and "the server's today" is
    not always the athlete's — nor is it reproducible in a test.
    """
    return parse_date(value, "today") if value else date.today()


def _check_range(value: float, limits: tuple[float, float], what: str, unit: str) -> None:
    low, high = limits
    if not (low <= value <= high):
        raise CoachError(
            f"{what} of {value:g} {unit} is outside {low:g}-{high:g} {unit} — refusing to "
            f"store it. Check the units; a wrong {what} silently rescales every zone and "
            f"every training-load number computed from it."
        )


def _one_of(value: str | None, allowed: tuple[str, ...], what: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text not in allowed:
        raise CoachError(f"{what} must be one of {list(allowed)}, got {text!r}")
    return text


def _ensure_athlete(conn: sqlite3.Connection, athlete_id: int) -> dict:
    """The athlete row, created empty if this is the first call.

    Reading the profile is the onboarding entry point, so it has to work before
    anything has been said — an empty row with a full `gaps` list is the
    interview.
    """
    row = conn.execute("SELECT * FROM athlete WHERE athlete_id = ?", (athlete_id,)).fetchone()
    if row is None:
        stamp = now_utc()
        conn.execute(
            "INSERT INTO athlete (athlete_id, created_at, updated_at) VALUES (?, ?, ?)",
            (athlete_id, stamp, stamp),
        )
        row = conn.execute("SELECT * FROM athlete WHERE athlete_id = ?", (athlete_id,)).fetchone()
    return _dict(row) or {}


#: Dated history tables. Named here rather than interpolated from a caller's
#: argument, so a table name in a query is always one of these.
_HISTORY_TABLES = ("ftp_history", "weight_history", "hr_history")


def _load_history(conn: sqlite3.Connection, athlete_id: int, table: str) -> list[dict]:
    """Every entry in one dated history table, oldest first.

    Loaded whole. These tables hold one row per FTP test, weigh-in or HR entry
    — tens of rows for a serious athlete after years — so reading one once per
    request costs less than the per-activity queries it replaces: scoring a
    season used to issue up to eight indexed lookups per ride, for the same
    handful of rows every time.
    """
    if table not in _HISTORY_TABLES:
        raise CoachError(f"unknown history table {table!r}")
    return [
        _dict(row) or {}
        for row in conn.execute(
            f"SELECT * FROM {table} WHERE athlete_id = ? ORDER BY effective_date, id",
            (athlete_id,),
        )
    ]


def _resolve_rows(rows: list[dict], as_of: str | None, column: str | None = None) -> dict | None:
    """The entry in effect on `as_of`, from an oldest-first list.

    One resolver for every dated figure, because the rule is the same for all
    of them and two copies of it drift: the latest entry at or before the date,
    else — when the date precedes all of them — the earliest, flagged
    `extrapolated_backwards`.

    That fallback is deliberate. A ride from before the first recorded FTP has
    to be scored against *something*; refusing makes it vanish out of CTL,
    which reads as a rest week that never happened. The flag says the number is
    a guess about an athlete who was probably fitter or less fit than it says.

    `column` restricts the search to entries where that field is set, which is
    what lets a lone resting-HR entry sit in the table without shadowing the
    threshold recorded before it.
    """
    candidates = [row for row in rows if column is None or row.get(column) is not None]
    if not candidates:
        return None
    in_effect = [row for row in candidates if as_of is None or row["effective_date"] <= as_of]
    if in_effect:
        return {**in_effect[-1], "extrapolated_backwards": False}
    return {**candidates[0], "extrapolated_backwards": True}


class History:
    """The athlete's dated figures, read once and resolved in memory.

    Build one at the top of any tool that scores more than a single activity.
    The alternative — resolving from the database per ride — is what turns a
    loop over a season into thousands of queries for the same forty rows.
    """

    def __init__(self, conn: sqlite3.Connection, athlete_id: int) -> None:
        self.ftp_rows = _load_history(conn, athlete_id, "ftp_history")
        self.weight_rows = _load_history(conn, athlete_id, "weight_history")
        self.hr_rows = _load_history(conn, athlete_id, "hr_history")

    def ftp(self, on_date: str | None) -> dict | None:
        """The FTP in effect on a date. See `resolve_ftp` for why it is dated."""
        return _resolve_rows(self.ftp_rows, on_date)

    def weight(self, on_date: str | None) -> dict | None:
        return _resolve_rows(self.weight_rows, on_date)

    def hr(self, on_date: str | None) -> dict | None:
        """The HR figures in effect on a date, each resolved on its own.

        Threshold, maximum and resting are resolved **per field**, not by
        taking the latest row wholesale. `log_hr` accepts any subset — logging
        a resting HR on its own is a normal thing to do — and a row-at-a-time
        resolution would let that entry shadow the threshold recorded six
        months earlier. The athlete would then be told they have no threshold
        HR on file, and every no-power ride would stop being scored, because
        they mentioned their morning pulse.

        When only a maximum HR is known, threshold is estimated at 92% of it
        and flagged `threshold_hr_estimated`. That estimate is wide — measured
        LTHR lands either side of it routinely — so anything computed from it
        is a direction, not a measurement.
        """
        fields = {
            column: _resolve_rows(self.hr_rows, on_date, column)
            for column in ("threshold_hr", "max_hr", "resting_hr")
        }
        if not any(fields.values()):
            return None

        entry: dict[str, Any] = {
            column: (row[column] if row else None) for column, row in fields.items()
        }
        # Each figure keeps the date of the entry it actually came from; one
        # effective_date across three fields from three different days would be
        # a provenance claim that is not true.
        entry["effective_dates"] = {
            column: (row["effective_date"] if row else None) for column, row in fields.items()
        }
        entry["extrapolated_backwards"] = any(
            row["extrapolated_backwards"] for row in fields.values() if row
        )

        if entry.get("threshold_hr"):
            entry["threshold_hr_estimated"] = False
        elif entry.get("max_hr"):
            entry["threshold_hr"] = round(entry["max_hr"] * LTHR_FROM_MAX_HR)
            entry["threshold_hr_estimated"] = True
        else:
            entry["threshold_hr_estimated"] = False
        return entry


def _latest(
    conn: sqlite3.Connection, table: str, athlete_id: int, as_of: str | None
) -> dict | None:
    """The most recent entry at or before `as_of`, with no backwards guess.

    This answers "what was on file then", where None means nothing was.
    `resolve_ftp` is the one that extrapolates.
    """
    rows = _load_history(conn, athlete_id, table)
    in_effect = [row for row in rows if as_of is None or row["effective_date"] <= as_of]
    return in_effect[-1] if in_effect else None


def _row_by_id(conn: sqlite3.Connection, table: str, row_id: int | None) -> dict | None:
    """Read back exactly the row that was just written.

    Not `_latest`. A backdated entry — "my FTP test was actually three weeks
    ago" — is a normal thing to log, and re-reading by date returns a different
    row than the one just inserted. The write would then report someone else's
    numbers as what it stored, which is the one thing a write is supposed to be
    trustworthy about.
    """
    if row_id is None:
        return None
    if table not in _HISTORY_TABLES:
        raise CoachError(f"unknown history table {table!r}")
    return _dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())


def _dated_write_outcome(
    conn: sqlite3.Connection,
    table: str,
    athlete_id: int,
    effective_date: str,
    row_id: int,
    column: str | None = None,
) -> dict:
    """What a just-written dated entry means: does it govern today, what did it replace?

    Shared by all three loggers, because a backdated entry is a normal
    correction in every one of them and only `log_ftp` used to say so. Being
    handed today's W/kg after logging last month's weigh-in, or zones that are
    not in force after logging a threshold from March, is the same defect
    wearing different units.

    `column` narrows both questions to one field, which is what HR needs: a
    backdated resting HR supersedes nothing about the threshold.
    """
    rows = _load_history(conn, athlete_id, table)
    others = [
        row
        for row in rows
        if row["id"] != row_id and (column is None or row.get(column) is not None)
    ]
    later = [row for row in others if (row["effective_date"], row["id"]) > (effective_date, row_id)]
    earlier = [row for row in others if row["effective_date"] <= effective_date]
    return {
        "is_current": not later,
        "superseded_by": later[0] if later else None,
        "previous": earlier[-1] if earlier else None,
    }


def resolve_ftp(conn: sqlite3.Connection, athlete_id: int, on_date: str | None) -> dict | None:
    """The FTP in effect on `on_date` — the latest entry dated at or before it.

    This is the whole reason `ftp_history` is a table and not a column. Scoring
    a March ride against a July FTP inflates nothing and deflates everything:
    the same watts against a bigger number is a smaller IF, so a block of
    training silently shrinks the moment the athlete tests better.

    A ride *before* the first recorded FTP falls back to that earliest entry,
    flagged `extrapolated_backwards` — see `_resolve_rows`.

    Scoring more than one activity? Build a `History` once instead; this reads
    the table on every call.
    """
    return _resolve_rows(_load_history(conn, athlete_id, "ftp_history"), on_date)


def resolve_hr(conn: sqlite3.Connection, athlete_id: int, on_date: str | None) -> dict | None:
    """The HR figures in effect on `on_date`. See `History.hr`."""
    return History(conn, athlete_id).hr(on_date)


def _find_activity(
    conn: sqlite3.Connection,
    athlete_id: int,
    activity_id: int | None = None,
    garmin_activity_id: str | int | None = None,
) -> dict:
    """One activity by local id or Garmin id, or a refusal naming what was searched."""
    if activity_id is not None:
        row = conn.execute(
            "SELECT * FROM activities WHERE id = ? AND athlete_id = ?", (activity_id, athlete_id)
        ).fetchone()
        if row is None:
            raise CoachError(f"no stored activity with id {activity_id}")
        return _dict(row) or {}
    if garmin_activity_id is not None:
        key = str(garmin_activity_id).strip()
        row = conn.execute(
            "SELECT * FROM activities WHERE garmin_activity_id = ? AND athlete_id = ?",
            (key, athlete_id),
        ).fetchone()
        if row is None:
            raise CoachError(
                f"no stored activity with garmin_activity_id {key!r} — import it first with "
                "import_activities"
            )
        return _dict(row) or {}
    raise CoachError("pass either activity_id or garmin_activity_id")


def _activity_load(history: History, activity: dict) -> Load:
    """Score one activity against the figures in effect on its own date.

    Takes a `History` rather than a connection: every caller scores a list, and
    resolving from the database per ride issued the same handful of lookups
    over and over for the same forty rows.
    """
    on_date = activity.get("local_date")
    ftp = history.ftp(on_date)
    hr = history.hr(on_date)
    load = compute_activity_load(
        activity,
        ftp["value_watts"] if ftp else None,
        hr.get("threshold_hr") if hr else None,
        bool(hr and hr.get("threshold_hr_estimated")),
    )
    if ftp and ftp.get("extrapolated_backwards") and load.method.startswith("power"):
        load.flags.append("ftp_extrapolated_backwards")
    return load


def _aggregate_load(entries: list[dict]) -> dict:
    """Total the loads in a list of rows, without lying about what went into it.

    Two things a bare `sum(... or 0)` gets wrong, both of which this repo says
    out loud elsewhere and so must not drift here:

    * **A null TSS is not a zero.** It means the ride could not be scored — no
      power, no heart rate, no FTP for that date. Folding it to zero makes a
      week with three unscored rides read as a light week, which is the same
      mistake as treating a rest day and an unmeasured ride alike.
    * **Power TSS and hrTSS are different quantities.** Adding them gives a
      number on the TSS scale that is not a TSS. It is reported, because a
      partial total beats no total, but never without saying what it is made
      of.

    Returns the total plus `scored`, `unscored`, `by_method`, and a warning
    string when the total mixes methods.
    """
    by_method: dict[str, int] = {}
    scored: list[float] = []
    unscored = 0
    for entry in entries:
        load = entry.get("load") or entry
        method = load.get("method", "none")
        by_method[method] = by_method.get(method, 0) + 1
        if load.get("tss") is None:
            unscored += 1
        else:
            scored.append(float(load["tss"]))

    result: dict[str, Any] = {
        "total_tss": round(sum(scored), 1),
        "scored": len(scored),
        "unscored": unscored,
        "by_method": by_method,
    }
    if by_method.get("hr") and (by_method.get("power") or by_method.get("power_avg")):
        result["mixed_methods_warning"] = (
            "This total adds power TSS and hrTSS together. They are different quantities on "
            "the same scale — read it as an order of magnitude, and compare like with like "
            "when judging whether a week was harder than the last."
        )
    if unscored:
        result["unscored_warning"] = (
            f"{unscored} activit{'y' if unscored == 1 else 'ies'} could not be scored and "
            "contributed nothing to this total, which therefore understates the week. "
            "compute_load says why, per ride."
        )
    return result


def _flags_json(row: dict) -> str | None:
    """The stored form of an activity's data-quality flags."""
    flags = row_flags(row)
    return json.dumps(flags) if flags else None


def _spec_of(row: dict) -> dict:
    return json.loads(row["spec_json"])


def _planned_summary(row: dict) -> dict:
    """A stored planned workout, with the session read back out of its spec."""
    out = {
        "id": row["id"],
        "scheduled_date": row["scheduled_date"],
        "status": row["status"],
        "pushed_to": row["pushed_to"],
        "linked_activity_id": row["linked_activity_id"],
        "note": row["note"],
        "spec": _spec_of(row),
        "warnings": json.loads(row["warnings_json"]) if row.get("warnings_json") else [],
    }
    try:
        workout = load_spec(out["spec"])
    except SpecError as exc:
        out["spec_errors"] = exc.errors
        out["summary"] = "stored spec no longer validates"
        return out
    metrics = compute_metrics(workout)
    out["name"] = workout.name
    out["summary"] = (
        f"{workout.name} — {format_duration(metrics.total_seconds)} · "
        f"IF {metrics.intensity_factor:.2f} · TSS {metrics.tss:.0f}"
    )
    out["planned_seconds"] = metrics.total_seconds
    out["planned_tss"] = round(metrics.tss, 1)
    out["table"] = describe(workout)
    out["spec_digest"] = payload_digest(out["spec"])
    return out


# --------------------------------------------------------------------------
# profile and history
# --------------------------------------------------------------------------

_GAP_REASONS = {
    "display_name": "what to call the athlete when writing a plan",
    "height_cm": "context for weight; not used in any computation here",
    "birth_year": "age-based HR estimates, and how much recovery a block needs",
    "availability": "how many sessions a week fit, and which day the long ride goes on",
    "equipment": "whether indoor sessions are possible, and whether power is measured at all",
    "constraints": "injuries, travel, work patterns — what the plan must route around",
    "ftp": "every power target and every power-based training-load number",
    "weight": "W/kg, which is what a hilly event is actually decided on",
    "threshold_hr": "heart-rate targets, and load for any ride recorded without power",
    "max_hr": "a fallback threshold estimate when no threshold test exists",
    "resting_hr": "a baseline for reading fatigue from morning HR",
    "objective": "the event the plan is anchored to; without one there is nothing to build toward",
}


def get_profile(athlete_id: int = DEFAULT_ATHLETE_ID) -> dict:
    """The athlete record, the current dated figures, and what is still unknown."""
    with open_db() as conn:
        athlete = _ensure_athlete(conn, athlete_id)
        ftp = _latest(conn, "ftp_history", athlete_id, None)
        weight = _latest(conn, "weight_history", athlete_id, None)
        # Per-field, so a lone resting-HR entry does not read as "no threshold
        # on file" and reopen a gap that was closed months ago.
        hr = resolve_hr(conn, athlete_id, None)
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE athlete_id = ? AND status = 'upcoming'",
            (athlete_id,),
        ).fetchone()["n"]
        activities = conn.execute(
            "SELECT COUNT(*) AS n FROM activities WHERE athlete_id = ?", (athlete_id,)
        ).fetchone()["n"]

    gaps: list[dict] = []
    for field in ("display_name", "height_cm", "birth_year", "availability", "equipment"):
        if not athlete.get(field):
            gaps.append({"field": field, "matters_for": _GAP_REASONS[field]})
    if not ftp:
        gaps.append({"field": "ftp", "matters_for": _GAP_REASONS["ftp"]})
    if not weight:
        gaps.append({"field": "weight", "matters_for": _GAP_REASONS["weight"]})
    # `resolve_hr` substitutes 92% of max HR for a missing threshold, so testing
    # the resolved dict closed this gap the moment a max HR existed — the
    # athlete was never asked for a measured LTHR again, and every no-power
    # ride's load stayed pinned to the estimate.
    if not hr or not hr.get("threshold_hr") or hr.get("threshold_hr_estimated"):
        reason = _GAP_REASONS["threshold_hr"]
        if hr and hr.get("threshold_hr_estimated"):
            reason = (
                f"{reason} — currently estimated at {hr['threshold_hr']} bpm from max HR, "
                "which is wide enough to be worth replacing with a measured figure"
            )
        gaps.append({"field": "threshold_hr", "matters_for": reason})
    if not hr or not hr.get("max_hr"):
        gaps.append({"field": "max_hr", "matters_for": _GAP_REASONS["max_hr"]})
    if not events:
        gaps.append({"field": "objective", "matters_for": _GAP_REASONS["objective"]})

    watts_per_kg = None
    if ftp and weight and weight["value_kg"]:
        watts_per_kg = round(ftp["value_watts"] / weight["value_kg"], 2)

    return {
        "athlete": {
            key: athlete.get(key)
            for key in (
                "athlete_id",
                "display_name",
                "height_cm",
                "birth_year",
                "availability",
                "equipment",
                "constraints",
                "updated_at",
            )
        },
        "ftp": ftp,
        "weight": weight,
        "hr": hr,
        "watts_per_kg": watts_per_kg,
        "upcoming_events": events,
        "stored_activities": activities,
        "gaps": gaps,
        "gaps_note": (
            "Each gap is a field with nothing on file. Ask about them conversationally, a "
            "few at a time, and store the answers as they arrive — an empty profile is the "
            "start of an interview, not an error."
        ),
    }


def update_profile(
    display_name: str | None = None,
    height_cm: float | None = None,
    birth_year: int | None = None,
    availability: str | None = None,
    equipment: str | None = None,
    constraints: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Set any subset of the athlete fields. Omitted fields are left alone."""
    updates: dict[str, Any] = {}
    if display_name is not None:
        updates["display_name"] = _text(display_name)
    if height_cm is not None:
        _check_range(float(height_cm), (100.0, 250.0), "height", "cm")
        updates["height_cm"] = float(height_cm)
    if birth_year is not None:
        year = int(birth_year)
        if not (1900 <= year <= date.today().year):
            raise CoachError(f"birth_year of {year} is not a year an athlete was born in")
        updates["birth_year"] = year
    for key, value in (
        ("availability", availability),
        ("equipment", equipment),
        ("constraints", constraints),
    ):
        if value is not None:
            updates[key] = _text(value)

    if not updates:
        raise CoachError("nothing to update — pass at least one field")

    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE athlete SET {assignments}, updated_at = ? WHERE athlete_id = ?",
            (*updates.values(), now_utc(), athlete_id),
        )
        row = conn.execute("SELECT * FROM athlete WHERE athlete_id = ?", (athlete_id,)).fetchone()
    return {"updated_fields": sorted(updates), "athlete": _dict(row)}


def log_ftp(
    value_watts: float | None = None,
    twenty_min_watts: float | None = None,
    effective_date: str | None = None,
    method: str | None = None,
    note: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Append a dated FTP. Give either the FTP itself or a 20-minute test result."""
    if (value_watts is None) == (twenty_min_watts is None):
        raise CoachError(
            "give exactly one of value_watts (the FTP) or twenty_min_watts (the best "
            "20-minute average, which is multiplied by 0.95)"
        )

    derived_note = _text(note)
    if twenty_min_watts is not None:
        raw = float(twenty_min_watts)
        _check_range(raw, (50, 800), "20-minute power", "W")
        value = round(raw * TWENTY_MINUTE_FACTOR)
        method = method or "20min_test"
        detail = f"{raw:g} W for 20 min x {TWENTY_MINUTE_FACTOR} = {value} W"
        derived_note = f"{derived_note} ({detail})" if derived_note else detail
    else:
        value = round(float(value_watts))
        method = method or "stated"

    _check_range(value, FTP_LIMITS, "FTP", "W")
    method = _one_of(method, FTP_METHODS, "method")
    when = parse_date(effective_date, "effective_date") if effective_date else date.today()

    warnings: list[str] = []
    if not (FTP_USUAL[0] <= value <= FTP_USUAL[1]):
        warnings.append(f"{value} W is an unusual FTP — stored as given, but worth confirming")

    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        cursor = conn.execute(
            "INSERT INTO ftp_history (athlete_id, value_watts, effective_date, method, note, "
            "recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (athlete_id, value, when.isoformat(), method, derived_note, now_utc()),
        )
        row_id = cursor.lastrowid
        stored = _row_by_id(conn, "ftp_history", row_id)
        outcome = _dated_write_outcome(
            conn, "ftp_history", athlete_id, when.isoformat(), row_id or 0
        )

    result: dict[str, Any] = {
        "stored": stored,
        "zones": power_zones(value),
        "zones_apply_from": when.isoformat(),
        "is_current": outcome["is_current"],
        "warnings": warnings,
    }
    previous = outcome["previous"]
    if previous and previous["value_watts"] != value:
        delta = value - previous["value_watts"]
        result["change"] = (
            f"{previous['value_watts']} W ({previous['effective_date']}) -> {value} W "
            f"({when.isoformat()}), {delta:+d} W"
        )
    superseded = outcome["superseded_by"]
    if superseded:
        # Backdated. The zones above are the ones this entry establishes, which
        # are not today's — saying otherwise would have the athlete training to
        # a number that a later test already replaced.
        result["note"] = _backdated_note(
            when.isoformat(),
            f"{superseded['value_watts']} W",
            superseded["effective_date"],
            "today's zones are unchanged",
        )
    elif result.get("change"):
        result["note"] = (
            "Zones have moved. Any planned workout written in watts against the old FTP "
            "now targets a different percentage — re-render before pushing it, and "
            "recheck the targets in this week's plan."
        )
    return result


def _backdated_note(effective_date: str, later_value: str, later_date: str, unchanged: str) -> str:
    """The same sentence for every dated logger, with the units filled in."""
    return (
        f"This entry is backdated: {later_value} ({later_date}) still applies from that date "
        f"onward, so {unchanged}. What it does change is how the window from {effective_date} "
        f"to {later_date} is read — recompute with compute_load if that matters."
    )


def log_weight(
    value_kg: float,
    effective_date: str | None = None,
    note: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Append a dated weight in kilograms.

    The range check cannot catch a weight given in pounds: 160 lb is 73 kg, and
    160 is a plausible weight in kilograms for someone. So a heavy figure asks
    the question rather than refusing — but every W/kg computed from a pounds
    figure is wrong by a factor of 2.2, so the question is worth asking.
    """
    value = float(value_kg)
    _check_range(value, WEIGHT_LIMITS_KG, "weight", "kg")
    when = parse_date(effective_date, "effective_date") if effective_date else date.today()

    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        cursor = conn.execute(
            "INSERT INTO weight_history (athlete_id, value_kg, effective_date, note, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (athlete_id, value, when.isoformat(), _text(note), now_utc()),
        )
        row_id = cursor.lastrowid
        stored = _row_by_id(conn, "weight_history", row_id)
        outcome = _dated_write_outcome(
            conn, "weight_history", athlete_id, when.isoformat(), row_id or 0
        )
        ftp = _latest(conn, "ftp_history", athlete_id, when.isoformat())

    result: dict[str, Any] = {"stored": stored, "is_current": outcome["is_current"]}
    if ftp:
        # Against the FTP of the weigh-in's own date, so a backdated entry
        # yields the W/kg the athlete had then rather than a figure mixing two
        # points in time.
        result["watts_per_kg"] = round(ftp["value_watts"] / value, 2)
        result["watts_per_kg_as_of"] = when.isoformat()
    superseded = outcome["superseded_by"]
    if superseded:
        result["note"] = _backdated_note(
            when.isoformat(),
            f"{superseded['value_kg']:g} kg",
            superseded["effective_date"],
            "today's weight is unchanged",
        )
    if value > POUNDS_SUSPICION_KG:
        result["warning"] = (
            f"{value:g} kg is heavy for a cyclist — check it is not pounds. {value:g} lb would "
            f"be {value / 2.20462:.1f} kg, and every W/kg from the wrong one is out by 2.2."
        )
    return result


def log_hr(
    threshold_hr: int | None = None,
    max_hr: int | None = None,
    resting_hr: int | None = None,
    effective_date: str | None = None,
    method: str | None = None,
    note: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Append a dated heart-rate entry. Any of the three figures may be omitted."""
    values: dict[str, int | None] = {}
    for key, raw in (
        ("threshold_hr", threshold_hr),
        ("max_hr", max_hr),
        ("resting_hr", resting_hr),
    ):
        if raw is None:
            values[key] = None
            continue
        number = round(float(raw))
        _check_range(number, HR_LIMITS, key, "bpm")
        values[key] = number

    if not any(values.values()):
        raise CoachError("give at least one of threshold_hr, max_hr, resting_hr")
    if values["threshold_hr"] and values["max_hr"] and values["threshold_hr"] > values["max_hr"]:
        raise CoachError(
            f"threshold HR ({values['threshold_hr']}) is above max HR ({values['max_hr']}) — "
            "one of the two is wrong"
        )

    method = _one_of(method, HR_METHODS, "method")
    when = parse_date(effective_date, "effective_date") if effective_date else date.today()

    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        cursor = conn.execute(
            "INSERT INTO hr_history (athlete_id, threshold_hr, max_hr, resting_hr, "
            "effective_date, method, note, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                athlete_id,
                values["threshold_hr"],
                values["max_hr"],
                values["resting_hr"],
                when.isoformat(),
                method,
                _text(note),
                now_utc(),
            ),
        )
        row_id = cursor.lastrowid
        stored = _row_by_id(conn, "hr_history", row_id)
        # Per field: a backdated resting HR supersedes nothing about the
        # threshold, so asking the question row-at-a-time would answer it about
        # figures this entry never touched.
        outcomes = {
            column: _dated_write_outcome(
                conn, "hr_history", athlete_id, when.isoformat(), row_id or 0, column
            )
            for column, given in values.items()
            if given is not None
        }
        in_effect = History(conn, athlete_id).hr(None)

    superseded_fields = {
        column: outcome["superseded_by"]
        for column, outcome in outcomes.items()
        if outcome["superseded_by"]
    }
    result: dict[str, Any] = {
        "stored": stored,
        "in_effect_today": in_effect,
        "is_current": not superseded_fields,
    }
    if superseded_fields:
        result["note"] = (
            "This entry is backdated for "
            + ", ".join(
                f"{column} (a later entry dated {row['effective_date']} still applies)"
                for column, row in sorted(superseded_fields.items())
            )
            + ". The zones below are the ones this entry establishes on "
            f"{when.isoformat()}, not today's."
        )
    if values["threshold_hr"]:
        result["hr_zones"] = hr_zones(values["threshold_hr"])
    elif values["max_hr"]:
        estimated = round(values["max_hr"] * LTHR_FROM_MAX_HR)
        result["hr_zones"] = hr_zones(estimated)
        result["threshold_hr_estimated"] = estimated
        result["note"] = (
            f"No threshold HR on file, so these zones use {LTHR_FROM_MAX_HR:.0%} of max HR "
            f"({estimated} bpm) as an estimate. A measured threshold routinely lands several "
            "bpm either side of that, and every boundary moves with it."
        )
    return result


def get_zones(as_of: str | None = None, athlete_id: int = DEFAULT_ATHLETE_ID) -> dict:
    """Power and heart-rate zones from the figures in effect on a date.

    `as_of` defaults to today. Pass the date of a ride to see the zones that
    ride was actually performed against, which is not the same table as today's
    once an FTP has moved.
    """
    when = as_of if as_of is None else parse_date(as_of, "as_of").isoformat()
    with open_db() as conn:
        ftp = resolve_ftp(conn, athlete_id, when)
        hr = resolve_hr(conn, athlete_id, when)

    result: dict[str, Any] = {"as_of": when or date.today().isoformat()}
    if ftp:
        result["ftp"] = {
            "value_watts": ftp["value_watts"],
            "effective_date": ftp["effective_date"],
            "method": ftp["method"],
            "extrapolated_backwards": ftp["extrapolated_backwards"],
        }
        result["power_zones"] = power_zones(ftp["value_watts"])
    else:
        result["ftp"] = None
        result["power_zones"] = []
        result["problem"] = "no FTP on file — log one with log_ftp before writing power targets"

    if hr and hr.get("threshold_hr"):
        result["threshold_hr"] = hr["threshold_hr"]
        result["threshold_hr_estimated"] = hr["threshold_hr_estimated"]
        result["hr_zones"] = hr_zones(hr["threshold_hr"])
        if hr["threshold_hr_estimated"]:
            result["hr_zones_note"] = (
                f"Threshold HR estimated at {LTHR_FROM_MAX_HR:.0%} of max HR. Treat these "
                "boundaries as approximate."
            )
    else:
        result["threshold_hr"] = None
        result["hr_zones"] = []
    return result


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------


def _event_out(row: dict) -> dict:
    out = dict(row)
    if out.get("finish_time_s"):
        out["finish_time"] = format_duration(int(out["finish_time_s"]))
    return out


def add_event(
    name: str,
    event_date: str,
    distance_km: float | None = None,
    elevation_m: float | None = None,
    priority: str | None = None,
    status: str | None = None,
    note: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Record a race or objective, past or planned.

    An event is not a planned workout. A workout fills a week; an event is what
    the weeks are pointing at, and the next A-event is what a periodised plan
    is anchored to. Recording past races matters as much as future ones —
    that is where `record_race_result`'s debriefs accumulate, and a debrief
    from the same event last year is the most specific information available
    when planning for it again.
    """
    label = _text(name)
    if not label:
        raise CoachError("an event needs a name")
    when = parse_date(event_date, "event_date")
    priority = _one_of(priority.upper() if priority else None, EVENT_PRIORITIES, "priority")
    status = _one_of(status, EVENT_STATUSES, "status") or (
        "upcoming" if when >= date.today() else "completed"
    )

    stamp = now_utc()
    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        cursor = conn.execute(
            "INSERT INTO events (athlete_id, name, event_date, distance_km, elevation_m, "
            "priority, status, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                athlete_id,
                label,
                when.isoformat(),
                None if distance_km is None else float(distance_km),
                None if elevation_m is None else float(elevation_m),
                priority,
                status,
                _text(note),
                stamp,
                stamp,
            ),
        )
        row = conn.execute("SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {"stored": _event_out(_dict(row) or {})}


def update_event(
    event_id: int,
    name: str | None = None,
    event_date: str | None = None,
    distance_km: float | None = None,
    elevation_m: float | None = None,
    priority: str | None = None,
    status: str | None = None,
    note: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Change any subset of an event's fields. Results are set by record_race_result."""
    updates: dict[str, Any] = {}
    if name is not None:
        updates["name"] = _text(name)
    if event_date is not None:
        updates["event_date"] = parse_date(event_date, "event_date").isoformat()
    if distance_km is not None:
        updates["distance_km"] = float(distance_km)
    if elevation_m is not None:
        updates["elevation_m"] = float(elevation_m)
    if priority is not None:
        updates["priority"] = _one_of(priority.upper(), EVENT_PRIORITIES, "priority")
    if status is not None:
        updates["status"] = _one_of(status, EVENT_STATUSES, "status")
    if note is not None:
        updates["note"] = _text(note)
    if not updates:
        raise CoachError("nothing to update — pass at least one field")

    with open_db() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE id = ? AND athlete_id = ?", (event_id, athlete_id)
        ).fetchone()
        if row is None:
            raise CoachError(f"no event with id {event_id}")
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE events SET {assignments}, updated_at = ? WHERE id = ?",
            (*updates.values(), now_utc(), event_id),
        )
        stored = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return {"updated_fields": sorted(updates), "stored": _event_out(_dict(stored) or {})}


def list_events(
    when: str = "all",
    status: str | None = None,
    today: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Every event, newest objective first, with the next A-event called out.

    `when` is "all", "upcoming" or "past", judged on the event date against
    `today` (which defaults to the server's date — pass it when the athlete's
    date differs). `next_a_event` is the earliest upcoming priority-A event and
    is the anchor a periodised plan is built backwards from; it is null when
    there is none, which is itself the answer to "what are we training for".
    """
    reference = _today(today)
    if when not in ("all", "upcoming", "past"):
        raise CoachError("when must be 'all', 'upcoming' or 'past'")
    status = _one_of(status, EVENT_STATUSES, "status")

    with open_db() as conn:
        rows = [
            _dict(row)
            for row in conn.execute(
                "SELECT * FROM events WHERE athlete_id = ? ORDER BY event_date ASC", (athlete_id,)
            )
        ]

    events = []
    for row in rows:
        assert row is not None
        if status and row["status"] != status:
            continue
        is_future = parse_date(row["event_date"], "event_date") >= reference
        if when == "upcoming" and not is_future:
            continue
        if when == "past" and is_future:
            continue
        entry = _event_out(row)
        entry["days_away"] = (parse_date(row["event_date"], "event_date") - reference).days
        events.append(entry)

    next_a = next(
        (
            event
            for event in sorted(events, key=lambda e: e["event_date"])
            if event["priority"] == "A"
            and event["status"] == "upcoming"
            and event["days_away"] >= 0
        ),
        None,
    )
    return {
        "today": reference.isoformat(),
        "count": len(events),
        "events": events,
        "next_a_event": next_a,
        "weeks_to_next_a_event": (None if next_a is None else round(next_a["days_away"] / 7.0, 1)),
    }


def record_race_result(
    event_id: int,
    activity_id: int | None = None,
    garmin_activity_id: str | int | None = None,
    finish_time: str | float | None = None,
    debrief: str | None = None,
    status: str | None = None,
    force: bool = False,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Close out an event: link the race-day ride, record the time, store the debrief.

    Refuses to link an activity whose `local_date` is not the event date unless
    `force` is set. That check has caught the realistic mistake — linking the
    Sunday recovery spin after a Saturday race, which then makes the A-event
    look like an easy hour.

    Linking matters beyond tidiness: `get_week` stops reporting a linked
    race-day ride as unplanned training. It still counts in full toward load
    and CTL, because the athlete's body does not know it was a race, but it is
    no longer a deviation from a plan that never prescribed it.

    `finish_time` accepts "4:32:10" or a number of seconds. The debrief is free
    text and is the most valuable thing stored here: pacing, nutrition, where
    it went wrong, what to do differently. `list_events` hands it back the next
    time this event is planned for.
    """
    # Omitted means "leave it alone", not "completed". Adding a debrief to a
    # race the athlete abandoned used to quietly rewrite it as finished — and
    # nothing in the response said so.
    status = _one_of(status, EVENT_STATUSES, "status")
    seconds: int | None = None
    if finish_time is not None:
        try:
            seconds = parse_duration(finish_time)
        except ValueError as exc:
            raise CoachError(f"finish_time: {exc}") from None

    with open_db() as conn:
        event = _dict(
            conn.execute(
                "SELECT * FROM events WHERE id = ? AND athlete_id = ?", (event_id, athlete_id)
            ).fetchone()
        )
        if event is None:
            raise CoachError(f"no event with id {event_id}")

        activity = None
        if activity_id is not None or garmin_activity_id is not None:
            activity = _find_activity(conn, athlete_id, activity_id, garmin_activity_id)
            if activity["local_date"] != event["event_date"] and not force:
                raise CoachError(
                    f"activity {activity['id']} is dated {activity['local_date']} but "
                    f"'{event['name']}' is on {event['event_date']}. Linking the wrong ride "
                    "makes the race look like whatever that day's session was. Pass "
                    "force=true if the date genuinely differs (a race that ran past "
                    "midnight, a mis-set device clock)."
                )

        updates: dict[str, Any] = {}
        if status is not None:
            updates["status"] = status
        elif event["status"] == "upcoming":
            # No result recorded yet, so filing one completes it. An event
            # already marked abandoned or dns keeps that: it is the outcome.
            updates["status"] = "completed"
        if activity is not None:
            updates["linked_activity_id"] = activity["id"]
        if seconds is not None:
            updates["finish_time_s"] = seconds
        if debrief is not None:
            updates["debrief"] = _text(debrief)

        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE events SET {assignments}, updated_at = ? WHERE id = ?",
            (*updates.values(), now_utc(), event_id),
        )
        stored = _dict(conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())
        load = _activity_load(History(conn, athlete_id), activity).as_dict() if activity else None

    result: dict[str, Any] = {"stored": _event_out(stored or {}), "updated_fields": sorted(updates)}
    if "status" not in updates:
        result["status_unchanged"] = (
            f"'{event['name']}' stays {event['status']}. Pass status explicitly to change it."
        )
    if activity is not None:
        result["linked_activity"] = _project(activity, _ACTIVITY_OUT_FIELDS)
        result["linked_activity_load"] = load
    if not (stored or {}).get("debrief"):
        result["missing"] = (
            "No debrief stored. Write one now while the ride is fresh — pacing, nutrition, "
            "where it cracked, what to change. It is the only part of this record that is "
            "still useful a year from now."
        )
    return result


# --------------------------------------------------------------------------
# activities
# --------------------------------------------------------------------------


def _same(left: Any, right: Any) -> bool:
    """Equal for change detection, with 3600 and 3600.0 counting as the same number."""
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) < 1e-9
    return left == right


def import_activities(payload: Any, athlete_id: int = DEFAULT_ATHLETE_ID) -> dict:
    """Store raw Garmin MCP activity output, keyed on `activityId`.

    Pass the Garmin result **unchanged** — a list from `get_activities`, one
    object from `get_activity`, or a wrapper dict around either. The field
    mapping happens here on purpose: transcribing numbers into a clean schema
    on the way in is the one step that can silently corrupt an average power,
    and a corrupt average power is a training load that is wrong and looks
    entirely reasonable.

    Idempotent on `activityId`. Re-importing the same payload reports
    `unchanged`, so syncing an overlapping window every week is free and safe.

    **A null never overwrites a stored value.** `get_activities` returns a
    thinner summary than `get_activity`, so re-importing the list after
    fetching one ride in detail would otherwise blank its normalised power —
    and the ride would keep its load number while losing the field that
    produced it.

    Returns counts plus, for every rejected item, the reason. An activity with
    no `activityId` or no readable start time is rejected; an unknown extra key
    never is.
    """
    # Raised, not returned as {"ok": False}. The tool layer renders every
    # refusal in one place; a function returning its own ok flag worked only
    # because the caller happened to spread this dict last, and the next person
    # to reorder that line would have shipped a failure reported as a success.
    items = as_activity_list(payload)

    inserted: list[dict] = []
    updated: list[dict] = []
    unchanged: list[dict] = []
    rejected: list[dict] = []
    stamp = now_utc()

    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        for index, item in enumerate(items):
            row, reason = normalize_activity(item)
            if row is None:
                rejected.append({"index": index, "reason": reason})
                continue
            row.pop("_flags", None)

            existing = conn.execute(
                "SELECT * FROM activities WHERE athlete_id = ? AND garmin_activity_id = ?",
                (athlete_id, row["garmin_activity_id"]),
            ).fetchone()
            raw = json.dumps(item, sort_keys=True, ensure_ascii=False)

            if existing is None:
                row["flags_json"] = _flags_json(row)
                columns = [
                    "athlete_id",
                    "garmin_activity_id",
                    *_IMPORT_FIELDS,
                    "raw_json",
                    "imported_at",
                ]
                values = [athlete_id, row["garmin_activity_id"]]
                values += [row.get(field) for field in _IMPORT_FIELDS]
                values += [raw, stamp]
                placeholders = ", ".join("?" for _ in columns)
                cursor = conn.execute(
                    f"INSERT INTO activities ({', '.join(columns)}) VALUES ({placeholders})",
                    values,
                )
                stored = conn.execute(
                    "SELECT * FROM activities WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
                inserted.append(_project(stored, _ACTIVITY_OUT_FIELDS))
                continue

            current = _dict(existing) or {}
            # The row as it will stand after the merge, so the flags describe
            # what is stored rather than what this particular payload carried.
            merged = {
                **current,
                **{
                    field: value
                    for field, value in row.items()
                    if value is not None and field in _IMPORT_FIELDS
                },
            }
            merged["flags_json"] = _flags_json(merged)
            changes = {
                field: merged.get(field)
                for field in _IMPORT_FIELDS
                if not _same(merged.get(field), current.get(field))
            }
            if not changes:
                unchanged.append(_project(current, _ACTIVITY_OUT_FIELDS))
                continue
            assignments = ", ".join(f"{key} = ?" for key in changes)
            conn.execute(
                f"UPDATE activities SET {assignments}, raw_json = ?, imported_at = ? WHERE id = ?",
                (*changes.values(), raw, stamp, current["id"]),
            )
            stored = conn.execute(
                "SELECT * FROM activities WHERE id = ?", (current["id"],)
            ).fetchone()
            entry = _project(stored, _ACTIVITY_OUT_FIELDS)
            entry["changed_fields"] = sorted(changes)
            updated.append(entry)

    result: dict[str, Any] = {
        "seen": len(items),
        "inserted": len(inserted),
        "updated": len(updated),
        "unchanged": len(unchanged),
        "rejected": len(rejected),
        "activities": {
            "inserted": inserted,
            "updated": updated,
            "unchanged": [entry["garmin_activity_id"] for entry in unchanged],
        },
        "rejections": rejected,
    }
    flagged = [
        {"garmin_activity_id": entry["garmin_activity_id"], "flags": entry["flags"]}
        for entry in [*inserted, *updated, *unchanged]
        if entry.get("flags")
    ]
    if flagged:
        result["flags"] = flagged
        result["flags_note"] = (
            "local_date_from_utc: no local start time, so the plan date came from UTC and may "
            "be a day off for an early-morning or late-evening ride. no_power / "
            "no_normalized_power: training load falls back to average power or to heart rate; "
            "compute_load says which was used per ride."
        )
    return result


def import_activity_laps(
    payload: Any,
    activity_id: int | None = None,
    garmin_activity_id: str | int | None = None,
    replace: bool = True,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Store the splits of one activity, in execution order.

    Pass the Garmin MCP's `get_activity_splits` result unchanged, plus which
    activity it belongs to. Laps are what make `compliance_report` able to say
    "the second block fell to 228 W" instead of "the ride averaged 210 W" — a
    summary cannot distinguish an interval session ridden correctly from the
    same session ridden as one long tempo.

    `get_activity_split_summaries` is refused: it aggregates by split *type*,
    not by lap, so its rows do not line up with the blocks of a plan.

    `replace=True` (the default) clears the stored laps first, so re-importing
    is idempotent rather than doubling them.
    """
    laps = as_lap_list(payload)

    with open_db() as conn:
        activity = _find_activity(conn, athlete_id, activity_id, garmin_activity_id)
        if replace:
            conn.execute("DELETE FROM activity_laps WHERE activity_id = ?", (activity["id"],))
        for index, item in enumerate(laps, start=1):
            row = normalize_lap(item, index)
            columns = ["activity_id", *_LAP_OUT_FIELDS, "raw_json"]
            values = [activity["id"]]
            values += [row.get(field) for field in _LAP_OUT_FIELDS]
            values.append(json.dumps(item, sort_keys=True, ensure_ascii=False))
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO activity_laps ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
        stored = [
            _project(row, _LAP_OUT_FIELDS)
            for row in conn.execute(
                "SELECT * FROM activity_laps WHERE activity_id = ? ORDER BY lap_index",
                (activity["id"],),
            )
        ]

    total = sum(lap["duration_s"] or 0 for lap in stored)
    result: dict[str, Any] = {
        "activity_id": activity["id"],
        "garmin_activity_id": activity["garmin_activity_id"],
        "stored_laps": len(stored),
        "laps": stored,
        "lap_total_seconds": round(total),
    }
    if activity.get("duration_s") and abs(total - activity["duration_s"]) > 60:
        result["warning"] = (
            f"the laps sum to {round(total)} s but the activity is "
            f"{round(activity['duration_s'])} s. Either these splits belong to a different "
            "ride, or the file has gaps the splits do not cover."
        )
    return result


def annotate_activity(
    activity_id: int | None = None,
    garmin_activity_id: str | int | None = None,
    rpe: int | None = None,
    feel: str | None = None,
    note: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Attach the subjective layer to a ride: RPE 1-10, how it felt, free text.

    This is the half of a session no device records, and it is what makes a
    plan adaptable: two rides with identical power files, one of which felt
    catastrophic, call for different weeks. Store it whenever the athlete says
    anything about how a session went.
    """
    updates: dict[str, Any] = {}
    if rpe is not None:
        value = int(rpe)
        if not (1 <= value <= 10):
            raise CoachError(f"rpe must be 1-10, got {rpe!r}")
        updates["rpe"] = value
    if feel is not None:
        updates["feel"] = _text(feel)
    if note is not None:
        updates["note"] = _text(note)
    if not updates:
        raise CoachError("nothing to store — pass rpe, feel or note")

    with open_db() as conn:
        activity = _find_activity(conn, athlete_id, activity_id, garmin_activity_id)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE activities SET {assignments}, annotated_at = ? WHERE id = ?",
            (*updates.values(), now_utc(), activity["id"]),
        )
        stored = conn.execute("SELECT * FROM activities WHERE id = ?", (activity["id"],)).fetchone()
    return {
        "updated_fields": sorted(updates),
        "stored": _project(stored, _ACTIVITY_OUT_FIELDS),
    }


def list_activities(
    start: str | None = None,
    end: str | None = None,
    sport: str | None = None,
    include_load: bool = True,
    limit: int = 200,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Stored activities in a date range, newest first, with computed load.

    Dates are `local_date` — the day the athlete rode, not the UTC day. `sport`
    filters on the family ("cycling", "running", "swimming", "other"), which
    deliberately catches every indoor and virtual sub-type; the device's own
    key is in `sub_sport`.

    A ride imported from a payload carrying no activity type has an **unknown**
    sport, not "other", and a sport filter excludes it — it is not known to be
    cycling. That exclusion is reported rather than silent: the response
    carries `excluded_unknown_sport` when the filter hid any, because a ride
    dropping out of a list without a word is how a real session becomes a
    missed-session narrative.
    """
    sport = _one_of(sport, SPORTS, "sport")
    clauses = ["athlete_id = ?"]
    params: list[Any] = [athlete_id]
    if start:
        clauses.append("local_date >= ?")
        params.append(parse_date(start, "start").isoformat())
    if end:
        clauses.append("local_date <= ?")
        params.append(parse_date(end, "end").isoformat())

    # The date filter alone, kept separately: it is what counts the rides a
    # sport filter would hide rather than report.
    date_clauses, date_params = list(clauses), list(params)
    if sport:
        clauses.append("sport = ?")
        params.append(sport)

    with open_db() as conn:
        # One more than asked for, so "there is more" is a fact rather than the
        # guess `len(rows) == limit` makes when exactly `limit` rows match.
        rows = conn.execute(
            f"SELECT {_ACTIVITY_SELECT} FROM activities WHERE {' AND '.join(clauses)} "
            f"ORDER BY local_date DESC, start_time_utc DESC, id DESC LIMIT ?",
            (*params, int(limit) + 1),
        ).fetchall()
        truncated = len(rows) > int(limit)
        rows = rows[: int(limit)]
        unknown_sport = 0
        if sport:
            unknown_sport = conn.execute(
                f"SELECT COUNT(*) AS n FROM activities "
                f"WHERE {' AND '.join(date_clauses)} AND sport IS NULL",
                date_params,
            ).fetchone()["n"]
        history = History(conn, athlete_id) if include_load else None
        activities = []
        for row in rows:
            entry = _project(row, _ACTIVITY_OUT_FIELDS)
            if history is not None:
                entry["load"] = _activity_load(history, _dict(row) or {}).as_dict()
            activities.append(entry)

    result: dict[str, Any] = {
        "count": len(activities),
        "start": start,
        "end": end,
        "sport": sport,
        "activities": activities,
        "truncated": truncated,
    }
    if include_load:
        result.update(_aggregate_load(activities))
    else:
        result["total_tss"] = None
    if sport and unknown_sport:
        result["excluded_unknown_sport"] = unknown_sport
        result["excluded_unknown_sport_note"] = (
            f"{unknown_sport} activit{'y' if unknown_sport == 1 else 'ies'} in this range "
            f"{'carries' if unknown_sport == 1 else 'carry'} no sport from Garmin and "
            f"{'is' if unknown_sport == 1 else 'are'} not in this list. That is training "
            "that happened; drop the sport filter to see it."
        )
    return result


def link_activity(
    planned_workout_id: int,
    activity_id: int | None = None,
    garmin_activity_id: str | int | None = None,
    auto: bool = False,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Attach the ride that was done to the session that was planned.

    Pass an activity explicitly, or set `auto` to have the server propose one:
    same `local_date`, same sport family, ranked by how close the duration is
    to the plan. **`auto` only links when there is exactly one candidate.**
    Anything else returns the candidates and links nothing — an automatic match
    that picks the wrong ride out of two on the same day produces a compliance
    report that is confidently about the wrong session, and nothing downstream
    would reveal it.

    Linking sets the planned workout's status to `completed`.
    """
    with open_db() as conn:
        planned = _dict(
            conn.execute(
                "SELECT * FROM planned_workouts WHERE id = ? AND athlete_id = ?",
                (planned_workout_id, athlete_id),
            ).fetchone()
        )
        if planned is None:
            raise CoachError(f"no planned workout with id {planned_workout_id}")

        chosen: dict | None = None
        candidates: list[dict] = []
        if activity_id is not None or garmin_activity_id is not None:
            chosen = _find_activity(conn, athlete_id, activity_id, garmin_activity_id)
        elif auto:
            try:
                # total_seconds sums the blocks; compute_metrics would expand a
                # 1 Hz power series for the whole session to reach the same number.
                planned_seconds = load_spec(_spec_of(planned)).total_seconds
            except SpecError:
                planned_seconds = None
            # NULL sport means the payload carried no activity type, not that
            # the ride was something other than a bike. Excluding it reported
            # "no cycling activity stored on that date" for a ride sitting in
            # the log on exactly that date — a missed-session narrative for a
            # session that happened.
            rows = conn.execute(
                f"SELECT {_ACTIVITY_SELECT} FROM activities WHERE athlete_id = ? "
                f"AND local_date = ? AND (sport = ? OR sport IS NULL) ORDER BY start_time_utc",
                (athlete_id, planned["scheduled_date"], "cycling"),
            ).fetchall()
            for row in rows:
                entry = _project(row, _ACTIVITY_OUT_FIELDS)
                if planned_seconds and entry["duration_s"]:
                    entry["duration_delta_s"] = round(entry["duration_s"] - planned_seconds)
                if entry["sport"] is None:
                    entry["sport_note"] = (
                        "Garmin sent no activity type for this ride, so its sport is unknown. "
                        "It is offered as a candidate rather than hidden."
                    )
                candidates.append(entry)
            candidates.sort(key=lambda e: abs(e.get("duration_delta_s") or 0))
            if len(candidates) == 1:
                chosen = _find_activity(conn, athlete_id, candidates[0]["id"], None)
        else:
            raise CoachError("pass activity_id/garmin_activity_id, or auto=true")

        if chosen is None:
            return {
                "linked": False,
                "planned_workout_id": planned_workout_id,
                "scheduled_date": planned["scheduled_date"],
                "candidates": candidates,
                "reason": (
                    "no cycling activity stored on that date"
                    if not candidates
                    else f"{len(candidates)} cycling activities on that date — ambiguous, so "
                    "nothing was linked. Pick one and call link_activity with its id."
                ),
            }

        if chosen["local_date"] != planned["scheduled_date"]:
            date_note = (
                f"the activity is dated {chosen['local_date']} and the session was planned "
                f"for {planned['scheduled_date']}"
            )
        else:
            date_note = None

        conn.execute(
            "UPDATE planned_workouts SET linked_activity_id = ?, status = 'completed', "
            "updated_at = ? WHERE id = ?",
            (chosen["id"], now_utc(), planned_workout_id),
        )
        stored = _dict(
            conn.execute(
                "SELECT * FROM planned_workouts WHERE id = ?", (planned_workout_id,)
            ).fetchone()
        )
        load = _activity_load(History(conn, athlete_id), chosen).as_dict()

    result: dict[str, Any] = {
        "linked": True,
        "planned_workout": _planned_summary(stored or {}),
        "activity": _project(chosen, _ACTIVITY_OUT_FIELDS),
        "activity_load": load,
        "next_step": (
            "Call compliance_report to compare what was prescribed against what was ridden."
        ),
    }
    if date_note:
        result["warning"] = date_note
    return result


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


def save_planned_workouts(workouts: list[dict], athlete_id: int = DEFAULT_ATHLETE_ID) -> dict:
    """Store planned sessions. Each item is {spec, scheduled_date, note?}.

    The spec is the same document `render_garmin` and `render_zwo` consume, and
    it is stored verbatim — a stored plan is directly renderable months later
    with no translation step. Every spec is validated first and an invalid one
    is refused rather than stored: a plan that cannot be rendered is not a
    plan, and the failure would otherwise surface on the morning it was meant
    to be ridden. Warnings are stored alongside rather than blocking.

    Valid items are stored even when others in the same call are refused, so
    one bad session does not lose a week's work. The response says which.

    Note the FTP question. A spec carries its own `ftp`, and it is the FTP at
    the time of writing; if the athlete tests before riding it, the watts in
    the stored spec are stale. `get_week` flags that comparison.
    """
    if not isinstance(workouts, list) or not workouts:
        raise CoachError("workouts must be a non-empty list of {spec, scheduled_date}")

    saved: list[dict] = []
    refused: list[dict] = []
    stamp = now_utc()

    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        for index, item in enumerate(workouts):
            if not isinstance(item, dict):
                refused.append({"index": index, "errors": ["item must be an object"]})
                continue
            spec = item.get("spec")
            try:
                when = parse_date(item.get("scheduled_date"), "scheduled_date").isoformat()
            except ValueError as exc:
                refused.append({"index": index, "errors": [str(exc)]})
                continue

            workout, errors, warnings = _validate_spec(spec)
            if workout is None:
                refused.append(
                    {
                        "index": index,
                        "scheduled_date": when,
                        "errors": errors,
                        "reason": "spec is invalid, so it was not stored",
                    }
                )
                continue

            clash = conn.execute(
                "SELECT id FROM planned_workouts WHERE athlete_id = ? AND scheduled_date = ? "
                "AND status IN ('planned', 'pushed')",
                (athlete_id, when),
            ).fetchall()
            cursor = conn.execute(
                "INSERT INTO planned_workouts (athlete_id, spec_json, scheduled_date, status, "
                "note, warnings_json, created_at, updated_at) VALUES (?, ?, ?, 'planned', ?, ?, "
                "?, ?)",
                (
                    athlete_id,
                    json.dumps(spec, sort_keys=True, ensure_ascii=False),
                    when,
                    _text(item.get("note")),
                    json.dumps(warnings),
                    stamp,
                    stamp,
                ),
            )
            stored = _dict(
                conn.execute(
                    "SELECT * FROM planned_workouts WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
            )
            entry = _planned_summary(stored or {})
            if clash:
                entry["warning"] = (
                    f"another session is already planned for {when} "
                    f"(id {', '.join(str(row['id']) for row in clash)}). Two sessions on one "
                    "day is a real plan sometimes and a duplicate more often — use "
                    "update_planned_workout to reschedule or skip one if this was not intended."
                )
            saved.append(entry)

    return {
        "saved": len(saved),
        "refused": len(refused),
        "planned_workouts": saved,
        "refusals": refused,
        "next_step": (
            "These specs are renderable as they stand: pass spec_json to render_garmin or "
            "render_zwo. After a successful upload, call "
            "update_planned_workout(status='pushed', pushed_to=...)."
        ),
    }


def update_planned_workout(
    planned_workout_id: int,
    status: str | None = None,
    scheduled_date: str | None = None,
    pushed_to: str | None = None,
    note: str | None = None,
    spec: dict | None = None,
    linked_activity_id: int | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Change a planned session: status, date, push target, note, or the spec itself.

    Statuses: `planned` (written, not sent anywhere), `pushed` (on a platform —
    set this after a verified upload, with `pushed_to`), `completed` (ridden,
    normally set by `link_activity`), `missed` (not done), `skipped`
    (deliberately dropped). The distinction between missed and skipped is
    coaching information, not bookkeeping: one is a plan the athlete could not
    follow, the other a plan the coach withdrew.

    Replacing `spec` re-validates it and refuses an invalid one, exactly as
    `save_planned_workouts` does.
    """
    updates: dict[str, Any] = {}
    if status is not None:
        updates["status"] = _one_of(status, PLANNED_STATUSES, "status")
    if scheduled_date is not None:
        updates["scheduled_date"] = parse_date(scheduled_date, "scheduled_date").isoformat()
    if pushed_to is not None:
        updates["pushed_to"] = _one_of(pushed_to, PUSH_TARGETS, "pushed_to")
    if note is not None:
        updates["note"] = _text(note)
    if linked_activity_id is not None:
        updates["linked_activity_id"] = int(linked_activity_id)

    warnings: list[str] = []
    if spec is not None:
        workout, errors, spec_warnings = _validate_spec(spec)
        if workout is None:
            return {
                "updated": False,
                "errors": errors,
                "reason": "the replacement spec is invalid, so nothing was changed",
            }
        updates["spec_json"] = json.dumps(spec, sort_keys=True, ensure_ascii=False)
        updates["warnings_json"] = json.dumps(spec_warnings)
        warnings = spec_warnings

    if not updates:
        raise CoachError("nothing to update — pass at least one field")

    with open_db() as conn:
        row = conn.execute(
            "SELECT * FROM planned_workouts WHERE id = ? AND athlete_id = ?",
            (planned_workout_id, athlete_id),
        ).fetchone()
        if row is None:
            raise CoachError(f"no planned workout with id {planned_workout_id}")
        if "linked_activity_id" in updates:
            _find_activity(conn, athlete_id, updates["linked_activity_id"], None)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE planned_workouts SET {assignments}, updated_at = ? WHERE id = ?",
            (*updates.values(), now_utc(), planned_workout_id),
        )
        stored = _dict(
            conn.execute(
                "SELECT * FROM planned_workouts WHERE id = ?", (planned_workout_id,)
            ).fetchone()
        )

    result: dict[str, Any] = {
        "updated": True,
        "updated_fields": sorted(updates),
        "planned_workout": _planned_summary(stored or {}),
    }
    if warnings:
        result["spec_warnings"] = warnings
    if updates.get("status") == "pushed" and not (stored or {}).get("pushed_to"):
        result["note"] = (
            "Status is 'pushed' but pushed_to is unset. Record which platform it went to — "
            "otherwise nothing distinguishes a session on the head unit from one in MyWhoosh."
        )
    return result


def get_week(
    start: str,
    end: str,
    today: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """The plan and the reality for a date range, and where they diverge.

    Returns the planned sessions (each with the block table `describe_spec`
    produces), every stored activity in the range with its computed load, and
    two kinds of deviation:

    * **planned, not ridden** — a session whose date has passed with no
      activity linked and no status explaining it. This is the list to read
      before writing the next week.
    * **ridden, not planned** — an activity linked to no planned session. A
      race-day ride linked to an event is *not* reported here: it was training
      the plan knew about. It still counts in full toward load and CTL.

    `today` defaults to the server's date and decides which planned sessions
    are late rather than merely upcoming; pass the athlete's date explicitly
    when they might differ.

    A planned session whose spec was written against an FTP that has since
    changed is flagged: the watts in it are stale, and rendering it unedited
    prescribes the old intensity.
    """
    first = parse_date(start, "start")
    last = parse_date(end, "end")
    if last < first:
        raise CoachError(f"end ({end}) is before start ({start})")
    reference = _today(today)

    with open_db() as conn:
        planned_rows = [
            _dict(row)
            for row in conn.execute(
                "SELECT * FROM planned_workouts WHERE athlete_id = ? AND scheduled_date "
                "BETWEEN ? AND ? ORDER BY scheduled_date, id",
                (athlete_id, first.isoformat(), last.isoformat()),
            )
        ]
        activity_rows = [
            _dict(row)
            for row in conn.execute(
                f"SELECT {_ACTIVITY_SELECT} FROM activities WHERE athlete_id = ? "
                f"AND local_date BETWEEN ? AND ? ORDER BY local_date, start_time_utc, id",
                (athlete_id, first.isoformat(), last.isoformat()),
            )
        ]
        event_rows = [
            _dict(row)
            for row in conn.execute(
                "SELECT * FROM events WHERE athlete_id = ? AND event_date BETWEEN ? AND ?",
                (athlete_id, first.isoformat(), last.isoformat()),
            )
        ]
        history = History(conn, athlete_id)
        activities = []
        for row in activity_rows:
            assert row is not None
            entry = _project(row, _ACTIVITY_OUT_FIELDS)
            entry["load"] = _activity_load(history, row).as_dict()
            activities.append(entry)

        # The FTP each session should have been written against: the one in
        # effect on its own date. Resolving "the latest entry" instead let a
        # future-dated one — a scheduled test result, or a typo — mark this
        # week's correctly-written plans as stale against a number not yet in
        # force.
        ftp_on_date = {
            row["scheduled_date"]: resolve_ftp(conn, athlete_id, row["scheduled_date"])
            for row in planned_rows
            if row
        }

        # Links are stored on the planned workout, and link_activity permits a
        # cross-date link — a Sunday session ridden Monday. Looking only at
        # plans scheduled inside the window left that ride reported as
        # unplanned in the following week, with the link sitting in the
        # database the whole time.
        activity_ids = [entry["id"] for entry in activities]
        linked_elsewhere: set[int] = set()
        events_by_activity_any_date: dict[int, dict] = {}
        if activity_ids:
            marks = ", ".join("?" for _ in activity_ids)
            linked_elsewhere = {
                row["linked_activity_id"]
                for row in conn.execute(
                    f"SELECT linked_activity_id FROM planned_workouts WHERE athlete_id = ? "
                    f"AND linked_activity_id IN ({marks})",
                    (athlete_id, *activity_ids),
                )
            }
            events_by_activity_any_date = {
                row["linked_activity_id"]: _dict(row) or {}
                for row in conn.execute(
                    f"SELECT * FROM events WHERE athlete_id = ? "
                    f"AND linked_activity_id IN ({marks})",
                    (athlete_id, *activity_ids),
                )
            }

    linked_to_plan = {
        row["linked_activity_id"] for row in planned_rows if row and row["linked_activity_id"]
    } | linked_elsewhere
    event_by_activity = {
        **events_by_activity_any_date,
        **{
            row["linked_activity_id"]: row
            for row in event_rows
            if row and row["linked_activity_id"]
        },
    }

    planned: list[dict] = []
    not_ridden: list[dict] = []
    for row in planned_rows:
        assert row is not None
        entry = _planned_summary(row)
        in_effect = ftp_on_date.get(row["scheduled_date"])
        if (
            in_effect
            and entry.get("spec", {}).get("ftp")
            and entry["spec"]["ftp"] != in_effect["value_watts"]
        ):
            entry["stale_ftp"] = (
                f"written against {entry['spec']['ftp']} W; the FTP in effect on "
                f"{row['scheduled_date']} is {in_effect['value_watts']} W "
                f"({in_effect['effective_date']}). Update the spec before rendering, or it "
                "prescribes the wrong intensity."
            )
        planned.append(entry)
        if (
            row["linked_activity_id"] is None
            and row["status"] in ("planned", "pushed")
            and parse_date(row["scheduled_date"], "scheduled_date") < reference
        ):
            not_ridden.append(
                {
                    "planned_workout_id": row["id"],
                    "scheduled_date": row["scheduled_date"],
                    "summary": entry.get("summary"),
                    "status": row["status"],
                    "sentence": (
                        f"{entry.get('summary', 'a planned session')} was scheduled for "
                        f"{row['scheduled_date']} and no ride is linked to it"
                    ),
                }
            )

    unplanned: list[dict] = []
    for entry in activities:
        if entry["id"] in linked_to_plan:
            continue
        event = event_by_activity.get(entry["id"])
        if event:
            entry["event"] = {"id": event["id"], "name": event["name"]}
            continue
        unplanned.append(
            {
                "activity_id": entry["id"],
                "local_date": entry["local_date"],
                "sport": entry["sport"],
                "name": entry["name"],
                "duration": format_duration(round(entry["duration_s"] or 0)),
                "tss": (entry.get("load") or {}).get("tss"),
                "sentence": (
                    f"{entry['local_date']}: {entry['name'] or entry['sport']}, "
                    f"{format_duration(round(entry['duration_s'] or 0))}, was ridden with "
                    "nothing planned for it"
                ),
            }
        )

    aggregate = _aggregate_load(activities)
    planned_tss = sum(entry.get("planned_tss") or 0.0 for entry in planned)
    return {
        "start": first.isoformat(),
        "end": last.isoformat(),
        "today": reference.isoformat(),
        "planned_workouts": planned,
        "activities": activities,
        "events": [_event_out(row) for row in event_rows if row],
        "totals": {
            "planned_sessions": len(planned),
            "activities": len(activities),
            "planned_tss": round(planned_tss, 1),
            "actual_tss": aggregate["total_tss"],
            "activities_scored": aggregate["scored"],
            "activities_unscored": aggregate["unscored"],
            "by_method": aggregate["by_method"],
            # planned_tss is always power-based, so a like-for-like reading of
            # the two numbers depends on knowing what actual_tss is made of.
            **{
                key: aggregate[key]
                for key in ("mixed_methods_warning", "unscored_warning")
                if key in aggregate
            },
        },
        "deviations": {
            "planned_not_ridden": not_ridden,
            "ridden_not_planned": unplanned,
        },
    }


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------


def compute_load(
    start: str | None = None,
    end: str | None = None,
    activity_ids: list[int] | None = None,
    sport: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Training load per activity, each scored against the figures of its own date.

    Power first: TSS = duration_h x IF^2 x 100 with IF = NP / FTP, where FTP is
    the entry in effect on the ride's date, not today's. When a ride has no
    normalised power, average power is used and the result is flagged
    `no_normalized_power` — NP is never below average, so that number
    understates a variable ride.

    Without power, the fallback is hrTSS: the same formula with
    (average HR / threshold HR) in place of the power ratio. **The two are not
    comparable.** hrTSS cannot see variability, so thirty sprints and a steady
    tempo ride at the same average HR score identically; cardiac drift inflates
    long rides; and it inherits every error in the threshold HR, which is often
    itself an estimate from max HR. `method` on each row says which was used,
    and `by_method` counts them — a week whose total mixes both is a week whose
    total means less than it looks.

    Rides with neither power nor heart rate return a null TSS and a reason,
    never a zero. A zero would be indistinguishable from a rest day.
    """
    sport = _one_of(sport, SPORTS, "sport")
    if activity_ids is not None and not activity_ids:
        # An empty list is a filter that matched nothing, not an absent filter.
        # Falling through to "no id clause" scored the athlete's entire history
        # and returned it as this selection's total — a plausible wrong number,
        # which is worse than an empty one.
        return {
            "count": 0,
            "activities": [],
            "by_method": {},
            "total_tss": 0.0,
            "unscored": 0,
            "note": "activity_ids was an empty list, so nothing was selected.",
        }
    clauses = ["athlete_id = ?"]
    params: list[Any] = [athlete_id]
    if activity_ids:
        clauses.append(f"id IN ({', '.join('?' for _ in activity_ids)})")
        params.extend(int(value) for value in activity_ids)
    if start:
        clauses.append("local_date >= ?")
        params.append(parse_date(start, "start").isoformat())
    if end:
        clauses.append("local_date <= ?")
        params.append(parse_date(end, "end").isoformat())
    if sport:
        clauses.append("sport = ?")
        params.append(sport)

    rows_out: list[dict] = []
    with open_db() as conn:
        history = History(conn, athlete_id)
        for row in conn.execute(
            f"SELECT {_ACTIVITY_SELECT} FROM activities WHERE {' AND '.join(clauses)} "
            f"ORDER BY local_date, id",
            params,
        ):
            activity = _dict(row) or {}
            load = _activity_load(history, activity)
            rows_out.append(
                {
                    "activity_id": activity["id"],
                    "garmin_activity_id": activity["garmin_activity_id"],
                    "local_date": activity["local_date"],
                    "name": activity["name"],
                    "sport": activity["sport"],
                    "duration": format_duration(round(activity["duration_s"] or 0)),
                    "normalized_power": activity["normalized_power"],
                    "avg_power": activity["avg_power"],
                    "avg_hr": activity["avg_hr"],
                    **load.as_dict(),
                }
            )

    return {"count": len(rows_out), "activities": rows_out, **_aggregate_load(rows_out)}


def get_form(
    start: str,
    end: str,
    seed_ctl: float = 0.0,
    seed_atl: float = 0.0,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """CTL, ATL and TSB across a date range, from the stored activities.

    The standard exponentially weighted model, stepped once per calendar day
    including rest days:

        CTL(d) = CTL(d-1) + (TSS(d) - CTL(d-1)) / 42     "fitness"
        ATL(d) = ATL(d-1) + (TSS(d) - ATL(d-1)) / 7      "fatigue"
        TSB(d) = CTL(d-1) - ATL(d-1)                     "form"

    TSB is **yesterday's** balance — the form carried into a day, before that
    day's session lands on it. Some tools report same-day CTL - ATL instead;
    the two differ by about the size of a session, which is the difference
    between reading a Tuesday as fresh and reading it as buried.

    The walk starts at the earliest stored activity so that CTL entering the
    window is built from real history. Where that run-up is shorter than 42
    days the numbers are still climbing out of their starting value and are
    reported as `warmup_incomplete`: an athlete whose first import is three
    weeks old has a CTL that says more about the import date than about them.
    Pass `seed_ctl`/`seed_atl` if better starting values are known.

    **Cross-check this against the Garmin MCP's `get_training_load_trend`.**
    Garmin computes from every activity it holds, this from what has been
    imported, and it uses its own load metric rather than TSS. A disagreement
    is information — usually a gap in what was imported, or rides scored by
    heart rate here — and it is worth finding the cause rather than splitting
    the difference between two numbers, only one of which you can explain.
    """
    first = parse_date(start, "start")
    last = parse_date(end, "end")
    if last < first:
        raise CoachError(f"end ({end}) is before start ({start})")

    daily: dict[date, float] = {}
    methods: dict[str, int] = {}
    earliest: date | None = None
    with open_db() as conn:
        history = History(conn, athlete_id)
        for row in conn.execute(
            f"SELECT {_ACTIVITY_SELECT} FROM activities WHERE athlete_id = ? "
            f"AND local_date <= ? ORDER BY local_date",
            (athlete_id, last.isoformat()),
        ):
            activity = _dict(row) or {}
            day = parse_date(activity["local_date"], "local_date")
            earliest = day if earliest is None else min(earliest, day)
            load = _activity_load(history, activity)
            methods[load.method] = methods.get(load.method, 0) + 1
            if load.tss is not None:
                daily[day] = daily.get(day, 0.0) + load.tss

    points = form_series(daily, first, last, seed_ctl=seed_ctl, seed_atl=seed_atl)
    runup_days = (first - earliest).days if earliest else 0

    result: dict[str, Any] = {
        "start": first.isoformat(),
        "end": last.isoformat(),
        "series": [point.as_dict() for point in points],
        "constants": {"ctl_days": 42, "atl_days": 7, "tsb": "yesterday's CTL minus ATL"},
        "methods": methods,
        "history_days_before_start": max(runup_days, 0),
    }
    if points:
        latest = points[-1]
        result["latest"] = latest.as_dict()
        result["sentence"] = (
            f"On {latest.day.isoformat()}: CTL {latest.ctl:.0f}, ATL {latest.atl:.0f}, "
            f"TSB {latest.tsb:+.0f}."
        )
    if runup_days < 42:
        result["warmup_incomplete"] = (
            f"Only {max(runup_days, 0)} days of history precede {first.isoformat()}, and CTL "
            "needs about 42 to stop being dominated by its starting value. These figures are "
            "an underestimate — import more history, or pass seed_ctl/seed_atl."
        )
    if methods.get("hr"):
        result["mixed_methods_warning"] = (
            f"{methods['hr']} activities were scored from heart rate rather than power. "
            "hrTSS is not the same quantity as power TSS; a CTL built from both is an "
            "approximation."
        )
    return result


def _planned_blocks(spec: dict) -> list[dict]:
    """Every executable block of a spec in ride order, with target watts.

    Repeats are expanded, because that is what the laps on a head unit look
    like: three intervals in a repeat are three laps, not one.
    """
    workout = load_spec(spec)
    blocks = []
    for index, block in enumerate(workout.steps(), start=1):
        if block.kind == "free":
            low = high = None
        elif block.kind == "ramp":
            ends = sorted((workout.watts(block.p_from), workout.watts(block.p_to)))
            low, high = ends[0], ends[1]
        else:
            low = workout.watts(block.p_low)
            high = workout.watts(block.p_high)
        blocks.append(
            {
                "index": index,
                "role": block.role,
                "kind": block.kind,
                "duration_s": block.duration_s,
                "low_w": low,
                "high_w": high,
                "hr_note": block.hr_note,
            }
        )
    return blocks


def _compliance_verdict(
    alignment: str, comparisons: list[dict], deviating: list[dict], unverifiable: list[dict]
) -> str:
    """One word for the session: what the comparison actually established.

    `unverifiable` is its own answer. A ride with no power in any lap says
    nothing about whether the targets were held, and calling that either
    `as_prescribed` or `deviated` asserts something the data cannot support.
    """
    if alignment != "by_lap":
        return "no_block_data"
    if comparisons and len(unverifiable) == len(comparisons):
        return "unverifiable"
    if deviating:
        return "deviated"
    return "as_prescribed"


def compliance_report(
    planned_workout_id: int,
    activity_id: int | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """What was prescribed against what was ridden, block by block where possible.

    Uses the activity linked to the planned session unless `activity_id`
    overrides it. When laps are stored and their count matches the plan's
    executable blocks — repeats expanded, because three intervals are three
    laps — each block is compared against its lap and the finding is written
    out as a sentence: "the second block fell to 228 W against a 250 W target".

    When the counts differ, no pairing is invented. Aligning six laps against
    nine blocks by position produces confident statements about the wrong
    intervals, and a lap count rarely matches a plan exactly — an athlete who
    presses lap at a junction, or a head unit that auto-laps every 5 km, breaks
    it. The laps are returned as they are, with the summary comparison, and the
    mismatch is stated.

    With no laps at all, only durations and average intensity can be compared.
    That distinguishes a session that was shortened from one that was not; it
    cannot distinguish an interval session ridden properly from the same
    average ridden as steady tempo.

    A tolerance of 5% of the target either side counts as on target — a
    trainer holds a watt target far more tightly than a road ever will. Note
    that this is **not** the band `render_garmin` writes to the head unit,
    which is 2% for intervals, 5% at the easy end, and whatever
    `garmin_target_band_pct` says when the spec sets it. The two are measuring
    different things: the rendered band is what the athlete was told to hold,
    this one is how far off a lap average has to be before it is worth
    mentioning. An athlete riding the top edge of a 2% displayed band is inside
    this tolerance and reads as on target, which is the intended answer. A
    recovery, warmup or cooldown block ridden below its target is reported as
    `easier_than_target` and does not count against compliance: that target is
    a ceiling, and spinning easier than prescribed on a recovery is the session
    working, not failing. A block cut short is still a deviation even when the
    watts were right: `off_target_blocks` counts wrong power,
    `deviating_blocks` adds short and long, and `verdict` follows the latter.
    """
    with open_db() as conn:
        planned = _dict(
            conn.execute(
                "SELECT * FROM planned_workouts WHERE id = ? AND athlete_id = ?",
                (planned_workout_id, athlete_id),
            ).fetchone()
        )
        if planned is None:
            raise CoachError(f"no planned workout with id {planned_workout_id}")

        target_id = activity_id if activity_id is not None else planned["linked_activity_id"]
        if target_id is None:
            raise CoachError(
                f"planned workout {planned_workout_id} has no activity linked to it. Call "
                "link_activity first, or pass activity_id."
            )
        activity = _find_activity(conn, athlete_id, target_id, None)
        laps = [
            _project(row, _LAP_OUT_FIELDS)
            for row in conn.execute(
                "SELECT * FROM activity_laps WHERE activity_id = ? ORDER BY lap_index",
                (activity["id"],),
            )
        ]
        history = History(conn, athlete_id)
        load = _activity_load(history, activity)
        ftp_entry = history.ftp(activity["local_date"])

    spec = _spec_of(planned)
    try:
        workout = load_spec(spec)
    except SpecError as exc:
        raise CoachError(
            "the stored spec no longer validates, so there is nothing to compare against: "
            + "; ".join(exc.errors)
        ) from None
    metrics = compute_metrics(workout)
    blocks = _planned_blocks(spec)

    comparisons: list[dict] = []
    alignment = "none"
    if laps and len(laps) == len(blocks):
        alignment = "by_lap"
        for block, lap in zip(blocks, laps, strict=True):
            comparisons.append(
                compare_block(
                    block["index"],
                    block["role"],
                    block["duration_s"],
                    block["low_w"],
                    block["high_w"],
                    lap,
                ).as_dict()
            )
    elif laps:
        alignment = "mismatch"

    planned_seconds = metrics.total_seconds
    actual_seconds = activity.get("duration_s") or 0
    duration_delta = actual_seconds - planned_seconds
    actual_np = activity.get("normalized_power") or activity.get("avg_power")
    summary_sentences: list[str] = [
        f"Planned {format_duration(planned_seconds)} at IF "
        f"{metrics.intensity_factor:.2f} (NP {metrics.normalised_power} W, "
        f"TSS {metrics.tss:.0f}); rode {format_duration(round(actual_seconds))}"
        + (f" at {round(actual_np)} W" if actual_np else "")
        + (f" for {load.tss:.0f} TSS" if load.tss is not None else "")
        + "."
    ]
    if abs(duration_delta) >= max(120, planned_seconds * 0.05):
        summary_sentences.append(
            f"The ride was {format_duration(abs(round(duration_delta)))} "
            f"{'longer' if duration_delta > 0 else 'shorter'} than planned."
        )
    if alignment == "mismatch":
        summary_sentences.append(
            f"{len(laps)} laps were recorded against {len(blocks)} planned blocks, so no "
            "block-by-block comparison was made."
        )
    elif not laps:
        summary_sentences.append(
            "No laps are stored for this ride, so only totals could be compared. Import them "
            "with import_activity_laps to see the session block by block."
        )
    summary_sentences.extend(entry["sentence"] for entry in comparisons)

    # "Deviated" has to include a block that was cut short, not only one ridden
    # at the wrong power. A 10-minute interval abandoned after 5 at exactly the
    # right watts was reported as prescribed, which is the opposite of what
    # happened.
    off_target = [entry for entry in comparisons if entry["verdict"] in ("under", "over")]
    unverifiable = [entry for entry in comparisons if entry["verdict"] in UNVERIFIABLE_VERDICTS]
    # `no_power` is not a deviation: the lap recorded no watts, so the target
    # was neither hit nor missed. Counting it made an HR-only ride — ridden for
    # exactly the right durations — come back as a failed session.
    deviating = [
        entry
        for entry in comparisons
        if entry["verdict"] not in COMPLIANT_VERDICTS
        and entry["verdict"] not in UNVERIFIABLE_VERDICTS
    ]
    return {
        "planned_workout_id": planned_workout_id,
        "activity_id": activity["id"],
        "scheduled_date": planned["scheduled_date"],
        "ridden_date": activity["local_date"],
        "date_matches": planned["scheduled_date"] == activity["local_date"],
        "alignment": alignment,
        "ftp_used_w": ftp_entry["value_watts"] if ftp_entry else None,
        "spec_ftp_w": spec.get("ftp"),
        "planned": {
            "name": workout.name,
            "duration": format_duration(planned_seconds),
            "duration_s": planned_seconds,
            "normalized_power_w": metrics.normalised_power,
            "intensity_factor": round(metrics.intensity_factor, 3),
            "tss": round(metrics.tss, 1),
            "blocks": blocks,
        },
        "actual": {
            "name": activity["name"],
            "duration": format_duration(round(actual_seconds)),
            "duration_s": actual_seconds,
            "normalized_power_w": activity.get("normalized_power"),
            "avg_power_w": activity.get("avg_power"),
            "avg_hr": activity.get("avg_hr"),
            "rpe": activity.get("rpe"),
            "feel": activity.get("feel"),
            **load.as_dict(),
        },
        "blocks": comparisons,
        # Laps are returned whenever they exist, including on a count mismatch:
        # that is precisely the case where no comparison could be made and the
        # caller has to look at them.
        "laps": laps,
        "off_target_blocks": len(off_target),
        "deviating_blocks": len(deviating),
        "unverifiable_blocks": len(unverifiable),
        "sentences": summary_sentences,
        "verdict": _compliance_verdict(alignment, comparisons, deviating, unverifiable),
    }


# --------------------------------------------------------------------------
# maintenance
# --------------------------------------------------------------------------

#: Export and restore order: parents first. `events` and `planned_workouts`
#: both carry a `linked_activity_id`, so they must be inserted after the
#: activities they point at or the restore fails on a foreign key. Deletion
#: walks this list in reverse for the same reason.
_EXPORT_TABLES = (
    "athlete",
    "ftp_history",
    "weight_history",
    "hr_history",
    "activities",
    "activity_laps",
    "events",
    "planned_workouts",
)


def export_data(athlete_id: int | None = None) -> dict:
    """The whole database as JSON, with a digest of the content.

    Every row of every table, `raw_json` included, so a restore is lossless.
    `digest` covers the `data` object exactly as returned: pass it back to
    `import_data` as `expected_digest` and a restore that lost or altered a row
    on the way through is refused rather than silently applied. That matters
    here more than anywhere else in this server — a restore overwrites, and
    there is nothing to compare against afterwards.

    Omit `athlete_id` to export everything.
    """
    data: dict[str, list[dict]] = {}
    with open_db() as conn:
        version = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
        for table in _EXPORT_TABLES:
            if athlete_id is None:
                rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            elif table == "activity_laps":
                # Laps carry no athlete of their own; they belong to whichever
                # activity they hang off. Exporting all of them for a single
                # athlete would ship another athlete's ride data.
                rows = conn.execute(
                    "SELECT * FROM activity_laps WHERE activity_id IN "
                    "(SELECT id FROM activities WHERE athlete_id = ?) ORDER BY rowid",
                    (athlete_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE athlete_id = ? ORDER BY rowid", (athlete_id,)
                ).fetchall()
            data[table] = [_dict(row) or {} for row in rows]

    payload = {"schema_version": int(version or 0), "athlete_id": athlete_id, "tables": data}
    return {
        "data": payload,
        "digest": payload_digest(payload),
        "counts": {table: len(rows) for table, rows in data.items()},
        "note": (
            "Pass `data` and `digest` to import_data to restore. Keep the digest with the "
            "file; it is the only way to tell a truncated copy from a complete one."
        ),
    }


def import_data(data: dict, force: bool = False, expected_digest: str | None = None) -> dict:
    """Restore a database from `export_data` output. Refuses to overwrite by default.

    A non-empty database is left untouched unless `force` is set, and `force`
    **deletes every existing row** before inserting — this is a restore, not a
    merge. Merging two training logs is not something to do implicitly: the
    same ride imported under two Garmin ids, or two conflicting FTP entries for
    one date, would silently change every number computed afterwards.

    Pass `expected_digest` from the export. A mismatch refuses the restore
    rather than applying a payload that was truncated or edited in transit.

    An export from a newer schema than this build understands is refused
    outright — the missing columns would be dropped silently on insert.
    """
    if not isinstance(data, dict) or not isinstance(data.get("tables"), dict):
        raise CoachError("data must be the object returned by export_data (with a 'tables' key)")

    if expected_digest is not None:
        actual = payload_digest(data)
        if actual != expected_digest.strip():
            return {
                "restored": False,
                "reason": (
                    f"this payload digests to {actual} but {expected_digest.strip()} was "
                    "expected — it is not the export it claims to be. Nothing was changed."
                ),
            }

    exported_version = int(data.get("schema_version") or 0)
    if exported_version > CURRENT_SCHEMA_VERSION:
        return {
            "restored": False,
            "reason": (
                f"the export is from schema v{exported_version} and this build understands "
                f"v{CURRENT_SCHEMA_VERSION}. Restoring it would drop the columns this build "
                "does not know about. Upgrade first."
            ),
        }

    tables = data["tables"]
    with open_db() as conn:
        existing = {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in _EXPORT_TABLES
        }
        occupied = {table: count for table, count in existing.items() if count}
        if occupied and not force:
            return {
                "restored": False,
                "reason": (
                    "the database already holds data "
                    f"({', '.join(f'{table}: {count}' for table, count in occupied.items())}). "
                    "Restoring replaces all of it. Pass force=true if that is the intent — "
                    "and export the current contents first."
                ),
                "existing_counts": existing,
            }

        # Children before parents, so foreign keys hold at every step.
        for table in reversed(_EXPORT_TABLES):
            conn.execute(f"DELETE FROM {table}")

        counts: dict[str, int] = {}
        for table in _EXPORT_TABLES:
            rows = tables.get(table) or []
            if not isinstance(rows, list):
                raise CoachError(f"tables.{table} must be a list of rows")
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    raise CoachError(
                        f"tables.{table}[{index}] is a {type(row).__name__}, not a row object. "
                        "This is not export_data output; nothing was restored."
                    )
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for row in rows:
                usable = {key: value for key, value in row.items() if key in columns}
                if not usable:
                    continue
                names = ", ".join(usable)
                placeholders = ", ".join("?" for _ in usable)
                conn.execute(
                    f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                    list(usable.values()),
                )
            counts[table] = len(rows)

    return {
        "restored": True,
        "forced": bool(occupied) if force else False,
        "counts": counts,
        "schema_version": exported_version,
    }

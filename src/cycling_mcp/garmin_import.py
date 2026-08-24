"""Normalise raw Garmin MCP output into the rows this server stores.

Ingestion is model-mediated: Claude calls the Garmin MCP, gets JSON back, and
hands that JSON to this server unchanged. Nothing here reaches Garmin — this
module only reads what was pasted in.

That design is deliberate and it is the whole reason this module exists. The
alternative — a tool taking a clean typed schema — would make the model retype
every number on the way through, and a mistyped average power is a training
load that is wrong and looks fine. So the import surface accepts Garmin's own
shapes, warts included, and does the field mapping here where it can be tested.

Shapes tolerated, because the Garmin MCP emits all of them depending on the
call: a bare list of activities (`get_activities`), a single activity object
(`get_activity`), a detailed activity whose numbers live under `summaryDTO`,
and a wrapper dict keyed `activities` / `activityList` / `results` / `data`.
JSON handed over as a string is parsed. Unknown keys are kept in `raw_json`
rather than rejected — Garmin adds fields without warning, and an import that
fails on a new one fails for every ride at once.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, NamedTuple

# Garmin reports a bike ride under whichever child type the device chose:
# "virtual_ride" for a trainer app, "indoor_cycling" for a smart trainer with no
# app, "road_biking"/"gravel_cycling" outdoors. Filtering on typeKey == "cycling"
# quietly drops most indoor training, which is exactly the training a plan is
# built from — so the raw key is kept in `sub_sport` and this family is what
# `sport` holds.
_CYCLING_MARKERS = ("cycling", "biking", "bike", "ride")
_RUNNING_MARKERS = ("running", "run")
_SWIMMING_MARKERS = ("swimming", "swim")

# Fields read straight off the activity, in the order they are looked for. The
# first key present wins; each is a real name seen in Garmin MCP output.
_ALIASES: dict[str, tuple[str, ...]] = {
    "garmin_activity_id": ("activityId", "activity_id", "id"),
    "name": ("activityName", "activity_name", "name"),
    "start_time_local": ("startTimeLocal", "start_time_local"),
    "start_time_utc": ("startTimeGMT", "startTimeGmt", "start_time_gmt", "startTimeUtc"),
    "duration_s": ("duration", "elapsedDuration", "elapsed_duration", "durationInSeconds"),
    "moving_duration_s": ("movingDuration", "moving_duration"),
    "distance_m": ("distance", "distanceInMeters"),
    "elevation_gain_m": ("elevationGain", "elevation_gain", "totalElevationGain"),
    "avg_hr": ("averageHR", "averageHr", "avgHr", "average_hr"),
    "max_hr": ("maxHR", "maxHr", "max_hr"),
    "avg_power": ("avgPower", "averagePower", "average_power"),
    "max_power": ("maxPower", "max_power"),
    "normalized_power": ("normPower", "normalizedPower", "normalized_power"),
    "calories": ("calories", "activeKilocalories"),
}

_LAP_ALIASES: dict[str, tuple[str, ...]] = {
    "duration_s": ("duration", "elapsedDuration", "durationInSeconds"),
    "moving_duration_s": ("movingDuration",),
    "distance_m": ("distance",),
    "avg_power": ("averagePower", "avgPower"),
    "max_power": ("maxPower",),
    "normalized_power": ("normalizedPower", "normPower"),
    "avg_hr": ("averageHR", "averageHr", "avgHr"),
    "max_hr": ("maxHR", "maxHr"),
    "avg_cadence": (
        "averageBikingCadenceInRevPerMinute",
        "averageRunCadence",
        "averageCadence",
        "avgCadence",
    ),
    "elevation_gain_m": ("elevationGain",),
}

_INT_FIELDS = {"avg_hr", "max_hr"}

# A fractional-seconds field, with whatever follows it (an offset, or nothing).
_FRACTION = re.compile(r"^(.*?)\.(\d+)(.*)$")

# A trailing UTC offset in any legal ISO-8601 spelling: "+02:00", "+0200",
# "+02". `strftime("%z")` writes the middle one and `fromisoformat` refuses it
# before 3.11 — and a fallback that strips an offset turns it into an instant
# two hours from the truth. The minute field is optional because the hour-only
# form was the fourth shape of that same defect to be found; matching the shape
# is what lets `_from_patterns` reject anything it still cannot apply. With the
# minutes optional this *can* match inside a bare date — "2026-08-20" ends in
# "-20" — so it is never read on its own: `_split_offset` requires a time in
# front of it.
_OFFSET = re.compile(r"([+-])(\d{2})(?::?(\d{2}))?$")

# A value that is a whole date and nothing else. It is what tells a real
# offset on a date-only value from the false positive the pattern above finds
# inside every date: "2026-08-20" leaves "2026-08", "2026-08-20-05:00" leaves
# a whole date and so really does carry an offset.
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


#: The band a ride's date has to fall in to be a ride at all. The floor
#: predates power meters, Garmin Connect and every athlete's training log; the
#: ceiling is today plus enough slop for a device in a timezone ahead of this
#: server. Outside it, a date is a sentinel or a broken clock — not a training
#: date — and it is refused per row.
#:
#: Convertible is not the same question as plausible, which is why this exists
#: beside the overflow guards rather than instead of them: "0001-01-01" with a
#: *negative* offset converts away from the underflow and stores perfectly
#: happily, and every date-walking tool downstream then has to cross two
#: thousand years to reach the ride behind it.
PLAUSIBLE_DATE_FLOOR = date(1990, 1, 1)
FUTURE_DATE_SLOP_DAYS = 2


def implausible_date(day: str, today: date | None = None) -> str | None:
    """Why this date cannot be a ride's, or None when it can be.

    A sentence, not a bool: the import path reports per-row rejections with a
    reason, and "3 rejected" tells nobody what to fix.
    """
    try:
        parsed = date.fromisoformat(day)
    except (TypeError, ValueError):
        return f"has an unreadable date ({day!r})"
    ceiling = (today or date.today()) + timedelta(days=FUTURE_DATE_SLOP_DAYS)
    if parsed < PLAUSIBLE_DATE_FLOOR:
        return (
            f"is dated {parsed.isoformat()}, before {PLAUSIBLE_DATE_FLOOR.isoformat()} — that is "
            "a zero-date sentinel or a device clock that never set, not a ride. Storing it puts "
            "a ride two thousand years behind the rest of the log, which every form and load "
            "calculation then has to walk across"
        )
    if parsed > ceiling:
        return (
            f"is dated {parsed.isoformat()}, which is in the future — a device clock that is "
            "wrong, not a ride that has happened"
        )
    return None


class GarminPayloadError(ValueError):
    """Raised when a payload is not something this module can read at all."""


def _loads(raw: Any) -> Any:
    """Parse a JSON string, or pass through anything already decoded."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GarminPayloadError(
                f"payload is a string but not JSON ({exc.msg} at position {exc.pos}). "
                "Pass the Garmin MCP's result as an object or array, not as prose."
            ) from None
    return raw


def as_activity_list(raw: Any) -> list[dict]:
    """Every activity object in `raw`, whatever shape the Garmin MCP returned.

    Raises GarminPayloadError when the payload holds no recognisable activity, with a
    message naming what was seen — "this is a list of strings" is a fixable
    report, "0 imported" is not.
    """
    data = _loads(raw)

    if isinstance(data, dict):
        for key in ("activities", "activityList", "results", "data", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]

    if not isinstance(data, list):
        raise GarminPayloadError(
            f"expected an activity object or a list of them, got {type(data).__name__}"
        )

    items = [item for item in data if isinstance(item, dict)]
    if not items:
        kinds = sorted({type(item).__name__ for item in data}) or ["nothing"]
        raise GarminPayloadError(
            f"no activity objects in the payload — it holds {', '.join(kinds)}. "
            "Pass the Garmin MCP result unchanged rather than a list of ids or names."
        )
    return items


def as_lap_list(raw: Any) -> list[dict]:
    """Every lap object in a splits payload.

    `get_activity_split_summaries` is rejected on purpose: it returns
    per-*type* aggregates ("CLIMB", "DESCENT"), not the laps in execution
    order, and comparing a plan block-by-block against them silently compares
    the wrong things.
    """
    data = _loads(raw)

    if isinstance(data, dict):
        if isinstance(data.get("splitSummaries"), list) and not any(
            isinstance(data.get(k), list) for k in ("lapDTOs", "laps", "splits")
        ):
            raise GarminPayloadError(
                "this is a split *summary* (per-type aggregates), not the laps. Use the "
                "Garmin MCP's get_activity_splits, which returns lapDTOs in execution order."
            )
        for key in ("lapDTOs", "laps", "splits", "lapList", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]

    if not isinstance(data, list):
        raise GarminPayloadError(f"expected a splits payload, got {type(data).__name__}")

    laps = [item for item in data if isinstance(item, dict)]
    if not laps:
        raise GarminPayloadError("no lap objects in the payload")
    return laps


# --------------------------------------------------------------------------
# field reading
# --------------------------------------------------------------------------


def _pick(item: dict, keys: tuple[str, ...]) -> Any:
    """First present, non-null value among `keys`, looked up in the nested DTOs too.

    `get_activities` returns everything flat; `get_activity` buries the same
    numbers in `summaryDTO` and the type in `activityTypeDTO`. Reading both
    means one import tool covers a list fetch and a detail fetch.
    """
    sources = [item]
    for nested in ("summaryDTO", "activityTypeDTO", "summary", "activitySummary"):
        value = item.get(nested)
        if isinstance(value, dict):
            sources.append(value)
    for key in keys:
        for source in sources:
            if source.get(key) is not None:
                return source[key]
    return None


def _number(value: Any) -> float | None:
    """A float, or None. Booleans are not numbers here, whatever Python thinks.

    Deliberately not shared with `verify.py`'s two coercers, which look similar
    and are not interchangeable:

    * `verify._number` rejects strings outright, because a string where an API
      payload should hold a number is a shape error worth failing on.
    * `verify._as_number` parses strings but must not touch commas — it reads
      values scraped off a web page, where "1,234" is one thousand two hundred
      and thirty-four.

    This one treats a comma as a decimal separator, because a Garmin export
    made under a European locale writes 232,5 for 232.5. Folding the three into
    one would silently pick one of those three behaviours for all of them.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", ".")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _activity_id(raw: Any) -> str | None:
    """The Garmin id as text.

    Text, not an integer, because it is a dedupe key that travels through JSON:
    a 10-digit id that arrives as 1.662651131e9 and is stored as a float would
    stop matching itself on the next import, and the ride would be duplicated
    rather than updated.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    if isinstance(raw, (int, str)):
        text = str(raw).strip()
        return text or None
    return None


def _timestamp(value: Any, to_utc: bool = False) -> str | None:
    """Garmin's several time formats, normalised to `YYYY-MM-DDTHH:MM:SS`.

    Seen in the wild: "2026-08-20 07:12:33", the same with a "T", a trailing
    ".0" of milliseconds, epoch milliseconds on `beginTimestamp`, and — from
    anything that re-exports Garmin data — full ISO-8601 with a UTC offset,
    "2026-08-20T07:12:33+02:00".

    The offset form is parsed first, by `fromisoformat`, because the pattern
    list cannot read one: stripping fractional seconds leaves "+02:00" attached
    and every pattern fails. Both start times then come back None and the whole
    activity is rejected as having no readable start — a ride lost to a
    timezone suffix.

    `to_utc` decides what an offset means. For a local start time the wall
    clock is the point ("the day the athlete rode"), so the offset is dropped.
    For `startTimeGMT` the instant is the point, so an offset is converted;
    a naive value is already UTC and is left alone.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Epoch milliseconds. Seconds-since-epoch would put us in 1970, which
        # is not a date any athlete rode on.
        #
        # The whole branch is inside the try, not just the arithmetic: JSON
        # integers are arbitrary-precision in Python, so `float(10**400)`
        # overflows one line *above* where the guard used to start, and that
        # raise aborted the entire import call — losing every valid ride in the
        # batch to one corrupt row, which is the exact invariant the guard
        # exists to hold. Only OverflowError is reachable here now: NaN and the
        # infinities are refused by the finiteness check rather than left to
        # raise ValueError from `timedelta`, so a narrow except keeps a future
        # bug in this block loud instead of turning it into a silent rejection.
        try:
            seconds = float(value) / 1000.0
            if not math.isfinite(seconds) or seconds < 10**8:
                return None
            moment = datetime(1970, 1, 1) + timedelta(seconds=seconds)
        except OverflowError:
            return None
        return _iso(moment)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    # Normalised once, not shape by shape. `fromisoformat` before 3.11 — this
    # package's declared floor — rejects "Z", a fractional-second field that is
    # not exactly 3 or 6 digits, and a UTC offset written without its colon,
    # which is precisely what `strftime("%z")` emits. Each of those used to
    # fall through to the pattern loop below, which discarded everything after
    # the dot and took the offset with it, storing an instant hours wrong with
    # nothing to show for it. Wrong and plausible is worse than rejected.
    iso = text.replace(" ", "T")
    if iso.endswith(("Z", "z")):
        iso = iso[:-1] + "+00:00"
    iso = _pad_fraction(iso)
    split = _split_offset(iso)
    if split is None:
        return None
    try:
        parsed = datetime.fromisoformat(split.normalised)
    except ValueError:
        parsed = None
    if parsed is None:
        parsed = _from_patterns(split)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        try:
            parsed = parsed.astimezone(timezone.utc) if to_utc else parsed
        except OverflowError:
            # A sentinel date with an offset — "0001-01-01T00:00:00+0200", the
            # zero date some exporters write — cannot be moved to UTC without
            # leaving the representable range. A payload is data, not input the
            # caller controls: raising here aborted the whole import call and
            # took every valid ride in the batch with it. An unconvertible
            # timestamp is an unreadable one, and the per-row rejection path
            # already says so with a reason. (The sentinels that convert
            # *without* overflowing are caught by `implausible_date`, because
            # convertible and plausible are not the same question.)
            return None
        # Naive either way: `start_time_utc` is UTC by contract and
        # `start_time_local` is a wall clock, so the offset has done its job by
        # here. Dropped explicitly rather than left for the formatter to omit.
        parsed = parsed.replace(tzinfo=None)
    return _iso(parsed)


class _Split(NamedTuple):
    """A timestamp taken apart once: the datetime text, and its offset if any.

    Both parse paths read this, so the offset is found, normalised and turned
    into a `tzinfo` exactly once. Splitting in `_timestamp` and splitting again
    in the fallback meant the same string was parsed twice and the offset was
    re-read by fixed slicing, which only worked while both halves agreed on the
    spelling they had just written.
    """

    body: str
    offset: str | None
    tz: timezone | None

    @property
    def normalised(self) -> str:
        """The timestamp with its offset in the one spelling `fromisoformat` takes."""
        return self.body + self.offset if self.offset else self.body


def _iso(moment: datetime) -> str:
    """A naive datetime as `YYYY-MM-DDTHH:MM:SS`, four-digit year on every platform.

    Not `strftime("%Y-...")`. That delegates the year to the platform's C
    library, and glibc writes year 1 as "1" where macOS and Windows write
    "0001" — so a sentinel timestamp became "1-01-01T00:00:00", `local_date`
    sliced the first ten characters into "1-01-01T00", and a test written on
    one platform passed while CI failed on another. `isoformat` is Python's
    own, and pads.
    """
    return moment.isoformat(sep="T", timespec="seconds")


def _split_offset(iso: str) -> _Split | None:
    """A timestamp split into its datetime part and its UTC offset as `+HH:MM`.

    None means the string carries an offset that cannot be read as one, and the
    timestamp is unreadable rather than merely offset-less. Two ways in:

    * an offset on a value with no time. `fromisoformat` on 3.11+ takes any
      separator, so it read `"2026-08-20-05:00"` as the *date* 2026-08-20 at
      the *time* 05:00 — a fabricated time of day, from digits that were a
      timezone, and `to_utc` then did nothing because the result was naive. On
      3.10 the same string rejects, so this corruption was invisible to half
      the test matrix. A date with an offset and no clock is not an instant;
    * an offset outside the ±24h a `timezone` can hold, e.g. "+99:00".

    An offset is only *read* off a value carrying a time, because the hour-only
    form matches inside a bare date — "2026-08-20" ends in "-20". That match is
    a false positive when what precedes it is not a whole date, and a real
    offset when it is; the two are told apart rather than both waved through.
    """
    match = _OFFSET.search(iso)
    if match is None:
        return _Split(iso, None, None)
    body = iso[: match.start()]
    if "T" not in body:
        # No clock in front of the match. Either the match landed inside the
        # date itself (a bare "2026-08-20"), which is not an offset at all, or
        # the value really is a date with an offset stuck to it, which is not
        # a readable instant.
        return None if _DATE_ONLY.match(body) else _Split(iso, None, None)
    offset = f"{match.group(1)}{match.group(2)}:{match.group(3) or '00'}"
    sign = -1 if match.group(1) == "-" else 1
    try:
        tz = timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0)))
    except ValueError:
        # "+99:00" is not a UTC offset. Refuse rather than drop it.
        return None
    return _Split(body, offset, tz)


def _from_patterns(split: _Split) -> datetime | None:
    """The shapes `fromisoformat` will not take, with any UTC offset preserved.

    The offset is carried in from `_split_offset` rather than truncated away,
    and anything still attached that this function cannot account for is a
    rejection rather than a truncation. That rule is the point: a fallback that
    silently drops "+02:00" answers with a real-looking instant two hours from
    the truth and nothing downstream can tell, and four rounds of review found
    four spellings of the same offset doing exactly that, one at a time.
    Rejecting costs one row, which `import_activities` reports with a reason.
    """
    # Drop a fractional-seconds field, and *only* a fractional-seconds field.
    # Truncating at the dot is what discarded offsets for four rounds; if what
    # follows the dot is not digits alone, something is still attached that
    # this function has not accounted for.
    head, dot, tail = split.body.partition(".")
    if dot and not tail.isdigit():
        return None

    text = head.replace("T", " ").strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if split.tz is not None and pattern == "%Y-%m-%d":
            # A date, an offset, and no clock between them — "2026-08-20T+02:00"
            # is the spelling that gets here rather than through the guard in
            # `_split_offset`. Reading it as midnight in that zone would invent
            # the same time of day out of the same digits, one branch over.
            return None
        return parsed if split.tz is None else parsed.replace(tzinfo=split.tz)
    return None


def _pad_fraction(text: str) -> str:
    """Pad a fractional-seconds field to six digits, leaving any offset attached.

    `.5` and `.12345` are legal ISO-8601 and rejected by `fromisoformat` before
    3.11; six digits is accepted everywhere, so every fraction is rewritten to
    six — `.500` becomes `.500000`, which is the same instant. A string with no
    fractional field at all comes back untouched.
    """
    match = _FRACTION.match(text)
    if match is None:
        return text
    head, digits, tail = match.groups()
    return f"{head}.{digits[:6].ljust(6, '0')}{tail}"


def local_date_of(row: dict) -> str:
    """The day the athlete believes they trained, from a row's start times.

    Local wall clock when there is one, else the UTC date. Derived rather than
    carried, because a stored `local_date` and the times beside it must never
    be able to disagree: a re-import from a payload with no `startTimeLocal`
    used to overwrite the date with the UTC one while the stored local time
    still said otherwise, moving an evening ride to the next day with nothing
    flagged. `row_flags` reads the same two fields, so the flag and the
    derivation agree by construction.
    """
    return (row.get("start_time_local") or row.get("start_time_utc") or "")[:10]


def sport_family(type_key: str | None) -> str:
    """The family a Garmin activity type belongs to: cycling, running, swimming, other.

    Matching is on substrings of the raw key, so a type Garmin adds later
    ("gravel_cycling", "e_bike_fitness") lands in the right family without a
    release here. The exact key is kept as `sub_sport` — the family is for
    filtering, not for forgetting what the device said.
    """
    if not type_key:
        return "other"
    key = str(type_key).strip().lower()
    for markers, family in (
        (_SWIMMING_MARKERS, "swimming"),
        (_CYCLING_MARKERS, "cycling"),
        (_RUNNING_MARKERS, "running"),
    ):
        if any(marker in key for marker in markers):
            return family
    return "other"


def _type_key(item: dict) -> str | None:
    for holder in ("activityType", "activityTypeDTO", "type"):
        value = item.get(holder)
        if isinstance(value, dict):
            for key in ("typeKey", "type_key", "key"):
                if value.get(key):
                    return str(value[key])
        elif isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("activityType", "sportType", "sport", "typeKey"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def normalize_activity(item: dict) -> tuple[dict | None, str | None]:
    """One Garmin activity as a row for the `activities` table.

    Returns (row, None) or (None, reason). A reason is a sentence naming what
    was missing, because "3 rejected" tells nobody what to fix.

    **On dates.** Two are stored. `start_time_utc` comes from `startTimeGMT`
    and orders rides unambiguously. `local_date` comes from the date half of
    `startTimeLocal` — the day the athlete believes they trained — and is what
    every plan comparison keys on. They disagree more often than seems likely:
    an 07:00 ride in UTC-5 is 12:00 UTC the same day, but a 22:00 one is 03:00
    UTC *the next*, so scheduling against UTC moves an evening session onto
    Wednesday's plan. When `startTimeLocal` is absent the UTC date is used and
    the row is flagged `local_date_from_utc`, which is a fact worth reporting
    rather than a detail worth hiding.
    """
    values: dict[str, Any] = {}
    for field, keys in _ALIASES.items():
        values[field] = _pick(item, keys)

    activity_id = _activity_id(values["garmin_activity_id"])
    if activity_id is None:
        return None, (
            "no activityId — every field but the dedupe key can be missing, this one "
            "cannot, or a re-import would duplicate the ride instead of updating it"
        )

    local = _timestamp(values["start_time_local"])
    utc = _timestamp(values["start_time_utc"], to_utc=True)
    if local is None and utc is None:
        return None, f"activity {activity_id} has no readable start time (startTimeLocal/GMT)"

    type_key = _type_key(item)
    row: dict[str, Any] = {
        "garmin_activity_id": activity_id,
        "name": (str(values["name"]).strip() or None) if values["name"] is not None else None,
        # Null when the payload carried no type at all, rather than the "other"
        # that sport_family would derive from nothing. The import rule that a
        # null never overwrites a stored value only protects fields that are
        # genuinely absent — a derived "other" is not absent, and re-importing
        # a thinner payload would quietly reclassify a bike ride as unknown.
        "sport": sport_family(type_key) if type_key else None,
        "sub_sport": type_key,
        "start_time_utc": utc,
        "start_time_local": local,
        "source": "garmin",
    }
    for field in (
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
    ):
        number = _number(values[field])
        if number is not None and field in _INT_FIELDS:
            row[field] = round(number)
        else:
            row[field] = number

    row["local_date"] = local_date_of(row)
    problem = implausible_date(row["local_date"])
    if problem:
        return None, f"activity {activity_id} {problem}"
    return row, None


def row_flags(row: dict) -> list[str]:
    """What is worth knowing about a stored row's data quality.

    Derived from **the row**, not from the payload that produced it. That
    distinction is the whole point: `get_activities` returns a thinner summary
    than `get_activity`, so flagging the payload would stamp
    `no_normalized_power` onto a ride whose NP is sitting in the database from
    an earlier detailed fetch — the same mistake the null-preserving merge
    exists to prevent, one column over.
    """
    flags: list[str] = []
    if not row.get("start_time_local"):
        # The plan date came from UTC, which is a different day for an
        # early-morning or late-evening ride.
        flags.append("local_date_from_utc")
    if not row.get("sub_sport"):
        flags.append("no_sport_type")
    if row.get("duration_s") is None:
        flags.append("no_duration")
    if row.get("avg_power") is None and row.get("normalized_power") is None:
        flags.append("no_power")
    elif row.get("normalized_power") is None:
        # Common on a ride recorded without a power meter connected to the head
        # unit's NP field, and on some third-party uploads. TSS falls back to
        # average power, which understates a variable ride.
        flags.append("no_normalized_power")
    return flags


def normalize_lap(item: dict, index: int) -> dict:
    """One lap as a row for `activity_laps`. `index` is 1-based execution order.

    Laps are never rejected: a lap with no power still pins the duration of a
    block, which is half of what compliance asks.
    """
    row: dict[str, Any] = {"lap_index": index}
    for field, keys in _LAP_ALIASES.items():
        number = _number(_pick(item, keys))
        if number is not None and field in _INT_FIELDS:
            row[field] = round(number)
        else:
            row[field] = number
    return row

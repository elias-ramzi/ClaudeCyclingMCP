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
import re
from datetime import datetime, timedelta, timezone
from typing import Any

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

# A trailing UTC offset, colon or no colon. `strftime("%z")` writes "+0200",
# which `fromisoformat` refuses before 3.11 — and which a fallback that strips
# it turns into an instant two hours from the truth. A date cannot match this:
# "2026-08-20" has no two digits left after the sign and the day.
_OFFSET = re.compile(r"([+-])(\d{2}):?(\d{2})$")


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
        seconds = float(value) / 1000.0
        if seconds < 10**8:
            return None
        return (datetime(1970, 1, 1) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")
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
    iso = _OFFSET.sub(r"\1\2:\3", iso)
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
    if parsed is None:
        parsed = _from_patterns(iso)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc) if to_utc else parsed.replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _from_patterns(iso: str) -> datetime | None:
    """The shapes `fromisoformat` will not take, with any UTC offset preserved.

    The offset is split off and reattached rather than truncated away. A
    fallback that silently drops "+02:00" answers with a real-looking instant
    two hours from the truth, and nothing downstream can tell — which is worse
    than this function returning None and the activity being rejected.
    """
    offset = None
    match = _OFFSET.search(iso)
    if match is not None:
        iso = iso[: match.start()]
        sign = -1 if match.group(1) == "-" else 1
        offset = timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
    body = iso.replace("T", " ").split(".")[0].strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(body, pattern)
        except ValueError:
            continue
        return parsed if offset is None else parsed.replace(tzinfo=offset)
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

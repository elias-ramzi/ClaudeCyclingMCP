"""Reading Garmin's own payload shapes, warts included.

Ingestion is model-mediated: the JSON arrives exactly as the Garmin MCP emitted
it. So the shapes here are the shapes the tool has to survive, and the field
names are Garmin's, not a tidied version of them.
"""

from __future__ import annotations

import json

import pytest

from cycling_mcp.garmin_import import (
    GarminPayloadError,
    as_activity_list,
    as_lap_list,
    normalize_activity,
    normalize_lap,
    sport_family,
)

RIDE = {
    "activityId": 1662651131,
    "activityName": "Sweet spot 3x10",
    "activityType": {"typeId": 10, "typeKey": "virtual_ride", "parentTypeId": 2},
    "startTimeLocal": "2026-07-05 07:00:00",
    "startTimeGMT": "2026-07-05 05:00:00",
    "duration": 4200.0,
    "movingDuration": 4180.0,
    "distance": 42000.0,
    "elevationGain": 120.0,
    "averageHR": 150,
    "maxHR": 172,
    "avgPower": 190.0,
    "maxPower": 320.0,
    "normPower": 198.0,
    "calories": 780.0,
}


# --------------------------------------------------------------------------
# shapes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        [RIDE],
        RIDE,
        {"activities": [RIDE]},
        {"activityList": [RIDE]},
        {"results": [RIDE]},
        json.dumps([RIDE]),
        json.dumps(RIDE),
    ],
)
def test_every_shape_the_garmin_mcp_emits_is_read(payload):
    assert as_activity_list(payload) == [RIDE]


def test_a_payload_with_no_activities_says_what_it_held():
    with pytest.raises(GarminPayloadError) as exc:
        as_activity_list([1662651131, 1662651132])
    assert "list of ids" in str(exc.value)


def test_prose_instead_of_json_is_refused_with_the_parse_position():
    with pytest.raises(GarminPayloadError) as exc:
        as_activity_list("the ride from Tuesday")
    assert "not JSON" in str(exc.value)


def test_split_summaries_are_refused_and_name_the_right_call():
    """They aggregate by split type, not by lap, so they do not align with a plan."""
    with pytest.raises(GarminPayloadError) as exc:
        as_lap_list({"splitSummaries": [{"splitType": "CLIMB", "duration": 600}]})
    assert "get_activity_splits" in str(exc.value)


def test_lap_dtos_are_read_from_the_wrapper_garmin_uses():
    laps = as_lap_list({"lapDTOs": [{"duration": 600.0}, {"duration": 300.0}]})
    assert len(laps) == 2


# --------------------------------------------------------------------------
# field mapping
# --------------------------------------------------------------------------


def test_a_whole_ride_maps_onto_the_stored_row():
    row, reason = normalize_activity(RIDE)
    assert reason is None
    assert row["garmin_activity_id"] == "1662651131"
    assert row["sport"] == "cycling"
    assert row["sub_sport"] == "virtual_ride"
    assert row["local_date"] == "2026-07-05"
    assert row["start_time_utc"] == "2026-07-05T05:00:00"
    assert row["duration_s"] == 4200.0
    assert row["normalized_power"] == 198.0
    assert row["avg_hr"] == 150


def test_the_activity_id_is_text_so_it_survives_json():
    """A 10-digit id that comes back as 1.662651131e9 would stop matching itself.

    The ride would then be inserted again on the next import rather than
    recognised, and the duplicate would double its training load.
    """
    row, _ = normalize_activity({**RIDE, "activityId": 1662651131.0})
    assert row["garmin_activity_id"] == "1662651131"


def test_the_detailed_fetch_shape_is_read_from_summary_dto():
    """`get_activity` buries the same numbers one level down."""
    detailed = {
        "activityId": 42,
        "activityName": "Endurance",
        "activityTypeDTO": {"typeKey": "road_biking"},
        "summaryDTO": {
            "startTimeLocal": "2026-07-06T09:00:00.0",
            "startTimeGMT": "2026-07-06T07:00:00.0",
            "duration": 7200.0,
            "averagePower": 175.0,
            "normalizedPower": 182.0,
            "averageHR": 138,
        },
    }
    row, reason = normalize_activity(detailed)
    assert reason is None
    assert row["duration_s"] == 7200.0
    assert row["normalized_power"] == 182.0
    assert row["sport"] == "cycling" and row["sub_sport"] == "road_biking"


def test_unknown_keys_are_carried_rather_than_rejected():
    """Garmin adds fields without warning; failing on one fails every ride."""
    row, reason = normalize_activity({**RIDE, "someFieldAddedIn2027": {"nested": True}})
    assert reason is None and row["garmin_activity_id"] == "1662651131"


def test_an_activity_without_an_id_is_rejected_with_the_reason():
    row, reason = normalize_activity({k: v for k, v in RIDE.items() if k != "activityId"})
    assert row is None
    assert "no activityId" in reason


def test_an_activity_without_a_start_time_is_rejected():
    row, reason = normalize_activity({"activityId": 7})
    assert row is None
    assert "no readable start time" in reason


def test_a_missing_local_time_falls_back_to_utc_and_flags_it():
    """The UTC date is a different day for a late-evening or pre-dawn ride."""
    from cycling_mcp.garmin_import import row_flags

    row, _ = normalize_activity({k: v for k, v in RIDE.items() if k != "startTimeLocal"})
    assert row["local_date"] == "2026-07-05"
    assert "local_date_from_utc" in row_flags(row)


def test_the_local_date_wins_over_utc_when_they_disagree():
    """22:00 local in UTC-5 is 03:00 the next day in UTC. The plan says today."""
    row, _ = normalize_activity(
        {**RIDE, "startTimeLocal": "2026-07-05 22:00:00", "startTimeGMT": "2026-07-06 03:00:00"}
    )
    assert row["local_date"] == "2026-07-05"
    assert row["start_time_utc"] == "2026-07-06T03:00:00"


def test_epoch_milliseconds_are_read_as_a_timestamp():
    row, _ = normalize_activity(
        {"activityId": 9, "startTimeGMT": 1783236600000, "activityType": {"typeKey": "cycling"}}
    )
    assert row["start_time_utc"].startswith("2026-")


def test_a_ride_with_no_power_is_flagged_not_rejected():
    from cycling_mcp.garmin_import import row_flags

    row, reason = normalize_activity(
        {k: v for k, v in RIDE.items() if k not in ("avgPower", "maxPower", "normPower")}
    )
    assert reason is None
    assert "no_power" in row_flags(row)


def test_average_power_without_np_is_flagged_separately():
    from cycling_mcp.garmin_import import row_flags

    row, _ = normalize_activity({k: v for k, v in RIDE.items() if k != "normPower"})
    flags = row_flags(row)
    assert "no_normalized_power" in flags
    assert "no_power" not in flags


# --- round-9 rework: row_flags reads through the same placeholder rule the
# read surfaces apply, so the flags and the nulled projection in one response
# cannot disagree about the same row ---


def test_a_placeholder_power_is_flagged_no_power_not_missing_np():
    """avg_power -50 with normPower 0 is no measurement at all: before the
    placeholder rule reached row_flags, this row said `no_normalized_power`
    ("average power is usable") while the projection beside it read null."""
    from cycling_mcp.garmin_import import row_flags

    row, reason = normalize_activity({**RIDE, "avgPower": -50.0, "normPower": 0})
    assert reason is None
    flags = row_flags(row)
    assert "no_power" in flags
    assert "no_normalized_power" not in flags


def test_a_barely_positive_power_is_not_flagged():
    """Just outside the guard: 1 W is a measurement, however small."""
    from cycling_mcp.garmin_import import row_flags

    row, _ = normalize_activity({**RIDE, "avgPower": 1, "normPower": 1})
    flags = row_flags(row)
    assert "no_power" not in flags
    assert "no_normalized_power" not in flags


def test_a_zero_duration_is_flagged_no_duration():
    from cycling_mcp.garmin_import import row_flags

    row, _ = normalize_activity({**RIDE, "duration": 0})
    assert "no_duration" in row_flags(row)


def test_import_response_flags_agree_with_its_own_nulled_fields(db):
    """One response, one story: the entry whose power fields the projection
    nulls must carry `no_power`, not a clean flag list beside four nulls."""
    from cycling_mcp import coach

    result = coach.import_activities(
        [{**RIDE, "activityId": 6021, "avgPower": -50.0, "normPower": 0, "averageHR": 0}]
    )
    entry = result["activities"]["inserted"][0]
    assert entry["avg_power"] is None
    assert entry["normalized_power"] is None
    assert "no_power" in entry["flags"]


# --------------------------------------------------------------------------
# sport families
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "type_key",
    [
        "cycling",
        "virtual_ride",
        "indoor_cycling",
        "road_biking",
        "gravel_cycling",
        "mountain_biking",
    ],
)
def test_every_way_garmin_says_bike_lands_in_cycling(type_key):
    """Filtering on typeKey == "cycling" would drop a whole winter of training."""
    assert sport_family(type_key) == "cycling"


@pytest.mark.parametrize("type_key", ["running", "treadmill_running", "trail_running"])
def test_running_variants_land_in_running(type_key):
    assert sport_family(type_key) == "running"


def test_an_unknown_sport_is_other_not_a_crash():
    assert sport_family("underwater_basket_weaving") == "other"
    assert sport_family(None) == "other"


def test_laps_are_never_rejected_only_thinned():
    """A lap with no power still pins a block's duration."""
    row = normalize_lap({"duration": 600.0}, 3)
    assert row["lap_index"] == 3 and row["duration_s"] == 600.0
    assert row["avg_power"] is None


def test_a_bike_lap_reads_its_cadence_field():
    row = normalize_lap({"duration": 600.0, "averageBikingCadenceInRevPerMinute": 92.0}, 1)
    assert row["avg_cadence"] == 92.0


# --------------------------------------------------------------------------
# ISO-8601 with a UTC offset — found in code review
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-08-20T07:12:33+02:00", "2026-08-20T07:12:33"),
        ("2026-08-20T07:12:33.0+02:00", "2026-08-20T07:12:33"),
        ("2026-08-20T07:12:33-05:00", "2026-08-20T07:12:33"),
        ("2026-08-20T07:12:33Z", "2026-08-20T07:12:33"),
        ("2026-08-20 07:12:33", "2026-08-20T07:12:33"),
        ("2026-08-20", "2026-08-20T00:00:00"),
    ],
)
def test_a_timestamp_with_an_offset_is_read_rather_than_dropped(value, expected):
    """Stripping fractional seconds left "+02:00" attached and every pattern
    failed, so both start times came back None and the whole ride was rejected
    as having no readable start — a ride lost to a timezone suffix."""
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp(value) == expected


def test_a_local_time_with_an_offset_keeps_its_wall_clock():
    """The point of a local start time is the day the athlete believes they
    rode; converting it to UTC would move an evening ride onto the next day."""
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp("2026-08-20T22:00:00+02:00") == "2026-08-20T22:00:00"


def test_a_gmt_time_with_an_offset_is_converted_to_the_instant():
    """`startTimeGMT` names an instant, so an offset on it means something."""
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp("2026-08-20T07:12:33+02:00", to_utc=True) == "2026-08-20T05:12:33"
    assert _timestamp("2026-08-20 07:12:33", to_utc=True) == "2026-08-20T07:12:33"


def test_an_activity_timed_only_in_offset_form_is_imported():
    row, reason = normalize_activity(
        {
            "activityId": 77,
            "activityType": {"typeKey": "virtual_ride"},
            "startTimeLocal": "2026-08-20T22:00:00+02:00",
            "startTimeGMT": "2026-08-20T20:00:00+00:00",
            "duration": 3600.0,
        }
    )
    assert reason is None
    assert row["local_date"] == "2026-08-20"
    assert row["start_time_utc"] == "2026-08-20T20:00:00"


def test_the_three_numeric_coercers_stay_different_on_purpose():
    """They look alike and are not interchangeable.

    Folding them together would pick one behaviour for all three: either a
    scraped "1,234" becomes 1.234, or a Garmin export written under a European
    locale loses its decimals, or a string in an API payload stops being the
    shape error it is.
    """
    from cycling_mcp.garmin_import import _number as from_garmin
    from cycling_mcp.verify import _as_number as from_page
    from cycling_mcp.verify import _number as from_payload

    assert from_garmin("232,5") == 232.5, "a European-locale export"
    assert from_page("1,234") is None, "a scraped thousands separator is not 1.234"
    assert from_payload("72.0") is None, "a string in a DTO is a shape error"
    assert from_page("72.0") == 72.0
    assert (from_garmin(72), from_page(72), from_payload(72)) == (72.0, 72.0, 72.0)


@pytest.mark.parametrize(
    "value,expected_utc",
    [
        ("2026-08-20T23:12:33.5+02:00", "2026-08-20T21:12:33"),
        ("2026-08-20T23:12:33.12345+02:00", "2026-08-20T21:12:33"),
        ("2026-08-20T23:12:33.500+02:00", "2026-08-20T21:12:33"),
        ("2026-08-20T23:12:33.500000+02:00", "2026-08-20T21:12:33"),
        ("2026-08-20T23:12:33.5Z", "2026-08-20T23:12:33"),
    ],
)
def test_an_odd_fractional_second_does_not_cost_the_utc_offset(value, expected_utc):
    """Python 3.10's fromisoformat wants exactly 3 or 6 fractional digits.

    `.5` fell through to the strptime fallback, which truncates at the dot and
    took the "+02:00" with it — storing the instant two hours wrong, with
    nothing to show for it. Wrong and plausible is worse than rejected, and
    3.10 is this package's declared floor, so it only ever failed there.
    """
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp(value, to_utc=True) == expected_utc
    assert _timestamp(value) == "2026-08-20T23:12:33"


def test_the_local_date_is_derived_from_the_row_not_carried():
    """One source of truth, so a stored date and the times beside it cannot
    disagree — which is what let a re-import move a ride to the next day."""
    from cycling_mcp.garmin_import import local_date_of

    assert (
        local_date_of(
            {"start_time_local": "2026-07-07T22:00:00", "start_time_utc": "2026-07-08T03:00:00"}
        )
        == "2026-07-07"
    )
    assert local_date_of({"start_time_utc": "2026-07-08T03:00:00"}) == "2026-07-08"
    assert local_date_of({}) == ""


# --------------------------------------------------------------------------
# review round 3 — the third 3.10-only shape of the same fallback
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        # What strftime("%z") writes. `fromisoformat` refuses it before 3.11.
        ("2026-08-20T23:12:33.5+0200", "2026-08-20T21:12:33"),
        ("2026-08-20T23:12:33+0200", "2026-08-20T21:12:33"),
        ("2026-08-20T23:12:33.5-0500", "2026-08-21T04:12:33"),
        ("2026-08-20T23:12:33.5+0000", "2026-08-20T23:12:33"),
        # Shapes fromisoformat rejects on every version, so the pattern
        # fallback runs — and must apply the offset rather than truncate it.
        ("2026-08-20 23:12:33 +02:00", "2026-08-20T21:12:33"),
        ("2026-08-20 23:12:33 +0200", "2026-08-20T21:12:33"),
    ],
)
def test_a_colon_less_offset_is_applied_not_discarded(value, expected):
    """`.split(".")[0]` took the offset with the fraction, so an instant came
    back hours from the truth — wrong, plausible, and invisible downstream.
    Patched one shape at a time across three rounds; normalised once now."""
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp(value, to_utc=True) == expected


def test_a_colon_less_offset_on_a_local_time_still_keeps_the_wall_clock():
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp("2026-08-20T23:12:33.5+0200") == "2026-08-20T23:12:33"


def test_a_plain_date_is_not_mistaken_for_an_offset():
    """ "2026-08-20" ends in "-08-20"; a greedier offset pattern would eat it."""
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp("2026-08-20") == "2026-08-20T00:00:00"
    assert _timestamp("2026-08-20 07:12") == "2026-08-20T07:12:00"


def test_an_unreadable_timestamp_is_still_refused():
    """The fallback exists to read more shapes, not to invent an instant."""
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp("last Tuesday") is None
    assert _timestamp("2026-08-20T25:12:33+02:00") is None


def test_the_fallback_never_drops_a_utc_offset(monkeypatch):
    """The offset shapes above all reach `fromisoformat` on 3.11+, so this pins
    the path 3.10 — the declared floor — actually takes: the pattern fallback,
    which used to truncate at the dot and take the offset with it. Simulated by
    making `fromisoformat` refuse everything, which is what 3.10 does to a
    colon-less offset and to a one-digit fraction.
    """
    from datetime import datetime as real_datetime

    from cycling_mcp import garmin_import

    class Strict(real_datetime):
        @classmethod
        def fromisoformat(cls, value):
            raise ValueError("simulating Python 3.10")

    monkeypatch.setattr(garmin_import, "datetime", Strict)
    assert garmin_import._timestamp("2026-08-20T23:12:33.5+0200", to_utc=True) == (
        "2026-08-20T21:12:33"
    )
    assert garmin_import._timestamp("2026-08-20T23:12:33.5+02:00", to_utc=True) == (
        "2026-08-20T21:12:33"
    )
    assert garmin_import._timestamp("2026-08-20T23:12:33.5+0200") == "2026-08-20T23:12:33"
    assert garmin_import._timestamp("2026-08-20T23:12:33Z", to_utc=True) == "2026-08-20T23:12:33"
    assert garmin_import._timestamp("2026-08-20") == "2026-08-20T00:00:00"


# --------------------------------------------------------------------------
# review round 4 — the offset class, closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "0001-01-01T00:00:00+0200",  # the zero-date sentinel some exporters write
        "0001-01-01T00:00:00.5+02:00",
        "9999-12-31T23:59:59-0500",
        10**20,  # epoch milliseconds far outside any representable date
    ],
)
def test_an_unrepresentable_instant_is_refused_not_raised(value):
    """`astimezone` raises OverflowError at the edges of the range. Import
    handles a bad row by rejecting it with a reason; an exception aborts the
    whole call instead and takes every valid ride in the batch with it."""
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp(value, to_utc=True) is None


def test_a_sentinel_timestamp_is_a_rejection_reason_not_an_exception():
    """`normalize_activity` reports a bad row; it never raises. The offset
    conversion did, which killed the caller's whole batch."""
    from cycling_mcp.garmin_import import normalize_activity

    row, reason = normalize_activity(
        {"activityId": 4242, "startTimeGMT": "0001-01-01T00:00:00+0200", "duration": 3600.0}
    )
    assert row is None
    assert "start time" in reason


def test_a_sentinel_local_time_is_refused_even_though_it_reads_cleanly():
    """Round 4 caught the sentinels whose UTC conversion overflowed. The local
    wall clock needs no conversion, so it read fine and stored a ride in year 1
    — convertible and plausible are different questions, and this one is
    answered by the date band rather than by an exception."""
    from cycling_mcp.garmin_import import normalize_activity

    row, reason = normalize_activity(
        {
            "activityId": 4243,
            "startTimeGMT": "0001-01-01T00:00:00+0200",
            "startTimeLocal": "0001-01-01T00:00:00+0200",
            "duration": 3600.0,
        }
    )
    assert row is None
    assert "before 1990-01-01" in reason
    assert "zero-date sentinel" in reason


@pytest.mark.parametrize(
    "value,to_utc,expected",
    [
        ("2026-08-20T23:12:33.5+02", True, "2026-08-20T21:12:33"),
        ("2026-08-20T23:12:33+02", True, "2026-08-20T21:12:33"),
        ("2026-08-20T23:12:33.5+02", False, "2026-08-20T23:12:33"),
        ("2026-08-20T23:12:33-05", True, "2026-08-21T04:12:33"),
    ],
)
def test_an_hour_only_offset_is_applied(value, to_utc, expected):
    """The fourth spelling of the same offset, found one at a time across four
    rounds: "+02" is legal ISO-8601 and `_OFFSET` wanted four digits."""
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp(value, to_utc=to_utc) == expected


def test_the_hour_only_form_does_not_turn_a_date_into_an_offset():
    """Making the minutes optional lets the pattern match inside "2026-08-20",
    whose tail is "-20". `_split_offset` requires a time in front of it."""
    from cycling_mcp.garmin_import import _split_offset, _timestamp

    split = _split_offset("2026-08-20")
    assert (split.body, split.offset, split.tz) == ("2026-08-20", None, None)
    assert _timestamp("2026-08-20", to_utc=True) == "2026-08-20T00:00:00"
    assert _timestamp("2026-08-20") == "2026-08-20T00:00:00"


def _strict_fromisoformat(monkeypatch):
    """Make `fromisoformat` refuse everything, which is what 3.10 does to the
    offset and fraction shapes below. The declared floor is the only version
    that reaches the pattern fallback for these."""
    from datetime import datetime as real_datetime

    from cycling_mcp import garmin_import

    class Strict(real_datetime):
        @classmethod
        def fromisoformat(cls, value):
            raise ValueError("simulating Python 3.10")

    monkeypatch.setattr(garmin_import, "datetime", Strict)
    return garmin_import


def test_the_fallback_applies_an_hour_only_offset(monkeypatch):
    garmin_import = _strict_fromisoformat(monkeypatch)
    assert garmin_import._timestamp("2026-08-20T23:12:33.5+02", to_utc=True) == (
        "2026-08-20T21:12:33"
    )
    assert garmin_import._timestamp("2026-08-20T23:12:33.5+02") == "2026-08-20T23:12:33"


def test_the_fallback_rejects_a_trailing_offset_it_cannot_apply(monkeypatch):
    """The class fix: rather than truncating at the dot and silently dropping
    whatever followed, anything still attached is a rejection. Worst case one
    row is refused with a reason, which import_activities already reports."""
    garmin_import = _strict_fromisoformat(monkeypatch)
    for value in (
        "2026-08-20T23:12:33.5+2:00",  # one-digit hour: not an offset this reads
        "2026-08-20T23:12:33.500x",
        "2026-08-20T23:12:33+99:00",  # 99 hours is not a UTC offset
    ):
        assert garmin_import._timestamp(value, to_utc=True) is None, value


def test_the_fallback_still_reads_the_shapes_it_always_did(monkeypatch):
    garmin_import = _strict_fromisoformat(monkeypatch)
    assert garmin_import._timestamp("2026-08-20 23:12:33") == "2026-08-20T23:12:33"
    assert garmin_import._timestamp("2026-08-20") == "2026-08-20T00:00:00"
    assert garmin_import._timestamp("2026-08-20T23:12:33Z", to_utc=True) == "2026-08-20T23:12:33"
    assert garmin_import._timestamp("2026-08-20T23:12:33.5+0200", to_utc=True) == (
        "2026-08-20T21:12:33"
    )


def test_the_lap_columns_and_the_lap_aliases_stay_in_step():
    """The INSERT writes `row.get(field)`, so a key drifted between these two
    lists stores NULL with no test failing — drop `avg_power` and every block
    of every session compares as `no_power`. The activities pair has the same
    pinning test one table over."""
    from cycling_mcp import coach
    from cycling_mcp.garmin_import import _LAP_ALIASES

    assert set(coach._LAP_OUT_FIELDS) == {"lap_index"} | set(_LAP_ALIASES)


# --------------------------------------------------------------------------
# review round 5 — the boundaries of the round 4 fixes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [10**400, -(10**400), 10**20, float("nan"), float("inf")])
def test_a_number_too_big_to_be_a_float_is_refused_not_raised(value):
    """The round-4 guard started one line below `float(value)`, and JSON
    integers are arbitrary-precision: `float(10**400)` raised OverflowError
    outside the try and took the whole import call with it."""
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp(value, to_utc=True) is None
    assert _timestamp(value) is None


def test_an_oversized_epoch_is_a_rejection_reason_not_an_exception():
    from cycling_mcp.garmin_import import normalize_activity

    row, reason = normalize_activity(
        {"activityId": 5150, "startTimeGMT": 10**400, "startTimeLocal": 10**400}
    )
    assert row is None
    assert "no readable start time" in reason


@pytest.mark.parametrize(
    "value",
    ["2026-08-20-05:00", "2026-08-20+02:00", "2026-08-20+02", "2026-08-20+0200"],
)
def test_a_bare_date_with_an_offset_is_refused_on_the_native_path(value):
    """`fromisoformat` on 3.11+ takes any separator, so it read the offset sign
    as the date/time separator and the offset digits as a clock: a fabricated
    time of day out of a timezone, naive, with `to_utc` doing nothing. On 3.10
    the same input rejected, so the corruption was invisible on half the
    matrix — hence the pair of tests."""
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp(value, to_utc=True) is None
    assert _timestamp(value) is None


@pytest.mark.parametrize(
    "value",
    ["2026-08-20-05:00", "2026-08-20+02:00", "2026-08-20+02", "2026-08-20+0200"],
)
def test_a_bare_date_with_an_offset_is_refused_on_the_fallback_path(value, monkeypatch):
    garmin_import = _strict_fromisoformat(monkeypatch)
    assert garmin_import._timestamp(value, to_utc=True) is None
    assert garmin_import._timestamp(value) is None


def test_a_bare_date_still_reads_on_both_paths(monkeypatch):
    """The offset pattern matches inside every date — "2026-08-20" ends in
    "-20" — so the rejection above must not swallow the date itself."""
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp("2026-08-20", to_utc=True) == "2026-08-20T00:00:00"
    garmin_import = _strict_fromisoformat(monkeypatch)
    assert garmin_import._timestamp("2026-08-20", to_utc=True) == "2026-08-20T00:00:00"


@pytest.mark.parametrize(
    "start_local,start_gmt",
    [
        ("0001-01-01T00:00:00-05:00", None),  # converts away from the underflow
        ("0001-01-01 00:00:00", None),  # naive, never converted at all
        (None, "0001-01-01T00:00:00-05:00"),
        ("1899-12-31 00:00:00", None),
    ],
)
def test_a_sentinel_date_is_refused_however_it_reads(start_local, start_gmt):
    """Round 4 caught only the sentinels whose conversion overflowed. These
    convert perfectly well and stored a ride in year 1, which then made
    `get_form` walk two thousand years to reach the real training."""
    from cycling_mcp.garmin_import import normalize_activity

    item = {"activityId": 5151, "duration": 3600.0}
    if start_local:
        item["startTimeLocal"] = start_local
    if start_gmt:
        item["startTimeGMT"] = start_gmt
    row, reason = normalize_activity(item)
    assert row is None
    assert "before 1990-01-01" in reason


def test_a_ride_dated_in_the_future_is_refused():
    from cycling_mcp.garmin_import import normalize_activity

    row, reason = normalize_activity(
        {"activityId": 5152, "startTimeLocal": "2099-01-01 08:00:00", "duration": 3600.0}
    )
    assert row is None
    assert "in the future" in reason


def test_today_and_tomorrow_are_still_plausible():
    """The ceiling carries slop for a device in a timezone ahead of the server;
    a guard that rejected today's ride would be worse than the bug."""
    from datetime import date, timedelta

    from cycling_mcp.garmin_import import implausible_date

    today = date(2026, 8, 24)
    assert implausible_date(today.isoformat(), today=today) is None
    assert implausible_date((today + timedelta(days=1)).isoformat(), today=today) is None
    assert implausible_date("1990-01-01", today=today) is None
    assert implausible_date("1989-12-31", today=today) is not None


@pytest.mark.parametrize("value", ["2026-08-20T+02:00", "2026-08-20T+0200", "2026-08-20T-05:00"])
def test_an_offset_with_no_clock_between_is_refused(value):
    """The spelling that reaches the pattern loop rather than the guard in
    `_split_offset`: reading it as midnight in that zone invents the same time
    of day out of the same digits, one branch over."""
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp(value, to_utc=True) is None
    assert _timestamp(value) is None


def test_an_offset_with_no_clock_is_refused_on_the_fallback_path_too(monkeypatch):
    garmin_import = _strict_fromisoformat(monkeypatch)
    assert garmin_import._timestamp("2026-08-20T+02:00", to_utc=True) is None
    assert garmin_import._timestamp("2026-08-20T00:00:00+02:00", to_utc=True) == (
        "2026-08-19T22:00:00"
    ), "a real midnight with an offset is still an instant"


def test_a_year_below_1000_still_writes_a_four_digit_year():
    """`strftime("%Y")` delegates the year to the platform's C library: glibc
    writes year 1 as "1" where macOS and Windows write "0001". The sentinel
    then read back as "1-01-01T00:00:00", `local_date` sliced its first ten
    characters into "1-01-01T00", and the date band could not judge it — so it
    was refused as unreadable rather than as the sentinel it is. Green locally,
    red on CI, for two rounds.
    """
    from datetime import datetime

    from cycling_mcp.garmin_import import _iso, _timestamp, local_date_of

    assert _iso(datetime(1, 1, 1)) == "0001-01-01T00:00:00"
    assert _iso(datetime(999, 12, 31, 23, 59, 59)) == "0999-12-31T23:59:59"
    assert _timestamp("0001-01-01 00:00:00") == "0001-01-01T00:00:00"
    assert local_date_of({"start_time_local": _timestamp("0001-01-01 00:00:00")}) == "0001-01-01"


def test_a_converted_instant_is_stored_naive():
    """`isoformat` writes the offset that `strftime("%Y-%m-%dT%H:%M:%S")` used
    to drop, so the tzinfo has to come off explicitly rather than by omission."""
    from cycling_mcp.garmin_import import _timestamp

    assert _timestamp("2026-08-20T23:12:33+02:00", to_utc=True) == "2026-08-20T21:12:33"
    assert _timestamp("2026-08-20T23:12:33+02:00") == "2026-08-20T23:12:33"
    assert _timestamp("2026-08-20T23:12:33Z", to_utc=True) == "2026-08-20T23:12:33"


# --------------------------------------------------------------------------
# review round 6 — plausibility is judged per timestamp, not per row
# --------------------------------------------------------------------------


def test_a_sentinel_local_time_beside_a_real_gmt_falls_back_rather_than_rejecting():
    """The whole row used to be rejected even though the UTC fallback exists
    for exactly this case — every ride from a device that always sends a
    zero-date local time was lost."""
    row, reason = normalize_activity(
        {
            **RIDE,
            "activityId": 4300,
            "startTimeLocal": "0001-01-01T00:00:00",
            "startTimeGMT": "2026-07-05 05:00:00",
        }
    )
    assert reason is None
    assert row["local_date"] == "2026-07-05"
    assert row["start_time_utc"] == "2026-07-05T05:00:00"
    assert row["start_time_local"] is None
    from cycling_mcp.garmin_import import row_flags

    assert "local_date_from_utc" in row_flags(row)
    # The rest of the payload is untouched by the timestamp rejection.
    assert row["avg_power"] == 190.0
    assert row["normalized_power"] == 198.0


def test_a_sentinel_gmt_beside_a_real_local_time_nulls_only_the_utc_side():
    """A sentinel `start_time_utc` used to store unflagged, and that column is
    the ordering tiebreak in list_activities/link_activity/get_week."""
    from cycling_mcp.garmin_import import row_flags

    row, reason = normalize_activity(
        {
            **RIDE,
            "activityId": 4301,
            "startTimeLocal": "2026-07-05 07:00:00",
            "startTimeGMT": "0001-01-01T00:00:00",
        }
    )
    assert reason is None
    assert row["start_time_utc"] is None
    assert row["local_date"] == "2026-07-05"
    assert row["start_time_local"] == "2026-07-05T07:00:00"
    assert "no_utc_time" in row_flags(row)


def test_both_timestamps_sentinel_is_still_a_rejection():
    row, reason = normalize_activity(
        {
            "activityId": 4302,
            "startTimeLocal": "0001-01-01T00:00:00",
            "startTimeGMT": "0001-01-01T00:00:00",
            "duration": 3600.0,
        }
    )
    assert row is None
    assert "activity 4302" in reason


def test_a_local_date_of_exactly_the_floor_is_stored_as_is():
    row, reason = normalize_activity(
        {**RIDE, "activityId": 4303, "startTimeLocal": "1990-01-01 07:00:00"}
    )
    assert reason is None
    assert row["local_date"] == "1990-01-01"


def test_one_day_before_the_floor_is_implausible():
    row, reason = normalize_activity(
        {"activityId": 4304, "startTimeLocal": "1989-12-31 07:00:00", "duration": 3600.0}
    )
    assert row is None
    assert "before 1990-01-01" in reason


# --------------------------------------------------------------------------
# review round 8 — non-finite numeric fields (finding 2)
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A coach DB in a temp directory, for the tests below that go through
    `coach.import_activities` rather than `normalize_activity` directly — that
    is the level at which "the batch survives" is actually observable."""
    from cycling_mcp import store

    monkeypatch.setenv(store.ENV_DB_PATH, str(tmp_path / "coach.db"))
    return tmp_path / "coach.db"


def test_an_infinite_calories_value_rejects_the_row_and_names_the_field(db):
    """`json.loads('{"calories": 1e999}')` parses cleanly to `float('inf')` —
    no exception at parse time, so `import_activities` used to report
    `ok: true` and store `calories=inf`. Every downstream sum (nutrition's
    measured kcal, suggest_targets' exercise figure) became inf from there.
    The row must be rejected instead, by name, and the valid ride beside it
    must still import."""
    from cycling_mcp import coach

    bad_json = json.dumps({**RIDE, "activityId": 6001, "calories": "__CALORIES__"}).replace(
        '"__CALORIES__"', "1e999"
    )
    bad_ride = json.loads(bad_json)
    assert bad_ride["calories"] == float("inf")

    result = coach.import_activities([{**RIDE, "activityId": 6002}, bad_ride])
    assert result["inserted"] == 1
    assert result["rejected"] == 1
    reason = result["rejections"][0]["reason"]
    assert "calories" in reason
    stored = coach.list_activities()["activities"]
    assert [row["garmin_activity_id"] for row in stored] == ["6002"]


def test_an_oversized_calories_integer_rejects_the_row_without_killing_the_batch(db):
    """A 400-digit JSON integer parses as an arbitrary-precision Python int;
    `float()` on it raises `OverflowError`. Unguarded, that escaped past the
    per-row reject path and killed the whole import call with a field-less
    "int too large to convert to float" — losing every valid ride beside it."""
    from cycling_mcp import coach

    bad_ride = {**RIDE, "activityId": 6003, "calories": 10**400}

    result = coach.import_activities([{**RIDE, "activityId": 6004}, bad_ride])
    assert result["inserted"] == 1
    assert result["rejected"] == 1
    reason = result["rejections"][0]["reason"]
    assert "calories" in reason
    stored = coach.list_activities()["activities"]
    assert [row["garmin_activity_id"] for row in stored] == ["6004"]


def test_a_nan_calories_value_rejects_the_row(db):
    """NaN is not producible in strict JSON (`json.loads` refuses the bare
    token unless the caller opts in), so this is passed in-process — the same
    class of bug reaches `_number` however NaN arrives."""
    from cycling_mcp import coach

    bad_ride = {**RIDE, "activityId": 6005, "calories": float("nan")}

    result = coach.import_activities([bad_ride])
    assert result["inserted"] == 0
    assert result["rejected"] == 1
    assert "calories" in result["rejections"][0]["reason"]


def test_a_decimal_comma_calories_value_still_imports(db):
    """Regression: the finite-number guard must not touch the decimal-comma
    parsing `_number` exists for — a European-locale Garmin export writes
    232,5 for 232.5."""
    from cycling_mcp import coach

    result = coach.import_activities([{**RIDE, "activityId": 6006, "calories": "232,5"}])
    assert result["inserted"] == 1
    stored = coach.list_activities()["activities"][0]
    assert stored["calories"] == 232.5


def test_a_large_but_finite_calories_value_still_imports(db):
    """Just outside the guard: a large finite float is a plausible-if-odd
    value, not an unusable one, and `_normalize` applies no plausibility cap
    to calories — it must import, not be rejected."""
    from cycling_mcp import coach

    result = coach.import_activities([{**RIDE, "activityId": 6007, "calories": 1e6}])
    assert result["inserted"] == 1
    stored = coach.list_activities()["activities"][0]
    assert stored["calories"] == 1e6


def test_a_non_finite_lap_value_is_thinned_not_raised():
    """Laps are never rejected (see `normalize_lap`'s own docstring) — a
    non-finite lap value must fold to None the same way a missing one does,
    not raise `_NotFiniteNumber` past this call site."""
    row = normalize_lap({"averagePower": float("inf"), "elapsedDuration": 60.0}, 1)
    assert row["avg_power"] is None
    assert row["duration_s"] == 60.0


# --------------------------------------------------------------------------
# review round 8 — finite-but-unstorable integer fields (finding 2 rework)
# --------------------------------------------------------------------------


def test_a_finite_but_oversized_avg_hr_rejects_the_row_without_killing_the_batch(db):
    """1e19 is finite, sails past the `_NotFiniteNumber` guard, and `round()`
    turns it into a Python int outside SQLite's signed-64-bit INTEGER range
    (-2**63 .. 2**63-1). Unguarded, the sqlite3 binding raised its own
    `OverflowError` at INSERT time — inside `import_activities`'s db loop,
    with no per-item try/except — killing the whole batch with a field-less
    message. The row must be rejected by name instead, and the valid ride
    beside it must still import."""
    from cycling_mcp import coach

    bad_ride = {**RIDE, "activityId": 6008, "averageHR": 1e19}

    result = coach.import_activities([{**RIDE, "activityId": 6009}, bad_ride])
    assert result["inserted"] == 1
    assert result["rejected"] == 1
    reason = result["rejections"][0]["reason"]
    assert "avg_hr" in reason or "averageHR" in reason
    stored = coach.list_activities()["activities"]
    assert [row["garmin_activity_id"] for row in stored] == ["6009"]


def test_an_avg_hr_just_inside_sqlite_integer_range_still_imports(db):
    """Just inside the boundary: 9.2e18 is below 2**63 (~9.223e18), so it is
    a value SQLite genuinely can store. The bound must reject only what
    SQLite cannot hold, not every large heart-rate figure."""
    from cycling_mcp import coach

    assert 9.2e18 < 2**63
    result = coach.import_activities([{**RIDE, "activityId": 6010, "averageHR": 9.2e18}])
    assert result["inserted"] == 1
    stored = coach.list_activities()["activities"][0]
    assert stored["avg_hr"] == round(9.2e18)


def test_the_rejection_message_reports_the_value_as_the_payload_sent(db):
    """The reason names what the payload actually carried, not the float the
    range round-trip produced. A numeric *string* is the shape where the two
    genuinely differ: '9223372036854775807' still rejects through the float
    round-trip (the documented asymmetry — only an int payload skips it), and
    pre-fix the message showed 9.223372036854776e+18 instead of the string
    that was actually in the row."""
    from cycling_mcp import coach

    result = coach.import_activities(
        [{**RIDE, "activityId": 6011, "averageHR": "9223372036854775807"}]
    )
    assert result["rejected"] == 1
    assert repr("9223372036854775807") in result["rejections"][0]["reason"]


def test_an_int_at_exactly_sqlite_integer_max_imports(db):
    """Just outside the old guard: 2**63-1 is exactly SQLite's INTEGER max,
    but `float(2**63 - 1)` rounds up to `2**63` — one float-ulp over the
    line, which used to reject the largest value SQLite can actually store.
    An int payload value is range-checked as itself, not through that
    round-trip."""
    from cycling_mcp import coach

    assert float(2**63 - 1) == float(2**63)  # the round-trip that used to lose the boundary
    result = coach.import_activities([{**RIDE, "activityId": 6012, "averageHR": 2**63 - 1}])
    assert result["inserted"] == 1
    stored = coach.list_activities()["activities"][0]
    assert stored["avg_hr"] == 2**63 - 1


def test_an_int_one_past_sqlite_integer_max_still_rejects(db):
    """The value just outside the fixed guard, sent as an int: 2**63 is one
    past what SQLite can store. The float arm of the same boundary
    (`float(2**63)`, sent as a JSON float) is covered by
    test_a_finite_but_oversized_avg_hr_rejects_the_row_without_killing_the_batch
    above, which sends 1e19 — well past the line but exercising the same
    float-branch rejection path."""
    from cycling_mcp import coach

    result = coach.import_activities([{**RIDE, "activityId": 6013, "averageHR": 2**63}])
    assert result["rejected"] == 1
    assert str(2**63) in result["rejections"][0]["reason"]


def test_a_float_one_past_sqlite_integer_max_still_rejects(db):
    """The float arm of the same boundary, exactly one past: `float(2**63)`
    sent as the payload value must reject the same as the int form above."""
    from cycling_mcp import coach

    result = coach.import_activities([{**RIDE, "activityId": 6014, "averageHR": float(2**63)}])
    assert result["rejected"] == 1
    assert str(float(2**63)) in result["rejections"][0]["reason"]


def test_an_oversized_lap_avg_hr_is_thinned_not_raised():
    """`normalize_lap` never rejects a row — the same out-of-range integer
    must fold to None here, not raise past this call site the way the
    unguarded `round()` used to (a Python-int-too-large `OverflowError` at
    lap INSERT time)."""
    row = normalize_lap({"averageHR": 1e19, "elapsedDuration": 60.0}, 1)
    assert row["avg_hr"] is None
    assert row["duration_s"] == 60.0


def test_a_lap_avg_hr_at_exactly_sqlite_integer_max_imports():
    """The same int-vs-float-round-trip split as `normalize_activity`
    (`float(2**63 - 1)` rounds up to `2**63`) applies on the lap path too —
    an int payload value is range-checked as itself, so the largest value
    SQLite can store must survive here as well, not just on the activity
    path."""
    row = normalize_lap({"averageHR": 2**63 - 1, "elapsedDuration": 60.0}, 1)
    assert row["avg_hr"] == 2**63 - 1


def test_a_lap_avg_hr_one_past_sqlite_integer_max_is_thinned():
    """Just outside the fixed lap-side guard: 2**63 is one past what SQLite
    can store, so it must still thin to None, not raise or overflow at
    INSERT time."""
    row = normalize_lap({"averageHR": 2**63, "elapsedDuration": 60.0}, 1)
    assert row["avg_hr"] is None

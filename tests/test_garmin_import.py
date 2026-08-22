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


def test_a_sentinel_utc_time_still_keeps_a_readable_local_one():
    """The local wall clock needs no conversion, so it survives — the row is
    stored with a null UTC time rather than discarded."""
    from cycling_mcp.garmin_import import normalize_activity

    row, reason = normalize_activity(
        {
            "activityId": 4243,
            "startTimeGMT": "0001-01-01T00:00:00+0200",
            "startTimeLocal": "0001-01-01T00:00:00+0200",
            "duration": 3600.0,
        }
    )
    assert reason is None
    assert row["start_time_utc"] is None
    assert row["start_time_local"] == "0001-01-01T00:00:00"


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

    assert _split_offset("2026-08-20") == ("2026-08-20", None)
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

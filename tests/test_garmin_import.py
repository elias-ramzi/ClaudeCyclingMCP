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
    row, _ = normalize_activity({k: v for k, v in RIDE.items() if k != "startTimeLocal"})
    assert row["local_date"] == "2026-07-05"
    assert "local_date_from_utc" in row["_flags"]


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
    row, reason = normalize_activity(
        {k: v for k, v in RIDE.items() if k not in ("avgPower", "maxPower", "normPower")}
    )
    assert reason is None
    assert "no_power" in row["_flags"]


def test_average_power_without_np_is_flagged_separately():
    row, _ = normalize_activity({k: v for k, v in RIDE.items() if k != "normPower"})
    assert "no_normalized_power" in row["_flags"]
    assert "no_power" not in row["_flags"]


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

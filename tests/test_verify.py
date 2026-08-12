"""The round-trip comparison, tested against a real recorded API exchange.

The recorded fixture is an actual upload/fetch pair from Garmin Connect. It
pins the observed behaviour — most importantly that a target sent as id 2 comes
back as "power.zone" with the watts intact — so a future schema drift shows up
as a failing test rather than as a mangled workout.
"""

import copy
import json
from pathlib import Path

import pytest

from cycling_mcp.verify import compare_upload, total_step_seconds

GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def recorded():
    return json.loads((GOLDEN / "recorded_roundtrip.json").read_text())


def test_a_real_exchange_compares_clean(recorded):
    assert compare_upload(recorded["sent"], recorded["fetched"]) == []


def test_garmins_derived_duration_disagrees_with_the_steps(recorded):
    """Warning 3: estimated_duration_seconds follows Garmin's own rules.

    600 + 3*(240+120) + 300 = 1980 here, which happens to agree, but the field
    is still not what the comparison relies on — the reference workout
    1662651131 reports 5400 against 5700 s of steps.
    """
    assert total_step_seconds(recorded["sent"]) == 1980.0


def test_a_corrupted_repeat_count_is_caught(recorded):
    """The failure mode when a RepeatGroupDTO omits conditionTypeId."""
    fetched = copy.deepcopy(recorded["fetched"])
    fetched["segments"][0]["steps"][1]["repeat_count"] = 1
    problems = compare_upload(recorded["sent"], fetched)
    assert any("repeat count" in p and "conditionTypeId 7" in p for p in problems)


def test_a_pace_target_is_caught(recorded):
    """What sending target type id 6 on a cycling workout actually produced."""
    fetched = copy.deepcopy(recorded["fetched"])
    fetched["segments"][0]["steps"][0]["target_type"] = "pace.zone"
    problems = compare_upload(recorded["sent"], fetched)
    assert any("target_type" in p and "pace.zone" in p for p in problems)


def test_changed_watts_are_caught(recorded):
    fetched = copy.deepcopy(recorded["fetched"])
    fetched["segments"][0]["steps"][0]["target_value_high"] = 300.0
    problems = compare_upload(recorded["sent"], fetched)
    assert any("target_high" in p for p in problems)


def test_a_dropped_step_is_caught(recorded):
    fetched = copy.deepcopy(recorded["fetched"])
    del fetched["segments"][0]["steps"][2]
    problems = compare_upload(recorded["sent"], fetched)
    assert any("sent 3 steps" in p for p in problems)


def test_a_dropped_nested_step_is_caught(recorded):
    fetched = copy.deepcopy(recorded["fetched"])
    del fetched["segments"][0]["steps"][1]["steps"][1]
    problems = compare_upload(recorded["sent"], fetched)
    assert any("steps" in p for p in problems)


def test_a_changed_step_type_is_caught(recorded):
    fetched = copy.deepcopy(recorded["fetched"])
    fetched["segments"][0]["steps"][0]["type"] = "interval"
    problems = compare_upload(recorded["sent"], fetched)
    assert any("type sent as 'warmup'" in p for p in problems)


def test_a_changed_duration_is_caught(recorded):
    fetched = copy.deepcopy(recorded["fetched"])
    fetched["segments"][0]["steps"][0]["end_condition_value"] = 900.0
    problems = compare_upload(recorded["sent"], fetched)
    assert any("duration_s" in p for p in problems)


def test_a_renamed_workout_is_caught(recorded):
    fetched = copy.deepcopy(recorded["fetched"])
    fetched["name"] = "Something else"
    problems = compare_upload(recorded["sent"], fetched)
    assert any("workout name" in p for p in problems)


def test_a_dropped_cadence_target_is_caught(recorded):
    fetched = copy.deepcopy(recorded["fetched"])
    del fetched["segments"][0]["steps"][0]["secondary_target_type"]
    problems = compare_upload(recorded["sent"], fetched)
    assert any("secondary_target_type" in p for p in problems)


def test_no_target_steps_compare_clean():
    """A no.target step comes back with the target field absent entirely."""
    sent = {
        "workoutName": "T",
        "sportType": {"sportTypeId": 2, "sportTypeKey": "cycling"},
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "workoutSteps": [
                    {
                        "type": "ExecutableStepDTO",
                        "stepOrder": 1,
                        "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                        "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                        "endConditionValue": 300,
                        "targetType": {
                            "workoutTargetTypeId": 1,
                            "workoutTargetTypeKey": "no.target",
                        },
                    }
                ],
            }
        ],
    }
    fetched = {
        "name": "T",
        "sport": "cycling",
        "segments": [
            {
                "order": 1,
                "steps": [
                    {
                        "order": 1,
                        "type": "interval",
                        "end_condition": "time",
                        "end_condition_value": 300.0,
                    }
                ],
            }
        ],
    }
    assert compare_upload(sent, fetched) == []


def test_the_renderer_output_compares_clean_against_its_own_echo(recorded):
    """A sanity check on the harness itself: identical data must not diff."""
    assert compare_upload(recorded["sent"], copy.deepcopy(recorded["fetched"])) == []

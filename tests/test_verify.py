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
    return json.loads((GOLDEN / "recorded_roundtrip.json").read_text(encoding="utf-8"))


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


# --- MyWhoosh header checks ---------------------------------------------

from cycling_mcp.verify import compare_mywhoosh_import  # noqa: E402

# The 68 min session from the 2026-08-19 run: 4080 s, TSS 71.
GOOD = {"expected_seconds": 4080.0, "expected_tss": 71.0}
SNAPSHOT = {"workout_time": "70:00", "training_load": "70"}


def test_a_matching_header_with_a_changed_snapshot_is_safe():
    result = compare_mywhoosh_import(
        **GOOD, workout_time="68:00", training_load=71, before=SNAPSHOT
    )
    assert result["safe_to_export"]
    assert result["problems"] == []
    assert result["warnings"] == []


def test_an_hour_plus_clock_parses():
    result = compare_mywhoosh_import(**GOOD, workout_time="1:08:00", before=SNAPSHOT)
    assert result["safe_to_export"]


def test_a_header_identical_to_the_snapshot_is_a_silent_no_op():
    """The case a duration check alone cannot catch.

    "Create New" opens an editor that may already hold a workout. If that
    workout happens to share this session's duration — which happened on
    2026-08-19 — a matching header is indistinguishable from a failed import
    without the snapshot.
    """
    result = compare_mywhoosh_import(
        expected_seconds=4200.0,
        expected_tss=70.0,
        workout_time="70:00",
        training_load="70",
        before=SNAPSHOT,
    )
    assert not result["safe_to_export"]
    assert "import did nothing" in " ".join(result["problems"])


def test_nan_is_a_failed_import():
    result = compare_mywhoosh_import(**GOOD, workout_time="NaN", before=SNAPSHOT)
    assert not result["safe_to_export"]
    assert "not a duration" in " ".join(result["problems"])


def test_zero_duration_is_a_failed_import():
    result = compare_mywhoosh_import(**GOOD, workout_time="00:00", before=SNAPSHOT)
    assert not result["safe_to_export"]


def test_a_wrong_duration_blocks_export():
    result = compare_mywhoosh_import(**GOOD, workout_time="42:00", before=SNAPSHOT)
    assert not result["safe_to_export"]
    assert "42:00" in " ".join(result["problems"])


def test_a_missing_snapshot_blocks_rather_than_passing_on_weaker_checks():
    """This used to warn and pass. That was wrong, and wrong in the dangerous
    direction.

    On a real no-op the duration and Training Load checks both pass — that is
    exactly how the failure presents — so without the snapshot the tool cannot
    tell success from nothing having happened. Reporting "safe to export" on
    two checks that provably do not discriminate is worse than reporting
    nothing. Blocking costs only time, because no credit is spent before the
    export itself.
    """
    result = compare_mywhoosh_import(**GOOD, workout_time="68:00", training_load=71)
    assert result["safe_to_export"] is False
    assert any("did not run" in p for p in result["problems"])
    assert any("costs a credit" in p for p in result["problems"])


def test_a_far_off_training_load_warns_but_does_not_block():
    """Training Load is MyWhoosh's model, not ours; a gap is a hint, not proof."""
    result = compare_mywhoosh_import(
        **GOOD, workout_time="68:00", training_load=44, before=SNAPSHOT
    )
    assert result["safe_to_export"]
    assert any("Training Load" in w for w in result["warnings"])

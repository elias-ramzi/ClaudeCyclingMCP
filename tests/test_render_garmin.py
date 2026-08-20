import json
from pathlib import Path

import pytest

from cycling_mcp.render_garmin import render_garmin
from cycling_mcp.spec import load_spec, validate_spec
from cycling_mcp.verify import total_step_seconds

GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def sweetspot():
    return load_spec(json.loads((GOLDEN / "sweetspot-3x10.json").read_text(encoding="utf-8")))


def payload(spec: dict) -> dict:
    return render_garmin(load_spec(spec))[0]


def steps(built: dict) -> list[dict]:
    return built["workoutSegments"][0]["workoutSteps"]


def walk(step_list):
    for step in step_list:
        yield step
        yield from walk(step.get("workoutSteps", []))


def test_matches_the_golden_payload(sweetspot):
    expected = json.loads((GOLDEN / "sweetspot-3x10.garmin.json").read_text(encoding="utf-8"))
    assert render_garmin(sweetspot)[0] == expected


def test_sport_is_cycling(sweetspot):
    built = render_garmin(sweetspot)[0]
    assert built["sportType"] == {"sportTypeId": 2, "sportTypeKey": "cycling"}
    assert built["workoutSegments"][0]["sportType"]["sportTypeId"] == 2


def test_step_durations_survive_the_render(sweetspot):
    assert total_step_seconds(render_garmin(sweetspot)[0]) == sweetspot.total_seconds


# --------------------------------------------------------------------------
# the schema details that fail silently when wrong
# --------------------------------------------------------------------------


def test_power_targets_use_id_2_with_absolute_watts(sweetspot):
    """Id 6 uploads fine and is stored as a *pace* target on a cycling workout."""
    built = render_garmin(sweetspot)[0]
    power_steps = [
        s for s in walk(steps(built)) if s.get("targetType", {}).get("workoutTargetTypeId") == 2
    ]
    assert len(power_steps) == 7
    for step in power_steps:
        assert step["targetType"]["workoutTargetTypeKey"] == "power.zone"
        assert "zoneNumber" not in step
        assert step["targetValueOne"] < step["targetValueTwo"]


def test_no_step_uses_target_type_id_6(sweetspot):
    for step in walk(steps(render_garmin(sweetspot)[0])):
        assert step.get("targetType", {}).get("workoutTargetTypeId") != 6


def test_percent_targets_are_resolved_to_watts():
    built = payload(
        {
            "name": "T",
            "ftp": 200,
            "blocks": [{"type": "steady", "duration": 600, "power_pct": [95, 105]}],
        }
    )
    step = steps(built)[0]
    assert (step["targetValueOne"], step["targetValueTwo"]) == (190.0, 210.0)


def test_repeat_group_carries_the_numeric_condition_type_id():
    """Omitting conditionTypeId makes Garmin silently corrupt the repeat count."""
    built = payload(
        {
            "name": "T",
            "ftp": 250,
            "blocks": [
                {
                    "type": "repeat",
                    "count": 3,
                    "blocks": [{"type": "steady", "duration": 240, "power_pct": 100}],
                }
            ],
        }
    )
    group = steps(built)[0]
    assert group["type"] == "RepeatGroupDTO"
    assert group["endCondition"] == {"conditionTypeId": 7, "conditionTypeKey": "iterations"}
    assert group["numberOfIterations"] == 3
    assert group["endConditionValue"] == 3.0


def test_repeats_stay_native_rather_than_being_flattened():
    """Unlike the .zwo, Garmin has a real repeat construct — use it."""
    built = payload(
        {
            "name": "T",
            "ftp": 250,
            "blocks": [
                {
                    "type": "repeat",
                    "count": 5,
                    "blocks": [
                        {"type": "steady", "duration": 240, "power_pct": 105},
                        {"type": "steady", "duration": 120, "power_pct": 55},
                    ],
                }
            ],
        }
    )
    assert len(steps(built)) == 1
    assert len(steps(built)[0]["workoutSteps"]) == 2


def test_step_order_is_global_and_continues_through_repeats():
    built = payload(
        {
            "name": "T",
            "ftp": 250,
            "blocks": [
                {"type": "steady", "duration": 600, "power_pct": 60},
                {
                    "type": "repeat",
                    "count": 3,
                    "blocks": [
                        {"type": "steady", "duration": 240, "power_pct": 105},
                        {"type": "steady", "duration": 120, "power_pct": 55},
                    ],
                },
                {"type": "steady", "duration": 600, "power_pct": 55},
            ],
        }
    )
    assert [s["stepOrder"] for s in walk(steps(built))] == [1, 2, 3, 4, 5]


def test_end_conditions_are_time_with_the_numeric_id(sweetspot):
    for step in walk(steps(render_garmin(sweetspot)[0])):
        if step["type"] == "ExecutableStepDTO":
            assert step["endCondition"] == {"conditionTypeId": 2, "conditionTypeKey": "time"}


def test_target_values_sit_on_the_step_not_inside_the_target_type(sweetspot):
    for step in walk(steps(render_garmin(sweetspot)[0])):
        assert "targetValueOne" not in step.get("targetType", {})
        assert "zoneNumber" not in step.get("targetType", {})


def test_step_types_map_to_their_ids(sweetspot):
    built = render_garmin(sweetspot)[0]
    pairs = [(s["stepType"]["stepTypeId"], s["stepType"]["stepTypeKey"]) for s in steps(built)]
    assert pairs[0] == (1, "warmup")
    assert pairs[1] == (3, "interval")
    assert pairs[2] == (4, "recovery")
    assert pairs[-1] == (2, "cooldown")


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------


def test_scalar_target_gets_a_band_because_garmin_needs_a_range():
    built = payload(
        {
            "name": "T",
            "ftp": 255,
            "blocks": [{"type": "steady", "duration": 600, "power_w": 232, "role": "interval"}],
        }
    )
    step = steps(built)[0]
    assert (step["targetValueOne"], step["targetValueTwo"]) == (227.0, 237.0)


def test_the_easy_end_of_a_session_gets_a_wider_band_than_the_hard_end():
    """One width does not fit both ends.

    +/-2% of a 250 W interval is +/-5 W, which is right; +/-2% of a 140 W
    recovery is +/-3 W, a window narrow enough to alarm continuously on an easy
    spin. Reported from a real ride, 2026-08-19.
    """
    built = payload(
        {
            "name": "T",
            "ftp": 255,
            "blocks": [
                {"type": "steady", "duration": 600, "power_w": 250, "role": "interval"},
                {"type": "steady", "duration": 300, "power_w": 140, "role": "recovery"},
            ],
        }
    )
    interval, recovery = steps(built)
    assert (interval["targetValueOne"], interval["targetValueTwo"]) == (245.0, 255.0)
    assert (recovery["targetValueOne"], recovery["targetValueTwo"]) == (133.0, 147.0)


def test_an_explicit_band_overrides_every_role():
    """The existing knob keeps meaning exactly what it says."""
    built = payload(
        {
            "name": "T",
            "ftp": 255,
            "garmin_target_band_pct": 1.0,
            "blocks": [
                {"type": "steady", "duration": 600, "power_w": 250, "role": "interval"},
                {"type": "steady", "duration": 300, "power_w": 140, "role": "recovery"},
            ],
        }
    )
    interval, recovery = steps(built)
    assert (interval["targetValueOne"], interval["targetValueTwo"]) == (248.0, 252.0)
    assert (recovery["targetValueOne"], recovery["targetValueTwo"]) == (139.0, 141.0)


def test_band_width_is_configurable():
    built = payload(
        {
            "name": "T",
            "ftp": 200,
            "blocks": [{"type": "steady", "duration": 600, "power_w": 200}],
            "garmin_target_band_pct": 5,
        }
    )
    step = steps(built)[0]
    assert (step["targetValueOne"], step["targetValueTwo"]) == (190.0, 210.0)


def test_explicit_range_is_not_widened():
    built = payload(
        {
            "name": "T",
            "ftp": 200,
            "blocks": [{"type": "steady", "duration": 600, "power_w": [180, 220]}],
        }
    )
    step = steps(built)[0]
    assert (step["targetValueOne"], step["targetValueTwo"]) == (180.0, 220.0)


def test_ramp_becomes_one_step_spanning_the_range():
    built = payload(
        {
            "name": "T",
            "ftp": 255,
            "blocks": [{"type": "ramp", "duration": 600, "from_w": 130, "to_w": 180}],
        }
    )
    assert len(steps(built)) == 1
    step = steps(built)[0]
    assert (step["targetValueOne"], step["targetValueTwo"]) == (130.0, 180.0)


def test_descending_ramp_still_reports_low_then_high():
    """targetValueOne must be the lower watt figure regardless of direction."""
    built = payload(
        {
            "name": "T",
            "ftp": 255,
            "blocks": [{"type": "ramp", "duration": 600, "from_w": 180, "to_w": 130}],
        }
    )
    step = steps(built)[0]
    assert (step["targetValueOne"], step["targetValueTwo"]) == (130.0, 180.0)


def test_ramp_steps_stair_step_the_ramp():
    built = payload(
        {
            "name": "T",
            "ftp": 200,
            "blocks": [
                {"type": "ramp", "duration": 600, "from_w": 100, "to_w": 200, "ramp_steps": 4}
            ],
        }
    )
    assert len(steps(built)) == 4
    assert sum(s["endConditionValue"] for s in steps(built)) == 600.0
    assert steps(built)[0]["targetValueOne"] == 100.0
    assert steps(built)[-1]["targetValueTwo"] == 200.0


def test_free_ride_has_no_target():
    built = payload({"name": "T", "ftp": 250, "blocks": [{"type": "free", "duration": 600}]})
    step = steps(built)[0]
    assert step["targetType"] == {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
    assert "targetValueOne" not in step


def test_cadence_becomes_a_secondary_target_keeping_its_range():
    built = payload(
        {
            "name": "T",
            "ftp": 250,
            "blocks": [{"type": "steady", "duration": 600, "power_pct": 90, "cadence": [85, 95]}],
        }
    )
    step = steps(built)[0]
    assert step["secondaryTargetType"] == {
        "workoutTargetTypeId": 3,
        "workoutTargetTypeKey": "cadence",
    }
    assert (step["secondaryTargetValueOne"], step["secondaryTargetValueTwo"]) == (85.0, 95.0)


def test_hr_note_becomes_a_description_never_a_heart_rate_target():
    built = payload(
        {
            "name": "T",
            "ftp": 250,
            "blocks": [
                {
                    "type": "steady",
                    "duration": 600,
                    "power_pct": 90,
                    "message": "Tempo",
                    "hr_note": "expect 145-155 bpm",
                }
            ],
        }
    )
    step = steps(built)[0]
    assert step["description"] == "Tempo | expect 145-155 bpm"
    for value in json.dumps(built).split():
        assert "heart.rate" not in value


def test_no_heart_rate_target_is_ever_emitted(sweetspot):
    assert "heart.rate" not in json.dumps(render_garmin(sweetspot)[0])


# --------------------------------------------------------------------------
# the tool surface must agree with what the renderer emits
# --------------------------------------------------------------------------


def test_tool_docstring_does_not_contradict_the_renderer():
    """A stale docstring is worse than no docstring.

    It said "power.between (workoutTargetTypeId 6)" long after the renderer
    moved to id 2, and a downstream client duly flagged the payload as wrong
    and refused to upload it. The documentation and the output have to move
    together.
    """
    from cycling_mcp.server import render_garmin as tool

    doc = tool.__doc__ or ""
    assert "power.between" not in doc or "wrong" in doc
    assert "workoutTargetTypeId 2" in doc


def test_schema_notes_travel_with_the_payload():
    """So a client reading the payload later does not "fix" it back to id 6."""
    import json as _json

    from cycling_mcp.server import GARMIN_SCHEMA_NOTES
    from cycling_mcp.server import render_garmin as tool

    result = _json.loads(
        tool(
            {
                "name": "T",
                "ftp": 255,
                "blocks": [{"type": "steady", "duration": 600, "power_w": 232}],
            }
        )
    )
    assert result["schema_notes"] == GARMIN_SCHEMA_NOTES
    assert "id 6" in GARMIN_SCHEMA_NOTES["why_not_id_6"]
    assert "pace.zone" in GARMIN_SCHEMA_NOTES["why_not_id_6"]


def test_warning_paths_agree_with_the_validator():
    """One warnings list used to carry two 'blocks[1]' labels meaning two
    different blocks: the validator counted from 0 and the renderers from 1.
    Both now match the JSON array the author wrote."""
    spec = {
        "name": "T",
        "ftp": 255,
        "blocks": [
            {"type": "ramp", "duration": "12:00", "from_pct": 50, "to_pct": 70},
            {
                "type": "repeat",
                "count": 2,
                "duration": "0:00",
                "blocks": [{"type": "steady", "duration": 60, "power_pct": 90}],
            },
        ],
    }
    _, _, spec_warnings = validate_spec(spec)
    render_warnings = render_garmin(load_spec(spec))[1]

    # The validator's complaint is about the repeat, the second block.
    assert any(w.startswith("blocks[1]:") for w in spec_warnings)
    # The renderer's is about the ramp, the first — and must not collide.
    assert any(w.startswith("blocks[0]:") and "ramp" in w for w in render_warnings)
    assert not any(w.startswith("blocks[1]:") and "ramp" in w for w in render_warnings)

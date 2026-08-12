"""Live round-trip against Garmin Connect: render, upload, fetch back, compare.

Deselected by default (`-m 'not live'`). Run with:

    pytest -m live

It needs Garmin tokens, which the Garmin MCP already stores. Point GARMINTOKENS
at the token directory, or leave it unset to use ~/.garminconnect. No
credentials belong in this repository — the test reads an existing token store
and never prompts for or stores a password.

What this proves, and what it cannot:

* It compares the fetched workout against *the payload that was sent*, not
  against an assumed read shape. Comparing against the curated projection would
  pass while the units were wrong.
* Using the raw API response, it also checks `targetValueUnit`, which is the
  only field distinguishing a watt target from a %FTP one.
* It cannot prove the workout *displays* correctly on a head unit. That needs a
  human to open it in Garmin Connect once — see the README.

The test deletes the workout it creates, including when an assertion fails.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cycling_mcp.render_garmin import render_garmin
from cycling_mcp.spec import load_spec
from cycling_mcp.verify import (
    compare_upload,
    compare_upload_raw,
    percent_targets,
    total_step_seconds,
)

pytestmark = pytest.mark.live

GOLDEN = Path(__file__).parent / "golden"
WORKOUT_NAME = "ClaudeCyclingMCP_test_roundtrip"


@pytest.fixture(scope="module")
def client():
    garminconnect = pytest.importorskip(
        "garminconnect", reason="pip install 'claude-cycling-mcp[live]'"
    )
    tokens = os.environ.get("GARMINTOKENS", os.path.expanduser("~/.garminconnect"))
    if not Path(tokens).exists():
        pytest.skip(f"no Garmin token store at {tokens}")
    api = garminconnect.Garmin()
    api.login(tokens)
    return api


@pytest.fixture
def spec():
    """A spec exercising every construct the renderer can emit."""
    return {
        "name": WORKOUT_NAME,
        "ftp": 255,
        "description": "Automated round-trip test. Deleted by the test.",
        "blocks": [
            {
                "type": "ramp",
                "duration": "10:00",
                "from_w": 120,
                "to_w": 200,
                "role": "warmup",
                "cadence": [85, 95],
                "message": "Warmup ramp",
            },
            {
                "type": "repeat",
                "count": 3,
                "blocks": [
                    {
                        "type": "steady",
                        "duration": "04:00",
                        "power_pct": [95, 105],
                        "cadence": 95,
                        "message": "Effort",
                        "hr_note": "expect 155-165 bpm",
                    },
                    {"type": "steady", "duration": "02:00", "power_w": 140, "role": "recovery"},
                ],
            },
            {"type": "free", "duration": "05:00", "message": "Free spin"},
            {"type": "ramp", "duration": "05:00", "from_w": 150, "to_w": 110, "role": "cooldown"},
        ],
    }


@pytest.fixture
def uploaded(client, spec):
    """Upload the rendered workout, yield (payload, raw fetch), then delete it."""
    payload, warnings = render_garmin(load_spec(spec))
    assert warnings == []

    result = client.upload_workout(payload)
    workout_id = result.get("workoutId")
    assert workout_id, f"upload returned no workoutId: {result!r}"

    try:
        yield payload, client.get_workout_by_id(workout_id)
    finally:
        client.delete_workout(workout_id)


def test_garmin_stores_exactly_what_was_sent(uploaded):
    payload, raw = uploaded
    problems = compare_upload_raw(payload, raw)
    assert problems == [], "Garmin altered the workout:\n" + "\n".join(problems)


def test_power_targets_are_stored_as_watts_not_percent(uploaded):
    """The mangling the curated read cannot show.

    A %FTP target carries targetValueUnit "percent"; a watt target carries no
    unit at all. 232 stored as percent is a 592 W interval at this FTP.
    """
    _, raw = uploaded
    assert percent_targets(raw) == []


def test_repeat_count_survives(uploaded):
    """Corrupted silently when a RepeatGroupDTO omits conditionTypeId 7."""
    _, raw = uploaded
    groups = [
        step
        for segment in raw["workoutSegments"]
        for step in segment["workoutSteps"]
        if step.get("type") == "RepeatGroupDTO"
    ]
    assert len(groups) == 1
    assert groups[0]["numberOfIterations"] == 3
    assert len(groups[0]["workoutSteps"]) == 2


def test_step_durations_are_unchanged(uploaded):
    payload, raw = uploaded
    # 600 warmup + 3*(240+120) + 300 free + 300 cooldown.
    assert total_step_seconds(raw) == total_step_seconds(payload) == 2280.0


def test_no_heart_rate_target_was_created(uploaded):
    """hr_note is a check figure and must never become a control target."""
    _, raw = uploaded
    assert "heart.rate" not in json.dumps(raw["workoutSegments"])


def test_the_curated_read_also_compares_clean(client, uploaded):
    """The curated shape is what the Garmin MCP's get_workout_by_id returns.

    It is lossy, so it is the weaker check — but the bundled garmin-upload skill
    has only this shape available, so it must agree too.
    """
    payload, raw = uploaded
    curated = _curate(raw)
    problems = compare_upload(payload, curated)
    assert problems == [], "\n".join(problems)


def _curate(raw: dict) -> dict:
    """Reduce a raw response to the shape the Garmin MCP's read tool returns."""

    def step(node: dict) -> dict:
        if node.get("type") == "RepeatGroupDTO":
            return {
                "order": node.get("stepOrder"),
                "type": "repeat",
                "end_condition": (node.get("endCondition") or {}).get("conditionTypeKey"),
                "end_condition_value": node.get("endConditionValue"),
                "repeat_count": node.get("numberOfIterations"),
                "steps": [step(child) for child in node.get("workoutSteps", [])],
            }
        target = node.get("targetType") or {}
        secondary = node.get("secondaryTargetType") or {}
        curated = {
            "order": node.get("stepOrder"),
            "type": (node.get("stepType") or {}).get("stepTypeKey"),
            "end_condition": (node.get("endCondition") or {}).get("conditionTypeKey"),
            "end_condition_value": node.get("endConditionValue"),
            "description": node.get("description") or None,
            "target_value_low": node.get("targetValueOne"),
            "target_value_high": node.get("targetValueTwo"),
            "secondary_target_value_low": node.get("secondaryTargetValueOne"),
            "secondary_target_value_high": node.get("secondaryTargetValueTwo"),
        }
        if target.get("workoutTargetTypeKey") not in (None, "no.target"):
            curated["target_type"] = target.get("workoutTargetTypeKey")
        if secondary.get("workoutTargetTypeKey"):
            curated["secondary_target_type"] = secondary.get("workoutTargetTypeKey")
        return {k: v for k, v in curated.items() if v is not None}

    return {
        "name": raw.get("workoutName"),
        "sport": (raw.get("sportType") or {}).get("sportTypeKey"),
        "segments": [
            {"order": s.get("segmentOrder"), "steps": [step(n) for n in s.get("workoutSteps", [])]}
            for s in raw.get("workoutSegments", [])
        ],
    }

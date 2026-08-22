"""Drive the server the way a client does: over stdio, through the schema.

These exist because two bugs shipped past a green in-process suite. Both were
type mismatches that only appear once arguments have been through JSON and
pydantic coercion — a digest that rejected every payload because 227.0 came
back as 227, and a snapshot check that reported "changed" because one side was
the string "72" and the other the float 72.0. Calling the Python function
directly cannot reproduce either: the coercion is the bug.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "golden"


class Client:
    """A minimal MCP stdio client — enough to call tools and read results."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "from cycling_mcp.server import main; main()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=ROOT,
            env={**os.environ, **(env or {})},
        )
        self._id = 0
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            }
        )
        self._read()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _next(self) -> int:
        self._id += 1
        return self._id

    def _send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def _read(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError("server closed the connection")
        return json.loads(line)

    def call(self, tool: str, **arguments):
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next(),
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        result = self._read()["result"]
        text = "".join(block.get("text", "") for block in result["content"])
        if result.get("isError"):
            return {"_error": text}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def close(self) -> None:
        self.proc.kill()


@pytest.fixture(scope="module")
def client():
    c = Client()
    yield c
    c.close()


@pytest.fixture(scope="module")
def spec():
    path = ROOT / "tests" / "golden" / "sweetspot-3x10.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_snapshot_check_refuses_an_unchanged_header(client, spec):
    """The bug this file exists for.

    A scraped Training Load arrives as text; `training_load` is typed numeric
    and `before_training_load` was typed string, so the two sides of the same
    number were compared as "72" against "72.0" and reported as a change. The
    tool then said safe_to_export on the exact silent no-op it guards against —
    and a caller who followed the skill and captured a snapshot was worse off
    than one who skipped it, since omitting it at least produced a warning.
    """
    result = client.call(
        "verify_mywhoosh_import",
        spec=spec,
        workout_time="1:10:00",
        training_load=70,
        before_workout_time="1:10:00",
        before_training_load="70",
    )
    assert result["safe_to_export"] is False, result
    assert any("import did nothing" in p for p in result["problems"])


@pytest.mark.parametrize(
    "before_load,after_load",
    [("70", 70), (70, "70"), (70, 70), ("70", "70"), (70.0, "70"), ("70.0", 70)],
)
def test_the_snapshot_check_survives_every_number_shape(client, spec, before_load, after_load):
    """Whichever side arrives as text, the same reading is the same reading."""
    result = client.call(
        "verify_mywhoosh_import",
        spec=spec,
        workout_time="1:10:00",
        training_load=after_load,
        before_workout_time="1:10:00",
        before_training_load=before_load,
    )
    assert result["safe_to_export"] is False, (before_load, after_load, result)


def test_a_genuinely_changed_header_still_passes(client, spec):
    result = client.call(
        "verify_mywhoosh_import",
        spec=spec,
        workout_time="1:10:00",
        training_load=70,
        before_workout_time="45:00",
        before_training_load="40",
    )
    assert result["safe_to_export"] is True, result


def test_the_check_shows_what_it_compared(client, spec):
    """A bare boolean is not evidence the comparison happened. This one was
    wrong for a week while looking exactly like a passing check."""
    result = client.call(
        "verify_mywhoosh_import",
        spec=spec,
        workout_time="1:10:00",
        training_load=70,
        before_workout_time="45:00",
        before_training_load="40",
    )
    check = next(c for c in result["checks"] if c["check"] == "changed_from_snapshot")
    assert check["before"] == {"workout_time": "45:00", "training_load": "40"}
    assert check["after"] == {"workout_time": "1:10:00", "training_load": 70}


def test_a_retyped_payload_passes_the_digest(client, spec):
    """227.0 rendered, 227 retyped — the same payload, and it must digest so."""
    rendered = client.call("render_garmin", spec=spec)
    retyped = json.loads(json.dumps(rendered["payload"]))
    checked = client.call(
        "check_garmin_payload", payload=retyped, expected_digest=rendered["payload_digest"]
    )
    assert checked["matches_rendered"] is True, checked


def test_every_tool_answers_over_stdio(client, spec):
    """No tool here should ever be slow or silent; three sessions reported
    otherwise and every time the request had not reached the server."""
    assert client.call("server_info")["version"]
    assert client.call("validate_spec", spec=spec)["valid"] is True
    assert "Sweet Spot" in client.call("describe_spec", spec=spec)
    assert client.call("render_zwo", spec=spec)["ok"] is True
    assert client.call("render_garmin", spec=spec)["ok"] is True
    assert client.call("spec_schema")["schema"]
    assert client.call("get_skill", name="mywhoosh-upload")


def test_a_missing_snapshot_blocks_over_stdio(client, spec):
    """Two checks that both pass on a no-op are not evidence of success."""
    result = client.call(
        "verify_mywhoosh_import", spec=spec, workout_time="1:10:00", training_load=70
    )
    assert result["safe_to_export"] is False
    assert any("did not run" in p for p in result["problems"])


def test_the_library_card_that_actually_landed(client, spec):
    """The real card from the 2026-08-21 export, in the card's own formats.

    "1h 18m" not "78:00", and the name rendered with U+00D7 while the ASCII
    form was uploaded.
    """
    result = client.call(
        "verify_mywhoosh_library_entry",
        spec={**spec, "filename": "Tempo-3x14"},
        name="Tempo-3×14",
        duration="1h 10m",
        tss=70,
        intensity_factor=0.77,
    )
    assert result["landed"] is True, result
    assert result["problems"] == []


def test_a_library_card_for_a_different_workout_is_caught(client, spec):
    result = client.call(
        "verify_mywhoosh_library_entry",
        spec={**spec, "filename": "Tempo-3x14"},
        name="Sweet-Spot-3x11",
        duration="1h 08m",
    )
    assert result["landed"] is False
    assert any("different workout" in p for p in result["problems"])


# --------------------------------------------------------------------------
# the coach layer over the wire
#
# These matter for the same reason the rest of this file does. The import tools
# take Garmin's payload as a union of shapes, and a schema that rejects one of
# them in the client is invisible to a test that calls the Python function.
# --------------------------------------------------------------------------


RIDE = {
    "activityId": 5001,
    "activityName": "Sweet spot",
    "activityType": {"typeKey": "virtual_ride"},
    "startTimeLocal": "2026-07-05 07:00:00",
    "startTimeGMT": "2026-07-05 05:00:00",
    "duration": 4200.0,
    "avgPower": 190.0,
    "normPower": 198.0,
    "averageHR": 150,
}


@pytest.fixture(scope="module")
def coach_client(tmp_path_factory):
    """A server whose database is in a temporary directory, not the real one."""
    from cycling_mcp.store import ENV_DB_PATH

    path = tmp_path_factory.mktemp("coach") / "coach.db"
    client = Client(env={ENV_DB_PATH: str(path)})
    yield client
    client.close()


def test_server_info_reports_the_temporary_database(coach_client):
    info = coach_client.call("server_info")
    assert info["database"]["exists"] is False, "reporting must not create it"
    assert info["database"]["path"].endswith("coach.db")


def test_the_profile_starts_as_a_list_of_gaps(coach_client):
    profile = coach_client.call("get_profile")
    assert profile["ok"] is True
    assert {gap["field"] for gap in profile["gaps"]} >= {"ftp", "objective"}


def test_an_ftp_logged_over_the_wire_comes_back_with_zones(coach_client):
    result = coach_client.call("log_ftp", twenty_min_watts=280, effective_date="2026-06-01")
    assert result["ok"] is True
    assert result["stored"]["value_watts"] == 266
    assert any(zone["zone"] == "sweet_spot" for zone in result["zones"])


@pytest.mark.parametrize("shape", ["list", "object", "wrapped", "string"])
def test_every_payload_shape_survives_the_tool_schema(coach_client, shape):
    """The union type is the risk: pydantic coerces before the code runs.

    A schema that only accepted a list would make `get_activity` output — a
    single object — unimportable, and the failure would look like Garmin's.
    """
    ride = {
        **RIDE,
        "activityId": {"list": 6001, "object": 6002, "wrapped": 6003, "string": 6004}[shape],
    }
    payload = {
        "list": [ride],
        "object": ride,
        "wrapped": {"activities": [ride]},
        "string": json.dumps([ride]),
    }[shape]
    result = coach_client.call("import_activities", payload=payload)
    assert result["ok"] is True, result
    assert result["inserted"] == 1, result


def test_re_importing_over_the_wire_is_still_idempotent(coach_client):
    """227.0 rendered and 227 retyped is the bug this file exists for; the same
    coercion applies to every number on an activity."""
    first = coach_client.call("import_activities", payload=[RIDE])
    again = coach_client.call("import_activities", payload=[json.loads(json.dumps(RIDE))])
    assert first["inserted"] == 1
    assert again["unchanged"] == 1 and again["inserted"] == 0


def test_a_planned_week_round_trips_through_the_schema(coach_client):
    spec = {
        "name": "Endurance 2h",
        "ftp": 266,
        "blocks": [{"type": "steady", "duration": 7200, "power_pct": 65}],
    }
    saved = coach_client.call(
        "save_planned_workouts",
        workouts=[{"spec": spec, "scheduled_date": "2026-07-05", "note": "steady"}],
    )
    assert saved["ok"] is True and saved["saved"] == 1
    stored = saved["planned_workouts"][0]
    assert stored["spec"] == spec, "the spec must survive JSON unchanged to stay renderable"

    week = coach_client.call("get_week", start="2026-07-01", end="2026-07-12", today="2026-07-20")
    assert week["ok"] is True
    assert any(p["id"] == stored["id"] for p in week["planned_workouts"])


def test_a_refusal_arrives_as_an_answer_not_an_error(coach_client):
    """A refusal is information the caller acts on. Thrown across the tool
    boundary it arrives as an unstructured error string instead."""
    result = coach_client.call("log_ftp", value_watts=3.9)
    assert "_error" not in result
    assert result["ok"] is False
    assert "Check the units" in result["error"]


def test_every_coach_tool_answers_over_stdio(coach_client):
    assert coach_client.call("get_zones")["ok"] is True
    assert coach_client.call("list_activities")["ok"] is True
    assert coach_client.call("compute_load")["ok"] is True
    assert coach_client.call("get_form", start="2026-07-01", end="2026-07-10")["ok"] is True
    assert coach_client.call("list_events")["ok"] is True
    assert coach_client.call("export_data")["ok"] is True
    assert coach_client.call("get_skill", name="coaching")["ok"] is True


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("annotate_activity", {"garmin_activity_id": 5001, "rpe": 7}),
        ("import_activity_laps", {"garmin_activity_id": 5001, "payload": {"lapDTOs": [{}]}}),
    ],
)
def test_a_numeric_garmin_id_is_accepted_by_the_tool_schema(coach_client, tool, arguments):
    """Garmin's activityId is a JSON number and pydantic does not coerce one to
    a string, so a `str | None` schema rejected the call before any coach code
    ran — the id form the repo's own fixtures use was unreachable over the wire.
    """
    coach_client.call(
        "import_activities",
        payload=[{**RIDE, "activityId": 5001}],
    )
    result = coach_client.call(tool, **arguments)
    assert "_error" not in result, result
    assert result["ok"] is True, result


def test_a_numeric_finish_time_is_accepted_by_the_tool_schema(coach_client):
    """The docstring promises "4:32:10" or seconds; seconds must reach the code."""
    event = coach_client.call(
        "add_event", name="Numeric finish", event_date="2026-07-04", priority="C"
    )
    result = coach_client.call(
        "record_race_result", event_id=event["stored"]["id"], finish_time=16330
    )
    assert "_error" not in result, result
    assert result["stored"]["finish_time"] == "4:32:10"


def test_a_numeric_id_links_a_planned_session(coach_client):
    coach_client.call("import_activities", payload=[{**RIDE, "activityId": 5001}])
    saved = coach_client.call(
        "save_planned_workouts",
        workouts=[
            {
                "spec": {
                    "name": "Linked",
                    "ftp": 266,
                    "blocks": [{"type": "steady", "duration": 3600, "power_pct": 65}],
                },
                "scheduled_date": "2026-07-05",
            }
        ],
    )
    result = coach_client.call(
        "link_activity",
        planned_workout_id=saved["planned_workouts"][0]["id"],
        garmin_activity_id=5001,
    )
    assert "_error" not in result, result
    assert result["linked"] is True


def test_an_unreadable_payload_is_still_ok_false_at_the_tool_boundary(coach_client):
    """The coach function raises; the tool layer is what renders the refusal.

    This is the behaviour a client actually sees, and the reason the function
    below it no longer returns its own "ok" flag.
    """
    result = coach_client.call("import_activities", payload="last Tuesday's ride")
    assert "_error" not in result
    assert result["ok"] is False
    assert "not JSON" in result["error"]


def test_split_summaries_are_refused_at_the_tool_boundary(coach_client):
    coach_client.call("import_activities", payload=[{**RIDE, "activityId": 7100}])
    result = coach_client.call(
        "import_activity_laps",
        payload={"splitSummaries": [{"splitType": "CLIMB"}]},
        garmin_activity_id=7100,
    )
    assert result["ok"] is False
    assert "get_activity_splits" in result["error"]


def test_clearing_a_text_field_survives_the_schema(coach_client):
    """`clear` is a list of names over the wire. A schema that rejected it, or
    coerced it to a string, would leave the only erase path unreachable — which
    is exactly how the str-only garmin_activity_id bugs shipped."""
    coach_client.call("update_profile", constraints="broken collarbone — no outdoor riding")
    result = coach_client.call("update_profile", clear=["constraints"])
    assert "_error" not in result, result
    assert result["ok"] is True
    assert result["cleared_fields"] == ["constraints"]
    assert result["athlete"]["constraints"] is None


def test_clearing_a_field_the_tool_does_not_own_is_a_refusal_over_the_wire(coach_client):
    result = coach_client.call("update_profile", clear=["debrief"])
    assert "_error" not in result
    assert result["ok"] is False
    assert "cannot clear 'debrief'" in result["error"]

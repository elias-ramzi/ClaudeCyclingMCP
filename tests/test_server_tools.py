"""The tool layer: what a client actually receives, as opposed to what the
renderers produce. These pin the parts of the response an agent depends on."""

import json
from pathlib import Path

import pytest

from cycling_mcp.server import (
    check_garmin_payload,
    render_garmin,
    render_zwo,
    verify_garmin_upload,
    verify_mywhoosh_import,
)

GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def spec():
    return json.loads((GOLDEN / "sweetspot-3x10.json").read_text())


def test_the_js_literal_is_the_same_file_safely_quoted(spec):
    """The MyWhoosh flow pastes this straight into a script.

    A backtick or a "${" in a name or message would break a template literal
    and could inject; athlete-supplied message text reaches that path, so the
    escaping has to happen here rather than in the skill.
    """
    result = json.loads(render_zwo(spec))
    assert json.loads(result["xml_js_literal"]) == result["xml"]


def test_hostile_message_text_survives_the_js_literal(spec):
    """The literal is double-quoted, which is what makes backticks inert.

    A backtick and a "${" need no escaping inside "..." — they are only special
    in a template literal, which is precisely the construct this field exists to
    replace. What must hold is that the quoting is intact and the round-trip is
    exact.
    """
    spec = dict(spec)
    spec["blocks"] = [dict(b) for b in spec["blocks"]]
    spec["blocks"][0]["message"] = 'back`tick and ${injection} and "quotes"'
    result = json.loads(render_zwo(spec))
    literal = result["xml_js_literal"]
    assert json.loads(literal) == result["xml"]
    assert literal.startswith('"') and literal.endswith('"')
    # Non-ASCII is escaped too, which keeps U+2028/U+2029 — legal in JSON but a
    # line break to older JS parsers — out of the literal entirely.
    assert literal.isascii()


def test_an_unwritable_out_path_says_whose_filesystem_it_is(spec, tmp_path):
    """Errno 45 for "/home/claude" on macOS says nothing about the cause.

    A caller running in a container reaches for a container path first, so the
    error has to name the machine the server is on — otherwise the next guess
    is as blind as the first.
    """
    blocked = tmp_path / "wall"
    blocked.write_text("not a directory")
    result = json.loads(render_zwo(spec, out_path=str(blocked / "sub" / "x.zwo")))
    assert result["ok"] is True, "a failed write must not lose the rendered file"
    assert result["written_to"] is None
    assert "machine running this MCP server" in result["write_error"]


def test_a_good_out_path_still_reports_where_it_landed(spec, tmp_path):
    target = tmp_path / "nested" / "x.zwo"
    result = json.loads(render_zwo(spec, out_path=str(target)))
    assert result["written_to"] == str(target)
    assert "write_error" not in result
    assert target.read_text() == result["xml"]


def test_verify_mywhoosh_import_blocks_on_an_unchanged_header(spec):
    result = json.loads(
        verify_mywhoosh_import(
            spec,
            workout_time="70:00",
            training_load=70,
            before_workout_time="70:00",
            before_training_load="70",
        )
    )
    assert result["safe_to_export"] is False


def test_verify_mywhoosh_import_passes_a_real_import(spec):
    result = json.loads(
        verify_mywhoosh_import(
            spec,
            workout_time="70:00",
            training_load=70,
            before_workout_time="45:00",
            before_training_load="40",
        )
    )
    assert result["safe_to_export"] is True
    assert result["expected"]["duration"] == "1:10:00"


# --- the transcription hole ---------------------------------------------


@pytest.fixture
def rendered(spec):
    return json.loads(render_garmin(spec))


def test_a_faithfully_copied_payload_matches_its_digest(rendered):
    result = json.loads(check_garmin_payload(rendered["payload"], rendered["payload_digest"]))
    assert result["matches_rendered"] is True
    assert "problem" not in result


def test_reformatting_is_not_mistaken_for_corruption(rendered):
    """Key order and whitespace are not content; only the values are.

    A model retyping a payload will not preserve key order, and failing it for
    that would train it to ignore the check.
    """
    reordered = json.loads(json.dumps(rendered["payload"], sort_keys=True))
    reordered["workoutSegments"] = list(reversed(list(reversed(reordered["workoutSegments"]))))
    result = json.loads(check_garmin_payload(reordered, rendered["payload_digest"]))
    assert result["matches_rendered"] is True


def test_one_wrong_digit_is_caught(rendered):
    """The failure the whole check exists for.

    A payload with 237 W mistyped as 337 uploads without error and passes the
    round-trip, because that compares Garmin against what was sent.
    """
    mangled = json.loads(json.dumps(rendered["payload"]))
    step = mangled["workoutSegments"][0]["workoutSteps"][1]
    assert step["targetValueTwo"] == 237
    step["targetValueTwo"] = 337
    result = json.loads(check_garmin_payload(mangled, rendered["payload_digest"]))
    assert result["matches_rendered"] is False
    assert "Do not upload" in result["problem"]


def test_a_missing_digest_warns_rather_than_silently_passing(rendered):
    result = json.loads(check_garmin_payload(rendered["payload"]))
    assert result["digest_checked"] is False
    assert "not compared" in result["warning"]
    assert "matches_rendered" not in result


def test_the_ui_checklist_covers_every_step(rendered):
    result = json.loads(check_garmin_payload(rendered["payload"]))
    checklist = result["ui_checklist"]
    assert any("227-237 W" in line for line in checklist)
    assert any(line.startswith("Warm Up") for line in checklist)
    assert any(line.startswith("Cool Down") for line in checklist)
    assert any("Récupération" in line for line in checklist), "the locale trap is the point"


def test_verify_fails_a_payload_that_garmin_stored_faithfully_but_was_mistyped(rendered):
    """Garmin keeping what it was given is not the same as it being right."""
    mangled = json.loads(json.dumps(rendered["payload"]))
    mangled["workoutSegments"][0]["workoutSteps"][1]["targetValueTwo"] = 337
    fetched = {"workout_id": 1, "workout_name": mangled["workoutName"], "steps": []}
    result = json.loads(verify_garmin_upload(mangled, fetched, rendered["payload_digest"]))
    assert result["matches_rendered"] is False
    assert any("not the payload render_garmin produced" in d for d in result["differences"])


def test_render_garmin_warns_that_a_flat_ramp_is_lossy(rendered):
    """The athlete sees "hold 130-180 W", not "climb". Nothing else says so."""
    assert any("ramp renders on Garmin" in w for w in rendered["warnings"])
    assert any("ramp_steps > 1" in w for w in rendered["warnings"])


def test_a_stair_stepped_ramp_does_not_warn(spec):
    spec = json.loads(json.dumps(spec))
    spec["blocks"][0]["ramp_steps"] = 4
    result = json.loads(render_garmin(spec))
    assert not any("ramp renders on Garmin" in w for w in result["warnings"])


def test_the_renderers_point_at_the_skill(spec, rendered):
    """A model that never finds the skill improvises the upload and skips
    verification — reached by an entirely mundane route under deferred tool
    loading. The return value is read at the right moment; the docstring may
    have been read several turns ago, or not at all."""
    assert "get_skill('garmin-upload')" in rendered["next_step"]
    assert "get_skill('mywhoosh-upload')" in json.loads(render_zwo(spec))["next_step"]

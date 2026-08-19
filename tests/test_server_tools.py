"""The tool layer: what a client actually receives, as opposed to what the
renderers produce. These pin the parts of the response an agent depends on."""

import json
from pathlib import Path

import pytest

from cycling_mcp.server import render_zwo, verify_mywhoosh_import

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

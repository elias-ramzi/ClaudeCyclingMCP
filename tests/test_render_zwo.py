import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from cycling_mcp.render_zwo import format_fraction, render_zwo, safe_filename, zwo_filename
from cycling_mcp.spec import load_spec

GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def sweetspot():
    return load_spec(json.loads((GOLDEN / "sweetspot-3x10.json").read_text()))


def render(spec: dict) -> str:
    return render_zwo(load_spec(spec))[0]


def test_matches_the_golden_file(sweetspot):
    """The 70 min sweet-spot session that imported cleanly into MyWhoosh."""
    assert render_zwo(sweetspot)[0] == (GOLDEN / "sweetspot-3x10.zwo").read_text()


def test_emitted_xml_parses(sweetspot):
    root = ET.fromstring(render_zwo(sweetspot)[0])
    assert root.tag == "workout_file"
    assert root.findtext("sportType") == "bike"
    assert root.find("workout") is not None


def test_every_generated_workout_parses():
    spec = {
        "name": "Everything",
        "ftp": 250,
        "blocks": [
            {"type": "ramp", "duration": 600, "from_pct": 50, "to_pct": 75, "message": "Go"},
            {"type": "free", "duration": 300},
            {
                "type": "repeat",
                "count": 2,
                "blocks": [
                    {"type": "steady", "duration": 240, "power_pct": 105, "cadence": [95, 105]},
                    {"type": "steady", "duration": 120, "power_pct": 55},
                ],
            },
            {"type": "ramp", "duration": 300, "from_pct": 60, "to_pct": 45},
        ],
    }
    ET.fromstring(render(spec))


# --------------------------------------------------------------------------
# format decisions that exist for MyWhoosh's benefit
# --------------------------------------------------------------------------


def test_power_is_a_fraction_of_ftp_never_watts():
    xml = render(
        {
            "name": "T",
            "ftp": 255,
            "blocks": [{"type": "steady", "duration": 600, "power_w": 232}],
        }
    )
    assert 'Power="0.9098"' in xml
    assert "232" not in xml


def test_ramps_are_explicit_never_warmup_or_cooldown(sweetspot):
    """Cooldown ramp direction is read differently by different implementations."""
    xml = render_zwo(sweetspot)[0]
    assert "<Warmup" not in xml
    assert "<Cooldown" not in xml
    assert xml.count("<Ramp ") == 2


def test_descending_ramp_keeps_its_direction():
    xml = render(
        {
            "name": "T",
            "ftp": 255,
            "blocks": [{"type": "ramp", "duration": 600, "from_w": 140, "to_w": 130}],
        }
    )
    # PowerLow is the *start* of the ramp, so a cooldown has Low above High.
    assert 'PowerLow="0.54902" PowerHigh="0.5098"' in xml


def test_repeats_are_flattened_not_intervalst():
    """MyWhoosh's editor makes IntervalsT blocks indivisible after import."""
    xml = render(
        {
            "name": "T",
            "ftp": 250,
            "blocks": [
                {
                    "type": "repeat",
                    "count": 3,
                    "blocks": [
                        {"type": "steady", "duration": 240, "power_pct": 105},
                        {"type": "steady", "duration": 120, "power_pct": 55},
                    ],
                }
            ],
        }
    )
    assert "IntervalsT" not in xml
    assert xml.count("<SteadyState") == 6


def test_free_ride_is_flat_road():
    xml = render({"name": "T", "ftp": 250, "blocks": [{"type": "free", "duration": 600}]})
    assert '<FreeRide Duration="600" FlatRoad="1"/>' in xml


def test_tags_element_is_present_and_empty(sweetspot):
    assert "<tags></tags>" in render_zwo(sweetspot)[0]


# --------------------------------------------------------------------------
# text events
# --------------------------------------------------------------------------


def test_message_becomes_a_nested_text_event():
    xml = render(
        {
            "name": "T",
            "ftp": 250,
            "blocks": [
                {"type": "steady", "duration": 600, "power_pct": 90, "message": "Settle in"}
            ],
        }
    )
    assert '<textevent timeoffset="10" message="Settle in"/>' in xml


def test_text_offset_is_clamped_on_a_short_block():
    xml = render(
        {
            "name": "T",
            "ftp": 250,
            "blocks": [{"type": "steady", "duration": 8, "power_pct": 120, "message": "Go"}],
        }
    )
    assert 'timeoffset="3"' in xml


def test_hr_note_is_carried_as_a_message_never_as_a_target():
    xml = render(
        {
            "name": "T",
            "ftp": 250,
            "blocks": [
                {
                    "type": "steady",
                    "duration": 600,
                    "power_pct": 90,
                    "hr_note": "expect 145-155 bpm",
                }
            ],
        }
    )
    assert "expect 145-155 bpm" in xml
    assert "HeartRate" not in xml
    assert "Hr" not in xml


def test_accents_and_typographic_quotes_are_folded():
    xml, warnings = render_zwo(
        load_spec(
            {
                "name": "T",
                "ftp": 250,
                "blocks": [
                    {
                        "type": "steady",
                        "duration": 600,
                        "power_pct": 90,
                        "message": "Récupération — don’t chase it",
                    }
                ],
            }
        )
    )
    assert "Recuperation - don't chase it" in xml
    assert any("folded" in w for w in warnings)


def test_xml_special_characters_are_escaped():
    xml = render(
        {
            "name": "A & B <test>",
            "ftp": 250,
            "blocks": [
                {"type": "steady", "duration": 600, "power_pct": 90, "message": 'Push "hard" & go'}
            ],
        }
    )
    assert "<name>A &amp; B &lt;test&gt;</name>" in xml
    ET.fromstring(xml)


# --------------------------------------------------------------------------
# cadence
# --------------------------------------------------------------------------


def test_cadence_range_collapses_to_its_midpoint():
    xml = render(
        {
            "name": "T",
            "ftp": 250,
            "blocks": [{"type": "steady", "duration": 600, "power_pct": 90, "cadence": [85, 95]}],
        }
    )
    assert 'Cadence="90"' in xml


# --------------------------------------------------------------------------
# filenames — MyWhoosh names the library entry from the filename, not <name>
# --------------------------------------------------------------------------


def test_filename_field_wins_over_the_name():
    workout = load_spec(
        {
            "name": "Sweet Spot 3x10",
            "filename": "ss-3x10",
            "ftp": 250,
            "blocks": [{"type": "steady", "duration": 600, "power_pct": 90}],
        }
    )
    assert zwo_filename(workout) == "ss-3x10.zwo"


def test_filename_falls_back_to_a_slug_of_the_name(sweetspot):
    workout = load_spec(
        {
            "name": "Sweet Spot 3x10",
            "ftp": 250,
            "blocks": [{"type": "steady", "duration": 600, "power_pct": 90}],
        }
    )
    assert zwo_filename(workout) == "Sweet-Spot-3x10.zwo"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Séance à 91%", "Seance-a-91"),
        ("3 x 10' sweet spot", "3-x-10-sweet-spot"),
        ("../../etc/passwd", "etcpasswd"),  # no path separators survive
        ("", "workout"),
    ],
)
def test_safe_filename_folds_and_strips(raw, expected):
    assert safe_filename(raw) == expected


def test_format_fraction_trims_noise():
    assert format_fraction(0.91) == "0.91"
    assert format_fraction(232 / 255) == "0.9098"
    assert format_fraction(1.0) == "1.0"


def test_the_name_tag_is_spelled_name(sweetspot):
    """Pin `<name>`, because a field report once claimed we emit `<n>`.

    A 2026-08-19 run reported the returned XML as containing `<n>Sweet Spot…`
    and filed it as a renderer bug. It was not: the source, the golden file and
    the file written to disk all carried `<name>`, so the mangling happened
    somewhere in that client's display of the tool result. The cost was real
    anyway — the agent "fixed" it by importing a hand-built substitute, so the
    file on disk and the file in MyWhoosh stopped being identical.

    This test exists so the next reader of a mangled transcript can check the
    claim in one command instead of editing the template.
    """
    xml, _ = render_zwo(sweetspot)
    assert "<name>Sweet Spot 3x10</name>" in xml
    assert "<n>" not in xml


def test_a_repeated_block_warns_once_not_once_per_repetition():
    """Repeats are flattened for MyWhoosh, so a child is emitted `count` times.

    It was authored once, though, so a warning about its content is one fact.
    Three identical lines in the warnings list is noise in the artifact people
    actually read.
    """
    workout = load_spec(
        {
            "name": "T",
            "ftp": 255,
            "blocks": [
                {
                    "type": "repeat",
                    "count": 3,
                    "blocks": [
                        {"type": "steady", "duration": 60, "power_pct": 90, "message": "Hold — it"}
                    ],
                }
            ],
        }
    )
    warnings = render_zwo(workout)[1]
    assert len(warnings) == 1, warnings
    assert "blocks[0].blocks[0]" in warnings[0]


def test_the_flattened_output_is_unchanged_by_that():
    """Suppressing the duplicate warning must not suppress the duplicate block."""
    workout = load_spec(
        {
            "name": "T",
            "ftp": 255,
            "blocks": [
                {
                    "type": "repeat",
                    "count": 3,
                    "blocks": [{"type": "steady", "duration": 60, "power_pct": 90}],
                }
            ],
        }
    )
    assert render_zwo(workout)[0].count("<SteadyState") == 3

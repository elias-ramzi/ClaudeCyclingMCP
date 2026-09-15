"""The get_form window caps are hand-copied into prose in two other files.

`MAX_FORM_SPAN_DAYS`, `MAX_FORM_RUNUP_DAYS` and `PLAUSIBLE_DATE_FLOOR` are the
numbers that actually gate the walk; server.py's docstring and docs/coaching.md
restate them in English ("five years", "ten years", "1990") for a reader who
never opens coach.py. Nothing ties the two together, so a constant can change
while the prose quietly goes on describing the old behaviour — this is that
tie, in the style of test_version_lockstep.py.
"""

from __future__ import annotations

from pathlib import Path

from cycling_mcp.coach import MAX_FORM_RUNUP_DAYS, MAX_FORM_SPAN_DAYS
from cycling_mcp.garmin_import import PLAUSIBLE_DATE_FLOOR

ROOT = Path(__file__).resolve().parents[1]

_NUMBER_WORDS = {5: "five", 10: "ten"}


def test_the_span_and_runup_caps_floor_to_the_years_the_prose_names():
    """Neither cap is an exact multiple of 365 (`MAX_FORM_RUNUP_DAYS` is ten
    years plus ten days of leap-year slop) — the prose already floors, the
    same way the runtime `CoachError` text does (`MAX_FORM_SPAN_DAYS // 365`).
    This pins the floored word, not a fictional exactness."""
    assert MAX_FORM_SPAN_DAYS // 365 == 5
    assert MAX_FORM_RUNUP_DAYS // 365 == 10


def test_server_docstring_states_the_enforced_span_and_runup():
    span_word = _NUMBER_WORDS[MAX_FORM_SPAN_DAYS // 365]
    text = (ROOT / "src" / "cycling_mcp" / "server.py").read_text(encoding="utf-8")
    assert f"At most {span_word} years per call" in text


def test_coaching_doc_states_the_enforced_span_runup_and_floor():
    span_word = _NUMBER_WORDS[MAX_FORM_SPAN_DAYS // 365]
    runup_word = _NUMBER_WORDS[MAX_FORM_RUNUP_DAYS // 365]
    text = (ROOT / "docs" / "coaching.md").read_text(encoding="utf-8")
    assert f"A window wider than {span_word} years is" in text
    assert f"run-up is cut at {runup_word} years" in text
    assert f"dated before {PLAUSIBLE_DATE_FLOOR.year}" in text

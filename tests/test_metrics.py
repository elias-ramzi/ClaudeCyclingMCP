import json
from pathlib import Path

import pytest

from cycling_mcp.metrics import compute_metrics, describe, normalised_power, power_series
from cycling_mcp.spec import format_duration, load_spec

GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def sweetspot():
    return load_spec(json.loads((GOLDEN / "sweetspot-3x10.json").read_text(encoding="utf-8")))


def test_power_series_has_one_sample_per_second(sweetspot):
    assert len(power_series(sweetspot)) == sweetspot.total_seconds == 4200


def test_duration_total_matches_the_blocks(sweetspot):
    # 20 min ramp + 3x10 min efforts + 2x5 min recoveries + 10 min ramp.
    assert sweetspot.total_seconds == (20 + 30 + 10 + 10) * 60
    assert format_duration(sweetspot.total_seconds) == "1:10:00"


def test_golden_session_reproduces_the_load_mywhoosh_reported(sweetspot):
    """MyWhoosh reported 70:00 / 70 TSS / 0.78 IF for this session on import.

    The tolerance on IF is a rounding allowance, not slack: the model gives
    0.7748, which MyWhoosh displays as 0.78.
    """
    stats = compute_metrics(sweetspot)
    assert stats.total_seconds == 4200
    assert stats.tss == pytest.approx(70, abs=0.5)
    assert stats.intensity_factor == pytest.approx(0.775, abs=0.005)
    assert stats.normalised_power == 198


def test_normalised_power_of_a_constant_effort_is_that_effort():
    assert normalised_power([200.0] * 3600) == pytest.approx(200.0)


def test_normalised_power_exceeds_average_for_a_variable_effort():
    """The whole point of NP: on/off work costs more than its mean suggests."""
    samples = ([300.0] * 60 + [100.0] * 60) * 20
    average = sum(samples) / len(samples)
    assert normalised_power(samples) > average


def test_short_workout_falls_back_to_the_mean():
    assert normalised_power([200.0] * 10) == pytest.approx(200.0)


def test_ramp_is_sampled_linearly():
    workout = load_spec(
        {
            "name": "Ramp",
            "ftp": 200,
            "blocks": [{"type": "ramp", "duration": 100, "from_w": 100, "to_w": 200}],
        }
    )
    samples = power_series(workout)
    assert samples[0] == pytest.approx(100.5)
    assert samples[-1] == pytest.approx(199.5)
    assert sum(samples) / len(samples) == pytest.approx(150.0)


def test_tss_of_an_hour_at_ftp_is_100():
    workout = load_spec(
        {
            "name": "Threshold hour",
            "ftp": 250,
            "blocks": [{"type": "steady", "duration": 3600, "power_pct": 100}],
        }
    )
    stats = compute_metrics(workout)
    assert stats.tss == pytest.approx(100.0, abs=0.1)
    assert stats.intensity_factor == pytest.approx(1.0, abs=0.001)


def test_free_ride_is_flagged_as_an_estimate():
    workout = load_spec(
        {
            "name": "Free",
            "ftp": 250,
            "blocks": [{"type": "free", "duration": 1800}],
        }
    )
    stats = compute_metrics(workout)
    assert stats.contains_free_ride
    assert "approximate" in stats.as_dict()["estimate_note"]


def test_describe_shows_watts_and_totals(sweetspot):
    table = describe(sweetspot)
    assert "232 W (91%)" in table
    assert "130 -> 180 W" in table
    assert "1:10:00 total" in table
    assert "TSS 70" in table


def test_describe_groups_repeats(sweetspot):
    workout = load_spec(
        {
            "name": "Intervals",
            "ftp": 250,
            "blocks": [
                {
                    "type": "repeat",
                    "count": 4,
                    "blocks": [
                        {"type": "steady", "duration": 300, "power_pct": 105},
                        {"type": "steady", "duration": 180, "power_pct": 55},
                    ],
                }
            ],
        }
    )
    table = describe(workout)
    assert "repeat x4" in table

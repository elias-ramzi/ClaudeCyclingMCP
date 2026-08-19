import pytest

from cycling_mcp.metrics import describe
from cycling_mcp.render_zwo import render_zwo
from cycling_mcp.spec import SpecError, load_spec, parse_duration, validate_spec


def base(**overrides):
    spec = {
        "name": "Test",
        "ftp": 250,
        "blocks": [{"type": "steady", "duration": 600, "power_w": 200}],
    }
    spec.update(overrides)
    return spec


# --------------------------------------------------------------------------
# durations
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (600, 600),
        (600.0, 600),
        ("600", 600),
        ("10:00", 600),
        ("05:00", 300),
        ("1:05:00", 3900),
        ("0:30", 30),
    ],
)
def test_parse_duration_forms(raw, expected):
    assert parse_duration(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "1:2:3:4", 60.5, True, None])
def test_parse_duration_rejects_nonsense(raw):
    with pytest.raises(ValueError):
        parse_duration(raw)


# --------------------------------------------------------------------------
# unit conversion
# --------------------------------------------------------------------------


def test_watts_convert_to_fractions_of_ftp():
    workout = load_spec(base(ftp=255, blocks=[{"type": "steady", "duration": 600, "power_w": 232}]))
    block = workout.nodes[0]
    assert block.p_low == pytest.approx(232 / 255)
    assert workout.watts(block.p_low) == 232


def test_percent_converts_to_fractions():
    workout = load_spec(
        base(ftp=250, blocks=[{"type": "steady", "duration": 600, "power_pct": 91}])
    )
    assert workout.nodes[0].p_low == pytest.approx(0.91)
    assert workout.watts(0.91) == 228


def test_watts_and_percent_agree_for_the_same_target():
    from_watts = load_spec(
        base(ftp=200, blocks=[{"type": "steady", "duration": 600, "power_w": 182}])
    )
    from_percent = load_spec(
        base(ftp=200, blocks=[{"type": "steady", "duration": 600, "power_pct": 91}])
    )
    assert from_watts.nodes[0].p_low == pytest.approx(from_percent.nodes[0].p_low)


def test_explicit_range_is_preserved():
    workout = load_spec(base(blocks=[{"type": "steady", "duration": 600, "power_pct": [88, 94]}]))
    block = workout.nodes[0]
    assert (block.p_low, block.p_high) == pytest.approx((0.88, 0.94))
    assert not block.is_scalar_target


# --------------------------------------------------------------------------
# the errors that actually bite
# --------------------------------------------------------------------------


def test_empty_workout_is_rejected():
    _, errors, _ = validate_spec(base(blocks=[]))
    assert any("empty" in e for e in errors)


@pytest.mark.parametrize("duration", [0, -60, "-10:00"])
def test_non_positive_duration_is_rejected(duration):
    _, errors, _ = validate_spec(
        base(blocks=[{"type": "steady", "duration": duration, "power_w": 200}])
    )
    assert any("greater than zero" in e for e in errors)


def test_power_outside_a_sane_fraction_is_rejected():
    # 232 written into a _pct field is 232% of FTP — almost always a unit slip.
    _, errors, _ = validate_spec(
        base(blocks=[{"type": "steady", "duration": 600, "power_pct": 232}])
    )
    assert any("sane range" in e for e in errors)


def test_watts_mistaken_for_a_fraction_is_rejected():
    _, errors, _ = validate_spec(
        base(ftp=250, blocks=[{"type": "steady", "duration": 600, "power_w": 12}])
    )
    assert any("sane range" in e for e in errors)


def test_ramp_with_equal_endpoints_is_rejected():
    _, errors, _ = validate_spec(
        base(blocks=[{"type": "ramp", "duration": 600, "from_w": 200, "to_w": 200}])
    )
    assert any("does not change" in e for e in errors)


def test_descending_ramp_is_accepted():
    workout = load_spec(
        base(blocks=[{"type": "ramp", "duration": 600, "from_w": 200, "to_w": 150}])
    )
    block = workout.nodes[0]
    assert block.p_from > block.p_to


def test_both_unit_forms_on_one_block_is_rejected():
    _, errors, _ = validate_spec(
        base(blocks=[{"type": "steady", "duration": 600, "power_w": 200, "power_pct": 80}])
    )
    assert any("give exactly one" in e for e in errors)


def test_missing_power_is_rejected():
    _, errors, _ = validate_spec(base(blocks=[{"type": "steady", "duration": 600}]))
    assert any("missing power" in e for e in errors)


def test_ftp_is_required_even_when_blocks_are_in_watts():
    spec = base()
    del spec["ftp"]
    _, errors, _ = validate_spec(spec)
    assert any("missing 'ftp'" in e for e in errors)


def test_missing_name_is_rejected():
    spec = base()
    del spec["name"]
    _, errors, _ = validate_spec(spec)
    assert any("missing 'name'" in e for e in errors)


def test_unknown_block_type_is_rejected():
    _, errors, _ = validate_spec(base(blocks=[{"type": "sprint", "duration": 60}]))
    assert any("unknown block type" in e for e in errors)


def test_empty_repeat_is_rejected():
    _, errors, _ = validate_spec(base(blocks=[{"type": "repeat", "count": 3, "blocks": []}]))
    assert any("non-empty" in e for e in errors)


def test_zero_repeat_count_is_rejected():
    _, errors, _ = validate_spec(
        base(
            blocks=[
                {
                    "type": "repeat",
                    "count": 0,
                    "blocks": [{"type": "steady", "duration": 60, "power_w": 200}],
                }
            ]
        )
    )
    assert any("at least 1" in e for e in errors)


def test_nested_repeats_are_rejected():
    _, errors, _ = validate_spec(
        base(
            blocks=[
                {
                    "type": "repeat",
                    "count": 2,
                    "blocks": [{"type": "repeat", "count": 2, "blocks": []}],
                }
            ]
        )
    )
    assert any("cannot be nested" in e for e in errors)


def test_typo_in_a_key_is_warned_about():
    _, errors, warnings = validate_spec(
        base(blocks=[{"type": "steady", "duration": 600, "power_w": 200, "powr_w": 210}])
    )
    assert errors == []
    assert any("powr_w" in w for w in warnings)


def test_load_spec_raises_on_error():
    with pytest.raises(SpecError):
        load_spec(base(blocks=[]))


# --------------------------------------------------------------------------
# roles
# --------------------------------------------------------------------------


def test_roles_are_inferred_at_the_edges():
    workout = load_spec(
        base(
            blocks=[
                {"type": "steady", "duration": 600, "power_w": 150},
                {"type": "steady", "duration": 600, "power_w": 220},
                {"type": "steady", "duration": 600, "power_w": 140},
            ]
        )
    )
    assert [b.role for b in workout.nodes] == ["warmup", "interval", "cooldown"]


def test_explicit_role_is_not_overridden():
    workout = load_spec(
        base(
            blocks=[
                {"type": "steady", "duration": 600, "power_w": 150, "role": "interval"},
                {"type": "steady", "duration": 600, "power_w": 140},
            ]
        )
    )
    assert [b.role for b in workout.nodes] == ["interval", "cooldown"]


def test_repeat_expands_in_execution_order():
    workout = load_spec(
        base(
            blocks=[
                {
                    "type": "repeat",
                    "count": 3,
                    "blocks": [
                        {"type": "steady", "duration": 240, "power_w": 240},
                        {"type": "steady", "duration": 120, "power_w": 140},
                    ],
                }
            ]
        )
    )
    steps = list(workout.steps())
    assert len(steps) == 6
    assert workout.total_seconds == 3 * (240 + 120)


# --- FTP provenance ------------------------------------------------------


def test_ftp_provenance_is_recorded_when_given():
    """Six months on, a workout is raw watts with no record of which FTP made
    them. The spec is the only place that can carry it."""
    workout = load_spec(
        {
            "name": "T",
            "ftp": 255,
            "ftp_source": "athlete_stated",
            "ftp_date": "2026-08-19",
            "blocks": [{"type": "steady", "duration": 600, "power_pct": 90}],
        }
    )
    assert workout.ftp_provenance() == "athlete stated, 2026-08-19"


def test_ftp_provenance_is_absent_when_unrecorded():
    workout = load_spec(
        {"name": "T", "ftp": 255, "blocks": [{"type": "steady", "duration": 600, "power_pct": 90}]}
    )
    assert workout.ftp_provenance() is None


def test_an_unknown_ftp_source_is_an_error_not_a_warning():
    _, errors, _ = validate_spec(
        {
            "name": "T",
            "ftp": 255,
            "ftp_source": "vibes",
            "blocks": [{"type": "steady", "duration": 600, "power_pct": 90}],
        }
    )
    assert any("ftp_source must be one of" in e for e in errors)


def test_provenance_reaches_the_artifacts_the_athlete_reads():
    spec = {
        "name": "T",
        "ftp": 255,
        "ftp_source": "test_result",
        "ftp_date": "2026-08-01",
        "blocks": [{"type": "steady", "duration": 600, "power_pct": 90}],
    }
    workout = load_spec(spec)
    assert "(test result, 2026-08-01)" in describe(workout)
    # A .zwo stores fractions only, so the description is the one field that
    # can still say what they were fractions of.
    assert "Built against FTP 255 W (test result, 2026-08-01)" in render_zwo(workout)[0]

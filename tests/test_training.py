"""The arithmetic the coach layer exists to do, against hand-computed numbers.

Every expected value here was worked out by hand from the formula in the
docstring, not read off the implementation. A test that asserts what the code
currently returns pins a bug as firmly as it pins a feature.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cycling_mcp.training import (
    BLOCK_CLASSES,
    KNOWN_DURATION_VERDICTS,
    KNOWN_VERDICTS,
    classify_block,
    compare_block,
    compute_activity_load,
    form_series,
    hr_tss,
    hr_zones,
    ordinal,
    power_tss,
    power_zones,
)

# --------------------------------------------------------------------------
# zones
# --------------------------------------------------------------------------


def test_an_hour_at_threshold_is_one_hundred_tss():
    """The definition every other load number is calibrated against."""
    assert power_tss(3600, 250, 250) == pytest.approx(100.0)


def test_tss_scales_with_the_square_of_intensity():
    """4200 s at NP 198 against FTP 266: 1.16667 x 0.744361^2 x 100 = 64.642."""
    assert power_tss(4200, 198, 266) == pytest.approx(64.642, abs=0.001)


def test_half_intensity_is_a_quarter_of_the_load():
    assert power_tss(3600, 125, 250) == pytest.approx(25.0)


def test_hr_tss_is_the_same_shape_with_a_heart_rate_ratio():
    """3600 s at 160 bpm against a 170 bpm threshold: (160/170)^2 x 100."""
    assert hr_tss(3600, 160, 170) == pytest.approx(88.58, abs=0.01)


def test_power_zones_are_the_classic_boundaries():
    zones = {zone["zone"]: zone for zone in power_zones(250)}
    assert zones["z2"]["low_w"] == 138 and zones["z2"]["high_w"] == 188  # 55-75%
    # 90-105%. The ceiling is 262.5 W and Python rounds half to even, so 262.
    assert zones["z4"]["low_w"] == 225 and zones["z4"]["high_w"] == 262
    assert zones["z6"]["high_w"] is None, "the top zone has no ceiling"
    assert zones["sweet_spot"]["range_w"] == "220-235 W"


def test_hr_zones_hang_off_threshold_not_maximum():
    zones = {zone["zone"]: zone for zone in hr_zones(170)}
    assert zones["z4"]["low_bpm"] == 160 and zones["z4"]["high_bpm"] == 170
    assert zones["z5c"]["high_bpm"] is None


# --------------------------------------------------------------------------
# which method a load came from
# --------------------------------------------------------------------------


def test_normalised_power_is_preferred_and_says_so():
    load = compute_activity_load(
        {"duration_s": 3600, "normalized_power": 250, "avg_power": 220}, 250, 170
    )
    assert load.method == "power"
    assert load.tss == pytest.approx(100.0)


def test_average_power_is_used_when_np_is_missing_and_flagged():
    load = compute_activity_load({"duration_s": 3600, "avg_power": 220}, 250, 170)
    assert load.method == "power_avg"
    assert "no_normalized_power" in load.flags
    assert "understates" in load.reason


def test_heart_rate_is_the_fallback_not_the_default():
    """Average power with no NP is still power. HR is only for rides with none."""
    load = compute_activity_load({"duration_s": 3600, "avg_power": 220, "avg_hr": 160}, 250, 170)
    assert load.method == "power_avg", "power exists, so HR must not be used"


def test_a_ride_without_power_falls_back_to_heart_rate():
    load = compute_activity_load({"duration_s": 3600, "avg_hr": 160}, 250, 170)
    assert load.method == "hr"
    assert load.tss == pytest.approx(88.58, abs=0.01)
    assert "Not comparable" in load.reason


def test_an_estimated_threshold_hr_is_flagged_through_to_the_result():
    load = compute_activity_load(
        {"duration_s": 3600, "avg_hr": 160}, 250, 175, threshold_hr_estimated=True
    )
    assert "threshold_hr_estimated" in load.flags


def test_a_ride_with_nothing_measurable_returns_null_not_zero():
    """A zero is indistinguishable from a rest day and would drag CTL down."""
    load = compute_activity_load({"duration_s": 3600}, 250, 170)
    assert load.tss is None
    assert load.method == "none"
    assert "neither power nor heart rate" in load.reason


def test_power_without_an_ftp_says_which_half_is_missing():
    load = compute_activity_load({"duration_s": 3600, "normalized_power": 250}, None, None)
    assert load.tss is None
    assert "no FTP is known" in load.reason


# --------------------------------------------------------------------------
# form
# --------------------------------------------------------------------------


def test_ctl_and_atl_after_a_single_hundred_tss_day():
    """CTL = 100/42 = 2.381, ATL = 100/7 = 14.286, and TSB is yesterday's, so 0."""
    day = date(2026, 3, 1)
    points = form_series({day: 100.0}, day, day)
    assert points[0].ctl == pytest.approx(100 / 42, abs=1e-6)
    assert points[0].atl == pytest.approx(100 / 7, abs=1e-6)
    assert points[0].tsb == pytest.approx(0.0)


def test_the_day_after_decays_and_tsb_becomes_yesterdays_balance():
    """CTL x 41/42, ATL x 6/7, TSB = yesterday's CTL - ATL = 2.381 - 14.286."""
    first, second = date(2026, 3, 1), date(2026, 3, 2)
    points = form_series({first: 100.0}, first, second)
    assert points[1].ctl == pytest.approx((100 / 42) * 41 / 42, abs=1e-6)
    assert points[1].atl == pytest.approx((100 / 7) * 6 / 7, abs=1e-6)
    assert points[1].tsb == pytest.approx(100 / 42 - 100 / 7, abs=1e-6)


def test_rest_days_are_stepped_through_not_skipped():
    """The decay is per calendar day; skipping empty days would keep ATL high."""
    start, end = date(2026, 3, 1), date(2026, 3, 8)
    points = form_series({start: 100.0}, start, end)
    assert len(points) == 8
    assert points[-1].atl == pytest.approx((100 / 7) * (6 / 7) ** 7, abs=1e-6)


def test_history_before_the_window_still_builds_ctl():
    """CTL entering a window must come from real rides, not from zero."""
    early, start, end = date(2026, 1, 1), date(2026, 2, 1), date(2026, 2, 1)
    with_history = form_series({early: 100.0}, start, end)
    without = form_series({}, start, end)
    assert with_history[0].ctl > without[0].ctl > -0.000001


def test_a_seed_is_carried_into_the_first_day():
    day = date(2026, 3, 1)
    points = form_series({}, day, day, seed_ctl=60.0, seed_atl=40.0)
    assert points[0].tsb == pytest.approx(20.0)
    assert points[0].ctl == pytest.approx(60.0 - 60.0 / 42, abs=1e-6)


def test_a_steady_hundred_a_day_converges_toward_a_hundred():
    """The long-run behaviour: under constant load, CTL and ATL meet at it."""
    start = date(2026, 1, 1)
    daily = {start + timedelta(days=offset): 100.0 for offset in range(200)}
    points = form_series(daily, start, start + timedelta(days=199))
    # 100 x (1 - (41/42)^200) = 99.19; ATL is long past converged.
    assert points[-1].ctl == pytest.approx(99.19, abs=0.01)
    assert points[-1].atl == pytest.approx(100.0, abs=0.01)
    assert points[-1].tsb == pytest.approx(-0.81, abs=0.02)


# --------------------------------------------------------------------------
# block comparison
# --------------------------------------------------------------------------


def test_a_block_under_target_reads_as_a_sentence():
    comparison = compare_block(2, "interval", 600, 250, 250, {"duration_s": 600, "avg_power": 228})
    assert comparison.verdict == "under"
    assert comparison.sentence == "the second block fell to 228 W against a 250 W target"


def test_a_block_inside_tolerance_is_on_target():
    comparison = compare_block(1, "interval", 600, 250, 250, {"duration_s": 600, "avg_power": 243})
    assert comparison.verdict == "on_target"


def test_a_recovery_ridden_easier_than_target_is_not_a_miss():
    """That target is a ceiling. Spinning under it is the session working."""
    comparison = compare_block(3, "recovery", 300, 140, 140, {"duration_s": 300, "avg_power": 110})
    assert comparison.verdict == "easier_than_target"
    assert "sat at 110 W" in comparison.sentence


def test_a_recovery_ridden_over_target_is_still_a_miss():
    comparison = compare_block(3, "recovery", 300, 140, 140, {"duration_s": 300, "avg_power": 200})
    assert comparison.verdict == "over"


def test_a_block_cut_short_is_reported_even_when_the_power_was_right():
    comparison = compare_block(2, "interval", 600, 250, 250, {"duration_s": 300, "avg_power": 250})
    assert comparison.verdict == "on_target", "the watts were right"
    assert comparison.duration_verdict == "short"
    assert "05:00 ridden against 10:00 planned" in comparison.sentence


def test_a_block_with_no_power_still_has_its_duration_judged():
    """The two verdicts are independent: duration is verifiable without watts,
    and folding it into the power verdict lost it entirely on an HR-only ride."""
    comparison = compare_block(2, "interval", 600, 250, 250, {"duration_s": 300})
    assert comparison.verdict == "no_power"
    assert comparison.duration_verdict == "short"


def test_a_free_block_cut_short_is_still_short():
    comparison = compare_block(1, "warmup", 600, None, None, {"duration_s": 200})
    assert comparison.verdict == "no_target"
    assert comparison.duration_verdict == "short"


def test_a_block_with_no_recorded_power_says_so_rather_than_scoring_it():
    comparison = compare_block(1, "interval", 600, 250, 250, {"duration_s": 600})
    assert comparison.verdict == "no_power"
    assert "cannot be checked" in comparison.sentence


def test_ordinals_stop_pretending_past_ten():
    assert ordinal(1) == "first"
    assert ordinal(10) == "tenth"
    assert ordinal(14) == "number 14"


# --------------------------------------------------------------------------
# the compliance partition
# --------------------------------------------------------------------------


def _verdicts_compare_block_can_produce() -> tuple[set[str], set[str]]:
    """Every verdict pair the comparison actually emits, driven not enumerated.

    The vocabulary lists exist to make `classify_block` closed-world; if
    `compare_block` learns a verdict they do not know about, that is the whole
    failure this partition is meant to make loud rather than silent.
    """
    laps = (
        {"duration_s": 600, "avg_power": 250},  # on target, on time
        {"duration_s": 600, "avg_power": 180},  # under
        {"duration_s": 600, "avg_power": 320},  # over
        {"duration_s": 200, "avg_power": 250},  # short
        {"duration_s": 900, "avg_power": 250},  # long
        {"duration_s": 600, "avg_power": 100},  # under a ceiling: easier_than_target
        {"duration_s": 600},  # no power
        {},  # no power, unknown duration
    )
    verdicts, durations = set(), set()
    for role, low, high in (("interval", 250, 250), ("recovery", 150, 150), ("warmup", None, None)):
        for lap in laps:
            comparison = compare_block(1, role, 600, low, high, lap).as_dict()
            verdicts.add(comparison["verdict"])
            durations.add(comparison["duration_verdict"])
    return verdicts, durations


def test_the_vocabulary_covers_every_verdict_the_comparison_emits():
    verdicts, durations = _verdicts_compare_block_can_produce()
    assert verdicts == set(KNOWN_VERDICTS)
    assert durations == set(KNOWN_DURATION_VERDICTS)


def test_every_verdict_pair_lands_in_exactly_one_class():
    """The previous version enumerated the positive buckets and let the rest
    fall through, so `no_target` over `unknown` — a free block whose lap carried
    no time — counted as evidence the session went to plan."""
    for verdict in KNOWN_VERDICTS:
        for duration_verdict in KNOWN_DURATION_VERDICTS:
            block = {"verdict": verdict, "duration_verdict": duration_verdict}
            assert classify_block(block) in BLOCK_CLASSES


def test_an_unknown_duration_makes_a_block_unverifiable():
    assert classify_block({"verdict": "no_target", "duration_verdict": "unknown"}) == "unverifiable"
    assert classify_block({"verdict": "on_target", "duration_verdict": "unknown"}) == "unverifiable"


def test_a_deviation_on_either_axis_outranks_an_unverifiable_one():
    assert classify_block({"verdict": "no_power", "duration_verdict": "short"}) == "deviating"
    assert classify_block({"verdict": "over", "duration_verdict": "unknown"}) == "deviating"


def test_only_a_block_checked_on_both_axes_is_compliant():
    assert classify_block({"verdict": "on_target", "duration_verdict": "on_time"}) == "compliant"
    assert (
        classify_block({"verdict": "easier_than_target", "duration_verdict": "on_time"})
        == "compliant"
    )
    assert classify_block({"verdict": "no_target", "duration_verdict": "on_time"}) == "compliant"
    assert classify_block({"verdict": "no_power", "duration_verdict": "on_time"}) == "unverifiable"


def test_a_verdict_nothing_knows_about_raises_rather_than_passing():
    """A verdict added to `compare_block` later must not fall through to
    "nothing wrong here"."""
    with pytest.raises(ValueError, match="unrecognised block verdict"):
        classify_block({"verdict": "sandbagged", "duration_verdict": "on_time"})
    with pytest.raises(ValueError, match="unrecognised block duration_verdict"):
        classify_block({"verdict": "on_target", "duration_verdict": "eventually"})

"""The nutrition layer end to end: resolve, compute, freeze, refuse.

The cases here are the ones that would be wrong in a way nobody would notice —
a day's calories that silently exclude an unpriced item, a protein target hit
with collagen, a log entry rewritten by a price correction made weeks later, a
calorie target computed against a race day as though it were a rest day.

Every expected total below is computed by hand in the test, not read back from
the code under test.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest

from cycling_mcp import coach, nutrition, store

# 63 kcal / 11 g protein per 100 g; a 400 g package at EUR 2.40 is EUR 0.60/100 g.
SKYR = {
    "name": "Skyr nature 0%",
    "aliases": ["skyr"],
    "kcal_100g": 63,
    "protein_100g": 11,
    "fiber_100g": 0,
    "default_portion_g": 200,
    "portion_label": "1 pot = 200 g",
    "package_price": 2.40,
    "package_weight_g": 400,
}
CRUESLI = {
    "name": "Cruesli",
    "kcal_100g": 450,
    "protein_100g": 8,
    "fiber_100g": 6,
    "note": "weigh it — this is where eyeballing drifts",
}
# An incomplete protein: all of its calories count, none of its protein does.
COLLAGEN = {
    "name": "Collagene",
    "aliases": ["collagen"],
    "kcal_100g": 360,
    "protein_100g": 90,
    "fiber_100g": 0,
    "counts_toward_protein": False,
}
RAW_RICE = {
    "name": "Riz basmati cru",
    "state": "raw",
    "kcal_100g": 350,
    "protein_100g": 7.5,
    "fiber_100g": 1.3,
}

LONG_RIDE = {
    "name": "Long endurance",
    "ftp": 266,
    "blocks": [{"type": "steady", "duration": 14400, "power_pct": 65, "role": "interval"}],
}


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setenv(store.ENV_DB_PATH, str(tmp_path / "coach.db"))
    return tmp_path / "coach.db"


@pytest.fixture
def athlete():
    """A complete profile, which is what a calorie target needs before it exists."""
    coach.update_profile(height_cm=178, birth_year=1995, gender="male")
    coach.log_weight(value_kg=72.0, effective_date="2026-08-01")


@pytest.fixture
def base():
    nutrition.add_ingredients([SKYR, CRUESLI, COLLAGEN, RAW_RICE])


# --------------------------------------------------------------------------
# the ingredient base
# --------------------------------------------------------------------------


def test_a_bulk_paste_accepts_the_good_rows_and_says_why_for_the_rest():
    result = nutrition.add_ingredients([SKYR, {"name": "no macros"}, CRUESLI])
    assert result["inserted"] == 2
    assert result["rejected"] == 1
    assert "kcal_100g is required" in result["rejections"][0]["reason"]
    assert result["rejections"][0]["name"] == "no macros"


def test_a_duplicate_name_is_reported_rather_than_overwriting_what_is_stored(base):
    result = nutrition.add_ingredients([{**SKYR, "kcal_100g": 200}])
    assert result["inserted"] == 0
    assert "already stored" in result["rejections"][0]["reason"]
    stored = nutrition.search_ingredients("skyr")["matches"][0]
    assert stored["kcal_100g"] == 63


def test_cost_per_100g_is_derived_from_the_package_not_stored(base):
    stored = nutrition.search_ingredients("skyr")["matches"][0]
    assert stored["cost_per_100g"] == pytest.approx(0.60)


def test_a_raw_ingredient_warns_about_the_weight_it_will_be_logged_at():
    result = nutrition.add_ingredients([RAW_RICE])
    assert any("cooked starch" in warning for warning in result["warnings"])


def test_search_is_blind_to_accents_and_casing(base):
    for spelling in ("COLLAGÈNE", "collagene", "Collagène"):
        matches = nutrition.search_ingredients(spelling)
        assert matches["exact_matches"] == 1, spelling


# --------------------------------------------------------------------------
# macros, by hand
# --------------------------------------------------------------------------


def test_a_days_macros_and_cost_match_the_arithmetic_done_by_hand(athlete, base):
    """200 g skyr + 50 g cruesli + 10 g collagen, each computed from the label."""
    nutrition.log_food(
        [
            {"ingredient": "skyr", "grams": 200},
            {"ingredient": "Cruesli", "grams": 50},
            {"ingredient": "collagen", "grams": 10},
        ],
        log_date="2026-08-24",
        slot="breakfast",
    )
    totals = nutrition.day_summary("2026-08-24")["totals"]

    # kcal: 63*2 + 450*0.5 + 360*0.1 = 126 + 225 + 36
    assert totals["kcal"] == pytest.approx(387.0)
    # protein: 11*2 + 8*0.5 = 22 + 4. The collagen's 9 g is excluded.
    assert totals["protein_g"] == pytest.approx(26.0)
    assert totals["protein_g_excluded"] == pytest.approx(9.0)
    # fibre: 6*0.5
    assert totals["fiber_g"] == pytest.approx(3.0)
    # cost: only the skyr carries a price — 0.60/100 g x 200 g
    assert totals["cost"] == pytest.approx(1.20)
    assert totals["cost_unpriced_entries"] == 2


def test_an_incomplete_protein_counts_in_kcal_and_never_in_protein(athlete, base):
    nutrition.log_food([{"ingredient": "collagen", "grams": 20}], log_date="2026-08-24")
    summary = nutrition.day_summary("2026-08-24")
    assert summary["totals"]["kcal"] == pytest.approx(72.0)
    assert summary["totals"]["protein_g"] == 0.0
    assert summary["totals"]["protein_g_excluded"] == pytest.approx(18.0)
    assert "Collagene" in summary["protein_note"]


def test_an_unknown_optional_macro_is_null_not_zero(base):
    """A carbohydrate total of 0 for a day of cereal is a wrong number that looks real."""
    nutrition.log_food([{"ingredient": "Cruesli", "grams": 50}], log_date="2026-08-24")
    totals = nutrition.day_summary("2026-08-24")["totals"]
    assert totals["carbs_g"] is None
    assert totals["carbs_g_missing_entries"] == 1


def test_a_portion_uses_the_stored_default_and_a_missing_one_is_refused(base):
    result = nutrition.log_food(
        [{"ingredient": "skyr", "portions": 1}, {"ingredient": "Cruesli", "portions": 1}],
        log_date="2026-08-24",
    )
    assert result["logged"] == 1
    assert result["entries"][0]["grams"] == pytest.approx(200.0)
    assert "no default portion stored" in result["rejections"][0]["reason"]


def test_grams_and_portions_together_are_refused_rather_than_reconciled(base):
    result = nutrition.log_food(
        [{"ingredient": "skyr", "grams": 150, "portions": 1}], log_date="2026-08-24"
    )
    assert result["logged"] == 0
    assert "not both" in result["rejections"][0]["reason"]


# --------------------------------------------------------------------------
# name resolution
# --------------------------------------------------------------------------


def test_a_near_miss_is_rejected_with_suggestions_never_guessed(base):
    result = nutrition.log_food([{"ingredient": "skyrr", "grams": 200}], log_date="2026-08-24")
    assert result["logged"] == 0
    reason = result["rejections"][0]["reason"]
    assert "no ingredient named" in reason
    assert "Skyr nature 0%" in reason
    assert nutrition.day_summary("2026-08-24")["entry_count"] == 0


def test_an_alias_shared_by_two_ingredients_is_ambiguous_not_a_coin_toss():
    nutrition.add_ingredients(
        [
            {
                "name": "Riz cru",
                "aliases": ["riz"],
                "kcal_100g": 350,
                "protein_100g": 7,
                "fiber_100g": 1,
            },
            {
                "name": "Riz cuit",
                "aliases": ["riz"],
                "kcal_100g": 130,
                "protein_100g": 2.6,
                "fiber_100g": 0.4,
            },
        ]
    )
    result = nutrition.log_food([{"ingredient": "riz", "grams": 100}], log_date="2026-08-24")
    assert result["logged"] == 0
    assert "matches more than one ingredient" in result["rejections"][0]["reason"]


def test_one_rejected_entry_does_not_lose_the_others_in_the_same_call(base):
    result = nutrition.log_food(
        [{"ingredient": "skyr", "grams": 200}, {"ingredient": "nothing like this", "grams": 10}],
        log_date="2026-08-24",
    )
    assert result["logged"] == 1
    assert result["rejected"] == 1


# --------------------------------------------------------------------------
# meals
# --------------------------------------------------------------------------


def test_a_meal_expands_into_its_ingredients_with_overrides_applied(athlete, base):
    nutrition.save_meal(
        "Petit-dej",
        [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}],
        default_for_slot="breakfast",
    )
    result = nutrition.log_meal(
        "Petit-dej", log_date="2026-08-24", overrides=[{"ingredient": "Cruesli", "grams": 30}]
    )
    assert result["logged"] == 2
    assert result["slot"] == "breakfast"
    # 63*2 + 450*0.3 = 126 + 135
    assert result["totals"]["kcal"] == pytest.approx(261.0)
    # 11*2 + 8*0.3 = 22 + 2.4
    assert result["totals"]["protein_g"] == pytest.approx(24.4)
    assert result["overrides_applied"] == [{"name": "Cruesli", "from_g": 50.0, "to_g": 30.0}]
    grouped = nutrition.day_summary("2026-08-24")["slots"][0]["meals"]
    assert grouped[0]["meal_name"] == "Petit-dej"
    assert len(grouped[0]["entries"]) == 2


def test_an_override_of_zero_leaves_the_ingredient_out_of_todays_log(base):
    nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}]
    )
    result = nutrition.log_meal(
        "Petit-dej", log_date="2026-08-24", overrides=[{"ingredient": "Cruesli", "grams": 0}]
    )
    assert result["logged"] == 1
    assert result["omitted"] == ["Cruesli"]


def test_overriding_something_the_meal_does_not_contain_is_refused(base):
    nutrition.save_meal("Petit-dej", [{"ingredient": "skyr", "grams": 200}])
    with pytest.raises(nutrition.NutritionError, match="is not part of"):
        nutrition.log_meal(
            "Petit-dej", log_date="2026-08-24", overrides=[{"ingredient": "Cruesli", "grams": 30}]
        )


def test_a_meals_macros_follow_a_corrected_ingredient(base):
    """The opposite rule to a log entry: a meal is a recipe, not a measurement."""
    nutrition.save_meal("Petit-dej", [{"ingredient": "skyr", "grams": 200}])
    nutrition.update_ingredient(name="skyr", kcal_100g=70)
    meal = nutrition.list_meals()["meals"][0]
    assert meal["totals"]["kcal"] == pytest.approx(140.0)


# --------------------------------------------------------------------------
# the freeze
# --------------------------------------------------------------------------


def test_editing_an_ingredient_never_rewrites_what_was_already_logged(athlete, base):
    nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    before = nutrition.day_summary("2026-08-24")["totals"]

    result = nutrition.update_ingredient(name="skyr", kcal_100g=200, package_price=9.60)
    assert "1 existing log entry kept the macros" in result["history_note"]

    after = nutrition.day_summary("2026-08-24")["totals"]
    assert after["kcal"] == before["kcal"] == pytest.approx(126.0)
    assert after["cost"] == before["cost"] == pytest.approx(1.20)

    # The correction applies from the next entry onward.
    nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-25")
    assert nutrition.day_summary("2026-08-25")["totals"]["kcal"] == pytest.approx(400.0)
    assert nutrition.day_summary("2026-08-25")["totals"]["cost"] == pytest.approx(4.80)


def test_a_renamed_ingredient_leaves_old_entries_readable(base):
    nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    nutrition.update_ingredient(name="skyr", new_name="Skyr Lidl 0%")
    entry = nutrition.day_summary("2026-08-24")["slots"][0]["entries"][0]
    assert entry["label"] == "Skyr nature 0%"


def test_restating_a_weight_recomputes_that_entry_and_nothing_else(base):
    logged = nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    entry_id = logged["entries"][0]["id"]
    result = nutrition.edit_log_entry(entry_id, grams=100)
    assert result["entry"]["kcal"] == pytest.approx(63.0)
    assert result["days"]["2026-08-24"]["totals"]["kcal"] == pytest.approx(63.0)


def test_an_estimates_quantity_cannot_be_recomputed_because_nothing_backs_it(base):
    logged = nutrition.log_food(
        [{"label": "canteen plate", "kcal": 700, "is_estimate": True}], log_date="2026-08-24"
    )
    with pytest.raises(nutrition.NutritionError, match="free-form estimate"):
        nutrition.edit_log_entry(logged["entries"][0]["id"], grams=300)


# --- round-8 finding 3, gate-4 survivor: edit_log_entry's own note blanking.
# `note=""` passed the `is not None` gate, `_text("")` folded to None, and the
# UPDATE wrote NULL over the stored food_log.note — a log entry is a frozen
# measurement (CLAUDE.md), and unlike update_ingredient/save_meal this tool had
# no clear=[...] verb at all, so the blank was not even the near-miss of one. ---


def test_edit_log_entry_blank_note_does_not_erase_it_and_is_reported(base):
    """Pinned regression: `edit_log_entry(id, note="")` used to silently null
    the stored note and report `updated_fields: ["note"]` as if intended."""
    logged = nutrition.log_food(
        [{"ingredient": "skyr", "grams": 200, "note": "weighed after draining"}],
        log_date="2026-08-24",
    )
    entry_id = logged["entries"][0]["id"]

    result = nutrition.edit_log_entry(entry_id, note="")
    assert result["entry"]["note"] == "weighed after draining"
    assert result["ignored_blank_fields"] == ["note"]
    assert "note" not in result["updated_fields"]

    result = nutrition.edit_log_entry(entry_id, note=" ")
    assert result["entry"]["note"] == "weighed after draining"
    assert result["ignored_blank_fields"] == ["note"]
    assert "note" not in result["updated_fields"]


def test_edit_log_entry_blank_note_alongside_a_real_change_still_rewrites_the_change(base):
    logged = nutrition.log_food(
        [{"ingredient": "skyr", "grams": 200, "note": "weighed after draining"}],
        log_date="2026-08-24",
    )
    entry_id = logged["entries"][0]["id"]

    result = nutrition.edit_log_entry(entry_id, grams=150, note="")
    assert result["entry"]["grams"] == pytest.approx(150.0)
    assert result["entry"]["kcal"] == pytest.approx(94.5)
    assert result["entry"]["note"] == "weighed after draining"
    assert result["ignored_blank_fields"] == ["note"]
    assert "grams" in result["updated_fields"]
    assert "note" not in result["updated_fields"]


def test_edit_log_entry_note_of_x_is_stored(base):
    """Value just outside the blank guard."""
    logged = nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    entry_id = logged["entries"][0]["id"]
    result = nutrition.edit_log_entry(entry_id, note="x")
    assert result["entry"]["note"] == "x"
    assert result["updated_fields"] == ["note"]
    assert "ignored_blank_fields" not in result


def test_edit_log_entry_with_nothing_given_refuses_nothing_to_change(base):
    logged = nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    entry_id = logged["entries"][0]["id"]
    with pytest.raises(nutrition.NutritionError, match="nothing to change"):
        nutrition.edit_log_entry(entry_id)


def test_edit_log_entry_blank_only_call_does_not_raise_nothing_to_change(base):
    """A blank-only call must not hit the empty-updates refusal — it is a
    no-op the caller was told about, not a missing argument."""
    logged = nutrition.log_food(
        [{"ingredient": "skyr", "grams": 200, "note": "weighed after draining"}],
        log_date="2026-08-24",
    )
    entry_id = logged["entries"][0]["id"]
    result = nutrition.edit_log_entry(entry_id, note="")
    assert result["ignored_blank_fields"] == ["note"]
    assert result["entry"]["note"] == "weighed after draining"


def test_edit_log_entry_clear_note_nulls_it(base):
    logged = nutrition.log_food(
        [{"ingredient": "skyr", "grams": 200, "note": "weighed after draining"}],
        log_date="2026-08-24",
    )
    entry_id = logged["entries"][0]["id"]
    result = nutrition.edit_log_entry(entry_id, clear=["note"])
    assert result["cleared_fields"] == ["note"]
    assert result["entry"]["note"] is None


def test_edit_log_entry_value_and_clear_of_note_raises_the_shared_refusal(base):
    """The identical both-given refusal `_clear_fields` raises everywhere else."""
    logged = nutrition.log_food(
        [{"ingredient": "skyr", "grams": 200, "note": "weighed after draining"}],
        log_date="2026-08-24",
    )
    entry_id = logged["entries"][0]["id"]
    with pytest.raises(nutrition.NutritionError, match="given both a new value and a request"):
        nutrition.edit_log_entry(entry_id, note="y", clear=["note"])
    # Nothing was written by the refused call.
    entry = nutrition.day_summary("2026-08-24")["slots"][0]["entries"][0]
    assert entry["note"] == "weighed after draining"


def test_edit_log_entry_clear_bogus_field_refuses_and_writes_nothing(base):
    logged = nutrition.log_food(
        [{"ingredient": "skyr", "grams": 200, "note": "weighed after draining"}],
        log_date="2026-08-24",
    )
    entry_id = logged["entries"][0]["id"]
    with pytest.raises(nutrition.NutritionError, match="cannot clear"):
        nutrition.edit_log_entry(entry_id, clear=["bogus"])
    entry = nutrition.day_summary("2026-08-24")["slots"][0]["entries"][0]
    assert entry["note"] == "weighed after draining"


# --------------------------------------------------------------------------
# estimates
# --------------------------------------------------------------------------


def test_an_estimate_is_flagged_counted_in_kcal_and_never_priced(base):
    nutrition.log_food(
        [
            {"ingredient": "skyr", "grams": 200},
            {
                "label": "canteen: chicken and chips",
                "kcal": 700,
                "protein_g": 35,
                "fiber_g": 5,
                "is_estimate": True,
                "slot": "lunch",
            },
        ],
        log_date="2026-08-24",
    )
    summary = nutrition.day_summary("2026-08-24")
    assert summary["totals"]["kcal"] == pytest.approx(826.0)
    assert summary["totals"]["protein_g"] == pytest.approx(57.0)
    # Only the skyr is priced; the estimate contributes nothing to cost.
    assert summary["totals"]["cost"] == pytest.approx(1.20)
    assert len(summary["estimates"]) == 1
    assert "approximate" in summary["estimates_note"]


def test_an_estimate_never_pulls_the_weeks_food_cost_down(athlete, base):
    nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    nutrition.log_food(
        [{"label": "restaurant", "kcal": 1200, "is_estimate": True}], log_date="2026-08-25"
    )
    week = nutrition.week_summary("2026-08-24", "2026-08-30")
    assert week["cost"]["total"] == pytest.approx(1.20)
    assert week["cost"]["unpriced_entries"] == 1
    assert "the real figure is higher" in week["cost"]["note"]


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------


def test_bmr_matches_mifflin_st_jeor_by_hand():
    # 10*72 + 6.25*178 - 5*31 + 5 = 720 + 1112.5 - 155 + 5
    assert nutrition.mifflin_st_jeor(72.0, 178.0, 31, "male") == pytest.approx(1682.5)
    assert nutrition.mifflin_st_jeor(72.0, 178.0, 31, "female") == pytest.approx(1516.5)


def test_a_target_is_bmr_times_the_baseline_plus_the_ride(athlete):
    """The whole chain, hand-computed, against an imported ride's own calories."""
    coach.import_activities(
        [
            {
                "activityId": 9001,
                "activityName": "Endurance",
                "activityType": {"typeKey": "cycling"},
                "startTimeLocal": "2026-08-24 09:00:00",
                "duration": 5400.0,
                "calories": 900.0,
            }
        ]
    )
    day = nutrition.suggest_targets(date_str="2026-08-24")["days"][0]

    assert day["day_type"] == "training"
    # BMR 1682.5 x 1.3 = 2187.25, + 900 measured = 3087.25
    assert day["working"]["bmr_kcal"] == 1682
    assert day["working"]["baseline_kcal"] == 2187
    assert day["working"]["exercise_kcal"] == 900
    assert day["working"]["exercise_source"] == "imported_activity"
    assert day["suggested"]["kcal"] == 3087
    # 2 g/kg of 72 kg, rounded to 5 g
    assert day["suggested"]["protein_g"] == 145.0
    assert day["suggested"]["fiber_g"] == 30.0
    assert any("Mifflin-St Jeor" in step for step in day["steps"])


def test_the_goals_rate_becomes_a_daily_deficit(athlete):
    nutrition.set_goal(
        "lose", target_weight_kg=68, rate_kg_per_week=-0.5, effective_date="2026-08-01"
    )
    day = nutrition.suggest_targets(date_str="2026-08-24")["days"][0]
    # -0.5 kg/week x 7700 kcal/kg / 7 days = -550 kcal/day
    assert day["working"]["goal_adjustment_kcal"] == -550


def test_a_race_sets_race_on_its_date_and_race_eve_the_day_before(athlete):
    coach.add_event(name="La Bisou", event_date="2026-09-27", priority="A")
    nutrition.set_goal("lose", rate_kg_per_week=-0.5, effective_date="2026-08-01")

    eve = nutrition.suggest_targets(date_str="2026-09-26")["days"][0]
    race = nutrition.suggest_targets(date_str="2026-09-27")["days"][0]
    ordinary = nutrition.suggest_targets(date_str="2026-09-20")["days"][0]

    assert eve["day_type"] == "race_eve"
    assert race["day_type"] == "race"
    assert ordinary["day_type"] == "rest"

    # Fibre is reduced on both, and the deficit is withheld on both.
    for day in (eve, race):
        assert day["suggested"]["fiber_g"] == 15.0
        assert day["working"]["goal_adjustment_kcal"] == 0
        assert day["working"]["goal_adjustment_withheld_kcal"] == -550
        assert any("not applied" in note or "withheld" in note for note in day["notes"])
    assert ordinary["suggested"]["fiber_g"] == 30.0


def test_a_long_planned_session_makes_the_day_a_big_session_and_estimates_its_cost(athlete):
    coach.save_planned_workouts([{"spec": LONG_RIDE, "scheduled_date": "2026-09-05"}])
    nutrition.set_goal("lose", rate_kg_per_week=-0.5, effective_date="2026-08-01")
    day = nutrition.suggest_targets(date_str="2026-09-05")["days"][0]

    assert day["day_type"] == "big_session"
    assert day["working"]["exercise_source"] == "planned_workout"
    # 4 h at 65% of 266 W = 172.9 W -> 2489 kJ of work / (0.24 x 4.184) kcal
    assert day["working"]["exercise_kcal"] == pytest.approx(2479, abs=15)
    # A big session is not a deficit day.
    assert day["working"]["goal_adjustment_kcal"] == 0


def test_an_unlinked_plan_next_to_a_measured_ride_sums_rather_than_vanishes(athlete):
    """A measurement wins over a model for the SAME session, but an unlinked planned
    session is not assumed to be that same session just because an import landed the
    same day — it is added, not discarded. See item (4)."""
    coach.save_planned_workouts([{"spec": LONG_RIDE, "scheduled_date": "2026-08-20"}])
    coach.import_activities(
        [
            {
                "activityId": 9002,
                "activityType": {"typeKey": "cycling"},
                "startTimeLocal": "2026-08-20 09:00:00",
                "duration": 9000.0,
                "calories": 1800.0,
            }
        ]
    )
    day = nutrition.suggest_targets(date_str="2026-08-20")["days"][0]
    long_ride_estimate = next(
        p["estimated_kcal"] for p in day["training"]["planned"] if p["name"] == "Long endurance"
    )
    assert day["working"]["exercise_source"] == "imported_activity+planned_workout"
    assert day["working"]["exercise_kcal"] == 1800 + long_ride_estimate
    assert any("Long endurance" in note for note in day["notes"])
    # The estimate is still reported beside it, so a plan not followed is visible.
    assert day["training"]["planned_exercise_kcal"] is not None


def test_a_single_unlinked_plan_is_added_even_with_only_one_import_that_day(athlete):
    """The no-double-count case is an explicit link (see the sibling test right below),
    never an unclaimed-activity-count heuristic: one planned session and one unlinked
    import must still sum, not silently absorb the plan."""
    coach.save_planned_workouts([{"spec": LONG_RIDE, "scheduled_date": "2026-08-21"}])
    coach.import_activities([_cycling_activity(9003, "2026-08-21", 3600.0, 600.0)])

    day = nutrition.suggest_targets(date_str="2026-08-21")["days"][0]
    long_ride_estimate = next(
        p["estimated_kcal"] for p in day["training"]["planned"] if p["name"] == "Long endurance"
    )
    assert day["working"]["exercise_source"] == "imported_activity+planned_workout"
    assert day["working"]["exercise_kcal"] == 600 + long_ride_estimate
    assert any("Long endurance" in note for note in day["notes"])


def test_a_target_below_bmr_is_clamped_and_says_the_rate_is_what_should_change(athlete):
    nutrition.set_goal("lose", rate_kg_per_week=-2.0, effective_date="2026-08-01")
    day = nutrition.suggest_targets(date_str="2026-08-24")["days"][0]
    # 2187 - 2200 would be below zero of a day; the clamp puts it at BMR.
    assert day["working"]["clamped_to_bmr"] is True
    assert day["suggested"]["kcal"] == day["working"]["bmr_kcal"]
    assert any("too aggressive" in note for note in day["notes"])


def test_an_incomplete_profile_returns_what_is_missing_rather_than_a_guess():
    coach.log_weight(value_kg=72.0, effective_date="2026-08-01")
    day = nutrition.suggest_targets(date_str="2026-08-24")["days"][0]
    assert set(day["missing"]) == {"height_cm", "birth_year", "gender"}
    assert "suggested" not in day


def test_suggesting_stores_nothing_until_it_is_confirmed(athlete):
    nutrition.suggest_targets(date_str="2026-08-24")
    assert nutrition.day_summary("2026-08-24")["targets"] is None

    nutrition.confirm_targets(date_str="2026-08-24")
    stored = nutrition.day_summary("2026-08-24")["targets"]
    assert stored["source"] == "confirmed"
    assert stored["kcal"] == 2187


def test_an_override_is_recorded_as_one(athlete):
    result = nutrition.confirm_targets(date_str="2026-08-24", kcal=2400)
    assert result["targets"][0]["source"] == "overridden"
    assert result["targets"][0]["kcal"] == 2400
    assert result["targets"][0]["overrode"]["kcal"] == 2187


def test_confirming_a_target_under_bmr_is_refused_whoever_asked(athlete):
    result = nutrition.confirm_targets(date_str="2026-08-24", kcal=1200)
    assert result["stored"] == 0
    reason = result["rejections"][0]["reason"]
    assert "below this athlete's computed resting metabolic rate" in reason
    assert "dietitian" in reason


def test_a_lose_goal_refuses_a_positive_rate(athlete):
    with pytest.raises(nutrition.NutritionError, match="wrong way round"):
        nutrition.set_goal("lose", rate_kg_per_week=0.5)


def test_setting_a_goal_closes_the_previous_one_rather_than_deleting_it(athlete):
    nutrition.set_goal("lose", rate_kg_per_week=-0.5, effective_date="2026-08-01")
    nutrition.set_goal("maintain", effective_date="2026-09-01")
    goal = nutrition.get_goal()
    assert goal["active"]["goal_type"] == "maintain"
    assert [row["status"] for row in goal["history"]] == ["active", "abandoned"]


# --------------------------------------------------------------------------
# the day and the week
# --------------------------------------------------------------------------


def test_the_remainder_is_the_target_minus_what_was_eaten(athlete, base):
    nutrition.confirm_targets(date_str="2026-08-24")
    nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    summary = nutrition.day_summary("2026-08-24")
    assert summary["remaining"]["kcal"] == pytest.approx(2187 - 126)
    assert summary["remaining"]["protein_g"] == pytest.approx(145 - 22)
    assert summary["remaining"]["fiber_g"] == pytest.approx(30.0)


def test_a_day_with_no_targets_says_so_instead_of_inventing_a_remainder(base):
    nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    summary = nutrition.day_summary("2026-08-24")
    assert summary["remaining"] is None
    assert "suggest_targets" in summary["targets_note"]


def test_the_week_averages_only_the_days_that_were_logged(athlete, base):
    nutrition.confirm_targets(days=[{"date": "2026-08-24"}, {"date": "2026-08-25"}])
    nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    nutrition.log_food([{"ingredient": "Cruesli", "grams": 100}], log_date="2026-08-25")

    week = nutrition.week_summary("2026-08-24", "2026-08-30")
    # (126 + 450) / 2 logged days, not / 7 calendar days.
    assert week["averages"]["kcal_per_day"] == pytest.approx(288.0)
    assert week["averages"]["logged_days"] == 2
    assert len(week["unlogged_days"]) == 5
    assert "counted as zero calories" in week["unlogged_note"]
    assert "weekly average" in week["judged_on"]


def test_the_weight_trend_is_the_moving_average_not_the_scale(athlete):
    nutrition.set_goal("lose", rate_kg_per_week=-0.5, effective_date="2026-08-01")
    for day, kg in (
        ("2026-08-17", 72.0),
        ("2026-08-18", 72.4),
        ("2026-08-19", 71.8),
        ("2026-08-24", 71.5),
        ("2026-08-25", 71.9),
        ("2026-08-26", 71.3),
    ):
        coach.log_weight(value_kg=kg, effective_date=day)

    trend = nutrition.week_summary("2026-08-24", "2026-08-30")["weight_trend"]
    assert trend["moving_average_days"] == 7
    assert trend["goal_kg_per_week"] == -0.5
    # Falling, and read off the mean rather than the last morning's number.
    assert trend["kg_per_week"] < 0
    assert trend["latest_mean_kg"] != 71.3


def test_two_weigh_ins_are_not_a_trend_and_it_says_so(athlete):
    coach.log_weight(value_kg=72.0, effective_date="2026-08-24")
    trend = nutrition.week_summary("2026-08-24", "2026-08-30")["weight_trend"]
    assert trend["trend"] is None
    assert "Not enough weigh-ins" in trend["note"]


def test_a_backwards_or_oversized_range_is_refused():
    with pytest.raises(nutrition.NutritionError, match="is before"):
        nutrition.week_summary("2026-08-30", "2026-08-24")
    with pytest.raises(nutrition.NutritionError, match="walks at most"):
        nutrition.week_summary("2026-01-01", "2026-12-31")


# --------------------------------------------------------------------------
# the shared database
# --------------------------------------------------------------------------


def test_the_nutrition_tables_arrive_by_migration_from_the_coach_schema(tmp_path, monkeypatch):
    """A database built by the coach layer alone must gain these, not need rebuilding."""
    path = tmp_path / "existing.db"
    monkeypatch.setenv(store.ENV_DB_PATH, str(path))

    import sqlite3

    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    store.schema_version(conn)
    for version, statements in store.MIGRATIONS:
        if version > 3:
            break
        for statement in statements():
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-01-01T00:00:00Z')",
            (version,),
        )
    conn.execute(
        "INSERT INTO athlete (athlete_id, height_cm, created_at, updated_at) "
        "VALUES (1, 178, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.close()

    with store.open_db() as conn:
        assert store.schema_version(conn) == store.CURRENT_SCHEMA_VERSION
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert {
        "ingredients",
        "meals",
        "meal_items",
        "food_log",
        "daily_targets",
        "nutrition_goals",
    } <= tables

    # The existing athlete survived, and gained the column BMR needs.
    profile = coach.get_profile()
    assert profile["athlete"]["height_cm"] == 178
    assert profile["athlete"]["gender"] is None
    assert "gender" in {gap["field"] for gap in profile["gaps"]}


def test_the_garmin_import_stores_the_calories_targets_are_built_on():
    result = coach.import_activities(
        [
            {
                "activityId": 9003,
                "activityType": {"typeKey": "cycling"},
                "startTimeLocal": "2026-08-24 09:00:00",
                "duration": 3600.0,
                "calories": 750.0,
            }
        ]
    )
    assert result["activities"]["inserted"][0]["calories"] == pytest.approx(750.0)


def test_an_export_carries_the_nutrition_tables_too(athlete, base):
    """A restore that silently dropped the food base would be discovered too late."""
    nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    counts = coach.export_data()["counts"]
    assert counts["ingredients"] == 4
    assert counts["food_log"] == 1
    assert "daily_targets" in counts


# --------------------------------------------------------------------------
# review round 7 — the target-arithmetic family
# --------------------------------------------------------------------------

EVENING_INTERVALS = {
    "name": "Evening intervals",
    "ftp": 250,
    "blocks": [{"type": "steady", "duration": 3600, "power_pct": 90, "role": "interval"}],
}
MORNING_ENDURANCE = {
    "name": "Morning endurance",
    "ftp": 250,
    "blocks": [{"type": "steady", "duration": 3600, "power_pct": 60, "role": "interval"}],
}


def _cycling_activity(activity_id: int, day: str, duration_s: float, calories: float) -> dict:
    return {
        "activityId": activity_id,
        "activityType": {"typeKey": "cycling"},
        "startTimeLocal": f"{day} 07:00:00",
        "duration": duration_s,
        "calories": calories,
    }


# --- (1) confirm_targets gets the same three knobs suggest_targets does ---


def test_confirm_targets_takes_the_same_knobs_suggest_targets_does(athlete):
    suggested = nutrition.suggest_targets(date_str="2026-08-24", exercise_kcal_override=800)
    kcal = suggested["days"][0]["suggested"]["kcal"]

    confirmed = nutrition.confirm_targets(date_str="2026-08-24", exercise_kcal_override=800)
    assert confirmed["targets"][0]["kcal"] == kcal
    assert confirmed["targets"][0]["source"] == "confirmed"


def test_confirm_targets_without_knobs_still_matches_a_knobless_suggestion(athlete):
    suggested = nutrition.suggest_targets(date_str="2026-08-24")
    kcal = suggested["days"][0]["suggested"]["kcal"]

    confirmed = nutrition.confirm_targets(date_str="2026-08-24")
    assert confirmed["targets"][0]["kcal"] == kcal


def test_a_per_day_knob_wins_over_the_top_level_one(athlete):
    result = nutrition.confirm_targets(
        protein_g_per_kg=1.5,
        days=[{"date": "2026-08-24", "protein_g_per_kg": 2.5}],
    )
    # 2.5 g/kg of 72 kg = 180, already a multiple of 5.
    assert result["targets"][0]["protein_g"] == pytest.approx(180.0)


def test_knobs_alone_do_not_flip_the_source_to_overridden(athlete):
    result = nutrition.confirm_targets(
        date_str="2026-08-24",
        baseline_factor=1.6,
        protein_g_per_kg=2.2,
        exercise_kcal_override=500,
    )
    assert result["targets"][0]["source"] == "confirmed"


def test_confirm_targets_records_the_knobs_it_used(athlete):
    nutrition.confirm_targets(date_str="2026-08-24", baseline_factor=1.6, protein_g_per_kg=2.2)
    with store.open_db() as conn:
        row = conn.execute(
            "SELECT rationale_json FROM daily_targets WHERE target_date = '2026-08-24'"
        ).fetchone()
    knobs = json.loads(row["rationale_json"])["knobs"]
    assert knobs["baseline_factor"] == 1.6
    assert knobs["protein_g_per_kg"] == 2.2


def test_a_top_level_exercise_override_is_refused_across_several_days(athlete):
    with pytest.raises(nutrition.NutritionError, match="one day"):
        nutrition.confirm_targets(
            exercise_kcal_override=800,
            days=[{"date": "2026-08-24"}, {"date": "2026-08-25"}],
        )


def test_a_per_day_exercise_override_is_fine_across_several_days(athlete):
    result = nutrition.confirm_targets(
        days=[
            {"date": "2026-08-24", "exercise_kcal_override": 800},
            {"date": "2026-08-25", "exercise_kcal_override": 600},
        ]
    )
    assert result["stored"] == 2
    assert {row["target_date"] for row in result["targets"]} == {"2026-08-24", "2026-08-25"}


def test_an_exercise_override_just_outside_the_limit_is_refused_by_confirm_targets(athlete):
    with pytest.raises(nutrition.NutritionError, match="exercise_kcal_override"):
        nutrition.confirm_targets(date_str="2026-08-24", exercise_kcal_override=15000.01)


def test_an_exercise_override_at_the_limit_is_accepted_by_the_shared_validation(athlete):
    # confirm_targets validates exercise_kcal_override with the same _number
    # call suggest_targets uses; the top of its own range is exercised here,
    # where a stored kcal target's separate 12000 cap does not also collide.
    result = nutrition.suggest_targets(date_str="2026-08-24", exercise_kcal_override=15000.0)
    assert result["days"][0]["working"]["exercise_kcal"] == 15000


# --- (2) a goal resolves for the date it is asked about, not the latest one ---


def test_a_closed_goal_still_applies_to_a_date_inside_its_own_window(athlete):
    coach.log_weight(value_kg=74.0, effective_date="2026-01-01")
    nutrition.set_goal("lose", rate_kg_per_week=-0.4, effective_date="2026-03-01")
    nutrition.close_goal(status="reached", closed_date="2026-06-01")
    nutrition.set_goal("gain", rate_kg_per_week=0.3, effective_date="2026-06-01")

    inside_a = nutrition.suggest_targets(date_str="2026-03-15")["days"][0]
    assert inside_a["inputs"]["goal"]["goal_type"] == "lose"
    assert inside_a["working"]["goal_adjustment_kcal"] != 0

    inside_b = nutrition.suggest_targets(date_str="2026-06-15")["days"][0]
    assert inside_b["inputs"]["goal"]["goal_type"] == "gain"

    before_a = nutrition.suggest_targets(date_str="2026-02-15")["days"][0]
    assert before_a["inputs"]["goal"] is None
    assert before_a["working"]["goal_adjustment_kcal"] == 0


def test_the_handover_day_belongs_to_the_replacement_goal(athlete):
    """closed_date > on_date, not >=: the day A closes is inside A's window UNLESS
    something else is effective that same day, in which case the replacement wins
    the handover day. Pins the boundary the docstring at nutrition.py describes."""
    coach.log_weight(value_kg=74.0, effective_date="2026-01-01")
    nutrition.set_goal("lose", rate_kg_per_week=-0.4, effective_date="2026-03-01")
    nutrition.close_goal(status="reached", closed_date="2026-06-01")
    nutrition.set_goal("gain", rate_kg_per_week=0.3, effective_date="2026-06-01")

    handover = nutrition.suggest_targets(date_str="2026-06-01")["days"][0]
    assert handover["inputs"]["goal"]["goal_type"] == "gain"


def test_a_goal_closed_with_no_replacement_reads_as_no_goal_on_its_close_date(athlete):
    coach.log_weight(value_kg=74.0, effective_date="2026-01-01")
    nutrition.set_goal("lose", rate_kg_per_week=-0.4, effective_date="2026-03-01")
    nutrition.close_goal(status="reached", closed_date="2026-06-01")

    day_before_close = nutrition.suggest_targets(date_str="2026-05-31")["days"][0]
    assert day_before_close["inputs"]["goal"]["goal_type"] == "lose"

    on_close = nutrition.suggest_targets(date_str="2026-06-01")["days"][0]
    assert on_close["inputs"]["goal"] is None
    assert on_close["working"]["goal_adjustment_kcal"] == 0


def test_week_summary_over_a_closed_goals_window_reports_that_goal(athlete):
    coach.log_weight(value_kg=74.0, effective_date="2026-01-01")
    nutrition.set_goal("lose", rate_kg_per_week=-0.4, effective_date="2026-03-01")
    nutrition.close_goal(status="reached", closed_date="2026-06-01")
    nutrition.set_goal("gain", rate_kg_per_week=0.3, effective_date="2026-06-01")

    week = nutrition.week_summary("2026-03-10", "2026-03-16")
    assert week["goal"]["goal_type"] == "lose"


# --- (3) a protected day withholds a deficit, never a surplus ---


def test_a_deficit_is_withheld_on_a_protected_day(athlete):
    coach.add_event(name="La Bisou", event_date="2026-09-27", status="upcoming")
    nutrition.set_goal("lose", rate_kg_per_week=-0.35, effective_date="2026-08-01")
    day = nutrition.suggest_targets(date_str="2026-09-27")["days"][0]

    assert day["working"]["goal_adjustment_kcal"] == 0
    assert day["working"]["goal_adjustment_withheld_kcal"] == round(-0.35 * 7700 / 7)
    assert any("not applied" in note for note in day["notes"])


def test_a_surplus_is_applied_in_full_on_a_protected_day(athlete):
    coach.add_event(name="La Bisou", event_date="2026-09-27", status="upcoming")
    nutrition.set_goal("gain", rate_kg_per_week=0.35, effective_date="2026-08-01")
    day = nutrition.suggest_targets(date_str="2026-09-27")["days"][0]

    full_adjustment = round(0.35 * 7700 / 7)
    assert day["working"]["goal_adjustment_kcal"] == full_adjustment
    assert day["working"]["goal_adjustment_withheld_kcal"] == 0
    dumped = json.dumps(day)
    assert "not applied" not in dumped


def test_a_zero_rate_goal_leaves_no_goal_note_on_a_protected_day(athlete):
    coach.add_event(name="La Bisou", event_date="2026-09-27", status="upcoming")
    nutrition.set_goal("maintain", effective_date="2026-08-01")
    day = nutrition.suggest_targets(date_str="2026-09-27")["days"][0]

    assert day["working"]["goal_adjustment_kcal"] == 0
    assert not any("kcal/day" in note for note in day.get("notes", []))


# --- (4) a second planned session adds to the day once the first ride imports ---


def test_a_second_planned_session_is_added_once_the_first_ride_imports(athlete):
    # Two sessions planned for the day; only the morning one gets ridden,
    # imported, and explicitly linked back to its planned row — the way a real
    # import-then-link workflow works (see the sibling "does not double" test).
    # The evening session stays unlinked: it is additional exercise, not the
    # morning ride under a second name.
    saved = coach.save_planned_workouts(
        [{"spec": MORNING_ENDURANCE, "scheduled_date": "2026-08-10"}]
    )
    morning_id = saved["planned_workouts"][0]["id"]
    coach.save_planned_workouts([{"spec": EVENING_INTERVALS, "scheduled_date": "2026-08-10"}])
    lone = nutrition.suggest_targets(date_str="2026-08-10")["days"][0]
    combined_estimate = lone["training"]["planned_exercise_kcal"]
    assert combined_estimate  # both specs together estimate something nonzero

    imported = coach.import_activities([_cycling_activity(9101, "2026-08-10", 3600.0, 600.0)])
    activity_id = imported["activities"]["inserted"][0]["id"]
    coach.update_planned_workout(morning_id, linked_activity_id=activity_id)

    day = nutrition.suggest_targets(date_str="2026-08-10")["days"][0]
    evening_estimate = next(
        p["estimated_kcal"] for p in day["training"]["planned"] if p["name"] == "Evening intervals"
    )

    assert day["working"]["exercise_source"] == "imported_activity+planned_workout"
    assert day["working"]["exercise_kcal"] == 600 + evening_estimate
    assert any("Evening intervals" in note for note in day["notes"])


def test_a_planned_session_linked_to_the_ride_does_not_double_the_exercise(athlete):
    saved = coach.save_planned_workouts(
        [{"spec": EVENING_INTERVALS, "scheduled_date": "2026-08-11"}]
    )
    planned_id = saved["planned_workouts"][0]["id"]
    imported = coach.import_activities([_cycling_activity(9102, "2026-08-11", 3600.0, 600.0)])
    activity_id = imported["activities"]["inserted"][0]["id"]
    coach.update_planned_workout(planned_id, linked_activity_id=activity_id)

    day = nutrition.suggest_targets(date_str="2026-08-11")["days"][0]
    assert day["working"]["exercise_kcal"] == 600
    assert day["working"]["exercise_source"] == "imported_activity"


def test_a_skipped_planned_session_never_adds_to_the_days_exercise(athlete):
    saved = coach.save_planned_workouts(
        [{"spec": EVENING_INTERVALS, "scheduled_date": "2026-08-12"}]
    )
    planned_id = saved["planned_workouts"][0]["id"]
    coach.update_planned_workout(planned_id, status="skipped")
    coach.import_activities([_cycling_activity(9103, "2026-08-12", 3600.0, 600.0)])

    day = nutrition.suggest_targets(date_str="2026-08-12")["days"][0]
    assert day["working"]["exercise_kcal"] == 600
    assert day["working"]["exercise_source"] == "imported_activity"


# --- (5) a dns race does not trigger race-day fuelling ---


def test_a_dns_race_does_not_trigger_race_day_fuelling(athlete):
    coach.add_event(name="DNF'd race", event_date="2026-09-12", status="dns")
    nutrition.set_goal("lose", rate_kg_per_week=-0.5, effective_date="2026-08-01")

    race_day = nutrition.suggest_targets(date_str="2026-09-12")["days"][0]
    eve = nutrition.suggest_targets(date_str="2026-09-11")["days"][0]

    assert race_day["day_type"] in ("training", "rest", "big_session")
    assert eve["day_type"] != "race_eve"
    assert race_day["suggested"]["fiber_g"] == nutrition.DEFAULT_FIBER_G
    assert race_day["working"]["goal_adjustment_kcal"] < 0


def test_an_upcoming_event_still_triggers_race_day_fuelling(athlete):
    coach.add_event(name="La Bisou", event_date="2026-09-27", status="upcoming")
    eve = nutrition.suggest_targets(date_str="2026-09-26")["days"][0]
    race = nutrition.suggest_targets(date_str="2026-09-27")["days"][0]
    assert eve["day_type"] == "race_eve"
    assert race["day_type"] == "race"


# --- (6) negative placeholders never read as measurements ---


def test_a_negative_calories_activity_is_not_a_measurement(athlete):
    coach.import_activities([_cycling_activity(9201, "2026-08-13", 3600.0, -500.0)])
    training = nutrition.day_summary("2026-08-13")["training"]
    assert training["measured_exercise_kcal"] is None
    assert training["unmeasured_activities"] == 1


def test_calories_of_exactly_one_still_counts_as_a_measurement(athlete):
    coach.import_activities([_cycling_activity(9202, "2026-08-13", 100.0, 1.0)])
    training = nutrition.day_summary("2026-08-13")["training"]
    assert training["measured_exercise_kcal"] == pytest.approx(1.0)


def test_a_negative_duration_activity_does_not_subtract_from_activity_seconds(athlete):
    coach.import_activities(
        [
            _cycling_activity(9203, "2026-08-14", 7000.0, 200.0),
            _cycling_activity(9204, "2026-08-14", -7200.0, 50.0),
        ]
    )
    training = nutrition.day_summary("2026-08-14")["training"]
    assert training["activity_seconds"] == pytest.approx(7000.0)


# --- (7) the weight trend is anchored to the window, not the reach ---


def test_the_trend_is_anchored_to_the_window_not_the_six_day_reach(athlete):
    nutrition.set_goal("lose", rate_kg_per_week=-0.5, effective_date="2026-08-01")
    window_start = date(2026, 8, 24)
    for offset in range(-6, 7):
        day = (window_start + timedelta(days=offset)).isoformat()
        coach.log_weight(value_kg=80.0 - 0.1 * offset, effective_date=day)

    trend = nutrition.week_summary("2026-08-24", "2026-08-30")["weight_trend"]
    assert trend["over_days"] <= 6
    # The endpoints are the first and last IN-WINDOW means, not the reach-extended
    # ones: 80.3 -> 79.7 over the window's own 6 days, -0.7 kg/week.
    assert trend["change_kg"] == -0.6
    assert trend["kg_per_week"] == -0.7


def test_a_weighin_exactly_on_the_windows_first_day_counts_as_in_window(athlete):
    coach.log_weight(value_kg=80.0, effective_date="2026-08-24")
    coach.log_weight(value_kg=79.5, effective_date="2026-08-30")
    trend = nutrition.week_summary("2026-08-24", "2026-08-30")["weight_trend"]
    assert trend["weigh_ins_in_window"] == 2


def test_a_single_prewindow_weighin_does_not_create_a_phantom_multi_day_span(athlete):
    coach.log_weight(value_kg=80.0, effective_date="2026-08-20")  # before the window
    coach.log_weight(value_kg=79.5, effective_date="2026-08-26")  # inside the window
    trend = nutrition.week_summary("2026-08-24", "2026-08-30")["weight_trend"]
    assert "kg_per_week" not in trend
    assert trend["weigh_ins_in_window"] == 1
    # There ARE two weigh-ins on two different dates (08-20 and 08-26) — the
    # reason must not claim they are on one date. Only one of them is inside
    # the window, which is the true reason there is no span to read.
    assert "one date" not in trend.get("note", "")
    assert "one weigh-in" in trend.get("note", "").lower()


def test_several_in_window_weighins_on_one_date_is_the_genuine_one_date_case(athlete):
    coach.log_weight(value_kg=80.0, effective_date="2026-08-26")
    coach.log_weight(value_kg=79.8, effective_date="2026-08-26")
    trend = nutrition.week_summary("2026-08-24", "2026-08-30")["weight_trend"]
    assert trend["weigh_ins_in_window"] == 2
    assert "kg_per_week" not in trend
    assert "one date" in trend.get("note", "")


# --- (8) a range of days is resolved from one batch of queries, not per day ---


def _count_statements(monkeypatch, run) -> list[str]:
    """Every SQL statement any connection executes while `run` is called.

    `sqlite3.Connection` is a built-in type and cannot have its `execute`
    patched directly, so this wraps `sqlite3.connect` instead and traces each
    connection it hands back — the same effect the batch spec's own
    `set_trace_callback` suggestion has, from the one place every connection
    in this codebase is created (`store.open_db`).
    """
    original_connect = sqlite3.connect
    calls: list[str] = []

    def counting_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.set_trace_callback(calls.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    run()
    return calls


def test_week_summary_over_60_days_issues_a_bounded_number_of_queries(athlete, monkeypatch):
    calls = _count_statements(
        monkeypatch, lambda: nutrition.week_summary("2026-06-01", "2026-07-30")
    )
    assert len(calls) < 25, f"{len(calls)} statements for a 60-day range:\n" + "\n".join(calls)


def test_suggest_targets_over_60_days_issues_a_bounded_number_of_queries(athlete, monkeypatch):
    calls = _count_statements(
        monkeypatch, lambda: nutrition.suggest_targets(start="2026-06-01", end="2026-07-30")
    )
    assert len(calls) < 25, f"{len(calls)} statements for a 60-day range:\n" + "\n".join(calls)


def test_the_batched_range_agrees_with_the_single_date_path(athlete, base):
    """The optimisation must never change an answer, only how many queries it costs."""
    coach.add_event(name="La Bisou", event_date="2026-09-27", status="upcoming")
    nutrition.set_goal("lose", rate_kg_per_week=-0.5, effective_date="2026-08-01")
    coach.save_planned_workouts([{"spec": LONG_RIDE, "scheduled_date": "2026-09-05"}])
    nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    nutrition.confirm_targets(date_str="2026-08-24")

    ranged = nutrition.suggest_targets(start="2026-08-20", end="2026-09-10")["days"]
    for entry in ranged:
        single = nutrition.suggest_targets(date_str=entry["date"])["days"][0]
        assert entry.get("suggested") == single.get("suggested"), entry["date"]
        assert entry["day_type"] == single["day_type"], entry["date"]

    week = nutrition.week_summary("2026-08-20", "2026-08-30")
    day_row = next(row for row in week["days"] if row["date"] == "2026-08-24")
    single = nutrition.suggest_targets(date_str="2026-08-24")["days"][0]
    assert day_row["totals"]["kcal"] == pytest.approx(126.0)
    assert day_row["targets"]["kcal"] == single["suggested"]["kcal"]


# --------------------------------------------------------------------------
# review round 7 (fix-review-round batch nutrition-null-and-coercion)
# --------------------------------------------------------------------------

# --- (1) an estimate's unknown protein/fibre is null, not a folded zero ---


def test_an_estimates_unstated_protein_and_fiber_are_null_not_zero():
    result = nutrition.log_food(
        [{"label": "Canteen: chicken and chips", "kcal": 900, "is_estimate": True}],
        log_date="2026-08-24",
    )
    entry = result["entries"][0]
    assert entry["protein_g"] is None
    assert entry["fiber_g"] is None
    totals = result["day"]["totals"]
    assert totals["protein_g_missing_entries"] == 1
    assert totals["fiber_g_missing_entries"] == 1
    # 0.0, never folded, so the day's own totals still add up.
    assert totals["protein_g"] == 0.0
    assert totals["fiber_g"] == 0.0
    assert "overstates" in result["day"]["unknown_macros_note"]


def test_an_estimates_explicit_zero_protein_is_a_measurement_not_a_gap():
    result = nutrition.log_food(
        [
            {
                "label": "Black coffee",
                "kcal": 5,
                "protein_g": 0,
                "fiber_g": 0,
                "is_estimate": True,
            }
        ],
        log_date="2026-08-24",
    )
    entry = result["entries"][0]
    assert entry["protein_g"] == 0.0
    assert entry["fiber_g"] == 0.0
    totals = result["day"]["totals"]
    assert "protein_g_missing_entries" not in totals
    assert "fiber_g_missing_entries" not in totals
    assert "unknown_macros_note" not in result["day"]


def test_migration_5_rebuilds_food_log_with_existing_rows_intact(tmp_path, monkeypatch):
    """A v4 database with real food_log rows must migrate in place, not lose them."""
    path = tmp_path / "existing.db"
    monkeypatch.setenv(store.ENV_DB_PATH, str(path))

    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    store.schema_version(conn)
    for version, statements in store.MIGRATIONS:
        if version > 4:
            break
        for statement in statements():
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-01-01T00:00:00Z')",
            (version,),
        )
    conn.execute(
        "INSERT INTO athlete (athlete_id, created_at, updated_at) VALUES (1, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO food_log (athlete_id, log_date, slot, label, grams, kcal, protein_g, "
        "fiber_g, counts_toward_protein, is_estimate, logged_at, updated_at) VALUES "
        "(1, '2026-08-01', 'breakfast', 'Old entry', 100, 200, 10, 2, 1, 0, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.close()

    with store.open_db() as conn:
        assert store.schema_version(conn) == store.CURRENT_SCHEMA_VERSION == 6
        row = conn.execute("SELECT * FROM food_log WHERE label = 'Old entry'").fetchone()
        columns = {
            description[0] for description in conn.execute("SELECT * FROM food_log").description
        }
    assert row["kcal"] == 200
    assert row["protein_g"] == 10
    assert row["fiber_g"] == 2
    assert {"protein_g", "fiber_g", "grams", "carbs_g", "fat_g"} <= columns

    # And the new columns really are nullable now, from the running server.
    result = nutrition.log_food(
        [{"label": "New estimate", "kcal": 300, "is_estimate": True}], log_date="2026-08-02"
    )
    assert result["entries"][0]["protein_g"] is None


def test_migrations_stay_append_only_through_version_5():
    assert store.CURRENT_SCHEMA_VERSION == 6
    assert [version for version, _ in store.MIGRATIONS] == [1, 2, 3, 4, 5, 6]


# --- (2) a 400-digit number is a refusal, never an OverflowError crash ---


def test_a_400_digit_kcal_is_a_per_item_rejection_and_does_not_lose_the_rest():
    huge = 10**400
    result = nutrition.add_ingredients([{**SKYR, "kcal_100g": huge}, CRUESLI])
    assert result["inserted"] == 1
    assert result["rejected"] == 1
    assert result["ingredients"][0]["name"] == "Cruesli"
    assert "kcal_100g" in result["rejections"][0]["reason"]


def test_log_food_refuses_a_400_digit_grams_figure_rather_than_crashing(base):
    result = nutrition.log_food([{"ingredient": "skyr", "grams": 10**400}], log_date="2026-08-24")
    assert result["logged"] == 0
    assert "grams" in result["rejections"][0]["reason"]


def test_log_weight_refuses_a_400_digit_value_rather_than_crashing():
    with pytest.raises(coach.CoachError, match="value_kg"):
        coach.log_weight(value_kg=10**400)


def test_a_huge_but_finite_number_is_refused_by_the_limits_not_an_exception():
    """10**300 is a finite float — it must reach the range check, not the coercion one."""
    result = nutrition.add_ingredients([{**SKYR, "name": "Improbable food", "kcal_100g": 10**300}])
    assert result["inserted"] == 0
    assert "outside" in result["rejections"][0]["reason"]


# --- (3) boolean coercion: "false" is false, and a bool is not an int id ---


def test_counts_toward_protein_string_false_stores_false():
    nutrition.add_ingredients([{**SKYR, "counts_toward_protein": "false"}])
    stored = nutrition.search_ingredients("skyr")["matches"][0]
    assert stored["counts_toward_protein"] is False


def test_counts_toward_protein_string_true_stores_true():
    nutrition.add_ingredients([{**SKYR, "counts_toward_protein": "true"}])
    stored = nutrition.search_ingredients("skyr")["matches"][0]
    assert stored["counts_toward_protein"] is True


def test_an_unparseable_bool_spelling_is_a_per_item_rejection():
    result = nutrition.add_ingredients([{**SKYR, "counts_toward_protein": "banana"}])
    assert result["inserted"] == 0
    assert "counts_toward_protein" in result["rejections"][0]["reason"]


def test_log_meal_refuses_a_boolean_meal_reference(base):
    nutrition.save_meal("Petit-dej", [{"ingredient": "skyr", "grams": 200}])
    with pytest.raises(nutrition.NutritionError, match="not a meal reference"):
        nutrition.log_meal(True, log_date="2026-08-24")


def test_is_estimate_false_with_an_ingredient_reference_takes_the_ingredient_path(base):
    result = nutrition.log_food(
        [{"ingredient": "skyr", "grams": 200, "is_estimate": "false"}], log_date="2026-08-24"
    )
    assert result["logged"] == 1
    entry = result["entries"][0]
    assert entry["is_estimate"] is False
    assert entry["ingredient_id"] is not None


# --- (4) a zero-gram entry is a placeholder, not a food ---


def test_log_food_refuses_a_zero_gram_entry(base):
    result = nutrition.log_food([{"ingredient": "skyr", "grams": 0}], log_date="2026-08-24")
    assert result["logged"] == 0
    assert "zero" in result["rejections"][0]["reason"]
    assert nutrition.day_summary("2026-08-24")["entry_count"] == 0


def test_log_food_refuses_a_negative_gram_entry(base):
    result = nutrition.log_food([{"ingredient": "skyr", "grams": -100}], log_date="2026-08-24")
    assert result["logged"] == 0


def test_log_meal_override_of_zero_still_omits_with_the_note(base):
    nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}]
    )
    result = nutrition.log_meal(
        "Petit-dej", log_date="2026-08-24", overrides=[{"ingredient": "Cruesli", "grams": 0}]
    )
    assert result["omitted"] == ["Cruesli"]
    assert "Cruesli had an override of 0 g" in result["omitted_note"]
    assert result["logged"] == 1


def test_log_meal_override_of_a_string_zero_still_omits(base):
    """The omit guard compares the coerced figure, not the raw value: every
    other quantity in this module reads the numeric strings a pasted payload
    carries, so "0" has to mean what 0 means."""
    nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}]
    )
    result = nutrition.log_meal(
        "Petit-dej", log_date="2026-08-24", overrides=[{"ingredient": "Cruesli", "grams": "0"}]
    )
    assert result["omitted"] == ["Cruesli"]
    assert "Cruesli had an override of 0 g" in result["omitted_note"]
    assert result["logged"] == 1


def test_log_meal_override_of_a_bool_is_refused_not_read_as_a_quantity(base):
    """`float(False)` is 0.0, so an unguarded bool would silently omit an
    ingredient — the same trap resolve_ingredient and _resolve_meal refuse."""
    nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}]
    )
    with pytest.raises(nutrition.NutritionError, match="grams must be a number"):
        nutrition.log_meal(
            "Petit-dej",
            log_date="2026-08-24",
            overrides=[{"ingredient": "Cruesli", "grams": False}],
        )


def test_a_tenth_of_a_gram_is_accepted(base):
    result = nutrition.log_food([{"ingredient": "skyr", "grams": 0.1}], log_date="2026-08-24")
    assert result["logged"] == 1


# --- round-8 finding 1: the override omit pre-check ran before the
# grams-and-portions contradiction refusal, so a both-given override with one
# side zero was silently treated as an omission instead of refused ---


def test_override_of_grams_and_zero_portions_is_still_refused_as_a_contradiction(base):
    """Pinned regression: at the base commit this refused; the round-7 omit
    pre-check made it log nothing instead, with `to_g: 0.0` looking intended."""
    nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}]
    )
    with pytest.raises(nutrition.NutritionError, match="not both"):
        nutrition.log_meal(
            "Petit-dej",
            log_date="2026-08-24",
            overrides=[{"ingredient": "skyr", "grams": 200, "portions": 0}],
        )


def test_zero_grams_and_given_portions_is_also_refused_as_a_contradiction(base):
    """Same contradiction, the other ordering."""
    nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}]
    )
    with pytest.raises(nutrition.NutritionError, match="not both"):
        nutrition.log_meal(
            "Petit-dej",
            log_date="2026-08-24",
            overrides=[{"ingredient": "skyr", "grams": 0, "portions": 2}],
        )


def test_override_of_a_plain_zero_still_omits_and_excludes_from_the_day_total(base):
    nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}]
    )
    result = nutrition.log_meal(
        "Petit-dej", log_date="2026-08-24", overrides=[{"ingredient": "Cruesli", "grams": "0"}]
    )
    assert result["omitted"] == ["Cruesli"]
    assert "Cruesli" not in [entry["label"] for entry in result["entries"]]
    # 63 kcal/100g * 200g = 126; Cruesli is fully excluded, not folded to 0.
    assert result["totals"]["kcal"] == pytest.approx(126.0)


def test_override_portions_of_zero_omits_even_without_a_default_portion(base):
    """Omitting an ingredient must not require it to carry a stored portion
    size — the zero-return has to happen before the no-default-portion
    refusal. Cruesli has no `default_portion_g`."""
    nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}]
    )
    result = nutrition.log_meal(
        "Petit-dej", log_date="2026-08-24", overrides=[{"ingredient": "Cruesli", "portions": 0}]
    )
    assert result["omitted"] == ["Cruesli"]
    assert result["logged"] == 1


def test_override_of_a_tenth_gram_is_accepted_not_treated_as_an_omission(base):
    """Value just outside the zero guard."""
    nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}]
    )
    result = nutrition.log_meal(
        "Petit-dej", log_date="2026-08-24", overrides=[{"ingredient": "Cruesli", "grams": 0.1}]
    )
    assert result.get("omitted", []) == []
    assert result["overrides_applied"] == [{"name": "Cruesli", "from_g": 50.0, "to_g": 0.1}]


def test_override_of_one_portion_uses_the_stored_default_portion_size(base):
    """Value just outside the zero guard, on the portions path."""
    nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}]
    )
    result = nutrition.log_meal(
        "Petit-dej", log_date="2026-08-24", overrides=[{"ingredient": "skyr", "portions": 1}]
    )
    assert result.get("omitted", []) == []
    assert result["overrides_applied"] == [
        {"name": "Skyr nature 0%", "from_g": 200.0, "to_g": 200.0}
    ]


def test_override_grams_true_is_refused_naming_the_field(base):
    nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}]
    )
    with pytest.raises(nutrition.NutritionError, match="grams must be a number"):
        nutrition.log_meal(
            "Petit-dej", log_date="2026-08-24", overrides=[{"ingredient": "Cruesli", "grams": True}]
        )


def test_override_portions_false_is_refused_naming_the_field(base):
    nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}]
    )
    with pytest.raises(nutrition.NutritionError, match="portions must be a number"):
        nutrition.log_meal(
            "Petit-dej",
            log_date="2026-08-24",
            overrides=[{"ingredient": "Cruesli", "portions": False}],
        )


def test_log_foods_ingredient_path_still_refuses_zero_grams_as_a_placeholder(base):
    """`_entry_row` calls `_quantity` without `allow_zero` — the value just
    outside the new allow_zero mode, on the one path that must never gain it."""
    result = nutrition.log_food([{"ingredient": "skyr", "grams": 0}], log_date="2026-08-24")
    assert result["logged"] == 0
    assert "placeholder" in result["rejections"][0]["reason"]


# --- (5) a path to null on ingredients/meals, and a zero price is unpriced ---


def test_clearing_package_price_returns_cost_to_unknown(base):
    result = nutrition.update_ingredient(name="skyr", clear=["package_price"])
    assert result["cleared_fields"] == ["package_price"]
    assert result["ingredient"]["package_price"] is None
    assert result["ingredient"]["cost_per_100g"] is None


def test_clearing_a_required_field_is_refused_and_nothing_changes(base):
    with pytest.raises(nutrition.NutritionError, match="cannot clear"):
        nutrition.update_ingredient(name="skyr", clear=["kcal_100g"])
    stored = nutrition.search_ingredients("skyr")["matches"][0]
    assert stored["kcal_100g"] == 63


# --- round-8 finding 3: a blank note/portion_label on update_ingredient
# passed the "is not None" gate, folded to None through `_text`, and was
# written straight over the stored value with no `cleared_fields` to say so ---


def test_update_ingredient_blank_note_and_portion_label_are_both_ignored(base):
    """Pinned regression: `update_ingredient(name=..., note="", portion_label=" ")`
    used to silently destroy both stored values."""
    nutrition.add_ingredients(
        [
            {
                "name": "Riz cuit maison",
                "kcal_100g": 130,
                "protein_100g": 2.6,
                "fiber_100g": 0.4,
                "note": "cooked weight",
                "portion_label": "1 bowl = 150 g",
            }
        ]
    )
    result = nutrition.update_ingredient(name="Riz cuit maison", note="", portion_label=" ")
    stored = nutrition.search_ingredients("Riz cuit maison")["matches"][0]
    assert stored["note"] == "cooked weight"
    assert stored["portion_label"] == "1 bowl = 150 g"
    assert set(result["ignored_blank_fields"]) == {"note", "portion_label"}
    assert "not an instruction to erase" in result["ignored_blank_note"]


def test_update_ingredient_blank_note_alone_does_not_erase_it_and_is_reported(base):
    result = nutrition.update_ingredient(name="Cruesli", note="")
    stored = nutrition.search_ingredients("Cruesli")["matches"][0]
    assert stored["note"] == "weigh it — this is where eyeballing drifts"
    assert result["ignored_blank_fields"] == ["note"]
    assert result["updated_fields"] == []


def test_update_ingredient_whitespace_portion_label_does_not_erase_it(base):
    result = nutrition.update_ingredient(name="skyr", portion_label="   ")
    stored = nutrition.search_ingredients("skyr")["matches"][0]
    assert stored["portion_label"] == "1 pot = 200 g"
    assert result["ignored_blank_fields"] == ["portion_label"]


def test_update_ingredient_note_of_x_is_stored(base):
    """Value just outside the blank guard."""
    result = nutrition.update_ingredient(name="Cruesli", note="x")
    stored = nutrition.search_ingredients("Cruesli")["matches"][0]
    assert stored["note"] == "x"
    assert result["updated_fields"] == ["note"]
    assert "ignored_blank_fields" not in result


def test_update_ingredient_portion_label_of_x_is_stored(base):
    """Value just outside the blank guard — the portion_label twin of the note case."""
    result = nutrition.update_ingredient(name="skyr", portion_label="x")
    stored = nutrition.search_ingredients("skyr")["matches"][0]
    assert stored["portion_label"] == "x"
    assert result["updated_fields"] == ["portion_label"]
    assert "ignored_blank_fields" not in result


def test_update_ingredient_clear_note_still_nulls_it(base):
    result = nutrition.update_ingredient(name="Cruesli", clear=["note"])
    stored = nutrition.search_ingredients("Cruesli")["matches"][0]
    assert stored["note"] is None
    assert result["cleared_fields"] == ["note"]


def test_update_ingredient_clear_portion_label_still_nulls_it(base):
    result = nutrition.update_ingredient(name="skyr", clear=["portion_label"])
    stored = nutrition.search_ingredients("skyr")["matches"][0]
    assert stored["portion_label"] is None
    assert result["cleared_fields"] == ["portion_label"]


def test_update_ingredient_blank_note_and_clear_of_note_is_not_double_reported(base):
    """Blank *and* cleared on the same field is not a contradiction — the
    blank is a no-op and the clear is the instruction — see `coach._stage_clear`."""
    result = nutrition.update_ingredient(name="Cruesli", note="", clear=["note"])
    stored = nutrition.search_ingredients("Cruesli")["matches"][0]
    assert stored["note"] is None
    assert result["cleared_fields"] == ["note"]
    assert "ignored_blank_fields" not in result


def test_a_zero_priced_package_is_unpriced_not_free(base):
    nutrition.update_ingredient(name="skyr", package_price=0, package_weight_g=400)
    stored = nutrition.search_ingredients("skyr")["matches"][0]
    assert stored["cost_per_100g"] is None


def test_a_cent_priced_package_is_priced(base):
    nutrition.update_ingredient(name="skyr", package_price=0.01, package_weight_g=400)
    stored = nutrition.search_ingredients("skyr")["matches"][0]
    assert stored["cost_per_100g"] == pytest.approx(0.01 * 100 / 400)


def test_a_zero_priced_entrys_cost_counts_as_unpriced_in_the_days_totals(base):
    nutrition.update_ingredient(name="skyr", package_price=0, package_weight_g=400)
    result = nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    entry = result["entries"][0]
    assert entry["cost"] is None
    assert result["day"]["totals"]["cost_unpriced_entries"] == 1


def test_week_summary_reports_a_zero_priced_entry_in_its_cost_note(athlete, base):
    nutrition.update_ingredient(name="skyr", package_price=0, package_weight_g=400)
    nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    week = nutrition.week_summary("2026-08-24", "2026-08-24")
    assert week["cost"]["unpriced_entries"] == 1
    assert "1 logged entry had no price" in week["cost"]["note"]


def test_week_summary_cost_note_pluralizes_two_unpriced_entries(athlete, base):
    """Cleanup nutrition.py:3186: the hand-rolled `entr{'y'/'ies'}` inline
    became `_plural(unpriced, 'logged entry')` — pin both counts' wording."""
    nutrition.update_ingredient(name="skyr", package_price=0, package_weight_g=400)
    nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    nutrition.log_food([{"ingredient": "skyr", "grams": 100}], log_date="2026-08-25")
    week = nutrition.week_summary("2026-08-24", "2026-08-25")
    assert week["cost"]["unpriced_entries"] == 2
    assert "2 logged entries had no price" in week["cost"]["note"]


def test_week_summary_reports_unstated_macros_from_an_estimate(athlete, base):
    """A 900 kcal unstated-protein estimate must not read as a complete average.

    Round-7 follow-up: `protein_g_per_day` used to sum `row["totals"]["protein_g"]`
    with no reference to `protein_g_missing_entries`, so a week holding one estimate
    that never stated protein reported an average as if every entry had.
    """
    nutrition.log_food(
        [{"label": "Canteen: chicken and chips", "kcal": 900, "is_estimate": True}],
        log_date="2026-08-24",
    )
    nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-25")
    week = nutrition.week_summary("2026-08-24", "2026-08-25")

    assert week["averages"]["protein_g_missing_entries"] == 1
    assert week["averages"]["fiber_g_missing_entries"] == 1
    assert "unknown_macros_note" in week
    assert "protein from 1 entry" in week["unknown_macros_note"]
    assert "fibre from 1 entry" in week["unknown_macros_note"]
    # The average itself is only over the days' known entries — the note says so.
    assert "undercount" in week["unknown_macros_note"]
    # The old blanket claim ("count in the ... protein averages") must be gone.
    assert "count in the calorie and protein averages" not in week["estimate_note"]
    assert "only when they stated a figure" in week["estimate_note"]


def test_delete_ingredient_refuses_when_logged(base):
    nutrition.log_food([{"ingredient": "skyr", "grams": 200}], log_date="2026-08-24")
    ingredient_id = nutrition.search_ingredients("skyr")["matches"][0]["id"]
    with pytest.raises(nutrition.NutritionError, match="1 logged entry"):
        nutrition.delete_ingredient(ingredient_id=ingredient_id)
    # Refused, so it is still there.
    assert nutrition.search_ingredients("skyr")["exact_matches"] == 1


def test_delete_ingredient_removes_a_never_referenced_one(base):
    ingredient_id = nutrition.search_ingredients("Cruesli")["matches"][0]["id"]
    result = nutrition.delete_ingredient(ingredient_id=ingredient_id)
    assert result["deleted"]["name"] == "Cruesli"
    assert nutrition.search_ingredients("Cruesli")["exact_matches"] == 0


def test_delete_meal_succeeds_and_old_entries_still_show_the_meal_name(base):
    nutrition.save_meal("Petit-dej", [{"ingredient": "skyr", "grams": 200}])
    logged = nutrition.log_meal("Petit-dej", log_date="2026-08-24")
    entry_id = logged["entries"][0]["id"]

    result = nutrition.delete_meal("Petit-dej")
    assert result["deleted"]["name"] == "Petit-dej"
    assert result["history_note"]

    day = nutrition.day_summary("2026-08-24")
    grouped_entries = [
        entry
        for slot in day["slots"]
        for meal in slot["meals"]
        for entry in meal["entries"]
        if entry["id"] == entry_id
    ]
    assert grouped_entries and grouped_entries[0]["meal_name"] == "Petit-dej"


# --- cleanup nutrition.py:1221: delete_meal's history_note verb agreement ---


def test_delete_meal_history_note_agrees_for_one_logged_entry(base):
    nutrition.save_meal("Petit-dej", [{"ingredient": "skyr", "grams": 200}])
    nutrition.log_meal("Petit-dej", log_date="2026-08-24")
    result = nutrition.delete_meal("Petit-dej")
    assert "1 existing log entry logged from 'Petit-dej' keeps the macros" in result["history_note"]


def test_delete_meal_history_note_agrees_for_two_logged_entries(base):
    nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}, {"ingredient": "Cruesli", "grams": 50}]
    )
    nutrition.log_meal("Petit-dej", log_date="2026-08-24")
    result = nutrition.delete_meal("Petit-dej")
    assert (
        "2 existing log entries logged from 'Petit-dej' keep the macros" in result["history_note"]
    )


def test_save_meal_clears_its_note(base):
    nutrition.save_meal("Petit-dej", [{"ingredient": "skyr", "grams": 200}], note="old note")
    result = nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}], clear=["note"]
    )
    assert result["stored"]["note"] is None
    assert result["cleared_fields"] == ["note"]


def test_save_meal_on_a_brand_new_name_with_a_bad_clear_field_is_refused(base):
    """A mistyped meal name silently creates a new meal — `clear` must still be
    validated on that path, or a caller trying to erase something is told it
    worked. Round-7 follow-up: this used to insert the meal with `cleared: []`.
    """
    with pytest.raises(nutrition.NutritionError, match="cannot clear"):
        nutrition.save_meal("brand new", [{"ingredient": "skyr", "grams": 200}], clear=["items"])
    assert nutrition.list_meals()["meals"] == []


# --- round-8 finding 9: save_meal's new-meal branch validated `clear` against
# an empty dict, so the value-plus-clear contradiction never fired there ---


def test_save_meal_new_meal_value_and_clear_of_the_same_field_refuses_identically(base):
    """Pinned regression: the same call was refused against an existing meal
    but silently accepted (note stored, clear ignored) against a new one."""
    nutrition.save_meal("Existing", [{"ingredient": "skyr", "grams": 200}])
    with pytest.raises(nutrition.NutritionError) as existing_exc:
        nutrition.save_meal(
            "Existing", [{"ingredient": "skyr", "grams": 200}], note="x", clear=["note"]
        )

    with pytest.raises(nutrition.NutritionError) as new_exc:
        nutrition.save_meal(
            "Brand New Meal", [{"ingredient": "skyr", "grams": 200}], note="x", clear=["note"]
        )

    assert str(new_exc.value) == str(existing_exc.value)
    assert all(meal["name"] != "Brand New Meal" for meal in nutrition.list_meals()["meals"])


def test_save_meal_new_meal_with_only_clear_and_no_value_succeeds_with_a_null_note(base):
    """Value just outside the contradiction: nothing to conflict with, so it
    succeeds — a brand-new meal already has nothing to clear."""
    result = nutrition.save_meal(
        "Fresh Meal", [{"ingredient": "skyr", "grams": 200}], clear=["note"]
    )
    assert result["stored"]["note"] is None
    assert "cleared_fields" not in result


# --- (6) one _one_of, tolerant, shared by both layers ---


def test_a_nutrition_enum_tolerates_case_after_unification(base):
    result = nutrition.save_meal(
        "Petit-dej", [{"ingredient": "skyr", "grams": 200}], default_for_slot="Breakfast"
    )
    assert result["stored"]["default_for_slot"] == "breakfast"


# --------------------------------------------------------------------------
# review round 8 — finding 8: confirm-time gate on summed exercise
# --------------------------------------------------------------------------


def _pushed_session(scheduled_date: str) -> int:
    """A planned session pushed to a platform — the ordinary pre-import state."""
    saved = coach.save_planned_workouts(
        [{"spec": EVENING_INTERVALS, "scheduled_date": scheduled_date}]
    )
    planned_id = saved["planned_workouts"][0]["id"]
    coach.update_planned_workout(planned_id, status="pushed", pushed_to="garmin")
    return planned_id


def test_confirm_targets_refuses_a_summed_day_naming_the_session_and_link_activity(athlete):
    """(a) The ordinary push -> ride -> import flow, nobody calls link_activity: the
    suggestion still sums (round 7's fix, pinned elsewhere), but confirm_targets must
    not file the doubled figure."""
    _pushed_session("2026-08-15")
    coach.import_activities([_cycling_activity(9301, "2026-08-15", 3600.0, 600.0)])

    suggested = nutrition.suggest_targets(date_str="2026-08-15")["days"][0]
    assert suggested["working"]["exercise_source"] == "imported_activity+planned_workout"

    result = nutrition.confirm_targets(date_str="2026-08-15")
    assert result["stored"] == 0
    assert result["rejected"] == 1
    reason = result["rejections"][0]["reason"]
    assert "Evening intervals" in reason
    assert "link_activity" in reason
    assert "accept_summed_exercise" in reason
    assert "exercise_kcal_override" in reason

    with store.open_db() as conn:
        row = conn.execute(
            "SELECT * FROM daily_targets WHERE target_date = '2026-08-15'"
        ).fetchone()
    assert row is None


def test_confirm_targets_succeeds_after_linking_with_exercise_alone(athlete):
    """(b) Linking the ride to the session removes the sum entirely — the filed
    rationale's exercise figure is the measured one alone."""
    planned_id = _pushed_session("2026-08-16")
    imported = coach.import_activities([_cycling_activity(9302, "2026-08-16", 3600.0, 600.0)])
    activity_id = imported["activities"]["inserted"][0]["id"]
    coach.link_activity(planned_id, activity_id=activity_id)

    result = nutrition.confirm_targets(date_str="2026-08-16")
    assert result["stored"] == 1
    assert result["rejected"] == 0

    with store.open_db() as conn:
        row = conn.execute(
            "SELECT rationale_json FROM daily_targets WHERE target_date = '2026-08-16'"
        ).fetchone()
    steps = json.loads(row["rationale_json"])["steps"]
    assert any("600 kcal of exercise (imported_activity)" in step for step in steps)

    suggested_after_link = nutrition.suggest_targets(date_str="2026-08-16")["days"][0]
    assert suggested_after_link["working"]["exercise_source"] == "imported_activity"
    assert result["targets"][0]["kcal"] == suggested_after_link["suggested"]["kcal"]


def test_confirm_targets_accepts_a_summed_day_with_the_top_level_flag(athlete):
    """(c) accept_summed_exercise=true at the top level files the summed figure and
    says so explicitly in the response."""
    _pushed_session("2026-08-17")
    coach.import_activities([_cycling_activity(9303, "2026-08-17", 3600.0, 600.0)])

    result = nutrition.confirm_targets(date_str="2026-08-17", accept_summed_exercise=True)
    assert result["stored"] == 1
    note = result["targets"][0]["accepted_summed_exercise_note"]
    assert "explicitly" in note
    assert "Evening intervals" in note

    suggested = nutrition.suggest_targets(date_str="2026-08-17")["days"][0]
    assert result["targets"][0]["kcal"] == suggested["suggested"]["kcal"]


def test_confirm_targets_accepts_a_summed_day_with_the_per_day_string_spelling(athlete):
    """(c) The per-day spelling wins over the top level, and a string "true" behaves
    like the boolean — _bool's own contract."""
    _pushed_session("2026-08-18")
    coach.import_activities([_cycling_activity(9304, "2026-08-18", 3600.0, 600.0)])

    result = nutrition.confirm_targets(
        accept_summed_exercise=False,
        days=[{"date": "2026-08-18", "accept_summed_exercise": "true"}],
    )
    assert result["stored"] == 1
    assert "explicitly" in result["targets"][0]["accepted_summed_exercise_note"]


def test_confirm_targets_needs_no_flag_with_only_a_planned_session(athlete):
    """(d) Just outside the gate: exercise_source "planned_workout" alone confirms
    without accept_summed_exercise."""
    coach.save_planned_workouts([{"spec": LONG_RIDE, "scheduled_date": "2026-08-19"}])
    result = nutrition.confirm_targets(date_str="2026-08-19")
    assert result["stored"] == 1
    assert "accepted_summed_exercise_note" not in result["targets"][0]


def test_confirm_targets_needs_no_flag_with_only_an_import(athlete):
    """(d) Just outside the gate: exercise_source "imported_activity" alone confirms
    without accept_summed_exercise."""
    coach.import_activities([_cycling_activity(9305, "2026-08-22", 3600.0, 600.0)])
    result = nutrition.confirm_targets(date_str="2026-08-22")
    assert result["stored"] == 1
    assert "accepted_summed_exercise_note" not in result["targets"][0]


def test_confirm_targets_needs_no_flag_when_an_override_is_given(athlete):
    """(d) Just outside the gate: exercise_kcal_override bypasses it entirely — there
    is no sum left to accept."""
    _pushed_session("2026-08-23")
    coach.import_activities([_cycling_activity(9306, "2026-08-23", 3600.0, 600.0)])

    result = nutrition.confirm_targets(date_str="2026-08-23", exercise_kcal_override=900)
    assert result["stored"] == 1
    assert "accepted_summed_exercise_note" not in result["targets"][0]


def test_confirm_targets_bulk_call_separates_a_gated_day_from_a_clean_one(athlete):
    """(e) One gated day and one clean day in the same bulk call: the clean one is
    stored, the gated one lands in rejected, and the response shows both."""
    _pushed_session("2026-08-27")
    coach.import_activities([_cycling_activity(9307, "2026-08-27", 3600.0, 600.0)])

    result = nutrition.confirm_targets(days=[{"date": "2026-08-27"}, {"date": "2026-08-28"}])
    assert result["stored"] == 1
    assert result["rejected"] == 1
    assert result["targets"][0]["target_date"] == "2026-08-28"
    assert result["rejections"][0]["date"] == "2026-08-27"
    assert "link_activity" in result["rejections"][0]["reason"]


# --- cleanup nutrition.py:2499 — the unmeasured-activities note, pluralized ---


def test_suggest_targets_unmeasured_activity_note_pins_the_singular(athlete):
    coach.import_activities([_cycling_activity(9308, "2026-08-29", 3600.0, -50.0)])
    day = nutrition.suggest_targets(date_str="2026-08-29")["days"][0]
    assert (
        "1 imported activity on this date carries no calorie figure from Garmin and "
        "contributed nothing." in day["notes"]
    )


def test_suggest_targets_unmeasured_activity_note_pins_the_plural(athlete):
    coach.import_activities(
        [
            _cycling_activity(9309, "2026-08-30", 3600.0, -50.0),
            _cycling_activity(9310, "2026-08-30", 1800.0, -20.0),
        ]
    )
    day = nutrition.suggest_targets(date_str="2026-08-30")["days"][0]
    assert (
        "2 imported activities on this date carry no calorie figure from Garmin and "
        "contributed nothing." in day["notes"]
    )


# --- cleanup nutrition.py:2591 — _RangeHistory.weight/.goal parity with the
# single-date resolvers they hand-duplicate ---


def test_range_history_agrees_with_the_single_date_resolvers_at_the_edges(athlete):
    """Seeds the edge cases the duplication could silently drift on: a reach-back
    weigh-in from well before the window, a date with no weigh-in at all, a goal's
    exclusive closed_date bound, a goal closed the day after (still applies), and
    the handover day between two overlapping goals — see the FTP-is-dated rule."""
    coach.log_weight(value_kg=70.0, effective_date="2025-11-01")
    nutrition.set_goal("lose", rate_kg_per_week=-0.3, effective_date="2026-01-01")
    nutrition.set_goal("lose", rate_kg_per_week=-0.6, effective_date="2026-01-10")

    with store.open_db() as conn:
        history = nutrition._RangeHistory(
            conn, nutrition.DEFAULT_ATHLETE_ID, "2026-01-01", "2026-01-15"
        )
        for day in ("2026-01-01", "2026-01-05", "2026-01-09", "2026-01-10", "2026-01-15"):
            expected_weight = nutrition._latest_weight(conn, nutrition.DEFAULT_ATHLETE_ID, day)
            expected_goal = nutrition._active_goal(conn, nutrition.DEFAULT_ATHLETE_ID, day)
            assert (history.weight(day) or {}).get("id") == ((expected_weight or {}).get("id")), day
            assert (history.goal(day) or {}).get("id") == ((expected_goal or {}).get("id")), day
        # The exclusive bound and the handover, spelled out rather than only
        # compared: goal1 does not apply on its own closed_date, goal2 does.
        assert history.goal("2026-01-09")["rate_kg_per_week"] == pytest.approx(-0.3)
        assert history.goal("2026-01-10")["rate_kg_per_week"] == pytest.approx(-0.6)

    # No weigh-in at all: a date before the only weigh-in on file.
    with store.open_db() as conn:
        early_history = nutrition._RangeHistory(
            conn, nutrition.DEFAULT_ATHLETE_ID, "2025-10-01", "2025-10-05"
        )
        assert nutrition._latest_weight(conn, nutrition.DEFAULT_ATHLETE_ID, "2025-10-03") is None
        assert early_history.weight("2025-10-03") is None


# --- CUT — the three knob ranges suggest_targets/confirm_targets both enforce,
# now named constants; this is the parity test that catches a drifted range ---


@pytest.mark.parametrize(
    "knob,accepted,just_outside",
    [
        ("protein_g_per_kg", 4.0, 4.01),
        ("protein_g_per_kg", 0.5, 0.49),
        ("baseline_factor", 2.5, 2.51),
        ("baseline_factor", 1.0, 0.99),
        ("exercise_kcal_override", 15000.0, 15000.01),
        ("exercise_kcal_override", 0.0, -0.01),
    ],
)
def test_suggest_and_confirm_targets_knob_ranges_stay_in_lockstep(
    athlete, knob, accepted, just_outside
):
    # At the bound, neither tool refuses the knob value itself.
    nutrition.suggest_targets(date_str="2026-08-24", **{knob: accepted})
    nutrition.confirm_targets(date_str="2026-08-24", **{knob: accepted})

    # Just outside it, both refuse — with the identical message, because both
    # read the same named constant rather than a copy of the numbers.
    with pytest.raises(nutrition.NutritionError) as suggest_exc:
        nutrition.suggest_targets(date_str="2026-08-24", **{knob: just_outside})
    with pytest.raises(nutrition.NutritionError) as confirm_exc:
        nutrition.confirm_targets(date_str="2026-08-24", **{knob: just_outside})
    assert str(suggest_exc.value) == str(confirm_exc.value)
    assert knob in str(suggest_exc.value)

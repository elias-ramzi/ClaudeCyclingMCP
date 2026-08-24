"""The nutrition layer end to end: resolve, compute, freeze, refuse.

The cases here are the ones that would be wrong in a way nobody would notice —
a day's calories that silently exclude an unpriced item, a protein target hit
with collagen, a log entry rewritten by a price correction made weeks later, a
calorie target computed against a race day as though it were a rest day.

Every expected total below is computed by hand in the test, not read back from
the code under test.
"""

from __future__ import annotations

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


def test_a_measured_ride_beats_the_plans_estimate_on_the_same_day(athlete):
    """A measurement wins over a model, even a good one — and the model stays visible."""
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
    assert day["working"]["exercise_source"] == "imported_activity"
    assert day["working"]["exercise_kcal"] == 1800
    # The estimate is still reported beside it, so a plan not followed is visible.
    assert day["training"]["planned_exercise_kcal"] is not None


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

"""The coach layer end to end: store, resolve, compare, refuse.

Each test drives the same functions the tools wrap, against a database in a
temporary directory. The cases are the ones that would be wrong in a way nobody
would notice — a ride scored against the wrong FTP, a re-import that duplicates
a week, a plan stored that cannot be rendered on the morning it is due.
"""

from __future__ import annotations

import pytest

from cycling_mcp import coach, store
from cycling_mcp.garmin_import import GarminPayloadError

RIDE = {
    "activityId": 5001,
    "activityName": "Sweet spot 3x10",
    "activityType": {"typeKey": "virtual_ride"},
    "startTimeLocal": "2026-07-05 07:00:00",
    "startTimeGMT": "2026-07-05 05:00:00",
    "duration": 4200.0,
    "movingDuration": 4180.0,
    "distance": 42000.0,
    "elevationGain": 120.0,
    "averageHR": 150,
    "maxHR": 172,
    "avgPower": 190.0,
    "normPower": 198.0,
}

SPEC = {
    "name": "Sweet Spot 3x10",
    "ftp": 266,
    "blocks": [
        {"type": "steady", "duration": 600, "power_pct": 55, "role": "warmup"},
        {
            "type": "repeat",
            "count": 3,
            "blocks": [
                {"type": "steady", "duration": 600, "power_pct": 90, "role": "interval"},
                {"type": "steady", "duration": 300, "power_pct": 50, "role": "recovery"},
            ],
        },
        {"type": "steady", "duration": 300, "power_pct": 50, "role": "cooldown"},
    ],
}


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setenv(store.ENV_DB_PATH, str(tmp_path / "coach.db"))
    return tmp_path / "coach.db"


# --------------------------------------------------------------------------
# profile and dated history
# --------------------------------------------------------------------------


def test_an_empty_profile_is_an_interview_not_an_error():
    profile = coach.get_profile()
    fields = {gap["field"] for gap in profile["gaps"]}
    assert {"ftp", "availability", "equipment", "objective"} <= fields
    assert all(gap["matters_for"] for gap in profile["gaps"])


def test_a_gap_closes_when_the_answer_is_stored():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    fields = {gap["field"] for gap in coach.get_profile()["gaps"]}
    assert "ftp" not in fields


def test_a_twenty_minute_test_records_the_convention_and_the_arithmetic():
    result = coach.log_ftp(twenty_min_watts=280, effective_date="2026-06-01")
    assert result["stored"]["value_watts"] == 266
    assert result["stored"]["method"] == "20min_test"
    assert "280 W for 20 min x 0.95 = 266 W" in result["stored"]["note"]


def test_giving_both_ftp_forms_is_refused():
    with pytest.raises(coach.CoachError, match="exactly one"):
        coach.log_ftp(value_watts=266, twenty_min_watts=280)


def test_an_ftp_in_the_wrong_units_is_refused_rather_than_stored():
    with pytest.raises(coach.CoachError, match="Check the units"):
        coach.log_ftp(value_watts=3.9)


def test_an_impossible_weight_is_refused():
    with pytest.raises(coach.CoachError, match="Check the units"):
        coach.log_weight(725.0)


def test_a_heavy_weight_is_stored_but_queried_as_possibly_pounds():
    """The range cannot catch this: 160 lb is a plausible weight in kg.

    So the check is a question, not a refusal — 160 kg is real for some people
    and every W/kg from a pounds figure would be wrong by 2.2.
    """
    result = coach.log_weight(160.0)
    assert result["stored"]["value_kg"] == 160.0
    assert "pounds" in result["warning"]


def test_a_threshold_above_max_hr_is_refused():
    with pytest.raises(coach.CoachError, match="one of the two is wrong"):
        coach.log_hr(threshold_hr=195, max_hr=190)


def test_changing_the_ftp_warns_that_stored_targets_moved():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    result = coach.log_ftp(value_watts=290, effective_date="2026-08-01")
    assert result["change"] == "266 W (2026-06-01) -> 290 W (2026-08-01), +24 W"
    assert "re-render" in result["note"]


# --------------------------------------------------------------------------
# the reason FTP is dated
# --------------------------------------------------------------------------


def test_a_ride_is_scored_against_the_ftp_in_effect_on_its_own_date():
    """The single most consequential rule in this layer.

    Scoring a June ride against an August FTP shrinks it: the same watts over a
    bigger number is a smaller IF, so a block of training silently disappears
    the moment the athlete tests better.
    """
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.log_ftp(value_watts=290, effective_date="2026-08-01")
    coach.import_activities([RIDE])  # 2026-07-05, NP 198

    entry = coach.compute_load(start="2026-01-01")["activities"][0]
    assert entry["ftp_used_w"] == 266, "the August FTP did not exist on 5 July"
    assert entry["tss"] == pytest.approx(64.6, abs=0.05)


def test_a_ride_after_the_change_uses_the_new_ftp():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.log_ftp(value_watts=290, effective_date="2026-08-01")
    coach.import_activities([{**RIDE, "startTimeLocal": "2026-08-05 07:00:00"}])
    assert coach.compute_load(start="2026-01-01")["activities"][0]["ftp_used_w"] == 290


def test_a_ride_on_the_day_of_a_change_uses_the_new_ftp():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.log_ftp(value_watts=290, effective_date="2026-08-01")
    coach.import_activities([{**RIDE, "startTimeLocal": "2026-08-01 07:00:00"}])
    assert coach.compute_load(start="2026-01-01")["activities"][0]["ftp_used_w"] == 290


def test_a_ride_before_any_recorded_ftp_is_scored_and_flagged():
    """Refusing would make it vanish from CTL, which reads as a rest week."""
    coach.log_ftp(value_watts=266, effective_date="2026-08-01")
    coach.import_activities([RIDE])  # July, before the only entry
    entry = coach.compute_load(start="2026-01-01")["activities"][0]
    assert entry["ftp_used_w"] == 266
    assert "ftp_extrapolated_backwards" in entry["flags"]


def test_zones_can_be_asked_for_as_of_a_past_date():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.log_ftp(value_watts=290, effective_date="2026-08-01")
    assert coach.get_zones("2026-07-01")["ftp"]["value_watts"] == 266
    assert coach.get_zones("2026-08-15")["ftp"]["value_watts"] == 290


def test_zones_without_an_ftp_say_what_is_missing():
    result = coach.get_zones()
    assert result["power_zones"] == []
    assert "no FTP on file" in result["problem"]


# --------------------------------------------------------------------------
# import idempotence
# --------------------------------------------------------------------------


def test_importing_the_same_payload_twice_reports_unchanged():
    first = coach.import_activities([RIDE])
    second = coach.import_activities([RIDE])
    assert (first["inserted"], first["updated"], first["unchanged"]) == (1, 0, 0)
    assert (second["inserted"], second["updated"], second["unchanged"]) == (0, 0, 1)
    assert coach.list_activities()["count"] == 1


def test_a_changed_field_is_an_update_and_names_what_moved():
    coach.import_activities([RIDE])
    result = coach.import_activities([{**RIDE, "activityName": "Renamed on Garmin"}])
    assert result["updated"] == 1
    assert result["activities"]["updated"][0]["changed_fields"] == ["name"]


def test_a_thinner_re_import_never_blanks_a_stored_value():
    """`get_activities` returns less than `get_activity`.

    Without this rule, re-syncing the weekly list after fetching one ride in
    detail would erase its normalised power while leaving the training load
    that was computed from it.
    """
    coach.import_activities([RIDE])
    summary = {k: v for k, v in RIDE.items() if k not in ("normPower", "averageHR")}
    result = coach.import_activities([summary])
    assert result["unchanged"] == 1
    stored = coach.list_activities()["activities"][0]
    assert stored["normalized_power"] == 198.0
    assert stored["avg_hr"] == 150


def test_a_rejected_item_says_why_and_does_not_stop_the_others():
    result = coach.import_activities([RIDE, {"activityName": "no id here"}])
    assert result["inserted"] == 1 and result["rejected"] == 1
    assert "no activityId" in result["rejections"][0]["reason"]


def test_an_unreadable_payload_is_refused_by_raising():
    """The tool layer renders every refusal in one place.

    Returning {"ok": False} from here worked only because `_coach` spread the
    result after its own "ok" — reorder that one line and a failure is reported
    as a success. The tool-level behaviour is pinned over stdio instead.
    """
    with pytest.raises(GarminPayloadError, match="not JSON"):
        coach.import_activities("last Tuesday's ride")


def test_an_indoor_ride_is_filterable_as_cycling():
    coach.import_activities([RIDE])  # virtual_ride
    assert coach.list_activities(sport="cycling")["count"] == 1
    assert coach.list_activities()["activities"][0]["sub_sport"] == "virtual_ride"


def test_annotations_survive_a_re_import():
    """The subjective layer belongs to this server, not to Garmin."""
    coach.import_activities([RIDE])
    coach.annotate_activity(garmin_activity_id="5001", rpe=8, feel="cooked by the third")
    coach.import_activities([RIDE])
    stored = coach.list_activities()["activities"][0]
    assert stored["rpe"] == 8 and stored["feel"] == "cooked by the third"


def test_an_rpe_outside_the_scale_is_refused():
    coach.import_activities([RIDE])
    with pytest.raises(coach.CoachError, match="1-10"):
        coach.annotate_activity(garmin_activity_id="5001", rpe=12)


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


def test_a_valid_spec_is_stored_with_its_session_read_back_out():
    result = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    stored = result["planned_workouts"][0]
    assert result["saved"] == 1
    assert stored["name"] == "Sweet Spot 3x10"
    assert stored["planned_seconds"] == 3600
    assert "Sweet Spot 3x10" in stored["table"]
    assert stored["spec"] == SPEC, "the spec must be stored verbatim and stay renderable"


def test_an_invalid_spec_is_refused_rather_than_stored():
    """A plan that cannot be rendered would fail on the morning it is due."""
    result = coach.save_planned_workouts(
        [{"spec": {"name": "No FTP", "blocks": []}, "scheduled_date": "2026-07-06"}]
    )
    assert result["saved"] == 0 and result["refused"] == 1
    assert any("ftp" in error for error in result["refusals"][0]["errors"])


def test_one_bad_session_does_not_lose_the_rest_of_the_week():
    result = coach.save_planned_workouts(
        [
            {"spec": SPEC, "scheduled_date": "2026-07-05"},
            {"spec": {"name": "broken"}, "scheduled_date": "2026-07-07"},
            {"spec": SPEC, "scheduled_date": "2026-07-09"},
        ]
    )
    assert result["saved"] == 2 and result["refused"] == 1


def test_a_spec_with_a_typo_key_is_stored_with_the_warning_attached():
    spec = {**SPEC, "blocks": [{"type": "steady", "duration": 600, "power_pct": 60, "powr": 1}]}
    result = coach.save_planned_workouts([{"spec": spec, "scheduled_date": "2026-07-05"}])
    assert result["saved"] == 1
    assert any("powr" in warning for warning in result["planned_workouts"][0]["warnings"])


def test_a_second_session_on_one_day_is_flagged_not_blocked():
    coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    result = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    assert result["saved"] == 1
    assert "already planned" in result["planned_workouts"][0]["warning"]


def test_replacing_a_spec_with_an_invalid_one_changes_nothing():
    """Raised, not returned. A returned {"updated": False} came back inside the
    tool layer's {"ok": True, ...} envelope — a failure reported as success."""
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    planned_id = saved["planned_workouts"][0]["id"]
    with pytest.raises(coach.CoachError, match="nothing was changed"):
        coach.update_planned_workout(planned_id, spec={"name": "broken"})
    assert coach.get_week("2026-07-01", "2026-07-10")["planned_workouts"][0]["spec"] == SPEC


def test_marking_pushed_without_a_platform_is_called_out():
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    result = coach.update_planned_workout(saved["planned_workouts"][0]["id"], status="pushed")
    assert "pushed_to is unset" in result["note"]


# --------------------------------------------------------------------------
# the week
# --------------------------------------------------------------------------


def _week_with_a_plan_and_a_ride():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    saved = coach.save_planned_workouts(
        [
            {"spec": SPEC, "scheduled_date": "2026-07-05"},
            {"spec": SPEC, "scheduled_date": "2026-07-07"},
        ]
    )
    coach.import_activities([RIDE])
    return saved["planned_workouts"][0]["id"]


def test_a_session_whose_day_has_passed_with_no_ride_is_a_deviation():
    _week_with_a_plan_and_a_ride()
    week = coach.get_week("2026-07-01", "2026-07-12", today="2026-07-20")
    missed = week["deviations"]["planned_not_ridden"]
    assert [entry["scheduled_date"] for entry in missed] == ["2026-07-05", "2026-07-07"]
    assert "no ride is linked" in missed[0]["sentence"]


def test_a_future_session_is_not_yet_a_deviation():
    _week_with_a_plan_and_a_ride()
    week = coach.get_week("2026-07-01", "2026-07-12", today="2026-07-06")
    assert [e["scheduled_date"] for e in week["deviations"]["planned_not_ridden"]] == ["2026-07-05"]


def test_an_unlinked_ride_is_reported_as_unplanned():
    _week_with_a_plan_and_a_ride()
    week = coach.get_week("2026-07-01", "2026-07-12", today="2026-07-20")
    assert len(week["deviations"]["ridden_not_planned"]) == 1
    assert "nothing planned for it" in week["deviations"]["ridden_not_planned"][0]["sentence"]


def test_linking_the_ride_clears_both_deviations_for_that_day():
    planned_id = _week_with_a_plan_and_a_ride()
    coach.link_activity(planned_id, auto=True)
    week = coach.get_week("2026-07-01", "2026-07-12", today="2026-07-20")
    assert week["deviations"]["ridden_not_planned"] == []
    assert [e["scheduled_date"] for e in week["deviations"]["planned_not_ridden"]] == ["2026-07-07"]


def test_a_race_day_ride_linked_to_an_event_is_not_an_unplanned_deviation():
    """It was training the plan knew about. It still counts toward load."""
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities([RIDE])
    event = coach.add_event("Club 100", "2026-07-05", priority="B")["stored"]
    coach.record_race_result(event["id"], garmin_activity_id="5001", finish_time="1:10:00")
    week = coach.get_week("2026-07-01", "2026-07-12", today="2026-07-20")
    assert week["deviations"]["ridden_not_planned"] == []
    assert week["totals"]["actual_tss"] == pytest.approx(64.6, abs=0.05)


def test_a_stale_ftp_on_a_stored_session_is_flagged():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-08-05"}])
    coach.log_ftp(value_watts=290, effective_date="2026-08-01")
    week = coach.get_week("2026-08-01", "2026-08-10", today="2026-08-02")
    assert "written against 266 W" in week["planned_workouts"][0]["stale_ftp"]


def test_a_backwards_date_range_is_refused():
    with pytest.raises(coach.CoachError, match="is before start"):
        coach.get_week("2026-07-10", "2026-07-01")


# --------------------------------------------------------------------------
# linking
# --------------------------------------------------------------------------


def test_auto_link_refuses_to_choose_between_two_rides_on_one_day():
    """Picking the wrong one produces a report that is confidently about the
    wrong session, and nothing downstream would ever reveal it."""
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities(
        [RIDE, {**RIDE, "activityId": 5002, "activityName": "Commute", "duration": 1800.0}]
    )
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    result = coach.link_activity(saved["planned_workouts"][0]["id"], auto=True)
    assert result["linked"] is False
    assert len(result["candidates"]) == 2
    assert "ambiguous" in result["reason"]


def test_auto_link_with_no_ride_that_day_says_so():
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-09"}])
    result = coach.link_activity(saved["planned_workouts"][0]["id"], auto=True)
    assert result["linked"] is False
    assert "no cycling activity" in result["reason"]


def test_linking_marks_the_session_completed():
    planned_id = _week_with_a_plan_and_a_ride()
    result = coach.link_activity(planned_id, auto=True)
    assert result["linked"] is True
    assert result["planned_workout"]["status"] == "completed"


def test_linking_a_ride_from_another_day_warns():
    coach.import_activities([RIDE])
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-08"}])
    result = coach.link_activity(saved["planned_workouts"][0]["id"], garmin_activity_id="5001")
    assert result["linked"] is True
    assert "planned for 2026-07-08" in result["warning"]


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------


def test_the_next_a_event_is_the_anchor_and_the_bs_are_not():
    coach.add_event("Local crit", "2026-09-01", priority="B")
    coach.add_event("La Grande", "2026-09-27", priority="A", distance_km=148, elevation_m=2412)
    coach.add_event("Next year", "2027-06-01", priority="A")
    listing = coach.list_events(today="2026-08-22")
    assert listing["next_a_event"]["name"] == "La Grande"
    assert listing["weeks_to_next_a_event"] == pytest.approx(5.1, abs=0.05)


def test_past_and_upcoming_can_be_asked_for_separately():
    coach.add_event("Last year", "2025-09-27", priority="A", status="completed")
    coach.add_event("This year", "2026-09-27", priority="A")
    assert coach.list_events("past", today="2026-08-22")["count"] == 1
    assert coach.list_events("upcoming", today="2026-08-22")["count"] == 1


def test_a_result_refuses_a_ride_from_the_wrong_day():
    """The realistic slip is the Sunday spin after a Saturday race."""
    coach.import_activities([RIDE])  # 2026-07-05
    event = coach.add_event("Club 100", "2026-07-04", priority="B")["stored"]
    with pytest.raises(coach.CoachError, match="is dated 2026-07-05"):
        coach.record_race_result(event["id"], garmin_activity_id="5001")


def test_a_result_with_force_links_anyway():
    coach.import_activities([RIDE])
    event = coach.add_event("Club 100", "2026-07-04", priority="B")["stored"]
    result = coach.record_race_result(event["id"], garmin_activity_id="5001", force=True)
    assert result["stored"]["linked_activity_id"] is not None


def test_a_finish_time_takes_the_clock_form_and_reads_back_the_same():
    event = coach.add_event("Club 100", "2026-07-04", priority="B")["stored"]
    result = coach.record_race_result(event["id"], finish_time="4:32:10", debrief="held on")
    assert result["stored"]["finish_time_s"] == 16330
    assert result["stored"]["finish_time"] == "4:32:10"
    assert result["stored"]["status"] == "completed"


def test_a_result_without_a_debrief_asks_for_one():
    event = coach.add_event("Club 100", "2026-07-04", priority="B")["stored"]
    result = coach.record_race_result(event["id"], finish_time=16330)
    assert "No debrief stored" in result["missing"]


def test_a_debrief_comes_back_when_the_event_is_listed_again():
    event = coach.add_event("La Grande", "2025-09-27", priority="A", status="completed")["stored"]
    coach.record_race_result(event["id"], debrief="cracked at 120 km, ate nothing after hour three")
    past = coach.list_events("past", today="2026-08-22")["events"][0]
    assert "cracked at 120 km" in past["debrief"]


# --------------------------------------------------------------------------
# form and compliance
# --------------------------------------------------------------------------


def test_form_is_computed_from_the_stored_rides():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities([RIDE])
    result = coach.get_form("2026-07-05", "2026-07-06")
    assert result["series"][0]["tss"] == pytest.approx(64.6, abs=0.05)
    # 64.642 / 42 = 1.539 and 64.642 / 7 = 9.235, reported to one decimal.
    assert result["series"][0]["ctl"] == 1.5
    assert result["series"][1]["tsb"] == -7.7


def test_a_short_history_is_reported_as_not_yet_converged():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities([RIDE])
    assert "warmup_incomplete" in coach.get_form("2026-07-06", "2026-07-10")


def test_form_says_when_heart_rate_rides_are_in_the_mix():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.log_hr(threshold_hr=170, effective_date="2026-06-01")
    coach.import_activities([{k: v for k, v in RIDE.items() if k not in ("avgPower", "normPower")}])
    assert "mixed_methods_warning" in coach.get_form("2026-07-05", "2026-07-06")


# Eight laps for eight blocks, summing to the ride's 4200 s. Against a 266 W
# FTP the plan asks for 146 / 239 / 133 W, so: the intervals go 240, 228, 205 —
# the third one falling away — the recoveries are ridden easier than the
# ceiling, and the cooldown runs 15 minutes instead of 5.
LAPS = {
    "lapDTOs": [
        {"duration": 600.0, "averagePower": 145.0, "averageHR": 120},
        {"duration": 600.0, "averagePower": 240.0, "averageHR": 158},
        {"duration": 300.0, "averagePower": 110.0, "averageHR": 130},
        {"duration": 600.0, "averagePower": 228.0, "averageHR": 160},
        {"duration": 300.0, "averagePower": 110.0, "averageHR": 128},
        {"duration": 600.0, "averagePower": 205.0, "averageHR": 162},
        {"duration": 300.0, "averagePower": 110.0, "averageHR": 125},
        {"duration": 900.0, "averagePower": 120.0, "averageHR": 115},
    ]
}


def _linked_session_with_laps():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities([RIDE])
    coach.import_activity_laps(LAPS, garmin_activity_id="5001")
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    planned_id = saved["planned_workouts"][0]["id"]
    coach.link_activity(planned_id, auto=True)
    return planned_id


def test_compliance_reads_block_by_block_when_the_laps_line_up():
    """Eight laps against eight blocks — the repeat expanded, as a head unit does."""
    report = coach.compliance_report(_linked_session_with_laps())
    assert report["alignment"] == "by_lap"
    assert len(report["blocks"]) == 8
    assert report["blocks"][5]["verdict"] == "under"
    assert report["blocks"][5]["sentence"] == "the sixth block fell to 205 W against a 239 W target"


def test_compliance_does_not_count_an_easy_recovery_against_the_athlete():
    report = coach.compliance_report(_linked_session_with_laps())
    recoveries = [block for block in report["blocks"] if block["role"] == "recovery"]
    assert [block["verdict"] for block in recoveries] == ["easier_than_target"] * 3
    assert report["off_target_blocks"] == 1


def test_compliance_notices_a_block_ridden_far_longer_than_planned():
    report = coach.compliance_report(_linked_session_with_laps())
    assert report["blocks"][7]["duration_verdict"] == "long"
    assert "15:00 ridden against 05:00 planned" in report["blocks"][7]["sentence"]


def test_compliance_refuses_to_pair_laps_that_do_not_match_the_plan():
    """Six laps against eight blocks aligned by position is a confident lie."""
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities([RIDE])
    coach.import_activity_laps({"lapDTOs": LAPS["lapDTOs"][:6]}, garmin_activity_id="5001")
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    planned_id = saved["planned_workouts"][0]["id"]
    coach.link_activity(planned_id, auto=True)

    report = coach.compliance_report(planned_id)
    assert report["alignment"] == "mismatch"
    assert report["blocks"] == []
    assert any("6 laps were recorded against 8" in s for s in report["sentences"])


def test_compliance_without_laps_compares_the_totals_and_says_so():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities([RIDE])
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    planned_id = saved["planned_workouts"][0]["id"]
    coach.link_activity(planned_id, auto=True)

    report = coach.compliance_report(planned_id)
    assert report["alignment"] == "none"
    assert any("No laps are stored" in s for s in report["sentences"])
    assert report["planned"]["duration"] == "1:00:00"


def test_compliance_needs_something_to_compare_against():
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    with pytest.raises(coach.CoachError, match="no activity linked"):
        coach.compliance_report(saved["planned_workouts"][0]["id"])


def test_split_summaries_are_refused_by_the_lap_import():
    coach.import_activities([RIDE])
    with pytest.raises(GarminPayloadError, match="get_activity_splits"):
        coach.import_activity_laps(
            {"splitSummaries": [{"splitType": "CLIMB"}]}, garmin_activity_id="5001"
        )


def test_laps_that_do_not_add_up_to_the_ride_are_flagged():
    coach.import_activities([RIDE])
    result = coach.import_activity_laps(
        {"lapDTOs": [{"duration": 600.0}]}, garmin_activity_id="5001"
    )
    assert "belong to a different" in result["warning"]


def test_re_importing_laps_replaces_rather_than_doubles_them():
    coach.import_activities([RIDE])
    coach.import_activity_laps(LAPS, garmin_activity_id="5001")
    result = coach.import_activity_laps(LAPS, garmin_activity_id="5001")
    assert result["stored_laps"] == 8


# --------------------------------------------------------------------------
# backup and restore
# --------------------------------------------------------------------------


def _populate():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.log_weight(72.5, "2026-06-01")
    coach.import_activities([RIDE])
    coach.import_activity_laps(LAPS, garmin_activity_id="5001")
    coach.add_event("La Grande", "2026-09-27", priority="A")
    coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])


def test_a_restore_round_trips_every_table():
    _populate()
    export = coach.export_data()
    restored = coach.import_data(export["data"], force=True, expected_digest=export["digest"])
    assert restored["restored"] is True
    assert coach.export_data()["digest"] == export["digest"], "the restore must be lossless"


def test_a_restore_refuses_a_non_empty_database_by_default():
    _populate()
    export = coach.export_data()
    result = coach.import_data(export["data"])
    assert result["restored"] is False
    assert "already holds data" in result["reason"]


def test_a_restore_refuses_a_payload_that_does_not_match_its_digest():
    _populate()
    export = coach.export_data()
    result = coach.import_data(export["data"], force=True, expected_digest="0123456789abcdef")
    assert result["restored"] is False
    assert "not the export it claims to be" in result["reason"]
    assert coach.list_activities()["count"] == 1, "nothing may have been touched"


def test_a_restore_from_a_newer_schema_is_refused():
    _populate()
    export = coach.export_data()
    export["data"]["schema_version"] = store.CURRENT_SCHEMA_VERSION + 5
    result = coach.import_data(export["data"], force=True)
    assert result["restored"] is False
    assert "Upgrade first" in result["reason"]


def test_a_restore_into_an_empty_database_needs_no_force():
    _populate()
    export = coach.export_data()
    coach.import_data(export["data"], force=True)  # clears and refills
    assert coach.list_activities()["count"] == 1


# --------------------------------------------------------------------------
# defects found in review, each pinned so it cannot come back
# --------------------------------------------------------------------------


def test_a_partial_hr_entry_does_not_shadow_the_thresholds_on_file():
    """log_hr takes any subset, so logging a resting HR alone is normal.

    Resolving the latest *row* rather than the latest value per field let that
    entry erase a threshold recorded months earlier — and every no-power ride
    then stopped being scored, with the reason "no threshold HR is on file".
    """
    coach.log_hr(threshold_hr=170, max_hr=190, effective_date="2026-01-01")
    coach.log_hr(resting_hr=45, effective_date="2026-06-01")

    zones = coach.get_zones("2026-07-01")
    assert zones["threshold_hr"] == 170
    assert zones["hr_zones"], "the zones must survive a later partial entry"
    assert not [gap for gap in coach.get_profile()["gaps"] if gap["field"] == "threshold_hr"]


def test_each_hr_figure_keeps_the_date_it_came_from():
    coach.log_hr(threshold_hr=170, max_hr=190, effective_date="2026-01-01")
    coach.log_hr(resting_hr=45, effective_date="2026-06-01")
    dates = coach.get_profile()["hr"]["effective_dates"]
    assert dates == {
        "threshold_hr": "2026-01-01",
        "max_hr": "2026-01-01",
        "resting_hr": "2026-06-01",
    }


def test_a_newer_threshold_still_wins_and_an_older_date_is_unaffected():
    coach.log_hr(threshold_hr=170, effective_date="2026-01-01")
    coach.log_hr(threshold_hr=175, effective_date="2026-07-01")
    assert coach.get_zones("2026-08-01")["threshold_hr"] == 175
    assert coach.get_zones("2026-02-01")["threshold_hr"] == 170


def test_a_backdated_entry_returns_the_row_it_actually_wrote():
    """Re-reading by date returns a different row than the one just inserted.

    The write then reported someone else's numbers as what it stored — with, in
    the same response, zones computed from the value that was passed in. The
    two halves contradicted each other.
    """
    coach.log_ftp(value_watts=260, effective_date="2026-08-01")
    result = coach.log_ftp(value_watts=200, effective_date="2026-01-01")
    assert result["stored"]["value_watts"] == 200
    assert result["stored"]["effective_date"] == "2026-01-01"
    assert result["is_current"] is False
    assert "still applies from that date onward" in result["note"]


def test_a_backdated_weight_and_hr_also_return_their_own_row():
    coach.log_weight(72.0, "2026-08-01")
    assert coach.log_weight(80.0, "2026-01-01")["stored"]["value_kg"] == 80.0
    coach.log_hr(threshold_hr=170, effective_date="2026-08-01")
    assert (
        coach.log_hr(threshold_hr=160, effective_date="2026-01-01")["stored"]["threshold_hr"] == 160
    )


def test_the_change_line_compares_against_what_this_entry_replaces():
    """Not against the globally latest entry, which may be in the future."""
    coach.log_ftp(value_watts=250, effective_date="2026-01-01")
    coach.log_ftp(value_watts=300, effective_date="2026-09-01")
    result = coach.log_ftp(value_watts=270, effective_date="2026-05-01")
    assert result["change"] == "250 W (2026-01-01) -> 270 W (2026-05-01), +20 W"
    assert result["is_current"] is False


def test_a_block_cut_short_makes_the_session_a_deviation():
    """The watts were right and the interval was abandoned half-way.

    Counting only under/over reported that as prescribed, which is the opposite
    of what happened.
    """
    coach.log_ftp(value_watts=280, effective_date="2026-06-01")
    coach.import_activities([{**RIDE, "activityId": 8001, "duration": 1200.0}])
    spec = {
        "name": "2x10",
        "ftp": 280,
        "blocks": [
            {"type": "steady", "duration": 600, "power_pct": 95, "role": "interval"},
            {"type": "steady", "duration": 600, "power_pct": 95, "role": "interval"},
        ],
    }
    coach.import_activity_laps(
        {
            "lapDTOs": [
                {"duration": 300.0, "averagePower": 266.0},
                {"duration": 600.0, "averagePower": 266.0},
            ]
        },
        garmin_activity_id="8001",
    )
    saved = coach.save_planned_workouts([{"spec": spec, "scheduled_date": "2026-07-05"}])
    planned_id = saved["planned_workouts"][0]["id"]
    coach.link_activity(planned_id, garmin_activity_id="8001")

    report = coach.compliance_report(planned_id)
    assert [block["duration_verdict"] for block in report["blocks"]] == ["short", "on_time"]
    assert [block["verdict"] for block in report["blocks"]] == ["on_target", "on_target"]
    assert report["off_target_blocks"] == 0, "the power was right"
    assert report["off_duration_blocks"] == 1
    assert report["deviating_blocks"] == 1
    assert report["verdict"] == "deviated"


def test_the_laps_come_back_even_when_they_could_not_be_paired():
    """The docstring promises them, and a mismatch is exactly when they matter."""
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities([RIDE])
    coach.import_activity_laps({"lapDTOs": LAPS["lapDTOs"][:6]}, garmin_activity_id="5001")
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    planned_id = saved["planned_workouts"][0]["id"]
    coach.link_activity(planned_id, auto=True)

    report = coach.compliance_report(planned_id)
    assert report["alignment"] == "mismatch"
    assert len(report["laps"]) == 6
    assert report["laps"][0]["avg_power"] == 145.0


def test_a_thinner_re_import_cannot_reclassify_a_ride_as_unknown():
    """`sport` is derived, so it is never null — the null-guard did not cover it.

    A re-import without a type key rewrote a virtual_ride to sport "other",
    and the ride then vanished from every cycling filter and from auto-linking
    while still showing `sub_sport: virtual_ride`.
    """
    coach.import_activities([RIDE])
    result = coach.import_activities([{k: v for k, v in RIDE.items() if k != "activityType"}])
    assert result["unchanged"] == 1
    stored = coach.list_activities()["activities"][0]
    assert (stored["sport"], stored["sub_sport"]) == ("cycling", "virtual_ride")
    assert coach.list_activities(sport="cycling")["count"] == 1


def test_an_activity_with_no_type_at_all_is_stored_with_no_sport():
    """Null, not "other" — the difference is what protects the re-import."""
    result = coach.import_activities([{k: v for k, v in RIDE.items() if k != "activityType"}])
    assert result["inserted"] == 1
    assert "no_sport_type" in result["flags"][0]["flags"]
    assert coach.list_activities()["activities"][0]["sport"] is None


def test_a_restore_of_something_that_is_not_an_export_refuses_rather_than_crashing():
    _populate()
    with pytest.raises(coach.CoachError, match="not a row object"):
        coach.import_data({"tables": {"athlete": ["oops"]}}, force=True)
    assert coach.list_activities()["count"] == 1, "the delete sweep must be rolled back"


# --------------------------------------------------------------------------
# defects found in code review of the pull request, each pinned
# --------------------------------------------------------------------------


def test_totals_do_not_count_an_unscored_ride_as_zero():
    """A null TSS means the ride could not be scored, not that it was easy.

    Folding it to zero made a week with unscored rides read as a light week —
    the same mistake as treating a rest day and an unmeasured ride alike.
    """
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities(
        [
            RIDE,
            {
                "activityId": 5002,
                "activityType": {"typeKey": "road_biking"},
                "startTimeLocal": "2026-07-06 08:00:00",
                "duration": 7200.0,
            },
        ]
    )
    listing = coach.list_activities()
    assert listing["scored"] == 1
    assert listing["unscored"] == 1
    assert "understates" in listing["unscored_warning"]
    assert listing["total_tss"] == pytest.approx(64.6, abs=0.05)


def test_a_week_total_says_what_it_is_made_of():
    """planned_tss is always power-based, so actual_tss has to declare itself."""
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.log_hr(threshold_hr=170, effective_date="2026-06-01")
    coach.import_activities(
        [
            RIDE,
            {
                "activityId": 5003,
                "activityType": {"typeKey": "road_biking"},
                "startTimeLocal": "2026-07-06 08:00:00",
                "duration": 3600.0,
                "averageHR": 150,
            },
        ]
    )
    totals = coach.get_week("2026-07-01", "2026-07-12", today="2026-07-20")["totals"]
    assert totals["by_method"] == {"power": 1, "hr": 1}
    assert (
        "not comparable" in totals["mixed_methods_warning"]
        or "different quantities" in (totals["mixed_methods_warning"])
    )
    assert totals["activities_scored"] == 2


def test_a_ride_with_no_power_in_any_lap_is_unverifiable_not_deviated():
    """`no_power` says nothing about whether the target was held.

    Counting it as a deviation made an HR-only ride, ridden for exactly the
    right durations, come back as a failed session.
    """
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities(
        [{**RIDE, "activityId": 6001, "avgPower": None, "normPower": None, "duration": 1200.0}]
    )
    spec = {
        "name": "2x10",
        "ftp": 266,
        "blocks": [
            {"type": "steady", "duration": 600, "power_pct": 90, "role": "interval"},
            {"type": "steady", "duration": 600, "power_pct": 90, "role": "interval"},
        ],
    }
    coach.import_activity_laps(
        {"lapDTOs": [{"duration": 600.0, "averageHR": 150}, {"duration": 600.0, "averageHR": 152}]},
        garmin_activity_id="6001",
    )
    saved = coach.save_planned_workouts([{"spec": spec, "scheduled_date": "2026-07-05"}])
    planned_id = saved["planned_workouts"][0]["id"]
    coach.link_activity(planned_id, garmin_activity_id="6001")

    report = coach.compliance_report(planned_id)
    assert report["unverifiable_blocks"] == 2
    assert report["deviating_blocks"] == 0
    assert report["verdict"] == "unverifiable"


def test_a_ride_linked_to_a_plan_outside_the_window_is_not_unplanned():
    """A Sunday session ridden Monday, linked, then read in the Monday week.

    The link lives on the planned workout, so looking only at plans scheduled
    inside the window reported the ride as unplanned with the link sitting in
    the database the whole time.
    """
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities([RIDE])  # 2026-07-05
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-04"}])
    coach.link_activity(saved["planned_workouts"][0]["id"], garmin_activity_id="5001")

    week = coach.get_week("2026-07-05", "2026-07-11", today="2026-07-20")
    assert week["deviations"]["ridden_not_planned"] == []


def test_a_race_linked_to_an_event_outside_the_window_is_not_unplanned():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities([RIDE])
    event = coach.add_event("Club 100", "2026-07-04", priority="B")["stored"]
    coach.record_race_result(event["id"], garmin_activity_id="5001", force=True)

    week = coach.get_week("2026-07-05", "2026-07-11", today="2026-07-20")
    assert week["deviations"]["ridden_not_planned"] == []


def test_a_ride_with_an_unknown_sport_can_still_be_auto_linked():
    """NULL sport means Garmin sent no type, not that it was not a bike.

    Excluding it reported "no cycling activity stored on that date" for a ride
    sitting in the log on exactly that date.
    """
    coach.import_activities([{k: v for k, v in RIDE.items() if k != "activityType"}])
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    result = coach.link_activity(saved["planned_workouts"][0]["id"], auto=True)
    assert result["linked"] is True
    assert result["activity"]["sport"] is None


def test_a_sport_filter_reports_the_unknown_rides_it_hid():
    coach.import_activities([{k: v for k, v in RIDE.items() if k != "activityType"}])
    listing = coach.list_activities(sport="cycling")
    assert listing["count"] == 0
    assert listing["excluded_unknown_sport"] == 1
    assert "carries no sport" in listing["excluded_unknown_sport_note"]


def test_a_debrief_added_later_does_not_resurrect_an_abandoned_race():
    """Filing a debrief months on must not rewrite a DNF into a finish."""
    event = coach.add_event("Club 100", "2026-07-04", priority="B")["stored"]
    coach.update_event(event["id"], status="abandoned")
    result = coach.record_race_result(event["id"], debrief="cracked at km 90")
    assert result["stored"]["status"] == "abandoned"
    assert result["stored"]["debrief"] == "cracked at km 90"
    assert "stays abandoned" in result["status_unchanged"]


def test_a_result_on_an_upcoming_event_still_completes_it():
    event = coach.add_event("Club 100", "2026-07-04", priority="B")["stored"]
    result = coach.record_race_result(event["id"], finish_time="3:00:00")
    assert result["stored"]["status"] == "completed"


def test_an_explicit_status_still_wins():
    event = coach.add_event("Club 100", "2026-07-04", priority="B")["stored"]
    coach.update_event(event["id"], status="abandoned")
    result = coach.record_race_result(event["id"], status="completed")
    assert result["stored"]["status"] == "completed"


def test_a_future_dated_ftp_does_not_make_this_week_stale():
    """log_ftp has no future-date guard, and a scheduled test result or a typo
    made every correctly-written session this week report stale_ftp against a
    number not yet in force."""
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.log_ftp(value_watts=300, effective_date="2026-12-01")
    coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-08-05"}])
    week = coach.get_week("2026-08-01", "2026-08-10", today="2026-08-02")
    assert "stale_ftp" not in week["planned_workouts"][0]


def test_a_genuinely_stale_spec_is_still_flagged():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-08-05"}])
    coach.log_ftp(value_watts=290, effective_date="2026-08-01")
    week = coach.get_week("2026-08-01", "2026-08-10", today="2026-08-02")
    assert "written against 266 W" in week["planned_workouts"][0]["stale_ftp"]


def test_an_empty_activity_id_filter_selects_nothing():
    """Falsy-checking the list dropped the clause and scored the whole history —
    a plausible wrong number, which is worse than an empty one."""
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities([RIDE])
    result = coach.compute_load(activity_ids=[])
    assert result["count"] == 0
    assert result["total_tss"] == 0.0
    assert "empty list" in result["note"]


def test_a_populated_activity_id_filter_still_works():
    coach.log_ftp(value_watts=266, effective_date="2026-06-01")
    coach.import_activities([RIDE])
    stored = coach.list_activities()["activities"][0]
    assert coach.compute_load(activity_ids=[stored["id"]])["count"] == 1


def test_an_estimated_threshold_hr_stays_an_open_gap():
    """resolve_hr substitutes 92% of max HR, so testing the resolved dict closed
    the gap the moment a max HR existed — the athlete was never asked for a
    measured LTHR, and every hrTSS stayed pinned to the estimate."""
    coach.log_hr(max_hr=190, effective_date="2026-06-01")
    gaps = {gap["field"]: gap for gap in coach.get_profile()["gaps"]}
    assert "threshold_hr" in gaps
    assert "estimated at 175 bpm" in gaps["threshold_hr"]["matters_for"]


def test_a_measured_threshold_closes_the_gap():
    coach.log_hr(threshold_hr=170, max_hr=190, effective_date="2026-06-01")
    assert "threshold_hr" not in {gap["field"] for gap in coach.get_profile()["gaps"]}


def test_the_truncated_flag_is_not_true_for_an_exact_fit():
    coach.import_activities([RIDE])
    assert coach.list_activities(limit=1)["truncated"] is False
    coach.import_activities([{**RIDE, "activityId": 5009, "startTimeLocal": "2026-07-06 08:00:00"}])
    assert coach.list_activities(limit=1)["truncated"] is True


# --------------------------------------------------------------------------
# the shared machinery, pinned so a future split re-introduces the drift
# --------------------------------------------------------------------------


def test_a_plan_and_a_ride_are_scored_by_the_same_formula():
    """compliance_report sets these two numbers side by side.

    Two copies of the TSS formula would make that comparison a report on the
    arithmetic rather than on the session, and the drift would be invisible.
    """
    from cycling_mcp.metrics import compute_metrics
    from cycling_mcp.spec import load_spec
    from cycling_mcp.training import power_tss

    spec = {
        "name": "One hour at threshold",
        "ftp": 250,
        "blocks": [{"type": "steady", "duration": 3600, "power_pct": 100}],
    }
    planned = compute_metrics(load_spec(spec)).tss
    ridden = power_tss(3600, 250, 250)
    assert planned == pytest.approx(ridden)
    assert planned == pytest.approx(100.0)


def test_the_ftp_plausibility_band_is_one_set_of_numbers():
    """It was 50-600 in the validator and 40-700/80-500 in the store, so a
    550 W FTP was queried when stored and accepted when rendered.

    Asserted on behaviour rather than on a constant, so aliasing the numbers
    under a second name does not make this pass again.
    """
    from cycling_mcp.spec import FTP_PLAUSIBLE_W, FTP_USUAL_W, validate_spec

    just_inside = FTP_USUAL_W[1]
    just_outside = FTP_USUAL_W[1] + 50
    for value, expect_warning in ((just_inside, False), (just_outside, True)):
        _, errors, warnings = validate_spec(
            {
                "name": "x",
                "ftp": value,
                "blocks": [{"type": "steady", "duration": 600, "power_pct": 60}],
            }
        )
        assert errors == []
        assert bool([w for w in warnings if "unusual" in w]) is expect_warning
        stored = coach.log_ftp(value_watts=value, effective_date="2026-01-01")
        assert bool(stored["warnings"]) is expect_warning

    with pytest.raises(coach.CoachError, match="Check the units"):
        coach.log_ftp(value_watts=FTP_PLAUSIBLE_W[1] + 100)


def test_the_store_refuses_what_the_renderer_merely_warns_about():
    """The two layers act differently on the same band, on purpose."""
    from cycling_mcp.spec import validate_spec

    _, errors, warnings = validate_spec(
        {"name": "x", "ftp": 550, "blocks": [{"type": "steady", "duration": 600, "power_pct": 60}]}
    )
    assert errors == []
    assert any("unusual" in warning for warning in warnings)

    result = coach.log_ftp(value_watts=550)
    assert result["stored"]["value_watts"] == 550
    assert any("unusual" in warning for warning in result["warnings"])
    with pytest.raises(coach.CoachError, match="Check the units"):
        coach.log_ftp(value_watts=900)


def test_history_is_read_once_and_resolved_in_memory():
    """Scoring a season used to issue up to eight lookups per ride, for the
    same handful of rows every time."""
    import sqlite3

    coach.log_ftp(value_watts=266, effective_date="2026-01-01")
    coach.log_hr(threshold_hr=170, max_hr=190, effective_date="2026-01-01")
    coach.import_activities(
        [
            {
                "activityId": 20000 + index,
                "activityType": {"typeKey": "virtual_ride"},
                "startTimeLocal": f"2026-03-{1 + index:02d} 08:00:00",
                "duration": 3600.0,
                "normPower": 200.0,
            }
            for index in range(20)
        ]
    )

    counted = {"n": 0}

    class Counting(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            counted["n"] += 1
            return super().execute(*args, **kwargs)

    real = sqlite3.connect
    try:
        sqlite3.connect = lambda *a, **k: real(
            *a, factory=Counting, **{key: value for key, value in k.items() if key != "factory"}
        )
        coach.get_form("2026-03-01", "2026-03-31")
        queries = counted["n"]
    finally:
        sqlite3.connect = real

    # A fixed handful: the migration check, the three history tables, and one
    # pass over the activities. It must not scale with the number of rides.
    assert queries < 20, f"{queries} queries for 20 activities — the history is not being reused"


def test_a_backdated_weight_says_it_is_not_current():
    """Only log_ftp used to say so; the same defect wore different units in the
    other two loggers."""
    coach.log_ftp(value_watts=266, effective_date="2026-01-01")
    coach.log_weight(72.0, "2026-08-01")
    result = coach.log_weight(80.0, "2026-01-01")
    assert result["is_current"] is False
    assert "72 kg (2026-08-01) still applies" in result["note"]
    assert result["watts_per_kg_as_of"] == "2026-01-01"


def test_a_backdated_threshold_hr_says_which_field_was_superseded():
    coach.log_hr(threshold_hr=175, effective_date="2026-08-01")
    result = coach.log_hr(threshold_hr=160, effective_date="2026-01-01")
    assert result["is_current"] is False
    assert "threshold_hr" in result["note"]


def test_a_backdated_resting_hr_supersedes_nothing_about_the_threshold():
    """Per field, because a later threshold entry says nothing about resting HR."""
    coach.log_hr(threshold_hr=175, effective_date="2026-08-01")
    result = coach.log_hr(resting_hr=45, effective_date="2026-01-01")
    assert result["is_current"] is True


def test_import_flags_are_stored_not_only_reported():
    """A caveat that exists only in the response to the import call is a caveat
    nobody has by the time the week is read."""
    coach.import_activities([{k: v for k, v in RIDE.items() if k != "startTimeLocal"}])
    stored = coach.list_activities()["activities"][0]
    assert "local_date_from_utc" in stored["flags"]


def test_a_clean_import_stores_an_empty_flag_list():
    coach.import_activities([RIDE])
    assert coach.list_activities()["activities"][0]["flags"] == []


def test_flags_survive_a_re_import():
    coach.import_activities([{k: v for k, v in RIDE.items() if k != "startTimeLocal"}])
    coach.import_activities([{k: v for k, v in RIDE.items() if k != "startTimeLocal"}])
    assert "local_date_from_utc" in coach.list_activities()["activities"][0]["flags"]


def test_a_thinner_re_import_does_not_stamp_a_no_power_flag_on_a_powered_ride():
    """Flags describe the stored row, not the payload that arrived.

    Deriving them from the payload would stamp `no_normalized_power` on a ride
    whose NP is sitting in the database from an earlier detailed fetch — the
    same mistake the null-preserving merge exists to prevent, one column over.
    """
    coach.import_activities([RIDE])
    summary = {k: v for k, v in RIDE.items() if k not in ("normPower", "avgPower")}
    result = coach.import_activities([summary])
    assert result["unchanged"] == 1
    stored = coach.list_activities()["activities"][0]
    assert stored["flags"] == []
    assert stored["normalized_power"] == 198.0


def test_a_flag_clears_when_the_detail_fetch_fills_the_gap():
    """The reverse: import the summary first, then the detailed ride."""
    coach.import_activities([{k: v for k, v in RIDE.items() if k != "normPower"}])
    assert "no_normalized_power" in coach.list_activities()["activities"][0]["flags"]
    coach.import_activities([RIDE])
    assert coach.list_activities()["activities"][0]["flags"] == []


# --------------------------------------------------------------------------
# review round 2 — several of these are regressions from the round 1 fixes
# --------------------------------------------------------------------------


def test_a_bare_race_result_on_a_settled_event_changes_nothing_and_says_so():
    """Reachable only after status stopped defaulting to "completed".

    With nothing to update, the UPDATE built as "SET , updated_at = ?" and
    SQLite refused it — which `_coach` then reported as database corruption.
    """
    event = coach.add_event("Club 100", "2026-07-04", priority="B")["stored"]
    coach.update_event(event["id"], status="abandoned")
    result = coach.record_race_result(event["id"])
    assert result["updated_fields"] == []
    assert result["stored"]["status"] == "abandoned"
    assert "stays abandoned" in result["status_unchanged"]


def test_a_thin_re_import_cannot_move_a_ride_to_the_utc_day():
    """A 22:00 UTC-5 ride is the next day in UTC.

    `local_date` was merged as an ordinary field, so a payload with no
    `startTimeLocal` brought a UTC-derived date that overrode the stored one —
    while `start_time_local` still said otherwise and no flag was raised,
    because the flag reads the start times. The week then showed a missed
    session and an unplanned ride on consecutive days.
    """
    evening = {
        "activityId": 900,
        "activityType": {"typeKey": "virtual_ride"},
        "startTimeLocal": "2026-07-07 22:00:00",
        "startTimeGMT": "2026-07-08 03:00:00",
        "duration": 3600.0,
        "normPower": 200.0,
    }
    coach.import_activities([evening])
    result = coach.import_activities([{k: v for k, v in evening.items() if k != "startTimeLocal"}])
    assert result["unchanged"] == 1, "nothing about the ride actually changed"
    stored = coach.list_activities()["activities"][0]
    assert stored["local_date"] == "2026-07-07"
    assert stored["start_time_local"] == "2026-07-07T22:00:00"
    assert stored["flags"] == []


def test_a_corrected_start_time_does_move_the_date():
    """The date is derived, so a genuinely corrected start time still moves it."""
    coach.import_activities([{**RIDE, "startTimeLocal": "2026-07-05 07:00:00"}])
    coach.import_activities([{**RIDE, "startTimeLocal": "2026-07-06 07:00:00"}])
    assert coach.list_activities()["activities"][0]["local_date"] == "2026-07-06"


def _hr_only_session(activity_id, laps, blocks, date="2026-07-01"):
    coach.log_ftp(value_watts=266, effective_date="2026-01-01")
    coach.import_activities(
        [
            {
                "activityId": activity_id,
                "activityType": {"typeKey": "road_biking"},
                "startTimeLocal": f"{date} 08:00:00",
                "duration": float(sum(lap["duration"] for lap in laps)),
                "averageHR": 150,
            }
        ]
    )
    coach.import_activity_laps({"lapDTOs": laps}, garmin_activity_id=str(activity_id))
    spec = {"name": "S", "ftp": 266, "blocks": blocks}
    planned_id = coach.save_planned_workouts([{"spec": spec, "scheduled_date": date}])[
        "planned_workouts"
    ][0]["id"]
    coach.link_activity(planned_id, garmin_activity_id=str(activity_id))
    return coach.compliance_report(planned_id)


INTERVALS = [{"type": "steady", "duration": 600, "power_pct": 90, "role": "interval"}] * 3


def test_one_clean_block_does_not_certify_the_untestable_ones():
    """A warmup with power in front of three no-power intervals used to return
    `as_prescribed`, asserting the intervals had been held."""
    report = _hr_only_session(
        6100,
        [{"duration": 600.0, "averagePower": 146.0}] + [{"duration": 600.0, "averageHR": 150}] * 3,
        [{"type": "steady", "duration": 600, "power_pct": 55, "role": "warmup"}, *INTERVALS],
    )
    assert report["unverifiable_blocks"] == 3
    assert report["verdict"] == "unverifiable"


def test_a_free_block_does_not_make_unverifiable_unreachable():
    """`no_target` used to count as evidence of compliance, so any plan with a
    free block could never report `unverifiable`."""
    report = _hr_only_session(
        6101,
        [{"duration": 600.0, "averageHR": 140}] + [{"duration": 600.0, "averageHR": 150}] * 3,
        [{"type": "free", "duration": 600, "role": "warmup"}, *INTERVALS],
        date="2026-07-02",
    )
    assert report["verdict"] == "unverifiable"


def test_a_no_power_block_cut_in_half_is_still_a_deviation():
    """Duration is verifiable without a power meter, and the promotion used to
    apply only over an already-clean verdict — so an HR-only ride abandoned
    block by block reported nothing wrong at all."""
    report = _hr_only_session(
        6102,
        [{"duration": 300.0, "averageHR": 150}] * 3,
        list(INTERVALS),
        date="2026-07-03",
    )
    assert report["off_target_blocks"] == 0, "no power was recorded, so none was wrong"
    assert report["off_duration_blocks"] == 3
    assert report["deviating_blocks"] == 3
    assert report["verdict"] == "deviated"


def test_linking_does_not_resurrect_a_skipped_session():
    """The coach withdrew it; a matching ride is evidence about the ride, not a
    reversal of that decision."""
    coach.import_activities([RIDE])
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    planned_id = saved["planned_workouts"][0]["id"]
    coach.update_planned_workout(planned_id, status="skipped")

    result = coach.link_activity(planned_id, garmin_activity_id="5001")
    assert result["linked"] is True
    assert result["planned_workout"]["status"] == "skipped"
    assert result["planned_workout"]["linked_activity_id"] is not None
    assert "stays skipped" in result["status_unchanged"]


def test_linking_a_planned_session_still_completes_it():
    coach.import_activities([RIDE])
    saved = coach.save_planned_workouts([{"spec": SPEC, "scheduled_date": "2026-07-05"}])
    result = coach.link_activity(saved["planned_workouts"][0]["id"], auto=True)
    assert result["planned_workout"]["status"] == "completed"
    assert "status_unchanged" not in result


def test_compute_load_reports_the_unknown_sport_rides_a_filter_hid():
    """The NULL-sport fix landed in two of the three queries."""
    coach.log_ftp(value_watts=266, effective_date="2026-01-01")
    coach.import_activities(
        [RIDE, {k: v for k, v in RIDE.items() if k != "activityType"} | {"activityId": 5100}]
    )
    filtered = coach.compute_load(sport="cycling")
    assert filtered["count"] == 1
    assert filtered["excluded_unknown_sport"] == 1
    assert "drop the sport filter" in filtered["excluded_unknown_sport_note"]


def test_a_planned_session_that_cannot_be_scored_is_counted_not_ignored():
    """One corrupt spec used to fold to zero, so the week read as over-performed."""
    import sqlite3

    coach.log_ftp(value_watts=266, effective_date="2026-01-01")
    saved = coach.save_planned_workouts(
        [
            {"spec": SPEC, "scheduled_date": "2026-07-05"},
            {"spec": SPEC, "scheduled_date": "2026-07-06"},
        ]
    )
    broken_id = saved["planned_workouts"][1]["id"]
    conn = sqlite3.connect(store.db_path())
    conn.execute(
        "UPDATE planned_workouts SET spec_json = ? WHERE id = ?", ('{"name": "corrupt"}', broken_id)
    )
    conn.commit()
    conn.close()

    totals = coach.get_week("2026-07-01", "2026-07-10", today="2026-07-20")["totals"]
    assert totals["planned_sessions"] == 2
    assert totals["planned_sessions_scored"] == 1
    assert totals["planned_sessions_unscored"] == 1
    assert str(broken_id) in totals["planned_unscored_warning"]


def test_a_backdated_hr_entry_keeps_both_notes():
    """The estimation note used to overwrite the backdating one, so zones came
    back with no statement that they are not the ones in force."""
    coach.log_hr(max_hr=190, effective_date="2026-08-01")
    result = coach.log_hr(max_hr=192, effective_date="2026-01-01")
    assert result["is_current"] is False
    assert "backdated" in result["note"]
    assert "92%" in result["estimation_note"]


def test_the_empty_selection_has_the_same_shape_as_a_real_one():
    coach.log_ftp(value_watts=266, effective_date="2026-01-01")
    coach.import_activities([RIDE])
    full = coach.compute_load()
    empty = coach.compute_load(activity_ids=[])
    assert set(full) - set(empty) == set(), (
        f"missing from the empty result: {set(full) - set(empty)}"
    )
    assert empty["scored"] == 0 and empty["total_tss"] == 0.0


def test_a_candidate_with_no_duration_does_not_rank_as_a_perfect_match():
    """`abs(None or 0)` sorted an unknown duration ahead of a near-exact one."""
    coach.import_activities(
        [
            {
                "activityId": 5200,
                "activityType": {"typeKey": "virtual_ride"},
                "startTimeLocal": "2026-07-05 08:00:00",
                "duration": 3700.0,
            },
            {
                "activityId": 5201,
                "activityType": {"typeKey": "virtual_ride"},
                "startTimeLocal": "2026-07-05 09:00:00",
            },
        ]
    )
    spec = {
        "name": "S",
        "ftp": 266,
        "blocks": [{"type": "steady", "duration": 3600, "power_pct": 65}],
    }
    saved = coach.save_planned_workouts([{"spec": spec, "scheduled_date": "2026-07-05"}])
    result = coach.link_activity(saved["planned_workouts"][0]["id"], auto=True)
    assert result["linked"] is False, "two candidates is ambiguous"
    assert [c["garmin_activity_id"] for c in result["candidates"]] == ["5200", "5201"]

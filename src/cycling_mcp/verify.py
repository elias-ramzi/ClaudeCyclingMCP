"""Compare an uploaded Garmin payload against what Garmin gives back.

`get_workout_by_id` returns a *curated* projection, not the payload that was
sent. It is lossy in ways that matter:

* Target type is reported by Garmin's canonical key for the numeric id, so the
  key sent is not necessarily the key returned.
* An absolute watt range and a %FTP range both come back as "power.zone" with a
  low/high pair, with nothing in the curated shape to tell them apart. In the
  *raw* API response they differ by `targetValueUnit`: null for watts, and
  {"unitId": 253, "unitKey": "percent"} for %FTP. The curated read drops that
  field, which is exactly why it cannot be trusted to confirm units.
* `estimated_duration_seconds` is computed by Garmin's own rules and disagrees
  with the sum of the steps, so it cannot be used to validate anything.

So a round-trip check must compare the fetched workout against *what was sent*,
mapped through those known transformations — never against an assumed read
shape, which would happily agree with a wrong-units payload.
"""

from __future__ import annotations

from typing import Any

# Garmin normalises a target's key from its numeric id, which it treats as
# authoritative. Observed against the live API on 2026-08-12.
TARGET_KEY_BY_ID: dict[int, str | None] = {
    1: None,  # no.target — the curated read omits the field entirely
    2: "power.zone",
    3: "cadence",
    4: "heart.rate.zone",
    5: "speed.zone",
    6: "pace.zone",
}


# The unit object Garmin attaches to a %FTP power target. Its absence is what
# makes targetValueOne/Two mean watts.
PERCENT_UNIT_KEY = "percent"


def _unit_key(unit: Any) -> str | None:
    return unit.get("unitKey") if isinstance(unit, dict) else None


def _expected_target_key(target: Any) -> str | None:
    if not isinstance(target, dict):
        return None
    target_id = target.get("workoutTargetTypeId")
    if target_id in TARGET_KEY_BY_ID:
        return TARGET_KEY_BY_ID[target_id]
    return target.get("workoutTargetTypeKey")


def _normalise_sent(steps: list[dict]) -> list[dict]:
    """Flatten a sent payload's steps into comparable records."""
    result: list[dict] = []
    for step in steps:
        if step.get("type") == "RepeatGroupDTO":
            result.append(
                {
                    "kind": "repeat",
                    "order": step.get("stepOrder"),
                    "repeat_count": step.get("numberOfIterations"),
                    "children": _normalise_sent(step.get("workoutSteps", [])),
                }
            )
            continue
        result.append(
            {
                "kind": "step",
                "order": step.get("stepOrder"),
                "type": (step.get("stepType") or {}).get("stepTypeKey"),
                "duration_s": _number(step.get("endConditionValue")),
                "target_type": _expected_target_key(step.get("targetType")),
                "target_low": _number(step.get("targetValueOne")),
                "target_high": _number(step.get("targetValueTwo")),
                "target_unit": _unit_key(step.get("targetValueUnit")),
                "secondary_target_type": _expected_target_key(step.get("secondaryTargetType")),
                "secondary_low": _number(step.get("secondaryTargetValueOne")),
                "secondary_high": _number(step.get("secondaryTargetValueTwo")),
                "description": step.get("description") or None,
            }
        )
    return result


def _normalise_fetched(steps: list[dict]) -> list[dict]:
    """Flatten a curated `get_workout_by_id` response into comparable records."""
    result: list[dict] = []
    for step in steps:
        if step.get("type") == "repeat":
            result.append(
                {
                    "kind": "repeat",
                    "order": step.get("order"),
                    "repeat_count": step.get("repeat_count"),
                    "children": _normalise_fetched(step.get("steps", [])),
                }
            )
            continue
        result.append(
            {
                "kind": "step",
                "order": step.get("order"),
                "type": step.get("type"),
                "duration_s": _number(step.get("end_condition_value")),
                "target_type": step.get("target_type"),
                "target_low": _number(step.get("target_value_low")),
                "target_high": _number(step.get("target_value_high")),
                # The curated read drops targetValueUnit entirely, so units
                # cannot be checked from this shape — see compare_upload_raw.
                "target_unit": None,
                "secondary_target_type": step.get("secondary_target_type"),
                "secondary_low": _number(step.get("secondary_target_value_low")),
                "secondary_high": _number(step.get("secondary_target_value_high")),
                "description": step.get("description") or None,
            }
        )
    return result


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


_COMPARED_FIELDS = (
    "type",
    "duration_s",
    "target_type",
    "target_low",
    "target_high",
    "secondary_target_type",
    "secondary_low",
    "secondary_high",
    "description",
)

# Only the raw API response carries targetValueUnit, so only a raw comparison
# can tell a watt target from a %FTP one.
_RAW_COMPARED_FIELDS = _COMPARED_FIELDS + ("target_unit",)


def _compare_steps(
    sent: list[dict],
    fetched: list[dict],
    path: str,
    problems: list[str],
    fields: tuple[str, ...] = _COMPARED_FIELDS,
) -> None:
    if len(sent) != len(fetched):
        problems.append(
            f"{path}: sent {len(sent)} steps, Garmin returned {len(fetched)}"
        )
        return

    for index, (want, got) in enumerate(zip(sent, fetched)):
        here = f"{path}[{index}]"
        if want["kind"] != got["kind"]:
            problems.append(f"{here}: sent a {want['kind']}, Garmin returned a {got['kind']}")
            continue

        if want["order"] != got["order"]:
            problems.append(
                f"{here}: stepOrder {want['order']} came back as {got['order']}"
            )

        if want["kind"] == "repeat":
            if want["repeat_count"] != got["repeat_count"]:
                problems.append(
                    f"{here}: repeat count {want['repeat_count']} came back as "
                    f"{got['repeat_count']} — check endCondition carries conditionTypeId 7"
                )
            _compare_steps(want["children"], got["children"], f"{here}.steps", problems, fields)
            continue

        for field in fields:
            if want[field] != got[field]:
                problems.append(f"{here}: {field} sent as {want[field]!r}, returned {got[field]!r}")


def compare_upload(payload: dict, fetched: dict) -> list[str]:
    """Return every mismatch between a sent payload and the fetched workout.

    An empty list means Garmin stored exactly what was sent, as far as the
    curated read can show. It cannot prove units (watts vs %FTP) — that needs a
    look at the workout in Garmin Connect.
    """
    problems: list[str] = []

    sent_name = payload.get("workoutName")
    got_name = fetched.get("name")
    if sent_name != got_name:
        problems.append(f"workout name sent as {sent_name!r}, returned {got_name!r}")

    sent_sport = (payload.get("sportType") or {}).get("sportTypeKey")
    got_sport = fetched.get("sport")
    if sent_sport != got_sport:
        problems.append(f"sport sent as {sent_sport!r}, returned {got_sport!r}")

    sent_segments = payload.get("workoutSegments") or []
    got_segments = fetched.get("segments") or []
    if len(sent_segments) != len(got_segments):
        problems.append(
            f"sent {len(sent_segments)} segments, Garmin returned {len(got_segments)}"
        )
        return problems

    for index, (sent_segment, got_segment) in enumerate(zip(sent_segments, got_segments)):
        _compare_steps(
            _normalise_sent(sent_segment.get("workoutSteps", [])),
            _normalise_fetched(got_segment.get("steps", [])),
            f"segment[{index}].steps",
            problems,
        )

    return problems


def compare_upload_raw(payload: dict, raw: dict) -> list[str]:
    """Compare a sent payload against Garmin's *raw* workout response.

    The raw response uses the same field names as the payload, and unlike the
    curated read it keeps `targetValueUnit` — so this comparison can prove that
    a power target was stored as watts rather than as %FTP. Use it when the raw
    API is reachable; `compare_upload` is the fallback for the curated shape.
    """
    problems: list[str] = []

    sent_name = payload.get("workoutName")
    got_name = raw.get("workoutName")
    if sent_name != got_name:
        problems.append(f"workout name sent as {sent_name!r}, returned {got_name!r}")

    sent_sport = (payload.get("sportType") or {}).get("sportTypeId")
    got_sport = (raw.get("sportType") or {}).get("sportTypeId")
    if sent_sport != got_sport:
        problems.append(f"sportTypeId sent as {sent_sport!r}, returned {got_sport!r}")

    sent_segments = payload.get("workoutSegments") or []
    got_segments = raw.get("workoutSegments") or []
    if len(sent_segments) != len(got_segments):
        problems.append(f"sent {len(sent_segments)} segments, Garmin returned {len(got_segments)}")
        return problems

    for index, (sent_segment, got_segment) in enumerate(zip(sent_segments, got_segments)):
        _compare_steps(
            _normalise_sent(sent_segment.get("workoutSteps", [])),
            _normalise_sent(got_segment.get("workoutSteps", [])),
            f"segment[{index}].steps",
            problems,
            _RAW_COMPARED_FIELDS,
        )

    return problems


def percent_targets(raw: dict) -> list[int]:
    """Step orders whose power target Garmin stored as %FTP rather than watts."""
    found: list[int] = []

    def walk(steps: list[dict]) -> None:
        for step in steps:
            if _unit_key(step.get("targetValueUnit")) == PERCENT_UNIT_KEY:
                found.append(step.get("stepOrder"))
            walk(step.get("workoutSteps", []))

    for segment in raw.get("workoutSegments", []):
        walk(segment.get("workoutSteps", []))
    return found


def total_step_seconds(payload: dict) -> float:
    """Sum every step's duration, expanding repeats.

    Garmin's own `estimated_duration_seconds` follows different rules and
    disagrees with this figure, so compare structure, not that field.
    """

    def walk(steps: list[dict]) -> float:
        total = 0.0
        for step in steps:
            if step.get("type") == "RepeatGroupDTO":
                iterations = step.get("numberOfIterations") or 1
                total += iterations * walk(step.get("workoutSteps", []))
            else:
                total += float(step.get("endConditionValue") or 0)
        return total

    return sum(walk(s.get("workoutSteps", [])) for s in payload.get("workoutSegments", []))

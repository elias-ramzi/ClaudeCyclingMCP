"""Check what a platform stored against what was sent to it.

Two halves, one per platform. The Garmin half compares an uploaded payload
against what Garmin gives back; the MyWhoosh half, at the bottom of this file,
compares a scraped builder header against the session that was rendered.
Neither makes a network call — both are given the two sides to compare.


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

import hashlib
import json
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
_RAW_COMPARED_FIELDS = (*_COMPARED_FIELDS, "target_unit")


def _compare_steps(
    sent: list[dict],
    fetched: list[dict],
    path: str,
    problems: list[str],
    fields: tuple[str, ...] = _COMPARED_FIELDS,
) -> None:
    if len(sent) != len(fetched):
        problems.append(f"{path}: sent {len(sent)} steps, Garmin returned {len(fetched)}")
        return

    for index, (want, got) in enumerate(zip(sent, fetched, strict=True)):
        here = f"{path}[{index}]"
        if want["kind"] != got["kind"]:
            problems.append(f"{here}: sent a {want['kind']}, Garmin returned a {got['kind']}")
            continue

        if want["order"] != got["order"]:
            problems.append(f"{here}: stepOrder {want['order']} came back as {got['order']}")

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


# The two shapes this module compares. A payload uses Garmin's DTO field names;
# the curated read renames nearly all of them. Swapping the two arguments is
# therefore not a near-miss — every lookup misses, every value reads as None,
# and the comparator reports a workout that Garmin supposedly emptied.
PAYLOAD_MARKERS = ("workoutSegments", "workoutName", "sportType")
FETCHED_MARKERS = ("segments", "name", "sport", "workout_id", "workoutId")


def _shape_of(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    if any(key in obj for key in PAYLOAD_MARKERS):
        return "payload"
    if any(key in obj for key in FETCHED_MARKERS):
        return "fetched"
    return None


def shape_problem(payload: dict, fetched: dict) -> str | None:
    """Reject a misused comparison instead of diffing two different shapes.

    A comparator that cannot tell it is being misused should not be trusted to
    say when Garmin misbehaves. Handed a payload in the `fetched` slot this
    used to report "workout name returned None / Garmin returned 0 segments" —
    confident, specific, and entirely an artefact of reading DTO keys with
    curated-read names. The skill's instruction on a mismatch is to delete the
    workout, so failing open here destroys correct work. Observed 2026-08-19.
    """
    if _shape_of(payload) == "fetched" and _shape_of(fetched) == "payload":
        return (
            "The two arguments are the wrong way round: 'payload' holds a "
            "get_workout_by_id response and 'fetched' holds an upload payload. Swap them."
        )
    if _shape_of(fetched) == "payload":
        return (
            "'fetched' looks like an upload payload, not a get_workout_by_id response "
            f"(found {sorted(k for k in PAYLOAD_MARKERS if k in fetched)}). Pass the "
            "workout Garmin returned in this slot. To check a payload before uploading, "
            "use check_garmin_payload instead."
        )
    if _shape_of(payload) == "fetched":
        return (
            "'payload' looks like a get_workout_by_id response, not the payload that was "
            f"sent (found {sorted(k for k in FETCHED_MARKERS if k in payload)}). The two "
            "arguments are the wrong way round."
        )
    if _shape_of(payload) is None:
        return "'payload' has none of the fields an upload payload carries; it is not a payload."
    if _shape_of(fetched) is None:
        return (
            "'fetched' has none of the fields a fetched workout carries; it is not a "
            "get_workout_by_id response."
        )
    return None


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
        problems.append(f"sent {len(sent_segments)} segments, Garmin returned {len(got_segments)}")
        return problems

    for index, (sent_segment, got_segment) in enumerate(
        zip(sent_segments, got_segments, strict=True)
    ):
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

    for index, (sent_segment, got_segment) in enumerate(
        zip(sent_segments, got_segments, strict=True)
    ):
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


# --- MyWhoosh -----------------------------------------------------------
#
# MyWhoosh has no API to read a workout back, so the only evidence that an
# import took is what its builder header shows: `Workout Time` and
# `Training Load`. That header is the single check standing between a good
# workout and spending an irreversible slot credit on a broken one, which is
# too much to hang on an agent eyeballing two numbers.


# `Training Load` is MyWhoosh's own model, not ours. Observed agreeing with our
# TSS to the digit on 2026-08-19 (68:00 session, TSS 71, TL 71) — one data
# point, so a disagreement here is reported as a warning, not a failure. The
# duration is the hard check: it comes from the file, not from a model.
TRAINING_LOAD_TOLERANCE_PCT = 10.0
TRAINING_LOAD_TOLERANCE_ABS = 3.0


def _parse_clock(value: Any) -> float | None:
    """Seconds from a header duration, or None if it isn't one.

    Accepts what the header actually renders: "68:00", "1:08:00", and the
    failure values "00:00" and "NaN" (returned as 0.0 and None respectively).
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() == "nan":
        return None
    parts = text.split(":")
    if not all(p.strip().isdigit() for p in parts) or len(parts) > 3:
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def compare_mywhoosh_import(
    expected_seconds: float,
    expected_tss: float,
    workout_time: Any,
    training_load: Any = None,
    before: dict | None = None,
) -> dict:
    """Check a MyWhoosh builder header against the session that was rendered.

    `before` is the pre-import header snapshot (`workout_time`,
    `training_load`, optionally `name`). It is what distinguishes a successful
    import from a silent no-op: "Create New" routinely opens an editor that
    already holds a previous workout, so a populated header proves nothing on
    its own — and if that workout happens to share this session's duration,
    neither does a duration that matches.
    """
    checks: list[dict] = []
    problems: list[str] = []
    warnings: list[str] = []

    observed_seconds = _parse_clock(workout_time)

    if observed_seconds is None:
        problems.append(
            f"Workout Time reads {workout_time!r}, which is not a duration. "
            "NaN means the import failed silently; do not export."
        )
    elif observed_seconds == 0:
        problems.append("Workout Time is 00:00 — the import did not take. Do not export.")
    elif abs(observed_seconds - expected_seconds) >= 1:
        problems.append(
            f"Workout Time is {workout_time} ({observed_seconds:.0f} s) but the "
            f"rendered session is {expected_seconds:.0f} s. Something was dropped "
            "on import; do not export."
        )
    checks.append(
        {
            "check": "workout_time",
            "expected": expected_seconds,
            "observed": observed_seconds,
            "ok": observed_seconds is not None and abs(observed_seconds - expected_seconds) < 1,
        }
    )

    if training_load is not None:
        try:
            observed_tl = float(training_load)
        except (TypeError, ValueError):
            observed_tl = None
        tolerance = max(
            TRAINING_LOAD_TOLERANCE_ABS, expected_tss * TRAINING_LOAD_TOLERANCE_PCT / 100
        )
        tl_ok = observed_tl is not None and abs(observed_tl - expected_tss) <= tolerance
        if not tl_ok:
            warnings.append(
                f"Training Load is {training_load} but our TSS is {expected_tss:.1f}. "
                "These are different models so they need not agree exactly, but a gap "
                "this wide usually means the builder's FTP is not the FTP the file was "
                "rendered against (Step 5)."
            )
        checks.append(
            {
                "check": "training_load",
                "expected": round(expected_tss, 1),
                "observed": observed_tl,
                "tolerance": round(tolerance, 1),
                "ok": tl_ok,
            }
        )

    if before is None:
        warnings.append(
            "No pre-import snapshot given, so a silent no-op cannot be ruled out: a "
            "header matching the session could be a workout that was already loaded. "
            "Capture Workout Time and Training Load before importing (Step 2)."
        )
    else:
        before_seconds = _parse_clock(before.get("workout_time"))
        same_time = before_seconds is not None and before_seconds == observed_seconds
        same_load = str(before.get("training_load")) == str(training_load)
        unchanged = same_time and same_load
        if unchanged:
            problems.append(
                "The header is identical to the pre-import snapshot "
                f"({before.get('workout_time')} / {before.get('training_load')}), so the "
                "import did nothing — regardless of whether those numbers match the "
                "session. Do not export."
            )
        checks.append(
            {"check": "changed_from_snapshot", "observed": not unchanged, "ok": not unchanged}
        )

    return {
        "ok": not problems,
        "safe_to_export": not problems,
        "checks": checks,
        "problems": problems,
        "warnings": warnings,
    }


# --- Transcription ------------------------------------------------------
#
# The payload leaves this server as text in a model's context and re-enters the
# world as an argument the model composes for a different MCP's upload tool.
# There is no mechanism by which a model can pass it through "unchanged" — it
# retypes ~90 lines of nested JSON. A single wrong digit in targetValueOne
# yields a workout that uploads cleanly, verifies as sent-equals-stored, and is
# wrong, because the round-trip check compares Garmin against what was *sent*,
# not against what the renderer produced.
#
# So the renderer issues a digest of what it produced, and this is where a
# retyped payload gets checked against it. Reported after a real upload on
# 2026-08-19 where the transcription happened to be faithful.

DIGEST_LENGTH = 16


def _canonical(value: Any) -> Any:
    """Strip everything that is representation rather than content.

    JSON has one number type. Python does not, so a renderer emitting 227.0
    and a model retyping it as 227 produce equal payloads that hash
    differently — and the retyping is unavoidable, because the payload leaves
    this server as text and re-enters as an argument someone composed.

    Getting this wrong is worse than not checking. A digest that fires on a
    clean payload teaches its reader to override it, which is precisely when
    the real corruption gets through. `diff_payloads` already compares numbers
    by value; this is the same rule, applied before hashing, so the two cannot
    disagree about what "the same payload" means. Observed disagreeing on a
    real session, 2026-08-20.
    """
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def payload_digest(payload: dict) -> str:
    """A short, stable digest of a payload's content.

    Canonicalised first — sorted keys, no insignificant whitespace, and
    integral floats folded to ints — so that only content changes the digest.
    """
    canonical = json.dumps(
        _canonical(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]


# Garmin's step type ids as the UI labels them. Note that a French UI renders
# both `cooldown` and `recovery` as "Récupération", so a cooldown looks like a
# fourth recovery block. Cosmetic — step type drives nothing at execution.
STEP_LABELS = {1: "Warm Up", 2: "Cool Down", 3: "Interval", 4: "Recovery", 5: "Rest"}


def ui_checklist(payload: dict) -> list[str]:
    """What the Garmin UI should show, step by step, for eyeballing by hand.

    The fallback when `get_workout_by_id` is unavailable: verification by a
    human reading the screen. Generating the list here rather than leaving a
    model to improvise it makes that check the same every time.
    """
    lines: list[str] = []

    def describe(step: dict, prefix: str = "") -> None:
        if step.get("type") == "RepeatGroupDTO":
            lines.append(f"{prefix}Repeat x{step.get('numberOfIterations')}:")
            for child in step.get("workoutSteps", []):
                describe(child, prefix + "  ")
            return
        label = STEP_LABELS.get(step.get("stepType", {}).get("stepTypeId"), "Step")
        seconds = int(float(step.get("endConditionValue") or 0))
        clock = f"{seconds // 60}:{seconds % 60:02d}"
        target = step.get("targetType", {}).get("workoutTargetTypeId")
        if target == 2:
            low, high = step.get("targetValueOne"), step.get("targetValueTwo")
            shown = f"{int(low)}-{int(high)} W"
        elif target == 1:
            shown = "no target"
        else:
            shown = f"target type {target}"
        lines.append(f"{prefix}{label} · {clock} · {shown}")

    for segment in payload.get("workoutSegments", []):
        for step in segment.get("workoutSteps", []):
            describe(step)

    lines.append(
        "A French UI labels a cooldown 'Récupération', the same as a recovery — "
        "that is a translation, not a wrong step type."
    )
    return lines


def diff_payloads(expected: dict, actual: dict, path: str = "") -> list[str]:
    """Field-by-field differences between two upload payloads.

    The digest says *that* a payload was altered; this says where. Given the
    spec, the expected payload can be regenerated here, so the check needs no
    state carried between tool calls — which matters when the caller composing
    the payload is a language model rather than a program.
    """
    problems: list[str] = []

    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            where = f"{path}.{key}" if path else key
            if key not in actual:
                problems.append(f"{where}: missing (expected {expected[key]!r})")
            elif key not in expected:
                problems.append(f"{where}: unexpected extra field {actual[key]!r}")
            else:
                problems.extend(diff_payloads(expected[key], actual[key], where))
        return problems

    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            problems.append(f"{path}: expected {len(expected)} items, got {len(actual)}")
            return problems
        for index, (want, got) in enumerate(zip(expected, actual, strict=True)):
            problems.extend(diff_payloads(want, got, f"{path}[{index}]"))
        return problems

    # Numbers compare by value: 245 and 245.0 are the same target, and a model
    # retyping a payload will not preserve int-vs-float.
    numeric = (int, float)
    if (
        isinstance(expected, numeric)
        and isinstance(actual, numeric)
        and not isinstance(expected, bool)
        and not isinstance(actual, bool)
    ):
        if float(expected) != float(actual):
            problems.append(f"{path}: expected {expected!r}, got {actual!r}")
        return problems

    if expected != actual:
        problems.append(f"{path}: expected {expected!r}, got {actual!r}")
    return problems

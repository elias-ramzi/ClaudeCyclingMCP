"""Render a resolved workout to a Garmin Connect `upload_workout` payload.

Schema notes, all of them load-bearing (see README "Garmin schema provenance"):

* Steps use Garmin's DTO format: `ExecutableStepDTO` for ordinary steps,
  `RepeatGroupDTO` for repeated sets.
* A `RepeatGroupDTO` must carry a complete `endCondition`, including the numeric
  `conditionTypeId: 7`. Omitting the id makes the API silently corrupt the
  repeat count — no error, wrong workout.
* Power targets use workoutTargetTypeId 2 (key "power.zone") with an absolute
  watt range in targetValueOne/targetValueTwo, and no zoneNumber.

  This contradicts the Garmin MCP's own `upload_workout` docstring, which says
  cycling watt ranges take workoutTargetTypeId 6 / "power.between". Against the
  live API that is wrong, and wrong silently: id 6 uploads without error and
  Garmin normalises it to the key "pace.zone" on a cycling workout — a pace
  target, not a power one. Id 2 round-trips as "power.zone" with the watts
  intact, byte-for-byte the shape Garmin's own web UI produces for a watt
  target. Verified by upload/fetch probe, 2026-08-12; see README.
* Percentages from the spec are resolved to watts here using the spec's FTP.
  Garmin's UI also accepts a %FTP target, which the API stores in this same
  shape — the curated read cannot tell the two apart (README, "the conflation").
  Watts are unambiguous, so watts are what this renderer emits.
* Target values live on the step, next to `targetType`, never inside it.
"""

from __future__ import annotations

from .spec import (
    DEFAULT_TARGET_BAND_PCT,
    RECOVERY_TARGET_BAND_PCT,
    Block,
    Repeat,
    Workout,
)

SPORT_CYCLING = {"sportTypeId": 2, "sportTypeKey": "cycling"}

STEP_TYPE_IDS = {
    "warmup": 1,
    "cooldown": 2,
    "interval": 3,
    "recovery": 4,
    "rest": 5,
    "repeat": 6,
}

END_CONDITION_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time"}
END_CONDITION_ITERATIONS = {"conditionTypeId": 7, "conditionTypeKey": "iterations"}

TARGET_NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
# Absolute watt range. Id 2 despite the "zone" in the key — see the module
# docstring; id 6 silently becomes a pace target on a cycling workout.
TARGET_POWER = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone"}
TARGET_CADENCE = {"workoutTargetTypeId": 3, "workoutTargetTypeKey": "cadence"}

# How wide a single-step ramp has to be before flattening it is worth a warning.
# A few watts either side of a target reads as a band anyway; 20 W is where the
# athlete would notice they were told to hold rather than climb.
RAMP_LOSSY_WATTS = 20

# The easy end of a session gets a wider band than the hard end. See the note
# on RECOVERY_TARGET_BAND_PCT in spec.py for why one width does not fit both.
ROLE_BAND_PCT = {
    "interval": DEFAULT_TARGET_BAND_PCT,
    "rest": DEFAULT_TARGET_BAND_PCT,
    "recovery": RECOVERY_TARGET_BAND_PCT,
    "warmup": RECOVERY_TARGET_BAND_PCT,
    "cooldown": RECOVERY_TARGET_BAND_PCT,
}


def _band_pct(block: Block, workout: Workout) -> float:
    """The band width for this block: the spec's override, or the role default."""
    if workout.target_band_pct is not None:
        return workout.target_band_pct
    return ROLE_BAND_PCT.get(block.role, DEFAULT_TARGET_BAND_PCT)


class _Counter:
    """Garmin numbers steps globally, continuing through nested repeat groups."""

    def __init__(self) -> None:
        self.value = 0

    def next(self) -> int:
        self.value += 1
        return self.value


def _step_type(role: str) -> dict:
    return {"stepTypeId": STEP_TYPE_IDS[role], "stepTypeKey": role}


def _description(block: Block) -> str | None:
    """Message and HR note, carried as step notes rather than as targets."""
    parts = [part for part in (block.message, block.hr_note) if part]
    return " | ".join(parts) if parts else None


def _watt_bounds(
    block: Block, workout: Workout, warnings: list[str], where: str
) -> tuple[int, int]:
    """The low/high watt pair for a steady block.

    A single number gets a band around it, because Garmin power targets are
    ranges. An explicit [low, high] in the spec is used as written.
    """
    low_fraction, high_fraction = block.p_low, block.p_high
    assert low_fraction is not None and high_fraction is not None

    band_pct = _band_pct(block, workout)
    if block.is_scalar_target:
        centre = low_fraction * workout.ftp
        margin = centre * band_pct / 100.0
        low, high = round(centre - margin), round(centre + margin)
        if low == high and band_pct > 0:
            low, high = low - 1, high + 1
    else:
        low, high = workout.watts(low_fraction), workout.watts(high_fraction)

    if low == high:
        warnings.append(
            f"{where}: Garmin power target is a single value ({low} W) because "
            "garmin_target_band_pct is 0; most head units expect a range"
        )
    return low, high


def target_band_watts(block: Block, workout: Workout) -> tuple[int, int] | None:
    """The watt band this block will carry on Garmin, or None if it has none.

    Exposed so `describe_spec` can show the band that actually ships. The
    block table is where the skills say a wrong number is cheap to catch, and
    band width is both invisible there and consequential on a head unit —
    a tight corridor at VO2 power alarms continuously outdoors.
    """
    if block.kind != "steady" or not block.is_scalar_target:
        return None
    return _watt_bounds(block, workout, [], "")


def _apply_cadence(step: dict, block: Block) -> None:
    if block.cadence_low is None:
        return
    step["secondaryTargetType"] = dict(TARGET_CADENCE)
    step["secondaryTargetValueOne"] = float(block.cadence_low)
    step["secondaryTargetValueTwo"] = float(block.cadence_high)  # type: ignore[arg-type]


def _executable(
    block: Block,
    order: int,
    duration_s: int,
    target: dict,
    low: float | None,
    high: float | None,
) -> dict:
    step: dict = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": _step_type(block.role),
        "endCondition": dict(END_CONDITION_TIME),
        "endConditionValue": float(duration_s),
        "targetType": dict(target),
    }
    description = _description(block)
    if description:
        step["description"] = description
    if low is not None and high is not None:
        step["targetValueOne"] = float(low)
        step["targetValueTwo"] = float(high)
    _apply_cadence(step, block)
    return step


def _ramp_segments(block: Block, count: int) -> list[tuple[int, float, float]]:
    """Split a ramp into `count` segments of (seconds, start_fraction, end_fraction)."""
    total = block.duration_s
    start, end = block.p_from, block.p_to
    assert start is not None and end is not None

    base, remainder = divmod(total, count)
    segments: list[tuple[int, float, float]] = []
    elapsed = 0
    for index in range(count):
        seconds = base + (1 if index < remainder else 0)
        segment_start = start + (end - start) * (elapsed / total)
        elapsed += seconds
        segment_end = start + (end - start) * (elapsed / total)
        segments.append((seconds, segment_start, segment_end))
    return segments


def _render_block(
    block: Block, workout: Workout, counter: _Counter, warnings: list[str], where: str
) -> list[dict]:
    if block.kind == "free":
        return [_executable(block, counter.next(), block.duration_s, TARGET_NO_TARGET, None, None)]

    if block.kind == "ramp":
        # Garmin has no ramp primitive. One step carrying the whole from->to
        # span is the closest honest equivalent; ramp_steps stair-steps it when
        # a finer approximation is wanted.
        segments = _ramp_segments(block, block.ramp_steps)
        steps = []
        for seconds, start_fraction, end_fraction in segments:
            low_w = workout.watts(min(start_fraction, end_fraction))
            high_w = workout.watts(max(start_fraction, end_fraction))
            if low_w == high_w:
                high_w = low_w + 1
            steps.append(_executable(block, counter.next(), seconds, TARGET_POWER, low_w, high_w))

        # Say out loud that this rendering is lossy, in both the ways it is.
        # A single step spanning the whole ramp displays on Garmin as a static
        # band to hold, not a sweep; and because Garmin ranges are low-first,
        # a descending ramp is reordered, so its direction survives only in the
        # spec. Nothing downstream can recover either fact — a backwards
        # cooldown and a correct one produce identical payloads, so the
        # round-trip check passes on both. Observed 2026-08-19.
        if block.ramp_steps == 1:
            low_w = workout.watts(min(block.p_from, block.p_to))
            high_w = workout.watts(max(block.p_from, block.p_to))
            if high_w - low_w >= RAMP_LOSSY_WATTS:
                direction = "down" if block.p_to < block.p_from else "up"
                from_w = workout.watts(block.p_from)
                to_w = workout.watts(block.p_to)
                warnings.append(
                    f"{where}: this {from_w}->{to_w} W ramp renders on Garmin as one "
                    f"{low_w}-{high_w} W step — a band to hold, not a sweep. Garmin has no ramp "
                    f"primitive and its ranges are low-first, so the fact that it goes "
                    f"{direction} is not in the payload at all and no round-trip check can "
                    f"catch it being wrong. Set ramp_steps > 1 to stair-step it. The .zwo "
                    f"keeps the real ramp."
                )
        return steps

    low, high = _watt_bounds(block, workout, warnings, where)
    return [_executable(block, counter.next(), block.duration_s, TARGET_POWER, low, high)]


def render_garmin(workout: Workout) -> tuple[dict, list[str]]:
    """Return the `upload_workout` payload and any warnings raised."""
    warnings: list[str] = []
    counter = _Counter()
    steps: list[dict] = []

    for index, node in enumerate(workout.nodes, start=1):
        where = f"blocks[{index}]"
        if isinstance(node, Repeat):
            group: dict = {
                "type": "RepeatGroupDTO",
                "stepOrder": counter.next(),
                "stepType": _step_type("repeat"),
                "numberOfIterations": node.count,
                "endCondition": dict(END_CONDITION_ITERATIONS),
                "endConditionValue": float(node.count),
                "smartRepeat": False,
                "workoutSteps": [],
            }
            for child_index, child in enumerate(node.blocks, start=1):
                group["workoutSteps"].extend(
                    _render_block(
                        child, workout, counter, warnings, f"{where}.blocks[{child_index}]"
                    )
                )
            steps.append(group)
        else:
            steps.extend(_render_block(node, workout, counter, warnings, where))

    payload: dict = {
        "workoutName": workout.name,
        "sportType": dict(SPORT_CYCLING),
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": dict(SPORT_CYCLING),
                "workoutSteps": steps,
            }
        ],
    }
    if workout.description:
        payload["description"] = workout.description
    return payload, warnings

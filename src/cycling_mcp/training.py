"""Zones, training load, form, and plan-versus-actual comparison.

Pure functions over numbers that came out of the store. This is where the
coaching layer earns its keep: a model can describe a week convincingly, but it
cannot reliably do 40 days of exponential smoothing in its head, and the places
where it goes wrong are invisible in the answer. So the arithmetic happens here,
deterministically, and the model reads the result.

Every formula below is written out in the docstring of the function that
implements it, including what it cannot see. A training-load number carries no
units and no error bars; the only defence against reading too much into one is
knowing how it was made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .spec import format_duration

# The classic Coggan boundaries, as fractions of FTP. Each entry is
# (key, label, low, high); the top zone has no upper bound.
POWER_ZONES: tuple[tuple[str, str, float, float | None], ...] = (
    ("z1", "Active recovery", 0.0, 0.55),
    ("z2", "Endurance", 0.55, 0.75),
    ("z3", "Tempo", 0.75, 0.90),
    ("z4", "Threshold", 0.90, 1.05),
    ("z5", "VO2max", 1.05, 1.20),
    ("z6", "Anaerobic", 1.20, None),
)

# Sweet spot straddles the z3/z4 boundary, which is exactly why it is quoted
# separately rather than as a zone: it is the band where a session gets most of
# the threshold adaptation for noticeably less of the cost.
SWEET_SPOT = (0.88, 0.94)

# Heart-rate zones as fractions of threshold HR (LTHR), the Friel table. They
# are not the power zones with a different unit: HR lags, so the boundaries do
# not line up effort for effort, and a z4 power interval starts in HR z3.
HR_ZONES: tuple[tuple[str, str, float, float | None], ...] = (
    ("z1", "Recovery", 0.0, 0.81),
    ("z2", "Aerobic", 0.81, 0.89),
    ("z3", "Tempo", 0.89, 0.94),
    ("z4", "Threshold", 0.94, 1.00),
    ("z5a", "Just over threshold", 1.00, 1.03),
    ("z5b", "VO2max", 1.03, 1.06),
    ("z5c", "Anaerobic", 1.06, None),
)

# When only a maximum HR is known, threshold is estimated at this fraction of
# it. A wide-open approximation: measured LTHR routinely lands 5 bpm either
# side, and every HR zone shifts with it. Flagged wherever it is used.
LTHR_FROM_MAX_HR = 0.92

# The 20-minute test convention: FTP is 95% of the best 20-minute average.
TWENTY_MINUTE_FACTOR = 0.95

CTL_TIME_CONSTANT_DAYS = 42
ATL_TIME_CONSTANT_DAYS = 7

# How far a lap's average power may sit from its target before compliance calls
# it a miss. 5% of target, not of FTP: the same window on a 300 W interval and
# on a 150 W recovery would be meaningless at one end or the other.
COMPLIANCE_TOLERANCE_PCT = 5.0

# Roles where the power target is a ceiling rather than a figure to hold. Going
# under one of these is the session working as intended.
EASY_ROLES = ("recovery", "warmup", "cooldown")

# The power-verdict vocabulary, split into the three things a block can be
# evidence of. `no_power` is deliberately not compliant: the lap recorded no
# watts, so the target was neither hit nor missed, and calling that compliance
# asserts a session was verified when nothing about it was.
COMPLIANT_VERDICTS = ("on_target", "easier_than_target")
DEVIATING_VERDICTS = ("under", "over")
UNVERIFIABLE_VERDICTS = ("no_power",)
#: A block that asked for no power target. Neither evidence of compliance nor
#: of a problem on the power axis — it is simply outside the power question, so
#: what is known about such a block comes from its duration instead.
NO_TARGET_VERDICTS = ("no_target",)

# The duration-verdict vocabulary, the same three ways. `unknown` is
# unverifiable rather than fine: `duration_s` is nullable, so a lap from a thin
# payload carries no time and how long the block was ridden for is not known.
COMPLIANT_DURATION_VERDICTS = ("on_time",)
DEVIATING_DURATION_VERDICTS = ("short", "long")
UNVERIFIABLE_DURATION_VERDICTS = ("unknown",)

#: Everything `compare_block` can put on each axis. `classify_block` refuses
#: anything outside these rather than letting a verdict added later fall
#: through to "nothing wrong here".
KNOWN_VERDICTS = (
    COMPLIANT_VERDICTS + DEVIATING_VERDICTS + UNVERIFIABLE_VERDICTS + NO_TARGET_VERDICTS
)
KNOWN_DURATION_VERDICTS = (
    COMPLIANT_DURATION_VERDICTS + DEVIATING_DURATION_VERDICTS + UNVERIFIABLE_DURATION_VERDICTS
)
#: What a block can be evidence of, once both axes are read together.
BLOCK_CLASSES = ("compliant", "deviating", "unverifiable")

_ORDINALS = (
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
)


def ordinal(index: int) -> str:
    """ "second" for 2, "block 14" past the point where words stop helping."""
    if 1 <= index <= len(_ORDINALS):
        return _ORDINALS[index - 1]
    return f"number {index}"


def parse_date(value: str, what: str = "date") -> date:
    """A YYYY-MM-DD string as a date, or a ValueError that says which field."""
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be YYYY-MM-DD, got {value!r}") from None


def days_between(start: date, end: date) -> list[date]:
    """Every date from `start` to `end` inclusive."""
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


# --------------------------------------------------------------------------
# zones
# --------------------------------------------------------------------------


def power_zones(ftp: int) -> list[dict]:
    """The six power zones in watts for an FTP, plus sweet spot.

    Boundaries are inclusive-low, exclusive-high, and rounded to the watt the
    athlete will actually read on a head unit.
    """
    zones = []
    for key, label, low, high in POWER_ZONES:
        zone = {
            "zone": key,
            "label": label,
            "low_pct": round(low * 100),
            "high_pct": round(high * 100) if high is not None else None,
            "low_w": round(low * ftp),
            "high_w": round(high * ftp) if high is not None else None,
        }
        zone["range_w"] = (
            f"{zone['low_w']}-{zone['high_w']} W" if high is not None else f"{zone['low_w']}+ W"
        )
        zones.append(zone)
    zones.append(
        {
            "zone": "sweet_spot",
            "label": "Sweet spot",
            "low_pct": round(SWEET_SPOT[0] * 100),
            "high_pct": round(SWEET_SPOT[1] * 100),
            "low_w": round(SWEET_SPOT[0] * ftp),
            "high_w": round(SWEET_SPOT[1] * ftp),
            "range_w": f"{round(SWEET_SPOT[0] * ftp)}-{round(SWEET_SPOT[1] * ftp)} W",
            "note": "Straddles the tempo/threshold boundary; not one of the six zones.",
        }
    )
    return zones


def hr_zones(threshold_hr: int) -> list[dict]:
    """The Friel HR zones in bpm for a threshold heart rate."""
    zones = []
    for key, label, low, high in HR_ZONES:
        low_bpm = round(low * threshold_hr)
        high_bpm = round(high * threshold_hr) if high is not None else None
        zones.append(
            {
                "zone": key,
                "label": label,
                "low_pct_lthr": round(low * 100),
                "high_pct_lthr": round(high * 100) if high is not None else None,
                "low_bpm": low_bpm,
                "high_bpm": high_bpm,
                "range_bpm": f"{low_bpm}-{high_bpm} bpm" if high_bpm else f"{low_bpm}+ bpm",
            }
        )
    return zones


# --------------------------------------------------------------------------
# training load
# --------------------------------------------------------------------------


@dataclass
class Load:
    """One activity's training load, and how honestly it was arrived at."""

    tss: float | None
    method: str  # "power" | "power_avg" | "hr" | "none"
    intensity_factor: float | None = None
    ftp_used: int | None = None
    threshold_hr_used: int | None = None
    reason: str | None = None
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        result: dict = {
            "tss": round(self.tss, 1) if self.tss is not None else None,
            "method": self.method,
        }
        if self.intensity_factor is not None:
            result["intensity_factor"] = round(self.intensity_factor, 3)
        if self.ftp_used is not None:
            result["ftp_used_w"] = self.ftp_used
        if self.threshold_hr_used is not None:
            result["threshold_hr_used_bpm"] = self.threshold_hr_used
        if self.reason:
            result["reason"] = self.reason
        if self.flags:
            result["flags"] = self.flags
        return result


def training_stress_score(duration_s: float, intensity_factor: float) -> float:
    """TSS = duration_h x IF^2 x 100.

    The single definition in this package. `metrics.py` scores a *plan* and this
    module scores a *ride*, and the whole point of `compliance_report` is to set
    those two numbers side by side — so they must come from one function. Two
    copies that drift by a rounding choice turn every plan-versus-actual
    comparison into a report on the arithmetic.

    One hour at threshold is 100 by construction.
    """
    return (duration_s / 3600.0) * intensity_factor**2 * 100.0


def power_tss(duration_s: float, normalized_power: float, ftp: int) -> float:
    """TSS from power: IF is NP / FTP.

    This is the number every other load figure here is calibrated against.
    """
    return training_stress_score(duration_s, normalized_power / ftp)


def hr_tss(duration_s: float, avg_hr: float, threshold_hr: int) -> float:
    """hrTSS = (duration_h) x (avg HR / threshold HR)^2 x 100.

    The power formula with a heart-rate ratio substituted for the power one. It
    is the best available when a ride has no power, and it is **not the same
    quantity** — do not compare one against a power TSS or read a difference
    between them as a change in training:

    * It cannot see variability. A criterium of thirty sprints and a steady
      tempo ride with the same average HR score identically, and the power TSS
      of the first is far higher.
    * Cardiac drift inflates long rides. Heart rate climbs at constant power in
      heat and over hours, so a five-hour endurance ride scores high for work
      that was easy.
    * It under-scores short, sharp sessions, where heart rate never catches up
      with the effort before the interval ends.
    * It rests entirely on the threshold HR. That figure is often estimated
      from maximum HR, and a 5 bpm error moves every hrTSS by about 7%.

    Use it to keep a no-power ride from vanishing out of the load history, not
    to make fine judgements about it.
    """
    return training_stress_score(duration_s, avg_hr / threshold_hr)


def _positive(value: float | None) -> float | None:
    """A duration (or other measured quantity) that is really one, or None.

    Null, zero and negative are the same answer — "this row does not say" —
    and only the first of the three used to be treated that way. Shared by
    every reader that would otherwise fold a stored placeholder zero into a
    real measurement: a lap with no power meter reads `avg_power` as 0, not
    absent, and comparing that against a target reported it as "under" rather
    than as unverifiable.
    """
    return value if value is not None and value > 0 else None


def plural(count: int, noun: str) -> str:
    """ "1 lap" / "3 laps" — the count and its noun, agreeing.

    The consonant-y swap English makes on the plural — "entry" -> "entries",
    never "entrys" — is the one spelling rule needed beyond +s. Shared by the
    coach and nutrition layers, which each hand-rolled it per message before.
    """
    if count == 1:
        return f"{count} {noun}"
    if len(noun) >= 2 and noun[-1] == "y" and noun[-2] not in "aeiou":
        return f"{count} {noun[:-1]}ies"
    return f"{count} {noun}s"


#: The irregular pairs these layers conjugate: "was left out" / "were left
#: out", "is unknown" / "are unknown". Everything else is regular present
#: tense.
_IRREGULAR_AGREEMENT = {"was": "were", "is": "are"}


def agree(count: int, verb: str) -> str:
    """The verb form for a subject of `count`, singular or plural.

    Regular present tense: `sum` -> `sums`/`sum`, and the consonant-y spelling
    swap English always makes on the singular — `carry` -> `carries`, never
    `carrys`. Plus the irregular pairs the layers need, `was`/`were` and
    `is`/`are`, via `_IRREGULAR_AGREEMENT`.
    """
    if verb in _IRREGULAR_AGREEMENT:
        return verb if count == 1 else _IRREGULAR_AGREEMENT[verb]
    if count != 1:
        return verb
    if len(verb) >= 2 and verb[-1] == "y" and verb[-2] not in "aeiou":
        return verb[:-1] + "ies"
    return f"{verb}s"


def one_of(value: Any, allowed: tuple[str, ...], what: str) -> str | None:
    """A fixed vocabulary, matched case-insensitively and returned in its stored spelling.

    Shared by `coach._one_of` and `nutrition._one_of`, which used to disagree on
    this exact point — nutrition folded case before matching, coach did not, so
    `status="PLANNED"` was a valid datum in one layer and a refusal in the other
    for the same word. Living here rather than in either layer: `coach.py`
    imports `GENDERS` from `nutrition.py` at module load, so a module-level
    import in the other direction would be a real circular import, and this
    module has no dependency on either.

    Returns the *canonical* spelling from `allowed` (not a lowercased one) so a
    vocabulary that is not itself lowercase — `EVENT_PRIORITIES = ("A", "B",
    "C")` — still round-trips to its own stored form rather than to "a"/"b"/"c".
    Raises a plain `ValueError` naming `what`; each caller wraps that in its own
    refusal type (`CoachError` / `NutritionError`) so every existing
    `except NutritionError` in `nutrition.py`'s per-item loops keeps catching
    what it always caught.
    """
    if value is None:
        return None
    text = str(value).strip()
    folded = text.lower()
    for candidate in allowed:
        if candidate.lower() == folded:
            return candidate
    raise ValueError(f"{what} must be one of {list(allowed)}, got {value!r}")


def compute_activity_load(
    activity: dict,
    ftp: int | None,
    threshold_hr: int | None,
    threshold_hr_estimated: bool = False,
) -> Load:
    """The load for one stored activity, preferring power and saying which it used.

    Order: normalised power against the FTP in effect on the ride's own date;
    then average power (flagged, because it understates a variable ride —
    NP exceeds average whenever the effort moves); then heart rate; then
    nothing, with a reason.

    `duration_s` is the ride's elapsed duration as Garmin reported it, not
    moving time. That matches how NP was computed over the file. Where the two
    diverge sharply — a café stop, autopause off — the stored
    `moving_duration_s` is the place to look.
    """
    duration = _positive(activity.get("duration_s"))
    if duration is None:
        return Load(None, "none", reason="the activity has no duration")

    np_watts = activity.get("normalized_power")
    avg_watts = activity.get("avg_power")

    if ftp and (np_watts or avg_watts):
        if np_watts:
            return Load(
                tss=power_tss(duration, np_watts, ftp),
                method="power",
                intensity_factor=np_watts / ftp,
                ftp_used=ftp,
            )
        return Load(
            tss=power_tss(duration, avg_watts, ftp),
            method="power_avg",
            intensity_factor=avg_watts / ftp,
            ftp_used=ftp,
            flags=["no_normalized_power"],
            reason=(
                "no normalised power on this ride, so average power was used. NP is never "
                "below average, so this understates a variable ride."
            ),
        )

    avg_hr = activity.get("avg_hr")
    if threshold_hr and avg_hr:
        flags = ["hr_based"]
        if threshold_hr_estimated:
            flags.append("threshold_hr_estimated")
        return Load(
            tss=hr_tss(duration, avg_hr, threshold_hr),
            method="hr",
            intensity_factor=avg_hr / threshold_hr,
            threshold_hr_used=threshold_hr,
            flags=flags,
            reason=(
                "heart-rate based. Not comparable with a power TSS: it cannot see "
                "variability and cardiac drift inflates long rides."
            ),
        )

    if not ftp and (np_watts or avg_watts):
        return Load(None, "none", reason="power was recorded but no FTP is known for that date")
    if avg_hr and not threshold_hr:
        return Load(
            None,
            "none",
            reason="only heart rate was recorded, and no threshold HR is on file — log one",
        )
    return Load(None, "none", reason="the activity has neither power nor heart rate")


# --------------------------------------------------------------------------
# form
# --------------------------------------------------------------------------


@dataclass
class FormPoint:
    day: date
    tss: float
    ctl: float
    atl: float
    tsb: float

    def as_dict(self) -> dict:
        return {
            "date": self.day.isoformat(),
            "tss": round(self.tss, 1),
            "ctl": round(self.ctl, 1),
            "atl": round(self.atl, 1),
            "tsb": round(self.tsb, 1),
        }


def form_series(
    daily_tss: dict[date, float],
    start: date,
    end: date,
    seed_ctl: float = 0.0,
    seed_atl: float = 0.0,
) -> list[FormPoint]:
    """CTL/ATL/TSB day by day, from the first day with data through `end`.

    The standard exponentially weighted model, one step per calendar day
    including rest days:

        CTL(d) = CTL(d-1) + (TSS(d) - CTL(d-1)) / 42
        ATL(d) = ATL(d-1) + (TSS(d) - ATL(d-1)) / 7
        TSB(d) = CTL(d-1) - ATL(d-1)

    **TSB is yesterday's balance**, which is the convention TrainingPeaks uses:
    the form you carry *into* a day, before that day's session is on it.
    Some tools report same-day CTL - ATL instead; the two differ by a day and
    by roughly the size of the session, which is enough to read a hard Tuesday
    as good form if the convention is mixed up.

    The walk starts at the earliest day in `daily_tss` (or `start`, whichever is
    earlier) so that CTL entering the reported window is built from real
    history rather than from zero. Only `start`..`end` is returned. Seeding from
    zero still means the first weeks of any history are an underestimate — CTL
    needs about 42 days to stop being dominated by its starting value, and the
    caller is told when the run-up is shorter than that.
    """
    first_data = min(daily_tss) if daily_tss else start
    walk_from = min(first_data, start)

    ctl, atl = float(seed_ctl), float(seed_atl)
    points: list[FormPoint] = []
    for day in days_between(walk_from, end):
        tsb = ctl - atl  # yesterday's balance, before today's session lands
        tss = float(daily_tss.get(day, 0.0))
        ctl += (tss - ctl) / CTL_TIME_CONSTANT_DAYS
        atl += (tss - atl) / ATL_TIME_CONSTANT_DAYS
        if start <= day <= end:
            points.append(FormPoint(day=day, tss=tss, ctl=ctl, atl=atl, tsb=tsb))
    return points


# --------------------------------------------------------------------------
# plan vs actual
# --------------------------------------------------------------------------


@dataclass
class BlockComparison:
    index: int
    role: str
    planned_seconds: int
    planned_low_w: int | None
    planned_high_w: int | None
    actual_seconds: float | None
    actual_avg_power_w: float | None
    actual_avg_hr: int | None
    verdict: str
    duration_verdict: str
    sentence: str

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "role": self.role,
            "planned_seconds": self.planned_seconds,
            "planned_target_w": (
                None
                if self.planned_low_w is None
                else (
                    f"{self.planned_low_w} W"
                    if self.planned_low_w == self.planned_high_w
                    else f"{self.planned_low_w}-{self.planned_high_w} W"
                )
            ),
            "actual_seconds": (None if self.actual_seconds is None else round(self.actual_seconds)),
            "actual_avg_power_w": (
                None if self.actual_avg_power_w is None else round(self.actual_avg_power_w)
            ),
            "actual_avg_hr": self.actual_avg_hr,
            "verdict": self.verdict,
            "duration_verdict": self.duration_verdict,
            "sentence": self.sentence,
        }


def compare_block(
    index: int,
    role: str,
    planned_seconds: int,
    planned_low_w: int | None,
    planned_high_w: int | None,
    lap: dict,
    tolerance_pct: float = COMPLIANCE_TOLERANCE_PCT,
) -> BlockComparison:
    """One planned block against the lap that was ridden for it.

    Two verdicts, because they answer different questions and one can be
    knowable while the other is not. `verdict` is about power:
    `on_target` / `under` / `over` / `easier_than_target` (a recovery, warmup or
    cooldown ridden below a target that is a ceiling), `no_power` when the lap
    recorded none, `no_target` when the block asked for none.
    `duration_verdict` is `on_time` / `short` / `long` / `unknown`, judged
    independently — a block cut in half is a deviation whether or not a power
    meter was running.
    `sentence` is the finding as a clause an athlete can read — the point of
    this whole comparison is a sentence like "the second block fell to 228 W
    against a 250 W target", and assembling that from six numeric fields is
    exactly the step where a wrong one gets asserted confidently.
    """
    # A lap with no power meter (or no timer running) reports 0, not a null —
    # Garmin does not distinguish "measured zero" from "nothing measured" on
    # these fields, and a stationary block never legitimately averages 0 W or
    # takes 0 s. Treated as a placeholder rather than a measurement, the same
    # rule compute_activity_load applies to duration.
    actual_power = _positive(lap.get("avg_power"))
    actual_seconds = _positive(lap.get("duration_s"))
    actual_hr = lap.get("avg_hr")
    label = f"the {ordinal(index)} block"

    if planned_low_w is None:
        verdict = "no_target"
        target_text = "no power target"
    else:
        target_text = (
            f"{planned_low_w} W"
            if planned_low_w == planned_high_w
            else f"{planned_low_w}-{planned_high_w} W"
        )

    if planned_low_w is not None and actual_power is not None:
        assert planned_high_w is not None
        slack_low = planned_low_w * (1 - tolerance_pct / 100.0)
        slack_high = planned_high_w * (1 + tolerance_pct / 100.0)
        if actual_power < slack_low:
            # Riding a recovery, warmup or cooldown block easier than
            # prescribed is compliance, not deviation — the target is a
            # ceiling there, and an athlete who spins 135 W in a 146 W
            # recovery has done exactly the right thing. Riding one *over* is
            # the real error, and that is still reported.
            verdict = "easier_than_target" if role in EASY_ROLES else "under"
            verb = "sat at" if verdict == "easier_than_target" else "fell to"
            sentence = f"{label} {verb} {round(actual_power)} W against a {target_text} target"
        elif actual_power > slack_high:
            verdict = "over"
            sentence = f"{label} ran to {round(actual_power)} W against a {target_text} target"
        else:
            verdict = "on_target"
            sentence = f"{label} held {round(actual_power)} W against a {target_text} target"
    elif planned_low_w is None:
        sentence = f"{label} had {target_text}; it was ridden for {_mmss(actual_seconds)}"
    else:
        verdict = "no_power"
        sentence = (
            f"{label} was ridden for {_mmss(actual_seconds)} but recorded no power, so "
            f"the {target_text} target cannot be checked"
        )

    # Duration is judged on its own, not folded into the power verdict. It was
    # promoted only over an already-clean verdict, so a block with no recorded
    # power kept `no_power` however badly its duration deviated — and an
    # HR-only ride abandoned block by block reported nothing wrong. Duration is
    # verifiable without a power meter; that is the whole point of checking it.
    duration_verdict = "unknown"
    if actual_seconds is not None and planned_seconds:
        duration_verdict = "on_time"
        drift = actual_seconds - planned_seconds
        if abs(drift) >= max(30.0, planned_seconds * 0.1):
            duration_verdict = "short" if drift < 0 else "long"
            sentence += (
                f" ({_mmss(actual_seconds)} ridden against {_mmss(planned_seconds)} planned)"
            )

    return BlockComparison(
        index=index,
        role=role,
        planned_seconds=planned_seconds,
        planned_low_w=planned_low_w,
        planned_high_w=planned_high_w,
        actual_seconds=actual_seconds,
        actual_avg_power_w=actual_power,
        actual_avg_hr=actual_hr,
        verdict=verdict,
        duration_verdict=duration_verdict,
        sentence=sentence,
    )


def classify_block(comparison: dict) -> str:
    """ "compliant", "deviating" or "unverifiable" — exactly one, for every block.

    Closed-world on purpose. Enumerating the positive buckets and letting the
    rest fall through meant a block that was neither — a free block whose lap
    carried no duration, `no_target` over `unknown` — counted as evidence the
    session went to plan, and a verdict added to `compare_block` later would
    have done the same silently. Here every `(verdict, duration_verdict)` pair
    lands in exactly one class and an unrecognised one raises.

    Deviating outranks unverifiable, and either axis can supply it: a block can
    be unverifiable on power and short on time at once, and the short is the
    more specific thing known about it.
    """
    verdict = comparison["verdict"]
    duration_verdict = comparison["duration_verdict"]
    if verdict not in KNOWN_VERDICTS:
        raise ValueError(f"unrecognised block verdict {verdict!r}")
    if duration_verdict not in KNOWN_DURATION_VERDICTS:
        raise ValueError(f"unrecognised block duration_verdict {duration_verdict!r}")
    if verdict in DEVIATING_VERDICTS or duration_verdict in DEVIATING_DURATION_VERDICTS:
        return "deviating"
    if verdict in UNVERIFIABLE_VERDICTS or duration_verdict in UNVERIFIABLE_DURATION_VERDICTS:
        return "unverifiable"
    return "compliant"


def format_duration_or(seconds: float | None, absent: str) -> str:
    """A duration, or the phrase that stands in when there is no duration.

    One formatter for every nullable duration this layer prints. `duration_s`
    is nullable on both activities and laps — a thin payload is tolerated and
    flagged, not rejected — and `format_duration(round(x or 0))` turned every
    one of those into a confident "0:00": a ride reported as zero-length, and a
    fabricated deviation against whatever was planned.

    Formatting is `spec.format_duration`, so a sentence and the table beside it
    never disagree about how long ten minutes is.
    """
    return absent if seconds is None else format_duration(round(seconds))


def _mmss(seconds: float | None) -> str:
    """A duration for a compliance sentence, or a phrase saying there isn't one."""
    return format_duration_or(seconds, "an unknown time")

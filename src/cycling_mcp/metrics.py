"""Duration, Normalised Power, Intensity Factor and TSS for a resolved workout.

The model expands the workout to a one-sample-per-second power series and runs
the standard NP calculation over it: a 30-second rolling mean, then the fourth
root of the mean fourth power. IF is NP/FTP and TSS follows from IF and time.

These are estimates from the plan, not from a ride. They exist so a session can
be sanity-checked before it is rendered.
"""

from __future__ import annotations

from dataclasses import dataclass

from .render_garmin import target_band_watts
from .spec import Block, Repeat, Workout, format_duration

# A free-ride block has no target, so it cannot contribute a real number. It is
# scored at this fraction of FTP so the totals stay meaningful, and any workout
# containing one is flagged as an estimate.
FREE_RIDE_ASSUMED_FRACTION = 0.65

NP_WINDOW_SECONDS = 30


@dataclass
class Metrics:
    total_seconds: int
    normalised_power: int
    intensity_factor: float
    tss: float
    average_power: int
    work_kj: float
    contains_free_ride: bool

    def as_dict(self) -> dict:
        result = {
            "total_seconds": self.total_seconds,
            "total_duration": format_duration(self.total_seconds),
            "average_power_w": self.average_power,
            "normalised_power_w": self.normalised_power,
            "intensity_factor": round(self.intensity_factor, 3),
            "tss": round(self.tss, 1),
            "work_kj": round(self.work_kj, 1),
        }
        if self.contains_free_ride:
            result["estimate_note"] = (
                f"Contains free-ride blocks scored at "
                f"{FREE_RIDE_ASSUMED_FRACTION * 100:.0f}% FTP; IF and TSS are approximate."
            )
        return result


def power_series(workout: Workout) -> list[float]:
    """One power sample per second, in watts, for the whole workout."""
    samples: list[float] = []
    for block in workout.steps():
        duration = block.duration_s
        if block.kind == "ramp":
            start, end = block.p_from, block.p_to
            assert start is not None and end is not None
            for second in range(duration):
                # Sample the middle of each second so the ramp is symmetric.
                fraction = start + (end - start) * ((second + 0.5) / duration)
                samples.append(fraction * workout.ftp)
        else:
            nominal = block.nominal()
            fraction = FREE_RIDE_ASSUMED_FRACTION if nominal is None else nominal
            samples.extend([fraction * workout.ftp] * duration)
    return samples


def normalised_power(samples: list[float]) -> float:
    """Standard NP: 30 s rolling mean, then the fourth root of the mean 4th power."""
    if not samples:
        return 0.0
    if len(samples) < NP_WINDOW_SECONDS:
        return sum(samples) / len(samples)

    window_total = sum(samples[:NP_WINDOW_SECONDS])
    fourth_power_total = (window_total / NP_WINDOW_SECONDS) ** 4
    count = 1
    for index in range(NP_WINDOW_SECONDS, len(samples)):
        window_total += samples[index] - samples[index - NP_WINDOW_SECONDS]
        fourth_power_total += (window_total / NP_WINDOW_SECONDS) ** 4
        count += 1
    return (fourth_power_total / count) ** 0.25


def compute_metrics(workout: Workout) -> Metrics:
    samples = power_series(workout)
    total = len(samples)
    np_watts = normalised_power(samples)
    intensity = np_watts / workout.ftp if workout.ftp else 0.0
    tss = (total / 3600.0) * intensity**2 * 100.0
    average = sum(samples) / total if total else 0.0
    return Metrics(
        total_seconds=total,
        normalised_power=round(np_watts),
        intensity_factor=intensity,
        tss=tss,
        average_power=round(average),
        work_kj=sum(samples) / 1000.0,
        contains_free_ride=any(b.kind == "free" for b in workout.steps()),
    )


# --------------------------------------------------------------------------
# human-readable table
# --------------------------------------------------------------------------


def _target_text(block: Block, workout: Workout) -> str:
    if block.kind == "free":
        return "free ride (no target)"
    if block.kind == "ramp":
        low_w = workout.watts(block.p_from)  # type: ignore[arg-type]
        high_w = workout.watts(block.p_to)  # type: ignore[arg-type]
        return (
            f"{low_w} -> {high_w} W "
            f"({block.p_from * 100:.0f} -> {block.p_to * 100:.0f}%)"  # type: ignore[operator]
        )
    low_w = workout.watts(block.p_low)  # type: ignore[arg-type]
    if block.is_scalar_target:
        return f"{low_w} W ({block.p_low * 100:.0f}%)"  # type: ignore[operator]
    high_w = workout.watts(block.p_high)  # type: ignore[arg-type]
    return (
        f"{low_w}-{high_w} W "
        f"({block.p_low * 100:.0f}-{block.p_high * 100:.0f}%)"  # type: ignore[operator]
    )


def _band_text(block: Block, workout: Workout) -> str:
    """What Garmin will actually show for a scalar target.

    A single number cannot be a Garmin target, so one becomes a band — and the
    band is wider at the easy end of the session than the hard end. That is
    deliberate, but it is a difference the athlete should be able to see here
    rather than discover on the bike.
    """
    band = target_band_watts(block, workout)
    return f"{band[0]}-{band[1]} W" if band else ""


def _cadence_text(block: Block) -> str:
    if block.cadence_low is None:
        return ""
    if block.cadence_low == block.cadence_high:
        return f"{block.cadence_low} rpm"
    return f"{block.cadence_low}-{block.cadence_high} rpm"


def _notes_text(block: Block) -> str:
    parts = [p for p in (block.message, block.hr_note) if p]
    return " | ".join(parts)


def describe(workout: Workout) -> str:
    """A block table with computed watts, plus totals and estimated load."""
    stats = compute_metrics(workout)

    rows: list[tuple[str, str, str, str, str, str, str]] = []
    elapsed = 0
    for index, node in enumerate(workout.nodes, start=1):
        if isinstance(node, Repeat):
            group_seconds = node.count * sum(b.duration_s for b in node.blocks)
            elapsed += group_seconds
            rows.append(
                (
                    str(index),
                    f"repeat x{node.count}",
                    format_duration(group_seconds),
                    format_duration(elapsed),
                    "",
                    "",
                    "",
                    "",
                )
            )
            for child_index, child in enumerate(node.blocks, start=1):
                rows.append(
                    (
                        f"{index}.{child_index}",
                        f"  {child.role}",
                        format_duration(child.duration_s),
                        "",
                        _target_text(child, workout),
                        _band_text(child, workout),
                        _cadence_text(child),
                        _notes_text(child),
                    )
                )
        else:
            elapsed += node.duration_s
            rows.append(
                (
                    str(index),
                    node.role,
                    format_duration(node.duration_s),
                    format_duration(elapsed),
                    _target_text(node, workout),
                    _band_text(node, workout),
                    _cadence_text(node),
                    _notes_text(node),
                )
            )

    headers = ("#", "Block", "Dur", "Elapsed", "Target", "Garmin", "Cadence", "Notes")
    if not any(row[5] for row in rows):
        # Nothing to say — every target is already a range, a ramp or a free
        # ride. An empty column is worse than no column.
        headers = tuple(h for h in headers if h != "Garmin")
        rows = [row[:5] + row[6:] for row in rows]
    widths = [
        max(len(headers[column]), *(len(row[column]) for row in rows))
        for column in range(len(headers))
    ]

    def line(values: tuple[str, ...]) -> str:
        return "  ".join(value.ljust(widths[i]) for i, value in enumerate(values)).rstrip()

    provenance = workout.ftp_provenance()
    header_text = f"{workout.name}  —  FTP {workout.ftp} W"
    if provenance:
        header_text += f" ({provenance})"
    summary = (
        f"{format_duration(stats.total_seconds)} total · "
        f"NP {stats.normalised_power} W · IF {stats.intensity_factor:.2f} · "
        f"TSS {stats.tss:.0f} · {stats.work_kj:.0f} kJ"
    )

    body = [header_text, summary, "", line(headers), line(tuple("-" * w for w in widths))]
    body.extend(line(row) for row in rows)

    if stats.contains_free_ride:
        body.append("")
        body.append(
            f"Note: free-ride blocks are scored at {FREE_RIDE_ASSUMED_FRACTION * 100:.0f}% FTP, "
            "so IF and TSS are approximate."
        )

    # The arrow in a ramp row reads as a sweep, and this table is what the
    # skills tell you to show the athlete — so it is the wrong place to leave
    # the Garmin flattening unsaid. The .zwo does ramp properly; only Garmin
    # needs the caveat, so name the platform rather than qualifying the row.
    flat = [b for b in workout.steps() if b.kind == "ramp" and b.ramp_steps == 1]
    if flat:
        body.append("")
        body.append(
            f"Note: {len(flat)} ramp{'s' if len(flat) > 1 else ''} above "
            f"{'show' if len(flat) > 1 else 'shows'} a sweep. "
            "MyWhoosh rides that sweep; Garmin has no ramp primitive, so each becomes one "
            "step showing the whole range as a band to hold. Set ramp_steps > 1 to "
            "stair-step it for Garmin."
        )
    if workout.warnings:
        body.append("")
        body.append("Warnings:")
        body.extend(f"  - {w}" for w in workout.warnings)

    return "\n".join(body)

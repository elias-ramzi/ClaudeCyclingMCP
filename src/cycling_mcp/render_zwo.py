"""Render a resolved workout to a MyWhoosh-compatible `.zwo` file.

Two deliberate choices, both about surviving MyWhoosh's editor:

1. Every ramp is emitted as `<Ramp>` with explicit PowerLow -> PowerHigh, never
   as `<Warmup>` or `<Cooldown>`. Cooldown ramp direction is read differently by
   different implementations; an explicit Ramp cannot be misread.
2. Repeated sets are flattened into individual blocks rather than emitted as
   `<IntervalsT>`. MyWhoosh's editor treats an IntervalsT block as indivisible,
   so a single repetition cannot be adjusted after import.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape, quoteattr

from .spec import Block, Repeat, Workout
from .text import fold_with_warning

# Seconds into a block before its message appears. Resistance needs a moment to
# settle after a transition, so text fired at 0 is read against the wrong effort.
MESSAGE_OFFSET_SECONDS = 10
HR_NOTE_OFFSET_SECONDS = 25


def format_fraction(value: float) -> str:
    """Fractions of FTP with up to five decimals and no trailing zero noise."""
    text = f"{value:.5f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def safe_filename(name: str) -> str:
    """Turn a workout name into a conservative filename stem.

    MyWhoosh takes the workout's library name from the uploaded *filename*, not
    from the <name> tag, so this string is what the athlete will actually see.
    """
    from .text import fold_to_ascii

    stem = fold_to_ascii(name).strip()
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "", stem)
    stem = re.sub(r"[\s_]+", "-", stem).strip("-.")
    return stem or "workout"


def _text_events(block: Block, warnings: list[str], where: str) -> list[str]:
    """Message and HR-note text events for one block.

    The heart-rate note is carried as a message and never as a control target:
    both platforms drive on power, and an HR range in a session description is a
    check figure, not something to chase.
    """
    events: list[str] = []
    limit = max(block.duration_s - 5, 0)

    for raw, offset, label in (
        (block.message, MESSAGE_OFFSET_SECONDS, "message"),
        (block.hr_note, HR_NOTE_OFFSET_SECONDS, "hr_note"),
    ):
        if not raw:
            continue
        folded, warning = fold_with_warning(raw, f"{where} {label}")
        if warning:
            warnings.append(warning)
        events.append(f'<textevent timeoffset="{min(offset, limit)}" message={quoteattr(folded)}/>')
    return events


def _element(block: Block, warnings: list[str], where: str) -> list[str]:
    """One `.zwo` element, as a list of lines (indented by the caller)."""
    attributes: list[str] = [f'Duration="{block.duration_s}"']

    if block.kind == "steady":
        attributes.append(f'Power="{format_fraction(block.nominal())}"')  # type: ignore[arg-type]
        tag = "SteadyState"
    elif block.kind == "ramp":
        attributes.append(f'PowerLow="{format_fraction(block.p_from)}"')  # type: ignore[arg-type]
        attributes.append(f'PowerHigh="{format_fraction(block.p_to)}"')  # type: ignore[arg-type]
        tag = "Ramp"
    else:
        attributes.append('FlatRoad="1"')
        tag = "FreeRide"

    if block.cadence_low is not None:
        # `.zwo` carries a single cadence per block, so a range becomes its
        # midpoint. The Garmin renderer keeps the full range.
        cadence = round((block.cadence_low + block.cadence_high) / 2)  # type: ignore[operator]
        attributes.append(f'Cadence="{cadence}"')

    opening = f"<{tag} {' '.join(attributes)}"
    events = _text_events(block, warnings, where)
    if not events:
        return [opening + "/>"]
    return [opening + ">", *[f"    {event}" for event in events], f"</{tag}>"]


def _description_text(workout: Workout) -> str:
    provenance = workout.ftp_provenance()
    if not provenance:
        return workout.description
    note = f"Built against FTP {workout.ftp} W ({provenance})."
    return f"{workout.description} {note}".strip()


def render_zwo(workout: Workout) -> tuple[str, list[str]]:
    """Return the `.zwo` XML and any warnings raised while rendering."""
    warnings: list[str] = []

    def folded(value: str, label: str) -> str:
        result, warning = fold_with_warning(value, label)
        if warning:
            warnings.append(warning)
        return result

    lines = [
        "<workout_file>",
        f"    <author>{escape(folded(workout.author, 'author'))}</author>",
        f"    <name>{escape(folded(workout.name, 'name'))}</name>",
        # The FTP a .zwo was built against is not recoverable from the file —
        # it stores fractions — so when the spec records where the number came
        # from, that goes in the one field the athlete can still read later.
        f"    <description>{escape(folded(_description_text(workout), 'description'))}</description>",
        "    <sportType>bike</sportType>",
        "    <tags></tags>",
        "    <workout>",
    ]

    for index, node in enumerate(workout.nodes):
        if isinstance(node, Repeat):
            for iteration in range(node.count):
                for child_index, child in enumerate(node.blocks):
                    # Repeats are flattened for MyWhoosh, so each child is
                    # emitted `count` times — but it was authored once, and a
                    # warning about its content is one fact, not `count` facts.
                    # Collect only on the first pass, and name where the author
                    # can find it rather than which repetition produced it.
                    sink = warnings if iteration == 0 else []
                    where = f"blocks[{index}].blocks[{child_index}]"
                    lines.extend(f"        {line}" for line in _element(child, sink, where))
        else:
            lines.extend(f"        {line}" for line in _element(node, warnings, f"blocks[{index}]"))

    lines.append("    </workout>")
    lines.append("</workout_file>")
    return "\n".join(lines) + "\n", warnings


def zwo_filename(workout: Workout) -> str:
    """The filename to upload under — this becomes the MyWhoosh library name."""
    stem = workout.filename or workout.name
    stem = safe_filename(stem)
    if stem.lower().endswith(".zwo"):
        return stem
    return f"{stem}.zwo"

"""Parsing, validation and normalisation of the canonical workout spec.

The spec is a flat, hand-editable JSON document. This module turns it into a
resolved tree of blocks with every power expressed as a fraction of FTP, which
is the single representation both renderers consume.

Nothing here touches the network or the filesystem.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

# A block's power, as a fraction of FTP, must land inside this range. Anything
# outside is far more likely to be a unit mistake (watts written into a _pct
# field, or a percentage written as 91.0 into a fraction) than a real target.
MIN_POWER_FRACTION = 0.2
MAX_POWER_FRACTION = 2.0

# Outside this narrower range the value is legal but worth a second look.
SANE_POWER_FRACTION = (0.3, 1.6)

CADENCE_RANGE = (30, 150)

BLOCK_TYPES = ("steady", "ramp", "free", "repeat")
ROLES = ("warmup", "interval", "recovery", "cooldown")

_COMMON_KEYS = {"type", "duration", "cadence", "message", "hr_note", "role", "comment"}
_ALLOWED_KEYS = {
    "steady": _COMMON_KEYS | {"power_pct", "power_w"},
    "ramp": _COMMON_KEYS | {"from_pct", "to_pct", "from_w", "to_w", "ramp_steps"},
    "free": _COMMON_KEYS,
    "repeat": {"type", "count", "blocks", "comment"},
}

_SPEC_KEYS = {
    "name",
    "author",
    "description",
    "ftp",
    "filename",
    "garmin_target_band_pct",
    "ftp_source",
    "ftp_date",
    "blocks",
    "comment",
}

# Where an FTP came from. Six months on, a workout is a set of raw watts with
# no record of which athlete-number produced them; this keeps the provenance
# with the session rather than only in whoever's memory built it.
FTP_SOURCES = ("athlete_stated", "garmin_profile", "test_result")

# The band placed around a scalar power target, per role. Garmin needs ranges,
# and one width does not fit both ends of a session: +/-2% of a 250 W interval
# is +/-5 W, which is right, while +/-2% of a 140 W recovery is +/-3 W — a
# window narrow enough to alarm continuously on an easy spin. Reported from a
# real ride, 2026-08-19. An explicit garmin_target_band_pct overrides all of it.
DEFAULT_TARGET_BAND_PCT = 2.0
RECOVERY_TARGET_BAND_PCT = 5.0


class SpecError(ValueError):
    """Raised when a spec is invalid and cannot be rendered."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors) if errors else "invalid spec")


@dataclass
class Block:
    """One executable block, with power resolved to fractions of FTP."""

    kind: Literal["steady", "ramp", "free"]
    duration_s: int
    role: str = "interval"
    role_explicit: bool = False
    # steady: the target band (equal endpoints mean "a single number").
    p_low: float | None = None
    p_high: float | None = None
    # ramp: the two endpoints, in order.
    p_from: float | None = None
    p_to: float | None = None
    ramp_steps: int = 1
    cadence_low: int | None = None
    cadence_high: int | None = None
    message: str | None = None
    hr_note: str | None = None

    @property
    def is_scalar_target(self) -> bool:
        """True when the author gave one number rather than a range."""
        return self.p_low is not None and self.p_low == self.p_high

    def nominal(self) -> float | None:
        """The single fraction that best represents this block.

        `.zwo` steady blocks carry one power value, and the metrics model needs
        one number per second, so a range collapses to its midpoint.
        """
        if self.kind == "free":
            return None
        if self.kind == "ramp":
            return (self.p_from + self.p_to) / 2.0  # type: ignore[operator]
        return (self.p_low + self.p_high) / 2.0  # type: ignore[operator]


@dataclass
class Repeat:
    """A repeated set. Flattened for `.zwo`, native for Garmin."""

    count: int
    blocks: list[Block] = field(default_factory=list)


Node = Block | Repeat


@dataclass
class Workout:
    """A fully resolved, render-ready workout."""

    name: str
    ftp: int
    nodes: list[Node]
    author: str = "ClaudeCyclingMCP"
    description: str = ""
    filename: str | None = None
    # None means no explicit override; the Garmin renderer then bands by role.
    target_band_pct: float | None = None
    ftp_source: str | None = None
    ftp_date: str | None = None
    warnings: list[str] = field(default_factory=list)

    def ftp_provenance(self) -> str | None:
        """Where this FTP came from, as a phrase, or None if unrecorded."""
        if self.ftp_source is None:
            return None
        phrase = {
            "athlete_stated": "athlete stated",
            "garmin_profile": "Garmin profile",
            "test_result": "test result",
        }[self.ftp_source]
        return f"{phrase}, {self.ftp_date}" if self.ftp_date else phrase

    def steps(self) -> Iterator[Block]:
        """Every executable block in execution order, repeats expanded."""
        for node in self.nodes:
            if isinstance(node, Repeat):
                for _ in range(node.count):
                    yield from node.blocks
            else:
                yield node

    @property
    def total_seconds(self) -> int:
        return sum(b.duration_s for b in self.steps())

    def watts(self, fraction: float) -> int:
        return round(fraction * self.ftp)


# --------------------------------------------------------------------------
# duration parsing
# --------------------------------------------------------------------------


def parse_duration(raw: Any) -> int:
    """Accept integer seconds, or "MM:SS" / "HH:MM:SS".

    Raises ValueError with a message suitable for showing to the author.
    """
    if isinstance(raw, bool):
        raise ValueError('duration must be seconds or "MM:SS", not a boolean')
    if isinstance(raw, (int, float)):
        seconds = float(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ValueError("duration is empty")
        parts = text.split(":")
        if len(parts) > 3:
            raise ValueError(f"{raw!r} has too many ':' parts; use MM:SS or HH:MM:SS")
        try:
            values = [float(p) for p in parts]
        except ValueError:
            raise ValueError(
                f'{raw!r} is not a duration; use seconds (600) or "MM:SS" ("10:00")'
            ) from None
        if len(values) == 1:
            seconds = values[0]
        elif len(values) == 2:
            seconds = values[0] * 60 + values[1]
        else:
            seconds = values[0] * 3600 + values[1] * 60 + values[2]
    else:
        raise ValueError(f"duration must be a number or string, got {type(raw).__name__}")

    if seconds != int(seconds):
        raise ValueError("duration must be a whole number of seconds")
    return int(seconds)


def format_duration(seconds: int) -> str:
    """Seconds as MM:SS, or HH:MM:SS once it passes an hour."""
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


# --------------------------------------------------------------------------
# validation + normalisation
# --------------------------------------------------------------------------


class _Collector:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(f"{path}: {message}")


def _as_pair(raw: Any, path: str, key: str, out: _Collector) -> tuple[float, float] | None:
    """Read a scalar or a [low, high] pair of positive numbers."""
    if isinstance(raw, bool):
        out.error(path, f"{key} must be a number or [low, high], not a boolean")
        return None
    if isinstance(raw, (int, float)):
        return (float(raw), float(raw))
    if isinstance(raw, (list, tuple)):
        if len(raw) != 2:
            out.error(path, f"{key} as a list must have exactly 2 entries [low, high]")
            return None
        try:
            low, high = float(raw[0]), float(raw[1])
        except (TypeError, ValueError):
            out.error(path, f"{key} list entries must be numbers")
            return None
        if low > high:
            out.error(path, f"{key} is [{low:g}, {high:g}] — low must not exceed high")
            return None
        return (low, high)
    out.error(path, f"{key} must be a number or [low, high], got {type(raw).__name__}")
    return None


def _check_fraction(value: float, path: str, key: str, out: _Collector) -> bool:
    if not (MIN_POWER_FRACTION <= value <= MAX_POWER_FRACTION):
        out.error(
            path,
            f"{key} resolves to {value * 100:.0f}% FTP, outside the sane range "
            f"{MIN_POWER_FRACTION * 100:.0f}-{MAX_POWER_FRACTION * 100:.0f}% "
            f"(check whether watts and percentages got swapped)",
        )
        return False
    low, high = SANE_POWER_FRACTION
    if not (low <= value <= high):
        out.warn(path, f"{key} is {value * 100:.0f}% FTP — unusual, but rendered as given")
    return True


def _resolve_power(
    block: dict,
    path: str,
    ftp: float | None,
    keys: tuple[str, str],
    out: _Collector,
) -> tuple[float, float] | None:
    """Resolve one power field, given as either <name>_pct or <name>_w.

    `keys` is the (percent_key, watt_key) pair. Returns fractions of FTP.
    """
    pct_key, watt_key = keys
    has_pct, has_w = pct_key in block, watt_key in block

    if has_pct and has_w:
        out.error(path, f"has both {pct_key} and {watt_key} — give exactly one")
        return None
    if not has_pct and not has_w:
        out.error(path, f"missing power: set either {pct_key} or {watt_key}")
        return None

    key = pct_key if has_pct else watt_key
    pair = _as_pair(block[key], path, key, out)
    if pair is None:
        return None

    if has_pct:
        low, high = pair[0] / 100.0, pair[1] / 100.0
    else:
        if not ftp:
            return None  # ftp error already reported at the top level
        low, high = pair[0] / ftp, pair[1] / ftp

    ok = _check_fraction(low, path, key, out)
    if high != low:
        ok = _check_fraction(high, path, key, out) and ok
    return (low, high) if ok else None


def _resolve_cadence(block: dict, path: str, out: _Collector) -> tuple[int | None, int | None]:
    if "cadence" not in block:
        return (None, None)
    pair = _as_pair(block["cadence"], path, "cadence", out)
    if pair is None:
        return (None, None)
    low, high = round(pair[0]), round(pair[1])
    lo_limit, hi_limit = CADENCE_RANGE
    for value in {low, high}:
        if not (lo_limit <= value <= hi_limit):
            out.warn(path, f"cadence {value} rpm is outside {lo_limit}-{hi_limit} rpm")
    return (low, high)


def _resolve_block(raw: Any, path: str, ftp: float | None, out: _Collector) -> Block | None:
    if not isinstance(raw, dict):
        out.error(path, f"must be an object, got {type(raw).__name__}")
        return None

    kind = raw.get("type")
    if kind not in ("steady", "ramp", "free"):
        out.error(path, f"unknown block type {kind!r}; expected one of {', '.join(BLOCK_TYPES)}")
        return None

    for key in raw:
        if key not in _ALLOWED_KEYS[kind]:
            out.warn(path, f"unknown key {key!r} on a {kind} block — ignored (typo?)")

    if "duration" not in raw:
        out.error(path, "missing duration")
        return None
    try:
        duration = parse_duration(raw["duration"])
    except ValueError as exc:
        out.error(path, str(exc))
        return None
    if duration <= 0:
        out.error(path, f"duration is {duration}s — must be greater than zero")
        return None

    role = raw.get("role")
    if role is not None and role not in ROLES:
        out.error(path, f"unknown role {role!r}; expected one of {', '.join(ROLES)}")
        role = None

    cadence_low, cadence_high = _resolve_cadence(raw, path, out)
    message = _text(raw.get("message"), path, "message", out)
    hr_note = _text(raw.get("hr_note"), path, "hr_note", out)

    block = Block(
        kind=kind,
        duration_s=duration,
        role=role or "interval",
        role_explicit=role is not None,
        cadence_low=cadence_low,
        cadence_high=cadence_high,
        message=message,
        hr_note=hr_note,
    )

    if kind == "steady":
        power = _resolve_power(raw, path, ftp, ("power_pct", "power_w"), out)
        if power is None:
            return None
        block.p_low, block.p_high = power
    elif kind == "ramp":
        start = _resolve_power(raw, path, ftp, ("from_pct", "from_w"), out)
        end = _resolve_power(raw, path, ftp, ("to_pct", "to_w"), out)
        if start is None or end is None:
            return None
        if start[0] != start[1] or end[0] != end[1]:
            out.error(path, "ramp endpoints must be single numbers, not ranges")
            return None
        if start[0] == end[0]:
            out.error(
                path,
                f"ramp starts and ends at {start[0] * 100:.0f}% FTP — "
                "a ramp that does not change is a steady block",
            )
            return None
        block.p_from, block.p_to = start[0], end[0]
        steps = raw.get("ramp_steps", 1)
        if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1:
            out.error(path, f"ramp_steps must be an integer >= 1, got {steps!r}")
            return None
        if steps > duration:
            out.error(path, f"ramp_steps ({steps}) exceeds the block duration ({duration}s)")
            return None
        block.ramp_steps = steps

    return block


def _text(raw: Any, path: str, key: str, out: _Collector) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        out.error(path, f"{key} must be a string, got {type(raw).__name__}")
        return None
    text = raw.strip()
    return text or None


def _resolve_repeat(raw: dict, path: str, ftp: float | None, out: _Collector) -> Repeat | None:
    for key in raw:
        if key not in _ALLOWED_KEYS["repeat"]:
            out.warn(path, f"unknown key {key!r} on a repeat block — ignored (typo?)")

    count = raw.get("count")
    if not isinstance(count, int) or isinstance(count, bool):
        out.error(path, f"count must be an integer, got {count!r}")
        return None
    if count < 1:
        out.error(path, f"count is {count} — must be at least 1")
        return None
    if count == 1:
        out.warn(path, "count is 1 — the repeat wrapper has no effect")

    body = raw.get("blocks")
    if not isinstance(body, list) or not body:
        out.error(path, "a repeat needs a non-empty 'blocks' list")
        return None

    blocks: list[Block] = []
    for index, child in enumerate(body):
        child_path = f"{path}.blocks[{index}]"
        if isinstance(child, dict) and child.get("type") == "repeat":
            out.error(child_path, "repeats cannot be nested — flatten the inner set")
            continue
        resolved = _resolve_block(child, child_path, ftp, out)
        if resolved is not None:
            blocks.append(resolved)

    if not blocks:
        return None
    return Repeat(count=count, blocks=blocks)


def _infer_roles(nodes: list[Node]) -> None:
    """Fill in warmup/cooldown for the outer executable blocks.

    Only blocks the author left unlabelled are touched, and only at the top
    level — a block inside a repeat is an interval unless it says otherwise.
    """
    executable = [(i, n) for i, n in enumerate(nodes) if isinstance(n, Block)]
    if not executable:
        return
    first_index, first = executable[0]
    if first_index == 0 and not first.role_explicit:
        first.role = "warmup"
    last_index, last = executable[-1]
    if last_index == len(nodes) - 1 and last is not first and not last.role_explicit:
        last.role = "cooldown"


def validate_spec(spec: Any) -> tuple[Workout | None, list[str], list[str]]:
    """Validate a spec.

    Returns (workout, errors, warnings). `workout` is None when errors is
    non-empty. Warnings never block rendering.
    """
    out = _Collector()

    if not isinstance(spec, dict):
        return None, [f"spec must be a JSON object, got {type(spec).__name__}"], []

    for key in spec:
        if key not in _SPEC_KEYS:
            out.warn("spec", f"unknown top-level key {key!r} — ignored (typo?)")

    name = _text(spec.get("name"), "spec", "name", out)
    if not name:
        out.error("spec", "missing 'name' — it titles the workout on both platforms")

    ftp_raw = spec.get("ftp")
    ftp: float | None = None
    if ftp_raw is None:
        out.error(
            "spec",
            "missing 'ftp' — .zwo stores power as a fraction of FTP, so it is required "
            "even when every block is written in watts",
        )
    elif isinstance(ftp_raw, bool) or not isinstance(ftp_raw, (int, float)):
        out.error("spec", f"ftp must be a number, got {type(ftp_raw).__name__}")
    elif ftp_raw <= 0:
        out.error("spec", f"ftp is {ftp_raw} — must be greater than zero")
    elif not (50 <= ftp_raw <= 600):
        out.warn("spec", f"ftp of {ftp_raw:g} W is unusual — check it is watts, not a percentage")
        ftp = float(ftp_raw)
    else:
        ftp = float(ftp_raw)

    # None means "no explicit override", which is what lets the renderer pick a
    # band by role. A caller who sets the knob still gets exactly that number.
    band: float | None = None
    if "garmin_target_band_pct" in spec:
        raw_band = spec["garmin_target_band_pct"]
        if isinstance(raw_band, bool) or not isinstance(raw_band, (int, float)) or raw_band < 0:
            out.error("spec", f"garmin_target_band_pct must be a number >= 0, got {raw_band!r}")
        else:
            band = float(raw_band)

    ftp_source = _text(spec.get("ftp_source"), "spec", "ftp_source", out)
    if ftp_source is not None and ftp_source not in FTP_SOURCES:
        out.error("spec", f"ftp_source must be one of {list(FTP_SOURCES)}, got {ftp_source!r}")
        ftp_source = None
    ftp_date = _text(spec.get("ftp_date"), "spec", "ftp_date", out)

    author = _text(spec.get("author"), "spec", "author", out)
    description = _text(spec.get("description"), "spec", "description", out)
    filename = _text(spec.get("filename"), "spec", "filename", out)

    raw_blocks = spec.get("blocks")
    nodes: list[Node] = []
    if not isinstance(raw_blocks, list):
        out.error("spec", "missing 'blocks' list")
    elif not raw_blocks:
        out.error("spec", "'blocks' is empty — a workout needs at least one block")
    else:
        for index, raw in enumerate(raw_blocks):
            path = f"blocks[{index}]"
            if isinstance(raw, dict) and raw.get("type") == "repeat":
                node = _resolve_repeat(raw, path, ftp, out)
            else:
                node = _resolve_block(raw, path, ftp, out)
            if node is not None:
                nodes.append(node)

    if out.errors:
        return None, out.errors, out.warnings

    _infer_roles(nodes)

    workout = Workout(
        name=name or "",
        ftp=int(ftp or 0),
        nodes=nodes,
        author=author or "ClaudeCyclingMCP",
        description=description or "",
        filename=filename,
        target_band_pct=band,
        ftp_source=ftp_source,
        ftp_date=ftp_date,
        warnings=list(out.warnings),
    )
    return workout, [], out.warnings


def load_spec(spec: Any) -> Workout:
    """Validate and return the workout, raising SpecError on any error."""
    workout, errors, _ = validate_spec(spec)
    if workout is None:
        raise SpecError(errors)
    return workout

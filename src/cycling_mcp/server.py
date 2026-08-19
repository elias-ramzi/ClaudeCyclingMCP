"""MCP server: author a cycling workout once, render it for both platforms.

Every tool here is pure and deterministic. Nothing in this server makes a
network call, reads credentials, or uploads anything. Uploading needs auth and
a human in the loop, so it lives in the bundled skills instead — see
`.claude/skills/garmin-upload` and `.claude/skills/mywhoosh-upload`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:  # mcp SDK 2.x
    from mcp.server import MCPServer as _Server
except ImportError:  # mcp SDK 1.x, where the same class is called FastMCP
    from mcp.server.fastmcp import FastMCP as _Server

from . import __version__
from .metrics import compute_metrics, describe
from .render_garmin import render_garmin as _render_garmin
from .render_zwo import render_zwo as _render_zwo
from .render_zwo import zwo_filename
from .skills import Skill, build_skill_message, load_skills
from .spec import SpecError, load_spec
from .spec import validate_spec as _validate_spec
from .verify import compare_upload, total_step_seconds


def _build_server():
    """Construct the server, advertising the package version in the handshake.

    The version is the contract an MCP client sees, so it belongs in
    `serverInfo`. Older SDKs do not accept a `version` argument, and passing an
    unknown keyword there would take the server down entirely — so fall back to
    an unversioned handshake rather than failing to start.
    """
    try:
        return _Server("claude-cycling-mcp", version=__version__)
    except TypeError:
        return _Server("claude-cycling-mcp")


app = _build_server()

SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name", "ftp", "blocks"],
    "properties": {
        "name": {"type": "string", "description": "Workout title, used on both platforms."},
        "ftp": {
            "type": "number",
            "description": (
                "Functional threshold power in watts. Always required: .zwo stores power "
                "as a fraction of FTP, so it is needed even when blocks are written in watts. "
                "Source it from Garmin's get_cycling_ftp, but check the is_stale flag and "
                "confirm with the athlete rather than trusting a stale profile value."
            ),
        },
        "author": {"type": "string"},
        "description": {"type": "string"},
        "filename": {
            "type": "string",
            "description": (
                "Optional filename stem for the .zwo. MyWhoosh takes the workout's library "
                "name from the uploaded filename, NOT from the <name> tag, so this is what "
                "the athlete sees in their library. Defaults to a slug of 'name'."
            ),
        },
        "garmin_target_band_pct": {
            "type": "number",
            "default": 2.0,
            "description": (
                "Half-width of the watt band placed around a scalar power target when "
                "rendering for Garmin, which needs ranges. Ignored where the spec already "
                "gives an explicit [low, high]."
            ),
        },
        "blocks": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/block"}},
    },
    "$defs": {
        "duration": {
            "description": 'Whole seconds (600) or a clock string ("10:00", "1:05:00").',
            "oneOf": [{"type": "integer", "minimum": 1}, {"type": "string"}],
        },
        "power": {
            "description": "A single number, or [low, high] for an explicit range.",
            "oneOf": [
                {"type": "number"},
                {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
            ],
        },
        "block": {
            "type": "object",
            "required": ["type", "duration"],
            "properties": {
                "type": {"enum": ["steady", "ramp", "free", "repeat"]},
                "duration": {"$ref": "#/$defs/duration"},
                "power_pct": {
                    "$ref": "#/$defs/power",
                    "description": "steady only. Percent of FTP (91 = 91%). Exclusive with power_w.",
                },
                "power_w": {
                    "$ref": "#/$defs/power",
                    "description": "steady only. Absolute watts. Exclusive with power_pct.",
                },
                "from_pct": {"type": "number", "description": "ramp only. Start, percent of FTP."},
                "to_pct": {"type": "number", "description": "ramp only. End, percent of FTP."},
                "from_w": {"type": "number", "description": "ramp only. Start, watts."},
                "to_w": {"type": "number", "description": "ramp only. End, watts."},
                "ramp_steps": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": (
                        "ramp only. Garmin has no ramp primitive; by default a ramp becomes "
                        "one step showing the whole from->to range. Set >1 to stair-step it "
                        "into that many discrete Garmin steps. Does not affect the .zwo."
                    ),
                },
                "count": {"type": "integer", "minimum": 1, "description": "repeat only."},
                "blocks": {
                    "type": "array",
                    "description": "repeat only. Cannot contain further repeats.",
                    "items": {"$ref": "#/$defs/block"},
                },
                "cadence": {
                    "$ref": "#/$defs/power",
                    "description": (
                        "Target rpm, single value or [low, high]. Garmin keeps the range as a "
                        "secondary target; .zwo carries one value, so a range becomes its midpoint."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": (
                        "On-screen text. .zwo fires it ~10s into the block so resistance settles "
                        "first; Garmin carries it as the step description. Non-ASCII is folded "
                        "for MyWhoosh, which renders accents and curly quotes badly."
                    ),
                },
                "hr_note": {
                    "type": "string",
                    "description": (
                        "A heart-rate check figure, carried as a message on both platforms and "
                        "NEVER as a control target. Both platforms drive on power."
                    ),
                },
                "role": {
                    "enum": ["warmup", "interval", "recovery", "cooldown"],
                    "description": (
                        "Picks the Garmin step type; the .zwo ignores it. Inferred when absent: "
                        "first top-level block is warmup, last is cooldown, everything else is "
                        "interval."
                    ),
                },
            },
        },
    },
}

GARMIN_SCHEMA_NOTES = {
    "power_target": (
        "Power targets use workoutTargetTypeId 2 ('power.zone') with an absolute watt range "
        "in targetValueOne/targetValueTwo and no zoneNumber. This is deliberate and verified "
        "against the live API on 2026-08-12. Do NOT rewrite it to id 6 / 'power.between'."
    ),
    "why_not_id_6": (
        "The Garmin MCP's upload_workout docstring says cycling watt ranges take id 6. Against "
        "the live API that is wrong and fails silently: id 6 uploads without error and Garmin "
        "normalises it to 'pace.zone' on a cycling workout — a pace target, not a power one."
    ),
    "evidence": (
        "Garmin's own web UI writes a watt target as id 2 with raw watts (confirmed by reading "
        "the raw DTO of reference workout 1662651131, entered by hand as 200-220 W); an "
        "upload/fetch probe of both encodings; and a visual check in Garmin Connect showing "
        "watts, not percentages."
    ),
    "not_zone_numbers": (
        "Watt values are never read as zone numbers. Zones are 1-7, and the Garmin MCP only "
        "coerces targetValueOne to a zone when it is between 1 and 5."
    ),
    "percent_ftp": (
        "A %FTP target would be the same id-2 shape plus targetValueUnit "
        "{'unitId': 253, 'unitKey': 'percent'}. This renderer emits watts, so it sends no unit "
        "object at all — that absence is what makes the numbers watts."
    ),
    "repeat_groups": (
        "RepeatGroupDTO carries endCondition with the numeric conditionTypeId 7. Omitting the "
        "id makes the API silently corrupt the repeat count."
    ),
}

_AUTHORING_NOTES = """\
Design rules worth knowing before writing a spec:

- Power units are explicit. A steady block carries either power_pct or power_w,
  never both. Ramps use from_pct/to_pct or from_w/to_w. There is no magic
  guessing from magnitude.
- ftp is mandatory. .zwo is fraction-only, so watts cannot be rendered without it.
- Repeats are flattened in the .zwo (MyWhoosh's editor makes IntervalsT blocks
  indivisible) and emitted as a native RepeatGroupDTO for Garmin.
- Ramps always render as an explicit <Ramp PowerLow PowerHigh>, never <Warmup>
  or <Cooldown>, because cooldown ramp direction is ambiguous across
  implementations.
- hr_note is a check figure. It is never emitted as a heart-rate target.
- The .zwo filename, not the <name> tag, becomes the MyWhoosh library name.
"""


def _write(out_path: str, content: str) -> str:
    path = Path(out_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _dump(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


@app.tool()
def validate_spec(spec: dict) -> str:
    """Check a workout spec and report every problem found.

    Catches the mistakes that actually bite: empty workouts, zero or negative
    durations, power that resolves outside a sane fraction of FTP (usually watts
    and percentages swapped), ramps whose endpoints are equal, both unit forms
    on one block, percentages with no FTP to resolve them against, and empty or
    nested repeats. Unknown keys are reported as warnings, which catches typos
    like "powr_w" that would otherwise be silently ignored.

    Returns valid/errors/warnings, plus a summary of the workout when it is valid.
    """
    workout, errors, warnings = _validate_spec(spec)
    result: dict[str, Any] = {
        "valid": workout is not None,
        "errors": errors,
        "warnings": warnings,
    }
    if workout is not None:
        result["summary"] = compute_metrics(workout).as_dict()
        result["summary"]["name"] = workout.name
        result["summary"]["ftp_w"] = workout.ftp
        result["summary"]["zwo_filename"] = zwo_filename(workout)
    return _dump(result)


@app.tool()
def describe_spec(spec: dict) -> str:
    """Render a workout spec as a human-readable block table for sanity-checking.

    Shows every block with its computed watts, duration and elapsed time, plus
    total duration, average and normalised power, estimated IF and TSS, and
    total work. Use this before rendering to confirm a session is what was
    intended — it is much easier to spot a wrong number here than in XML.
    """
    try:
        workout = load_spec(spec)
    except SpecError as exc:
        return "Spec is invalid, nothing to describe:\n" + "\n".join(f"  - {e}" for e in exc.errors)
    return describe(workout)


@app.tool()
def render_zwo(spec: dict, out_path: str | None = None) -> str:
    """Render a workout spec to a MyWhoosh-compatible .zwo file.

    Returns the XML and the filename to upload it under. That filename matters:
    MyWhoosh takes the workout's library name from the uploaded filename, not
    from the <name> tag inside the file. Set the spec's "filename" field to
    control it.

    Ramps are emitted as explicit <Ramp PowerLow PowerHigh> and repeats are
    flattened into individual blocks, both so the result stays unambiguous and
    editable inside MyWhoosh's editor.

    Pass out_path to also write the file to disk; the content is returned either
    way. Writing is the only filesystem access this server performs.
    """
    try:
        workout = load_spec(spec)
    except SpecError as exc:
        return _dump({"ok": False, "errors": exc.errors})

    xml, warnings = _render_zwo(workout)
    result: dict[str, Any] = {
        "ok": True,
        "filename": zwo_filename(workout),
        "xml": xml,
        "warnings": workout.warnings + warnings,
        "summary": compute_metrics(workout).as_dict(),
    }
    if out_path:
        result["written_to"] = _write(out_path, xml)
    return _dump(result)


@app.tool()
def render_garmin(spec: dict, out_path: str | None = None) -> str:
    """Render a workout spec to a Garmin Connect workout payload.

    The returned "payload" is ready to hand to the Garmin MCP's upload_workout
    tool UNCHANGED. Percentages in the spec are resolved to watts against the
    spec's FTP. Repeats become RepeatGroupDTO groups carrying a complete
    endCondition, including the numeric conditionTypeId that Garmin needs to
    avoid silently corrupting the repeat count.

    Read this before "correcting" the payload: power targets are emitted as
    workoutTargetTypeId 2 ("power.zone") carrying an absolute watt range in
    targetValueOne/targetValueTwo, with no zoneNumber. This contradicts the
    Garmin MCP's own upload_workout docstring, which says cycling watt ranges
    take id 6 / "power.between". Against the live API that guidance is wrong,
    and wrong silently: id 6 uploads without error and Garmin normalises it to
    "pace.zone" on a cycling workout — a pace target, not a power one.

    Id 2 with raw watts is byte-for-byte what Garmin's own web UI writes for a
    watt target, confirmed by reading the raw DTO of a workout built by hand in
    Garmin Connect, by an upload/fetch probe of both encodings, and by opening
    the result in Garmin Connect and seeing watts. Watt values are never read as
    zone numbers: zones are 1-7, and the Garmin MCP only coerces targetValueOne
    to a zone when it is between 1 and 5.

    So do not rewrite the targetType to id 6. Doing so is the one change that
    produces a workout which uploads cleanly and is wrong.

    This server does not upload. Use the bundled garmin-upload skill, which
    uploads and then verifies by fetching the workout back and comparing it
    against what was sent.

    Pass out_path to also write the payload as JSON to disk.
    """
    try:
        workout = load_spec(spec)
    except SpecError as exc:
        return _dump({"ok": False, "errors": exc.errors})

    payload, warnings = _render_garmin(workout)
    result: dict[str, Any] = {
        "ok": True,
        "payload": payload,
        "warnings": workout.warnings + warnings,
        "summary": compute_metrics(workout).as_dict(),
        # Travels with the payload so a client reading it later does not
        # "fix" the target type back into the silently-broken form.
        "schema_notes": GARMIN_SCHEMA_NOTES,
    }
    if out_path:
        result["written_to"] = _write(out_path, _dump(payload))
    return _dump(result)


@app.tool()
def verify_garmin_upload(payload: dict, fetched: dict) -> str:
    """Check that Garmin stored the workout that was actually sent.

    Pass the payload given to upload_workout, and the response from
    get_workout_by_id for the workout it created. Returns the list of
    differences; an empty list means Garmin kept what was sent.

    This compares against the sent payload, not against an assumed read shape —
    the difference matters, because get_workout_by_id returns a lossy
    projection that will happily agree with a wrong-units payload. It also
    catches a repeat count silently corrupted by a missing conditionTypeId.

    Two things it cannot prove: whether a power target was stored as watts or
    as %FTP (the curated read drops the targetValueUnit field that distinguishes
    them), and whether the workout displays correctly on a head unit. Confirm
    those by opening the workout in Garmin Connect once.

    Pure comparison, no network access.
    """
    problems = compare_upload(payload, fetched)
    return _dump(
        {
            "match": not problems,
            "differences": problems,
            "sent_step_seconds": total_step_seconds(payload),
            "note": (
                "Garmin's estimated_duration_seconds follows its own rules and can "
                "disagree with the sum of the steps; it is not used for this check."
            ),
        }
    )


@app.tool()
def get_skill(name: str | None = None) -> str:
    """Fetch a bundled procedure for uploading a workout to a platform.

    Call this whenever you are asked to follow, use, or run one of this
    server's skills by name — for example "use the mywhoosh-upload skill" — or
    when you are asked to put a workout onto MyWhoosh or Garmin Connect and want
    the procedure rather than improvising one.

    Omit `name` to list what is available. Pass a name to get that skill's full
    instructions, which you should then follow step by step.

    Why this exists: the same procedures ship as `.claude/skills` (which only
    Claude Code reads) and as MCP prompts (which a human must pick from a menu).
    Neither route lets a model retrieve a procedure it has just been asked for,
    which is what this tool is for.

    Both skills stop and ask before doing anything irreversible — a MyWhoosh
    export spends a finite slot credit — so follow them as written rather than
    summarising them.
    """
    skills = load_skills()
    if not skills:
        return _dump({"ok": False, "error": "no bundled skills found in this install"})

    if name is None:
        return _dump(
            {
                "ok": True,
                "skills": [{"name": s.name, "description": s.description} for s in skills],
                "hint": "Call get_skill(name=...) to get the full procedure.",
            }
        )

    wanted = name.strip().lower()
    for skill in skills:
        if skill.name.lower() == wanted:
            return _dump(
                {
                    "ok": True,
                    "name": skill.name,
                    "description": skill.description,
                    "instructions": skill.body,
                    "note": (
                        "Follow these steps as written. They stop for confirmation before any "
                        "irreversible or credit-spending action."
                    ),
                }
            )

    return _dump(
        {
            "ok": False,
            "error": f"no bundled skill named {name!r}",
            "available": [s.name for s in skills],
        }
    )


@app.tool()
def spec_schema() -> str:
    """Return the workout spec's JSON schema plus the notes needed to author one.

    Call this before writing a spec by hand if the format is not already known.
    """
    return _dump({"schema": SPEC_SCHEMA, "authoring_notes": _AUTHORING_NOTES})


def _register_skill_prompts() -> list[str]:
    """Offer each bundled skill as an MCP prompt.

    `.claude/skills` is a Claude Code mechanism, so this is how the same
    procedures reach Claude Desktop and other MCP clients — shipped with the
    server rather than uploaded per user. Unlike a real skill these are
    user-invoked: the client lists them and the human picks one.

    The prompt name matches the skill slug so it reads the same everywhere.
    """
    registered: list[str] = []
    for skill in load_skills():

        def make(bound: Skill):
            def run(session: str | None = None) -> str:
                return build_skill_message(bound, session)

            run.__doc__ = bound.description
            return run

        app.prompt(name=skill.name, description=skill.description)(make(skill))
        registered.append(skill.name)
    return registered


def main() -> None:
    _register_skill_prompts()
    app.run()


if __name__ == "__main__":
    main()

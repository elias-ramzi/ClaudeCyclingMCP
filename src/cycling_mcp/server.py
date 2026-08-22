"""MCP server: author a cycling workout once, render it for both platforms.

Two layers. The renderers and verifiers are pure and deterministic — a spec in,
a file or a comparison out, nothing stored. The coach layer above them keeps the
athlete's file in a local SQLite database: profile, dated FTP/weight/HR history,
objectives, imported activities, planned sessions, and the load arithmetic over
all of it.

Neither layer makes a network call or holds a credential. Activities reach this
server because the model fetched them from the Garmin MCP and passed them in;
uploads leave it the same way, through the bundled skills, with a human in the
loop — see `.claude/skills/garmin-upload`, `mywhoosh-upload` and `coaching`.

Filesystem access is limited to the coaching database and to explicit `out_path`
writes. `server_info` reports where that database is.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:  # mcp SDK 2.x
    from mcp.server import MCPServer as _Server
except ImportError:  # mcp SDK 1.x, where the same class is called FastMCP
    from mcp.server.fastmcp import FastMCP as _Server

from . import __version__, coach
from .coach import CoachError
from .garmin_import import GarminPayloadError
from .metrics import compute_metrics, describe
from .render_garmin import render_garmin as _render_garmin
from .render_zwo import render_zwo as _render_zwo
from .render_zwo import zwo_filename
from .skills import Skill, _skills_dir, build_skill_message, load_skills
from .spec import FTP_SOURCES, SpecError, load_spec
from .spec import validate_spec as _validate_spec
from .store import StoreError, db_status
from .verify import (
    compare_library_entry,
    compare_mywhoosh_import,
    compare_upload,
    diff_payloads,
    payload_digest,
    shape_problem,
    total_step_seconds,
    ui_checklist,
)


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
            "description": (
                "Half-width of the watt band placed around a scalar power target when "
                "rendering for Garmin, which needs ranges. It is a percentage OF THE "
                "RESOLVED TARGET, not of FTP: at 2.0, a 293 W target becomes 287-299 W "
                "(+/-5.9 W) while a 128 W target becomes 125-131 W (+/-2.6 W). Ignored "
                "where the spec already gives an explicit [low, high]. Omit it and the "
                "width is chosen by role: "
                "2% for interval and rest, 5% for recovery, warmup and cooldown — because "
                "2% of a 140 W easy spin is a 6 W window that alarms continuously, while "
                "2% of a 250 W interval is right. Setting it applies that one number "
                "everywhere."
            ),
        },
        "ftp_source": {
            "enum": list(FTP_SOURCES),
            "description": (
                "Optional. Where the FTP came from. A workout is a set of raw watts once "
                "it is on the head unit, and a .zwo stores only fractions, so neither "
                "file records which number produced it. Recorded in the describe_spec "
                "header and in the .zwo description."
            ),
        },
        "ftp_date": {
            "type": "string",
            "description": "Optional. When that FTP was established, e.g. '2026-08-19'.",
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
            # A repeat has no duration of its own — it is the sum of its
            # contents — so requiring one of every block told an author to add
            # a key the server then reported as a probable typo. Branch on the
            # type rather than requiring the union of both shapes.
            "required": ["type"],
            "allOf": [
                {
                    "if": {"properties": {"type": {"const": "repeat"}}},
                    "then": {
                        "required": ["count", "blocks"],
                        "not": {"required": ["duration"]},
                    },
                    "else": {"required": ["duration"]},
                }
            ],
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


def _write(out_path: str, content: str) -> tuple[str | None, str | None]:
    """Write to the *server's* filesystem, or explain why it could not.

    A caller running in a container naturally reaches for a container path, and
    the OS error for one is unhelpful: `/home/claude` on macOS fails with
    ENOTSUP ("Operation not supported") from the autofs mount, which says
    nothing about whose filesystem this is. So name the machine in the error —
    an agent that knows the platform and home directory can retry correctly on
    the first attempt instead of guessing.
    """
    try:
        path = Path(out_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return None, (
            f"Could not write {out_path!r}: {exc.strerror or exc}. out_path is "
            f"resolved on the machine running this MCP server (platform "
            f"{sys.platform}, home {Path.home()}), which may not be the "
            f"filesystem you are working in. Try a path under {Path.home()}."
        )
    return str(path), None


def _first_target_text(payload: dict) -> str | None:
    """The first watt range in a payload, for use as a worked example."""
    for line in ui_checklist(payload):
        _, _, tail = line.rpartition("· ")
        if tail.endswith(" W"):
            return tail
    return None


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
    way. Writing is the only filesystem access this server performs, and it
    happens on the machine running the server, which is not necessarily the
    filesystem the caller is working in — a failed write says which.

    "xml_js_literal" is the same XML as a ready-made JavaScript string literal.
    Use it rather than interpolating "xml" into a template literal: a backtick
    or a "${" in a workout name or message would otherwise break the script.

    A planned session stored by save_planned_workouts holds this same spec
    verbatim: pass its `spec` straight here, no translation. After a verified
    import, record it with update_planned_workout(status="pushed",
    pushed_to="mywhoosh") — otherwise nothing distinguishes a session that
    reached the trainer from one that was only written down.
    """
    try:
        workout = load_spec(spec)
    except SpecError as exc:
        return _dump({"ok": False, "errors": exc.errors})

    xml, warnings = _render_zwo(workout)
    result: dict[str, Any] = {
        "ok": True,
        "filename": zwo_filename(workout),
        "next_step": "Call get_skill('mywhoosh-upload') and follow it.",
        "xml": xml,
        # The MyWhoosh flow injects this XML into a page as JavaScript. A
        # backtick or a "${" in an athlete's workout name or message text
        # breaks a template literal and can inject, so ship a form that cannot:
        # a complete JS string literal, ready to drop in as-is.
        "xml_js_literal": json.dumps(xml),
        "warnings": workout.warnings + warnings,
        "summary": compute_metrics(workout).as_dict(),
    }
    if out_path:
        written, error = _write(out_path, xml)
        result["written_to"] = written
        if error:
            result["write_error"] = error
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
    against what was sent — call get_skill("garmin-upload") to read it.

    "payload_digest" is a digest of the payload as rendered. You will retype
    this payload into another tool's arguments, and a single wrong digit gives
    a workout that uploads cleanly and is wrong. Pass what you composed to
    check_garmin_payload with this digest BEFORE uploading.

    Pass out_path to also write the payload as JSON to disk. That path is
    resolved on the machine running this server; a failed write says so.

    A planned session stored by save_planned_workouts holds this same spec
    verbatim: pass its `spec` straight here, no translation. Check
    get_week for a `stale_ftp` flag first — a session written against an FTP
    that has since moved renders watts for the athlete they used to be. After a
    verified upload, record it with update_planned_workout(status="pushed",
    pushed_to="garmin").
    """
    try:
        workout = load_spec(spec)
    except SpecError as exc:
        return _dump({"ok": False, "errors": exc.errors})

    payload, warnings = _render_garmin(workout)
    result: dict[str, Any] = {
        "ok": True,
        "payload": payload,
        # Read at the moment of composing the upload call, which the docstring
        # may not be. A model that never found the skill improvises the upload
        # and skips verification entirely — reported 2026-08-19, where the
        # skill only surfaced on a second, lucky tool search.
        "next_step": (
            "Call get_skill('garmin-upload') and follow it. Before uploading, pass the "
            "payload you composed to check_garmin_payload with BOTH payload_digest and "
            "spec — the spec diff names the field that moved, the digest only says one "
            "did."
        ),
        "payload_digest": payload_digest(payload),
        # Returned here, not only from check_garmin_payload, because this is
        # the manual-verification fallback for when the Garmin read is
        # unavailable — and a fallback that requires another call to this
        # server is no fallback when this server is the thing that stopped
        # answering. Reported after two 4-minute timeouts, 2026-08-20.
        "ui_checklist": ui_checklist(payload),
        # The curated read drops targetValueUnit, so no round-trip can prove a
        # target was stored as watts rather than %FTP. That leaves a visual
        # check as the only evidence — which needs a concrete criterion, not
        # "open it once and see if it looks right".
        "expected_display": (
            "Every power target should read in watts on the head unit, e.g. "
            + (
                f"'{_first_target_text(payload)}'. "
                if _first_target_text(payload)
                else "'245-255 W'. "
            )
            + "If any reads as a percentage or as a zone number instead, targetValueUnit "
            "was added somewhere and the workout is scaled to the wrong athlete."
        ),
        "warnings": workout.warnings + warnings,
        "summary": compute_metrics(workout).as_dict(),
        # Travels with the payload so a client reading it later does not
        # "fix" the target type back into the silently-broken form.
        "schema_notes": GARMIN_SCHEMA_NOTES,
    }
    if out_path:
        written, error = _write(out_path, _dump(payload))
        result["written_to"] = written
        if error:
            result["write_error"] = error
        else:
            # A digest beside the file answers, in a later session with none of
            # this context, whether the JSON on disk is still the JSON that was
            # rendered. Without it a re-upload months later is an act of faith.
            sidecar, sidecar_error = _write(f"{out_path}.sha256", result["payload_digest"] + "\n")
            if sidecar:
                result["digest_written_to"] = sidecar
            else:
                result["write_error"] = sidecar_error
            checklist, _ = _write(f"{out_path}.checklist.txt", "\n".join(result["ui_checklist"]))
            if checklist:
                result["checklist_written_to"] = checklist
    return _dump(result)


@app.tool()
def check_garmin_payload(
    payload: dict, expected_digest: str | None = None, spec: dict | None = None
) -> str:
    """Check a payload you composed against the one the renderer produced.

    Call this BEFORE upload_workout, every time. render_garmin returns the
    payload as text into your context, and upload_workout takes an object you
    compose — so "hand it over unchanged" really means retyping ~90 lines of
    nested JSON. A single wrong digit in targetValueOne produces a workout that
    uploads without error, passes verify_garmin_upload (which compares Garmin
    against what you *sent*, not against what was rendered), and is wrong.

    Pass the payload you are about to upload and the "payload_digest" from
    render_garmin. A mismatch means what you composed is not what was rendered:
    re-copy it rather than hunting for the difference.

    Also returns ui_checklist — what each step should read in the Garmin UI.
    That is the fallback verification when get_workout_by_id is unavailable.

    Pass `spec` as well, or instead, to get a field-by-field diff: the spec is
    re-rendered here and compared against the payload you supply. That answers
    the question directly — was this payload altered after rendering? — and
    names the step and field, rather than only saying that something differs.

    Pure comparison, no network access.

    When the payload came from a stored planned session, this is also the step
    that catches a spec edited in the database but not re-rendered: pass the
    stored `spec` and the diff names the field.
    """
    actual = payload_digest(payload)
    result: dict[str, Any] = {
        "digest": actual,
        "step_seconds": total_step_seconds(payload),
        "ui_checklist": ui_checklist(payload),
    }

    if spec is not None:
        try:
            expected, _ = _render_garmin(load_spec(spec))
        except SpecError as exc:
            result["spec_errors"] = exc.errors
        else:
            differences = diff_payloads(expected, payload)
            result["matches_spec"] = not differences
            result["differences_from_spec"] = differences
            if differences:
                result["problem"] = (
                    "The payload you composed is not what this spec renders to. Do not "
                    "upload it — re-copy the payload from render_garmin."
                )
    if expected_digest is None:
        result["digest_checked"] = False
        if spec is None:
            # Neither reference given, so nothing was actually checked. Say so
            # rather than returning a bare digest that reads like a pass.
            result["warning"] = (
                "Nothing was compared: pass expected_digest (from render_garmin) or spec "
                "to check this payload against what the renderer produces."
            )
        return _dump(result)

    result["digest_checked"] = True
    result["matches_rendered"] = actual == expected_digest.strip()
    if not result["matches_rendered"]:
        result["problem"] = (
            f"This payload digests to {actual}, but render_garmin issued "
            f"{expected_digest.strip()}. What you composed is not what was "
            "rendered. Do not upload it — copy the payload from render_garmin again. "
            "(If the payload is right and the digest was mistyped, re-render and compare.)"
        )
    return _dump(result)


@app.tool()
def verify_garmin_upload(payload: dict, fetched: dict, expected_digest: str | None = None) -> str:
    """Check that Garmin stored the workout that was actually sent.

    Pass the payload given to upload_workout, and the response from
    get_workout_by_id for the workout it created. Returns the list of
    differences; an empty list means Garmin kept what was sent.

    This compares against the sent payload, not against an assumed read shape —
    the difference matters, because get_workout_by_id returns a lossy
    projection that will happily agree with a wrong-units payload. It also
    catches a repeat count silently corrupted by a missing conditionTypeId.

    Pass expected_digest (render_garmin's "payload_digest") to also check that
    what you sent is what was rendered. Without it this proves only that Garmin
    kept what it was given — which a payload mistyped on its way out of your
    context would also pass.

    Two things it cannot prove: whether a power target was stored as watts or
    as %FTP (the curated read drops the targetValueUnit field that distinguishes
    them), and whether the workout displays correctly on a head unit. Confirm
    those by opening the workout in Garmin Connect once.

    Pure comparison, no network access.
    """
    # Refuse a misused comparison rather than diffing two different shapes.
    # Failing open here is worse than not checking at all: the skill's response
    # to a mismatch is to delete the workout.
    misuse = shape_problem(payload, fetched)
    if misuse:
        return _dump({"ok": False, "error": "shape_mismatch", "detail": misuse})

    problems = compare_upload(payload, fetched)
    result: dict[str, Any] = {
        "ok": True,
        "match": not problems,
        "differences": problems,
        "sent_step_seconds": total_step_seconds(payload),
        "note": (
            "Garmin's estimated_duration_seconds follows its own rules and can "
            "disagree with the sum of the steps; it is not used for this check."
        ),
    }
    if expected_digest is not None:
        # This check is upstream of the round-trip: it asks whether the payload
        # that was sent is the payload that was rendered. A match here plus a
        # match above is the only combination that means end-to-end correct.
        result["matches_rendered"] = payload_digest(payload) == expected_digest.strip()
        if not result["matches_rendered"]:
            result["match"] = False
            result["differences"] = [
                *result["differences"],
                "The payload sent is not the payload render_garmin produced — it was "
                "altered or mistyped between rendering and upload. Garmin storing it "
                "faithfully does not make it right.",
            ]
    return _dump(result)


@app.tool()
def verify_mywhoosh_import(
    spec: dict,
    workout_time: str,
    training_load: float | str | None = None,
    before_workout_time: str | None = None,
    before_training_load: float | str | None = None,
) -> str:
    """Check a MyWhoosh builder header against the session that was rendered.

    Call this after importing a .zwo and before clicking EXPORT TO MYWHOOSH.
    MyWhoosh has no API to read a workout back, so its header is the only
    evidence the import took — and export spends an irreversible slot credit,
    which is too much to hang on eyeballing two numbers.

    Pass the header's `Workout Time` (as shown, e.g. "68:00") and
    `Training Load`. Pass the pre-import values as before_* too: they are what
    distinguishes a real import from a silent no-op, because "Create New"
    routinely opens an editor already holding a previous workout. Without them
    a header that matches the session might just be what was already loaded.

    Returns safe_to_export plus the individual checks. Pure comparison, no
    network and no browser access — it compares numbers you scraped.
    """
    try:
        workout = load_spec(spec)
    except SpecError as exc:
        return _dump({"ok": False, "errors": exc.errors})

    metrics = compute_metrics(workout)
    before = None
    if before_workout_time is not None or before_training_load is not None:
        before = {"workout_time": before_workout_time, "training_load": before_training_load}

    result = compare_mywhoosh_import(
        expected_seconds=metrics.total_seconds,
        expected_tss=metrics.tss,
        workout_time=workout_time,
        training_load=training_load,
        before=before,
    )
    result["expected"] = {
        "duration": metrics.as_dict()["total_duration"],
        "tss": round(metrics.tss, 1),
        "intensity_factor": round(metrics.intensity_factor, 3),
    }
    return _dump(result)


@app.tool()
def verify_mywhoosh_library_entry(
    spec: dict,
    name: str,
    duration: str,
    tss: float | str | None = None,
    intensity_factor: float | str | None = None,
) -> str:
    """Check a MyWhoosh library card against the session that was exported.

    Call this after EXPORT TO MYWHOOSH, with what the card in My Workouts
    shows. The credit is already spent by then, so this prevents nothing — it
    establishes whether the spend produced the right workout, which is the
    question the redirect alone cannot answer.

    Accepts the card's own formats: "1h 18m" as readily as "78:00". Names are
    compared with multiplication signs folded, because MyWhoosh renders an
    uploaded "Tempo-3x14" as "Tempo-3×14" on the card while the editor header
    shows the ASCII form.

    Pure comparison, no network and no browser access.
    """
    try:
        workout = load_spec(spec)
    except SpecError as exc:
        return _dump({"ok": False, "errors": exc.errors})

    metrics = compute_metrics(workout)
    result = compare_library_entry(
        expected_name=zwo_filename(workout).removesuffix(".zwo"),
        expected_seconds=metrics.total_seconds,
        expected_tss=metrics.tss,
        expected_if=metrics.intensity_factor,
        name=name,
        duration=duration,
        tss=tss,
        intensity_factor=intensity_factor,
    )
    result["expected"] = {
        "name": zwo_filename(workout).removesuffix(".zwo"),
        "duration": metrics.as_dict()["total_duration"],
        "tss": round(metrics.tss, 1),
        "intensity_factor": round(metrics.intensity_factor, 3),
    }
    return _dump(result)


@app.tool()
def server_info() -> str:
    """Identify this server: version, where it is loaded from, what it serves.

    Call it when reporting a problem with this server, or when you want to know
    whether the build you are talking to is the one you expect. A tool surface
    is a build fingerprint — a tool that exists in one release and not another
    dates a session precisely — but only if you can see it alongside a version.

    It answers instantly, so a reply is proof the server is alive and a
    non-reply is not about this server being slow: no tool here has ever taken
    more than a few milliseconds. It is very nearly side-effect free — it stats
    the coaching database and, if one is there, opens it read-only to read the
    schema version. It never creates it.

    "database" reports the coaching store: its path, whether it exists yet, and
    its schema version. Reading it does not create it — a tool whose job is to
    describe the world must not change it, or "exists: true" would only ever
    mean "you asked". A null schema_version on a file that exists means the
    file is there but unreadable, which is worth investigating before writing
    to it.
    """
    skills = load_skills()
    return _dump(
        {
            "name": "claude-cycling-mcp",
            "version": __version__,
            "package_path": str(Path(__file__).resolve().parent),
            "python": sys.version.split()[0],
            "skills": [skill.name for skill in skills],
            "skills_dir": str(_skills_dir()),
            "uploads": False,
            "database": db_status(),
            "note": (
                "No network, no credentials, no uploads; filesystem access is limited to "
                "this server's own database and to explicit out_path writes."
            ),
        }
    )


@app.tool()
def get_skill(name: str | None = None) -> str:
    """Fetch a bundled procedure by name: the two upload flows, or coaching.

    Read `garmin-upload` or `mywhoosh-upload` before uploading, scheduling or
    exporting a rendered cycling workout — a .zwo to MyWhoosh, or a Garmin
    Connect payload to a watch or head unit. They cover FTP sourcing, the
    upload call, verifying the stored result, and the traps that fail silently.

    Read `coaching` when the athlete is talking about their training rather
    than about one workout file: what to do this week, a session they missed, a
    race they are building toward. It covers the onboarding interview, the
    weekly loop, and the adaptation rules.

    Call this whenever you are asked to follow, use, or run one of this
    server's skills by name — for example "use the mywhoosh-upload skill" — or
    when you are asked to put a workout onto MyWhoosh or Garmin Connect and want
    the procedure rather than improvising one. If you have rendered a workout
    and are about to send it somewhere, you want this first.

    Omit `name` to list what is available. Pass a name to get that skill's full
    instructions, which you should then follow step by step.

    Why this exists: the same procedures ship as `.claude/skills` (which only
    Claude Code reads) and as MCP prompts (which a human must pick from a menu).
    Neither route lets a model retrieve a procedure it has just been asked for,
    which is what this tool is for.

    The upload skills stop and ask before doing anything irreversible — a
    MyWhoosh export spends a finite slot credit — and `coaching` proposes a
    week rather than pushing it. Follow them as written rather than summarising
    them.
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


# --------------------------------------------------------------------------
# the coach layer
#
# These read and write the athlete's local database. Everything above this
# point is pure; everything below stores state. Still no network and no
# credentials — Claude fetches from the Garmin MCP and passes the result here.
# --------------------------------------------------------------------------


def _coach(function, **kwargs) -> str:
    """Run a coach operation, turning a refusal into a readable result.

    A refusal here is an answer, not a crash: "that FTP is outside the range I
    will store" is something the caller acts on, and an exception thrown across
    the tool boundary arrives as an error string with no structure.
    """
    try:
        result = function(**kwargs)
    except json.JSONDecodeError as exc:
        # A ValueError subclass, so the clause below would render corrupted
        # stored JSON — a spec_json this server wrote and can no longer read —
        # as an ordinary refusal. It is not one: nothing the caller passed is
        # wrong, and the database needs looking at.
        return _dump(
            {
                "ok": False,
                "error": f"stored JSON in the coaching database could not be parsed: {exc}",
                "database": db_status(),
                "hint": "Restore from an export_data backup, or repair the row by hand.",
            }
        )
    except (CoachError, StoreError, GarminPayloadError, ValueError) as exc:
        return _dump({"ok": False, "error": str(exc)})
    except sqlite3.Error as exc:
        # A database that cannot be read is the caller's problem to act on —
        # usually a CLAUDE_CYCLING_DB pointing somewhere unexpected. Thrown
        # across the tool boundary it arrives as an unstructured error with no
        # indication of which database it was talking about.
        return _dump(
            {
                "ok": False,
                "error": f"the coaching database could not be used: {exc}",
                "database": db_status(),
            }
        )
    if "ok" in result:
        # Every refusal is raised, so nothing should be setting this itself. A
        # function that returned {"ok": False} used to work only because the
        # spread below happened to come last — reorder that line and a failure
        # is reported as a success.
        raise RuntimeError(f"{function.__name__} returned its own 'ok' key; it should raise")
    return _dump({"ok": True, **result})


@app.tool()
def get_profile() -> str:
    """Read the athlete's file: who they are, current FTP/weight/HR, and what is missing.

    Call this first, every time, before writing a plan or a target. It answers
    the two questions everything downstream depends on — what is the FTP, and
    when was it set — and it is cheap.

    The `gaps` list names every field with nothing on file, each with what it
    is needed for. On a fresh database that list is the whole profile, which is
    the signal to start an onboarding conversation rather than to report an
    error. Ask about the gaps a few at a time, in the athlete's own terms, and
    store answers as they arrive with update_profile / log_ftp / log_weight /
    log_hr. Do not read the list out as a form.

    Creates the database on first call. See server_info for where it lives.
    """
    return _coach(coach.get_profile)


@app.tool()
def update_profile(
    display_name: str | None = None,
    height_cm: float | None = None,
    birth_year: int | None = None,
    availability: str | None = None,
    equipment: str | None = None,
    constraints: str | None = None,
) -> str:
    """Set athlete fields. Anything omitted is left as it was.

    The three free-text fields carry most of the weight, so record what was
    actually said rather than a tidied summary:

    - `availability` — sessions per week, which days, which day the long ride
      can go on, how long an evening session can run.
    - `equipment` — trainer and which app, whether there is a power meter
      outdoors, HR strap or wrist. Whether power exists outdoors decides
      whether outdoor sessions can carry watt targets at all.
    - `constraints` — injuries, travel, shift work, anything the plan has to
      route around.

    Returns the stored row. FTP, weight and HR are not here: they are dated
    history, not profile fields — use log_ftp / log_weight / log_hr.
    """
    return _coach(
        coach.update_profile,
        display_name=display_name,
        height_cm=height_cm,
        birth_year=birth_year,
        availability=availability,
        equipment=equipment,
        constraints=constraints,
    )


@app.tool()
def log_ftp(
    value_watts: float | None = None,
    twenty_min_watts: float | None = None,
    effective_date: str | None = None,
    method: str | None = None,
    note: str | None = None,
) -> str:
    """Record a dated FTP. Give the FTP itself, or a 20-minute test to convert.

    Pass `twenty_min_watts` and the server applies the 0.95 convention and
    records the method and the arithmetic, so nobody later has to guess whether
    a stored number was already scaled. Pass `value_watts` for an FTP the
    athlete states, a ramp-test result, or a Garmin profile figure — and set
    `method` accordingly ("stated", "ramp_test", "garmin_profile", ...), because
    those are not equally trustworthy and only the method records which is which.

    **FTP is dated on purpose.** Every training-load number for a ride is
    computed against the FTP in effect *on that ride's date*, not today's. A
    single overwritten value silently rescales the athlete's whole history: the
    same watts against a bigger FTP is a smaller IF, so a block of training
    shrinks the moment they test better.

    `effective_date` defaults to today. When logging a test, date it to the test
    — not to when it was mentioned — or every ride in between is scored wrong.

    Returns the stored row and the new power zones. When the value has changed,
    it also says so: any planned session written in watts against the old FTP
    now prescribes a different percentage and must be re-rendered before it is
    pushed.
    """
    return _coach(
        coach.log_ftp,
        value_watts=value_watts,
        twenty_min_watts=twenty_min_watts,
        effective_date=effective_date,
        method=method,
        note=note,
    )


@app.tool()
def log_weight(value_kg: float, effective_date: str | None = None, note: str | None = None) -> str:
    """Record a dated weight in kilograms. Returns W/kg against the FTP of that date.

    Kilograms, always, and convert before calling rather than after. The range
    check **cannot** catch a pounds figure: 160 lb is 73 kg, and 160 kg is a
    real weight for someone, so both readings are inside any range wide enough
    to be usable. A heavy value comes back with a query attached; everything
    below it is taken at face value, and every W/kg from a pounds figure is out
    by a factor of 2.2.
    """
    return _coach(coach.log_weight, value_kg=value_kg, effective_date=effective_date, note=note)


@app.tool()
def log_hr(
    threshold_hr: int | None = None,
    max_hr: int | None = None,
    resting_hr: int | None = None,
    effective_date: str | None = None,
    method: str | None = None,
    note: str | None = None,
) -> str:
    """Record dated heart-rate figures: threshold, maximum, resting. Any subset.

    Threshold HR (LTHR) is the one that matters — every HR zone is a fraction
    of it, and it is what a no-power ride's training load is computed against.
    With only a maximum on file the server estimates threshold at 92% of it and
    says so; that estimate is wide enough that a measured figure is worth
    asking for.

    Do not pass an age-predicted maximum (220 minus age) as `max_hr` without
    saying so in `method`. It is wrong by ten beats either way for most people,
    and it propagates into every zone boundary.
    """
    return _coach(
        coach.log_hr,
        threshold_hr=threshold_hr,
        max_hr=max_hr,
        resting_hr=resting_hr,
        effective_date=effective_date,
        method=method,
        note=note,
    )


@app.tool()
def get_zones(as_of: str | None = None) -> str:
    """Power and HR zones from the figures in effect on a date. Defaults to today.

    Six power zones on the classic %FTP boundaries (55/75/90/105/120), plus
    sweet spot quoted separately at 88-94% because it straddles two of them.
    HR zones are the Friel table as fractions of threshold HR — they are not
    the power zones in another unit, and they do not line up effort for effort,
    because heart rate lags.

    Pass `as_of` with a ride's date to see the zones that ride was actually
    performed against. After an FTP change those are not today's zones, and
    reading an old ride against today's table makes it look easier than it was.

    Call this before quoting any target in watts, and again after any log_ftp.
    """
    return _coach(coach.get_zones, as_of=as_of)


@app.tool()
def add_event(
    name: str,
    event_date: str,
    distance_km: float | None = None,
    elevation_m: float | None = None,
    priority: str | None = None,
    status: str | None = None,
    note: str | None = None,
) -> str:
    """Record a race or objective — the thing the training is for.

    An event is not a planned workout: a workout fills a week, an event is what
    the weeks point at. The next priority-A event is the anchor a plan is built
    backwards from, so store it before writing any block of training.

    `priority` is A (the objective), B (raced, but trained through) or C (a hard
    day out). Distance and elevation are what make a session event-specific —
    2400 m of climbing over 148 km is a different event from a flat 148 km, and
    the plan should differ.

    Record **past** races too. Their debriefs, stored by record_race_result, are
    the most specific information available when planning for the same event
    again.
    """
    return _coach(
        coach.add_event,
        name=name,
        event_date=event_date,
        distance_km=distance_km,
        elevation_m=elevation_m,
        priority=priority,
        status=status,
        note=note,
    )


@app.tool()
def update_event(
    event_id: int,
    name: str | None = None,
    event_date: str | None = None,
    distance_km: float | None = None,
    elevation_m: float | None = None,
    priority: str | None = None,
    status: str | None = None,
    note: str | None = None,
) -> str:
    """Change an event's details: a moved date, a corrected profile, a dropped priority.

    Statuses are `upcoming`, `completed`, `abandoned` (started, did not finish)
    and `dns` (did not start). Use record_race_result rather than this to close
    out a race that was ridden — it links the activity and stores the debrief,
    which this deliberately will not touch.
    """
    return _coach(
        coach.update_event,
        event_id=event_id,
        name=name,
        event_date=event_date,
        distance_km=distance_km,
        elevation_m=elevation_m,
        priority=priority,
        status=status,
        note=note,
    )


@app.tool()
def list_events(when: str = "all", status: str | None = None, today: str | None = None) -> str:
    """Every stored objective, with the next A-event and how far away it is.

    `when` is "all", "upcoming" or "past". `next_a_event` and
    `weeks_to_next_a_event` are the periodisation anchor: how many weeks remain
    decides whether this is base, build or taper, and a null there means there
    is nothing to build toward — ask.

    Past events carry their `debrief`. Read those before planning for the same
    race again; an athlete's own account of where they cracked last year beats
    any general principle about pacing.

    `today` defaults to the server's date; pass the athlete's when they differ.
    """
    return _coach(coach.list_events, when=when, status=status, today=today)


@app.tool()
def record_race_result(
    event_id: int,
    activity_id: int | None = None,
    # str | int, because Garmin's activityId is a JSON number and pydantic does
    # not coerce one to a string: a str-only schema rejected the call before any
    # code here ran. Same for finish_time, whose docstring promises seconds.
    garmin_activity_id: str | int | None = None,
    finish_time: str | float | None = None,
    debrief: str | None = None,
    status: str | None = None,
    force: bool = False,
) -> str:
    """Close out a race: link the ride, store the time, write the debrief.

    Refuses to link an activity whose date is not the event's date unless
    `force` — the realistic mistake is linking the Sunday spin after a Saturday
    race, which then makes the A-event look like an easy hour.

    Linking changes what get_week reports: a race-day ride tied to an event
    stops being flagged as unplanned training. It still counts in full toward
    load and CTL — the athlete's body did not know it was a race.

    Omit `status` and an event still marked `upcoming` becomes `completed`,
    while one already marked `abandoned` or `dns` **keeps that** — adding a
    debrief months later must not quietly rewrite a race the athlete did not
    finish into one they did. Pass `status` explicitly to change it.

    `finish_time` takes "4:32:10" or a number of seconds. The **debrief is the
    point**: what the pacing was, what was eaten and when, what went wrong.
    Write it from the ride data plus what the athlete says, in their terms, and
    store it while it is fresh. A year later it is the only part of this record
    that still teaches anything.
    """
    return _coach(
        coach.record_race_result,
        event_id=event_id,
        activity_id=activity_id,
        garmin_activity_id=garmin_activity_id,
        finish_time=finish_time,
        debrief=debrief,
        status=status,
        force=force,
    )


@app.tool()
def import_activities(payload: list | dict | str) -> str:
    """Store Garmin activities. Pass the Garmin MCP's output UNCHANGED.

    This server has no network access; you are the transport. Call the Garmin
    MCP's `get_activities` (or `get_activity` for one ride in detail) and hand
    the result straight to this tool. It accepts a bare list, a single activity
    object, a wrapper dict, and the nested `summaryDTO` shape — the field
    mapping happens here.

    **Do not transcribe the numbers into a tidier shape first.** Every retyped
    digit is a chance to turn a 198 W normalised power into 189, and the result
    is a training load that is wrong and looks entirely reasonable. Unknown keys
    are kept, not rejected; Garmin adds fields without warning.

    Idempotent on `activityId`: re-importing the same payload reports
    `unchanged`, so syncing an overlapping window every week costs nothing. A
    stored value is never overwritten with a null, because `get_activities`
    returns a thinner summary than `get_activity` — without that rule,
    re-syncing the list after fetching one ride in detail would blank its
    normalised power while leaving the load number that came from it.

    Returns inserted / updated / unchanged / rejected, with a reason per
    rejection, and flags worth reading:

    - `local_date_from_utc` — no local start time, so the ride's plan date came
      from UTC and may be a day out for an early-morning or late-evening ride.
    - `no_power` / `no_normalized_power` — training load will fall back to
      average power or to heart rate. compute_load says which, per ride.

    An indoor ride arrives as `virtual_ride` or `indoor_cycling`, not
    `cycling`; both are stored with `sport: "cycling"` and the raw key in
    `sub_sport`, so filtering never silently drops a winter's training.
    """
    return _coach(coach.import_activities, payload=payload)


@app.tool()
def import_activity_laps(
    payload: list | dict | str,
    activity_id: int | None = None,
    garmin_activity_id: str | int | None = None,
) -> str:
    """Store one activity's splits, in execution order. Pass get_activity_splits output.

    Laps are what let compliance_report say "the second block fell to 228 W"
    instead of "the ride averaged 210 W". A ride summary cannot tell an interval
    session ridden correctly from the same session ridden as one long tempo;
    the laps can.

    Use the Garmin MCP's `get_activity_splits`. `get_activity_split_summaries`
    is refused: it aggregates by split *type* (climb, descent), not by lap, so
    its rows do not line up with a plan's blocks and comparing against them
    would produce confident statements about the wrong thing.

    Re-importing replaces the stored laps rather than doubling them. Warns when
    the laps do not sum to the activity's duration, which usually means they
    belong to a different ride.
    """
    return _coach(
        coach.import_activity_laps,
        payload=payload,
        activity_id=activity_id,
        garmin_activity_id=garmin_activity_id,
    )


@app.tool()
def annotate_activity(
    activity_id: int | None = None,
    garmin_activity_id: str | int | None = None,
    rpe: int | None = None,
    feel: str | None = None,
    note: str | None = None,
) -> str:
    """Attach the subjective read to a ride: RPE 1-10, how it felt, free text.

    Store this whenever the athlete says anything about how a session went —
    "legs were empty", "easiest 3x10 I've done", "cooked by the third one". It
    is the half of a session no device records and the half that decides the
    next week: two rides with identical power files, one of which felt
    catastrophic, call for different plans.

    Particularly valuable when no HRV or readiness data exists. Sensations are
    then the only fatigue signal there is, and an unrecorded one is gone by the
    next conversation.
    """
    return _coach(
        coach.annotate_activity,
        activity_id=activity_id,
        garmin_activity_id=garmin_activity_id,
        rpe=rpe,
        feel=feel,
        note=note,
    )


@app.tool()
def list_activities(
    start: str | None = None,
    end: str | None = None,
    sport: str | None = None,
    include_load: bool = True,
    limit: int = 200,
) -> str:
    """Stored activities in a date range, newest first, with computed training load.

    Dates filter on the ride's **local** date — the day the athlete believes
    they trained — not the UTC date.

    `sport` filters on the family: "cycling" catches virtual_ride,
    indoor_cycling, gravel_cycling and the rest, which is the point. The
    device's own key is in `sub_sport` on every row.

    This reads only what has been imported. It is not a view of Garmin: a ride
    that was never passed to import_activities does not exist here, and its
    absence looks exactly like a rest day.
    """
    return _coach(
        coach.list_activities,
        start=start,
        end=end,
        sport=sport,
        include_load=include_load,
        limit=limit,
    )


@app.tool()
def link_activity(
    planned_workout_id: int,
    activity_id: int | None = None,
    garmin_activity_id: str | int | None = None,
    auto: bool = False,
) -> str:
    """Attach the ride that happened to the session that was planned.

    Pass an activity, or set `auto` to have the server propose one: same date,
    cycling, ranked by closeness of duration to the plan. **`auto` links only
    when there is exactly one candidate.** With two rides on one day it returns
    both and links nothing, because an automatic match that picks the wrong one
    produces a compliance report that is confidently about the wrong session,
    and nothing downstream would ever reveal it.

    Sets the planned session's status to `completed`. Then call
    compliance_report.
    """
    return _coach(
        coach.link_activity,
        planned_workout_id=planned_workout_id,
        activity_id=activity_id,
        garmin_activity_id=garmin_activity_id,
        auto=auto,
    )


@app.tool()
def save_planned_workouts(workouts: list[dict]) -> str:
    """Store planned sessions, one item per session.

    Each item is {"spec": ..., "scheduled_date": "YYYY-MM-DD", "note": ...}.

    The spec is exactly the document render_garmin and render_zwo consume — call
    spec_schema if the format is not already familiar. It is stored verbatim, so
    a stored session is directly renderable later with no translation step.

    Every spec is validated first. An invalid one is refused rather than stored:
    a plan that cannot be rendered is not a plan, and the failure would
    otherwise surface on the morning it was meant to be ridden. Valid items in
    the same call are still stored, so one bad session does not lose a week.
    Warnings are stored alongside rather than blocking.

    Write the spec against the **current** FTP — check get_profile first. The
    spec carries its own `ftp` field, and get_week flags a stored session whose
    FTP no longer matches, but the flag arrives after the fact.

    Storing is not pushing. Nothing reaches Garmin or MyWhoosh until you render
    it and follow the upload skill, with the athlete's explicit agreement.
    """
    return _coach(coach.save_planned_workouts, workouts=workouts)


@app.tool()
def update_planned_workout(
    planned_workout_id: int,
    status: str | None = None,
    scheduled_date: str | None = None,
    pushed_to: str | None = None,
    note: str | None = None,
    spec: dict | None = None,
    linked_activity_id: int | None = None,
) -> str:
    """Change a planned session: status, date, push target, note, or the spec itself.

    Statuses: `planned` (written, nowhere yet), `pushed` (uploaded and verified
    — set this with `pushed_to` immediately after a successful upload),
    `completed` (ridden; normally set by link_activity), `missed` (the athlete
    could not do it), `skipped` (the coach withdrew it).

    Missed and skipped are not bookkeeping synonyms. One is a plan reality broke,
    the other a plan you changed; a week of "missed" is a plan that does not fit
    the athlete's life, and that is the thing to fix.

    Replacing `spec` re-validates it and refuses an invalid one, exactly as
    save_planned_workouts does.
    """
    return _coach(
        coach.update_planned_workout,
        planned_workout_id=planned_workout_id,
        status=status,
        scheduled_date=scheduled_date,
        pushed_to=pushed_to,
        note=note,
        spec=spec,
        linked_activity_id=linked_activity_id,
    )


@app.tool()
def get_week(start: str, end: str, today: str | None = None) -> str:
    """Plan against reality for a date range, and every place they diverge.

    Returns the planned sessions — each with the block table describe_spec
    produces — every stored activity with its computed load, any events in the
    window, and two lists of deviation:

    - `planned_not_ridden` — a session whose date has passed with no activity
      linked and no status explaining it.
    - `ridden_not_planned` — an activity tied to no planned session. A race-day
      ride linked to an event is **not** listed here; it was training the plan
      knew about, and it still counts fully toward load.

    Read this before writing the next week, and before asking the athlete
    anything: the answer to "did you ride Tuesday?" is already here, and asking
    a question the data answers wastes their time and yours.

    A planned session written against an FTP that has since changed is flagged
    `stale_ftp`. Rendering it unedited prescribes the old intensity.

    Deviations are facts, not verdicts. An unplanned three-hour ride with
    friends is training that happened; what it changes about the coming week is
    a judgement, and it belongs in the plan, not in a complaint.
    """
    return _coach(coach.get_week, start=start, end=end, today=today)


@app.tool()
def compute_load(
    start: str | None = None,
    end: str | None = None,
    activity_ids: list[int] | None = None,
    sport: str | None = None,
) -> str:
    """Training load per activity, each scored against the figures of its own date.

    With power: TSS = duration_h x IF^2 x 100, IF = NP / FTP, using the FTP in
    effect **on the ride's date**. Where a ride has no normalised power, average
    power is used and the row is flagged — NP is never below average, so that
    number understates a variable ride.

    Without power, the fallback is hrTSS: the same formula with
    (average HR / threshold HR) replacing the power ratio. **Do not compare the
    two.** hrTSS cannot see variability, so thirty sprints and a steady tempo
    ride at the same average HR score identically; cardiac drift inflates long
    rides; and it inherits every error in a threshold HR that is often itself
    estimated from max HR. `method` on each row says which produced it, and
    `by_method` counts them — a week's total that mixes both means less than it
    looks, and the response says so.

    A ride with neither power nor HR returns a null TSS and a reason, never a
    zero: a zero is indistinguishable from a rest day, and it would drag CTL
    down as if the athlete had not ridden.
    """
    return _coach(coach.compute_load, start=start, end=end, activity_ids=activity_ids, sport=sport)


@app.tool()
def get_form(start: str, end: str, seed_ctl: float = 0.0, seed_atl: float = 0.0) -> str:
    """CTL / ATL / TSB day by day from the stored activities.

    The standard exponentially weighted model, stepped every calendar day
    including rest days:

        CTL(d) = CTL(d-1) + (TSS(d) - CTL(d-1)) / 42      fitness
        ATL(d) = ATL(d-1) + (TSS(d) - ATL(d-1)) / 7       fatigue
        TSB(d) = CTL(d-1) - ATL(d-1)                      form

    TSB here is **yesterday's** balance — the form carried into a day, before
    that day's session lands on it. Some tools report same-day CTL - ATL; the
    two differ by roughly the size of a session, which is the difference
    between reading a hard Tuesday as fresh and reading it as buried.

    The walk starts at the earliest stored activity so CTL entering the window
    is built from real history. When that run-up is under 42 days the numbers
    are still climbing out of zero and `warmup_incomplete` says so — an athlete
    whose first import is three weeks old has a CTL that describes the import
    date, not them.

    **Cross-check against the Garmin MCP's `get_training_load_trend`.** Garmin
    computes from everything it holds, this from what was imported, and it uses
    its own load metric rather than TSS. A disagreement is information — usually
    a gap in what was imported, or rides scored by heart rate here. Find the
    cause. Do not average two numbers when only one of them can be explained.
    """
    return _coach(coach.get_form, start=start, end=end, seed_ctl=seed_ctl, seed_atl=seed_atl)


@app.tool()
def compliance_report(planned_workout_id: int, activity_id: int | None = None) -> str:
    """What was prescribed against what was ridden, block by block where the laps allow.

    Uses the activity linked to the session unless `activity_id` overrides it.
    When laps are stored and their count matches the plan's executable blocks —
    repeats expanded, because three intervals are three laps — each block is
    compared to its lap and written out as a sentence you can use directly:
    "the second block fell to 228 W against a 250 W target".

    When the counts differ, **no pairing is invented**. Six laps against nine
    blocks aligned by position produces confident claims about the wrong
    intervals, and lap counts rarely match a plan exactly — an athlete pressing
    lap at a junction, or a head unit auto-lapping every 5 km, breaks it. The
    laps come back as they are, with the totals compared and the mismatch
    stated.

    With no laps at all, only duration and average intensity can be compared.
    That separates a session that was cut short from one that was not; it cannot
    separate an interval session ridden properly from the same average ridden
    as steady tempo. Say which of the two you are looking at.

    A block within 5% of its target counts as on target. That is deliberately
    not the band `render_garmin` shows on the head unit (2% for intervals, 5%
    easy): the rendered band is what the athlete was told to hold, this is how
    far off an average has to be before it is worth mentioning.

    A recovery, warmup or cooldown ridden *below* target is `easier_than_target`
    and not a miss —
    that target is a ceiling. But a block cut short **is** a deviation even
    when the watts were right: `off_target_blocks` counts wrong power,
    `deviating_blocks` adds short and long, and `verdict` follows the latter.
    `unverifiable_blocks` is blocks that recorded no power; they count toward
    neither, because a lap with no watts says nothing about whether the target
    was held. When every block is unverifiable the verdict is `unverifiable` —
    an HR-only ride is not evidence the session was ridden correctly.

    Read `sentences` first: it is the report in order, already phrased.
    """
    return _coach(
        coach.compliance_report, planned_workout_id=planned_workout_id, activity_id=activity_id
    )


@app.tool()
def export_data(athlete_id: int | None = None) -> str:
    """Dump the whole coaching database as JSON, with a digest of the content.

    Every row of every table, `raw_json` included, so a restore loses nothing.
    Keep the `digest` with the file: it is the only way to tell a complete copy
    from a truncated one, and import_data will check it.

    Worth doing before anything destructive, and before a schema upgrade.
    """
    return _coach(coach.export_data, athlete_id=athlete_id)


@app.tool()
def import_data(data: dict, force: bool = False, expected_digest: str | None = None) -> str:
    """Restore the database from export_data output. Refuses to overwrite by default.

    A non-empty database is left untouched unless `force` is set — and `force`
    **deletes every existing row** before inserting. This is a restore, not a
    merge. Merging two training logs is not something to do implicitly: the same
    ride under two Garmin ids, or two FTP entries for one date, would change
    every number computed afterwards without any of it being visible.

    Pass `expected_digest` from the export. A mismatch refuses the restore
    rather than applying a payload that was truncated or edited in transit —
    which matters more here than anywhere else in this server, because after an
    overwrite there is nothing left to compare against.

    Export from a newer schema than this build knows is refused outright.
    """
    return _coach(coach.import_data, data=data, force=force, expected_digest=expected_digest)


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

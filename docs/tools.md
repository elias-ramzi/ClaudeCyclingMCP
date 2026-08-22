# Tool reference

What the server exposes, and the format details each renderer is pinned to.


| Tool | Does |
|---|---|
| `validate_spec` | Errors and warnings, plus a summary when valid. |
| `describe_spec` | The block table shown in the [spec format](spec-format.md) — computed watts, durations, IF, TSS. |
| `render_zwo` | `.zwo` XML plus the filename to upload it under. Optional `out_path`. |
| `render_garmin` | A payload ready for the Garmin MCP's `upload_workout`. Optional `out_path`. |
| `check_garmin_payload` | Checks the payload you composed against the digest the renderer issued, before uploading. Also returns a UI checklist for verifying by eye. Pure. |
| `verify_garmin_upload` | Compares a sent payload against what `get_workout_by_id` returns. Pure. |
| `verify_mywhoosh_import` | Compares a scraped MyWhoosh builder header against the rendered session, including the pre-import snapshot that catches a silent no-op. Pure. |
| `verify_mywhoosh_library_entry` | Checks a MyWhoosh library card against the exported session, in the card's own formats. The credit is already spent, so this says what it bought. Pure. |
| `server_info` | Version, package path, skills served, and the coaching database's path and schema version. A tool surface dates a session; this makes the build sayable in one call. Reports the database without creating it. |
| `get_skill` | Fetch a bundled upload procedure by name, so a model asked to "use the mywhoosh-upload skill" can retrieve it. |
| `spec_schema` | The spec's JSON schema and authoring notes. |

Those are pure: nothing is stored, and the same inputs always give the same answer. The tools below
read and write the athlete's local database — see [the coach layer](coaching.md) for where it lives
and what it holds.

| Tool | Does |
|---|---|
| `get_profile` | Athlete, current FTP/weight/HR, and a `gaps` list naming everything still unknown. The onboarding agenda. |
| `update_profile` | Partial update of the athlete fields: availability, equipment, constraints. |
| `log_ftp` | Append a dated FTP, or a 20-minute test converted at 0.95 with the method recorded. |
| `log_weight` · `log_hr` | Append a dated weight, or threshold / max / resting heart rate. |
| `get_zones` | Power and HR zones from the figures in effect on a date — not necessarily today's. |
| `add_event` · `update_event` · `list_events` | Objectives past and future; `list_events` returns the next A-event and the weeks to it. |
| `record_race_result` | Link the race-day ride, store the finish time and the debrief. Refuses a date mismatch unless forced. |
| `import_activities` | Raw Garmin MCP output in, idempotent on `activityId`. |
| `import_activity_laps` | One ride's splits, in execution order. `get_activity_splits`, not the summaries. |
| `annotate_activity` | RPE, feel, free text — the half of a session no device records. |
| `list_activities` | Date range and sport family, with computed load. |
| `link_activity` | Attach a ride to a planned session. `auto` proposes but links only when unambiguous. |
| `save_planned_workouts` | Store sessions as specs. Validates each; refuses an invalid one. |
| `get_week` | Plan against reality for a range, plus the deviations both ways. |
| `update_planned_workout` | Status, reschedule, mark pushed, or replace the spec. |
| `compute_load` | TSS per ride against the dated FTP, with an hrTSS fallback that says it is one. |
| `get_form` | CTL/ATL/TSB on the standard 42/7-day constants. |
| `compliance_report` | One planned session against the ride, block by block where the laps allow. |
| `export_data` · `import_data` | Full backup with a digest; restore that refuses to overwrite by default. |

`validate_spec` catches the mistakes that actually bite: empty workouts, zero or
negative durations, power resolving outside a sane fraction of FTP (usually
watts and percentages swapped), ramps whose endpoints are equal, both unit forms
on one block, percentages with no FTP, and empty or nested repeats. Unknown keys
come back as warnings, which catches typos like `powr_w` that would otherwise be
ignored in silence.

## Format notes

### `.zwo` (MyWhoosh)

- **The uploaded filename becomes the library name.** MyWhoosh ignores the
  `<name>` tag for this. Set `filename` in the spec and upload under exactly
  that name. Accepted extensions: `.zwo`, `.xml`, `.json`.
- Powers are fractions of FTP (`0.91` = 91%), never watts. Durations in seconds.
- **Ramps are always emitted as `<Ramp PowerLow PowerHigh>`**, never `<Warmup>`
  or `<Cooldown>`. Cooldown ramp direction is read differently by different
  implementations; an explicit `Ramp` cannot be misread. `PowerLow` is the
  *start* value, so a descending ramp has it above `PowerHigh`.
- **Repeats are flattened into individual blocks**, not `<IntervalsT>`.
  MyWhoosh's editor treats an `IntervalsT` block as indivisible, so a single
  repetition can't be adjusted after import.
- Messages become `<textevent timeoffset="10" .../>` nested in their block —
  `timeoffset` is seconds from the start of *that block*, and ~10 s lets
  resistance settle before the text is read against the wrong effort.
- Accented characters and typographic apostrophes render badly in-game, so
  message text is folded to ASCII, with a warning when folding changed anything.

### Garmin workout JSON

Emitted shape, per step:

```json
{
  "type": "ExecutableStepDTO",
  "stepOrder": 2,
  "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
  "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
  "endConditionValue": 600.0,
  "targetType": {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone"},
  "targetValueOne": 227.0,
  "targetValueTwo": 237.0
}
```

- Power targets are **absolute watts**; percentages in the spec are resolved
  against the spec's FTP.
- Repeats are native `RepeatGroupDTO` groups, each carrying a complete
  `endCondition` **including the numeric `conditionTypeId: 7`**. Omitting the id
  makes the API silently corrupt the repeat count — no error, wrong workout.
- `stepOrder` is global and continues through nesting, matching what Garmin's
  own UI produces.
- No heart-rate target is ever emitted.


## Coach-layer notes

- **FTP is resolved per ride, not globally.** Every load number uses the FTP entry in effect on that
  ride's date. A ride before the earliest entry is scored against it and flagged
  `ftp_extrapolated_backwards`. HR figures resolve one field at a time, so a lone resting-HR entry
  cannot shadow a threshold recorded earlier.
- **A backdated entry returns its own row**, with `is_current: false` when a later entry still
  governs today. The zones in that response are the ones the entry establishes, not today's.
- **Power TSS and hrTSS are not comparable.** Each row says which produced it, and a total mixing
  both comes back with a warning. A ride with neither power nor HR returns a null TSS and a reason —
  never a zero, which would be indistinguishable from a rest day.
- **TSB is yesterday's balance** (`CTL(d-1) - ATL(d-1)`), the form carried into a day. Some tools
  report same-day `CTL - ATL`; the two differ by about the size of a session.
- **Dates are the athlete's local dates.** `local_date` comes from `startTimeLocal` and is what plan
  comparison keys on; `start_time_utc` orders rides. An evening ride's UTC date is often the next day.
- **A planned workout is a spec**, stored verbatim and directly renderable. After a verified upload,
  call `update_planned_workout(status="pushed", pushed_to=...)`.
- **Nothing is pushed automatically.** The coach tools store and compute; uploading still goes
  through `render_garmin` / `render_zwo` and the upload skills, with a human in the loop.

Full detail, including every formula and its limits: [the coach layer](coaching.md).

---

Back to the [README](../README.md).

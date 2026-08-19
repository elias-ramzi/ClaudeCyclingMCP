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
| `get_skill` | Fetch a bundled upload procedure by name, so a model asked to "use the mywhoosh-upload skill" can retrieve it. |
| `spec_schema` | The spec's JSON schema and authoring notes. |

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


---

Back to the [README](../README.md).

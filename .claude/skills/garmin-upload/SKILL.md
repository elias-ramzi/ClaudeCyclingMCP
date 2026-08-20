---
name: garmin-upload
description: >-
  Build a structured cycling workout and put it on Garmin Connect. Use this
  whenever someone describes a bike session — intervals, sweet spot, threshold,
  VO2, endurance, a ramp test — and wants it created, added, sent, pushed, put
  on, synced to, or scheduled on Garmin Connect, their Garmin watch, or their
  Edge head unit. Trigger on any of "create a workout", "add this to Garmin",
  "send it to my Garmin", "put it on my head unit", "schedule this for Tuesday",
  not only on the word "upload". Also use to schedule a workout that already
  exists in the Garmin library.
---

# Uploading a cycling workout to Garmin Connect

Render the session with the ClaudeCyclingMCP server, upload it with the Garmin
MCP, then **verify it by fetching it back**. A workout that uploads without
error can still be stored wrong, and nothing tells you when that happens.

## What you need first

- **FTP.** Never assume one. **Unless the athlete states it in the request** —
  a stated figure beats a profile field and needs no second opinion — call
  `get_cycling_ftp`. It returns an `is_stale` flag; if stale, say so and confirm
  the figure with the athlete before rendering. Everything downstream is wrong
  if the FTP is wrong.
- **The session itself.** If any of duration, intensity or structure is
  ambiguous, ask before rendering rather than guessing.

## Steps

### 0. Check the destination exists

Confirm the Garmin MCP is connected — you need `upload_workout`,
`get_workout_by_id` and `delete_workout`. **If they are not there, say so now,
before rendering.** The workout can still be rendered and written to disk for
manual import, but do not begin the upload procedure and do not describe the
result as uploaded.

This costs one look and converts a failure at step 4 into a fact stated at the
start. Observed: a full session spent building a workout for a Garmin MCP that
was not connected.

### 1. Write the spec

Call `spec_schema` if the format isn't already familiar. Keep the spec flat and
readable — someone should be able to see the session in it.

### 2. Check it before rendering

Call `describe_spec`. **Show the block table to the user.** It gives computed
watts, total duration, IF and TSS. This is where a wrong number is cheap to
catch; after upload it is not.

If `validate_spec` reports warnings, surface them — they usually mean a typo'd
key was silently ignored.

### 3. Render

Call `render_garmin`. **Always pass `out_path`** — for example
`~/workouts/<name>.garmin.json`. Not "for example, if convenient": without it
there is no artifact to re-upload, diff, or fall back to, and you will only
discover you wanted one after something has gone wrong.

Hand the returned `payload` to the Garmin MCP **unchanged**. Do not hand-edit
it: the target type ids and the repeat group's `endCondition` are exactly the
fields that fail silently when altered.

**You cannot literally hand it over, and that is the problem.** `render_garmin`
returns the payload as text into your context; `upload_workout` takes an object
you compose. So "unchanged" in practice means retyping ~90 lines of nested JSON,
and one wrong digit in a `targetValueOne` gives a workout that uploads without
error, passes step 5's round-trip, and is wrong — because that check compares
Garmin against what you *sent*, not against what was rendered.

So don't rely on having retyped it correctly. Note `payload_digest` from the
render, and read the ramp warning if there is one (see Reporting back).

**If something warns you the power target looks wrong, read this before acting.**
The Garmin MCP's `upload_workout` docstring says cycling watt ranges take
`workoutTargetTypeId` 6 / `power.between`. Against the live API that guidance is
wrong: id 6 uploads without error and Garmin stores it as a *pace* target on a
cycling workout. The renderer emits id 2 (`power.zone`) with raw watts, which is
byte-for-byte what Garmin's own web UI writes, verified by upload/fetch probe
and by a visual check in Garmin Connect. The rendered payload carries a
`schema_notes` field saying the same thing. Rewriting the target type to id 6 is
the single change that produces a workout which uploads cleanly and is wrong.

### 4. Check what you composed, then upload

Before uploading, pass the payload you have composed to `check_garmin_payload`,
with step 3's `payload_digest` as `expected_digest` **and** the spec as `spec`.
The digest says whether it was altered; the spec says which field.

- `matches_rendered: true` → upload.
- `matches_spec: false` → **do not upload.** Read `differences_from_spec`: it
  names the step and field that moved. Copy the payload from the
  `render_garmin` result again rather than patching it — the difference you can
  see may not be the only one.
- `matches_rendered: false` **with** `matches_spec: true` and an empty
  `differences_from_spec` → those contradict, and the spec diff is the one to
  believe: it names fields, the digest only says something moved. Report the
  contradiction, do not upload, and treat it as a bug in this server rather
  than in your payload.
- Both true → upload.

Then call the Garmin MCP's `upload_workout` with that payload. Keep the returned
`workout_id`.

### 5. Verify — do not skip this

Call `get_workout_by_id(workout_id)`, then pass **both** the payload you sent
and the fetched result to `verify_garmin_upload` — `payload` is what you sent,
`fetched` is what Garmin returned, and they are different shapes. Getting them
the wrong way round is rejected rather than diffed, so an
`error: "shape_mismatch"` means fix the call, not the workout.

This tool is for *after* an upload. To check a payload beforehand, that is
step 4's `check_garmin_payload`.

Pass `expected_digest` too, so this checks both halves at once: that Garmin
kept what it was given, *and* that what it was given is what was rendered.

A `"success"` response from the upload call is not verification. It means the
request was accepted, not that the workout is correct.

- `match: true` → report the workout id and move on.
- `match: false` → **do not tell the user it worked.** Report the differences
  verbatim, delete the bad workout with `delete_workout`, and stop. A mangled
  workout is worse than a failed upload, because nothing else will tell them.

Pay particular attention to any difference mentioning **repeat count** — that
is the known silent corruption, and it means the payload was altered after
rendering.

**If `get_workout_by_id` fails or times out, that is a third outcome and it is
not success.** Observed: a four-minute timeout with the local MCP server
unresponsive. In that case:

- Say, in those words, that **the workout is uploaded but unverified**. Do not
  report success. An upload returning `"success"` means the request was
  accepted, nothing more.
- **Do not delete it.** It is probably fine, and a deleted workout cannot be
  inspected.
- Give the user the manual check: read them the `ui_checklist` **from the
  `render_garmin` result you already have** — that is what each step should
  show in Garmin Connect, generated rather than improvised. Do not call this
  server again to fetch it; if the Garmin read is failing, this server may be
  unreachable too, and the checklist is already in your context. It is also on
  disk beside the payload as `.checklist.txt` if you passed `out_path`.
- Offer to retry the fetch once the Garmin MCP is responsive again.

Two things this check cannot prove, so don't claim them:

- **Watts vs %FTP.** The read Garmin gives back drops the field that
  distinguishes them. The renderer emits watts and this is verified by the
  repo's live test, but if a workout ever looks wildly too hard on the head
  unit, this is the first thing to suspect.
- **How it displays.** The API can accept and echo a structure that still
  renders oddly. Worth one visual check the first time.

### 6. Offer to schedule it

Ask whether they want it on the calendar, then call
`schedule_workout(workout_id, "YYYY-MM-DD")`. It is idempotent, so re-running
it is safe.

## Reporting back

State the workout name and id, total duration, IF/TSS, and that verification
passed. If anything was assumed — an FTP that came back stale, a duration you
picked — say so explicitly.

Two things that look like faults but aren't, worth saying before the athlete
asks:

- **A ramp shows as a static watt range**, e.g. `130-180 W`, which reads as
  "hold anywhere in this band" rather than "climb steadily". Garmin has no ramp
  primitive. `render_garmin` warns when a ramp is wide enough for this to
  matter; pass on the warning, and offer `ramp_steps > 1` to stair-step it. The
  `.zwo` is unaffected — MyWhoosh has a real ramp.
- **A French UI labels the cooldown "Récupération"**, the same word as a
  recovery block, so a session with three recoveries appears to have four. It
  is a translation of the step type, and step type drives nothing at execution.

## Notes

- There is no rename or edit-in-place in the Garmin API. The operations are
  upload (always creates new), delete, and schedule. Fixing a workout means
  uploading a corrected copy and deleting the original.
- To change a session later, edit the spec and re-render. Don't patch the JSON.

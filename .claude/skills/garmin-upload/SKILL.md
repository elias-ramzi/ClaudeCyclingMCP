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

- **FTP.** Never assume one. Call `get_cycling_ftp`. It returns an `is_stale`
  flag — if stale, say so and confirm the figure with the athlete before
  rendering. Everything downstream is wrong if the FTP is wrong.
- **The session itself.** If any of duration, intensity or structure is
  ambiguous, ask before rendering rather than guessing.

## Steps

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

Call `render_garmin`. Pass `out_path` to keep a copy on disk (for example
`~/workouts/<name>.garmin.json`) so a re-upload later doesn't need re-rendering.

Hand the returned `payload` to the Garmin MCP **unchanged**. Do not hand-edit
it: the target type ids and the repeat group's `endCondition` are exactly the
fields that fail silently when altered.

**If something warns you the power target looks wrong, read this before acting.**
The Garmin MCP's `upload_workout` docstring says cycling watt ranges take
`workoutTargetTypeId` 6 / `power.between`. Against the live API that guidance is
wrong: id 6 uploads without error and Garmin stores it as a *pace* target on a
cycling workout. The renderer emits id 2 (`power.zone`) with raw watts, which is
byte-for-byte what Garmin's own web UI writes, verified by upload/fetch probe
and by a visual check in Garmin Connect. The rendered payload carries a
`schema_notes` field saying the same thing. Rewriting the target type to id 6 is
the single change that produces a workout which uploads cleanly and is wrong.

### 4. Upload

Call the Garmin MCP's `upload_workout` with that payload. Keep the returned
`workout_id`.

### 5. Verify — do not skip this

Call `get_workout_by_id(workout_id)`, then pass **both** the payload you sent
and the fetched result to `verify_garmin_upload`.

A `"success"` response from the upload call is not verification. It means the
request was accepted, not that the workout is correct.

- `match: true` → report the workout id and move on.
- `match: false` → **do not tell the user it worked.** Report the differences
  verbatim, delete the bad workout with `delete_workout`, and stop. A mangled
  workout is worse than a failed upload, because nothing else will tell them.

Pay particular attention to any difference mentioning **repeat count** — that
is the known silent corruption, and it means the payload was altered after
rendering.

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

## Notes

- There is no rename or edit-in-place in the Garmin API. The operations are
  upload (always creates new), delete, and schedule. Fixing a workout means
  uploading a corrected copy and deleting the original.
- To change a session later, edit the spec and re-render. Don't patch the JSON.

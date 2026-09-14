---
name: mywhoosh-activity-import
description: >-
  Take the activity file MyWhoosh recorded for an indoor ride and put it on
  Garmin Connect in place of a bad recording of the same session. Use this
  whenever someone says a ride was recorded twice and the two disagree, that
  their head unit's power looks wrong, that the pedals dropped out or read low,
  that a structured session shows up as one lap, or that they want to import,
  send, add or upload a MyWhoosh .fit to Garmin and replace, delete or fix the
  activity already there. Trigger on "my Garmin says 155 W but MyWhoosh says
  187", "the power on that ride is rubbish", "replace it with the MyWhoosh
  file", "where does MyWhoosh save its files", not only on the word "import".
  Covers diagnosing which recording is the real one before anything is deleted.
prompt_input: The ride to work from is
prompt_fallback: >-
  Ask which ride this is about if that is not already clear from the
  conversation, and identify both recordings — the MyWhoosh file and the Garmin
  activity — before changing anything.
---

# Replacing a Garmin activity with the MyWhoosh recording

When an indoor session is recorded by MyWhoosh *and* by a Garmin head unit at
the same time, you end up with two files for one ride. They are not
interchangeable. This procedure identifies the MyWhoosh file, decides — with
evidence — whether the Garmin one is actually bad, and only then replaces it.

**The destructive half is deletion, and Garmin activities have no trash.** There
is no undo, no restore, no support ticket that brings one back. So the decision
step below is not optional throat-clearing: it is the part that stops an agent
from deleting a perfectly good activity because two power meters disagreed.

## What this covers, and what it does not

Covers: **one ride, two recordings, and the Garmin copy is the worse record.**

It does **not** cover, and must not be stretched to cover by analogy:

- **A lost session** — one recording, or none. There is nothing to compare
  against and nothing to justify a deletion. If MyWhoosh recorded a ride Garmin
  never saw, that is a plain upload: Step 1 to find the file, then Step 4's
  upload and Step 5's verification. Nothing is deleted, so nothing needs the
  consent gate.
- **Merging two partial files** — a ride split by a crash or a dead battery.
  This procedure replaces one file with another; it never joins them.
- **Repairing the data inside a file** — no rescaling power, no patching gaps.
  If the MyWhoosh file is also wrong, stop and say so.
- **Structured workouts.** Sending a *planned* session to a platform is
  `garmin-upload` / `mywhoosh-upload`. This is about a *ride that happened*.
- **Anywhere but Garmin.** Strava, TrainingPeaks and intervals.icu each have
  their own duplicate handling.

## Step 0 — Check what you can actually do, and say so now

The replacement half needs `delete_activity` and `upload_activity` on the
**Garmin MCP**. Check for them before you analyse anything, because the answer
changes what you can promise.

**As of 2026-09-02 neither exists on the Garmin MCP used here.** The tools that
do exist, and that this procedure relies on, are `get_activities_by_date`,
`get_activity`, `get_activity_power_in_timezones`, `get_activity_hr_in_timezones`,
`get_activity_splits`, `download_activity_file`, `get_activity_fit_messages`,
`set_activity_name` and `set_activity_description`. Verify rather than trust this
paragraph — the Garmin MCP is a separate project and gains tools.

So there are two shapes this can take, and the user should hear which one applies
before you start, not after:

- **Both tools present** → the whole thing is automatable, and Step 3's consent
  gate is what stands between analysis and an irreversible delete.
- **Either missing** → **the diagnosis is still fully automatable and still
  worth doing; only the last two moves are manual.** Do not improvise a
  substitute — there is no other API route, and `create_manual_activity` makes a
  hollow entry with none of the ride's data, which is worse than doing nothing.
  Hand the user these steps instead:
  1. Delete the bad activity from its page on Garmin Connect: the **⋯** menu →
     *Delete*.
  2. Upload the replacement at `https://connect.garmin.com/modern/import-data`.
  3. Tell you the new activity id, so you can still run Step 5's verification.

Either way, say which route you are on **before** Step 2, so the user is not
told at the end that the fix they just agreed to needs them to open a browser.

## Step 1 — Find the MyWhoosh file

MyWhoosh writes opaque filenames, so nothing about a name tells you which ride
it holds. There are two places to get one, and they are the same bytes.

### The local file (macOS, Windows)

**macOS** — verified against a real MyWhoosh install, 2026-09-02:

```
~/Library/Containers/com.whoosh.whooshgame/Data/Library/Application Support/Epic/MyWhoosh/Content/Data/
```

**Windows** — the package folder carries a per-install publisher hash, so glob
the prefix rather than hard-coding it:

```
%LOCALAPPDATA%\Packages\MyWhooshTechnologyService.MyWhoosh_<publisher hash>\LocalCache\Local\MyWhoosh\Content\Data\
```

In that folder the file is named `MyNewActivity-<app version>.fit`, e.g.
`MyNewActivity-6.1.0.fit`. **Two things about it bite:**

- **It is overwritten by the next ride.** Only the most recent session is there.
  If they have ridden since, the local copy is already gone — use the portal.
- **Several versions can sit side by side** (`MyNewActivity-5.8.2.fit` next to
  `MyNewActivity-6.1.0.fit`) because an app update changes the name and leaves
  the old file. The highest version number is not necessarily the ride you want,
  and neither is the newest mtime if anything has touched the folder. Confirm by
  `time_created`, below.

### The portal (any platform, and the only route on iPad)

`https://event.mywhoosh.com/user/activities` — Profile → **ACTIVITY FILES** —
lists recent activities with a download per ride. Downloads land in the browser's
download folder under an opaque name like
`O1TJeZjituvg8a79Qkbthbg3dHc09pFYi83eDcZj.fit`.

**On iPad and iPhone there is no accessible path.** The app sandbox is not
browsable and MyWhoosh does not publish activity files to the Files app; the
portal is the route, and it works in Safari on the iPad itself.

**The two sources are the same file.** Verified 2026-09-02: the portal download
and the local `MyNewActivity-6.1.0.fit` had identical md5 sums, byte for byte. So
prefer whichever is easier to reach, and use the portal whenever the ride is not
the most recent one.

**Some accounts are served a `.dms` instead.** It is a FIT container under a
different extension — rename it to `.fit` and it parses, and Garmin's importer
wants that extension anyway.

### Confirm it is the right ride

Read the file. No MCP tool on this server parses FIT, so read it directly. Save
the script below as `fitread.py` — Step 2 uses it again for the zone comparison —
and run it with `uv`, which needs no install and no virtualenv:

```bash
uv run --with fitparse python fitread.py <file.fit> [zone floors in watts...]
```

Without `uv`: `pip install fitparse`, then `python fitread.py ...`.

```python
"""Read one FIT file: identity, session summary, zero-power share, time in zone."""

import sys
from fitparse import FitFile

path = sys.argv[1]
floors = [int(w) for w in sys.argv[2:]] or [0]  # zone floors in watts, ascending

fit = FitFile(path)
ident = next(({f.name: f.value for f in m} for m in fit.get_messages("file_id")), {})
session = next(({f.name: f.value for f in m} for m in fit.get_messages("session")), {})

secs = [0] * len(floors)
zeros = missing = total = 0
prev = None
for rec in fit.get_messages("record"):
    d = {f.name: f.value for f in rec}
    power, stamp = d.get("power"), d.get("timestamp")
    total += 1
    if power is None:
        missing += 1
        continue
    step = 1 if prev is None else max(0, min(10, (stamp - prev).total_seconds()))
    prev = stamp
    zeros += power == 0
    secs[max(i for i, f in enumerate(floors) if power >= f)] += step

summary = (
    "sport sub_sport start_time total_timer_time num_laps "
    "avg_power max_power avg_heart_rate max_heart_rate avg_cadence"
).split()
signature = [k for k in ("UUID", "Title", "CurrentRouteId", "IsEvent") if k in session]

print("file_id      ", {k: str(v) for k, v in ident.items()})
print("session      ", {k: session.get(k) for k in summary})
print("dev fields   ", signature)
print(f"records       {total}  power missing {missing}  power == 0 {zeros}")
for i, f in enumerate(floors):
    print(f"  Z{i + 1} (>= {f:>3} W): {int(secs[i]):>5} s")
print(f"  in-zone total: {int(sum(secs))} s")
```

**Match on `file_id.time_created`, and mind the clock.** FIT timestamps are
**UTC**; Garmin's `start_time` from `get_activities_by_date` is **local**. In the
reference case the file read `2026-09-01 16:36:42` and Garmin listed
`2026-09-01 18:36:42` — a two-hour offset that is a timezone, not a mismatch.
Convert one into the other's frame before declaring anything.

**A MyWhoosh file's signature**, for ruling out a file from some other source:

| Field | Value |
|---|---|
| `file_id.manufacturer` | `331` |
| `file_id.product` | `3570` |
| `session.sub_sport` | `virtual_activity` |
| developer fields on `session` | `UUID`, `Title`, `CurrentRouteId`, `IsEvent` |

*Expect:* all four. A file missing the developer fields is not from MyWhoosh —
say which check failed rather than proceeding on the timestamp alone.

## Step 2 — Decide whether the Garmin recording is actually bad

**Do this before proposing anything, and show the working.** Two power meters
disagreeing is ordinary. Two power meters disagreeing *because one stopped
recording* is a different fact, and only the second justifies deleting anything.

### 2a. Do the heart rates agree? — a gate, not a data point

Compare `avg_heart_rate` / `max_heart_rate` from the FIT against `avg_hr_bpm` /
`max_hr_bpm` from `get_activity(activity_id)`.

- **Within a beat or two** → same effort, same ride. Continue. Reference case:
  137/167 from MyWhoosh against 138/166 from the Edge — the same ride, recorded
  twice, beyond argument.
- **Materially different** → **stop.** You have matched the wrong file to the
  wrong activity. Do not continue on the strength of a close timestamp; go back
  to Step 1 and say the HR check failed. Nothing downstream is safe once the two
  files are not the same ride, and this is the only check that catches it.

Duration is a weak gate on its own — the two apps start and stop at different
moments, so a minute or two of difference is normal and proves nothing either
way.

### 2b. Compare the power distributions on identical bounds

Summary averages tell you the files disagree. The distribution tells you *why*,
and that is what decides this.

1. `get_activity_power_in_timezones(activity_id)` returns the Garmin side as
   `secsInZone` per zone plus each zone's `zoneLowBoundary` in watts.
2. Feed **those same floors** to the snippet in Step 1 to get the MyWhoosh side.

That gives two columns on identical bounds. This works: run against a file
Garmin already holds, the snippet reproduces Garmin's own table to within one
second (the only difference is the first sample's assumed interval), so a real
difference between the columns is a difference between the *files*.

Then read the shape:

**A calibration difference moves the whole distribution, coherently.** One meter
reads a fixed factor or a fixed offset above the other, so the time simply
shifts between neighbouring zones and the total time spent pedalling is
unchanged. Test it: take `k = avg_power_A / avg_power_B`, rescale one file's
floors by `k`, recompute, and see whether the columns line up. If they do — or
if a constant offset does it — **this is not corruption.** Both files recorded
the whole ride. **Do not delete anything.** Say which meter you believe and why,
and stop at the annotation in Step 3a if the record needs a note.

**A dropout adds time at the bottom that is not missing from anywhere else.**
When a sensor loses signal, the head unit records zeros — real pedalling filed as
no pedalling. The signature is an excess in the lowest zone far larger than the
difference in total duration, with no factor and no offset that aligns the two
distributions. In the reference case the Garmin file held **999 s more in zone 1
while being only 98 s longer overall** — about 900 seconds of riding recorded as
zero. Nothing multiplicative maps a real 200 W to 0 W, which is exactly why the
calibration test fails here and passes on a genuine calibration gap.

The snippet's `power == 0` count is the direct read of the same thing. A clean
file has a modest count from coasting and stops — 127 out of 4774 samples in the
reference file. A file with intermittent dropouts has a far larger one, scattered
through the ride rather than banked at the start and end.

### 2c. Cross-check against the athlete, not just the files

Zone maths says the files differ; the athlete's history says which number is
real. **At comparable heart rate, the plausible power is the one consistent with
their recent sessions.** Pull a few with `get_activities_by_date` and compare
avg power against avg HR, or use `get_power_duration_curve`. If 187 W at 137 bpm
is ordinary for them and 155 W at 138 bpm would be a step change with no
explanation, that settles it — and it is the check that would catch the case
where the *MyWhoosh* file is the broken one.

### 2d. Lap structure — a reason to prefer a file, not to delete one

A Garmin file that flattens a structured session into a single lap cannot support
any per-block analysis: there is no way to say the third interval faded. The
MyWhoosh file's 31 laps can. That is a real loss, and worth stating.

**On its own it does not justify a deletion.** If the power is sound and only the
laps are gone, annotate (Step 3a) and keep both records. Deletion is for when the
*data* is wrong.

### What to put in front of the user

Before proposing anything: the HR agreement, the two zone columns side by side,
which hypothesis the shape supports, the historical cross-check, and the lap
counts. Then the recommendation. If the evidence is mixed, say so and stop — a
confident replacement built on an ambiguous diagnosis is the failure this whole
step exists to prevent.

## Step 3 — Prefer the non-destructive fix

### 3a. Annotate, which is almost always the right answer

Rather than deleting, mark the bad activity for what it is:

- `set_activity_name(activity_id, "...")` — e.g. prefix the existing name with
  `[POWER SUSPECT]`, so it is obvious in a list.
- `set_activity_description(activity_id, "...")` — what is wrong, the evidence
  in one line, and where the good recording lives. For example: *"Power data
  unreliable — intermittent pedal dropouts, ~900 s recorded as 0 W. HR is
  sound. The MyWhoosh recording of this session has avg 187 W, 31 laps."*

Nothing is lost, nothing is irreversible, and a future analysis — human or
agent — is warned instead of misled.

**What annotation does not fix, so do not claim it does:** the bad numbers stay
in every aggregate Garmin computes. Training load, the power curve, training
status, weekly totals and the fitness trend all keep counting 155 W. Garmin will
not recompute them from a description.

**So replacement is justified when those aggregates have to be right** — someone
training off Garmin's load figures, or whose power curve is being used to set
zones. Otherwise the annotation is genuinely enough, and it is the option to
recommend.

### 3b. If replacement it is — the gate

Before anything is deleted, all of these must hold:

1. **Step 2 concluded dropouts, not calibration**, and you have shown the
   working.
2. **The replacement file is readable and complete.** Re-run the snippet and
   confirm: it parses, `session` carries `total_timer_time`, `avg_power`,
   `avg_heart_rate` and `num_laps`, and the record count is in the region of the
   duration in seconds. **Older MyWhoosh versions omitted the session averages
   entirely** — third-party fixer scripts exist for exactly that — and Garmin
   will show blank power for such a file. Present in 6.1.0; verify anyway, and if
   they are missing, **stop before deleting anything**.
3. **The user has said yes, in this conversation, to deleting that specific
   activity.** Name the id and the date and wait for a clear answer. A general
   "sort out my ride data" is not consent to delete; neither is a yes given
   before you had the diagnosis in front of them.

Deletion is permanent. There is no trash for Garmin activities.

## Step 4 — Delete, then upload. In that order

**Delete first.** Garmin rejects or silently deduplicates an upload whose
timestamp overlaps an existing activity, so uploading first gets you a duplicate
error and nothing landed.

1. `delete_activity(activity_id)` — or the manual route from Step 0.
2. Confirm it is gone with `get_activities_by_date` over that day before
   uploading. This costs one call and distinguishes "the delete failed" from
   "the upload failed", which the upload's own error message will not.
3. `upload_activity(<path to the .fit>)` — or
   `https://connect.garmin.com/modern/import-data`. Keep the new activity id.

**Keep the file on disk throughout.** It is the only copy of the good recording
once the local `MyNewActivity-*.fit` is overwritten, and the whole recovery from
a failed upload depends on it.

## Step 5 — Verify the replacement is real

An upload that returns success is not an upload that landed correctly. Call
`get_activity(new_id)` and check against the FIT's own `session`:

| Field | What it should say |
|---|---|
| `device_manufacturer` | `MYWHOOSH` — the strongest single signal that the right file is now in place; the head unit's copy names a Garmin device |
| `avg_power_watts` / `max_power_watts` | the FIT's `avg_power` / `max_power` |
| `lap_count` | the FIT's `num_laps` |
| `avg_hr_bpm` / `max_hr_bpm` | unchanged from the old activity — same ride |
| `duration_seconds` | the FIT's `total_timer_time` |

Reference case, after replacement: `MYWHOOSH`, 187 W avg, 354 W max, NP 215 W,
`lap_count` 31, 4774 s — matching the file exactly.

**Two traps here:**

- **`has_splits` reads `false` even with 31 laps**, and `get_activity_splits`
  can come back empty. Check `lap_count` from `get_activity`. Do not report the
  laps as lost on the strength of `has_splits`.
- **The new activity has a new id.** Anything pointing at the old one — a
  training log entry, a compliance report, a link stored elsewhere — now points
  at nothing. Re-link it, and say that you have.

Then record where it came from, so nobody has to reconstruct this later:
`set_activity_name` and `set_activity_description` naming the source file, the id
that was deleted, and why.

## Known failure modes

Each of these has a defined response. Report which one you hit rather than
improvising past it.

- **The portal serves a `.dms`.** Rename to `.fit`; it is the same container.
- **`MyNewActivity-*.fit` holds the wrong ride.** They rode again, and the file
  was overwritten. `time_created` will say so. Use the portal.
- **Two `MyNewActivity-<version>.fit` files.** An app update renamed the target
  and orphaned the old one. Pick by `time_created`, never by version number or
  mtime.
- **Heart rates disagree.** Wrong file matched to wrong activity. Stop at Step
  2a; do not proceed on a matching timestamp.
- **The distributions are offset but coherent.** Calibration, not corruption. Do
  not delete. Annotate if anything.
- **Upload rejected as a duplicate.** The old activity is still there — the
  delete did not take, or a device re-synced its copy. Check with
  `get_activities_by_date` and resolve that before retrying; do not upload twice.
- **Deleted, then the upload fails.** The file is on disk, which is why Step 3b
  checks it is readable *first*. Retry through the web importer. Do not delete
  anything else, and tell the user plainly that the day is currently empty on
  Garmin.
- **The Edge activity reappears.** A head unit that re-syncs can push its copy
  back. Check the day again afterwards and delete the duplicate — with fresh
  confirmation.
- **`delete_activity` / `upload_activity` absent.** Step 0. Hand over the manual
  steps; do not substitute `create_manual_activity`.
- **Timestamps look a whole number of hours out.** FIT is UTC, Garmin's
  listing is local. Two hours in the reference case, in summer, in Paris.

## Reporting back

Say what the evidence was, not just what you did: the HR agreement, the zone
comparison and which hypothesis it supported, what was deleted (id and date),
the new activity id, and what the verification read back. If you took the
annotation route, say what remains wrong in Garmin's aggregates — that is the
cost of the safe option and the user should know they are paying it.

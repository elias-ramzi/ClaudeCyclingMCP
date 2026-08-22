# The coach layer

Everything the server stores about an athlete, how their data gets in, and what it computes from it.


Up to here the server is a renderer: a spec in, a `.zwo` or a Garmin payload out, nothing kept. The
coach layer adds the other half of coaching a real person — remembering who they are, what they have
done, and what they are training for — and the arithmetic over it that a model should not be doing
in its head.

```
Garmin MCP ──(the model pastes the JSON)──> import_activities ──> coach.db
                                                                    │
   profile · dated FTP/weight/HR · events · activities · laps · planned workouts
                                                                    │
                                    compute_load · get_form · compliance_report · get_week
```

The division of labour is deliberate:

| | Does |
|---|---|
| **The server** | stores, resolves a dated FTP, computes load and form, compares plan against actual |
| **The `coaching` skill** | tells the model *how to coach* — the interview, the weekly loop, the adaptation rules |
| **The model** | fetches from the Garmin MCP, passes it here, and makes every judgement |

No coaching judgement is encoded in Python. What is encoded is the arithmetic, and the refusals that
stop a wrong number being stored.

## Where the database lives

`~/.claude-cycling/coach.db`, overridable with the `CLAUDE_CYCLING_DB` environment variable. The
file and its parent directory are created on the first coaching tool call — there is nothing to set
up, and no migration to run by hand.

`server_info` reports the path, whether the file exists, and its schema version, and reads none of
it into existence: a tool whose job is to describe the world must not change it.

```json
{
  "database": {
    "path": "/Users/you/.claude-cycling/coach.db",
    "exists": true,
    "env_var": "CLAUDE_CYCLING_DB",
    "expected_schema_version": 2,
    "schema_version": 2
  }
}
```

The server is still **no network, no credentials**. Filesystem access is limited to this database
and to explicit `out_path` writes.

Back it up with `export_data`, which returns every row plus a digest of the content; restore with
`import_data`, which refuses a non-empty database unless forced and refuses a payload whose digest
does not match.

## Ingestion is model-mediated

This server never talks to Garmin. Claude calls the Garmin MCP, gets JSON back, and hands that JSON
to `import_activities` **unchanged**.

That is the whole design, and the reason is narrow: the alternative is a tool with a clean typed
schema, which makes the model retype every number on the way through. A mistyped average power is a
training load that is wrong and looks entirely reasonable, and nothing downstream would ever catch
it. So the import surface takes Garmin's own shapes, warts included, and does the field mapping
where it can be tested.

### A worked example

```
> sync my rides from the last week
```

The model calls the Garmin MCP:

```
get_activities(limit=10)
```

which returns something like:

```json
[
  {
    "activityId": 1662651131,
    "activityName": "Sweet spot 3x10",
    "activityType": {"typeId": 10, "typeKey": "virtual_ride", "parentTypeId": 2},
    "startTimeLocal": "2026-07-05 07:00:00",
    "startTimeGMT": "2026-07-05 05:00:00",
    "duration": 4200.0,
    "movingDuration": 4180.0,
    "distance": 42000.0,
    "elevationGain": 120.0,
    "averageHR": 150,
    "maxHR": 172,
    "avgPower": 190.0,
    "normPower": 198.0,
    "calories": 780.0
  }
]
```

and passes it straight through:

```
import_activities(payload=<that array, verbatim>)
```

```json
{
  "ok": true,
  "seen": 1,
  "inserted": 1,
  "updated": 0,
  "unchanged": 0,
  "rejected": 0,
  "activities": {
    "inserted": [
      {
        "id": 1,
        "garmin_activity_id": "1662651131",
        "sport": "cycling",
        "sub_sport": "virtual_ride",
        "local_date": "2026-07-05",
        "duration_s": 4200.0,
        "normalized_power": 198.0
      }
    ]
  }
}
```

Run the same call again and it reports `unchanged: 1`. Syncing an overlapping window every week is
free.

For a session you want to analyse block by block, fetch its splits too:

```
get_activity_splits(1662651131)   →   import_activity_laps(payload=..., garmin_activity_id="1662651131")
```

### What the import guarantees

- **Idempotent on `activityId`**, which is stored as text. A 10-digit id that came back through JSON
  as `1.662651131e9` and was stored as a float would stop matching itself, and the ride would be
  duplicated rather than updated — doubling its training load.
- **A null never overwrites a stored value.** `get_activities` returns a thinner summary than
  `get_activity`; without this rule, re-syncing the list after fetching one ride in detail would
  blank its normalised power while leaving the load number computed from it.
- **Unknown keys are kept, not rejected.** Garmin adds fields without warning, and an import that
  fails on a new one fails for every ride at once.
- **Every shape is read**: a bare list, a single activity object, a `{"activities": [...]}` wrapper,
  the nested `summaryDTO` form, and a JSON string.
- **Indoor rides are found.** A trainer ride arrives as `virtual_ride` or `indoor_cycling`, never
  `cycling`. Both are stored with `sport: "cycling"` and the device's key in `sub_sport`, so a sport
  filter never silently drops a winter of training.
- **Annotations survive.** RPE, feel and notes belong to this server, not to Garmin, and a re-import
  does not touch them.
- **The ride's date is derived from its start times, never carried across a re-import.** A payload
  with no `startTimeLocal` carries a UTC-derived date that is not null, so merging it as an ordinary
  field moved an evening ride to the next day while the stored local time still said otherwise. It
  is recomputed from the merged row, which is also what the flag reads — so the two cannot disagree.
- **The data-quality flags are stored, and describe the row rather than the payload.** A ride whose
  normalised power arrived in a detailed fetch does not get stamped `no_normalized_power` when the
  thinner weekly list is re-synced over it — the flags are recomputed from what is stored after the
  merge, which is the same rule as the null guard, one column over. Every read returns them as
  `flags`.
- **A payload with no activity type stores a null sport, not `"other"`.** `sport` is derived, so an
  "unknown" value would not be caught by the null rule above — and a thinner re-import would quietly
  reclassify a `virtual_ride` as unknown, dropping it out of every cycling filter while its
  `sub_sport` still said otherwise.
- Rejections come back individually, with a reason each. An activity with no `activityId` or no
  readable start time is rejected; nothing else is.

### On dates

Two are stored per activity. `start_time_utc` orders rides unambiguously. `local_date` — the date
half of `startTimeLocal` — is the day the athlete believes they trained, and is what every plan
comparison keys on.

They disagree more often than seems likely. A 07:00 ride in UTC-5 is 12:00 UTC the same day, but a
22:00 one is 03:00 UTC *the next* — so scheduling against UTC moves an evening session onto
Wednesday's plan. When `startTimeLocal` is missing the UTC date is used and the row is flagged
`local_date_from_utc`.

## FTP is dated, and that is the point

`ftp_history` is a table, not a column. Every training-load number for a ride is computed against
the FTP **in effect on that ride's date**.

A single overwritten value silently rewrites the athlete's history: the same watts against a bigger
FTP is a smaller IF, so a whole block of training shrinks the moment they test better. Weight and HR
thresholds work the same way.

A ride *before* the earliest recorded FTP is scored against that earliest entry and flagged
`ftp_extrapolated_backwards`. Refusing to score it would be worse — the ride vanishes out of CTL,
which reads as a rest week that never happened.

**Every dated figure is resolved by one function.** FTP, weight and each HR field go through the
same rule — the latest entry at or before the date, else the earliest with `extrapolated_backwards`
— and a tool that scores more than one activity loads the history once and resolves in memory.
Scoring 200 rides used to issue over 800 queries for the same handful of rows; it now issues ten
regardless of how many rides there are.

**HR figures resolve one field at a time.** `log_hr` takes any subset, so logging a resting HR on
its own is a normal thing to do; resolving the latest *row* would let that entry shadow a threshold
recorded months earlier, and every no-power ride would stop being scored because the athlete
mentioned their morning pulse. Each figure keeps the date of the entry it came from, reported as
`effective_dates`.

**A backdated entry returns its own row, and says it is not current.** "My FTP test was actually
three weeks ago" is a normal correction. The response carries the row that was written, the zones it
establishes, `zones_apply_from`, and `is_current: false` when a later entry still governs today. All
three loggers do this — being handed today's W/kg after logging last month's weigh-in is the same
defect wearing different units — and `log_hr` answers it per field, since a backdated resting HR
supersedes nothing about the threshold.

`get_zones(as_of="2026-07-05")` returns the zones a ride was actually performed against, which after
an FTP change is not today's table.

## Training load

**With power**, the standard formula:

```
IF  = NP / FTP
TSS = duration_h x IF^2 x 100
```

One hour at threshold is 100 by construction. Where a ride has no normalised power, average power is
used and the row is flagged `no_normalized_power` — NP is never below average, so that number
understates a variable ride.

**Without power**, the fallback is hrTSS: the same formula with `avg HR / threshold HR` replacing
the power ratio.

**Every total says what it is made of.** `list_activities`, `get_week` and `compute_load` all report
`scored`, `unscored` and `by_method` beside the number, with a warning when the total mixes power
TSS and hrTSS, and another when unscored rides mean it understates the period. A null TSS is never
folded to zero on the way into a total — a week with three unscored rides must not read as a light
week.

**The two are not comparable, and each row says which it is.** hrTSS:

- cannot see variability — thirty sprints and a steady tempo ride at the same average HR score
  identically, and the power TSS of the first is far higher;
- is inflated by cardiac drift on long rides, where heart rate climbs at constant power;
- under-scores short sharp sessions, where HR never catches up with the effort;
- inherits every error in the threshold HR, which is often itself estimated at 92% of max HR — a
  5 bpm error moves every hrTSS by about 7%.

A ride with neither power nor heart rate returns a **null** TSS and a reason, never a zero. A zero is
indistinguishable from a rest day and would drag CTL down as if the athlete had not ridden.

## Form

`get_form` runs the standard exponentially weighted model, stepped once per calendar day including
rest days:

```
CTL(d) = CTL(d-1) + (TSS(d) - CTL(d-1)) / 42     fitness
ATL(d) = ATL(d-1) + (TSS(d) - ATL(d-1)) / 7      fatigue
TSB(d) = CTL(d-1) - ATL(d-1)                     form
```

**TSB is yesterday's balance** — the form carried into a day, before that day's session lands on it.
Some tools report same-day `CTL - ATL`; the two differ by roughly the size of a session, which is the
difference between reading a hard Tuesday as fresh and reading it as buried.

The walk starts at the earliest stored activity, so CTL entering the reported window is built from
real history rather than from zero. Where that run-up is under 42 days the numbers are still climbing
out of their starting value, and `warmup_incomplete` says so. Pass `seed_ctl`/`seed_atl` if better
starting values are known.

Cross-check against the Garmin MCP's `get_training_load_trend`. It computes from everything Garmin
holds, using its own load metric; this computes from what was imported, using TSS. A disagreement is
information — usually a gap in what was imported, or rides scored by heart rate here. Find the cause
rather than averaging two numbers when only one of them can be explained.

## Plan versus actual

A planned session **is** a spec — the same document `render_garmin` and `render_zwo` consume, stored
verbatim, so a stored plan is directly renderable months later. Every spec is validated on save and
an invalid one is refused: a plan that cannot be rendered is not a plan, and the failure would
otherwise surface on the morning it was due.

`get_week` returns the plan, the rides, and the deviations both ways:

- **planned, not ridden** — a session whose date has passed with nothing linked and no status
  explaining it;
- **ridden, not planned** — a ride tied to no session. A race-day ride linked to an event is *not*
  listed here: it was training the plan knew about. It still counts in full toward load.

`compliance_report` compares one session against the ride linked to it. When laps are stored and
their count matches the plan's executable blocks — repeats expanded, because three intervals are
three laps — each block is compared to its lap and written out as a sentence:

```
the second block fell to 228 W against a 250 W target
```

When the counts differ, **no pairing is invented**. Six laps aligned against nine blocks by position
produces confident claims about the wrong intervals, and lap counts rarely match a plan exactly — an
athlete pressing lap at a junction, or a head unit auto-lapping every 5 km, breaks it. The laps come
back as they are, with the totals compared and the mismatch stated.

A block within 5% of target counts as on target — deliberately not the band `render_garmin` writes
to the head unit (2% for intervals, 5% at the easy end, or `garmin_target_band_pct`). The two
measure different things: the rendered band is what the athlete was told to hold, this one is how
far off a lap average has to be before it is worth mentioning.

A recovery, warmup or cooldown ridden *below* target is `easier_than_target` and not a miss: that
target is a ceiling.

**Each block carries two verdicts**, because one can be knowable while the other is not: `verdict`
for power, `duration_verdict` for time. A lap with no watts says nothing about whether a target was
held — but if it ran half its planned length, that much *is* known, and it counts as a deviation.
Duration does not need a power meter.

`off_target_blocks` counts wrong power and `off_duration_blocks` counts short or long. Every block
then lands in exactly one of `deviating_blocks`, `unverifiable_blocks` and `compliant_blocks` — the
classification is closed-world, and a verdict nothing recognises raises rather than passing. A block
is unverifiable when its power could not be checked (`no_power`) **or** its lap carried no duration
at all, and compliant only when everything asked of it was checked and held. The session `verdict`
is `deviated` if anything deviated, else `unverifiable` if **any** block could not be checked — one
clean warmup in front of five no-power intervals is not evidence the intervals were ridden — else
`as_prescribed`.

The laps are returned whenever any are stored, mismatch included — that is precisely the case where
nothing could be compared and the caller has to look at them.

## Events

An event is not a planned workout. A workout fills a week; an event is what the weeks point at, and
the next priority-A event is the anchor a periodised plan is built backwards from — `list_events`
returns it as `next_a_event` with the weeks remaining.

The results half of the row is filled after the race by `record_race_result`: the race-day activity,
the finish time, and a free-text `debrief`. That last field is the one that keeps earning its place.
Where the athlete cracked last year, what they ate and when, how the pacing went — it comes back the
next time the same event is planned for, and it is more specific than any general principle.

Linking refuses an activity whose date is not the event's unless forced. The realistic slip is
linking the Sunday spin after a Saturday race, which then makes the A-event look like an easy hour.

`record_race_result` completes an event still marked `upcoming` — but only when the call carries a
result. A bare call writes nothing, so a retry or an existence probe cannot close a race with no
time, no ride and no debrief. And an empty string is never an erase: across every stored free-text
field — debrief, event and session notes, `feel`, the profile fields — blank text leaves what is
stored alone and the response names the field it ignored. A stored value is replaced by better
text, never by nothing.

## Nulls the coach layer refuses to round off

`duration_s` is nullable on activities and on laps: a thin payload is tolerated and flagged, not
rejected. Nothing folds that null to zero. A ride with no duration reads "unknown" in `get_week`
and `compute_load`, `compliance_report` says it could not be checked rather than inventing a
shortfall against the plan, and `import_activity_laps` reports how many laps carry no time instead
of accusing a ride's own splits of belonging to a different ride. Same rule as a null TSS: a zero
is indistinguishable from a real zero, and a fabricated deviation is worse than no answer.

## Schema and migrations

Migrations are ordered functions with a version, applied in sequence and recorded in
`schema_version`. They are **append-only**: never edit one that has run on a real database, because
the only thing that reruns is a version that has not been applied. Change the schema by adding the
next migration.

| Version | Adds |
|---|---|
| 1 | `athlete`, `ftp_history`, `weight_history`, `hr_history`, `activities`, `activity_laps` |
| 2 | `events`, `planned_workouts`, and the subjective columns (`rpe`, `feel`, `note`) on `activities` |
| 3 | `flags_json` on `activities` — the import's data-quality notes, kept rather than reported once |

Every table carries an `athlete_id`, defaulting to `1`. One athlete is the whole use case today; the
column exists so adding a second is a schema no-op rather than a rewrite.

---

Back to the [README](../README.md) · the [tool reference](tools.md) · the [skills](skills.md).

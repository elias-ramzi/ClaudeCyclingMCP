# The spec format

The one canonical input. Flat JSON, meant to be read and edited by hand — you should be able to look at it and see the session.


```json
{
  "name": "Sweet Spot 3x10",
  "ftp": 255,
  "filename": "sweetspot-3x10",
  "description": "70 min sweet spot",
  "blocks": [
    {"type": "ramp",   "duration": "20:00", "from_w": 130, "to_w": 180, "role": "warmup",
     "message": "Easy spin, let the legs open up"},

    {"type": "steady", "duration": "10:00", "power_w": 232, "cadence": 90,
     "message": "Sweet spot 1 of 3", "hr_note": "expect 145-155 bpm"},
    {"type": "steady", "duration": "05:00", "power_w": 145, "role": "recovery"},
    {"type": "steady", "duration": "10:00", "power_w": 232, "cadence": 90},
    {"type": "steady", "duration": "05:00", "power_w": 145, "role": "recovery"},
    {"type": "steady", "duration": "10:00", "power_w": 232, "cadence": 90},

    {"type": "ramp",   "duration": "10:00", "from_w": 140, "to_w": 130, "role": "cooldown"}
  ]
}
```

`describe_spec` on that gives:

```
Sweet Spot 3x10  —  FTP 255 W
1:10:00 total · NP 198 W · IF 0.77 · TSS 70 · 772 kJ

#  Block     Dur    Elapsed  Target                    Cadence  Notes
-  --------  -----  -------  ------------------------  -------  --------------------------------------
1  warmup    20:00  20:00    130 -> 180 W (51 -> 71%)           Easy spin, let the legs open up
2  interval  10:00  30:00    232 W (91%)               90 rpm   Sweet spot 1 of 3 | expect 145-155 bpm
3  recovery  05:00  35:00    145 W (57%)
4  interval  10:00  45:00    232 W (91%)               90 rpm
5  recovery  05:00  50:00    145 W (57%)
6  interval  10:00  1:00:00  232 W (91%)               90 rpm
7  cooldown  10:00  1:10:00  140 -> 130 W (55 -> 51%)
```

That spec is in [`examples/sweetspot-3x10.json`](../examples/sweetspot-3x10.json).

## Top level

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Workout title on both platforms. |
| `ftp` | yes | Watts. Required even when every block is in watts, because `.zwo` stores power **only** as a fraction of FTP. |
| `blocks` | yes | At least one. |
| `filename` | no | Filename stem for the `.zwo`. **This becomes the MyWhoosh library name** — see below. Defaults to a slug of `name`. |
| `author`, `description` | no | Copied into the `.zwo` and the Garmin payload. |
| `garmin_target_band_pct` | no | Half-width of the watt band put around a single-number target for Garmin, which needs ranges. A percentage **of the resolved target**, not of FTP: at 2, a 293 W target becomes 287–299 W and a 128 W target becomes 125–131 W. Omit it and the width is chosen by role — 2% for interval and rest, 5% for recovery, warmup and cooldown, because ±2% of an easy spin is a window that alarms continuously. Setting it applies one number everywhere. |
| `ftp_source` | no | Where the FTP came from: `athlete_stated`, `garmin_profile` or `test_result`. A workout on a head unit is raw watts and a `.zwo` stores only fractions, so neither file records which number produced it. |
| `ftp_date` | no | When that FTP was established, e.g. `2026-08-19`. Shown beside the FTP in `describe_spec` and in the `.zwo` description. |

## Blocks

`steady`, `ramp`, `free`, and `repeat`.

**Power units are explicit — there is no guessing from magnitude.** A steady
block carries either `power_pct` or `power_w`, never both. Ramps use
`from_pct`/`to_pct` or `from_w`/`to_w`.

Either form takes a single number or a `[low, high]` pair:

```json
{"type": "steady", "duration": "10:00", "power_pct": 91}
{"type": "steady", "duration": "10:00", "power_pct": [89, 93]}
```

`duration` is whole seconds (`600`) or a clock string (`"10:00"`, `"1:05:00"`).

Repeated sets nest one level, and take `count` and `blocks` **instead of**
`duration` — a repeat's duration is the sum of its contents:

```json
{"type": "repeat", "count": 3, "blocks": [
  {"type": "steady", "duration": "04:00", "power_pct": 105},
  {"type": "steady", "duration": "02:00", "power_pct": 55, "role": "recovery"}
]}
```

Optional on any block:

- `cadence` — target rpm, single value or `[low, high]`.
- `message` — on-screen text.
- `hr_note` — a heart-rate check figure. **Carried as a message on both
  platforms and never as a control target.** Both platforms drive on power; an
  HR range in a session description is something to check against, not chase.
- `role` — `warmup`/`interval`/`recovery`/`cooldown`. Picks the Garmin step
  type; the `.zwo` ignores it. Inferred when absent: first top-level block is
  warmup, last is cooldown, the rest are intervals.
- `ramp_steps` (ramps only) — Garmin has no ramp primitive, so a ramp becomes
  one step showing the whole range. Set this above 1 to stair-step it into that
  many Garmin steps instead. No effect on the `.zwo`.

## On FTP

**Never hardcode an FTP, and don't trust a profile value silently.** Get it from
Garmin's `get_cycling_ftp` — but that returns an `is_stale` flag, and it is
often true. On the account this was built against it reports a value from
January 2025 flagged stale. Confirm the number before rendering; everything
downstream is wrong if it's wrong.

`get_cycling_ftp` reads **Garmin's** profile. MyWhoosh keeps its own FTP and
there is no API to read it — but the `mywhoosh-upload` skill is in the browser
anyway, so it reads the FTP out of the builder and renders against *that*,
rather than assuming Garmin's number applies.

That matters because the two platforms consume FTP at different times:

| | What lands in the file | What sets the watts actually ridden |
|---|---|---|
| Garmin | absolute watts, resolved at render time | nothing further — the file is already in watts |
| MyWhoosh | fractions of FTP (`0.9098`) | **MyWhoosh's own profile FTP**, applied at ride time |

So for Garmin, `spec.ftp` is just the conversion factor for `power_pct` blocks,
and a mistake shows up in `describe_spec` as wrong watts.

For MyWhoosh it is sharper. The `.zwo` stores only ratios, so the watts ridden
are `fraction × the FTP in your MyWhoosh profile`. **If `spec.ftp` is 255 and
MyWhoosh's profile says 200, a 232 W block is ridden at 182 W, and nothing warns
you** — the file is still "correct", it is just scaled to a different athlete.
Keep `spec.ftp` equal to MyWhoosh's FTP. The skill does this by reading the
FTP from the builder *before* rendering, so the fractions are right by
construction.

The FTP field in the MyWhoosh *builder* is a third, separate thing: a preview
setting driving the displayed watts and Training Load. It resets to 200 W on
import and must be re-entered, but it does not change the stored fractions.


---

Back to the [README](../README.md).

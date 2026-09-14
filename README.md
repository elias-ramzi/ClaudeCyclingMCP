# ClaudeCyclingMCP

**Describe a structured cycling session once. Get a valid MyWhoosh `.zwo` and a valid Garmin
Connect workout out of it.**

```
spec (JSON) ──┬── render_zwo      → .zwo   (MyWhoosh)
              └── render_garmin   → .json  (Garmin upload_workout payload)
```

An MCP server that turns one hand-readable workout spec into files both platforms accept, and
then **checks that what the platform stored is what you sent**. On top of that sits a
[coach layer](#coaching): a local file of who the athlete is, what they have ridden, and what they
are training for, plus the load arithmetic over it.

No network, no credentials, no uploads — filesystem access is limited to the server's own database
and to explicit `out_path` writes. Uploading is a separate, human-in-the-loop step that ships as
[skills](#skills).

## Why this exists

Writing a structured bike session today means either clicking blocks around in a graphical editor
— slow, imprecise — or hand-writing XML. And the two platforms want different formats, so the
same session gets built twice.

Garmin's MCP has structured builders for running, strength and walk/run, but nothing for cycling:
a bike session there means raw JSON through `upload_workout`. MyWhoosh has no usable API at all.

Worse, both platforms fail *quietly*. A Garmin watt target sent with the target type the Garmin
MCP's own docs recommend uploads without error and is stored as a **pace** target. A repeat group
missing one numeric id silently gets the wrong repetition count. A `.zwo` stores power only as a
fraction of FTP, so a spec written against the wrong FTP produces a file that is still "correct"
— just scaled to a different athlete. None of these announce themselves.

So: one spec in, two files out, and a verification step that compares against **what was sent**
rather than trusting a success response.

## Highlights

- 📝 **One hand-readable spec** — flat JSON with `steady`, `ramp`, `free` and `repeat` blocks. Power is explicit: `power_w` or `power_pct`, never guessed from magnitude ([spec format](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/spec-format.md)).
- 📊 **See the session before you build it** — `describe_spec` prints a block table with computed watts, elapsed time, NP, IF, TSS and kJ.
- ✅ **Validation that catches the real mistakes** — swapped watts/percentages, zero durations, flat ramps, nested repeats, and typo'd keys like `powr_w` that would otherwise pass in silence.
- 🚴 **Two renderers, one source** — `.zwo` for MyWhoosh and a Garmin `upload_workout` payload, each pinned to a platform quirk that was expensive to learn ([format notes](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/tools.md#format-notes)).
- 🔍 **Verification, not optimism** — compare a Garmin upload against what was actually stored, and a MyWhoosh import against the scraped builder header, including a pre-import snapshot that catches a silent no-op.
- 🏋️ **A coach, not just a renderer** — a local file of profile, dated FTP/weight/HR, objectives, imported rides and planned sessions, with TSS, CTL/ATL/TSB and plan-vs-actual computed from it ([the coach layer](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/coaching.md)).
- 🥗 **Nutrition on the same file** — an ingredient base, standard meals and a per-ingredient food log, with calorie targets computed from BMR **and that day's actual training** — a big session is not a deficit day ([the nutrition layer](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/nutrition.md)).
- 🔐 **No credentials, ever** — the server never uploads and holds no tokens. Its only side effects are its own database and the file you ask for with `out_path`.
- 🧩 **Bundled skills** — Garmin upload-and-verify, a browser-driven MyWhoosh import that reads MyWhoosh's own FTP before rendering, a MyWhoosh-to-Garmin activity replacement that diagnoses which of two recordings is the real one before anything is deleted, and generic cycling-coach and nutrition procedures.

## Install

Requires Python 3.10+.

**Claude Code** — install the plugin; it registers the server **and** the skills in every session,
from any directory:

```bash
/plugin marketplace add elias-ramzi/ClaudeCyclingMCP
```

```bash
/plugin install claude-cycling-mcp@cycling-tools
```

**Any MCP client** — register the server; the bundled skills travel with it as MCP prompts:

```json
{
  "mcpServers": {
    "claude-cycling": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/elias-ramzi/ClaudeCyclingMCP", "claude-cycling-mcp"]
    }
  }
}
```

**Claude Desktop** — drag `claude-cycling-mcp.mcpb` from the
[latest release](https://github.com/elias-ramzi/ClaudeCyclingMCP/releases) onto the app.

Full details, including how to upload the skills to your Claude account so the model reaches for
them on its own: [Installing](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/install.md).

## What you can do

Once connected, describe a session and ask Claude to build it. It drives these
[tools](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/tools.md):

- **Write a spec** — `spec_schema` hands the model the schema and the authoring notes, so the JSON comes out valid the first time.
- **Check it** — `validate_spec` for errors and warnings, `describe_spec` for the block-by-block table with NP/IF/TSS.
- **Render** — `render_zwo` returns the XML plus the filename to upload it under (**the filename becomes the MyWhoosh library name**); `render_garmin` returns a payload ready for the Garmin MCP's `upload_workout`. Both take an optional `out_path`.
- **Verify before you upload** — `check_garmin_payload` compares the payload you composed against the digest the renderer issued, and returns a checklist for confirming by eye.
- **Verify after** — `verify_garmin_upload` diffs what Garmin returns against what was sent; `verify_mywhoosh_import` does the same for the scraped MyWhoosh builder header.

One thing worth reading before your first workout: [**On FTP**](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/spec-format.md#on-ftp) — the
two platforms consume FTP at different times, and getting it wrong on the MyWhoosh side is silent.

## Skills

Five bundled procedures in [`.claude/skills/`](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/.claude/skills), triggering on how someone
actually describes what they want — "create", "add", "send", "put it on", "what am I doing this
week", "the power on that ride is wrong" — not only on "upload":

- **[`garmin-upload`](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/.claude/skills/garmin-upload/SKILL.md)** — renders, uploads via the Garmin MCP, then fetches the workout back and compares it against what was sent. Offers to schedule it.
- **[`mywhoosh-upload`](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/.claude/skills/mywhoosh-upload/SKILL.md)** — drives the MyWhoosh builder through Claude in Chrome, since there is no API. It reads MyWhoosh's FTP out of the builder *before* rendering, so the fractions are right by construction, and stops for explicit confirmation before the export — which spends a finite slot credit.
- **[`mywhoosh-activity-import`](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/.claude/skills/mywhoosh-activity-import/SKILL.md)** — the ride that happened rather than the session that was planned. When an indoor ride is recorded twice, it finds the MyWhoosh `.fit` and works out *from the time-in-zone distribution* whether the Garmin copy is genuinely corrupt or the two power meters simply disagree — only the first justifies a replacement. Deletion is permanent, so it prefers annotating and asks before removing anything.
- **[`coaching`](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/.claude/skills/coaching/SKILL.md)** — how to coach with the tools below: the onboarding interview driven by whatever the profile is still missing, the weekly loop (read reality, compare to plan, then write it), and the adaptation rules that make it a plan rather than a template. Generic — it carries no athlete's facts.
- **[`nutrition`](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/.claude/skills/nutrition/SKILL.md)** — seeding a food base from whatever the athlete already tracks, the daily logging loop where "can I eat X?" is answered by subtraction rather than by a yes or no, and why a big session is not a deficit day. Equally generic: the athlete's own foods, portions and preparation quirks live in their database, not in the skill.

Each step states what it expects to see, so a run that breaks after a platform redesign reports
which assumption failed instead of quietly producing nothing.

How a skill reaches your client, and what each needs to actually run: [Skills](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/skills.md).

## Coaching

The renderer half of this server is stateless. The coach layer is not: it keeps the athlete's file
in a local SQLite database at **`~/.claude-cycling/coach.db`** (override with `CLAUDE_CYCLING_DB`),
created on the first coaching call with nothing to set up. `server_info` reports its path, whether
it exists, and its schema version — without creating it.

What it holds, and what it computes:

- **Profile and dated history** — availability, equipment, constraints, and append-only FTP, weight
  and HR entries. FTP is dated because every ride is scored against the FTP **in effect on that
  ride's date**; one overwritten value silently rewrites the athlete's whole history.
- **Objectives** — races past and future, with a debrief written after the race. The next A-event is
  the anchor a plan is built backwards from; last year's debrief is what makes planning for the same
  event specific.
- **Activities** — a normalised cache of what the athlete rode, with provenance and the raw payload
  kept, plus optional per-lap splits and a subjective layer (RPE, feel, notes).
- **Planned sessions** — stored as specs, so a plan written in March is still directly renderable in
  June.
- **Deterministic analysis** — `compute_load` (power TSS, hrTSS fallback, and which was used),
  `get_form` (CTL/ATL/TSB on the standard 42/7-day constants), `compliance_report` (block by block
  against the laps), `get_week` (plan against reality, and the deviations both ways).

### Ingestion is model-mediated

This server never talks to Garmin. Claude calls the Garmin MCP, gets JSON back, and passes it here
**unchanged** — because the alternative, a tool with a clean typed schema, makes the model retype
every number on the way through, and a mistyped average power is a training load that is wrong and
looks entirely reasonable.

```
get_activities(limit=10)          →  [{"activityId": 1662651131, "activityType": {"typeKey": "virtual_ride"},
                                       "startTimeLocal": "2026-07-05 07:00:00", "duration": 4200.0,
                                       "avgPower": 190.0, "normPower": 198.0, "averageHR": 150, ...}]

import_activities(payload=…)      →  {"inserted": 1, "updated": 0, "unchanged": 0, "rejected": 0}
import_activities(payload=…)      →  {"inserted": 0, "updated": 0, "unchanged": 1, "rejected": 0}
```

Idempotent on `activityId`, so syncing an overlapping window every week is free. A stored value is
never overwritten with a null, so re-syncing the summary list after fetching one ride in detail
cannot blank its normalised power. A trainer ride arriving as `virtual_ride` is still filterable as
cycling.

Full detail — the schema, the formulas and their limits, the timezone rule: [the coach
layer](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/coaching.md).

## Nutrition

The nutrition layer sits in the same database, for the same athlete, because that is what makes a
calorie target mean anything: it is computed from the athlete's profile **and from what the training
tables say about that date**.

- **An ingredient base** — per-100 g macros, aliases so "skyr, 200" resolves, habitual portions,
  raw-versus-cooked state, price per package, and a note for the athlete's own know-how (a
  rice-cooker water ratio, "weigh it — this is where eyeballing drifts").
- **Standard meals** — a name and a list of grams. Their macros are computed from the ingredients
  every time, so a correction propagates. A **log entry** does the exact opposite and freezes its
  macros and cost at log time: a day already eaten is a measurement, not a view over current data.
- **Targets that know about the ride** — Mifflin-St Jeor BMR, a sedentary baseline, plus Garmin's own
  calories for that day (or the planned session's mechanical work, for a date still ahead), then the
  goal's rate. Every input and intermediate is in the response, because a target whose arithmetic is
  invisible can only be accepted or refused.
- **Day type read, not asked** — an event makes its date `race` and the day before `race_eve`; a long
  or hard ride makes it `big_session`. **On those days the deficit is withheld**, and the response
  says how much: under-fuelling a hard session costs the session and the recovery from it.
- **Refusals that hold** — a target below computed BMR is clamped by `suggest_targets` and refused
  outright by `confirm_targets`, whoever asked.

```
suggest_targets(date="2026-08-24")   →  BMR 1682 x 1.3 = 2187, + 900 kcal ridden = 3087 maintenance,
                                        -550/day for a -0.5 kg/week goal  →  2537 kcal, 145 g protein
confirm_targets(date="2026-08-24")

log_meal(meal="Petit-dej", overrides=[{"ingredient": "cruesli", "grams": 30}])
                                     →  261 kcal, 24.4 g protein, grouped under "Petit-dej"
log_food(entries=[{"label": "canteen: chicken and chips", "kcal": 700,
                   "protein_g": 35, "is_estimate": true}], slot="lunch")

day_summary(date="2026-08-24")       →  961 kcal eaten; remaining 1576 kcal, 85.6 g protein,
                                        23.2 g fibre — 1 entry is a flagged estimate
```

The evening is then composed from that remainder and `list_meals`. The deficit itself is never judged
on one day: `week_summary` reads it on the weekly average and on the 7-day moving average of morning
weigh-ins, because day-to-day weight is water, glycogen and salt.

Full detail — the schema, the target formula, the freeze rule: [the nutrition
layer](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/nutrition.md).

## Documentation

- [Spec format](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/spec-format.md) — every field, the block types, and how FTP is consumed by each platform.
- [Tools](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/tools.md) — the full tool reference, plus the `.zwo` and Garmin format rules the renderers are pinned to.
- [The coach layer](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/coaching.md) — the database, the model-mediated ingestion flow, and every formula with its limits.
- [The nutrition layer](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/nutrition.md) — the food base, the freeze rule, and how a calorie target is derived from the day's training.
- [Garmin schema provenance](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/garmin-schema.md) — what the payload shape was derived against, the two silent-failure findings, and verification status.
- [Skills](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/skills.md) — what each bundled skill does, and the two ways one runs.
- [Testing](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/testing.md) — the offline gate and the live Garmin round-trip.
- [CLAUDE.md](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/CLAUDE.md) — architecture, and the platform quirks that are load-bearing.
- [CONTRIBUTING.md](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/CONTRIBUTING.md) — how to propose a change, and the two project-specific rules.
- [docs/versioning.md](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/versioning.md) · [CHANGELOG.md](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/CHANGELOG.md) · [SECURITY.md](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/SECURITY.md)

## Development

```bash
uv venv && uv sync --extra dev
```

```bash
ruff check . && ruff format --check . && pytest
```

The offline suite is hermetic and needs no credentials. The live Garmin round-trip is deselected
by default — see [Testing](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/testing.md).

## License

MIT.

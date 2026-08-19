# ClaudeCyclingMCP

Describe a structured cycling session once. Get a valid MyWhoosh `.zwo` and a
valid Garmin Connect workout out of it.

```
spec (JSON) ──┬── render_zwo      → .zwo   (MyWhoosh)
              └── render_garmin   → .json  (Garmin upload_workout payload)
```

## Why this exists

Writing a structured bike session today means either clicking blocks around in a
graphical editor — slow, imprecise — or hand-writing XML. And the two platforms
want different formats, so the same session gets built twice.

Garmin's MCP has structured builders for running, strength and walk/run, but
nothing for cycling: a bike session there means raw JSON through
`upload_workout`. MyWhoosh has no usable API at all.

So: one spec in, two files out.

## Scope

**The server takes a workout spec and emits valid files.** It is pure and
deterministic — no network, no auth, no credentials. Writing a rendered file to
disk, when you pass `out_path`, is the only side effect it has.

**Uploading is not part of the server.** Uploads need auth and browser state,
and a MyWhoosh export spends a limited resource, so they need a human in the
loop. They ship as [skills](#skills) instead.

## Install

Requires Python 3.10+.

### Claude Code — the plugin

Installs the server **and** the skills, in every session, from any directory:

```bash
/plugin marketplace add elias-ramzi/ClaudeCyclingMCP
```

```bash
/plugin install claude-cycling-mcp@cycling-tools
```

Running Claude Code from a clone works too — `.claude/skills` is picked up from
the working directory.

### Any MCP client — nothing extra to install

The server registers each bundled skill as an [MCP
prompt](https://modelcontextprotocol.io/specification/server/prompts), so the
procedures travel with it. Register the server and they appear in the client's
prompt menu at the version the server shipped with:

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

Or from a clone: `pip install -e . && python -m cycling_mcp`.

### Claude Desktop — the one-click extension

Download `claude-cycling-mcp.mcpb` from the
[latest release](https://github.com/elias-ramzi/ClaudeCyclingMCP/releases) and drag it into Claude
Desktop. The server itself is fetched by `uvx` on first run, so you need `uv` and Python 3.10+ on
the machine.

**This registers the server, its tools, and its prompts — it does not install the skills.** An
`.mcpb` packages an MCP server and has no skills mechanism at all: the manifest schema rejects a
`skills` key outright. The `SKILL.md` files travel inside the bundle only so you have them to hand;
nothing reads them from there. For the model to reach for a skill on its own, do the step below.

### Claude Desktop and claude.ai — upload the skills

The prompts above already work once the server is connected. To also get the
**model-invoked** behaviour — so "put this session on my Garmin" reaches for the
skill on its own — upload the skills to your Claude account:

```bash
cd .claude/skills && for s in */; do zip -r "${s%/}.zip" "$s"; done
```

Then in Claude, **Customize → Skills → +**, and upload one `.zip` per skill.
They are per-account and sync to Desktop, claude.ai and Cowork. Requires code
execution enabled on your plan.

## The spec format

Flat JSON, meant to be read and edited by hand. You should be able to look at it
and see the session.

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

That spec is in [`examples/sweetspot-3x10.json`](examples/sweetspot-3x10.json).

### Top level

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Workout title on both platforms. |
| `ftp` | yes | Watts. Required even when every block is in watts, because `.zwo` stores power **only** as a fraction of FTP. |
| `blocks` | yes | At least one. |
| `filename` | no | Filename stem for the `.zwo`. **This becomes the MyWhoosh library name** — see below. Defaults to a slug of `name`. |
| `author`, `description` | no | Copied into the `.zwo` and the Garmin payload. |
| `garmin_target_band_pct` | no | Half-width of the watt band put around a single-number target for Garmin, which needs ranges. Default 2. |

### Blocks

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

Repeated sets nest one level:

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

### On FTP

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

## Tools

| Tool | Does |
|---|---|
| `validate_spec` | Errors and warnings, plus a summary when valid. |
| `describe_spec` | The block table above — computed watts, durations, IF, TSS. |
| `render_zwo` | `.zwo` XML plus the filename to upload it under. Optional `out_path`. |
| `render_garmin` | A payload ready for the Garmin MCP's `upload_workout`. Optional `out_path`. |
| `verify_garmin_upload` | Compares a sent payload against what `get_workout_by_id` returns. Pure. |
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

## Garmin schema provenance

This is the part most likely to drift, so here is exactly what it was derived
against and how.

**Derived 2026-08-12**, against the live Garmin Connect API via
[`Taxuspt/garmin_mcp`](https://github.com/Taxuspt/garmin_mcp) 0.1.0 and
`garminconnect` 0.3.10.

**Reference workout: id `1662651131`.** Hand-built in the Garmin Connect web UI
to exercise as many constructs as possible, with known inputs. Losing it means
re-deriving the schema from scratch. There is no rename or edit-in-place in the
API — the operations are upload (creates new), delete, and schedule — so it is
left as is.

Two findings, both established by reading that workout's **raw** API response
rather than the MCP's curated projection:

### 1. Cycling watt targets are target type id 2, not id 6

The Garmin MCP's own `upload_workout` docstring says cycling watt ranges use
`workoutTargetTypeId: 6` / `"power.between"`. **Against this API version that is
wrong, and wrong silently.** Id 6 uploads without error, and Garmin normalises
it to the key `"pace.zone"` on a cycling workout — a pace target, not a power
one. Confirmed by upload/fetch probe.

Id 2 with key `"power.zone"` and `targetValueOne`/`targetValueTwo` round-trips
with the watts intact, and is byte-for-byte the shape the Garmin web UI produces
for a watt target. That is what this renderer emits.

### 2. %FTP is the same target type plus a unit object

The curated read shows an absolute watt range and a %FTP range identically —
both as `power.zone` with a low/high pair, with nothing to tell them apart. The
raw response distinguishes them:

| Entered in the UI as | `targetType` | values | `targetValueUnit` |
|---|---|---|---|
| 200–220 watts | id 2 `power.zone` | 200, 220 | `null` |
| 95–111 % FTP | id 2 `power.zone` | 95, 111 | `{"unitId": 253, "unitKey": "percent", "factor": 1.0}` |

So the %FTP encoding, left open in the original brief, is
`targetValueUnit: {"unitId": 253, "unitKey": "percent", "factor": 1.0}` on an
otherwise ordinary id-2 power target. **The curated read drops that field**,
which is precisely why it cannot confirm units, and why a round-trip test that
compares against the curated shape would pass while the units were wrong.

This renderer emits watts (no unit object), because absolute watts are
unambiguous and always correct. Percentage targets would only be worth pursuing
if you wanted targets that follow a changing FTP.

### 3. Derived fields are not evidence

`estimated_duration_seconds` is computed by Garmin's own rules and disagrees
with the arithmetic — the reference reports 5400 s against 5700 s of steps. It
is never sent and never used for verification. `estimated_distance_meters` and
`avg_training_speed_mps` are absent on freshly uploaded workouts, so their
absence proves nothing either.

### Verification status

The round-trip test **passes**. It renders a workout exercising every construct,
uploads it, fetches it back, compares against **what was sent** (not against the
curated read), asserts no power target was stored as a percentage, and deletes
the workout afterwards.

Visual confirmation in Garmin Connect is the one thing automation can't do —
the API can accept and echo a structure that still displays oddly.

## Skills

Two bundled skills in [`.claude/skills/`](.claude/skills), each triggering on
descriptions of a session — "create", "add", "send", "put it on" — not only on
"upload".

- **[`garmin-upload`](.claude/skills/garmin-upload/SKILL.md)** — renders, uploads
  via the Garmin MCP's `upload_workout`, then verifies by fetching the workout
  back and comparing it against what was sent, rather than trusting that the
  call returned success. Offers to schedule it.
- **[`mywhoosh-upload`](.claude/skills/mywhoosh-upload/SKILL.md)** — drives the
  MyWhoosh builder through Claude in Chrome, because there is no API. It reads
  MyWhoosh's FTP out of the builder before rendering, so the fractions in the
  `.zwo` are right by construction. Each step states what it expects to see, so
  a run that breaks after a MyWhoosh redesign reports which assumption failed
  instead of silently producing nothing.

### Two ways a skill runs

The same `SKILL.md` reaches a client through one of two mechanisms, and they
differ in **who decides to run it**.

**As a skill — model-invoked.** The client reads the skill's `description` and
reaches for it when your request matches: "put this on my Garmin" pulls in
`garmin-upload` without you naming it. This is what Claude Code does with
`.claude/skills`, and what an uploaded skill does in Claude Desktop and
claude.ai.

**As an MCP prompt — user-invoked.** The server registers every bundled skill as
a prompt of the same name, carrying the same instructions, so clients that don't
read `.claude/skills` can still run them. You pick it from the client's prompt
menu; the model will not reach for it on its own. Each takes an optional
`session` argument, so you can describe the workout up front instead of being
asked.

The two coexist: prompts always work because they travel with the server, and
installing the skills properly on top adds the model-invoked trigger.

**What each skill needs to actually run.** A skill triggers on description alone,
but it can only finish if its dependencies are present:

| Skill | Needs |
|---|---|
| `garmin-upload` | this server + the Garmin Connect MCP |
| `mywhoosh-upload` | this server + browser control (Claude in Chrome) |

So the Garmin path is portable to any client with both MCP servers connected,
while the MyWhoosh path only works where a browser is drivable. In a client
without browser tools the MyWhoosh skill will trigger and then have no way to
drive the page.

**The MyWhoosh export spends a finite slot credit**, so that skill stops and
asks for explicit confirmation before exporting, and uses the pause to settle
any open question about the session. It then confirms the workout actually
appears in My Workouts and that the slot counter decremented — the difference
between "clicked the button" and "the workout exists".

## Tests

```bash
pip install -e ".[dev]" && pytest
```

126 offline tests: watt↔fraction conversion in both directions, duration totals,
NP/IF/TSS, both renderers against golden files, XML that actually parses, the
round-trip comparison against a recorded real API exchange, and the skill
frontmatter that decides whether a skill is ever reached for.

The golden fixture is the 70-minute sweet-spot session in the spec example
above. Imported into MyWhoosh it reported **70:00 / 70 TSS / 0.78 IF**; the
model here gives 70:00 / TSS 70.0 / IF 0.775, so the metrics are pinned to a
real measurement rather than to themselves.

The live Garmin round-trip is deselected by default:

```bash
pip install -e ".[live]" && pytest -m live
```

It needs the Garmin tokens the Garmin MCP already stores (`GARMINTOKENS`, or
`~/.garminconnect`). It creates a workout named `ClaudeCyclingMCP_test_*` and
deletes it afterwards, including on failure. No credentials live in this repo.

## Development

```bash
uv venv && uv sync --extra dev
ruff check . && ruff format --check . && pytest
```

| | |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | how to propose a change, and the two project-specific rules |
| [CLAUDE.md](CLAUDE.md) | architecture, and the platform quirks that are load-bearing |
| [docs/versioning.md](docs/versioning.md) | SemVer policy, the release process, publishing setup |
| [CHANGELOG.md](CHANGELOG.md) | what changed, and what was verified against the live API |
| [SECURITY.md](SECURITY.md) | why the server holds no credentials, and where they do live |

## License

MIT.

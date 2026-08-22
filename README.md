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
- 🔐 **No credentials, ever** — the server never uploads and holds no tokens. Its only side effects are its own database and the file you ask for with `out_path`.
- 🧩 **Bundled skills** — Garmin upload-and-verify, a browser-driven MyWhoosh import that reads MyWhoosh's own FTP before rendering, and a generic cycling-coach procedure.

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

Three bundled procedures in [`.claude/skills/`](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/.claude/skills), triggering on how someone
actually describes what they want — "create", "add", "send", "put it on", "what am I doing this
week" — not only on "upload":

- **[`garmin-upload`](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/.claude/skills/garmin-upload/SKILL.md)** — renders, uploads via the Garmin MCP, then fetches the workout back and compares it against what was sent. Offers to schedule it.
- **[`mywhoosh-upload`](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/.claude/skills/mywhoosh-upload/SKILL.md)** — drives the MyWhoosh builder through Claude in Chrome, since there is no API. It reads MyWhoosh's FTP out of the builder *before* rendering, so the fractions are right by construction, and stops for explicit confirmation before the export — which spends a finite slot credit.
- **[`coaching`](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/.claude/skills/coaching/SKILL.md)** — how to coach with the tools below: the onboarding interview driven by whatever the profile is still missing, the weekly loop (read reality, compare to plan, then write it), and the adaptation rules that make it a plan rather than a template. Generic — it carries no athlete's facts.

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

## Documentation

- [Spec format](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/spec-format.md) — every field, the block types, and how FTP is consumed by each platform.
- [Tools](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/tools.md) — the full tool reference, plus the `.zwo` and Garmin format rules the renderers are pinned to.
- [The coach layer](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/coaching.md) — the database, the model-mediated ingestion flow, and every formula with its limits.
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

# ClaudeCyclingMCP

**Describe a structured cycling session once. Get a valid MyWhoosh `.zwo` and a valid Garmin
Connect workout out of it.**

```
spec (JSON) ──┬── render_zwo      → .zwo   (MyWhoosh)
              └── render_garmin   → .json  (Garmin upload_workout payload)
```

An MCP server that turns one hand-readable workout spec into files both platforms accept, and
then **checks that what the platform stored is what you sent**. It is pure and deterministic —
no network, no auth, no credentials. Uploading is a separate, human-in-the-loop step that ships
as [skills](#skills).

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
- 🔐 **No credentials, ever** — the server never uploads and holds no tokens. Writing a rendered file when you pass `out_path` is its only side effect.
- 🧩 **Bundled skills** — Garmin upload-and-verify, and a browser-driven MyWhoosh import that reads MyWhoosh's own FTP before rendering.

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

Two bundled procedures in [`.claude/skills/`](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/.claude/skills), triggering on descriptions of a
session — "create", "add", "send", "put it on" — not only on "upload":

- **[`garmin-upload`](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/.claude/skills/garmin-upload/SKILL.md)** — renders, uploads via the Garmin MCP, then fetches the workout back and compares it against what was sent. Offers to schedule it.
- **[`mywhoosh-upload`](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/.claude/skills/mywhoosh-upload/SKILL.md)** — drives the MyWhoosh builder through Claude in Chrome, since there is no API. It reads MyWhoosh's FTP out of the builder *before* rendering, so the fractions are right by construction, and stops for explicit confirmation before the export — which spends a finite slot credit.

Each step states what it expects to see, so a run that breaks after a platform redesign reports
which assumption failed instead of quietly producing nothing.

How a skill reaches your client, and what each needs to actually run: [Skills](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/skills.md).

## Documentation

- [Spec format](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/spec-format.md) — every field, the block types, and how FTP is consumed by each platform.
- [Tools](https://github.com/elias-ramzi/ClaudeCyclingMCP/blob/main/docs/tools.md) — the full tool reference, plus the `.zwo` and Garmin format rules the renderers are pinned to.
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

# Testing

The offline gate, the live Garmin round-trip, and what the numbers are pinned against.


```bash
pip install -e ".[dev]" && pytest
```

378 offline tests: watt↔fraction conversion in both directions, duration totals,
NP/IF/TSS, both renderers against golden files, XML that actually parses, the
round-trip comparison against a recorded real API exchange, and the skill
frontmatter that decides whether a skill is ever reached for.

The coach layer adds: the dated-FTP rule (a ride before an FTP change is scored
against the old one), import idempotence on Garmin's own payload shapes, TSS and
CTL/ATL/TSB against numbers worked out by hand from the formulas rather than read
off the implementation, spec validation on save, compliance against a fixture
plan-and-laps pair, and the migration walk from an empty database and from one
left at schema v1.

**Every test that touches the database points `CLAUDE_CYCLING_DB` at a `tmp_path`.**
The suite is hermetic: a run must never create or modify the real
`~/.claude-cycling/coach.db`, which is somebody's actual training log. The stdio
tests pass the same variable into the subprocess.

The golden fixture is the 70-minute sweet-spot session in the
[spec example](spec-format.md). Imported into MyWhoosh it reported **70:00 / 70 TSS / 0.78 IF**; the
model here gives 70:00 / TSS 70.0 / IF 0.775, so the metrics are pinned to a
real measurement rather than to themselves.

The live Garmin round-trip is deselected by default:

```bash
pip install -e ".[live]" && pytest -m live
```

It needs the Garmin tokens the Garmin MCP already stores (`GARMINTOKENS`, or
`~/.garminconnect`). It creates a workout named `ClaudeCyclingMCP_test_*` and
deletes it afterwards, including on failure. No credentials live in this repo.


---

Back to the [README](../README.md).

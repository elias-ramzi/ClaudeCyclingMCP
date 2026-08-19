# Smoke tests

Two checks: the [server](#server-smoke-test) and the [MyWhoosh browser flow](#mywhoosh-dry-run).

## Server smoke test

A copy-paste prompt for checking that a client — Claude Desktop, Claude Code, anything else — has
the server connected and is really calling it. Every expected value below is deterministic, so a
wrong answer is a genuine failure rather than a wording difference.

Run it in a **new chat**, so you get the current connector set rather than one captured earlier.

## The prompt

```text
Use the claude-cycling MCP server for all of this. Do not hand-write any
JSON, and do not upload anything to Garmin or MyWhoosh.

My FTP is 255 W. Build a workout named exactly "Sweet Spot 3x10", with
filename "sweetspot-3x10":

- 20 min ramp from 130 W to 180 W
- 10 min at 232 W
- 5 min at 145 W
- 10 min at 232 W
- 5 min at 145 W
- 10 min at 232 W
- 10 min ramp down from 140 W to 130 W

Then report exactly:
1. The describe_spec table, including the total/NP/IF/TSS summary line.
2. From render_garmin: how many steps, and the targetType object plus
   targetValueOne/targetValueTwo of the first 10-minute effort.
3. From render_zwo: the filename, and the first <SteadyState .../> line
   verbatim.

Render only. Do not upload.
```

## Expected

| Check | Correct answer |
|---|---|
| Summary line | `1:10:00 total · NP 198 W · IF 0.77 · TSS 70 · 772 kJ` |
| Garmin step count | `7` |
| `targetType` | `{"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone"}` |
| `targetValueOne` / `Two` | `227.0` / `237.0` |
| `.zwo` filename | `sweetspot-3x10.zwo` |
| First `SteadyState` | `<SteadyState Duration="600" Power="0.9098"/>` |

The two conversions are the tell: 232 W becomes `0.9098` in the `.zwo` and `227/237` for Garmin.
Neither is guessable, so getting them right proves the tools ran rather than the model improvising.

## Answers that mean something is actually wrong

- **`workoutTargetTypeId: 6` or `power.between`** — the payload was hand-written, not rendered.
  Id 6 uploads without error and Garmin stores it as a *pace* target on a cycling workout.
- **`targetValueOne: 232`** — the ±2% band was not applied, so this is not the renderer's output.
- **`Power="232"` in the `.zwo`** — watts leaked into a file that stores only fractions of FTP.

## If the client says the server is unavailable

Check whether a request ever reached it:

```bash
grep "Message from client" ~/Library/Logs/Claude/mcp-server-claude-cycling.log | tail -3
```

If there is no recent line, nothing was sent and restarting the server will not help — the
connector is not enabled for that conversation. If there are lines but no matching
`Message from server`, the problem is in the server.


## MyWhoosh dry run

Exercises the whole browser flow — login, editor, reading the FTP, import, verification — and
**stops before exporting**, so it spends no slot credit. Run it whenever MyWhoosh changes its UI,
or before relying on the flow for a session that matters.

The session is written in **% of FTP** on purpose. Duration, IF and TSS are then identical whatever
FTP the builder holds, so the expected values below hold without knowing it in advance.

Its durations are also deliberately odd — 18 / 11 / 4 / 9 minutes, totalling 1:08:00. An earlier
version used the same 1:10:00 / TSS 70 as the golden sweet-spot session, and a real run opened an
editor already holding that very workout: the header read 70:00 / TL 70, so the "does Workout Time
match?" check could not tell a successful import from one that did nothing. Odd numbers make the
header diagnostic.

### The prompt

```text
Use the mywhoosh-upload skill, but this is a DRY RUN: do not click
EXPORT TO MYWHOOSH under any circumstances. It spends a slot credit and I
am only testing the automation.

Build this session, named "ZZ Test Session", filename "zz-test-session".
It is written in % of FTP deliberately — read the FTP from the MyWhoosh
builder rather than assuming one:

- 18 min ramp from 51% to 71%
- 11 min at 91%
- 4 min at 57%
- 11 min at 91%
- 4 min at 57%
- 11 min at 91%
- 9 min ramp down from 55% to 51%

Work through the skill and report, step by step:
1. Which page you landed on, and whether you needed me to log in.
2. The /editor/<id> URL you reached.
3. The FTP and weight you read from the builder BEFORE importing, and
   whether they look like real values or the 200 W / 62 kg defaults.
4. Whether the editor already held a workout before you imported, and if so
   its name, Workout Time and Training Load.
5. Whether the import navigated to a NEW /editor/<id>, and that URL.
6. The Workout Time and Training Load AFTER importing, and whether they
   changed from what you recorded in step 4.
7. The slot counter value.

Then STOP. Show me the describe_spec table and say whether the header
Workout Time matches it. Do not export.
```

### Expected

| Check | Expected |
|---|---|
| `describe_spec` | `1:08:00 total · NP 202 W · IF 0.79 · TSS 71` |
| `.zwo` first `SteadyState` | `Power="0.91"` |
| Header **Workout Time** after import | `68:00` |
| Header **Training Load** after import | approximately 71 |
| Header **changed** from the pre-import snapshot | yes — this is the real check |
| Slot counter | **unchanged** — nothing was exported |

### Failure signals

- **`NaN` or `00:00` in the header.** The import silently failed. This is the most important check
  in the flow: everything downstream looks fine while the workout is empty.
- **The header is unchanged from before the import.** The import did nothing. This matters more
  than the absolute numbers: if the editor was already holding a workout of similar length, an
  unchanged header is the only signal that anything went wrong.
- **Workout Time does not match `describe_spec`.** Blocks were dropped on import.
- **Step 3 reads exactly 200 W / 62 kg.** Those are MyWhoosh's defaults, not the athlete's FTP. The
  skill should say so and ask rather than render against them.
- **A step reporting "expected X, saw Y".** That is the skill working as designed — each step states
  what it expects so a UI change names itself instead of producing silence.

### What it leaves behind

Nothing in **My Workouts** — a session only lands there on export, which this test never performs,
so the slot counter must be untouched. It does create editor drafts at `/editor/<id>`; whether
those persist visibly in the MyWhoosh UI has not been checked.

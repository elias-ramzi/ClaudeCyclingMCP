# Client smoke test

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

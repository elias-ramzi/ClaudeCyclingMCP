# Skills

The bundled procedures: what each does, how it reaches a client, and what it needs to actually run.


Five bundled skills in [`.claude/skills/`](../.claude/skills), each triggering
on how the request actually arrives — "create", "add", "send", "put it on",
"what am I doing this week", "the power on that ride is wrong" — not only on
"upload".

- **[`garmin-upload`](../.claude/skills/garmin-upload/SKILL.md)** — renders, uploads
  via the Garmin MCP's `upload_workout`, then verifies by fetching the workout
  back and comparing it against what was sent, rather than trusting that the
  call returned success. Offers to schedule it.
- **[`mywhoosh-upload`](../.claude/skills/mywhoosh-upload/SKILL.md)** — drives the
  MyWhoosh builder through Claude in Chrome, because there is no API. It reads
  MyWhoosh's FTP out of the builder before rendering, so the fractions in the
  `.zwo` are right by construction. Each step states what it expects to see, so
  a run that breaks after a MyWhoosh redesign reports which assumption failed
  instead of silently producing nothing.
- **[`mywhoosh-activity-import`](../.claude/skills/mywhoosh-activity-import/SKILL.md)** —
  the other direction: the ride that happened, not the session that was planned.
  When an indoor ride is recorded by MyWhoosh *and* by a head unit, it finds the
  MyWhoosh file, then decides **with evidence** whether the Garmin copy is
  actually bad — a calibration gap between two power meters looks nothing like a
  dropout in the time-in-zone distribution, and only the second justifies
  replacing anything. Deleting a Garmin activity is permanent, so it prefers
  annotating the bad one and gates deletion on explicit consent.
- **[`coaching`](../.claude/skills/coaching/SKILL.md)** — how to coach an athlete
  with this server's [coach layer](coaching.md): the onboarding interview,
  driven by whatever the profile is still missing rather than by a hardcoded
  script; the weekly loop of reading reality before asking about it, comparing
  it to the plan, then writing the next week; and the adaptation rules that
  make it a plan rather than a template. It is generic — no athlete's facts are
  in it — and it always proposes rather than pushing.
- **[`nutrition`](../.claude/skills/nutrition/SKILL.md)** — how to coach eating
  with this server's [nutrition layer](nutrition.md): seeding a food base from
  whatever the athlete already tracks, the daily logging loop where "can I eat
  X?" is answered by subtraction rather than by a yes or a no, and the fuelling
  rules around big sessions and races. Equally generic — the athlete's own
  foods, portions and preparation quirks belong in their database, where they
  can be corrected, not in a skill that ships to everyone.

## Two ways a skill runs

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
| `mywhoosh-activity-import` | the Garmin Connect MCP + a shell to read the `.fit`; `delete_activity` / `upload_activity` for the replacement half |
| `coaching` | this server + the Garmin Connect MCP (to read the athlete's data) |

So the Garmin path is portable to any client with both MCP servers connected,
while the MyWhoosh path only works where a browser is drivable. In a client
without browser tools the MyWhoosh skill will trigger and then have no way to
drive the page.

`mywhoosh-activity-import` is the one that needs no rendering at all — it reads
a `.fit` off disk — but it does need the Garmin MCP to carry `delete_activity`
and `upload_activity` to finish the job. Without them the diagnosis still runs
in full and the last two moves are handed to the user, which the skill says up
front rather than at the end.

**The MyWhoosh export spends a finite slot credit**, so that skill stops and
asks for explicit confirmation before exporting, and uses the pause to settle
any open question about the session. It then confirms the workout actually
appears in My Workouts and that the slot counter decremented — the difference
between "clicked the button" and "the workout exists".


---

Back to the [README](../README.md).

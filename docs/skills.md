# Skills

The bundled upload procedures: what each does, how it reaches a client, and what it needs to actually run.


Three bundled skills in [`.claude/skills/`](../.claude/skills), each triggering
on how the request actually arrives — "create", "add", "send", "put it on",
"what am I doing this week" — not only on "upload".

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
- **[`coaching`](../.claude/skills/coaching/SKILL.md)** — how to coach an athlete
  with this server's [coach layer](coaching.md): the onboarding interview,
  driven by whatever the profile is still missing rather than by a hardcoded
  script; the weekly loop of reading reality before asking about it, comparing
  it to the plan, then writing the next week; and the adaptation rules that
  make it a plan rather than a template. It is generic — no athlete's facts are
  in it — and it always proposes rather than pushing.

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
| `coaching` | this server + the Garmin Connect MCP (to read the athlete's data) |

So the Garmin path is portable to any client with both MCP servers connected,
while the MyWhoosh path only works where a browser is drivable. In a client
without browser tools the MyWhoosh skill will trigger and then have no way to
drive the page.

**The MyWhoosh export spends a finite slot credit**, so that skill stops and
asks for explicit confirmation before exporting, and uses the pause to settle
any open question about the session. It then confirms the workout actually
appears in My Workouts and that the slot counter decremented — the difference
between "clicked the button" and "the workout exists".


---

Back to the [README](../README.md).

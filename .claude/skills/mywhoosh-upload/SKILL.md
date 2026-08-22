---
name: mywhoosh-upload
description: >-
  Build a structured cycling workout and put it into MyWhoosh. Use this whenever
  someone describes a bike session — intervals, sweet spot, threshold, VO2,
  endurance, a ramp test — and wants it created, added, sent, imported, put on,
  or loaded into MyWhoosh, their MyWhoosh library, or "the trainer app" for an
  indoor ride. Trigger on any of "create a workout in MyWhoosh", "add this to
  MyWhoosh", "send it to MyWhoosh", "put it on the trainer", "make me a .zwo",
  not only on the word "upload". Drives the MyWhoosh workout builder through
  Claude in Chrome, because MyWhoosh has no usable API.
---

# Getting a workout into MyWhoosh

MyWhoosh has no API. The only supported route is the browser flow below, driven
through Claude in Chrome. Everything here was confirmed in a real session.

**Two things make this different from an ordinary browser task:**

1. **`EXPORT TO MYWHOOSH` spends a slot credit.** Credits are finite and there
   is no undo. You must stop and get explicit confirmation before clicking it.
2. **It is UI automation, so it will rot** when MyWhoosh redesigns something.
   Each step below says what you should expect to see. If what you see doesn't
   match, **stop and report which step's expectation failed** and what was on
   screen instead. Do not improvise around a mismatch — a stray click on the
   chart creates phantom blocks in the workout.

## Why the FTP has to come first

A `.zwo` stores power only as **fractions of FTP**. The watts actually ridden
are `fraction × the FTP MyWhoosh has`. So if the session is "3 x 10 min at
232 W" and you render against 255 W but MyWhoosh is set to 200 W, every target
is ridden at 182 W — about 22% too easy — and **nothing anywhere reports a
problem**. The file is valid; it is just scaled to a different athlete.

**Unless the session is written in percentages.** If every block is
`power_pct`, the percentages *are* the intent: they render to the same fractions
under any FTP, so the builder's FTP changes only the watts displayed and the
TSS/IF figures — nothing stored is rescaled. Still read it and still report a
disagreement, but do not halt over one. The FTP is load-bearing exactly when the
athlete gave watts.

`get_cycling_ftp` reads **Garmin's** profile, which is a different number and
frequently stale. It is a starting point, not the answer.

Since you are opening the browser anyway, **read MyWhoosh's own FTP out of the
builder and render against that** (Step 3). That makes the fractions correct by
construction, rather than correct by assumption.

Consequence for ordering: draft the session first, but **do not render until
after Step 3**. If the athlete gave targets in watts, those watts are the fixed
intent, and the FTP is what converts them.

## Before touching the browser

1. Draft the session with the athlete and agree the structure — durations, and
   targets in whatever unit they think in. If they gave absolute watts, keep
   them as watts (`power_w`); that is the intent to preserve.
2. Get a provisional FTP from `get_cycling_ftp` (Garmin MCP). Note whether it
   reports `is_stale`. Treat it as a cross-check for Step 3, not as the value
   to render against.
3. Decide the library name. **MyWhoosh takes the workout's name from the
   uploaded filename, not from the `<name>` tag inside the file.** The filename
   is what the athlete will see, so set the spec's `filename` field.

## Sessions

`event.mywhoosh.com` and `workout.mywhoosh.com` are **separate sessions**. Being
logged into one does not log you into the other. The builder is
`workout.mywhoosh.com`.

## Step 1 — Open the builder

Navigate to `https://workout.mywhoosh.com`.

*Expect:* either the workout dashboard (already signed in) or a login page.

**If a login page appears, hand off to the human.** It has a password field and
a reCAPTCHA. Do not fill in the password and do not attempt the CAPTCHA. Say
plainly: "MyWhoosh needs you to sign in — the page is open, tell me when you're
through." Then wait.

## Step 2 — Reach the editor

Path: **Workouts card → Explore → Create New**.

Locate each control with `find` and click the returned `ref`. **Never click by
coordinate.** The page reflows between screenshot and click, and a stray click
lands on the chart and creates a phantom block.

- `find` "Workouts card" → click its ref.
- `find` "Explore button" → click its ref.
- `find` "Create New button" → click its ref.

*Expect:* the URL becomes `workout.mywhoosh.com/editor/<id>`.

**The first click may not navigate.** Observed: clicking Explore left the page
on the landing view, and re-running `find` and clicking the returned ref worked.
Re-locate and retry once before treating it as a failure.

**Do not expect an empty chart.** "Create New" has been observed opening an
editor that already holds a previously edited workout, blocks drawn and header
populated. That is not a failure and not a reason to stop.

**Before importing, write down what is already there:** the workout name, and
the header's `Workout Time` and `Training Load`. Step 6 compares against that
snapshot. Without it, an import that silently does nothing looks identical to
one that worked — and if the loaded workout happens to have the same duration
as the session you are building, the duration check proves nothing at all.

*If a label has changed:* report which of the three you could not find. These
are located by their visible text, so a renamed button is the likely cause.

## Step 3 — Read MyWhoosh's FTP, then render against it

You are in the editor and have not imported anything yet. The FTP and weight
fields on this page are what MyWhoosh is currently working from — read them
**before** importing, because import sometimes overwrites them with defaults
(see Step 5) and you need the originals to restore.

Read the values directly rather than from rendered text, which is easier to
misparse:

```javascript
[...document.querySelectorAll('input')]
  .map((i, index) => ({index, name: i.name || i.id || i.getAttribute('placeholder'), value: i.value}))
  .filter(x => x.value !== '');
```

*Expect:* an FTP in watts and a weight in kg — **each appearing twice**, e.g.
`255, 72, 255, 72, ...`. That duplication is the same one Step 5 warns about
(`find` returns two sets of refs, the second is the live one). `name`, `id` and
`placeholder` all come back `null` on these fields, so the index is the only
handle you have — which is why the snippet reports it.

Agreement between the two copies is the normal case and means nothing is at
risk. **If they disagree, use the second**, for the same reason Step 5 writes to
the second. Say which you saw either way.

**Then judge what you got, out loud.** Which of the two branches below applies
depends on how the athlete wrote the session, so settle that first:

- **Targets in watts** → the FTP is load-bearing. It is the number that converts
  those watts into the fractions actually stored, so a wrong one silently
  rescales the whole session. Work through the cases below and resolve any
  disagreement before rendering.
- **Targets in percentages** → the FTP only moves displayed watts and the
  TSS/IF figures; the stored fractions are identical under any FTP. Still read
  it, still report a disagreement, but do not halt over one.

The cases:

- **A plausible athlete FTP** (not 200) → render against it.
- **Exactly 200 W / 62 kg** → these are MyWhoosh's defaults. That is not
  evidence of the athlete's FTP, it is evidence the field was never set. Do not
  render against 200 just because it was on screen. Say so and ask.
- **It disagrees with Garmin's `get_cycling_ftp`** → report both numbers and ask
  which is current. Do not silently prefer either. A stale Garmin value and a
  stale MyWhoosh value are both plausible.
- **Both read exactly 200** → this is not corroboration, and it is the one case
  where agreement is evidence of nothing. 200 W is MyWhoosh's default *and* a
  common stale Garmin entry; two independent defaults colliding looks identical
  to two sources confirming each other. Observed on 2026-08-19, when Garmin
  returned 200 W (`is_stale: true`, MANUAL, 2025-01-18) while the builder held
  the athlete's real 255 W. Ask.

**Known limitation, worth stating rather than papering over:** this reads the
*builder's* FTP field. Whether that field is populated from the athlete's
MyWhoosh profile, or is a local preview knob that merely defaults to 200, has
not been verified against the game app. So treat it as strong evidence, not
proof — and when the number matters and anything looks off, ask the athlete
what their MyWhoosh FTP is. They know, and it costs one question.

Now render:

1. Set `spec.ftp` to the FTP you settled on. Keep the athlete's watt targets as
   watts — the renderer converts them to fractions against this FTP, which is
   exactly the conversion that has to be right.
2. Call `describe_spec` and **show the block table to the user.** The watts in
   that table are what they will ride. This is the moment a wrong FTP is
   visible and cheap to fix.
3. Call `render_zwo` with `out_path` set — for example
   `~/workouts/<filename>.zwo`. **Always keep the file on disk**, so a failed
   export or a later re-upload does not need re-rendering. `out_path` resolves
   on the machine running the MCP server, not wherever you are running; if the
   write fails, the error names that machine's home directory.

**Import exactly what was rendered — never a version you retyped or corrected.**
If the XML looks wrong to you, say so and stop; do not fix it in flight. A run
on 2026-08-19 read the returned `<name>` tag as `<n>` — a display artefact, not
the file — and imported a hand-built substitute, leaving the file on disk and
the file in MyWhoosh no longer identical. That divergence is invisible
afterwards and defeats the point of keeping the file at all.

*Sanity check before importing:* pick one block and confirm
`watts ÷ spec.ftp` equals the fraction in the rendered file. If the session
says 232 W at FTP 255, the file should read `Power="0.9098"`.

## Step 4 — Import the .zwo

**Do not build the workout by hand in the graphical editor.** The per-block
tooltip is clipped by the chart container and resizing blocks needs
pixel-accurate drags. Import the file instead.

`file_upload` does **not** work here — it rejects filesystem paths. Construct
the file in-page with `javascript_tool` and dispatch a change event.

**Paste `xml_js_literal` from `render_zwo`, not `xml`.** It is the same file
already escaped as a JavaScript string literal. Interpolating the raw XML into a
template literal breaks on a backtick or a `${` in a workout name or message —
text the athlete supplies, so it is reachable.

There are several file inputs including a hidden one, and it is not obvious
which is live. **Fire into one at a time**, checking the URL between each, and
stop at the first that navigates. Run this once with `n = 0`, then `n = 1`, and
so on until it navigates or you run out of inputs:

```javascript
const n = 0;
const zwoText = "<paste xml_js_literal here, quotes included>";
const inputs = [...document.querySelectorAll('input[type=file]')];
const dt = new DataTransfer();
dt.items.add(new File([zwoText], 'session-name.zwo', {type: 'application/xml'}));
inputs[n].files = dt.files;
inputs[n].dispatchEvent(new Event('change', {bubbles: true}));
({inputs: inputs.length, fired: n, href: location.href});
```

Then read the page: if the URL is a new `/editor/<id>`, you are done — do not
fire into the remaining inputs.

**Do not loop over all of them in one call.** Doing so has been observed
**wedging the page**: on 2026-08-19 the call never returned, CDP timed out at
45 s, and the tab stayed unresponsive for another ~25 s before recovering with
the import not applied. Three concurrent imports into the same React app is the
likely cause.

Use the exact filename from `render_zwo` — it becomes the library name.

*Expect:* the page navigates to a **new** `/editor/<id>` and the chart now shows
the workout's blocks.

**If the call times out, it has told you nothing.** A timed-out
`javascript_tool` is not evidence the import failed *or* succeeded — the page
state is simply unknown. Wait for the tab to respond, re-read it, and compare
against the Step 2 snapshot. Never re-fire the import to "make sure"; that is
the move most likely to do damage.

*If the URL doesn't change and `Workout Time` / `Training Load` still match the
Step 2 snapshot:* the import didn't take. (Do not test for an *empty* chart —
Step 2 says the editor often opens with a previous workout already in it, so the
chart is rarely empty and that test would never fire.)

## Step 4a — If the import didn't take

Recover once, then stop. **Nothing in this recovery costs a credit** — credits
are spent only by `EXPORT TO MYWHOOSH` — so a clean retry is free, and stopping
dead at a recoverable failure helps nobody.

Prefer a **fresh editor** over retrying in place: re-firing into an input that
already holds the file is the genuinely risky move.

1. Navigate to `/workouts-library`.
2. Click **Create New** again for a new `/editor/<id>`.
3. Take a fresh Step 2 snapshot, then re-run the import above.

If the second attempt also fails, stop and report both attempts. Do not go
round a third time, and **do not fall back to building blocks by hand** — the
graphical editor is why this flow imports a file in the first place.

## Step 5 — Restore FTP and weight

**Import may reset FTP and weight to defaults (200 W / 62 kg) — re-read both
and restore only what actually changed.** It has been observed both ways: reset
on 2026-08-12, and left at the athlete's 255 W / 72 kg on 2026-08-19. So verify
rather than assume. Writing a value that is already correct is harmless in
principle but this is a page where every stray interaction is a risk, so don't.

If a value did change, put back what you read in Step 3 — you already have it,
so this is restoring a known state, not guessing at one. If the FTP you settled
on differs from what Step 3 read (because it was the 200 W default, or the
athlete corrected it), set the settled value.

The power fractions inside the file stay correct regardless — this field is a
preview setting, so it changes what the athlete reads off the screen, not what
is stored.

It still matters: the duration, TSS and IF you check in Step 6 and report in
Step 7 are computed against this number, so leaving it at 200 W means
sanity-checking the session against the wrong athlete. The watts shown here
should now agree with the `describe_spec` table from Step 3; if they don't, the
FTP in the field and the FTP you rendered against have diverged — stop and
resolve that before exporting.

Re-running Step 3's snippet here also returns a new row at index 0 —
`contained-button-file` holding `C:\fakepath\<name>.zwo`. That is expected
after an import, and its presence is weak corroboration that the file input
took. It is not one of the duplicated FTP/weight rows.

Set both with `form_input`. **Do not use `triple_click` + `type`** — typing does
not stick on these number fields.

`find` returns **two sets of refs** for these fields. **The second set is the
live one.** Use it.

*Expect:* the fields show the FTP you set and the athlete's weight, and the
displayed watts update to match.

## Step 6 — Verify the import actually worked

Check the header: it shows **`Workout Time`** and **`Training Load`**.

- *Expect:* a real duration matching the session, and a plausible Training Load.
- **`NaN` or `00:00` means the import silently failed.** Stop and report it. Do
  not proceed to export — that would spend a credit on a broken workout.

**Do not eyeball this.** Call `verify_mywhoosh_import` with the spec, the
header's `Workout Time` and `Training Load`, and the Step 2 snapshot as
`before_workout_time` / `before_training_load`. It does all three checks at
once — duration against the rendered session, Training Load against our TSS, and
the header against the snapshot — and returns `safe_to_export`.

**If `safe_to_export` is false, do not export.** Report its `problems` verbatim.
Its `warnings` are worth reading out but do not block.

**If the tool is unavailable — timeout, or the server is not answering — do not
export either.** The header alone is not sufficient evidence; that is the whole
reason this check exists, and a populated editor showing plausible numbers is
exactly what a silent no-op looks like. Nothing before `EXPORT TO MYWHOOSH`
costs a credit, so waiting is free and exporting on a guess is not. Say the
check could not be run, leave the editor as it is, and resume once the server
is back — the editor state and the `.zwo` on disk both survive.

Worth knowing before you diagnose: no tool on this server has ever taken more
than a few milliseconds. A timeout is a broken connection, not a slow
computation, so retrying the same call immediately is unlikely to help and
restarting the MCP server usually is.

The snapshot argument is the one that matters most: if the name, `Workout Time`
and `Training Load` are all unchanged from before the import, the import did
nothing — regardless of whether those numbers happen to match your session. That
check, not the duration, is what distinguishes a successful import from a silent
no-op, and it is why Step 2 insists on writing the numbers down.

## Step 7 — Stop and confirm before exporting

**`EXPORT TO MYWHOOSH` spends a slot credit. Credits are limited and the counter
is top-right. Do not click it without explicit confirmation.**

This pause is also the last free moment to change anything, so use it. Tell the
user:

- the workout name (i.e. the filename) as it will appear in their library
- duration, IF and TSS from `describe_spec`
- that `verify_mywhoosh_import` returned `safe_to_export`, and any warnings it
  raised
- the current slot counter value
- any open question about the session

Then ask directly whether to export. **Wait for a clear yes.** If they want
changes, edit the spec, re-render, and start again from Step 4 — do not patch
blocks in the graphical editor.

## Step 8 — Export and confirm it landed

Locate it with `find` and click the returned ref. **`find` returns two refs for
this button, both described as exact matches — use the second**, for the same
reason Step 5 uses the second set for the FTP fields. Confirmed working
2026-08-21: the second ref exported, redirected, and decremented the counter by
one. What the first ref does has never been established, so do not click it to
find out — this is the one irreversible control on the page, and "nothing
happened" is the observation most likely to produce a second click and a second
credit.

*Expect:* a redirect to the workout library, landing on the **Collections** tab.
Switch to **My Workouts** to see the session.

Then verify it actually exists — clicking the button is not the same as the
workout being there:

1. **My Workouts** tab lists the session, with a duration, TSS and IF. Pass
   those to `verify_mywhoosh_library_entry` along with the spec rather than
   comparing them by eye — the credit is already spent, so this is about
   knowing precisely what it bought.
2. The **slot counter has decremented by one**.

**The card's TSS and IF carry a ® — they are TrainingPeaks' metrics, computed
by MyWhoosh's own model.** They will not match Garmin's figures for the same
session, and neither is wrong. Do not report a difference between the two
platforms as a fault.

**The card rounds.** 72.4 shows as `72` and 0.746 as `0.75`, in different
directions. That is why `verify_mywhoosh_library_entry` compares on tolerances
rather than equality.

**The card renders the name with a multiplication sign.** An uploaded
`Tempo-3x14` displays as `Tempo-3×14`, while the editor header showed the ASCII
form — so the substitution happens at export or at library render. Whether the
stored name carries `×` has not been established. `verify_mywhoosh_library_entry`
folds the two together, so this is not a mismatch and should not be reported as
one.

*If the redirect happened but the workout is not in My Workouts:* report that,
and check the slot counter — if it decremented, a credit was spent without a
workout being created, which the user needs to know about.

## Reporting back

Say what landed: the library name, duration, TSS/IF, the new slot count, and
where the `.zwo` is on disk. If anything was assumed — a stale FTP, a duration
you chose — say so.

## If it broke

Report **which step's expectation failed** and what you saw instead. That is the
whole point of the expectations above: a future run that breaks should say
"Step 2: no element matching 'Create New'" rather than silently producing
nothing. Elements located by visible text are called out in each step, so a
renamed label is usually an obvious fix.

The `.zwo` on disk is unaffected by any browser failure — it can be imported by
hand, or the flow retried, without re-rendering.

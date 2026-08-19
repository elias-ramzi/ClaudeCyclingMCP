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
**before** importing, because import overwrites them with defaults.

Read the values directly rather than from rendered text, which is easier to
misparse:

```javascript
[...document.querySelectorAll('input')]
  .map(i => ({name: i.name || i.id || i.getAttribute('placeholder'), value: i.value}))
  .filter(x => x.value !== '');
```

*Expect:* an FTP in watts and a weight in kg.

**Then judge what you got, out loud:**

- **A plausible athlete FTP** (not 200) → render against it.
- **Exactly 200 W / 62 kg** → these are MyWhoosh's defaults. That is not
  evidence of the athlete's FTP, it is evidence the field was never set. Do not
  render against 200 just because it was on screen. Say so and ask.
- **It disagrees with Garmin's `get_cycling_ftp`** → report both numbers and ask
  which is current. Do not silently prefer either. A stale Garmin value and a
  stale MyWhoosh value are both plausible.

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
   export or a later re-upload does not need re-rendering.

*Sanity check before importing:* pick one block and confirm
`watts ÷ spec.ftp` equals the fraction in the rendered file. If the session
says 232 W at FTP 255, the file should read `Power="0.9098"`.

## Step 4 — Import the .zwo

**Do not build the workout by hand in the graphical editor.** The per-block
tooltip is clipped by the chart container and resizing blocks needs
pixel-accurate drags. Import the file instead.

`file_upload` does **not** work here — it rejects filesystem paths. Construct
the file in-page with `javascript_tool` and dispatch a change event. There are
several file inputs including a hidden one; setting all of them is simpler than
identifying the right one:

```javascript
const zwoText = `...contents of the .zwo...`;
const file = new File([zwoText], 'session-name.zwo', {type: 'application/xml'});
const dt = new DataTransfer();
dt.items.add(file);
for (const inp of document.querySelectorAll('input[type=file]')) {
  inp.files = dt.files;
  inp.dispatchEvent(new Event('change', {bubbles: true}));
}
```

Use the exact filename from `render_zwo` — it becomes the library name.

*Expect:* the page navigates to a **new** `/editor/<id>` and the chart now shows
the workout's blocks.

*If the URL doesn't change and the chart stays empty:* the import didn't take.
Report that the change event fired but no navigation followed, and stop — do not
retry blindly, and do not fall back to building blocks by hand.

## Step 5 — Restore FTP and weight

**Import resets FTP and weight to defaults (200 W / 62 kg).** Put back the
values you read in Step 3 — you already have them, so this is restoring a known
state, not guessing at one. If the FTP you settled on differs from what Step 3
read (because it was the 200 W default, or the athlete corrected it), set the
settled value.

The power fractions inside the file stay correct regardless — this field is a
preview setting, so it changes what the athlete reads off the screen, not what
is stored.

It still matters: the duration, TSS and IF you check in Step 6 and report in
Step 7 are computed against this number, so leaving it at 200 W means
sanity-checking the session against the wrong athlete. The watts shown here
should now agree with the `describe_spec` table from Step 3; if they don't, the
FTP in the field and the FTP you rendered against have diverged — stop and
resolve that before exporting.

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

**Compare against the Step 2 snapshot first.** If the name, `Workout Time` and
`Training Load` are all unchanged from what was loaded before the import, the
import did nothing — report that, regardless of whether the numbers happen to
match your session. This check, not the duration, is what distinguishes a
successful import from a silent no-op.

Then cross-check `Workout Time` against the total from `describe_spec`. If they
disagree, something was dropped on import; stop and report both numbers.

## Step 7 — Stop and confirm before exporting

**`EXPORT TO MYWHOOSH` spends a slot credit. Credits are limited and the counter
is top-right. Do not click it without explicit confirmation.**

This pause is also the last free moment to change anything, so use it. Tell the
user:

- the workout name (i.e. the filename) as it will appear in their library
- duration, IF and TSS from `describe_spec`
- the current slot counter value
- any open question about the session

Then ask directly whether to export. **Wait for a clear yes.** If they want
changes, edit the spec, re-render, and start again from Step 4 — do not patch
blocks in the graphical editor.

## Step 8 — Export and confirm it landed

Click `EXPORT TO MYWHOOSH` via `find` → `ref`.

*Expect:* a redirect to the workout library.

Then verify it actually exists — clicking the button is not the same as the
workout being there:

1. **My Workouts** tab lists the session, with a duration, TSS and IF.
2. The **slot counter has decremented by one**.

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

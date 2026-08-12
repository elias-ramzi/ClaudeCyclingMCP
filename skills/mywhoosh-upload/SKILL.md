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

## Before touching the browser

1. Get the FTP. Call `get_cycling_ftp` from the Garmin MCP; if it reports
   `is_stale`, confirm the number with the athlete. Never assume one.
2. Write the spec and call `describe_spec`. **Show the block table to the
   user** and get agreement on the session before automating anything.
3. Call `render_zwo` with `out_path` set — for example
   `~/workouts/<filename>.zwo`. **Always keep the file on disk.** If the export
   fails, or the session needs re-uploading later, the file should already be
   there.
4. Note the returned `filename`. **MyWhoosh takes the workout's library name
   from the uploaded filename, not from the `<name>` tag inside the file.** The
   filename is what the athlete will see. Set the spec's `filename` field to
   control it.

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

*Expect:* the URL becomes `workout.mywhoosh.com/editor/<id>` and an empty
workout chart is visible.

*If a label has changed:* report which of the three you could not find. These
are located by their visible text, so a renamed button is the likely cause.

## Step 3 — Import the .zwo

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

## Step 4 — Re-enter FTP and weight

**Import resets FTP and weight to defaults (200 W / 62 kg).** If you skip this,
the displayed watts and Training Load are wrong. The power fractions inside the
file stay correct regardless — this only affects what's shown, and what the
athlete will read off the screen.

Set both with `form_input`. **Do not use `triple_click` + `type`** — typing does
not stick on these number fields.

`find` returns **two sets of refs** for these fields. **The second set is the
live one.** Use it.

*Expect:* the fields show the FTP you set and the athlete's weight, and the
displayed watts update to match.

## Step 5 — Verify the import actually worked

Check the header: it shows **`Workout Time`** and **`Training Load`**.

- *Expect:* a real duration matching the session, and a plausible Training Load.
- **`NaN` or `00:00` means the import silently failed.** Stop and report it. Do
  not proceed to export — that would spend a credit on a broken workout.

Cross-check `Workout Time` against the total from `describe_spec`. If they
disagree, something was dropped on import; stop and report both numbers.

## Step 6 — Stop and confirm before exporting

**`EXPORT TO MYWHOOSH` spends a slot credit. Credits are limited and the counter
is top-right. Do not click it without explicit confirmation.**

This pause is also the last free moment to change anything, so use it. Tell the
user:

- the workout name (i.e. the filename) as it will appear in their library
- duration, IF and TSS from `describe_spec`
- the current slot counter value
- any open question about the session

Then ask directly whether to export. **Wait for a clear yes.** If they want
changes, edit the spec, re-render, and start again from Step 3 — do not patch
blocks in the graphical editor.

## Step 7 — Export and confirm it landed

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

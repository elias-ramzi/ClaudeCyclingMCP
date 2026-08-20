# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **The README is now an overview, not a manual.** It leads with why the platforms need this —
  both fail silently, in ways a success response does not reveal — then the capabilities, a short
  install, and links out. The reference material it used to carry moved verbatim into
  `docs/spec-format.md`, `docs/tools.md`, `docs/garmin-schema.md`, `docs/install.md`,
  `docs/skills.md` and `docs/testing.md`. No behaviour, no rules, and no numbers changed.
- **README links are absolute GitHub URLs.** `pyproject.toml` publishes the README as the PyPI long
  description, and PyPI does not resolve repo-relative paths — now that the reference lives in
  `docs/`, relative links would have rendered dead on the package page.
- **The `_comment` in `tests/golden/recorded_roundtrip.json`** points at `docs/garmin-schema.md`
  instead of the README for the id-6/`pace.zone` finding, which moved. Comment text only: no
  recorded request, response, or asserted value changed.

### Added

- **`get_skill`** — returns a bundled procedure by name. The skills reach Claude Code through
  `.claude/skills` and other clients through MCP prompts, but neither route lets a *model* retrieve
  a procedure it has just been asked for by name; in Claude Desktop, "use the mywhoosh-upload skill"
  resolved to nothing unless the skill had been uploaded to the account. This closes that gap.
- **A MyWhoosh dry run** in `docs/smoke-test.md` that exercises the browser flow and stops before
  the export, so it spends no slot credit.
- **`verify_mywhoosh_import`** — checks a scraped builder header against the rendered session and
  returns `safe_to_export`. MyWhoosh has no API to read a workout back, so that header is the only
  evidence an import took, and export is irreversible; the skill previously asked an agent to
  eyeball two numbers. It takes the pre-import snapshot as an argument, which is what distinguishes
  a real import from a silent no-op.
- **`check_garmin_payload`** — the payload leaves this server as text in a model's context and
  re-enters as an argument that model composes for the Garmin MCP, so "hand it over unchanged"
  really means retyping ~90 lines of nested JSON. One wrong digit in a `targetValueOne` gives a
  workout that uploads without error, passes `verify_garmin_upload` — which compares Garmin against
  what was *sent* — and is wrong. `render_garmin` now issues a `payload_digest`, and this checks a
  composed payload against it before upload. `verify_garmin_upload` takes the digest too, so the
  round-trip can check both halves at once.
- **A `ui_checklist`** of what each step should read in the Garmin UI, returned by
  `check_garmin_payload`. It is the fallback when `get_workout_by_id` is unavailable, generated
  rather than improvised.
- **`render_zwo` returns `xml_js_literal`**, the same XML pre-escaped as a JavaScript string
  literal. The MyWhoosh flow injects the file into the page, and the documented snippet interpolated
  raw XML into a template literal — so a backtick or a `${` in a workout name or message, both
  athlete-supplied, would break or inject.

### Fixed

- **The MyWhoosh skill no longer assumes "Create New" opens a blank editor.** A real run found it
  opening an editor that already held a previously edited workout, which the skill treated as a
  mismatch and stopped on. It now records the loaded workout's name and header before importing.
- **Import verification compares against that pre-import snapshot.** Checking only that
  `Workout Time` matches the session is blind when the editor already holds a workout of the same
  length — exactly what happened, since the loaded workout read 70:00 / TL 70 against a test session
  computing to the same. An unchanged header now reports the import as a no-op.
- **The dry run's expected `IF` was wrong** (0.77, copied from the watts variant; the %FTP variant
  computes 0.775 and displays 0.78) and its durations have been made deliberately odd so the header
  cannot coincide with an existing workout.
- **`back-merge.yml` pushes to `dev` instead of opening a pull request.** It failed on first use:
  opening a PR from a workflow needs a repository setting that is off by default.
- **A failed `out_path` write now says whose filesystem it is.** `/home/claude` fails on macOS with
  a bare `[Errno 45] Operation not supported`, which tells a caller running elsewhere nothing about
  why. The error now names the server's platform and home directory, and a failed write no longer
  loses the rendered file.
- **The MyWhoosh skill's import step fired into every file input at once**, which was observed
  wedging the page — the call never returned, CDP timed out at 45 s, and the import did not apply.
  It now fires into one input at a time.
- **The skill's import-failure test could never fire.** It looked for an empty chart, while the step
  before it says the editor usually opens with a previous workout already loaded. It now compares
  against the pre-import snapshot, which is the check that works.
- **The skill had no sanctioned recovery from a failed import**, so a free, fully recoverable
  failure ended the flow. A new Step 4a authorises exactly one clean restart in a fresh editor —
  nothing before `EXPORT TO MYWHOOSH` costs a credit.
- **"Import resets FTP and weight" was stated as always true.** It has since been observed leaving
  both untouched. The step now says to re-read and restore only what changed, on a page where every
  stray interaction is a risk.
- **The skill's FTP argument assumed watt-denominated sessions.** When every block is `power_pct`
  the percentages are the intent and render identically under any FTP, so a disagreement is a
  display and TSS question rather than a correctness one — worth reporting, not worth halting for.
- **A Garmin FTP of exactly 200 W agreeing with the builder is not corroboration.** 200 is
  MyWhoosh's default and a common stale Garmin entry, so two independent defaults colliding looks
  identical to two sources confirming each other. The skill now calls this out.
- **The skill says to import exactly what was rendered.** A run misread the returned `<name>` tag as
  `<n>` — a display artefact, not the file — and imported a hand-built substitute, leaving the file
  on disk and the file in MyWhoosh no longer identical. A test now pins the tag so the claim can be
  checked in one command.
- **Guidance for a timed-out browser call**, which the skill had none of. A timeout says nothing
  about whether the action applied, and it is the case most likely to tempt a destructive retry.
- **`verify_mywhoosh_import` reported an unchanged header as changed**, returning
  `safe_to_export: true` on the exact silent no-op it exists to catch. A scraped Training Load
  arrives as text while `training_load` is typed numeric, so the two sides of one reading were
  compared as `"72"` against `"72.0"`. Both are now coerced to numbers, both parameters accept
  either form, and the check reports the `before` and `after` it compared rather than a bare
  boolean. Note the shape of the failure: a caller who followed Step 2 and captured a snapshot got
  false reassurance, while one who skipped it got an honest warning — the careful path was the
  dangerous one.
- **A stdio contract suite** that drives the server the way a client does. Two bugs have now
  shipped past a green in-process suite — this and the payload digest — and both were type
  mismatches that only exist after JSON and pydantic coercion, which calling the Python function
  directly cannot reproduce.
- **A warning when the observed Training Load sits nearer the pre-import value than the session's**
  — independent of the tolerance, and a cheap second signal that the chart on screen is still the
  old workout.
- **`mywhoosh-upload` Step 3 says the FTP and weight fields appear twice**, that agreement is the
  normal case, and that a disagreement means read the second — matching Step 5, which already
  writes to the second. The snippet now reports each field's index, since `name`, `id` and
  `placeholder` are all null on those inputs.
- **CI was red on Windows only.** Tests read files this project writes as UTF-8 using the platform
  default encoding, which is cp1252 on Windows, so the `·` separators and `Récupération` in the
  Garmin UI checklist came back mangled. The written files were always correct; the tests were
  asserting something locale-dependent. Every read and write in the suite now names its encoding,
  one test asserts the bytes rather than the decoded text, and ruff's `PLW1514` is enabled so the
  next one is a lint error rather than a red build on one of nine matrix legs.
- **`server_info`** — version, package path, Python, and the skills served. Four sessions in a row
  either reported findings against a build that no longer existed or could not state their build at
  all; the tool surface dates a session, but only alongside a version and a load path. `package_path`
  also separates a local editable checkout from a `uvx` cache at a glance. It answers instantly and
  touches nothing, so a reply is also proof the server is alive.
- **`mywhoosh-upload` says what to do when `verify_mywhoosh_import` is unavailable**: do not export.
  The header alone is not sufficient evidence — a populated editor showing plausible numbers is
  exactly what a silent no-op looks like — and nothing before the export costs a credit, so waiting
  is free. Both skills now note that no tool here has ever taken more than a few milliseconds, so a
  timeout is a broken connection rather than a slow computation.
- **`payload_digest` rejected correct payloads, blocking every upload.** JSON has one number type,
  so a renderer emitting `227.0` and a model retyping it as `227` produce equal payloads that
  hashed differently — and that retyping is unavoidable, since the payload leaves this server as
  text and re-enters as an argument someone composed. `diff_payloads` already compared numbers by
  value, so one response could carry `matches_spec: true` with an empty diff *and*
  `matches_rendered: false`, which the skill turned into "do not upload". Integral floats are now
  folded before hashing, so the two checks cannot disagree about what "the same payload" means.
  **Only the tampered path had test coverage**, so a digest that rejected everything still passed
  the suite; the identity path is now tested through a JSON round-trip.
- **`render_garmin` returns `ui_checklist`** and writes it beside `out_path`. It is the
  manual-verification fallback for when the Garmin read is unavailable, and it previously required
  another call to this server — no fallback at all when this server is what stopped answering.
- **`garmin-upload` gains a step 0**: confirm the Garmin MCP is connected before rendering. A whole
  session was spent building a workout for a platform that was not there, discovered at step 4.
- **The skill's gate prefers `matches_spec`.** It names the field that moved; the digest only says
  one did. A digest that disagrees with a clean spec diff is now reported as a bug in this server,
  not as a corrupt payload.
- **A repeated block warns once, not once per repetition.** Repeats are flattened for MyWhoosh, so
  a child is emitted `count` times — but it was authored once, and three identical lines is noise
  in the artifact people read. The path now names where the author can find the block
  (`blocks[0].blocks[0]`) rather than which repetition produced it.
- **Warning paths agree between the validator and the renderers.** The validator counted blocks
  from 0 and both renderers from 1, so one warnings list could carry two `blocks[1]` labels meaning
  two different blocks. Everything is now 0-based, matching the JSON array the author wrote.
- **`describe_spec` shows the watt band that will actually ship to Garmin.** A scalar target is
  displayed as `293 W (115%)` but uploads as `287-299 W`, and the block table is where the skills
  say a wrong number is cheap to catch. The column appears only when something would fill it.
- **The schema no longer requires `duration` on a repeat block.** It required it on every block, so
  an author following the schema added one and the server then reported it as a probable typo. The
  warning now says what is actually wrong: a repeat's duration is computed from its contents.
- **`garmin_target_band_pct` is documented as a percentage of the resolved target**, not of FTP,
  with a worked example. It was ambiguous enough that a caller resolved it by experiment.
- **`render_garmin` writes a `.sha256` beside `out_path`.** A later session with none of the
  original context can then tell whether the JSON on disk is still what was rendered.
- **Target bands are chosen by role.** 2% for interval and rest, 5% for recovery, warmup and
  cooldown. ±2% of a 250 W interval is ±5 W, which is right; ±2% of a 140 W recovery is a 6 W
  window that alarms continuously on an easy spin. An explicit `garmin_target_band_pct` still
  applies one number everywhere. **The Garmin golden file changed** — the two recovery steps in
  `sweetspot-3x10.garmin.json` move from 142–148 W to 138–152 W; nothing else.
- **Optional `ftp_source` and `ftp_date`** record where an FTP came from. A workout on a head unit
  is raw watts and a `.zwo` stores only fractions, so neither file says which number produced it.
  Recorded in the `describe_spec` header and in the `.zwo` description. Not added to the Garmin
  payload: its DTO has a description field, but that field has never been verified against the live
  API, and this repo does not put unverified fields in a payload.
- **`verify_garmin_upload` rejects a misused comparison instead of diffing two different shapes.**
  Handed an upload payload where a fetched workout belongs, it reported that Garmin had dropped the
  workout name, the sport and every segment — confident, specific, and entirely an artefact of
  reading DTO keys with curated-read names. The skill's instruction on a mismatch is to *delete the
  workout*, so failing open here destroyed correct work. A comparator that cannot tell it is being
  misused should not be trusted to say when Garmin misbehaves.
- **`check_garmin_payload` takes the spec** and re-renders it internally, so a mismatch names the
  step and field rather than only saying the digest differs. Numbers compare by value, since a
  model retyping a payload will not preserve 245 vs 245.0.
- **A descending ramp warns that its direction is gone.** Garmin ranges are low-first, so 55→45%
  and 45→55% both render as 115-140 W: a backwards cooldown and a correct one produce identical
  payloads, and no round-trip check can tell them apart.
- **`describe_spec` says a ramp flattens on Garmin.** The arrow in the block table reads as a
  sweep, and that table is the artifact the skills tell you to show the athlete.
- **`render_garmin` returns `expected_display`**, giving the visual check a concrete criterion.
  The curated read drops `targetValueUnit`, so a look at the head unit is the only evidence a
  target was stored as watts — which needs a pass/fail rule, not "open it once and see".
- **`render_garmin` warns when a ramp is flattened.** Garmin has no ramp primitive, so a single
  step spanning 130→180 W displays as a band to hold rather than a climb. Nothing in the pipeline
  said so, and it was being explained to athletes from inference.
- **The renderers name the skill in their return value.** Under deferred tool loading a model can
  render for Garmin, never learn the skill exists, improvise the upload, skip verification, and
  "fix" the target type to id 6 on the Garmin MCP's advice — the exact catastrophe the design
  guards against, reached by an entirely mundane route. `get_skill`'s description now carries
  render/upload keywords so it surfaces alongside the renderers.
- **The `garmin-upload` skill has a branch for the fetch being unavailable**, which is a third
  outcome it did not cover and, on 2026-08-19, the one that happened: a four-minute timeout. The
  workout is then uploaded but *unverified* — not successful, and not to be deleted.
- **The skill says to always pass `out_path`**, not "for example". Without it no artifact exists to
  re-upload or diff against, which is only discovered after something has gone wrong.
- **The skill no longer implies a redundant `get_cycling_ftp` call** when the athlete stated their
  FTP in the request. A stated figure beats a profile field.
- **A note that a French Garmin UI labels a cooldown "Récupération"**, the same as a recovery, so a
  session with three recoveries appears to have four. Cosmetic, but it was being explained ad hoc.

## [0.1.0] - 2026-08-12

First release.

### Added

- **One spec, two renderers.** A flat, hand-editable JSON workout spec renders to a MyWhoosh `.zwo`
  and to a Garmin Connect `upload_workout` payload. Power is written in watts or % FTP; the spec's
  `ftp` converts between them.
- **Tools** — `validate_spec`, `describe_spec`, `render_zwo`, `render_garmin`,
  `verify_garmin_upload`, `spec_schema`. All pure and deterministic: no network, no credentials.
  Writing a rendered file when `out_path` is given is the only side effect.
- **`describe_spec`** returns a block table with computed watts, elapsed time, and estimated
  NP/IF/TSS, so a session can be sanity-checked before it is rendered.
- **`verify_garmin_upload`** compares a sent payload against what `get_workout_by_id` returns, so a
  round-trip is checked against what was actually sent rather than against an assumed read shape.
- **Bundled skills** — `garmin-upload` and `mywhoosh-upload`, in [`.claude/skills`](.claude/skills).
  Also registered as MCP prompts, so they reach clients that do not read `.claude/skills`.
- **`mywhoosh-upload` reads MyWhoosh's FTP from the builder before rendering.** A `.zwo` stores only
  fractions, so rendering against the wrong FTP silently rescales every target.

### Garmin schema, established against the live API

- **Cycling watt targets use `workoutTargetTypeId` 2 (`power.zone`) with `targetValueOne`/`Two`** —
  not id 6 / `power.between` as the Garmin MCP's own tool description states. Id 6 uploads without
  error and Garmin normalises it to `pace.zone` on a cycling workout, producing a silently mangled
  workout.
- **%FTP targets are the same id-2 target plus `targetValueUnit`**
  `{"unitId": 253, "unitKey": "percent"}`; watts carry no unit object. The curated read drops this
  field, which is why watts and percentages are indistinguishable through it.
- Repeat groups carry a complete `endCondition` including `conditionTypeId: 7`; omitting the numeric
  id makes the API silently corrupt the repeat count.

### Verified

- Round-trip test uploads a workout exercising every construct, fetches it back, compares against the
  sent payload, asserts no target was stored as a percentage, and deletes the workout.
- Visually confirmed in Garmin Connect that power targets display as watts.
- The golden 70-minute sweet-spot session reproduces the 70:00 / 70 TSS / 0.78 IF that MyWhoosh
  reported for it on import.

### Known limitations

- The `mywhoosh-upload` browser flow is written from a recorded manual session and has not been run
  end to end by an automated agent.
- Whether the MyWhoosh builder's FTP field reflects the athlete's game profile, or is a local preview
  value defaulting to 200 W, is not verified.

[Unreleased]: https://github.com/elias-ramzi/ClaudeCyclingMCP/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/elias-ramzi/ClaudeCyclingMCP/releases/tag/v0.1.0

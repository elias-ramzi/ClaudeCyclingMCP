# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-08-24

### Added

- **A nutrition layer**, in the same database and for the same athlete. An ingredient base (per-100 g
  macros, aliases, habitual portions, raw-versus-cooked state, package price, and a note for the
  athlete's own preparation know-how), standard meals, a per-ingredient food log, dated targets, and
  weekly summaries with food cost. Eighteen new tools — see
  [docs/nutrition.md](docs/nutrition.md).
- **Calorie targets computed from the day's training.** `suggest_targets` chains Mifflin-St Jeor BMR
  (from the weight in effect on that date, plus height, age and gender) x a sedentary baseline, plus
  Garmin's own calories for that date's imported ride — or, for a date still ahead, the planned
  session's mechanical work converted at cycling's gross efficiency — then the active goal's rate at
  7700 kcal/kg spread across the week. Every input and intermediate is in the response. It stores
  nothing; `confirm_targets` files what the athlete agreed to, as `confirmed` or `overridden`.
- **Day type read off the training tables, not asked for.** An event makes its date `race` and the day
  before `race_eve`; a ride of 3 h or 1500 kcal, imported or planned, makes it `big_session`. **On
  those days the goal's deficit is withheld** and the response says how much: under-fuelling a hard
  session costs the session and the recovery from it. Fibre is suggested at 15 g rather than 30 g on
  a race or race eve.
- **Two refusals that hold.** A target below computed BMR is clamped by `suggest_targets` and refused
  outright by `confirm_targets`, whoever asked. An ingredient name resolves on an exact name or alias
  match or is rejected with near-matches — a fuzzy hit taken as exact logs the wrong food and reads
  as a perfectly ordinary day afterwards.
- **A `nutrition` skill**, bundled alongside `coaching` and the two upload flows. Generic — the
  athlete's own foods, portions and quirks live in their database, where they can be corrected.
  Covers seeding a food base from whatever they already track, the daily logging loop where "can I
  eat X?" is answered by subtraction rather than a yes or no, fuelling around big sessions and
  races, and when to stop optimising numbers and recommend professional guidance.
- **`athlete.gender`**, asked for by the nutrition layer alone. Mifflin-St Jeor's male and female
  constants differ by 166 kcal/day, so it is not something to assume; it affects no training number.
- **A coach layer.** The server now keeps the athlete's file in a local SQLite database at
  `~/.claude-cycling/coach.db` (override with `CLAUDE_CYCLING_DB`), created on the first coaching
  call. It holds the profile, append-only dated FTP / weight / HR history, objectives, a normalised
  cache of imported Garmin activities with their raw payloads, optional per-lap splits, and planned
  sessions stored as specs. Twenty-three new tools cover profile and history, events, activities,
  planning, analysis and backup — see [docs/coaching.md](docs/coaching.md).
- **Deterministic training analysis.** `compute_load` (power TSS against the FTP in effect on each
  ride's own date, with an hrTSS fallback that is flagged as one), `get_form` (CTL/ATL/TSB on the
  standard 42/7-day constants, TSB as yesterday's balance), `compliance_report` (planned blocks
  against the executed laps, phrased as sentences), and `get_week` (plan against reality, with the
  deviations both ways).
- **A `coaching` skill**, bundled alongside `garmin-upload` and `mywhoosh-upload`. Generic — it
  carries no athlete's facts. Covers the onboarding interview, driven by whatever the profile is
  still missing rather than by a hardcoded script; the weekly loop; the adaptation rules; and the
  rule that sessions are always proposed, never pushed.
- **`server_info` now reports the database** — path, whether it exists, and its schema version —
  without creating it.

### Changed

- **Schema v4** adds the nutrition tables and `athlete.gender`. Applied automatically on the next
  call; a database from the coach layer alone migrates in place.
- **`export_data` / `import_data` now cover the nutrition tables** too. An export taken before this
  release does not contain them, and restoring one leaves the food base empty rather than corrupt.

- **The purity note is narrower and true.** It said "this server is pure: no network, no
  credentials, no uploads", and the only filesystem access was an `out_path` write. It now says
  "no network, no credentials, no uploads; filesystem access is limited to this server's own
  database and to explicit out_path writes". No network call or credential was added; a note that
  has drifted from the behaviour is worse than no note.
- **`render_garmin`, `render_zwo` and `check_garmin_payload` docstrings** point at the coach layer:
  a stored planned session's spec is directly renderable, `get_week` flags one written against an
  FTP that has since moved, and a verified upload should be recorded with
  `update_planned_workout(status="pushed")`. No rendering behaviour changed.

### Fixed

Found by an adversarial review of the new code before it shipped; each is pinned by a test.

- **A partial `log_hr` entry no longer shadows the figures already on file.** HR resolved the latest
  *row*, so logging a resting HR on its own erased the threshold recorded months earlier — and every
  no-power ride then came back unscored, with the reason "no threshold HR is on file". Each figure
  now resolves independently and keeps the date it came from.
- **A backdated `log_ftp` / `log_weight` / `log_hr` returns the row it actually wrote.** All three
  re-read by date after inserting, so correcting an FTP to three weeks ago returned the *other*
  entry as `stored` while returning zones computed from the new value — a response that contradicted
  itself. They now read back by row id, report `is_current`, and compare `change` against the entry
  the new one replaces rather than the globally latest.
- **A database that cannot be opened is an answer, not a traceback.** `sqlite3.Error` is not an
  `OSError`, so the `StoreError` path was unreachable, and the cleanup `ROLLBACK` raised over the top
  of the real cause — pointing `CLAUDE_CYCLING_DB` at a non-database file reported "cannot rollback"
  instead of "file is not a database".
- **`compliance_report` no longer calls a session `as_prescribed` when a block was cut short.** Only
  wrong-power blocks counted; an interval abandoned half-way at exactly the right watts passed.
  `deviating_blocks` now covers short and long as well, and drives the verdict.
- **`compliance_report` returns the laps on a count mismatch**, as its docstring already promised.
  That is the one case where nothing could be compared and the laps are all the caller has.
- **A thinner re-import cannot reclassify a ride's sport.** `sport` is derived, so it was never null
  and the "a null never overwrites a stored value" rule did not cover it: re-importing a summary
  without a type key rewrote a `virtual_ride` to `"other"`, dropping the ride out of every cycling
  filter while `sub_sport` still said otherwise. A payload with no type now stores a null sport and
  flags `no_sport_type`.
- **`import_data` refuses a malformed row** instead of raising `AttributeError` across the tool
  boundary after its delete sweep.
- **A null in a payload no longer folds to a number in the duration check.** `total_step_seconds`
  treated a missing repeat count as 1 and a missing step duration as 0, so a payload corrupted by
  a dropped `conditionTypeId 7` summed to a plausible smaller total — and when the fetched side
  carried the same null, `verify_garmin_upload` reported a corrupted workout as matching. The
  total is now reported as unknown (`null`), `check_garmin_payload` and `verify_garmin_upload`
  say explicitly what could not be checked, and `ui_checklist` prints `unknown` where it used to
  print a confident `0:00`. A real zero still sums and still prints as a clock.

#### Found in code review of the pull request

Ten confirmed correctness bugs, each reproduced before fixing and pinned by a test.

- **Four tool schemas rejected the numeric forms the coach layer was built to accept.**
  `garmin_activity_id` was typed `str | None` on `link_activity`, `annotate_activity`,
  `import_activity_laps` and `record_race_result`, and `finish_time` as `str | None` — but Garmin's
  `activityId` is a JSON number and pydantic does not coerce one to a string, so those calls failed
  schema validation before any code ran. `record_race_result`'s docstring promised `finish_time`
  takes seconds; that form was unreachable over the wire.
- **`_timestamp` could not read an ISO timestamp with a UTC offset.**
  `"2026-08-20T07:12:33+02:00"` returned `None`, and an activity carrying that form on both start
  times was rejected as having no readable start — a ride lost to a timezone suffix. It now parses
  offsets, keeping the wall clock for a local time and converting for `startTimeGMT`.
- **Weekly and listing totals folded a null TSS to zero and merged power TSS with hrTSS silently.**
  A week with three unscored rides read as a light week, and `get_week` set the merged figure beside
  an always-power-based `planned_tss`. One shared aggregator now reports `scored`, `unscored` and
  `by_method` with both warnings, wherever a total appears.
- **`compliance_report` counted `no_power` blocks as deviations**, so an HR-only ride with the right
  lap count and durations came back `deviated` while the same blocks were also counted as
  unverifiable. They now count toward neither, and a session where every block is unverifiable
  gets its own verdict rather than a guess in either direction.
- **`get_week` reported a cross-window linked ride as unplanned.** `link_activity` permits a Sunday
  session ridden Monday, but only plans scheduled inside the window were consulted — so the ride
  showed as "ridden with nothing planned for it" with the link in the database the whole time.
  Links and event links are now looked up by activity, regardless of date.
- **A ride with an unknown sport could never auto-link and vanished from cycling filters.** NULL
  sport means Garmin sent no activity type, not that the ride was not a bike; `link_activity(auto)`
  reported "no cycling activity stored on that date" for a ride sitting on exactly that date. Auto
  matching now offers unknowns, and a sport filter reports the unknowns it hid instead of dropping
  them in silence.
- **`record_race_result` silently flipped an abandoned or DNS event back to completed.** Adding a
  debrief months later rewrote the outcome. `status` omitted now means "leave it alone"; only an
  event still `upcoming` is completed by filing a result.
- **A future-dated FTP marked the current week's plans stale.** The check took the latest entry with
  no date bound, so a scheduled test result — or a typo — told the coach to rewrite correct specs
  against a number not yet in force. Each session is now checked against the FTP in effect on its
  own date.
- **`compute_load(activity_ids=[])` scored the entire history.** An empty list is a filter that
  matched nothing, not an absent filter; it returned the athlete's whole log as the selection's
  total. It now returns zero rows and says why.
- **An estimated threshold HR hid the `threshold_hr` onboarding gap.** The check ran on the resolved
  figure, which substitutes 92% of max HR, so once a max HR existed the athlete was never asked for
  a measured LTHR again and every hrTSS stayed pinned to the estimate.

Also, from the same review: corrupted stored JSON now reports as an error rather than a polite
refusal; `list_activities` no longer claims `truncated` on an exact fit; `link_activity` reads a
planned duration without expanding a 1 Hz power series; and the `server_info`, `get_skill` and
`compliance_report` docstrings were corrected where they had drifted from the behaviour — including
an explicit note that compliance's 5% tolerance is deliberately not the band `render_garmin` writes
to the head unit.

#### Consolidation, from the same review

Structural cleanups with no intended behaviour change, except where noted.

- **One resolver for every dated figure.** FTP, weight and each HR field went through two diverging
  copies of the same latest-else-earliest rule; there is now one, and a `History` object that loads
  the tiny history tables once per request and resolves in memory. `get_form` over 200 activities
  went from 852 queries to 10, and `compute_load` from 1007 to 10 — measured, and now pinned by a
  test that fails if the count scales with the number of rides. Reads also name their columns, so
  `raw_json` is no longer hauled back for every row of a query that never looks at it.
- **One TSS formula and one duration formatter.** `metrics.py` scored a plan and `training.py`
  scored a ride with separate copies of `duration_h x IF^2 x 100`, and `compliance_report`'s whole
  job is to set those two numbers side by side — two copies that drifted by a rounding choice would
  have made every comparison a report on the arithmetic.
- **One FTP plausibility band.** It was 50–600 in the validator and 40–700/80–500 in the store, so a
  550 W FTP was queried when stored and accepted when rendered. Both now read `FTP_PLAUSIBLE_W` and
  `FTP_USUAL_W` from `spec.py`. The two layers still *act* differently on the same numbers, which is
  the point: the renderer warns, the store refuses to persist. The validator's warning band is
  consequently 80–500 rather than 50–600.
- **The backdated-entry logic is shared by all three loggers.** Only `log_ftp` reported `is_current`
  and what superseded it; logging last month's weigh-in still handed back today's W/kg, and a
  threshold HR from March still returned zones as though they were in force. `log_hr` answers per
  field, because a backdated resting HR supersedes nothing about the threshold.
- **Import data-quality flags are persisted** (schema v3, `activities.flags_json`) and returned as
  `flags` on every read. They were computed at import and reported once, so no later read could see
  that a ride's date came from UTC or its sport was unknown. They are now derived from the **stored
  row** rather than from the payload that arrived — deriving them from the payload would stamp
  `no_normalized_power` on a ride whose NP came from an earlier detailed fetch, which is the mistake
  the null-preserving merge exists to prevent, one column over.
- **Payload errors raise instead of returning `{"ok": False}`.** Those dicts survived only because
  `_coach` spread the result after its own `ok` key; reordering that line would have reported a
  failure as a success. `_coach` now raises if a result carries an `ok` of its own.

The three numeric coercers were **not** merged, despite looking alike: `verify._number` rejects
strings because a string in a DTO is a shape error, `verify._as_number` parses them but must not
touch commas (it reads a page, where "1,234" is one thousand), and `garmin_import._number` reads a
decimal comma because a European-locale export writes 232,5. Folding them together picks one
behaviour for all three. Each now says so, and a test pins the differences.

#### Found in the second review round

Twelve more, each reproduced before fixing and pinned by a test. Four are regressions from the
round-1 fixes above — a generalisation that stopped one line short — and are marked as such.

- **`record_race_result` with no fields crashed on an empty UPDATE.** *(Regression.)* Making
  `status` mean "leave it alone" let `updates` be empty, and unlike every sibling updater there was
  no guard: `UPDATE events SET , updated_at = ?` is not a statement, and `_coach` reported the
  syntax error as database corruption.
- **A thin re-import could move a ride to the UTC day, with the flag suppressed.** *(Regression.)*
  `local_date` was merged as an ordinary field, so a payload with no `startTimeLocal` brought a
  UTC-derived date that overrode the stored one — while `start_time_local` still said otherwise and
  no flag was raised, because the flag reads the start times. A 22:00 UTC-5 ride moved to the next
  day, and the week showed a missed session and an unplanned ride on consecutive days. The date is
  now derived from the merged row by the same function the flag reads.
- **One clean block certified the untestable ones.** *(Regression.)* `unverifiable` was returned
  only when *every* block was, so a warmup with power in front of five no-power intervals came back
  `as_prescribed`. And a `free` block counted as evidence of compliance, so any plan containing one
  could never report `unverifiable` at all. Any unverifiable block now earns the verdict.
- **Duration deviations vanished on blocks with no power.** *(Regression.)* The drift check promoted
  only an already-clean verdict, so an HR-only ride abandoned block by block reported nothing wrong.
  Power and duration are now judged separately — every block carries a `verdict` and a
  `duration_verdict` — because duration is verifiable without a power meter.
- **On Python 3.10 a timestamp could be stored two hours wrong.** `fromisoformat` there wants
  exactly 3 or 6 fractional digits, so `"…33.5+02:00"` fell to the fallback, which truncates at the
  dot and took the UTC offset with it. Wrong and plausible is worse than the rejection this
  replaced, and 3.10 is the declared floor. Reproduced on a real 3.10; the fraction is now padded
  before parsing.
- **`link_activity` silently rewrote a `skipped` session to `completed`.** Linking a ride is
  evidence about the ride, not a reversal of a decision the coach made. It now auto-completes only
  from `planned` or `pushed` and reports when it did not.
- **`compute_load` still dropped unknown-sport rides under a sport filter**, recreating the
  missed-session narrative one function over from where it was fixed. All three queries now share
  one sport-filter helper.
- **`get_week`'s `planned_tss` folded an unparseable spec to a silent zero** — the null-folds-to-zero
  pattern `_aggregate_load` exists to stop, surviving on the planned side of the same summary. One
  corrupt spec made a week read as over-performed; planned sessions now get the same scored/unscored
  accounting as the actuals.
- **`log_hr`'s estimation note overwrote the backdated-entry note**, so backfilling a max HR returned
  zones with no statement that they are not the ones in force. It is a separate key now.
- **`update_planned_workout` returned its refusal inside an `ok: true` envelope** — the defect class
  the "a refusal is raised" rule exists to prevent, invisible to the new `_coach` guard because that
  keys on the literal `"ok"`. It raises.
- **`compute_load(activity_ids=[])` returned a different shape** from the normal path, which spreads
  the aggregator. It now spreads it too, so the two cannot drift.
- **`link_activity` ranked a candidate with no duration as a perfect match**, presenting it ahead of
  a near-exact one in the ambiguous list. Missing durations sort last.

Also from that review: `get_week` now uses the `History` already in scope instead of re-reading
`ftp_history` per planned row; `_latest` delegates to the shared resolver; the test-only `_flags`
key is gone, leaving one flag channel; the import dedup query names its columns and no longer
serialises a payload it is about to discard; `get_week`'s event lookup drops a no-op overlay;
`log_hr` and `get_zones` load each history table once; and `FTP_LIMITS`/`FTP_USUAL` no longer alias
the shared constants under second names.

#### Found in the third review round

Ten more, each reproduced before fixing and pinned by a test that asserts the shape of the output
rather than a count — the worst bug of this round survived a test that checked only how many rides
came back. Most are siblings of a round-2 fix, in the function the fix did not reach, so each was
fixed as a pattern across the module rather than at the cited line.

- **An idempotent re-import answered `unchanged: [null]`.** *(Regression.)* The dedup query was
  narrowed to the import fields, which do not include `garmin_activity_id`, while the unchanged
  branch still projected the row through the wider read list — and a projection fills a column it
  cannot see with None. Every data-quality caveat on an unchanged ride was anchored to no
  identifiable activity, and its `rpe`, `feel` and `note` came back null too. Every read of an
  activity row now uses the one column list, and a test pins that it covers the import fields.
- **An empty string erased a stored debrief, event name, session note, annotation or profile
  field.** `debrief=""` passed the `is not None` guard, `_text("")` returned None, and the UPDATE
  wrote NULL over the field the docstring calls the only part of a race record still useful a year
  later. One rule now covers every free-text update field: blank text never overwrites a stored
  value, the response names any field ignored for that reason, and a call carrying nothing else
  refuses with that reason rather than "pass at least one field".
- **`log_hr` returned estimated zones and a false note over a measured threshold.** The zones keyed
  off what *this* entry carried, so an athlete with 165 bpm on file who logged a max HR got zones
  computed from 92% of it and "No threshold HR on file" — beside an `in_effect_today` in the same
  response saying the threshold is measured and 165. Zones now come from the figures in force on the
  entry's own date, and the estimation note only when that resolved threshold is itself an estimate.
- **A bare `record_race_result` completed an upcoming race with nothing in it.** *(Regression.)*
  The auto-complete ran before the empty-updates guard added in round 2, so a partial retry or an
  existence probe closed the race with no time, no ride and no debrief. Completion now needs a call
  that actually carries a result, and the no-op path returns the same shape — debrief nudge included
  — as a real one instead of hand-building its own.
- **Evidence-free blocks still counted toward `as_prescribed`.** *(Regression.)* Round 2 replaced a
  closed-world classification with three positive buckets, so a block that was in none of them — a
  free block whose lap carried no duration — was silently evidence that the session went to plan,
  and a verdict added later would have been too. `classify_block` now partitions every
  `(verdict, duration_verdict)` pair into exactly one of compliant / deviating / unverifiable,
  treats an unknown duration as unverifiable, and raises on a verdict nothing recognises.
  `compliance_report` reports `compliant_blocks` alongside the other two.
- **A NULL ride duration was printed as "0:00" and reported as a deviation.** *(Regression of the
  same null-folds-to-zero pattern round 2 fixed in `planned_tss`.)* `compliance_report` said "rode
  0:00" and then "The ride was 1:00:00 shorter than planned" about a ride whose duration is simply
  not stored; `get_week` and `compute_load` printed a zero-length ride. One formatter now prints
  "unknown" wherever a duration may be null, and the comparison says it could not be made rather
  than inventing a shortfall.
- **NULL lap durations triggered the "splits belong to a different ride" warning.** Each contributed
  zero to the lap total, so the sum fell short of the ride and the tool accused the ride's own
  splits. It now counts the untimed laps and says the cross-check was not made.
- **On Python 3.10 a colon-less UTC offset still stored an instant hours wrong.** `"…33.5+0200"` —
  what `strftime("%z")` writes — was the third shape of the same defect patched in three rounds. The
  offset colon is now normalised alongside the existing `Z` and fraction rewrites, and the fallback
  splits any offset off and reapplies it instead of truncating at the dot: no path can drop an
  offset any more.
- **`link_activity` contradicted itself on a completed session.** Re-linking one to the correct ride
  — the routine mislink correction — warned that "linking did not mark it completed" about a session
  already completed, and invited a pointless status update. The note is now scoped to statuses that
  are coaching decisions, and the two near-identical UPDATE statements are one.
- **`get_week` printed a NULL ride duration as "0:00"** in the sentence a model relays, rather than
  saying the length is unknown.

Also from that review: `AUTO_COMPLETABLE_STATUSES` is used at all three sites that spelled out
`('planned', 'pushed')`; the unknown-sport count and its snapshot-before-splice dance are one helper
shared by `list_activities` and `compute_load`; the planned and actual sides of a week share one
unscored-total warning; `History` reads each history table on first use rather than all three
eagerly, so `get_profile` issues three queries where it issued five and `log_hr` one where it issued
four; the identity-set block classification is gone, superseded by the partition; and
`_pad_fraction`'s docstring no longer claims a 3-digit fraction comes back unchanged.

#### Found in the fourth review round

Eight, down from ten. Two are **overcorrections by the round-3 fixes** — a suppression that went
one case too wide — and one is a capability that round 3 removed without replacing.

- **A sentinel date with an offset took the whole import batch down.**
  `"0001-01-01T00:00:00+0200"` — the zero-date some exporters write — made the UTC conversion raise
  `OverflowError`, and `import_activities` reports a bad row with a reason rather than by throwing,
  so one corrupt row aborted the call and lost every valid ride beside it. Newly reachable, because
  round 3 started reattaching the offsets the old fallback stripped. The conversion and the
  epoch-milliseconds branch beside it now return None, and the row is rejected with a reason.
- **An untimed lap silenced a provable wrong-ride warning.** *(Overcorrection.)* Round 3 stopped a
  lap with no duration being read as zero and blamed for a gap — right — but suppressed the
  cross-check entirely, including when the timed laps *already exceed* the ride. Untimed laps can
  only add time, so that direction is a mismatch nothing missing can explain, and a genuine
  wrong-ride import passed in silence. The shortfall direction stays suppressed; the overshoot
  warns again; a ride with no duration of its own says so instead of comparing against nothing;
  and `duration_check` is now always returned, because the absence of a `warning` could not
  distinguish a sum that matched from one that was never made.
- **`get_form` read unscored rides as rest days, silently.** The last consumer of a null TSS without
  unscored accounting: a ride with no power and no heart rate never entered the series, so its day
  stepped as a rest day and a season with a dozen of them produced a CTL indistinguishable from
  detraining. It now reports `unscored` and the shared `_unscored_warning`, and still invents no
  load.
- **There was no way at all to clear a stored free-text field.** Blank-means-ignored closed the
  accidental-erase hole and left no deliberate one: a healed injury sitting in `constraints` routed
  every future plan around an injury that was over, and the workaround — writing "none" — is a
  constraint string downstream reads as real. Every update tool now takes `clear=["field", ...]`,
  which NULLs the named fields and reports `cleared_fields`. Blank still means "leave it alone";
  the erase is a verb, not a magic value. A field the tool does not own is refused (raised), as is
  a field given both new text and a clear; an event's name is not clearable at all. Clearing a
  debrief is explicitly *not* a result, so it cannot complete an upcoming race.
- **`log_hr`'s backdating note promised zones the response withheld.** *(Overcorrection.)* Round 3
  gated `hr_zones` on the entry carrying a threshold or a max HR, but left the note ending "the
  zones below are the ones in force on …" — so a backdated resting-HR entry pointed at zones that
  were not there. The backdating warning stays unconditional; only the sentence pointing at the
  zones is now conditioned on the same gate that emits them.
- **On Python 3.10 an hour-only offset still stored a wrong instant.** `"…33.5+02"` was the fourth
  spelling of one defect fixed one shape at a time across four rounds, so this fixes the class:
  `_OFFSET` matches every legal spelling (`+02:00`, `+0200`, `+02`) and normalises to one, and the
  pattern fallback now **rejects** any input still carrying a trailing offset it cannot apply
  instead of truncating at the dot. No future offset shape can silently corrupt an instant; the
  worst case is a rejected row with a reason. The widened pattern can match inside a bare date
  ("2026-08-20" ends in "-20"), so an offset is only read when a time precedes it — pinned by test.
- **The lap column list and the lap alias table were unpinned.** The INSERT writes `row.get(field)`,
  so a key drifted between the two silently stores NULL: drop `avg_power` and every block of every
  session compares as `no_power`. The activities pair got this test in round 3; the laps pair has
  the mirror of it now.
- **The laps tool docstring omitted the untimed-laps branch**, so a model could read a missing
  `warning` as "sum verified" when the response said it had not been checked. Both branches are
  documented, in the post-fix behaviour.

Also from that review: `docs/coaching.md`, `docs/tools.md` and the `coaching` skill document the
clear verb and the one-directional lap check; `_unscored_warning` takes the clause its third caller
needed; and `get_form`'s docstrings on both layers say what an unscored ride does to the curve.

#### Found in the fifth review round

Ten. Almost all of them live at the *boundary* of a round-4 fix — one line above a guard, one
Python version to the side of a rejection rule, one falsy value past an `is not None` — so each is
fixed by moving the boundary rather than by adding another case beside it.

- **An oversized integer timestamp still took the whole import batch down.** The round-4 overflow
  guard started one line below `float(value)`, and JSON integers are arbitrary-precision in Python:
  `float(10**400)` raised outside the try and lost every valid ride in the call. The whole epoch
  branch is inside the guard now, NaN and the infinities are refused rather than left to raise from
  `timedelta`, and the except is narrowed to `OverflowError` — the only thing that can still reach
  it.
- **A bare date with a UTC offset became a fabricated time of day on Python 3.11+.** `fromisoformat`
  there takes any separator, so `"2026-08-20-05:00"` read the offset *sign* as the date/time
  separator and the offset *digits* as a clock — a start time invented out of a timezone, naive, so
  `to_utc` did nothing. On 3.10 the same string rejected cleanly, which is why it survived a review
  round: the corruption was invisible on half the matrix. A date with an offset and no clock is now
  refused on both parse paths, and both are tested. A bare date with no offset still reads, because
  the offset pattern matches inside every date ("2026-08-20" ends in "-20") — the false positive and
  the real offset are told apart rather than both waved through.
- **A sentinel date that did not overflow imported as a ride in year 1.** Round 4 caught only the
  sentinels whose UTC conversion overflowed; `"0001-01-01T00:00:00-05:00"` converts *away* from the
  underflow, and a naive or local-time sentinel is never converted at all. `get_form` then walked
  ~740,000 days per call and reported 739,000 days of history for a three-week-old athlete.
  Convertible and plausible are different questions: an activity dated before 1990 or in the future
  is now rejected per row, with the reason, which closes the class regardless of offset or path.
- **A zero-duration activity accused its own splits of belonging to a different ride.** Round 4
  replaced a truthy gate with `is not None`, and manual entries carry `"duration": 0`. A placeholder
  zero is not a measurement — the same rule that keeps a null TSS out of a total — so null, zero and
  negative now route together to "carries no usable duration, could not be checked", for laps as
  well as rides.
- **`get_form` accepted an unbounded window and hung instead of refusing.** "All history" spelled
  `0001-01-01..9999-12-31` — a plausible guess for a model that has not been told a bound —
  materialised 3.65 million points. Windows beyond five years are refused with the bound named.
  Both ends of the walk are bounded now, not just the window: the run-up is capped at ten years
  (`runup_capped`) and rides dated before 1990 are kept out of it (`excluded_implausible_dates`) —
  a five-year window ending in 9999 was inside the span cap and still crossed eight thousand years
  to reach the first stored ride.
- **A wrongly auto-linked session could not be unlinked.** `link_activity` only ever re-links to
  another real ride, so a session auto-linked to the wrong activity and actually *missed* fell out
  of both of `get_week`'s deviation lists at once and `compliance_report` kept answering about that
  ride. `update_planned_workout(clear=["linked_activity_id"])` is the way back to "nothing was
  ridden for this".
- **A race result filed against the wrong event could not be retracted.** A result is three things —
  status, finish time, ride — and only the debrief was clearable, so an "undone" result left a
  finish time on an again-upcoming race and a ride still read as that race's training.
  `clear=["debrief", "finish_time_s", "linked_activity_id"]` retracts all three. Clearing is still
  not a result, so it never completes an upcoming event.
- **`get_form`'s tool docstring gave one reason for unscored rides where there are four.** No power
  and no heart rate is only one of them; power with no FTP on that date, heart rate with no
  threshold, and no duration are the others — and two of those are fixed by logging an earlier
  figure, not by importing more rides. Both layers now enumerate all four.
- **The `clear` documentation named one tool of five.** `docs/tools.md` and `docs/coaching.md` now
  list each tool's clearable fields, and the profile example is anchored to `update_profile` rather
  than sitting in a passage about `record_race_result`, where that exact call is refused.
- **`clear` as a bare string worked in Python and died over MCP.** pydantic does not coerce a scalar
  to a list, so the coach layer's tolerance was unreachable through the schema — the same
  disagreement as the round-1 `str | int` ids, fixed the same way: the tool signatures say
  `str | list[str]`. The sweep for that pattern found one more: `save_planned_workouts` promised
  that valid sessions are stored when others are refused, while `workouts: list[dict]` had pydantic
  reject the whole call over one malformed item. It takes `list[Any]` now, so the per-item refusal
  it documents is reachable.

One more, found by CI rather than by review: **a year below 1000 lost its leading zeros on glibc.**
`strftime("%Y")` delegates the year to the platform's C library, which writes year 1 as "1" on Linux
and "0001" on macOS and Windows — so a sentinel timestamp read back as `"1-01-01T00:00:00"`,
`local_date` sliced its first ten characters into `"1-01-01T00"`, and the date band above could not
judge it. Timestamps are formatted with `isoformat`, which is Python's own and pads, and the
converted instant now drops its tzinfo explicitly rather than relying on the formatter to omit it.
The tests that pinned the sentinel shape had been green locally and red on CI since round 4.

Also from that review: the offset is split and parsed once rather than twice per timestamp;
`_clear_notes` replaces the hand-threaded `(cleared, blanked, clearable)` triple and the default
argument that existed only to be forgotten; the `assert` in `log_hr` is gone in favour of narrowing
that survives (an `AssertionError` would have escaped `_coach`'s refusal rendering); the
loop-invariant string build is hoisted out of the strptime loop; the clear-set comments say what the
criterion actually is — clearable where empty is a state the record can be in, not "free text only";
`_unscored_warning`'s subject and verb agree for all three callers, as do the lap sentences ("the 1
timed lap sums to"); and the healed-injury rationale lives in `_stage_clear` alone.

#### Found in the sixth review round

- **A spec's repeat count and block duration multiply without a bound.** `count=2_000_000_000` over
  a 600 s block validated and stored — `describe_spec`, `render_zwo` and `render_garmin` all reach
  the same expansion, and `metrics.compute_metrics` allocates one float per second of it. Validation
  now refuses any spec whose total expanded duration (every repeat multiplied out, nesting included)
  exceeds 24 hours (`MAX_WORKOUT_SECONDS`), with the bound named in the refusal. One check on the
  product covers count, duration and nesting at once, computed by multiplication rather than by
  iterating the count. The test suite now runs under `pytest-timeout` (120 s/test), so a future hang
  of this shape fails the run instead of stalling it.
- **`get_form` could overflow instead of refuse.** `first - timedelta(days=MAX_FORM_RUNUP_DAYS)` was
  evaluated before `max()` ever clamped it, so any window starting inside about the first ten years
  of the representable range raised `OverflowError` — an `ArithmeticError`, outside the tool layer's
  catch tuple, and a crash across the MCP boundary instead of the ordinary refusal every other bad
  input here gets. The clamp now happens before the subtraction (date-minus-date never overflows),
  and `OverflowError` is in `_coach`'s catch tuple regardless, as a backstop for the next place this
  ordering mistake gets made.
- **A sentinel timestamp rejected the whole row even when its sibling was fine.** Plausibility was
  judged only on the derived `local_date`, so a sentinel `startTimeLocal` beside a genuine
  `startTimeGMT` lost the ride entirely though the UTC fallback exists for exactly this case — and a
  sentinel `startTimeGMT` beside a genuine local time stored a year-1 `start_time_utc` unflagged,
  silently corrupting the ordering tiebreak `list_activities`/`link_activity`/`get_week` all use.
  Each timestamp is now judged on its own date; a row is rejected only when neither survives, and a
  missing `start_time_utc` is flagged `no_utc_time`.
- **A lap's placeholder zero read as a measurement.** `avg_power: 0` (no power meter) against a
  250 W interval read as `under`, or as compliant `easier_than_target` on a recovery block; a
  `duration_s` of 0 read as `short`. Both now route through the same `_positive` gate
  `compute_activity_load` has always used for duration — moved to `training.py` so `compare_block`
  can share it — reading as `no_power` / `duration_verdict: unknown` instead. `compliance_report`'s
  own duration comparison had the same hole: a stored `0` reported "rode 00:00" and a fabricated
  "shorter than planned" sentence.
- **Unlinking an auto-completed session left it `completed` with no ride.** `link_activity` sets
  `completed`; `update_planned_workout(clear=["linked_activity_id"])` alone used to leave that
  status standing over nothing, invisible to `planned_not_ridden` (no status left that means
  "expected") and to `ridden_not_planned` (there is no ride). Clearing an *actual* link — the column
  was not already null — with no explicit `status` in the same call now reverts `status` to
  `planned`, the same way it was set, and says so; a session marked `completed` by hand with no link
  keeps that status, since `"linked_activity_id" in cleared` is true even when nothing was ever
  linked. `record_race_result` had the identical hole one field over: retracting all three result
  fields — its own documented recipe — left a `completed` event with no time, no ride and no
  debrief; a call that actually cleared something and left no result at all now reverts to
  `upcoming`. Gated on the clear having happened, not merely on "no result survives" — a bare
  `record_race_result(event_id)` probing an event a past-dated `add_event` already completed with no
  result, or a blank `debrief=""` on one deliberately marked `completed`, must not rewrite a status
  nobody asked to change. An explicit `status` in the same call always wins over either revert.
- **A zero or negative finish time stored and auto-completed a race.** `finish_time=-272` rendered
  as `"-1:55:28"`; `finish_time=0` completed the event while the display hid it entirely.
  `record_race_result` now refuses a non-positive `finish_time` at its own call site — the shared
  `parse_duration` is untouched, since spec blocks do their own `<= 0` check independently.
- **`get_form` scored rides its own window had already discarded.** A ride beyond the run-up cap or
  before the plausible-date floor was stripped from the walk by a post-loop filter, but was still
  scored and counted into `methods` and `unscored` first — so `mixed_methods_warning` and
  `unscored_warning` fired about rides that could never have contributed to the figures they were
  warning about. Both branches now skip scoring entirely.
- **`rpe` could not be cleared.** An unrated ride is a real state — logged before the athlete had a
  number for it, or attached to the wrong ride — and the only write path forced 1-10.
  `clear=["rpe"]` now returns it to unrated. `distance_km`/`elevation_m` on an event stay
  replace-only, deliberately: they come from the event's own definition, not a subjective read of a
  ride, so there is no "unknown" state more honest than "not yet entered".

Cleanup alongside: `_agree(1, "carry")` read `"carrys"`, and the run-up note's "was left out" clause
did not agree with a plural subject either — both fixed, and `test_the_implausible_note_agrees_*`
pins the full sentence for one ride and for several rather than a substring. `list_activities(limit=0)`
reported `count: 0` and `truncated: true` as if that were a real answer; `limit < 1` is now refused.
docs/coaching.md described the import date band as `1990-01-01..today`; the code has always enforced
`today + FUTURE_DATE_SLOP_DAYS` (2 days) per timestamp, and the doc now says so. A new lock-step test
ties `get_form`'s "five years" / "ten years" / "1990" prose in server.py and docs/coaching.md to
`MAX_FORM_SPAN_DAYS`, `MAX_FORM_RUNUP_DAYS` and `PLAUSIBLE_DATE_FLOOR`, in the style of
`test_version_lockstep.py`.

#### Found in the seventh review round

- **`confirm_targets` could file numbers the athlete never saw.** It recomputed the suggestion with
  hardcoded defaults — `DEFAULT_PROTEIN_G_PER_KG`, `SEDENTARY_FACTOR`, no exercise override —
  discarding the three knobs `suggest_targets` takes. A suggestion shown with
  `exercise_kcal_override=800` and accepted as it stood filed the *knob-less* number instead.
  `confirm_targets` now takes the same `protein_g_per_kg` / `baseline_factor` /
  `exercise_kcal_override`, top-level and per date inside `days` (a per-date value wins), validated
  with the same limits `suggest_targets` uses, and recorded in `rationale_json`. The knobs alone do
  not flip `source` to `overridden` — they are inputs to the derivation, not overrides of its result.
  A top-level `exercise_kcal_override` is refused across a multi-date `days` call, exactly as
  `suggest_targets` refuses it over a range; a per-date one is fine on any number of dates.
- **A goal resolved by "latest active", not by the date being asked about.** `_active_goal` filtered
  `status = 'active'`, so a goal closed in June was invisible to a target computed for a March date it
  was in force for the whole time — `suggest_targets`/`week_summary` for that date fell back to
  maintenance mid-deficit. It now resolves for the date: effective at or before it, and not yet closed
  as of it (`closed_date IS NULL OR closed_date > date`) — a closed goal was still the goal before its
  close. `get_goal`'s status-based "what is active right now" read is untouched; the two answer
  different questions on purpose.
- **A protected day withheld a gain goal's surplus along with a loss goal's deficit.**
  `adjustment = 0.0 if protected else full_adjustment` was sign-blind: a `+385` kcal/day gain goal on
  race day filed a *smaller* target than an ordinary day, while the note said "take the extra mostly
  as carbohydrate" about an adjustment the arithmetic had just zeroed. Only a deficit (`full_adjustment
  < 0`) is withheld on a `big_session`/`race`/`race_eve` day now; a surplus is applied in full, with a
  note that says so rather than claiming it was "not applied".
- **A second planned session vanished once the first ride imported.** The exercise `elif` chain let a
  measured activity win outright, so a morning ride imported at 600 kcal hid an unrelated evening
  session still planned at ~672 kcal — `exercise_kcal` read 600 with no mention of the second one, and
  a small commute imported next to a still-planned four-hour ride could erase the ride's ~2500 kcal
  entirely while the same response still labelled the day a `big_session`. `_day_type_and_training` now
  reports every still-`planned`/`pushed` session that is **not explicitly linked** to one of today's
  activities (`linked_activity_id`, set via `link_activity` or `update_planned_workout`) as additional
  exercise — summed with the measured figure and named in a note
  (`exercise_source: "imported_activity+planned_workout"`). Only a session that is `completed`, or
  explicitly linked, is treated as the same session as an import and left out; there is no
  unclaimed-activity-count heuristic standing in for an explicit link, because that heuristic silently
  absorbed a planned session whenever there was any unlinked import on the day, whatever its size.
  `docs/nutrition.md` updated to describe the summing rule.
- **A race recorded `dns` still triggered race-day fuelling.** Both event lookups excluded only
  `abandoned`, so a `dns` race still made its date `race` and the day before `race_eve` — deficit
  withheld, fibre halved, for a race the file says was never started. `dns` is now excluded alongside
  `abandoned`, via `coach.NON_STARTING_EVENT_STATUSES` (defined once, imported into nutrition.py
  rather than the two strings being inlined a second time).
- **A negative calories or duration on an activity was read as a measurement.** Truthiness let
  `calories: -500` count as measured exercise, and a negative `duration_s` *subtracted* from the
  day's activity time, suppressing `big_session`. Both now route through the shared `_positive` gate
  (moved to `training.py` in the round-six fix and reused here); a non-positive value reads as no
  measurement, the same way a placeholder zero already did.
- **The weight trend anchored outside its own window.** `start_point`/`end_point` were the first and
  last moving-average points the six-day reach happened to produce, so a weigh-in up to six days
  before the window anchored the "week's" trend — sometimes to a single raw reading, the exact noise
  the average exists to smooth. The endpoints are now the first and last *in-window* means; the reach
  still feeds the moving average itself, only the two endpoints moved. That change made a second,
  narrower no-span case reachable: exactly one weigh-in inside the window, with one or more before it.
  That is now told apart from several in-window weigh-ins genuinely sharing one date — the former says
  so ("only one weigh-in falls inside the window"), the latter keeps the original "all weigh-ins fall
  on one date" wording; before this the single-in-window case reused the "one date" text even though
  two different dates were plainly on file, three keys away from `weigh_ins_in_window` in the same
  response.
- **`_active_goal`'s own docstring asserted the opposite of what it does at the handover boundary.**
  It read "`closed_date > on_date` (not `>=`) so the day a goal closes is still inside its own window",
  which is only true when nothing replaces the goal that day — with a replacement effective the same
  date, the code (correctly) hands the handover day to the replacement, and the sentence was an
  invitation for a future edit to "fix" `>` to `>=` and silently misfile every handover day. Reworded to
  describe both cases, and both are now pinned by a test.
- **~720 queries for a 120-day range.** `suggest_targets` over a range and `week_summary` resolved
  weight, the goal and the day type per day. `_RangeHistory` mirrors `coach.History`: one query each
  for weight history, goals, activities, planned sessions, events and the food log/targets across the
  whole range, resolved per day in memory with identical semantics. Single-date calls are unaffected.
- Cleanup: the BMR docstring said the male/female gap is 161 kcal; it is 166, and every other surface
  already said so.
- **A free-form estimate's unstated protein or fibre stored as a measured zero.** `entry.get("protein_g")
  or 0` folded "not stated" and "stated as zero" into the same number, so a 900 kcal restaurant estimate
  landed with `protein_g: 0.0` — overstating `day_summary`'s remainder by the whole meal and quietly
  deflating `week_summary`'s protein average. `food_log.protein_g`/`.fiber_g` are now nullable
  (**schema v5** — a SQLite table rebuild, append-only alongside v1-v4; existing rows migrate in
  place); the estimate path uses the same optional-number coercion `carbs_g`/`fat_g` already had, and
  an *explicit* `protein_g: 0`/`fiber_g: 0` still stores a real `0.0` — a stated zero is a
  measurement, not a gap. `_totals` reports `protein_g_missing_entries` / `fiber_g_missing_entries`
  when any entry did not say, still summing what is known (0.0 on an empty or all-unknown day, never
  null — `day_summary`'s remainder arithmetic keeps working), and `day_summary` adds a note that the
  remainder above overstates by however much those entries turn out to hold. The same
  `entry.get(key, 0.0)` shape hid a second bug one line below: the default only fires on an *absent*
  key, and every entry carries `protein_g`, so a `None` from an estimate reached `sum()` directly —
  fixed by the same change.
- **A 400-digit number crashed instead of refusing.** `float()` on a JSON integer with no size limit
  raises `OverflowError`, reachable directly through any loosely-typed list/dict argument —
  `add_ingredients`, `log_food` — and through `coach.log_weight`'s own unguarded `float(value_kg)`.
  `nutrition._number` now catches `OverflowError` alongside `TypeError`/`ValueError`, and
  `log_weight` wraps its coercion the same way; both refuse naming the field, per item, rather than
  losing the rest of a bulk call to `_coach`'s `OverflowError` backstop (added in the round-six fix)
  catching it three layers up. A merely huge-but-finite figure (`10**300`) still passes coercion and
  is refused by the plausibility range, not by an exception.
- **`counts_toward_protein: "false"` stored `True`.** `_bool` was `bool(value)`, so any non-empty
  string — including the word "false" — was truthy, silently inverting the incomplete-protein
  exclusion for a spreadsheet-derived paste. `_bool(value, what, default)` now parses the case-
  insensitive usual spellings (`"true"`/`"false"`/`"1"`/`"0"`/`"yes"`/`"no"`, and `int` 0/1) and
  refuses anything else, naming the field; used for `counts_toward_protein` and `is_estimate` alike.
  `_resolve_meal` gained the `bool`-before-`int` guard `resolve_ingredient` already had —
  `log_meal(meal=True)` used to expand whichever meal has id 1, since `bool` is an `int` subclass.
- **`log_food` accepted `grams: 0`.** A zero-gram placeholder row logged a day as `logged: true` at
  ~0 kcal, deflating `week_summary`'s average. `_quantity` now refuses `grams <= 0` and
  `portions <= 0` as a placeholder rather than a food. `log_meal`'s override semantics — `grams: 0`
  meaning "leave this ingredient out today" — are preserved by detecting the explicit zero in the
  override loop *before* calling `_quantity`, which now refuses it. The omit check compares the
  *coerced* figure, not the raw value — every other quantity in the module reads the numeric
  strings a pasted payload carries, so `"0"` omits the same way `0` does — and refuses a bool
  first, since `float(False)` is `0.0` and would otherwise omit an ingredient silently.
- **No path back to unknown on an ingredient or a meal, and a `€0` package was priced.**
  `update_ingredient`'s optional fields only ever replaced; a wrong price, portion or macro could
  never return to "not known". `update_ingredient(clear=[...])` and `save_meal(clear=[...])` (there
  is no separate `update_meal`) port `coach._stage_clear`'s erase-is-a-verb rule — a field outside
  the clearable set is refused rather than silently ignored, and the response carries
  `cleared_fields`. Separately, `package_price: 0` produced `cost_per_100g: 0.0`, counted as *priced*
  by every reader checking for `None` — `cost_per_100g` now treats a zero or negative price (or
  weight) as unpriced, the same as an absent one, so a logged entry's cost comes back `None` and
  `week_summary`'s cost note reports it rather than silently understating the week's food spend. Two
  new tools, `delete_ingredient` (refused while any log entry or meal still references the row —
  frozen history and a meal's live macros are both load-bearing) and `delete_meal` (always allowed;
  existing log entries already froze their own macros and `meal_name`, so only the link back to the
  recipe is cleared).
- **Cleanup: `coach._one_of` and `nutrition._one_of` disagreed on case.** Nutrition folded input to
  lowercase before matching; coach did not, so `status="PLANNED"` was a valid datum in one layer and
  a refusal in the other for the same word. Unified into `training.one_of` — case-insensitive,
  returning the *canonical* spelling from the vocabulary (so `EVENT_PRIORITIES = ("A", "B", "C")`
  still round-trips to `"A"`, not `"a"`) — with `coach._one_of` and `nutrition._one_of` as thin
  wrappers that re-raise their own refusal type, so every existing `except NutritionError` around a
  per-item loop keeps catching what it always caught. Living in `training.py` because `coach.py`
  imports `GENDERS` from `nutrition.py` at module load, so a module-level import in either other
  direction is a real circular one. `coach._plural`/`coach._agree` made the same move for the same
  reason: nutrition had grown seven hand-rolled pluralizations of its own, one of which conjugated
  a conjoined subject wrong ("protein from 1 entry and fibre from 1 entry **is** unknown") — both
  layers now call `training.plural`/`training.agree`, extended with the `entry`→`entries` noun
  rule and the `is`/`are` pair those messages need.

The tool-surface pin (`manifest.json`) grows by two — `delete_ingredient`, `delete_meal` — updated
deliberately alongside the tools; `update_ingredient` and `save_meal` gained a `clear` parameter.

### Notes

Ingestion is model-mediated by design: Claude fetches from the Garmin MCP and passes the JSON here
unchanged. The import tools accept Garmin's own shapes — a bare list, a single activity, a wrapper
dict, the nested `summaryDTO` form, a JSON string — rather than a clean typed schema, because a
schema would make the model retype every number on the way through, and a mistyped average power is
a training load that is wrong and looks entirely reasonable.

## [0.2.0] - 2026-08-21

Everything below came out of real sessions using the server — four field reports from
agents that built and uploaded actual workouts. Three of the bugs they found had shipped
past a green test suite, all three for the same reason: the tests called the Python
functions, and the bugs only existed once arguments had been through JSON and a client.

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

- **`verify_mywhoosh_library_entry`** — checks the library card against the session that was
  exported, in the card's own formats (`"1h 18m"` as readily as `"78:00"`). The credit is already
  spent by then, so it prevents nothing; it establishes what the spend bought, which the redirect
  alone cannot say. It also folds multiplication signs, because MyWhoosh renders an uploaded
  `Tempo-3x14` as `Tempo-3×14` on the card while the editor header shows the ASCII form — so any
  future match-by-name would have missed.
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
- **`verify_mywhoosh_import` no longer passes when the snapshot is missing.** It used to warn and
  return `safe_to_export: true`. But on a real no-op the duration and Training Load checks both
  pass — that is exactly how the failure presents — so without the snapshot the tool cannot tell
  success from nothing having happened, and reporting "safe to export" on two checks that provably
  do not discriminate is worse than reporting nothing. Blocking costs only time: no credit is spent
  before the export itself, so a fresh editor and a re-import are free.
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
- **`mywhoosh-upload` Step 8 says `find` returns two refs for EXPORT TO MYWHOOSH, and which to
  click.** Step 5 documented that duplication for the FTP fields; Step 8 was silent about it at the
  one irreversible control on the page, leaving an agent to infer it unaided. It also notes the
  redirect lands on Collections rather than My Workouts, and that the card's `×` is not a mismatch.
- **Step 3 names its two branches** — targets in watts, where the FTP is load-bearing, versus
  targets in percentages, where it only moves the displayed numbers. The percentage case had been a
  paragraph inside prose organised around the watts case.
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

First release — cut in the repository but never tagged or published to PyPI, so `0.2.0` is the first version available to install.

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

[Unreleased]: https://github.com/elias-ramzi/ClaudeCyclingMCP/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/elias-ramzi/ClaudeCyclingMCP/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/elias-ramzi/ClaudeCyclingMCP/releases/tag/v0.2.0
[0.1.0]: https://github.com/elias-ramzi/ClaudeCyclingMCP/tree/6610803

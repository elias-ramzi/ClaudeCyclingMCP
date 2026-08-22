# Repository guide

Architecture and conventions for anyone — human or agent — working in this repo.

## The shape of it

One canonical spec in, two renderers out — and above that, a coach layer that stores the athlete
and computes from what they actually rode:

```
spec (JSON) → validate → resolved Workout tree ──┬── render_zwo    → .zwo   (MyWhoosh)
                       (power as fractions of FTP)└── render_garmin → JSON   (Garmin)

Garmin MCP ──(the model pastes the JSON)──> coach.py ──> coach.db ──> load · form · compliance
```

| Module | Holds |
|---|---|
| `spec.py` | parse, validate, normalise. Produces a `Workout` of `Block`/`Repeat` nodes with **all power as fractions of FTP** — the single representation both renderers consume. |
| `metrics.py` | 1 Hz power series, NP/IF/TSS, and the `describe_spec` table. |
| `render_zwo.py` | MyWhoosh XML. Hand-built strings, not ElementTree, to control the exact format. |
| `render_garmin.py` | Garmin `upload_workout` payload. |
| `verify.py` | compare what a platform stored against what was sent: Garmin's returned payload, and MyWhoosh's scraped builder header. |
| `store.py` | the SQLite database: where it lives, and the ordered migrations. The only module that touches state. |
| `garmin_import.py` | normalise raw Garmin MCP payloads into stored rows. Pure — it reads what was pasted in. |
| `training.py` | zones, TSS (power and HR), CTL/ATL/TSB, block-vs-lap comparison. Pure arithmetic over stored numbers. |
| `coach.py` | the coaching operations: read and write the athlete's file, compute from it. |
| `skills.py` | load `.claude/skills/*/SKILL.md` and serve them as MCP prompts. |
| `server.py` | the MCP tool surface. Thin — logic lives in the modules above. |

**Fractions of FTP are the internal currency.** Watts are converted in at parse time and out at
render time. If you find yourself carrying watts through the middle of the pipeline, that's a smell.

## Rules that are load-bearing

These encode things that were expensive to learn. Changing them needs evidence, not reasoning.

**Garmin: power targets are `workoutTargetTypeId` 2 (`power.zone`) with `targetValueOne`/`Two` in
watts.** Not id 6 / `power.between`, despite what the Garmin MCP's tool description says — id 6
uploads cleanly and Garmin stores it as a *pace* target on a cycling workout. A %FTP target is the
same id-2 shape plus `targetValueUnit: {"unitId": 253, "unitKey": "percent"}`; watts carry no unit
object at all.

**Garmin: never trust the curated read.** `get_workout_by_id` drops `targetValueUnit`, so watts and
percentages are indistinguishable through it, and its `estimated_duration_seconds` disagrees with
the sum of the steps. Verify against **what was sent**.

**Garmin: a `RepeatGroupDTO` must carry `conditionTypeId: 7`.** Omit the numeric id and the API
silently corrupts the repeat count.

**`.zwo`: ramps are always `<Ramp PowerLow PowerHigh>`.** Never `<Warmup>`/`<Cooldown>` — their
direction is read differently by different implementations. `PowerLow` is the *start*, so a descending
ramp has it above `PowerHigh`.

**`.zwo`: repeats are flattened, never `<IntervalsT>`.** MyWhoosh's editor makes an `IntervalsT`
block indivisible after import.

**`.zwo`: the filename is the library name.** MyWhoosh ignores the `<name>` tag for this.

**`hr_note` is never a control target.** Both platforms drive on power; an HR range in a session
description is a check figure. It is carried as message text on both sides.

**The server never uploads.** No network, no credentials. Uploading lives in the skills, in front of
a human — a MyWhoosh export spends a finite slot credit.

**Purity is scoped, not absolute.** Renderers, metrics and verification are pure. The coach layer
writes one SQLite file, at `~/.claude-cycling/coach.db` or `CLAUDE_CYCLING_DB`. Say it that way —
"no network, no credentials; filesystem access limited to its own database and explicit `out_path`
writes" — rather than calling the server pure, which stopped being true.

**FTP is dated, and load is resolved per ride.** Every training-load number uses the FTP entry in
effect on that ride's own date. A "current FTP" column would silently rewrite the athlete's history:
the same watts against a bigger FTP is a smaller IF, so a block of training shrinks the moment they
test better. The same holds for weight and HR thresholds.

**Power TSS and hrTSS are different quantities.** Never merge them into one number without saying
so. Each computed row carries the method that produced it; a ride with neither power nor HR gets a
null and a reason, never a zero — a zero is indistinguishable from a rest day.

**Import tools take Garmin's own shapes.** Never design a coach tool that requires the model to
retype numeric fields into a clean schema. Every retyped digit is a corruption opportunity, and a
mistyped average power is a load that is wrong and looks reasonable. Tolerate the shapes, keep
unknown keys, and never overwrite a stored value with a null.

**Coaching judgement lives in the `coaching` skill, not in code.** The server stores, computes and
refuses. What to do about a missed Tuesday is the skill's business.

**Migrations are append-only.** Never edit a migration that has run anywhere — only a version that
has not been applied ever runs again. Add the next one.

## The reference workout

Garmin workout id **`1662651131`** was hand-built in the Garmin UI with known inputs, and is what the
schema was derived from. Losing it means re-deriving from scratch. There is no rename or edit-in-place
in the API — only upload (creates new), delete, and schedule.

## Conventions

- **Line length 100**, `ruff` for both lint and format. Gate: `ruff check . && ruff format --check . && pytest`.
- **Comments explain why, not what.** Most comments in this repo record a platform quirk and what
  breaks without it. Keep that bar.
- **Golden files are behaviour.** `tests/golden/` is compared byte-for-byte. Updating one is a
  deliberate act that belongs in the changelog, never a way to make a test pass.
- **The recorded fixture** `tests/golden/recorded_roundtrip.json` is a real captured API exchange. It
  pins observed behaviour so drift shows up as a failing test.
- **Skills state their expectations.** Each step in `mywhoosh-upload` says what it should see, so a
  break reports which assumption failed. Preserve that when editing.
- **Version lives in four files** and is asserted in lock-step by a test — see
  [docs/versioning.md](docs/versioning.md).

## Testing

```bash
pytest              # offline, hermetic, no credentials
pytest -m live      # real Garmin round-trip; needs tokens, cleans up after itself
```

Every test that touches the database points `CLAUDE_CYCLING_DB` at a `tmp_path`. A test that writes
to the real `~/.claude-cycling/coach.db` is a bug: it would mutate the author's own training log.

The live suite uploads a workout, fetches it back, compares against what was sent, checks no target
was stored as a percentage, and deletes it — including on failure.

# Garmin schema provenance

Where the Garmin payload shape came from, what was probed to establish it, and what is verified. This is the part most likely to drift.


**Derived 2026-08-12**, against the live Garmin Connect API via
[`Taxuspt/garmin_mcp`](https://github.com/Taxuspt/garmin_mcp) 0.1.0 and
`garminconnect` 0.3.10.

**Reference workout: id `1662651131`.** Hand-built in the Garmin Connect web UI
to exercise as many constructs as possible, with known inputs. Losing it means
re-deriving the schema from scratch. There is no rename or edit-in-place in the
API — the operations are upload (creates new), delete, and schedule — so it is
left as is.

Two findings, both established by reading that workout's **raw** API response
rather than the MCP's curated projection:

## 1. Cycling watt targets are target type id 2, not id 6

The Garmin MCP's own `upload_workout` docstring says cycling watt ranges use
`workoutTargetTypeId: 6` / `"power.between"`. **Against this API version that is
wrong, and wrong silently.** Id 6 uploads without error, and Garmin normalises
it to the key `"pace.zone"` on a cycling workout — a pace target, not a power
one. Confirmed by upload/fetch probe.

Id 2 with key `"power.zone"` and `targetValueOne`/`targetValueTwo` round-trips
with the watts intact, and is byte-for-byte the shape the Garmin web UI produces
for a watt target. That is what this renderer emits.

## 2. %FTP is the same target type plus a unit object

The curated read shows an absolute watt range and a %FTP range identically —
both as `power.zone` with a low/high pair, with nothing to tell them apart. The
raw response distinguishes them:

| Entered in the UI as | `targetType` | values | `targetValueUnit` |
|---|---|---|---|
| 200–220 watts | id 2 `power.zone` | 200, 220 | `null` |
| 95–111 % FTP | id 2 `power.zone` | 95, 111 | `{"unitId": 253, "unitKey": "percent", "factor": 1.0}` |

So the %FTP encoding, left open in the original brief, is
`targetValueUnit: {"unitId": 253, "unitKey": "percent", "factor": 1.0}` on an
otherwise ordinary id-2 power target. **The curated read drops that field**,
which is precisely why it cannot confirm units, and why a round-trip test that
compares against the curated shape would pass while the units were wrong.

This renderer emits watts (no unit object), because absolute watts are
unambiguous and always correct. Percentage targets would only be worth pursuing
if you wanted targets that follow a changing FTP.

## 3. Derived fields are not evidence

`estimated_duration_seconds` is computed by Garmin's own rules and disagrees
with the arithmetic — the reference reports 5400 s against 5700 s of steps. It
is never sent and never used for verification. `estimated_distance_meters` and
`avg_training_speed_mps` are absent on freshly uploaded workouts, so their
absence proves nothing either.

## Verification status

The round-trip test **passes**. It renders a workout exercising every construct,
uploads it, fetches it back, compares against **what was sent** (not against the
curated read), asserts no power target was stored as a percentage, and deletes
the workout afterwards.

Visual confirmation in Garmin Connect is the one thing automation can't do —
the API can accept and echo a structure that still displays oddly.


---

Back to the [README](../README.md).

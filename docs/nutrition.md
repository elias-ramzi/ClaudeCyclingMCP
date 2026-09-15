# The nutrition layer

The same database and the same athlete as [the coach layer](coaching.md): an ingredient base,
standard meals, a per-ingredient food log, day-type-aware targets, and weekly summaries with food
cost. Eighteen tools, no network, nothing uploaded anywhere.

The division of labour is the whole design. **The server does every gram of the arithmetic** — macro
sums, running totals, BMR, the day's remainder, the weekly average — so nothing above it ever adds
food up in its head. A plausible calorie total is worse than none, because nobody checks it. The
coaching over the answers lives in the bundled [`nutrition` skill](../.claude/skills/nutrition/SKILL.md),
which is instructions for a model and carries no athlete's facts.

## Why it shares the training database

A calorie target that ignores what the athlete rode that day is a target for somebody else.
`suggest_targets` reads the same `activities`, `planned_workouts` and `events` tables the coach layer
writes, and that join is only free while both live in one file. It is also why the day type is read
rather than asked for.

## The data model

| Table | Holds |
|---|---|
| `nutrition_goals` | Dated and append-only, like `ftp_history`. Type (`lose`/`maintain`/`gain`), target and milestone weights, a **signed** rate in kg/week, status. One active at a time; setting a new one closes the previous rather than deleting it. |
| `ingredients` | Per-100 g kcal/protein/fibre (required) and carbs/fat/sat-fat/sugar/salt (optional). Aliases, `state` (`raw`/`cooked`/`as_sold`), `default_portion_g` with a label, package price and weight, `counts_toward_protein`, and a free-text note. |
| `meals` · `meal_items` | A name and a list of (ingredient, grams). **No macros are stored** — they are computed from the ingredient rows on every read. |
| `food_log` | One row per ingredient eaten, or a free-form estimate. **Macros and cost frozen at log time.** Entries from a standard meal carry its name so the day can group them. |
| `daily_targets` | One row per date: kcal, protein, fibre, `day_type`, and `source` (`suggested`/`confirmed`/`overridden`). |

`athlete.gender` was added alongside these. It is asked for one reason — Mifflin-St Jeor — and it
affects no training number.

## The freeze, and its exact opposite

These two rules look contradictory and are not:

- **A log entry freezes.** Its macros and its cost are computed once, from the ingredient as it stood,
  and stored on the row. Correcting a mistyped protein figure or a price that went up changes what
  happens from tomorrow and leaves every day already eaten exactly as it was. `update_ingredient`
  reports how many entries kept their old figures.
- **A meal does not.** It stores only ingredients and grams, so fixing a yoghurt's protein fixes every
  meal containing it.

A log entry is a measurement; a meal is a recipe. The one exception is `edit_log_entry` with a new
quantity, which recomputes — because there the athlete is restating what they ate, not re-reading old
data.

**Blank text never overwrites; `clear=[...]` is the only path to null.** `update_ingredient` and
`save_meal` (there is no separate `update_meal`) each take a `clear` list for their own optional
fields — a wrong package price or a stale note can return to unknown, the same erase-is-a-verb rule
`update_profile` and the other coach-layer update tools follow. `delete_ingredient` and `delete_meal`
remove a row outright: a meal always may, since every entry already logged from it keeps its own
frozen macros and meal name; an ingredient is refused while any log entry or meal still points at it.

## Resolving a name

Terse logging is the point: "skyr, 200" has to work. So every ingredient carries aliases, and names
are folded — lowercased, unaccented, punctuation collapsed — before comparison, which is also what the
uniqueness constraint sees. "Skyr", "skyr" and "SKYR" are one row.

**An exact match on the folded name or on any alias resolves. Everything else is refused**, with
near-matches attached — including a name that matches two rows, because an alias like "riz" can end up
on both the raw and the cooked entry and picking one would be a coin toss logged as fact. A fuzzy hit
taken as exact logs the wrong food, and the day's total looks entirely reasonable afterwards.

`search_ingredients` *is* allowed to be approximate. It is a lookup, not a write.

## Raw versus cooked

100 g of dry basmati is about 350 kcal. The same rice cooked is about 130. Both are "rice, 100 g" to
someone weighing a bowl, and the wrong one is a 220 kcal error on a side dish — a whole day's deficit,
invisible. `state` records which form the row describes, and storing a `raw` one returns a warning
saying to weigh it raw or add a second ingredient for the cooked form.

## Incomplete proteins

`counts_toward_protein: false` — collagen and the like. Their calories count in full; their protein
counts for nothing, because a protein target hit with a powder that does not do protein's job is not
hit. Every summary shows the entry and names the excluded grams.

## Estimates

Canteen food, a restaurant plate, someone's birthday cake: `{"label": ..., "kcal": ..., "is_estimate":
true}`. They count toward calories and protein, are flagged as approximate in every summary, and are
**never priced** — guessing what a restaurant cost per gram would poison the week's food cost with
numbers nobody measured. `week_summary` reports how many entries it could not price, because a total
treating them as free reads as a cheap week and is a wrong one.

`protein_g` and `fiber_g` are optional on an estimate, the way `carbs_g`/`fat_g` already were — a
restaurant plate states its calories and nothing else. An unstated figure comes back `null`, never a
folded `0`; `day_summary`'s totals still sum what *is* known (a day with nothing logged reports a real
`0.0`, not `null`) and name how many entries did not say, with a note that the remainder above
overstates by that much. A stated `protein_g: 0` (black coffee) still stores `0.0` and is not counted
as missing — a stated zero is a measurement.

## Suggesting a target

`suggest_targets` is deterministic and **stores nothing**. Every input and intermediate comes back in
`working` and `steps`, so the confirmation can be informed rather than a rubber stamp.

1. **BMR**, Mifflin-St Jeor: `10 x kg + 6.25 x cm - 5 x age`, then `+5` for men and `-161` for women.
   The weight is the one in effect on that date, from `weight_history`. `other` takes the midpoint
   (`-78`) and every target computed that way is flagged as the compromise it is. The equation is a
   population average — individual resting rates scatter around it by roughly ±10%.
2. **x 1.3** (`baseline_factor`) for everything that is not training. This must stay a *sedentary*
   factor: training is added separately, and folding a training multiplier in here is the commonest
   way a TDEE ends up 600 kcal too high.
3. **+ exercise calories.** Garmin's own figure from the imported activity where there is one — a
   measurement beats a model. For a future date, the planned session's mechanical work converted at
   cycling's gross efficiency (24%, and 4.184 kJ/kcal — which is why a ride's kJ and its kcal come out
   near enough equal). Where there is neither, zero, said out loud; `exercise_kcal_override` supplies
   an estimate for a race or an unimported ride. Where a date has an import AND a still-planned
   session that is not linked to that import, the two are summed — a second session that has not
   happened yet is not the same session as the one that has. A planned session that is completed,
   or explicitly linked to one of the day's activities (`link_activity` / `update_planned_workout`),
   is that same session under a different id and is never added on top.
4. **± the goal's rate**, at 7700 kcal/kg spread evenly across the week — **except** a deficit on a
   `big_session`, `race` or `race_eve` day, which is withheld. Under-fuelling a hard session costs
   the session and the recovery from it, and those are the least useful calories in the week to save.
   The response says how many were withheld, so the weekly average being slightly shallower than the
   stated rate is visible rather than a discrepancy. A **surplus** (a `gain` goal) is applied in full
   on those same days instead of withheld — under-fuelling is the risk a protected day runs, not
   over-fuelling.
5. **Clamped up to BMR.** A target below resting metabolic rate is not a target this server will
   produce. It clamps and says the goal's rate is what should change. `confirm_targets` refuses one
   outright, whoever asked.

Protein is `protein_g_per_kg` (default 2 g/kg — the endurance-in-a-deficit figure) x bodyweight,
rounded to 5 g. Fibre is 30 g, or 15 g on a `race` / `race_eve` day: not because fibre stopped being
good, but because it is still in the gut at kilometre 40.

With the profile incomplete — no weight, height, birth year or gender — this returns what is missing
rather than a guess. A BMR from an assumed gender is out by 166 kcal/day.

### Day type

Read off the training tables, never asked for:

| Day type | When |
|---|---|
| `race` | An event on that date. |
| `race_eve` | An event on the following date. |
| `big_session` | A ride — imported or planned — of 3 h or more, or 1500 kcal or more. |
| `training` | Any other ride or planned session. |
| `rest` | Nothing at all. |

## Reading a day, and a week

`day_summary` is the bilan: entries per slot, grouped under a meal's name where they came from one,
running totals, the day's targets and what remains. **"Can I eat X?" is answered from the remainder** —
a subtraction, shown — never as a bare yes or no.

`week_summary` is where the deficit is actually judged. Never on a single day: a restaurant on
Saturday inside a week that averaged on target is a week that went to plan. It reports daily rows,
weekly averages against the average target, the food cost, and the weight trend on the **7-day moving
average** of morning weigh-ins compared to the goal's rate. Day-to-day weight is water, glycogen and
salt — two kilos can appear after a race and be gone by Thursday — and the trend needs three or four
weeks before it means anything.

A day with nothing logged is reported as unlogged and excluded from the averages, not counted as a
zero-calorie day.

## Dates

Every tool takes the date rather than assuming it. The server's today is not reliably the athlete's,
and food is logged late at night and across timezones: a dinner filed on tomorrow breaks two days at
once. `edit_log_entry(log_date=...)` is the fix when it happens.

## A worked day

```
suggest_targets(date="2026-08-24")
  -> BMR 1682 x 1.3 = 2187, + 900 kcal from the imported ride = 3087 maintenance,
     -550/day for a -0.5 kg/week goal = 2537 kcal, 145 g protein, 30 g fibre
confirm_targets(date="2026-08-24")

log_meal(meal="Petit-dej", log_date="2026-08-24",
         overrides=[{"ingredient": "cruesli", "grams": 30}])
  -> 261 kcal, 24.4 g protein, grouped under "Petit-dej"

log_food(entries=[{"label": "canteen: chicken and chips", "kcal": 700,
                   "protein_g": 35, "fiber_g": 5, "is_estimate": true}],
         log_date="2026-08-24", slot="lunch")

day_summary(date="2026-08-24")
  -> 961 kcal, 59.4 g protein, 6.8 g fibre eaten
     remaining: 1576 kcal, 85.6 g protein, 23.2 g fibre
     1 entry is a free-form estimate; treat the totals as approximate by that much
```

The evening recommendation is then composed from that remainder and `list_meals` — protein first,
because it is the lagging macro, and fibre pushed at the meals that remain.

## What this layer will not do

No network: no barcode lookups, no food databases. The model can fetch and paste through
`add_ingredients`. No micronutrients, no hydration tracking, no photo recognition. No meal-plan
generation in code — composing a meal from a remainder is coaching, and that is the skill's job.

---

Back to the [README](../README.md), the [tool reference](tools.md), or [the coach layer](coaching.md).

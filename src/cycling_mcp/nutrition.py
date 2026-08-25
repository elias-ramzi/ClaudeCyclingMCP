"""The nutrition operations: the food base, the log over it, and the targets.

The same split as `coach.py`. This module does every gram of arithmetic — macro
sums, running totals, BMR, the day's remainder, the week's average — and the
bundled `nutrition` skill does the coaching over the answers. The division is
not stylistic: a model that adds up a day of food in its head gets a plausible
number, and a plausible calorie total is worse than none, because nobody checks
it.

Two rules run through everything here:

* **A logged entry is frozen.** Its macros and its cost are computed once, from
  the ingredient as it stood, and stored on the row. Correcting a mistyped
  protein figure today changes what happens tomorrow, never what happened in
  March.
* **A name is resolved or refused, never guessed.** Terse logging — "skyr, 200"
  — is the whole point of the aliases, but a near-match silently taken as an
  exact one logs the wrong food and there is nothing downstream that can tell.

Personal facts belong in the tables, not in the skill: an ingredient's note is
where "weigh it, this is where eyeballing drifts" and a rice-cooker water ratio
live.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import unicodedata
from datetime import date, timedelta
from typing import Any

from .metrics import compute_metrics
from .spec import SpecError, load_spec
from .store import DEFAULT_ATHLETE_ID, now_utc, open_db
from .training import _positive, parse_date
from .training import agree as _agree
from .training import one_of as _training_one_of
from .training import plural as _plural

GENDERS = ("male", "female", "other")
GOAL_TYPES = ("lose", "maintain", "gain")
GOAL_STATUSES = ("active", "reached", "abandoned")
INGREDIENT_STATES = ("raw", "cooked", "as_sold")
SLOTS = ("breakfast", "lunch", "dinner", "snack")
DAY_TYPES = ("rest", "training", "big_session", "race", "race_eve")
TARGET_SOURCES = ("suggested", "confirmed", "overridden")

#: Multiplies BMR to cover everything that is not training: work, walking,
#: digestion. Exercise is added separately from what the athlete actually rode,
#: so folding a training multiplier in here would count every session twice —
#: the single most common way a "TDEE" ends up 600 kcal too high.
SEDENTARY_FACTOR = 1.3

#: Energy in a kilogram of body mass. The classic 7700 kcal/kg figure — it is
#: an approximation for fat, and real weight change includes water and glycogen,
#: which is why the goal rate is judged over weeks and not days.
KCAL_PER_KG = 7700.0

#: Gross mechanical efficiency on a bike, and the kcal/kJ conversion. Together
#: they are why a ride's kJ of work and its kcal of cost are near enough the
#: same number: 1 kJ / 0.24 / 4.184 kJ-per-kcal = 0.996 kcal. This server uses
#: the two constants rather than the folk identity, so the assumption is
#: visible and adjustable.
GROSS_EFFICIENCY = 0.24
KJ_PER_KCAL = 4.184

#: Protein per kilogram of bodyweight. 2 g/kg is the endurance-in-a-deficit
#: figure: enough to hold muscle while under-eating, which is the whole risk of
#: a cutting phase for a cyclist.
DEFAULT_PROTEIN_G_PER_KG = 2.0
DEFAULT_FIBER_G = 30.0
#: Race day and the evening before. Fibre is reduced, not because it stopped
#: being good for the athlete, but because it is still in the gut at kilometre
#: 40.
RACE_FIBER_G = 15.0

#: What makes a day a `big_session` rather than ordinary training. Either the
#: ride is long enough that under-fuelling it costs the session, or it is hard
#: enough to do the same in less time.
BIG_SESSION_DURATION_S = 3 * 3600
BIG_SESSION_KCAL = 1500.0

#: Days either side of a window that a weight moving average may reach into.
#: A 7-day mean needs the six days before the first one it reports.
WEIGHT_TREND_WINDOW_DAYS = 7

#: The widest range the week tools will walk in one call. Long enough for a
#: quarter, short enough that "the whole year" is a refusal and not a timeout.
MAX_RANGE_DAYS = 120

#: Plausibility limits, in the same spirit as the coach layer's. Outside these
#: a number is far more likely to be the wrong field or the wrong unit than a
#: real food: 900 kcal/100 g is pure fat and nothing exceeds it.
KCAL_100G_LIMITS = (0.0, 900.0)
MACRO_100G_LIMITS = (0.0, 100.0)
GRAMS_LIMITS = (0.0, 5000.0)
RATE_KG_PER_WEEK_LIMITS = (-2.0, 2.0)
#: Faster than this and the loss is water and muscle, not fat. Stored anyway —
#: refusing an athlete's stated goal helps nobody — but said out loud.
BRISK_RATE_KG_PER_WEEK = 1.0

_INGREDIENT_OUT_FIELDS = (
    "id",
    "name",
    "aliases",
    "kcal_100g",
    "protein_100g",
    "fiber_100g",
    "carbs_100g",
    "fat_100g",
    "sat_fat_100g",
    "sugar_100g",
    "salt_100g",
    "state",
    "default_portion_g",
    "portion_label",
    "package_price",
    "package_weight_g",
    "cost_per_100g",
    "counts_toward_protein",
    "note",
)

_LOG_OUT_FIELDS = (
    "id",
    "log_date",
    "slot",
    "ingredient_id",
    "label",
    "grams",
    "kcal",
    "protein_g",
    "fiber_g",
    "carbs_g",
    "fat_g",
    "cost",
    "counts_toward_protein",
    "is_estimate",
    "meal_id",
    "meal_name",
    "note",
)

#: The macro columns an entry carries, and how each is summed. `protein_g` is
#: absent on purpose: it is the one total that excludes rows, so it is computed
#: where that exclusion is visible rather than hidden in a loop.
_SUMMED_MACROS = ("kcal", "fiber_g", "carbs_g", "fat_g")

#: Of those, the ones whose *total* comes back null on a day that logged
#: nothing of that macro. `kcal` is NOT NULL on every entry — an estimate still
#: states a kcal figure — so its sum is always a real 0.0 on an empty day,
#: which is what a day with targets and nothing logged yet has to be able to
#: report. `fiber_g` used to be NOT NULL too, but a free-form estimate may not
#: state it (migration 5); an unstated fibre figure is summed over the entries
#: that *did* state one — still 0.0 on an empty or all-unknown day, never
#: null, with `fiber_g_missing_entries` reporting the ones that did not say.
#: Carbohydrate and fat stay genuinely optional per *ingredient*, so a total of
#: 0 for a day of bread and rice would be a wrong number wearing the shape of a
#: real one; those come back null instead when nothing logged carries one.
_OPTIONAL_MACROS = ("carbs_g", "fat_g")

#: A meal's own optional fields, clearable via `save_meal(clear=[...])`. See
#: `CLEARABLE_INGREDIENT_FIELDS` for the criterion this and it both apply.
CLEARABLE_MEAL_FIELDS = ("note", "default_for_slot")

#: What `update_ingredient(clear=[...])` may erase. The criterion is the same
#: one `coach.py`'s `CLEARABLE_*` tuples use (see coach.py:88-94): empty is a
#: real state the record can be in, not "unknown until corrected". `name`,
#: `kcal_100g`, `protein_100g`, `fiber_100g` and `state` are excluded because
#: every target this server computes is built from them — an ingredient with
#: no calories is not "unknown calories", it is a corrupt row — and
#: `counts_toward_protein` is excluded because it is a boolean with no third
#: "unset" state to return to. Everything else here is a fact the athlete may
#: simply not have (no default portion, no note, no package price) and must be
#: able to say so about again after correcting it to the wrong thing.
CLEARABLE_INGREDIENT_FIELDS = (
    "carbs_100g",
    "fat_100g",
    "sat_fat_100g",
    "sugar_100g",
    "salt_100g",
    "default_portion_g",
    "portion_label",
    "package_price",
    "package_weight_g",
    "note",
)


class NutritionError(ValueError):
    """A refusal with a reason the caller can act on."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _dict(row: sqlite3.Row | None) -> dict | None:
    return None if row is None else dict(zip(row.keys(), tuple(row), strict=True))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_PUNCTUATION = re.compile(r"[^a-z0-9]+")


def fold(value: str) -> str:
    """The comparison form of a name: lowercase, unaccented, punctuation gone.

    "Skyr nature 0%", "skyr nature 0 %" and "SKYR NATURE 0%" are one ingredient
    and one row. The athlete types a food base in their own language, so the
    accents are not decoration — "protéine" and "proteine" have to be the same
    key or half the base is unreachable from a phone keyboard.

    Decomposition then dropping the combining marks, rather than a hand-written
    table: it covers every accented Latin letter, and languages whose
    diacritics change the word entirely are not what this base is written in.
    """
    decomposed = unicodedata.normalize("NFKD", str(value).strip().lower())
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _PUNCTUATION.sub(" ", stripped).strip()


def _number(value: Any, what: str, limits: tuple[float, float] | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        # OverflowError: a JSON integer has no size limit, so a pasted 400-digit
        # kcal_100g reaches float() directly — unlike a Python literal, there is
        # no earlier point where a too-large int could have been refused. Caught
        # here so it reads as an ordinary refusal naming the field, not a crash;
        # `_coach`'s own OverflowError clause is the backstop, not the fix.
        raise NutritionError(f"{what} must be a number, got {value!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise NutritionError(f"{what} must be a real number, got {value!r}")
    if limits is not None:
        low, high = limits
        if not (low <= number <= high):
            raise NutritionError(
                f"{what} of {number:g} is outside {low:g}-{high:g} — refusing to store it. "
                f"Check the units: per-100 g figures, grams for a weight."
            )
    return number


def _optional_number(
    value: Any, what: str, limits: tuple[float, float] | None = None
) -> float | None:
    return None if value is None else _number(value, what, limits)


def _one_of(value: Any, allowed: tuple[str, ...], what: str) -> str | None:
    """See `training.one_of`: case-insensitive, returns the stored spelling.

    Re-raised as `NutritionError` rather than left as the shared helper's plain
    `ValueError` (or `coach`'s `CoachError`) so every `except NutritionError`
    around a per-item loop in this module — `add_ingredients`, `log_food`,
    `confirm_targets` — keeps catching it. `coach.CoachError` and
    `NutritionError` are unrelated `ValueError` subclasses, not a hierarchy;
    swapping the type here would let one bad row abort the whole bulk call
    instead of being rejected on its own.
    """
    try:
        return _training_one_of(value, allowed, what)
    except ValueError as exc:
        raise NutritionError(str(exc)) from exc


_BOOL_TRUE_SPELLINGS = ("true", "1", "yes")
_BOOL_FALSE_SPELLINGS = ("false", "0", "no")


def _bool(value: Any, what: str, default: bool = True) -> bool:
    """A boolean from whatever shape a paste hands us, or a named refusal.

    `bool(value)` — the previous body — takes any non-empty string as truthy,
    so a spreadsheet-derived `counts_toward_protein: "false"` stored `True` and
    silently inverted the incomplete-protein exclusion. `None` means "not
    given" and takes `default`; an actual bool passes through; the usual string
    and int spellings of true/false are recognised case-insensitively; anything
    else is refused by name rather than guessed.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise NutritionError(f"{what} must be true or false, got {value!r}")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _BOOL_TRUE_SPELLINGS:
            return True
        if text in _BOOL_FALSE_SPELLINGS:
            return False
    raise NutritionError(f"{what} must be true or false, got {value!r}")


def _clear_fields(updates: dict[str, Any], clear: Any, clearable: tuple[str, ...]) -> list[str]:
    """See `coach._stage_clear` — the only path to a NULL, ported rather than reinvented.

    Deferred import for the same reason as `NON_STARTING_EVENT_STATUSES`: `coach.py`
    imports `GENDERS` from this module at load time, so a module-level import in
    this direction would be a real circular one. Re-raised as `NutritionError` so
    a caller of `update_ingredient`/`save_meal` sees this module's own refusal
    type, the same as every other refusal here — `coach.CoachError` and
    `NutritionError` are unrelated `ValueError` subclasses, not a hierarchy.
    """
    from .coach import CoachError, _stage_clear

    try:
        return _stage_clear(updates, clear, clearable)
    except CoachError as exc:
        raise NutritionError(str(exc)) from exc


def _round(value: float | None, places: int = 1) -> float | None:
    return None if value is None else round(value, places)


def _today(value: str | None = None) -> date:
    """The reference "today", explicit when given.

    Food is logged across midnight and across timezones — the server's date is
    not reliably the athlete's, and a late dinner filed on the wrong day both
    breaks that day's remainder and hands the next day a head start it did not
    earn. Every tool that needs a date takes one.
    """
    return parse_date(value, "today") if value else date.today()


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _range(start: str, end: str) -> tuple[date, date]:
    first = parse_date(start, "start")
    last = parse_date(end, "end")
    if last < first:
        raise NutritionError(f"end ({end}) is before start ({start})")
    span = (last - first).days + 1
    if span > MAX_RANGE_DAYS:
        raise NutritionError(
            f"{start}..{end} is {span} days; this tool walks at most {MAX_RANGE_DAYS}. "
            f"Ask for a shorter range."
        )
    return first, last


def _ensure_athlete(conn: sqlite3.Connection, athlete_id: int) -> dict:
    """The athlete row, created empty if this is the first call.

    Byte-identical to `coach._ensure_athlete` today — both query the one
    `athlete` table. Left duplicated rather than merged: it is a DB-touching
    helper, and the one cycle-free shared module (`training.py`, home to
    `one_of`/`_positive`) is documented as pure arithmetic with no store
    access — moving a `conn.execute` there would blur that boundary for a
    four-line function. `store.py` is the module that "touches state", but
    every caller here already goes through `coach.py`/`nutrition.py`'s own
    `open_db()` — revisit if a third module ever needs this exact upsert.
    """
    row = conn.execute("SELECT * FROM athlete WHERE athlete_id = ?", (athlete_id,)).fetchone()
    if row is None:
        stamp = now_utc()
        conn.execute(
            "INSERT INTO athlete (athlete_id, created_at, updated_at) VALUES (?, ?, ?)",
            (athlete_id, stamp, stamp),
        )
        row = conn.execute("SELECT * FROM athlete WHERE athlete_id = ?", (athlete_id,)).fetchone()
    return _dict(row) or {}


def _latest_weight(conn: sqlite3.Connection, athlete_id: int, on_date: str) -> dict | None:
    """The weigh-in in effect on a date — the latest at or before it.

    Deliberately does not extrapolate backwards the way `coach.resolve_ftp`
    does. A ride from before the first weigh-in still has to be scored somehow;
    a calorie target does not, and inventing a weight would quietly invent a
    BMR to go with it.
    """
    row = conn.execute(
        "SELECT * FROM weight_history WHERE athlete_id = ? AND effective_date <= ? "
        "ORDER BY effective_date DESC, id DESC LIMIT 1",
        (athlete_id, on_date),
    ).fetchone()
    return _dict(row)


# --------------------------------------------------------------------------
# ingredients
# --------------------------------------------------------------------------


def _aliases(value: Any) -> list[str]:
    """Normalise whatever the caller called aliases into a clean list."""
    if value is None:
        return []
    if isinstance(value, str):
        # "skyr, skyr nature" — one field, several names. A model pasting a
        # food base writes it either way and neither spelling should be a
        # rejected row.
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise NutritionError(f"aliases must be a list of names, got {type(value).__name__}")
    cleaned: list[str] = []
    for item in items:
        text = _text(item)
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _ingredient_out(row: dict) -> dict:
    """One ingredient as callers see it, with cost/100 g derived not stored.

    Derived because the price and the package weight are the two figures the
    athlete actually knows, and storing a third that must agree with them is a
    third thing to keep in step. A package with no price simply has no cost,
    which the week's total then reports as excluded rather than as zero.
    """
    out = {field: row.get(field) for field in _INGREDIENT_OUT_FIELDS}
    out["aliases"] = json.loads(row["aliases_json"]) if row.get("aliases_json") else []
    out["counts_toward_protein"] = bool(row.get("counts_toward_protein", 1))
    out["cost_per_100g"] = cost_per_100g(row)
    return out


def cost_per_100g(row: dict) -> float | None:
    """Price per 100 g from the package price and weight, or None.

    A zero (or negative) price is unpriced, not free: `package_price: 0` is the
    natural way to escape a required-looking field, and treating it as a real
    €0.00 makes `cost_per_100g` 0.0 — counted as *priced* by every reader that
    checks for `None` — which quietly deflates the week's food cost instead of
    reporting it as unpriced the way an absent price already does.
    """
    price = row.get("package_price")
    weight = row.get("package_weight_g")
    if price is None or not weight or float(price) <= 0 or float(weight) <= 0:
        return None
    return round(float(price) * 100.0 / float(weight), 4)


def _ingredient_fields(item: dict, existing: dict | None = None) -> dict:
    """Validate and coerce one ingredient's fields. Shared by add and update.

    `existing` is the stored row on an update, so a partial update inherits
    what it did not mention — and so the required macros can stay required on
    an insert without forcing them to be repeated on every edit.
    """
    base = existing or {}
    fields: dict[str, Any] = {}

    name = _text(item.get("name")) or (base.get("name") if base else None)
    if not name:
        raise NutritionError("an ingredient needs a name")
    fields["name"] = name
    fields["name_key"] = fold(name)
    if not fields["name_key"]:
        raise NutritionError(f"{name!r} folds to an empty name — give it letters or digits")

    if "aliases" in item or not base:
        fields["aliases_json"] = json.dumps(_aliases(item.get("aliases")), ensure_ascii=False)

    for column, key, limits in (
        ("kcal_100g", "kcal_100g", KCAL_100G_LIMITS),
        ("protein_100g", "protein_100g", MACRO_100G_LIMITS),
        ("fiber_100g", "fiber_100g", MACRO_100G_LIMITS),
    ):
        if item.get(key) is not None:
            fields[column] = _number(item[key], f"{key}", limits)
        elif not base:
            raise NutritionError(
                f"{name}: {key} is required. Calories, protein and fibre are what every target "
                f"is computed from, and a missing one reads as zero. The optional macros "
                f"(carbs, fat, sat_fat, sugar, salt) can be left out."
            )

    for column in ("carbs_100g", "fat_100g", "sat_fat_100g", "sugar_100g", "salt_100g"):
        if item.get(column) is not None:
            fields[column] = _number(item[column], column, MACRO_100G_LIMITS)

    state = _one_of(item.get("state"), INGREDIENT_STATES, "state")
    if state is not None:
        fields["state"] = state
    elif not base:
        fields["state"] = "as_sold"

    if item.get("default_portion_g") is not None:
        fields["default_portion_g"] = _number(
            item["default_portion_g"], "default_portion_g", GRAMS_LIMITS
        )
    for column in ("portion_label", "note"):
        if item.get(column) is not None:
            fields[column] = _text(item[column])
    if item.get("package_price") is not None:
        fields["package_price"] = _number(item["package_price"], "package_price", (0.0, 10000.0))
    if item.get("package_weight_g") is not None:
        fields["package_weight_g"] = _number(
            item["package_weight_g"], "package_weight_g", (0.0, 100000.0)
        )
    if item.get("counts_toward_protein") is not None:
        fields["counts_toward_protein"] = (
            1 if _bool(item["counts_toward_protein"], "counts_toward_protein") else 0
        )
    elif not base:
        fields["counts_toward_protein"] = 1
    return fields


def _state_warning(fields: dict) -> str | None:
    """The raw-vs-cooked warning, which is worth a lot more than it looks.

    100 g of dry rice is about 350 kcal; the same rice cooked is about 130.
    Both are "rice, 100 g" to someone weighing a bowl, and the wrong one is a
    220 kcal error on a single side dish — a whole day's deficit, invisible.
    """
    if fields.get("state") != "raw":
        return None
    return (
        f"{fields['name']} is recorded raw. Weigh it raw when logging, or add a second "
        f"ingredient for the cooked form — cooked starch is roughly a third of the calories "
        f"per 100 g, so mixing the two silently doubles or halves the entry."
    )


def add_ingredients(items: Any, athlete_id: int = DEFAULT_ATHLETE_ID) -> dict:
    """Add ingredients in bulk. Per-item accept or reject, with the reason.

    Built for onboarding: the athlete pastes whatever food base they already
    keep — a spreadsheet, a note on their phone — and the model passes the lot
    in one call. One bad row does not lose the rest, and a name already stored
    is reported as a duplicate rather than overwriting what is there (use
    `update_ingredient` for that, which is a deliberate act).

    Per-100 g figures throughout, never per portion: the portion is
    `default_portion_g` and the label beside it. Mixing the two is the error
    that makes an egg 155 kcal instead of 85.

    `state` matters more than it looks — see the warning on `raw`.
    """
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, (list, tuple)):
        raise NutritionError(f"pass a list of ingredients, got {type(items).__name__}")

    inserted: list[dict] = []
    rejected: list[dict] = []
    warnings: list[str] = []
    stamp = now_utc()

    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                rejected.append(
                    {"index": index, "reason": f"expected an object, got {type(item).__name__}"}
                )
                continue
            try:
                fields = _ingredient_fields(item)
            except NutritionError as exc:
                rejected.append({"index": index, "name": item.get("name"), "reason": str(exc)})
                continue

            clash = conn.execute(
                "SELECT id, name FROM ingredients WHERE athlete_id = ? AND name_key = ?",
                (athlete_id, fields["name_key"]),
            ).fetchone()
            if clash is not None:
                rejected.append(
                    {
                        "index": index,
                        "name": fields["name"],
                        "reason": (
                            f"already stored as {clash['name']!r} (id {clash['id']}). Nothing "
                            f"was changed — use update_ingredient to correct it."
                        ),
                    }
                )
                continue

            columns = [*fields, "athlete_id", "created_at", "updated_at"]
            values = [*fields.values(), athlete_id, stamp, stamp]
            marks = ", ".join("?" for _ in columns)
            cursor = conn.execute(
                f"INSERT INTO ingredients ({', '.join(columns)}) VALUES ({marks})", values
            )
            stored = _dict(
                conn.execute(
                    "SELECT * FROM ingredients WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
            )
            assert stored is not None
            inserted.append(_ingredient_out(stored))
            warning = _state_warning(fields)
            if warning:
                warnings.append(warning)

    result: dict[str, Any] = {
        "seen": len(items),
        "inserted": len(inserted),
        "rejected": len(rejected),
        "ingredients": inserted,
        "rejections": rejected,
    }
    if warnings:
        result["warnings"] = warnings
    return result


def update_ingredient(
    ingredient_id: int | None = None,
    name: str | None = None,
    new_name: str | None = None,
    clear: list[str] | str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
    **changes: Any,
) -> dict:
    """Correct a stored ingredient. Only the fields given are touched.

    **This never rewrites history.** Every food_log entry froze its macros and
    its cost when it was logged, so fixing a protein figure or a package price
    changes what happens from now on and leaves last month's days exactly as
    they were eaten. That is the point: a day that has been lived is a
    measurement, not a view over current data.

    Identify the ingredient by `ingredient_id`, or by `name` — which resolves
    the same way logging does, exactly or not at all. `new_name` renames it;
    `name` is the lookup and never the new value, because one argument doing
    both would make every rename indistinguishable from a mistyped lookup.

    `clear=[...]` erases an optional field back to unknown — `package_price`,
    `package_weight_g`, `default_portion_g`, `portion_label`, `note`, or any of
    the optional macros. There was previously no way back to "unknown" once a
    wrong price or portion had been stored over the right one; a bare field
    given as blank text is still ignored rather than stored, so `clear` is the
    only path to a null here, the same as everywhere else in this server.
    """
    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        row = _require_ingredient(conn, athlete_id, ingredient_id, name)
        fields = _ingredient_fields({**changes, "name": new_name}, existing=row)
        if new_name is None:
            # `_ingredient_fields` always emits a name and its key, because it
            # needs one to validate against — but nothing was asked about the
            # name here, so nothing about it should move.
            fields.pop("name", None)
            fields.pop("name_key", None)
        if fields.get("name_key") and fields["name_key"] != row["name_key"]:
            clash = conn.execute(
                "SELECT id, name FROM ingredients WHERE athlete_id = ? AND name_key = ? "
                "AND id != ?",
                (athlete_id, fields["name_key"], row["id"]),
            ).fetchone()
            if clash is not None:
                raise NutritionError(
                    f"another ingredient is already stored as {clash['name']!r} "
                    f"(id {clash['id']}). Nothing was changed."
                )
        cleared = _clear_fields(fields, clear, CLEARABLE_INGREDIENT_FIELDS)
        if not fields:
            raise NutritionError("nothing to update — pass at least one field, or clear=[...]")

        assignments = ", ".join(f"{key} = ?" for key in fields)
        conn.execute(
            f"UPDATE ingredients SET {assignments}, updated_at = ? WHERE id = ?",
            (*fields.values(), now_utc(), row["id"]),
        )
        stored = _dict(
            conn.execute("SELECT * FROM ingredients WHERE id = ?", (row["id"],)).fetchone()
        )
        assert stored is not None
        logged = conn.execute(
            "SELECT COUNT(*) AS n FROM food_log WHERE ingredient_id = ?", (row["id"],)
        ).fetchone()["n"]

    result: dict[str, Any] = {
        "updated_fields": sorted(fields),
        "ingredient": _ingredient_out(stored),
    }
    if cleared:
        result["cleared_fields"] = cleared
    if logged:
        result["history_note"] = (
            f"{_plural(logged, 'existing log entry')} kept the macros and cost "
            f"they were logged with. This edit applies from the next entry onward."
        )
    warning = _state_warning({**stored, **fields})
    if warning:
        result["warnings"] = [warning]
    return result


def delete_ingredient(
    ingredient_id: int | None = None,
    name: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Remove a stored ingredient — refused while anything still points at it.

    A logged entry or a saved meal referencing this row is load-bearing: a
    food_log row's macros are frozen at log time (so deleting the ingredient
    would not corrupt a past day), but `edit_log_entry` recomputes from the
    ingredient row on a new weight, and a meal's macros are computed from it
    on every read — deleting out from under either turns a correction into
    silent data loss. Refused by name and count rather than cascading; correct
    the row with `update_ingredient` instead, or delete the meals/entries that
    use it first if it genuinely should not exist.
    """
    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        row = _require_ingredient(conn, athlete_id, ingredient_id, name)
        logged = conn.execute(
            "SELECT COUNT(*) AS n FROM food_log WHERE ingredient_id = ?", (row["id"],)
        ).fetchone()["n"]
        in_meals = conn.execute(
            "SELECT COUNT(*) AS n FROM meal_items WHERE ingredient_id = ?", (row["id"],)
        ).fetchone()["n"]
        if logged or in_meals:
            parts = []
            if logged:
                parts.append(_plural(logged, "logged entry"))
            if in_meals:
                parts.append(_plural(in_meals, "meal item"))
            raise NutritionError(
                f"{row['name']!r} is still referenced by {' and '.join(parts)} and cannot be "
                f"deleted. Nothing was changed — correct it with update_ingredient, or remove "
                f"the entries/meals that use it first."
            )
        conn.execute("DELETE FROM ingredients WHERE id = ?", (row["id"],))
    return {"deleted": {"id": row["id"], "name": row["name"]}}


def _load_ingredients(conn: sqlite3.Connection, athlete_id: int) -> list[dict]:
    """Every ingredient, read once. A food base is tens to hundreds of rows.

    Whole-table reads are the right shape here for the same reason they are in
    `coach.History`: the resolver has to compare a typed name against every
    name *and every alias*, and doing that in SQL means either a query per
    candidate or JSON functions that vary by SQLite build.
    """
    return [
        _dict(row) or {}
        for row in conn.execute(
            "SELECT * FROM ingredients WHERE athlete_id = ? ORDER BY name", (athlete_id,)
        )
    ]


def _keys_of(row: dict) -> list[str]:
    """Every folded name this ingredient answers to."""
    aliases = json.loads(row["aliases_json"]) if row.get("aliases_json") else []
    return [fold(row["name"]), *[fold(alias) for alias in aliases]]


def _suggestions(query: str, rows: list[dict], limit: int = 5) -> list[dict]:
    """The nearest stored names to something that did not resolve.

    Substring first — a partial name is what someone actually types — then
    fuzzy, so a typo still finds its target. Returned as suggestions and never
    applied: the tool's job is to say "did you mean", not to decide.
    """
    key = fold(query)
    scored: list[tuple[float, dict]] = []
    for row in rows:
        best = 0.0
        for candidate in _keys_of(row):
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, key, candidate).ratio()
            if key and (key in candidate or candidate in key):
                ratio = max(ratio, 0.9)
            best = max(best, ratio)
        if best >= 0.55:
            scored.append((best, row))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    return [
        {"id": row["id"], "name": row["name"], "aliases": json.loads(row["aliases_json"] or "[]")}
        for _, row in scored[:limit]
    ]


def resolve_ingredient(rows: list[dict], query: Any) -> dict:
    """One ingredient from a typed name or an id — exactly, or a refusal.

    An exact match on the folded name or on any alias resolves. Anything else
    is refused with near-matches attached, including a name that matches two
    rows: the alias sets are the athlete's own, "riz" can end up on both the
    raw and the cooked entry, and picking one of them would be a coin toss
    logged as fact.

    Never a near-match. The failure mode this exists to prevent is silent: a
    fuzzy hit logs a plausible food, the day's total looks entirely reasonable,
    and nothing downstream can tell that the athlete ate something else.
    """
    if isinstance(query, bool):
        raise NutritionError(f"{query!r} is not an ingredient reference")
    if isinstance(query, int) or (isinstance(query, str) and query.strip().isdigit()):
        wanted = int(query)
        for row in rows:
            if row["id"] == wanted:
                return row
        raise NutritionError(f"no ingredient with id {wanted}")

    text = _text(query)
    if not text:
        raise NutritionError("give an ingredient name or id")
    key = fold(text)
    matches = [row for row in rows if key in _keys_of(row)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(f"{row['name']!r} (id {row['id']})" for row in matches)
        raise NutritionError(
            f"{text!r} matches more than one ingredient: {names}. Log it by id, or take the "
            f"shared alias off one of them."
        )
    near = _suggestions(text, rows)
    if near:
        listed = ", ".join(f"{row['name']!r} (id {row['id']})" for row in near)
        raise NutritionError(f"no ingredient named {text!r}. Nearest stored: {listed}.")
    raise NutritionError(
        f"no ingredient named {text!r}, and nothing stored is close to it. Add it with "
        f"add_ingredients, or log it as a free-form estimate."
    )


def _require_ingredient(
    conn: sqlite3.Connection,
    athlete_id: int,
    ingredient_id: int | None,
    name: str | None,
) -> dict:
    rows = _load_ingredients(conn, athlete_id)
    if ingredient_id is None and name is None:
        raise NutritionError("give ingredient_id or name")
    return resolve_ingredient(rows, ingredient_id if ingredient_id is not None else name)


def search_ingredients(
    query: str | None = None,
    limit: int = 25,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Find ingredients by name or alias, tolerant of accents and casing.

    Unlike logging, this *is* allowed to be approximate — it is a lookup, not a
    write. Omit `query` to list the whole base, which is what onboarding wants
    after a bulk paste.
    """
    with open_db() as conn:
        rows = _load_ingredients(conn, athlete_id)

    if not _text(query):
        listed = rows[: max(1, int(limit))]
        return {
            "query": None,
            "stored": len(rows),
            "matches": [_ingredient_out(row) for row in listed],
            "truncated": len(rows) > len(listed),
        }

    key = fold(str(query))
    exact = [row for row in rows if key in _keys_of(row)]
    partial = [
        row
        for row in rows
        if row not in exact and any(key and key in candidate for candidate in _keys_of(row))
    ]
    fuzzy_ids = {item["id"] for item in _suggestions(str(query), rows)}
    fuzzy = [
        row for row in rows if row["id"] in fuzzy_ids and row not in exact and row not in partial
    ]
    ordered = [*exact, *partial, *fuzzy][: max(1, int(limit))]
    return {
        "query": query,
        "stored": len(rows),
        "exact_matches": len(exact),
        "matches": [_ingredient_out(row) for row in ordered],
        "note": (
            "An exact name or alias match is the only thing log_food will accept; the partial "
            "and near matches here are for finding a name, not for logging one."
        ),
    }


# --------------------------------------------------------------------------
# macros
# --------------------------------------------------------------------------


def macros_for(row: dict, grams: float) -> dict:
    """The macros and cost of `grams` of one ingredient. The whole arithmetic.

    Everything per 100 g scaled by grams/100, rounded once at the end. Cost
    comes out None rather than 0 when the ingredient has no price, so a week's
    food cost can say how much of the log it could not price instead of
    quietly reporting a total that is too low.
    """
    factor = grams / 100.0
    out: dict[str, Any] = {
        "grams": round(grams, 1),
        "kcal": round(row["kcal_100g"] * factor, 1),
        "protein_g": round(row["protein_100g"] * factor, 1),
        "fiber_g": round(row["fiber_100g"] * factor, 1),
    }
    for column, key in (("carbs_100g", "carbs_g"), ("fat_100g", "fat_g")):
        value = row.get(column)
        out[key] = None if value is None else round(value * factor, 1)
    per_100g = cost_per_100g(row)
    out["cost"] = None if per_100g is None else round(per_100g * factor, 2)
    return out


def _quantity(entry: dict, ingredient: dict) -> float:
    """Grams from either `grams` or `portions`, or a refusal that says which.

    A portion is only a quantity if the ingredient carries one. Silently
    treating `portions: 1` as 1 gram, or as 100, is the kind of error that
    makes a pot of skyr disappear from a day's total.

    Zero or negative is refused here, for both `grams` and `portions`: a `0 g`
    placeholder row logs a day as `logged: true` at ~0 kcal, which is not "ate
    nothing", it is "someone forgot the weight" — and it deflates every
    average that treats a logged day as a real one. `log_meal`'s own
    `overrides=[{"grams": 0}]` is the one place a zero is meaningful ("leave
    this out today"); that caller detects the explicit zero and never reaches
    this function with it — see the override loop.
    """
    grams = entry.get("grams")
    portions = entry.get("portions")
    if grams is not None and portions is not None:
        raise NutritionError(
            f"{ingredient['name']}: give grams or portions, not both — they disagree by "
            f"definition when the portion is not exactly that many grams."
        )
    if grams is not None:
        value = _number(grams, "grams", GRAMS_LIMITS)
        if value <= 0:
            raise NutritionError(
                f"{ingredient['name']}: grams must be greater than zero — a zero-gram entry is "
                f"a placeholder, not a food."
            )
        return value
    if portions is None:
        raise NutritionError(f"{ingredient['name']}: give grams or portions")
    count = _number(portions, "portions", (0.0, 100.0))
    if count <= 0:
        raise NutritionError(
            f"{ingredient['name']}: portions must be greater than zero — a zero-portion entry "
            f"is a placeholder, not a food."
        )
    portion = ingredient.get("default_portion_g")
    if not portion:
        raise NutritionError(
            f"{ingredient['name']} has no default portion stored, so 'portions' has no "
            f"meaning for it. Give grams, or set default_portion_g with update_ingredient."
        )
    return count * float(portion)


def _entry_row(
    entry: dict,
    rows: list[dict],
    log_date: str,
    slot: str,
    meal: dict | None = None,
) -> tuple[dict, str | None]:
    """One food_log row, ready to insert, from one caller-supplied entry.

    Two shapes come through here. An ingredient reference plus a quantity is
    computed from the stored row. A free-form estimate — canteen food, a
    restaurant plate, someone's birthday cake — carries its own numbers, is
    marked `is_estimate`, and is never priced: guessing what a restaurant meal
    cost per gram would poison the week's food cost with numbers nobody
    measured.
    """
    stamp = now_utc()
    base = {
        "log_date": log_date,
        "slot": slot,
        "note": _text(entry.get("note")),
        "meal_id": meal["id"] if meal else None,
        "meal_name": meal["name"] if meal else None,
        "logged_at": stamp,
        "updated_at": stamp,
    }

    if _bool(entry.get("is_estimate"), "is_estimate", default=False) or (
        entry.get("ingredient") is None
        and entry.get("ingredient_id") is None
        and entry.get("name") is None
    ):
        label = _text(entry.get("label")) or _text(entry.get("name"))
        if not label:
            raise NutritionError(
                "an estimate needs a label — what was eaten, in the athlete's own words"
            )
        kcal = _number(entry.get("kcal"), f"{label}: kcal", (0.0, 20000.0))
        return (
            {
                **base,
                "ingredient_id": None,
                "label": label,
                "grams": _optional_number(entry.get("grams"), "grams", GRAMS_LIMITS),
                "kcal": kcal,
                # Unlike kcal, protein and fibre are genuinely optional on an
                # estimate: a restaurant plate states its calories and nothing
                # else. `entry.get("protein_g") or 0` folded "not stated" and
                # "stated as zero" into the same number — `_optional_number`
                # keeps them apart; a real 0 (black coffee) still stores 0.0.
                "protein_g": _optional_number(
                    entry.get("protein_g"), f"{label}: protein_g", (0.0, 500.0)
                ),
                "fiber_g": _optional_number(
                    entry.get("fiber_g"), f"{label}: fiber_g", (0.0, 200.0)
                ),
                "carbs_g": _optional_number(entry.get("carbs_g"), "carbs_g", (0.0, 2000.0)),
                "fat_g": _optional_number(entry.get("fat_g"), "fat_g", (0.0, 1000.0)),
                # Never priced. See the docstring.
                "cost": None,
                "counts_toward_protein": 1,
                "is_estimate": 1,
            },
            None,
        )

    reference = entry.get("ingredient")
    if reference is None:
        reference = entry.get("ingredient_id")
    if reference is None:
        reference = entry.get("name")
    ingredient = resolve_ingredient(rows, reference)
    grams = _quantity(entry, ingredient)
    macros = macros_for(ingredient, grams)
    return (
        {
            **base,
            "ingredient_id": ingredient["id"],
            # The name as it stands now, frozen with the macros: an entry has
            # to stay readable after a rename, and a NULLed ingredient_id must
            # not leave a row saying nothing about what was eaten.
            "label": ingredient["name"],
            "grams": macros["grams"],
            "kcal": macros["kcal"],
            "protein_g": macros["protein_g"],
            "fiber_g": macros["fiber_g"],
            "carbs_g": macros["carbs_g"],
            "fat_g": macros["fat_g"],
            "cost": macros["cost"],
            "counts_toward_protein": 1 if ingredient.get("counts_toward_protein", 1) else 0,
            "is_estimate": 0,
        },
        _state_warning(ingredient) if ingredient.get("state") == "raw" else None,
    )


def _insert_entries(conn: sqlite3.Connection, athlete_id: int, prepared: list[dict]) -> list[dict]:
    stored: list[dict] = []
    for row in prepared:
        columns = ["athlete_id", *row]
        values = [athlete_id, *row.values()]
        marks = ", ".join("?" for _ in columns)
        cursor = conn.execute(
            f"INSERT INTO food_log ({', '.join(columns)}) VALUES ({marks})", values
        )
        written = _dict(
            conn.execute("SELECT * FROM food_log WHERE id = ?", (cursor.lastrowid,)).fetchone()
        )
        assert written is not None
        stored.append(_log_out(written))
    return stored


def _log_out(row: dict) -> dict:
    out = {field: row.get(field) for field in _LOG_OUT_FIELDS}
    out["counts_toward_protein"] = bool(row.get("counts_toward_protein", 1))
    out["is_estimate"] = bool(row.get("is_estimate", 0))
    return out


# --------------------------------------------------------------------------
# meals
# --------------------------------------------------------------------------


def save_meal(
    name: str,
    items: Any,
    default_for_slot: str | None = None,
    note: str | None = None,
    clear: list[str] | str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Store a standard meal: a name and a list of (ingredient, grams).

    Saving a name that already exists **replaces its items**, which is what
    "the usual breakfast, but with less cruesli now" means in practice.

    No macros are stored on the meal. They are computed from the ingredient
    rows on every read, so correcting a yoghurt's protein figure fixes the
    breakfast too — the opposite rule to a log entry, and for the opposite
    reason: a meal is a recipe, and a log entry is a measurement.

    There is no separate `update_meal`; this is the meal-mutation path, so
    `clear=["note"]` / `clear=["default_for_slot"]` is how an existing meal's
    own optional fields return to unknown — the same erase-is-a-verb rule
    every other update tool in this server follows. Applies when saving over
    an existing meal; a brand new one already has nothing to clear.
    """
    title = _text(name)
    if not title:
        raise NutritionError("a meal needs a name")
    key = fold(title)
    if not key:
        raise NutritionError(f"{title!r} folds to an empty name — give it letters or digits")
    slot = _one_of(default_for_slot, SLOTS, "default_for_slot")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, (list, tuple)) or not items:
        raise NutritionError("a meal needs at least one ingredient")

    stamp = now_utc()
    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        rows = _load_ingredients(conn, athlete_id)
        prepared: list[tuple[int, float]] = []
        for item in items:
            if not isinstance(item, dict):
                raise NutritionError(f"expected an object per item, got {type(item).__name__}")
            reference = item.get("ingredient")
            if reference is None:
                reference = item.get("ingredient_id")
            if reference is None:
                reference = item.get("name")
            ingredient = resolve_ingredient(rows, reference)
            prepared.append((ingredient["id"], _quantity(item, ingredient)))

        existing = conn.execute(
            "SELECT * FROM meals WHERE athlete_id = ? AND name_key = ?", (athlete_id, key)
        ).fetchone()
        if existing is None:
            # A brand-new meal has nothing to clear, but a mistyped field name in
            # `clear` is a caller trying to erase something and must not be told
            # it worked just because the meal happened not to exist yet — the
            # same rule the existing-meal branch enforces below. Validate before
            # the INSERT so a bad name leaves nothing behind.
            _clear_fields({}, clear, CLEARABLE_MEAL_FIELDS)
            cursor = conn.execute(
                "INSERT INTO meals (athlete_id, name, name_key, default_for_slot, note, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (athlete_id, title, key, slot, _text(note), stamp, stamp),
            )
            meal_id = cursor.lastrowid
            replaced = False
            cleared: list[str] = []
        else:
            meal_id = existing["id"]
            replaced = True
            updates = {"name": title, "updated_at": stamp}
            if slot is not None:
                updates["default_for_slot"] = slot
            if _text(note) is not None:
                updates["note"] = _text(note)
            cleared = _clear_fields(updates, clear, CLEARABLE_MEAL_FIELDS)
            assignments = ", ".join(f"{column} = ?" for column in updates)
            conn.execute(
                f"UPDATE meals SET {assignments} WHERE id = ?", (*updates.values(), meal_id)
            )
            conn.execute("DELETE FROM meal_items WHERE meal_id = ?", (meal_id,))

        for position, (ingredient_id, grams) in enumerate(prepared):
            conn.execute(
                "INSERT INTO meal_items (meal_id, position, ingredient_id, grams) "
                "VALUES (?, ?, ?, ?)",
                (meal_id, position, ingredient_id, grams),
            )
        stored = _meal_out(conn, athlete_id, meal_id)

    result: dict[str, Any] = {"stored": stored, "replaced_items": replaced}
    if cleared:
        result["cleared_fields"] = cleared
    return result


def delete_meal(meal: Any, athlete_id: int = DEFAULT_ATHLETE_ID) -> dict:
    """Remove a standard meal. Always allowed — a meal is a recipe, not a measurement.

    Unlike `delete_ingredient`, nothing here is load-bearing for history: every
    `food_log` row logged from this meal already froze its own macros and its
    `meal_name` at log time (see `_entry_row`), so they read exactly as they
    did before the meal existed. Only `meal_id` moves, to NULL
    (`ON DELETE SET NULL`); `meal_items` for this meal go with it
    (`ON DELETE CASCADE`).
    """
    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        row = _resolve_meal(conn, athlete_id, meal)
        logged = conn.execute(
            "SELECT COUNT(*) AS n FROM food_log WHERE meal_id = ?", (row["id"],)
        ).fetchone()["n"]
        conn.execute("DELETE FROM meals WHERE id = ?", (row["id"],))

    result: dict[str, Any] = {"deleted": {"id": row["id"], "name": row["name"]}}
    if logged:
        result["history_note"] = (
            f"{_plural(logged, 'existing log entry')} logged from "
            f"{row['name']!r} keep the macros and the meal name they were logged with; only "
            f"the link back to this recipe is gone."
        )
    return result


def _meal_out(conn: sqlite3.Connection, athlete_id: int, meal_id: int) -> dict:
    """One meal with its items and their macros, computed from current rows."""
    meal = _dict(conn.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone())
    if meal is None:
        raise NutritionError(f"no meal with id {meal_id}")
    rows = {row["id"]: row for row in _load_ingredients(conn, athlete_id)}
    items: list[dict] = []
    for item in conn.execute(
        "SELECT * FROM meal_items WHERE meal_id = ? ORDER BY position", (meal_id,)
    ):
        ingredient = rows.get(item["ingredient_id"])
        if ingredient is None:
            # The FK makes this unreachable through this server's own tools; a
            # hand-edited database is the case it stops from crashing a read.
            items.append({"ingredient_id": item["ingredient_id"], "missing": True})
            continue
        items.append(
            {
                "ingredient_id": ingredient["id"],
                "name": ingredient["name"],
                **macros_for(ingredient, item["grams"]),
            }
        )
    return {
        "id": meal["id"],
        "name": meal["name"],
        "default_for_slot": meal["default_for_slot"],
        "note": meal["note"],
        "items": items,
        "totals": _totals(items),
    }


def list_meals(athlete_id: int = DEFAULT_ATHLETE_ID) -> dict:
    """Every standard meal, with macros computed from the ingredients as they stand now."""
    with open_db() as conn:
        ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM meals WHERE athlete_id = ? ORDER BY name", (athlete_id,)
            )
        ]
        meals = [_meal_out(conn, athlete_id, meal_id) for meal_id in ids]
    return {
        "meals": meals,
        "count": len(meals),
        "note": (
            "Macros are computed from the current ingredient rows, so they follow any "
            "correction made since the meal was saved."
        ),
    }


def _resolve_meal(conn: sqlite3.Connection, athlete_id: int, reference: Any) -> dict:
    rows = [
        _dict(row) or {}
        for row in conn.execute("SELECT * FROM meals WHERE athlete_id = ?", (athlete_id,))
    ]
    if isinstance(reference, bool):
        # bool is an int subclass, so `isinstance(reference, int)` below would
        # otherwise accept `meal=True` as `meal=1` — whichever meal happens to
        # have id 1, expanded and logged, not a refusal. See
        # `resolve_ingredient`, which already guards this the same way.
        raise NutritionError(f"{reference!r} is not a meal reference")
    if isinstance(reference, int) or (isinstance(reference, str) and reference.strip().isdigit()):
        wanted = int(reference)
        for row in rows:
            if row["id"] == wanted:
                return row
        raise NutritionError(f"no meal with id {wanted}")
    text = _text(reference)
    if not text:
        raise NutritionError("give a meal name or id")
    key = fold(text)
    matches = [row for row in rows if row["name_key"] == key]
    if len(matches) == 1:
        return matches[0]
    names = [row["name"] for row in rows]
    raise NutritionError(f"no meal named {text!r}. Stored meals: {names or 'none yet'}.")


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------


def log_food(
    entries: Any,
    log_date: str | None = None,
    slot: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Log what was eaten: one row per ingredient, macros frozen at log time.

    Each entry is either an ingredient reference (`ingredient` as a name or an
    id) plus a quantity (`grams`, or `portions` when the ingredient carries a
    default portion), or a free-form estimate — `label` plus `kcal` and
    whatever else is known — for canteen or restaurant food not worth a row in
    the food base.

    **A name resolves exactly or not at all.** An unresolvable or ambiguous
    name is rejected with near-matches attached and nothing is written for it;
    the other entries in the same call still go in. A fuzzy match taken as
    exact would log the wrong food and read as a perfectly ordinary day.

    `log_date` defaults to the server's today, which is not reliably the
    athlete's — pass theirs when logging near midnight or from another
    timezone, or a late dinner lands on tomorrow and both days are wrong.

    Per-entry `slot` overrides the call's; without either, entries land in
    `snack`.
    """
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, (list, tuple)) or not entries:
        raise NutritionError("pass at least one entry to log")

    when = _today(log_date).isoformat()
    call_slot = _one_of(slot, SLOTS, "slot")

    accepted: list[dict] = []
    rejected: list[dict] = []
    warnings: list[str] = []

    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        rows = _load_ingredients(conn, athlete_id)
        prepared: list[dict] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                rejected.append(
                    {"index": index, "reason": f"expected an object, got {type(entry).__name__}"}
                )
                continue
            try:
                entry_slot = _one_of(entry.get("slot"), SLOTS, "slot") or call_slot or "snack"
                row, warning = _entry_row(entry, rows, when, entry_slot)
            except NutritionError as exc:
                rejected.append(
                    {
                        "index": index,
                        "entry": entry.get("ingredient") or entry.get("name") or entry.get("label"),
                        "reason": str(exc),
                    }
                )
                continue
            prepared.append(row)
            if warning and warning not in warnings:
                warnings.append(warning)
        accepted = _insert_entries(conn, athlete_id, prepared)
        summary = _day_summary(conn, athlete_id, when)

    result: dict[str, Any] = {
        "date": when,
        "logged": len(accepted),
        "rejected": len(rejected),
        "entries": accepted,
        "rejections": rejected,
        "day": summary,
    }
    if warnings:
        result["warnings"] = warnings
    if rejected:
        result["rejection_note"] = (
            "Nothing was written for the rejected entries. Resolve the name against "
            "search_ingredients, add the food with add_ingredients, or log it as an estimate "
            "with a label and a kcal figure."
        )
    return result


def log_meal(
    meal: Any,
    log_date: str | None = None,
    slot: str | None = None,
    overrides: Any = None,
    extras: Any = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Log a standard meal, expanded into one row per ingredient.

    The expansion is the point. "The usual breakfast" stored as a single 620
    kcal row cannot answer "how much protein came from the skyr" and cannot be
    corrected a gram at a time — so the meal becomes its ingredients, each
    tagged with the meal's name, and `day_summary` groups them back together.

    `overrides` adjusts one ingredient's quantity for this logging only —
    `[{"ingredient": "cruesli", "grams": 30}]` — and leaves the stored meal
    untouched. Set an override to `grams: 0` to leave an ingredient out today.
    `extras` adds entries that are not part of the meal, in the same shape
    `log_food` takes.

    The slot comes from the meal's `default_for_slot` unless one is given.
    """
    when = _today(log_date).isoformat()
    given_slot = _one_of(slot, SLOTS, "slot")

    if overrides is None:
        overrides = []
    if isinstance(overrides, dict):
        overrides = [overrides]
    if extras is None:
        extras = []
    if isinstance(extras, dict):
        extras = [extras]

    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        stored_meal = _resolve_meal(conn, athlete_id, meal)
        entry_slot = given_slot or stored_meal["default_for_slot"] or "snack"
        rows = _load_ingredients(conn, athlete_id)
        by_id = {row["id"]: row for row in rows}

        overridden: dict[int, float] = {}
        for item in overrides:
            if not isinstance(item, dict):
                raise NutritionError(f"expected an object per override, got {type(item).__name__}")
            reference = item.get("ingredient")
            if reference is None:
                reference = item.get("ingredient_id")
            if reference is None:
                reference = item.get("name")
            ingredient = resolve_ingredient(rows, reference)
            # An explicit zero is "leave it out today" — the one meaning a
            # zero quantity has anywhere in this module — and has to be
            # recognised *before* `_quantity`, which refuses zero as a
            # placeholder. The comparison is on the coerced figure, not the
            # raw value: every other quantity here reads the numeric strings
            # a pasted payload carries, so "0" must mean what 0 means. Bools
            # are refused first — float(False) is 0.0, the same trap
            # resolve_ingredient and _resolve_meal guard. Caught on either
            # field so `portions: 0` means the same thing `grams: 0` does.
            omitted_today = False
            for field, limits in (("grams", GRAMS_LIMITS), ("portions", (0.0, 100.0))):
                raw = item.get(field)
                if isinstance(raw, bool):
                    raise NutritionError(
                        f"{ingredient['name']}: {field} must be a number, got {raw!r}"
                    )
                if raw is not None and _number(raw, field, limits) == 0:
                    omitted_today = True
            if omitted_today:
                overridden[ingredient["id"]] = 0.0
            else:
                overridden[ingredient["id"]] = _quantity(item, ingredient)

        items = [
            _dict(row) or {}
            for row in conn.execute(
                "SELECT * FROM meal_items WHERE meal_id = ? ORDER BY position",
                (stored_meal["id"],),
            )
        ]
        known = {item["ingredient_id"] for item in items}
        unknown = sorted(set(overridden) - known)
        if unknown:
            names = ", ".join(by_id[i]["name"] for i in unknown if i in by_id)
            raise NutritionError(
                f"{names} is not part of {stored_meal['name']!r}, so there is nothing to "
                f"override. Pass it in `extras` to add it to this meal today."
            )

        prepared: list[dict] = []
        warnings: list[str] = []
        applied: list[dict] = []
        skipped: list[str] = []
        for item in items:
            ingredient = by_id[item["ingredient_id"]]
            grams = overridden.get(item["ingredient_id"], item["grams"])
            if item["ingredient_id"] in overridden:
                applied.append({"name": ingredient["name"], "from_g": item["grams"], "to_g": grams})
            if grams <= 0:
                # An override of zero is "not today", which is a real thing to
                # say about a standard meal. Writing a zero-gram row instead
                # would put an ingredient nobody ate into the day's grouping.
                skipped.append(ingredient["name"])
                continue
            row, warning = _entry_row(
                {"ingredient_id": ingredient["id"], "grams": grams},
                rows,
                when,
                entry_slot,
                meal=stored_meal,
            )
            prepared.append(row)
            if warning and warning not in warnings:
                warnings.append(warning)

        for extra in extras:
            if not isinstance(extra, dict):
                raise NutritionError(f"expected an object per extra, got {type(extra).__name__}")
            row, warning = _entry_row(
                extra, rows, when, _one_of(extra.get("slot"), SLOTS, "slot") or entry_slot
            )
            prepared.append(row)
            if warning and warning not in warnings:
                warnings.append(warning)

        accepted = _insert_entries(conn, athlete_id, prepared)
        summary = _day_summary(conn, athlete_id, when)

    result: dict[str, Any] = {
        "date": when,
        "meal": stored_meal["name"],
        "slot": entry_slot,
        "logged": len(accepted),
        "entries": accepted,
        "totals": _totals(accepted),
        "day": summary,
    }
    if applied:
        result["overrides_applied"] = applied
    if skipped:
        result["omitted"] = skipped
        result["omitted_note"] = (
            f"{', '.join(skipped)} had an override of 0 g, so nothing was logged for "
            f"{'it' if len(skipped) == 1 else 'them'} today. The stored meal is unchanged."
        )
    if warnings:
        result["warnings"] = warnings
    return result


def edit_log_entry(
    entry_id: int,
    grams: float | None = None,
    portions: float | None = None,
    slot: str | None = None,
    log_date: str | None = None,
    note: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Correct one logged entry — usually a weight that was guessed then measured.

    A new quantity **recomputes the macros from the ingredient as it stands
    now**, which is the one case where a stored entry is allowed to move: the
    athlete is restating what they ate, not re-reading old data. Everything
    else about the freeze holds — nothing here touches any other row.

    An estimate has no ingredient behind it, so its quantity cannot be
    recomputed; change its numbers by deleting it and logging it again.
    """
    with open_db() as conn:
        row = _dict(
            conn.execute(
                "SELECT * FROM food_log WHERE id = ? AND athlete_id = ?", (entry_id, athlete_id)
            ).fetchone()
        )
        if row is None:
            raise NutritionError(f"no log entry with id {entry_id}")

        updates: dict[str, Any] = {}
        if slot is not None:
            updates["slot"] = _one_of(slot, SLOTS, "slot")
        if log_date is not None:
            updates["log_date"] = parse_date(log_date, "log_date").isoformat()
        if note is not None:
            updates["note"] = _text(note)

        if grams is not None or portions is not None:
            if row["ingredient_id"] is None:
                raise NutritionError(
                    f"entry {entry_id} is a free-form estimate with no ingredient behind it, so "
                    f"a new weight cannot be recomputed. Delete it and log it again with the "
                    f"corrected figures."
                )
            ingredient = _dict(
                conn.execute(
                    "SELECT * FROM ingredients WHERE id = ?", (row["ingredient_id"],)
                ).fetchone()
            )
            if ingredient is None:
                raise NutritionError(
                    f"entry {entry_id} points at an ingredient that is no longer stored; its "
                    f"macros stand as logged."
                )
            quantity = _quantity({"grams": grams, "portions": portions}, ingredient)
            macros = macros_for(ingredient, quantity)
            updates.update(
                {
                    "grams": macros["grams"],
                    "kcal": macros["kcal"],
                    "protein_g": macros["protein_g"],
                    "fiber_g": macros["fiber_g"],
                    "carbs_g": macros["carbs_g"],
                    "fat_g": macros["fat_g"],
                    "cost": macros["cost"],
                }
            )

        if not updates:
            raise NutritionError("nothing to change — pass grams, portions, slot, date or note")

        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE food_log SET {assignments}, updated_at = ? WHERE id = ?",
            (*updates.values(), now_utc(), entry_id),
        )
        stored = _dict(conn.execute("SELECT * FROM food_log WHERE id = ?", (entry_id,)).fetchone())
        assert stored is not None
        days = sorted({row["log_date"], stored["log_date"]})
        summaries = {day: _day_summary(conn, athlete_id, day) for day in days}

    return {
        "updated_fields": sorted(updates),
        "entry": _log_out(stored),
        "days": summaries,
    }


def delete_log_entry(entry_id: int, athlete_id: int = DEFAULT_ATHLETE_ID) -> dict:
    """Remove one logged entry. Returns what was removed and the day it left behind."""
    with open_db() as conn:
        row = _dict(
            conn.execute(
                "SELECT * FROM food_log WHERE id = ? AND athlete_id = ?", (entry_id, athlete_id)
            ).fetchone()
        )
        if row is None:
            raise NutritionError(f"no log entry with id {entry_id}")
        conn.execute("DELETE FROM food_log WHERE id = ?", (entry_id,))
        summary = _day_summary(conn, athlete_id, row["log_date"])
    return {"deleted": _log_out(row), "day": summary}


# --------------------------------------------------------------------------
# goals
# --------------------------------------------------------------------------


def set_goal(
    goal_type: str,
    target_weight_kg: float | None = None,
    milestone_weight_kg: float | None = None,
    rate_kg_per_week: float | None = None,
    effective_date: str | None = None,
    note: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Set the active nutrition goal. Any previous active goal is closed, not erased.

    Append-only like the training histories, and for the same reason: a deficit
    run in March is only readable against the goal that was active in March.
    Closing rather than deleting keeps that.

    `rate_kg_per_week` is signed, and a `lose` goal takes a negative rate — the
    sign is checked against the goal type rather than inferred, because a
    rate whose sign was guessed sets the deficit the wrong way round.

    One active goal at a time. Retire one with `close_goal`.
    """
    kind = _one_of(goal_type, GOAL_TYPES, "goal_type")
    target = _optional_number(target_weight_kg, "target_weight_kg", (25.0, 300.0))
    milestone = _optional_number(milestone_weight_kg, "milestone_weight_kg", (25.0, 300.0))
    rate = _optional_number(rate_kg_per_week, "rate_kg_per_week", RATE_KG_PER_WEEK_LIMITS)
    when = _today(effective_date).isoformat()

    warnings: list[str] = []
    if kind == "maintain":
        if rate:
            raise NutritionError(
                "a maintain goal has no rate — pass rate_kg_per_week=0 or leave it out"
            )
        rate = 0.0
    elif rate is not None:
        expected = -1 if kind == "lose" else 1
        if rate and (rate > 0) != (expected > 0):
            raise NutritionError(
                f"a {kind} goal takes a {'negative' if kind == 'lose' else 'positive'} "
                f"rate_kg_per_week; {rate:+g} would set the deficit the wrong way round."
            )
        if abs(rate) > BRISK_RATE_KG_PER_WEEK:
            warnings.append(
                f"{abs(rate):g} kg/week is fast. Past about {BRISK_RATE_KG_PER_WEEK:g} kg/week "
                f"most of what moves is water and muscle rather than fat, and training quality "
                f"goes with it. Stored as asked."
            )

    stamp = now_utc()
    with open_db() as conn:
        _ensure_athlete(conn, athlete_id)
        previous = _dict(
            conn.execute(
                "SELECT * FROM nutrition_goals WHERE athlete_id = ? AND status = 'active' "
                "ORDER BY effective_date DESC, id DESC LIMIT 1",
                (athlete_id,),
            ).fetchone()
        )
        if previous is not None:
            conn.execute(
                "UPDATE nutrition_goals SET status = 'abandoned', closed_date = ?, "
                "updated_at = ? WHERE athlete_id = ? AND status = 'active'",
                (when, stamp, athlete_id),
            )
        cursor = conn.execute(
            "INSERT INTO nutrition_goals (athlete_id, goal_type, target_weight_kg, "
            "milestone_weight_kg, rate_kg_per_week, status, note, effective_date, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
            (athlete_id, kind, target, milestone, rate, _text(note), when, stamp, stamp),
        )
        stored = _dict(
            conn.execute(
                "SELECT * FROM nutrition_goals WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        )
        weight = _latest_weight(conn, athlete_id, when)

    result: dict[str, Any] = {"stored": stored, "closed_previous": previous}
    if weight and target:
        gap = round(weight["value_kg"] - target, 1)
        result["to_go_kg"] = gap
        if rate:
            result["weeks_at_rate"] = round(abs(gap / rate), 1)
    if weight is None:
        result["missing"] = ["weight"]
        result["missing_note"] = (
            "No weigh-in on file, so this goal has no starting point and no calorie target "
            "can be computed. Log one with log_weight."
        )
    if warnings:
        result["warnings"] = warnings
    return result


def close_goal(
    status: str = "reached",
    closed_date: str | None = None,
    note: str | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Retire the active goal as `reached` or `abandoned`. The record stays."""
    state = _one_of(status, ("reached", "abandoned"), "status")
    when = _today(closed_date).isoformat()
    with open_db() as conn:
        row = _dict(
            conn.execute(
                "SELECT * FROM nutrition_goals WHERE athlete_id = ? AND status = 'active' "
                "ORDER BY effective_date DESC, id DESC LIMIT 1",
                (athlete_id,),
            ).fetchone()
        )
        if row is None:
            raise NutritionError("no active goal to close")
        updates = ["status = ?", "closed_date = ?", "updated_at = ?"]
        values: list[Any] = [state, when, now_utc()]
        if _text(note):
            updates.append("note = ?")
            values.append(_text(note))
        conn.execute(
            f"UPDATE nutrition_goals SET {', '.join(updates)} WHERE id = ?", (*values, row["id"])
        )
        stored = _dict(
            conn.execute("SELECT * FROM nutrition_goals WHERE id = ?", (row["id"],)).fetchone()
        )
    return {
        "closed": stored,
        "note": (
            "With no active goal, suggest_targets computes maintenance — BMR, daily activity "
            "and training, with no deficit or surplus applied."
        ),
    }


def _active_goal(conn: sqlite3.Connection, athlete_id: int, on_date: str) -> dict | None:
    """The goal in force on a date: effective at or before it, and not yet closed as of it.

    Not filtered by `status`. A goal closed in June was still the goal in
    force in April, and a deficit run back then is only readable against it —
    `get_goal` is the status-based read of "what is active right now"; this is
    the date-scoped one a target computation needs. `closed_date > on_date`,
    not `>=`, so the day a goal closes belongs to whatever replaced it — a
    replacement goal effective that same date wins the handover day. Only a
    goal with no replacement, closed and never re-set, reads as None on its
    own closing date.
    """
    return _dict(
        conn.execute(
            "SELECT * FROM nutrition_goals WHERE athlete_id = ? AND effective_date <= ? "
            "AND (closed_date IS NULL OR closed_date > ?) "
            "ORDER BY effective_date DESC, id DESC LIMIT 1",
            (athlete_id, on_date, on_date),
        ).fetchone()
    )


def get_goal(athlete_id: int = DEFAULT_ATHLETE_ID) -> dict:
    """The active goal, the history behind it, and where the athlete is against it."""
    today = date.today().isoformat()
    with open_db() as conn:
        # By status, not by date: a goal set to start next Monday is the active
        # one and belongs in this answer. `_active_goal` is date-scoped because
        # a *target* must be computed against the goal in force on its own day;
        # reading the file is a different question.
        active = _dict(
            conn.execute(
                "SELECT * FROM nutrition_goals WHERE athlete_id = ? AND status = 'active' "
                "ORDER BY effective_date DESC, id DESC LIMIT 1",
                (athlete_id,),
            ).fetchone()
        )
        history = [
            _dict(row) or {}
            for row in conn.execute(
                "SELECT * FROM nutrition_goals WHERE athlete_id = ? "
                "ORDER BY effective_date DESC, id DESC",
                (athlete_id,),
            )
        ]
        weight = _latest_weight(conn, athlete_id, today)

    result: dict[str, Any] = {"active": active, "history": history, "current_weight": weight}
    if active and active["effective_date"] > today:
        result["not_yet_in_force"] = (
            f"This goal starts on {active['effective_date']}. Targets suggested before then "
            f"use whatever was in force on their own date."
        )
    if active and weight and active.get("target_weight_kg"):
        result["to_go_kg"] = round(weight["value_kg"] - active["target_weight_kg"], 1)
        if active.get("milestone_weight_kg"):
            result["to_milestone_kg"] = round(weight["value_kg"] - active["milestone_weight_kg"], 1)
    return result


# --------------------------------------------------------------------------
# totals and the day
# --------------------------------------------------------------------------


def _totals(entries: list[dict]) -> dict:
    """Sum a list of macro-bearing rows. The one place these numbers are added.

    Protein is summed separately from everything else, because it is the only
    total with an exclusion: an ingredient marked `counts_toward_protein=false`
    — collagen, and the rest of the incomplete proteins — contributes its
    calories in full and none of its protein. Counting it would let the athlete
    hit a protein target with a powder that does not do the job protein is in
    the target for. A free-form estimate may not have stated a protein or a
    fibre figure — `protein_g_missing_entries` / `fiber_g_missing_entries`
    report how many did not, and `day_summary` turns a nonzero one into a note
    that the remainder above overstates by that much.

    Optional macros sum over the rows that have them, and report how many did
    not: a carbohydrate total that silently treats "unknown" as zero is a
    number that looks complete and is not.
    """
    totals: dict[str, Any] = {}
    for key in _SUMMED_MACROS:
        values = [entry[key] for entry in entries if entry.get(key) is not None]
        missing = sum(1 for entry in entries if entry.get(key) is None)
        totals[key] = round(sum(values), 1) if values or key not in _OPTIONAL_MACROS else None
        if missing:
            totals[f"{key}_missing_entries"] = missing

    counted = [entry for entry in entries if entry.get("counts_toward_protein", True)]
    excluded = [entry for entry in entries if not entry.get("counts_toward_protein", True)]
    # `entry.get("protein_g", 0.0)` looks like it folds a missing figure to
    # zero, but every entry carries the key (see `_LOG_OUT_FIELDS`) — a `.get`
    # default only fires for an *absent* key, so a `None` from a free-form
    # estimate that never stated protein passed straight through and crashed
    # `sum()` on the first mixed-type addition. Sum only the known ones, and
    # count what was skipped rather than silently treating "unknown" as zero.
    protein_missing = sum(1 for entry in entries if entry.get("protein_g") is None)
    totals["protein_g"] = round(
        sum(entry["protein_g"] for entry in counted if entry.get("protein_g") is not None), 1
    )
    if protein_missing:
        totals["protein_g_missing_entries"] = protein_missing
    if excluded:
        totals["protein_g_excluded"] = round(
            sum(entry["protein_g"] for entry in excluded if entry.get("protein_g") is not None),
            1,
        )
        totals["protein_excluded_from"] = sorted({entry.get("label", "?") for entry in excluded})

    priced = [entry for entry in entries if entry.get("cost") is not None]
    totals["cost"] = round(sum(float(entry["cost"]) for entry in priced), 2)
    unpriced = len(entries) - len(priced)
    if unpriced:
        totals["cost_unpriced_entries"] = unpriced
    return totals


def _event_on(conn: sqlite3.Connection, athlete_id: int, day: str) -> dict | None:
    """The event on one date that is still going to happen, or did.

    Excludes `NON_STARTING_EVENT_STATUSES` (`abandoned`, `dns`) — a race
    recorded as either is not one this day's fuelling should be built around.
    The import is deferred: `coach.py` imports `GENDERS` from this module at
    load time, so a module-level `from .coach import ...` here would be a real
    circular import, not just an ordering nuisance.
    """
    from .coach import NON_STARTING_EVENT_STATUSES

    placeholders = ", ".join("?" for _ in NON_STARTING_EVENT_STATUSES)
    return _dict(
        conn.execute(
            f"SELECT * FROM events WHERE athlete_id = ? AND event_date = ? "
            f"AND status NOT IN ({placeholders}) ORDER BY id LIMIT 1",
            (athlete_id, day, *NON_STARTING_EVENT_STATUSES),
        ).fetchone()
    )


def _day_type_and_training_from_rows(
    day: str,
    activities: list[dict],
    planned: list[dict],
    on_day: dict | None,
    next_day: dict | None,
) -> dict:
    """The pure half of `_day_type_and_training`: the same rules, rows in hand.

    Shared by the single-date path (`_day_type_and_training`, one query set
    per call) and `_RangeHistory` (every table for a range read once) — the
    two must never compute a day type differently, so there is exactly one
    implementation of the rules to keep in sync.

    * an event on the date makes it `race`, and the date before it `race_eve`;
    * a long or hard ride — imported or planned — makes it `big_session`;
    * any other ride makes it `training`;
    * nothing at all makes it `rest`.

    Exercise calories come from the imported activity's Garmin figure where
    there is one, because a measured number beats a modelled one even when the
    model is good. For a date still in the future there is no measurement, so
    the planned session's own mechanical work is converted at cycling's gross
    efficiency — the reason a ride's kJ and its kcal come out near enough
    equal. Where a date has both, the import wins and any planned session not
    yet accounted for by an import — still `planned`/`pushed`, and not linked
    to one of today's activities — adds its own estimate rather than
    vanishing: a second session that has not happened yet is not the same
    session as the one that has.
    """
    activity_ids = {row["id"] for row in activities}

    measured = [row for row in activities if _positive(row.get("calories")) is not None]
    measured_kcal = (
        round(sum(_positive(row["calories"]) for row in measured), 0) if measured else None
    )
    measured_ids = {row["id"] for row in measured}
    unmeasured = [row for row in activities if row["id"] not in measured_ids]
    activity_seconds = sum(_positive(row.get("duration_s")) or 0 for row in activities)

    planned_kcal = 0.0
    planned_seconds = 0.0
    planned_detail: list[dict] = []
    candidates: list[dict] = []  # still planned/pushed: might be an extra session, or the ride
    for row in planned:
        if row.get("status") in ("missed", "skipped"):
            continue
        try:
            workout = load_spec(json.loads(row["spec_json"]))
            metrics = compute_metrics(workout)
        except (SpecError, ValueError, json.JSONDecodeError):
            # A stored spec that no longer parses is a problem for the coach
            # tools to report; here it simply contributes no estimate rather
            # than taking the whole target computation down.
            continue
        kcal = metrics.work_kj / (GROSS_EFFICIENCY * KJ_PER_KCAL)
        planned_kcal += kcal
        planned_seconds += metrics.total_seconds
        planned_detail.append(
            {
                "planned_workout_id": row["id"],
                "name": workout.name,
                "duration_s": metrics.total_seconds,
                "work_kj": round(metrics.work_kj, 1),
                "estimated_kcal": round(kcal),
            }
        )
        if row.get("status") in ("planned", "pushed"):
            candidates.append({"row": row, "kcal": kcal, "name": workout.name})

    # A session `completed`, or explicitly linked to one of today's
    # activities, is that import wearing a different id — never added again.
    # Every other still-planned/pushed session is additional exercise: a
    # session that has not been marked as the ride behind an import is not
    # assumed to be one just because an import happens to exist on the same
    # day, however many activities were imported. See
    # `test_an_unlinked_plan_next_to_a_measured_ride_sums_rather_than_vanishes`.
    outstanding = [c for c in candidates if c["row"].get("linked_activity_id") not in activity_ids]
    outstanding_kcal = sum(c["kcal"] for c in outstanding)
    outstanding_names = [c["name"] for c in outstanding]

    if on_day is not None:
        day_type = "race"
    elif next_day is not None:
        day_type = "race_eve"
    elif (
        activity_seconds >= BIG_SESSION_DURATION_S
        or (measured_kcal or 0) >= BIG_SESSION_KCAL
        or planned_seconds >= BIG_SESSION_DURATION_S
        or planned_kcal >= BIG_SESSION_KCAL
    ):
        day_type = "big_session"
    elif activities or planned_detail:
        day_type = "training"
    else:
        day_type = "rest"

    return {
        "day_type": day_type,
        "event": {"name": on_day["name"], "date": on_day["event_date"]} if on_day else None,
        "race_tomorrow": (
            {"name": next_day["name"], "date": next_day["event_date"]} if next_day else None
        ),
        "activities": [
            {
                "id": row["id"],
                "name": row.get("name"),
                "sport": row.get("sport"),
                "duration_s": row.get("duration_s"),
                "calories": row.get("calories"),
            }
            for row in activities
        ],
        "planned": planned_detail,
        "measured_exercise_kcal": measured_kcal,
        "planned_exercise_kcal": round(planned_kcal) if planned_detail else None,
        "unmeasured_activities": len(unmeasured),
        "activity_seconds": activity_seconds,
        "outstanding_planned_kcal": round(outstanding_kcal) if outstanding_names else 0,
        "outstanding_planned_names": outstanding_names,
    }


def _day_type_and_training(conn: sqlite3.Connection, athlete_id: int, day: str) -> dict:
    """What the training tables say about one date: its day type and its cost.

    This is the coupling that justifies one database. See
    `_day_type_and_training_from_rows` for the rules — this is the single-date
    path that reads the rows for `day` and hands them to it; `_RangeHistory`
    reads a whole range at once and calls the same pure function per day.
    """
    activities = [
        _dict(row) or {}
        for row in conn.execute(
            "SELECT id, name, sport, duration_s, calories, avg_power, normalized_power "
            "FROM activities WHERE athlete_id = ? AND local_date = ? ORDER BY id",
            (athlete_id, day),
        )
    ]
    planned = [
        _dict(row) or {}
        for row in conn.execute(
            "SELECT id, spec_json, status, linked_activity_id FROM planned_workouts "
            "WHERE athlete_id = ? AND scheduled_date = ? ORDER BY id",
            (athlete_id, day),
        )
    ]
    on_day = _event_on(conn, athlete_id, day)
    tomorrow = (parse_date(day, "date") + timedelta(days=1)).isoformat()
    next_day = _event_on(conn, athlete_id, tomorrow)
    return _day_type_and_training_from_rows(day, activities, planned, on_day, next_day)


def _day_entries(conn: sqlite3.Connection, athlete_id: int, day: str) -> list[dict]:
    return [
        _log_out(_dict(row) or {})
        for row in conn.execute(
            "SELECT * FROM food_log WHERE athlete_id = ? AND log_date = ? ORDER BY id",
            (athlete_id, day),
        )
    ]


def _stored_targets(conn: sqlite3.Connection, athlete_id: int, day: str) -> dict | None:
    row = _dict(
        conn.execute(
            "SELECT * FROM daily_targets WHERE athlete_id = ? AND target_date = ?",
            (athlete_id, day),
        ).fetchone()
    )
    if row is None:
        return None
    row.pop("rationale_json", None)
    return row


def _day_summary(conn: sqlite3.Connection, athlete_id: int, day: str) -> dict:
    """The day's bilan, computed. Shared by every tool that ends with one.

    Per-slot entries with their macros, grouped by meal where they came from
    one, the running totals, the day's targets, and what is left. This is what
    answers "where am I today?" and, through the remainder, "can I eat X?" —
    which is a subtraction, never a yes or a no.
    """
    entries = _day_entries(conn, athlete_id, day)
    targets = _stored_targets(conn, athlete_id, day)

    slots: list[dict] = []
    for slot in SLOTS:
        in_slot = [entry for entry in entries if entry["slot"] == slot]
        if not in_slot:
            continue
        groups: list[dict] = []
        loose: list[dict] = []
        for entry in in_slot:
            if entry["meal_name"]:
                existing = next((g for g in groups if g["meal_name"] == entry["meal_name"]), None)
                if existing is None:
                    existing = {"meal_name": entry["meal_name"], "entries": []}
                    groups.append(existing)
                existing["entries"].append(entry)
            else:
                loose.append(entry)
        for group in groups:
            group["totals"] = _totals(group["entries"])
        slots.append(
            {
                "slot": slot,
                "meals": groups,
                "entries": loose,
                "totals": _totals(in_slot),
            }
        )

    totals = _totals(entries)
    estimates = [entry for entry in entries if entry["is_estimate"]]

    summary: dict[str, Any] = {
        "date": day,
        "slots": slots,
        "entry_count": len(entries),
        "totals": totals,
        "targets": targets,
    }

    if targets:
        summary["remaining"] = {
            "kcal": round(targets["kcal"] - totals["kcal"], 1),
            "protein_g": round(targets["protein_g"] - totals["protein_g"], 1),
            "fiber_g": round(targets["fiber_g"] - totals["fiber_g"], 1),
        }
        summary["day_type"] = targets["day_type"]
        summary["target_source"] = targets["source"]
    else:
        summary["remaining"] = None
        summary["targets_note"] = (
            f"No targets stored for {day}. Call suggest_targets(date='{day}') and confirm "
            f"them; until then there is no remainder to answer 'can I eat X' with."
        )

    if estimates:
        summary["estimates"] = [
            {"id": entry["id"], "label": entry["label"], "kcal": entry["kcal"]}
            for entry in estimates
        ]
        summary["estimates_note"] = (
            f"{_plural(len(estimates), 'entry')} {_agree(len(estimates), 'is')} a free-form "
            f"estimate: the macros were stated, not computed from a weighed ingredient, and "
            f"no cost is counted for them. Treat the totals as approximate by that much."
        )
    if totals.get("protein_g_excluded"):
        summary["protein_note"] = (
            f"{totals['protein_g_excluded']:g} g of protein from "
            f"{', '.join(totals['protein_excluded_from'])} is excluded from the protein total — "
            f"it is an incomplete protein. Its calories are counted in full."
        )
    protein_missing = totals.get("protein_g_missing_entries")
    fiber_missing = totals.get("fiber_g_missing_entries")
    if protein_missing or fiber_missing:
        parts = []
        if protein_missing:
            parts.append(f"protein from {_plural(protein_missing, 'entry')}")
        if fiber_missing:
            parts.append(f"fibre from {_plural(fiber_missing, 'entry')}")
        # The verb agrees with the number of conjoined subjects, not with the
        # entry counts: "protein from 2 entries IS unknown", but "protein ...
        # and fibre ... ARE unknown".
        summary["unknown_macros_note"] = (
            f"{' and '.join(parts)} {_agree(len(parts), 'is')} unknown, not zero — "
            f"an estimate that never stated it. "
            f"The remaining figure above is counted against known entries only, so it overstates "
            f"what is actually left by however much those entries turn out to hold."
        )
    return summary


def day_summary(date_str: str | None = None, athlete_id: int = DEFAULT_ATHLETE_ID) -> dict:
    """The day so far: what was eaten, what the targets are, what is left.

    Read this before answering anything about food. "Can I eat X?" is the
    remainder minus X, shown; it is not a yes or a no, and it is never a
    judgement.

    `date_str` defaults to the server's today. That is not reliably the
    athlete's today — pass theirs when it might differ, or this reports a day
    that has barely started as if it were nearly over.
    """
    day = _today(date_str).isoformat()
    with open_db() as conn:
        summary = _day_summary(conn, athlete_id, day)
        training = _day_type_and_training(conn, athlete_id, day)
    summary["training"] = training
    if summary.get("targets") is None:
        summary["inferred_day_type"] = training["day_type"]
    return summary


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------


def mifflin_st_jeor(weight_kg: float, height_cm: float, age_years: int, gender: str) -> float:
    """Resting metabolic rate, Mifflin-St Jeor. The floor everything is built on.

    10 x kg + 6.25 x cm - 5 x age, then +5 for men and -161 for women. The
    166 kcal gap between those two constants is why `gender` is asked for
    rather than assumed: guessing it moves the whole day's target by more than
    a meal.

    `other` and anything unrecognised take the midpoint (-78), which is a
    stated compromise and not a measurement — it is flagged wherever it is
    used.

    The equation is a population average. Individual resting rates scatter
    around it by roughly ±10%, so it is a starting point to be corrected
    against the weight trend over a few weeks, never a number to defend
    against the scale.
    """
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age_years
    if gender == "male":
        return base + 5.0
    if gender == "female":
        return base - 161.0
    return base - 78.0


def _suggest_one_for(
    conn: sqlite3.Connection,
    athlete_id: int,
    day: str,
    athlete: dict,
    protein_g_per_kg: float,
    baseline_factor: float,
    exercise_kcal_override: float | None,
) -> dict:
    """`_suggest_one`, resolving its three dated inputs with their own query set.

    The single-date path: one call from `suggest_targets` for a lone date, and
    every call from `confirm_targets`, which recomputes at most a handful of
    dates per call. `suggest_targets` over a `start`/`end` range and
    `week_summary` use `_RangeHistory` instead, so a season does not pay for
    this query set once per day for the same handful of rows.
    """
    weight = _latest_weight(conn, athlete_id, day)
    training = _day_type_and_training(conn, athlete_id, day)
    goal = _active_goal(conn, athlete_id, day)
    return _suggest_one(
        day,
        athlete,
        weight,
        training,
        goal,
        protein_g_per_kg,
        baseline_factor,
        exercise_kcal_override,
    )


def _suggest_one(
    day: str,
    athlete: dict,
    weight: dict | None,
    training: dict,
    goal: dict | None,
    protein_g_per_kg: float,
    baseline_factor: float,
    exercise_kcal_override: float | None,
) -> dict:
    """One date's suggested targets, with every input and intermediate shown.

    The output is the working, not the answer. A target the athlete cannot see
    the derivation of is one they can only accept or refuse — and the whole
    flow here is that the server proposes and the human confirms.

    Takes its three dated inputs already resolved, rather than a connection
    and a date, so a range of days can resolve them all from one batch of
    queries (`_RangeHistory`) instead of one query set per day — see
    `_suggest_one_for` for the single-date path that still queries directly.
    """
    missing: list[str] = []
    if weight is None:
        missing.append("weight")
    if not athlete.get("height_cm"):
        missing.append("height_cm")
    if not athlete.get("birth_year"):
        missing.append("birth_year")
    if not athlete.get("gender"):
        missing.append("gender")
    if missing:
        return {
            "date": day,
            "day_type": training["day_type"],
            "training": training,
            "missing": missing,
            "missing_note": (
                f"Cannot compute a target without {', '.join(missing)}: Mifflin-St Jeor needs "
                f"weight, height, age and gender, and a guessed one of those moves the answer "
                f"by more than a meal. Store them with log_weight and update_profile."
            ),
        }

    assert weight is not None
    # Age from the calendar year alone. The birthday within the year is not on
    # file, and the equation moves 5 kcal per year — smaller than the rounding
    # this target is quoted to.
    age = parse_date(day, "date").year - int(athlete["birth_year"])
    bmr = mifflin_st_jeor(weight["value_kg"], float(athlete["height_cm"]), age, athlete["gender"])
    baseline = bmr * baseline_factor

    outstanding_kcal = training.get("outstanding_planned_kcal") or 0
    if exercise_kcal_override is not None:
        exercise = float(exercise_kcal_override)
        exercise_source = "override"
    elif training["measured_exercise_kcal"] is not None:
        if outstanding_kcal:
            # A second session, still planned and not yet imported, is
            # additional exercise on top of what was measured — not the same
            # ride the import already counted. See
            # `_day_type_and_training_from_rows` for what "outstanding" means.
            exercise = float(training["measured_exercise_kcal"]) + float(outstanding_kcal)
            exercise_source = "imported_activity+planned_workout"
        else:
            exercise = float(training["measured_exercise_kcal"])
            exercise_source = "imported_activity"
    elif training["planned_exercise_kcal"] is not None:
        exercise = float(training["planned_exercise_kcal"])
        exercise_source = "planned_workout"
    else:
        exercise = 0.0
        exercise_source = "none"

    maintenance = baseline + exercise

    # The goal's rate spread evenly across the week, then withheld on the days
    # that cannot afford it. Under-fuelling a long ride costs the session and
    # the recovery from it, and the calories saved are the least useful in the
    # week — so a deficit is not applied on a big session, a race, or the day
    # before one. The weekly average deficit is smaller than the stated rate
    # by exactly that much, and the response says so.
    rate = float(goal["rate_kg_per_week"] or 0.0) if goal else 0.0
    full_adjustment = rate * KCAL_PER_KG / 7.0
    protected = training["day_type"] in ("big_session", "race", "race_eve")
    # Only a deficit is withheld. A surplus on a protected day is calories the
    # session needs, not calories saved — withholding it would make a gain
    # goal's target *smaller* on the day it most needs to be bigger.
    withhold = protected and full_adjustment < 0
    adjustment = 0.0 if withhold else full_adjustment

    target = maintenance + adjustment
    clamped = False
    if target < bmr:
        # Below resting metabolic rate is not a diet, it is a hole. Clamping
        # rather than refusing keeps the day usable, and saying so is what
        # makes the goal's rate the thing that gets revisited.
        target = bmr
        clamped = True

    protein = round(weight["value_kg"] * protein_g_per_kg / 5.0) * 5.0
    fiber = RACE_FIBER_G if training["day_type"] in ("race", "race_eve") else DEFAULT_FIBER_G

    steps = [
        f"BMR (Mifflin-St Jeor, {athlete['gender']}, {weight['value_kg']:g} kg, "
        f"{athlete['height_cm']:g} cm, age {age}) = {round(bmr)} kcal",
        f"x {baseline_factor:g} for non-training daily activity = {round(baseline)} kcal",
        f"+ {round(exercise)} kcal of exercise ({exercise_source}) = {round(maintenance)} kcal "
        f"maintenance",
    ]
    if goal and rate:
        if withhold:
            steps.append(
                f"goal is {rate:+g} kg/week ({round(full_adjustment):+d} kcal/day), NOT applied "
                f"on a {training['day_type']} day"
            )
        elif protected:
            steps.append(
                f"{round(adjustment):+d} kcal/day for a {rate:+g} kg/week goal, applied in full "
                f"on a {training['day_type']} day — a surplus is not withheld"
            )
        else:
            steps.append(f"{round(adjustment):+d} kcal/day for a {rate:+g} kg/week goal")
    elif goal:
        steps.append(f"goal is {goal['goal_type']}, so no deficit or surplus applied")
    else:
        steps.append("no active goal, so this is maintenance")
    if clamped:
        steps.append(f"clamped up to BMR ({round(bmr)} kcal)")
    steps.append(f"= {round(target)} kcal")

    result: dict[str, Any] = {
        "date": day,
        "day_type": training["day_type"],
        "suggested": {
            "kcal": round(target),
            "protein_g": protein,
            "fiber_g": fiber,
            "day_type": training["day_type"],
        },
        "inputs": {
            "weight_kg": weight["value_kg"],
            "weight_date": weight["effective_date"],
            "height_cm": athlete["height_cm"],
            "gender": athlete["gender"],
            "age_years": age,
            "baseline_factor": baseline_factor,
            "protein_g_per_kg": protein_g_per_kg,
            "goal": goal,
        },
        "working": {
            "bmr_kcal": round(bmr),
            "baseline_kcal": round(baseline),
            "exercise_kcal": round(exercise),
            "exercise_source": exercise_source,
            "maintenance_kcal": round(maintenance),
            "goal_adjustment_kcal": round(adjustment),
            "goal_adjustment_withheld_kcal": round(full_adjustment) if withhold else 0,
            "clamped_to_bmr": clamped,
        },
        "steps": steps,
        "training": training,
    }

    notes: list[str] = []
    if withhold:
        notes.append(
            f"{day} is a {training['day_type']} day, so the {round(full_adjustment):+d} kcal/day "
            f"goal adjustment was not applied. Under-fuelling a hard session costs the session "
            f"and the recovery from it; take the extra mostly as carbohydrate."
        )
    elif protected and full_adjustment > 0:
        notes.append(
            f"{day} is a {training['day_type']} day, and the goal's "
            f"{round(full_adjustment):+d} kcal/day surplus was applied in full: under-fuelling "
            f"is the risk on a day like this, not over-fuelling."
        )
    if exercise_source == "imported_activity+planned_workout":
        names = ", ".join(training.get("outstanding_planned_names") or [])
        notes.append(
            f"Exercise calories include an estimated {round(outstanding_kcal)} kcal from "
            f"{names}, still planned and not yet imported today. Re-run this once it is."
        )
    if clamped:
        notes.append(
            f"The goal's rate would have put this day below resting metabolic rate "
            f"({round(bmr)} kcal). It was clamped to BMR. If the rate keeps producing this, "
            f"the rate is too aggressive for this athlete's size — slow it down rather than "
            f"eating under BMR."
        )
    if training["day_type"] in ("race", "race_eve"):
        notes.append(
            f"Fibre is suggested at {fiber:g} g rather than {DEFAULT_FIBER_G:g} g for a "
            f"{training['day_type']} day: it is still in the gut when the race starts. Carbs "
            f"up, fibre down, a little more salt, and drink."
        )
    if exercise_source == "planned_workout":
        notes.append(
            "Exercise calories are estimated from the planned session's mechanical work at "
            f"{GROSS_EFFICIENCY:.0%} gross efficiency — not measured. Re-run this after the "
            "ride is imported if the session changed."
        )
    if exercise_source == "none" and training["day_type"] != "rest":
        notes.append(
            "No calorie figure for this day's training: the activity carries none and there is "
            "no planned session to estimate from. Pass exercise_kcal_override with an estimate, "
            "or import the ride."
        )
    if training["unmeasured_activities"]:
        notes.append(
            f"{training['unmeasured_activities']} imported "
            f"activit{'y' if training['unmeasured_activities'] == 1 else 'ies'} on this date "
            f"carr{'ies' if training['unmeasured_activities'] == 1 else 'y'} no calorie figure "
            f"from Garmin and contributed nothing."
        )
    if athlete["gender"] == "other":
        notes.append(
            "Gender is recorded as 'other', so BMR uses the midpoint of the two Mifflin-St "
            "Jeor constants. That is a compromise, not a measurement — correct the target "
            "against the weight trend over three or four weeks."
        )
    if notes:
        result["notes"] = notes
    return result


class _RangeHistory:
    """Every dated figure and training-table row a date range needs, read once.

    Mirrors `coach.History`: resolving weight, the goal and the day type one
    query at a time turned a 120-day `week_summary` or `suggest_targets` into
    roughly six queries a day for the same handful of rows. Built once for an
    inclusive `[first, last]` range of ISO date strings; every lookup after
    that is in memory. Single-date callers keep querying directly — see
    `_suggest_one_for`.
    """

    def __init__(self, conn: sqlite3.Connection, athlete_id: int, first: str, last: str) -> None:
        self._weight_rows = [
            _dict(row) or {}
            for row in conn.execute(
                "SELECT * FROM weight_history WHERE athlete_id = ? AND effective_date <= ? "
                "ORDER BY effective_date, id",
                (athlete_id, last),
            )
        ]
        self._goal_rows = [
            _dict(row) or {}
            for row in conn.execute(
                "SELECT * FROM nutrition_goals WHERE athlete_id = ? ORDER BY effective_date, id",
                (athlete_id,),
            )
        ]
        self._activities: dict[str, list[dict]] = {}
        for row in conn.execute(
            "SELECT id, local_date, name, sport, duration_s, calories, avg_power, "
            "normalized_power FROM activities WHERE athlete_id = ? "
            "AND local_date BETWEEN ? AND ? ORDER BY id",
            (athlete_id, first, last),
        ):
            record = _dict(row) or {}
            self._activities.setdefault(record["local_date"], []).append(record)
        self._planned: dict[str, list[dict]] = {}
        for row in conn.execute(
            "SELECT id, spec_json, status, linked_activity_id, scheduled_date "
            "FROM planned_workouts WHERE athlete_id = ? "
            "AND scheduled_date BETWEEN ? AND ? ORDER BY id",
            (athlete_id, first, last),
        ):
            record = _dict(row) or {}
            self._planned.setdefault(record["scheduled_date"], []).append(record)

        from .coach import NON_STARTING_EVENT_STATUSES  # deferred: see _event_on

        lookahead = (parse_date(last, "end") + timedelta(days=1)).isoformat()
        placeholders = ", ".join("?" for _ in NON_STARTING_EVENT_STATUSES)
        self._events: dict[str, dict] = {}
        for row in conn.execute(
            f"SELECT * FROM events WHERE athlete_id = ? AND event_date BETWEEN ? AND ? "
            f"AND status NOT IN ({placeholders}) ORDER BY id",
            (athlete_id, first, lookahead, *NON_STARTING_EVENT_STATUSES),
        ):
            record = _dict(row) or {}
            # First (lowest id) per date wins — matches `_event_on`'s own
            # `ORDER BY id LIMIT 1`. An event date is not unique in the schema.
            self._events.setdefault(record["event_date"], record)

        self._food_log: dict[str, list[dict]] = {}
        for row in conn.execute(
            "SELECT * FROM food_log WHERE athlete_id = ? AND log_date BETWEEN ? AND ? ORDER BY id",
            (athlete_id, first, last),
        ):
            record = _log_out(_dict(row) or {})
            self._food_log.setdefault(record["log_date"], []).append(record)
        self._targets: dict[str, dict] = {}
        for row in conn.execute(
            "SELECT * FROM daily_targets WHERE athlete_id = ? AND target_date BETWEEN ? AND ?",
            (athlete_id, first, last),
        ):
            record = _dict(row) or {}
            record.pop("rationale_json", None)
            self._targets[record["target_date"]] = record

    def weight(self, on_date: str) -> dict | None:
        """See `_latest_weight`: the latest weigh-in at or before the date, never extrapolated."""
        candidates = [row for row in self._weight_rows if row["effective_date"] <= on_date]
        return candidates[-1] if candidates else None

    def goal(self, on_date: str) -> dict | None:
        """See `_active_goal`: effective at or before the date, and not yet closed as of it."""
        candidates = [
            row
            for row in self._goal_rows
            if row["effective_date"] <= on_date
            and (row.get("closed_date") is None or row["closed_date"] > on_date)
        ]
        return candidates[-1] if candidates else None

    def training(self, day: str) -> dict:
        """See `_day_type_and_training`: the same rules, over the rows read once."""
        tomorrow = (parse_date(day, "date") + timedelta(days=1)).isoformat()
        return _day_type_and_training_from_rows(
            day,
            self._activities.get(day, []),
            self._planned.get(day, []),
            self._events.get(day),
            self._events.get(tomorrow),
        )

    def food_log(self, day: str) -> list[dict]:
        return self._food_log.get(day, [])

    def targets(self, day: str) -> dict | None:
        return self._targets.get(day)


def suggest_targets(
    date_str: str | None = None,
    start: str | None = None,
    end: str | None = None,
    protein_g_per_kg: float = DEFAULT_PROTEIN_G_PER_KG,
    baseline_factor: float = SEDENTARY_FACTOR,
    exercise_kcal_override: float | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Propose calorie, protein and fibre targets for a date or a range — showing the work.

    Deterministic, and nothing is stored: this proposes, and `confirm_targets`
    accepts or overrides. Every input and intermediate is in the response so
    the confirmation can be informed rather than a rubber stamp.

    The chain, per day:

    1. **BMR** from Mifflin-St Jeor — the weight in effect on that date, height,
       age from the birth year, and gender.
    2. **x baseline factor** (default 1.3) for everything that is not training.
       Training is added separately, so this factor must stay a *sedentary* one
       or every session is counted twice.
    3. **+ exercise calories.** Garmin's figure from the imported activity where
       there is one; otherwise the planned session's mechanical work converted
       at cycling's gross efficiency; otherwise nothing, said out loud.
    4. **+/- the active goal's rate**, spread evenly across the week — except on
       a `big_session`, `race` or `race_eve` day, where it is withheld.
    5. **Clamped up to BMR.** A target below resting metabolic rate is refused
       as a target: it is clamped and the response says the rate is the thing
       to revisit.

    Day type is read off the training tables, never asked for: an event makes
    its date `race` and the day before `race_eve`; a long or hard ride makes it
    `big_session`; any ride makes it `training`; nothing makes it `rest`.

    Protein is `protein_g_per_kg` x bodyweight, rounded to 5 g. Fibre is 30 g, or
    15 g on a race or race-eve day.

    With the profile incomplete this returns what is missing rather than a
    guess — a BMR computed from an assumed gender is out by 166 kcal/day.

    Pass `date_str` for one day, or `start`/`end` for a range (a week is the
    useful unit — the deficit is judged on the weekly average, never on one
    day).
    """
    if date_str and (start or end):
        raise NutritionError("pass date_str for one day, or start and end for a range — not both")
    if (start is None) != (end is None):
        raise NutritionError("a range needs both start and end")

    if start and end:
        first, last = _range(start, end)
        days = [day.isoformat() for day in _days(first, last)]
    else:
        days = [_today(date_str).isoformat()]

    factor = _number(baseline_factor, "baseline_factor", (1.0, 2.5))
    per_kg = _number(protein_g_per_kg, "protein_g_per_kg", (0.5, 4.0))
    override = _optional_number(exercise_kcal_override, "exercise_kcal_override", (0.0, 15000.0))
    if override is not None and len(days) > 1:
        raise NutritionError(
            "exercise_kcal_override applies to one day; it would be wrong on every other day "
            "of a range. Suggest that day on its own."
        )

    with open_db() as conn:
        athlete = _ensure_athlete(conn, athlete_id)
        if start and end:
            # One batch of queries for the whole range, not one query set per
            # day — see `_RangeHistory`.
            history = _RangeHistory(conn, athlete_id, days[0], days[-1])
            suggestions = [
                _suggest_one(
                    day,
                    athlete,
                    history.weight(day),
                    history.training(day),
                    history.goal(day),
                    per_kg,
                    factor,
                    override,
                )
                for day in days
            ]
        else:
            suggestions = [
                _suggest_one_for(conn, athlete_id, days[0], athlete, per_kg, factor, override)
            ]

    usable = [item for item in suggestions if "suggested" in item]
    result: dict[str, Any] = {
        "days": suggestions,
        "stored": False,
        "note": (
            "Nothing has been stored. Show the athlete the working, then call confirm_targets "
            "with what they agree to — as suggested, or with overrides."
        ),
    }
    if len(suggestions) == 1:
        result["date"] = suggestions[0]["date"]
    if usable and len(usable) > 1:
        result["range_average"] = {
            "kcal": round(sum(item["suggested"]["kcal"] for item in usable) / len(usable)),
            "protein_g": round(
                sum(item["suggested"]["protein_g"] for item in usable) / len(usable), 1
            ),
            "days": len(usable),
        }
        result["range_note"] = (
            "The deficit is judged on the weekly average, never on a single day. A restaurant "
            "on Saturday is absorbed by the other six."
        )
    return result


def confirm_targets(
    date_str: str | None = None,
    kcal: float | None = None,
    protein_g: float | None = None,
    fiber_g: float | None = None,
    day_type: str | None = None,
    note: str | None = None,
    days: Any = None,
    protein_g_per_kg: float | None = None,
    baseline_factor: float | None = None,
    exercise_kcal_override: float | None = None,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """Store the day's targets: the suggestion as it stands, or with overrides.

    Call with just a date to accept what `suggest_targets` proposed for it —
    the suggestion is recomputed here rather than passed back in, so nothing
    can be confirmed that the server would not have proposed. `protein_g_per_kg`,
    `baseline_factor` and `exercise_kcal_override` are the same three knobs
    `suggest_targets` takes, and the recomputation needs them too — without
    them a suggestion shown with `exercise_kcal_override=800` and accepted as
    it stood would be re-derived with none, and a different number would be
    filed than the one the athlete saw. They are inputs to the derivation, not
    overrides of the outcome: passing them alone does not flip `source` to
    `overridden` the way `kcal`/`protein_g`/`fiber_g` do.

    Pass any of `kcal` / `protein_g` / `fiber_g` to override the *result*, and
    `source` is recorded as `overridden` rather than `confirmed` so a later
    reading knows which numbers were the athlete's.

    Confirming again for the same date replaces that date's targets; one date
    has one set, or every remainder depends on which was read.

    `days` confirms several dates in one call, each an object in the same
    shape — `[{"date": "2026-08-24", "kcal": 2600}, ...]`. Any of the three
    knobs can be set per object too, and a per-object value wins over the
    top-level one for that date. `exercise_kcal_override` only ever applies to
    one day, so a top-level one is refused across a multi-day `days` call —
    exactly as `suggest_targets` refuses it over a range; a per-object one is
    fine on any number of dates, because each is that date's own figure.

    A kcal override below computed BMR is refused. That is not a target this
    server will file, whoever asked for it; the goal's rate is what should
    change.
    """
    requests: list[dict] = []
    if days is not None:
        if isinstance(days, dict):
            days = [days]
        if not isinstance(days, (list, tuple)):
            raise NutritionError(f"days must be a list of objects, got {type(days).__name__}")
        requests.extend(days)
    if date_str is not None or not requests:
        requests.append(
            {
                "date": date_str,
                "kcal": kcal,
                "protein_g": protein_g,
                "fiber_g": fiber_g,
                "day_type": day_type,
                "note": note,
                "protein_g_per_kg": protein_g_per_kg,
                "baseline_factor": baseline_factor,
                "exercise_kcal_override": exercise_kcal_override,
            }
        )

    default_protein_g_per_kg = (
        _number(protein_g_per_kg, "protein_g_per_kg", (0.5, 4.0))
        if protein_g_per_kg is not None
        else DEFAULT_PROTEIN_G_PER_KG
    )
    default_baseline_factor = (
        _number(baseline_factor, "baseline_factor", (1.0, 2.5))
        if baseline_factor is not None
        else SEDENTARY_FACTOR
    )
    default_exercise_override = _optional_number(
        exercise_kcal_override, "exercise_kcal_override", (0.0, 15000.0)
    )
    if default_exercise_override is not None and len(requests) > 1:
        raise NutritionError(
            "exercise_kcal_override applies to one day; it would be wrong on every other day "
            "of a multi-day confirm. Pass it per day inside `days`, or confirm that day alone."
        )

    stored: list[dict] = []
    rejected: list[dict] = []
    stamp = now_utc()

    with open_db() as conn:
        athlete = _ensure_athlete(conn, athlete_id)
        for index, item in enumerate(requests):
            if not isinstance(item, dict):
                rejected.append(
                    {"index": index, "reason": f"expected an object, got {type(item).__name__}"}
                )
                continue
            try:
                day = _today(item.get("date") or item.get("date_str")).isoformat()
                item_protein = (
                    _number(item["protein_g_per_kg"], "protein_g_per_kg", (0.5, 4.0))
                    if item.get("protein_g_per_kg") is not None
                    else default_protein_g_per_kg
                )
                item_baseline = (
                    _number(item["baseline_factor"], "baseline_factor", (1.0, 2.5))
                    if item.get("baseline_factor") is not None
                    else default_baseline_factor
                )
                item_override = (
                    _number(
                        item["exercise_kcal_override"], "exercise_kcal_override", (0.0, 15000.0)
                    )
                    if item.get("exercise_kcal_override") is not None
                    else default_exercise_override
                )
                suggestion = _suggest_one_for(
                    conn,
                    athlete_id,
                    day,
                    athlete,
                    item_protein,
                    item_baseline,
                    item_override,
                )
                overrides = {
                    key: item.get(key)
                    for key in ("kcal", "protein_g", "fiber_g")
                    if item.get(key) is not None
                }
                if "suggested" not in suggestion and not (
                    "kcal" in overrides and "protein_g" in overrides and "fiber_g" in overrides
                ):
                    raise NutritionError(
                        f"{day}: {suggestion['missing_note']} Or pass kcal, protein_g and "
                        f"fiber_g explicitly to file a target anyway."
                    )
                base = suggestion.get(
                    "suggested",
                    {"kcal": 0, "protein_g": 0, "fiber_g": 0, "day_type": suggestion["day_type"]},
                )
                values = {
                    "kcal": _number(overrides.get("kcal", base["kcal"]), "kcal", (500.0, 12000.0)),
                    "protein_g": _number(
                        overrides.get("protein_g", base["protein_g"]), "protein_g", (0.0, 500.0)
                    ),
                    "fiber_g": _number(
                        overrides.get("fiber_g", base["fiber_g"]), "fiber_g", (0.0, 200.0)
                    ),
                }
                bmr = (suggestion.get("working") or {}).get("bmr_kcal")
                if bmr and values["kcal"] < bmr:
                    raise NutritionError(
                        f"{day}: {values['kcal']:g} kcal is below this athlete's computed "
                        f"resting metabolic rate ({bmr} kcal). This server will not file that "
                        f"as a target. If the goal keeps producing it, the goal's rate is what "
                        f"needs to change — and a sustained intake under BMR alongside training "
                        f"is worth a dietitian's eyes, not a smaller number."
                    )
                kind = (
                    _one_of(item.get("day_type"), DAY_TYPES, "day_type")
                    or base.get("day_type")
                    or suggestion["day_type"]
                )
                source = "overridden" if overrides else "confirmed"
                conn.execute(
                    "INSERT INTO daily_targets (athlete_id, target_date, kcal, protein_g, "
                    "fiber_g, day_type, source, rationale_json, note, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(athlete_id, target_date) DO UPDATE SET "
                    "kcal = excluded.kcal, protein_g = excluded.protein_g, "
                    "fiber_g = excluded.fiber_g, day_type = excluded.day_type, "
                    "source = excluded.source, rationale_json = excluded.rationale_json, "
                    "note = excluded.note, updated_at = excluded.updated_at",
                    (
                        athlete_id,
                        day,
                        values["kcal"],
                        values["protein_g"],
                        values["fiber_g"],
                        kind,
                        source,
                        json.dumps(
                            {
                                "steps": suggestion.get("steps") or [],
                                "knobs": {
                                    "protein_g_per_kg": item_protein,
                                    "baseline_factor": item_baseline,
                                    "exercise_kcal_override": item_override,
                                },
                            },
                            ensure_ascii=False,
                        ),
                        _text(item.get("note")),
                        stamp,
                        stamp,
                    ),
                )
                row = _stored_targets(conn, athlete_id, day)
                assert row is not None
                entry = dict(row)
                if overrides:
                    entry["overrode"] = {key: base[key] for key in overrides if key in base}
                stored.append(entry)
            except (NutritionError, ValueError) as exc:
                rejected.append({"index": index, "date": item.get("date"), "reason": str(exc)})

    result: dict[str, Any] = {
        "stored": len(stored),
        "rejected": len(rejected),
        "targets": stored,
        "rejections": rejected,
    }
    if stored:
        result["note"] = (
            "day_summary now has a remainder to work from for "
            f"{', '.join(row['target_date'] for row in stored)}."
        )
    return result


# --------------------------------------------------------------------------
# the week
# --------------------------------------------------------------------------


def _moving_average(points: list[tuple[str, float]], window: int) -> list[dict]:
    """A trailing mean over `window` days of weigh-ins, one point per weigh-in.

    Over the weigh-ins that exist, not over the calendar: an athlete who
    skipped Wednesday has not lost a data point, and interpolating one would
    invent a measurement. A mean of fewer than `window` entries is still
    reported, with its count, because early in a goal that is all there is.
    """
    out: list[dict] = []
    for index, (day, _) in enumerate(points):
        window_points = points[max(0, index - window + 1) : index + 1]
        values = [value for _, value in window_points]
        out.append(
            {
                "date": day,
                "mean_kg": round(sum(values) / len(values), 2),
                "from_entries": len(values),
            }
        )
    return out


def _weight_trend(
    conn: sqlite3.Connection, athlete_id: int, first: date, last: date, goal: dict | None
) -> dict:
    """Where the weight is actually going, on the 7-day mean rather than the scale.

    Day-to-day weight is mostly water, glycogen and salt: two kilos can appear
    after a race and be gone by Thursday. Judging a deficit on a single morning
    is how an athlete concludes a good week failed. So this compares the
    trailing means at the two ends of the window and reports kg/week, next to
    the goal's rate.

    Fewer than two weigh-ins in the window is not a trend, and it is reported
    as not being one rather than as zero.
    """
    reach = (first - timedelta(days=WEIGHT_TREND_WINDOW_DAYS - 1)).isoformat()
    rows = [
        (row["effective_date"], float(row["value_kg"]))
        for row in conn.execute(
            "SELECT effective_date, value_kg FROM weight_history WHERE athlete_id = ? "
            "AND effective_date BETWEEN ? AND ? ORDER BY effective_date, id",
            (athlete_id, reach, last.isoformat()),
        )
    ]
    in_window = [point for point in rows if point[0] >= first.isoformat()]
    trend: dict[str, Any] = {
        "weigh_ins_in_window": len(in_window),
        "moving_average_days": WEIGHT_TREND_WINDOW_DAYS,
    }
    if len(rows) < 2 or not in_window:
        trend["trend"] = None
        trend["note"] = (
            "Not enough weigh-ins to read a trend. Weigh in the same way each morning — the "
            "trend needs the routine more than it needs precision."
        )
        return trend

    means = _moving_average(rows, WEIGHT_TREND_WINDOW_DAYS)
    reported = [point for point in means if point["date"] >= first.isoformat()] or means[-1:]
    trend["moving_average"] = reported
    # The endpoints are the first and last *in-window* means, not the first
    # and last mean the reach happened to produce. A weigh-in up to six days
    # before the window anchored the trend to a single raw reading before —
    # the reach exists to smooth the window's own points, not to relocate them.
    start_point, end_point = reported[0], reported[-1]
    span_days = (
        parse_date(end_point["date"], "date") - parse_date(start_point["date"], "date")
    ).days
    if span_days <= 0:
        trend["trend"] = None
        if len(reported) < 2:
            # Only one weigh-in date falls inside the window itself — the
            # pre-window rows the reach pulled in fed the moving average
            # (that is what the reach is for) but there is no second
            # in-window date to pair it with, so there is no span to anchor.
            # This is not the same failure as several in-window weigh-ins
            # sharing one calendar date; saying "one date" here would be
            # false when the window and the earlier readings plainly span
            # more than one date.
            trend["note"] = (
                "Only one weigh-in falls inside the window. Earlier weigh-ins before the "
                "window fed the moving average, but there is no second in-window date to "
                "read a span between."
            )
        else:
            trend["note"] = "All weigh-ins fall on one date; no span to read a rate over."
        return trend

    change = end_point["mean_kg"] - start_point["mean_kg"]
    per_week = change / span_days * 7.0
    trend["change_kg"] = round(change, 2)
    trend["over_days"] = span_days
    trend["kg_per_week"] = round(per_week, 2)
    trend["latest_mean_kg"] = end_point["mean_kg"]
    if goal and goal.get("rate_kg_per_week"):
        goal_rate = float(goal["rate_kg_per_week"])
        trend["goal_kg_per_week"] = goal_rate
        trend["difference_kg_per_week"] = round(per_week - goal_rate, 2)
        trend["note"] = (
            f"Trending {per_week:+.2f} kg/week against a goal of {goal_rate:+g} kg/week, on the "
            f"{WEIGHT_TREND_WINDOW_DAYS}-day mean. One morning's number is water and glycogen; "
            f"this is the figure to steer on, and it needs three or four weeks before it means "
            f"anything."
        )
    else:
        trend["note"] = (
            f"Trending {per_week:+.2f} kg/week on the {WEIGHT_TREND_WINDOW_DAYS}-day mean. No "
            f"active goal rate to compare it against."
        )
    return trend


def week_summary(
    start: str,
    end: str,
    athlete_id: int = DEFAULT_ATHLETE_ID,
) -> dict:
    """The week: each day against its targets, the averages, the trend, the cost.

    This is where the deficit is actually judged. **Never on a single day** —
    a restaurant on Saturday inside a week that averaged on target is a week
    that went to plan, and reading it a day at a time turns a normal life into
    a series of failures.

    Returns, per day: totals, targets, the difference, the day type, and
    whether anything was logged at all. Then the weekly averages against the
    average target, the weight trend on the 7-day moving average (not the
    scale), and the food cost.

    **Cost counts only what could be priced.** A free-form estimate is never
    priced, and an ingredient with no package price contributes none — both are
    counted and reported, because a total that treats unpriced entries as free
    reads as a cheap week and is a wrong one.

    A day with nothing logged is reported as unlogged rather than as a zero-
    calorie day. Averaging a blank day in as zero is the fastest way to make a
    week look like starvation.
    """
    first, last = _range(start, end)
    days = [day.isoformat() for day in _days(first, last)]

    with open_db() as conn:
        # One batch of queries for the whole range, not six per day — see
        # `_RangeHistory`.
        history = _RangeHistory(conn, athlete_id, first.isoformat(), last.isoformat())
        goal = history.goal(last.isoformat())
        rows: list[dict] = []
        for day in days:
            entries = history.food_log(day)
            targets = history.targets(day)
            training = history.training(day)
            totals = _totals(entries)
            row: dict[str, Any] = {
                "date": day,
                "day_type": (targets or {}).get("day_type") or training["day_type"],
                "logged": bool(entries),
                "entry_count": len(entries),
                "totals": totals if entries else None,
                "targets": targets,
                "exercise_kcal": training["measured_exercise_kcal"],
                "estimates": sum(1 for entry in entries if entry["is_estimate"]),
            }
            if entries and targets:
                row["difference"] = {
                    "kcal": round(totals["kcal"] - targets["kcal"], 1),
                    "protein_g": round(totals["protein_g"] - targets["protein_g"], 1),
                    "fiber_g": round(totals["fiber_g"] - targets["fiber_g"], 1),
                }
            rows.append(row)
        trend = _weight_trend(conn, athlete_id, first, last, goal)

    logged = [row for row in rows if row["logged"]]
    with_targets = [row for row in logged if row["targets"]]

    averages: dict[str, Any] = {"logged_days": len(logged), "days_in_range": len(rows)}
    if logged:
        for key in ("kcal", "protein_g", "fiber_g"):
            averages[f"{key}_per_day"] = round(
                sum(row["totals"][key] for row in logged) / len(logged), 1
            )
    if with_targets:
        for key in ("kcal", "protein_g", "fiber_g"):
            averages[f"target_{key}_per_day"] = round(
                sum(row["targets"][key] for row in with_targets) / len(with_targets), 1
            )
            averages[f"{key}_vs_target_per_day"] = round(
                sum(row["totals"][key] - row["targets"][key] for row in with_targets)
                / len(with_targets),
                1,
            )

    cost = round(sum((row["totals"] or {}).get("cost", 0.0) for row in logged), 2)
    unpriced = sum((row["totals"] or {}).get("cost_unpriced_entries", 0) for row in logged)
    estimates = sum(row["estimates"] for row in rows)
    # Mirrors `_totals`' per-day counters: a free-form estimate that never stated
    # protein or fibre is missing, not zero, and `protein_g_per_day` /
    # `fiber_g_per_day` below sum only the days' known figures — surfacing the
    # counters keeps that average from reading as complete when it is not.
    protein_missing = sum(
        (row["totals"] or {}).get("protein_g_missing_entries", 0) for row in logged
    )
    fiber_missing = sum((row["totals"] or {}).get("fiber_g_missing_entries", 0) for row in logged)
    if protein_missing:
        averages["protein_g_missing_entries"] = protein_missing
    if fiber_missing:
        averages["fiber_g_missing_entries"] = fiber_missing

    result: dict[str, Any] = {
        "start": first.isoformat(),
        "end": last.isoformat(),
        "days": rows,
        "averages": averages,
        "weight_trend": trend,
        "goal": goal,
        "cost": {
            "total": cost,
            "per_day": round(cost / len(logged), 2) if logged else None,
            "unpriced_entries": unpriced,
            "note": (
                f"{unpriced} logged entr{'y' if unpriced == 1 else 'ies'} had no price and "
                f"contributed nothing to this total, so the real figure is higher."
                if unpriced
                else "Every logged entry carried a price."
            ),
        },
        "judged_on": (
            "The weekly average, never a single day. A restaurant, a birthday or a race meal "
            "inside a week that averaged on target is a week that went to plan."
        ),
    }
    unlogged = [row["date"] for row in rows if not row["logged"]]
    if unlogged:
        result["unlogged_days"] = unlogged
        result["unlogged_note"] = (
            f"{_plural(len(unlogged), 'day')} had nothing logged and "
            f"{_agree(len(unlogged), 'is')} excluded from the averages rather than "
            f"counted as zero calories."
        )
    if estimates:
        result["estimate_note"] = (
            f"{_plural(estimates, 'entry')} {_agree(estimates, 'is')} free-form estimates. "
            f"They count in the calorie average always, in the protein and fibre averages only "
            f"when they stated a figure, and never in the cost."
        )
    if protein_missing or fiber_missing:
        parts = []
        if protein_missing:
            parts.append(f"protein from {_plural(protein_missing, 'entry')}")
        if fiber_missing:
            parts.append(f"fibre from {_plural(fiber_missing, 'entry')}")
        # Verb agreement follows the conjoined subjects, as in _day_summary.
        result["unknown_macros_note"] = (
            f"{' and '.join(parts)} across the week {_agree(len(parts), 'is')} unknown, "
            f"not zero — a free-form estimate "
            f"that never stated it. protein_g_per_day / fiber_g_per_day above are averaged over "
            f"known entries only, so they undercount by however much those entries turn out to "
            f"hold."
        )
    return result

## What and why

<!-- What changes, and what problem it solves. Link the issue if there is one. -->

## Does this change rendered output?

<!-- Delete whichever does not apply. -->

- [ ] **No** — the `.zwo` and Garmin payloads for an unchanged spec are byte-identical.
- [ ] **Yes** — golden files updated, and the change is described in CHANGELOG.md.

A change to rendered output alters what a caller actually rides, even when no tool signature moves.
If the golden files changed, say what the new output means and why it is more correct.

## Checklist

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `pytest` passes
- [ ] Tests added or updated for the behaviour change
- [ ] Docs updated (README, skills, `docs/`) if configuration or behaviour changed
- [ ] CHANGELOG.md `[Unreleased]` updated

## Garmin or MyWhoosh schema changes

<!-- Only if you touched a renderer. -->

- [ ] Verified against the live API (`pytest -m live`), not just against the unit tests
- [ ] The curated read was **not** used as the source of truth — see docs and `verify.py`

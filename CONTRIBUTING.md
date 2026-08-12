# Contributing

Contributions are welcome — this repo **accepts pull requests**. Bug reports, new block types, docs
fixes, and code changes are all appreciated.

## Getting started

```bash
uv venv && uv sync --extra dev
```

Or with pip: `pip install -e ".[dev]"`.

See [CLAUDE.md](CLAUDE.md) for the architecture and the repository conventions.

## The local gate

All three must pass before you push:

```bash
ruff check . && ruff format --check . && pytest
```

CI runs the same gate on ubuntu, windows and macOS, plus Python 3.10 and 3.13 on Linux.

## Submitting a pull request

1. **Open an issue first** for anything non-trivial, so the approach can be agreed before you invest
   time.
2. **Branch off `dev`**, not `main` — `main` only accepts PRs from `dev` (enforced by CI). Keep the
   change focused: one logical change per PR.
3. **Add or update tests.** The offline suite needs no network and no credentials.
4. **Update the docs** when you change the spec format, a tool, or behaviour.
5. **Add a CHANGELOG entry** under `## [Unreleased]`.

## Two rules specific to this project

**Rendered output is behaviour.** If a change makes `render_zwo` or `render_garmin` emit anything
different for an unchanged spec, that changes what someone actually rides. The golden files in
`tests/golden/` exist to make that impossible to land by accident. If a golden file needs updating,
that is fine — but say in the PR what the new output means and why it is more correct, and never
update a golden file just to make a test pass.

**Never verify a platform claim against the curated read.** Garmin's `get_workout_by_id` returns a
lossy projection: it drops `targetValueUnit`, so a watt target and a %FTP target look identical
through it, and it reports a derived duration that disagrees with the steps. A test that compares a
round-trip against that shape will pass while the units are wrong. Compare against **what was
sent** — that is what `verify.py` does. If you touch a renderer, run the live suite:

```bash
pip install -e ".[live]" && pytest -m live
```

It needs the Garmin tokens the Garmin MCP already stores (`GARMINTOKENS`, or `~/.garminconnect`).
It creates a workout named `ClaudeCyclingMCP_test_*` and deletes it afterwards, including on failure.

## Working on the MyWhoosh skill

The browser flow is the fragile part of the repo, because it drives a UI that will change. Two
conventions keep failures legible:

- **Every step states what it expects to see.** When a step breaks, the run should report *which
  assumption no longer holds* rather than silently producing nothing.
- **Elements are located by visible text or role, and the skill says so**, so a renamed label is an
  obvious fix rather than a mystery.

If you change the flow, keep both. And if you hit a new MyWhoosh quirk, document it in the skill
even if you worked around it — the undocumented quirks are what cost the most time.

## Tests

```bash
pytest              # offline: renderers, validation, metrics, skills (default)
pytest -m live      # adds the real Garmin round-trip; needs tokens
```

The offline suite is fast and hermetic. No test writes outside a tmp directory.

# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`get_skill`** — returns a bundled procedure by name. The skills reach Claude Code through
  `.claude/skills` and other clients through MCP prompts, but neither route lets a *model* retrieve
  a procedure it has just been asked for by name; in Claude Desktop, "use the mywhoosh-upload skill"
  resolved to nothing unless the skill had been uploaded to the account. This closes that gap.
- **A MyWhoosh dry run** in `docs/smoke-test.md` that exercises the browser flow and stops before
  the export, so it spends no slot credit.

### Fixed

- **`back-merge.yml` pushes to `dev` instead of opening a pull request.** It failed on first use:
  opening a PR from a workflow needs a repository setting that is off by default.

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

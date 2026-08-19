# Versioning and releases

This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html): every release is
`MAJOR.MINOR.PATCH`. The version is the contract the MCP server advertises, so it describes the
**tool surface an MCP client sees**, not just internal code churn.

## What bumps which number

- **MAJOR** (`1.0.0`) — a breaking change to that surface: a tool removed or renamed, a required
  input added, an input or output schema changed incompatibly, a spec field removed or given
  different meaning. While the project is pre-`1.0.0`, breaking changes ride the MINOR slot instead
  (see below), but still call them out loudly in the changelog.
- **MINOR** (`0.2.0`) — new, backward-compatible surface: a new tool, a new optional spec field, a
  new skill or prompt, a materially new capability. Existing specs keep rendering unchanged.
- **PATCH** (`0.1.1`) — bug fixes, docs, CI, and internal refactors that leave the tool surface
  identical.

### A note specific to this project

**A change to rendered output is a behaviour change even when the tool signature is identical.** If
`render_garmin` starts emitting a different target type, or `render_zwo` changes how a ramp is
written, a caller's workouts change meaning without any API change to warn them. Treat those as
MINOR at minimum, never PATCH, and say so explicitly in the changelog. The golden-file tests exist
to make such a change impossible to land unnoticed.

### Pre-1.0 rule

Below `1.0.0` the surface is still settling, so everything shifts down one slot: a **breaking**
change bumps MINOR (`0.1.x` → `0.2.0`) and a **feature** bumps PATCH only when purely additive and
low-risk; otherwise prefer MINOR. When in doubt between two levels, pick the higher one.

## The version lives in four files

They must never drift:

| File | Why |
|---|---|
| [`pyproject.toml`](../pyproject.toml) | the published package version |
| [`src/cycling_mcp/__init__.py`](../src/cycling_mcp/__init__.py) | `__version__`, reported at runtime |
| [`.claude-plugin/plugin.json`](../.claude-plugin/plugin.json) | the Claude Code plugin |
| [`.claude-plugin/marketplace.json`](../.claude-plugin/marketplace.json) | the plugin's marketplace entry |

`tests/test_version_lockstep.py` asserts all four agree, so a partial bump fails the normal test run
rather than being discovered at publish time.

`manifest.json` (the Claude Desktop `.mcpb` bundle) is synced to the tag by CI at bundle time, so it
does not need bumping by hand.

## Cutting a release

Releases merge from `dev` into `main` — a required CI gate (`only-dev-into-main.yml`) rejects a PR
into `main` from any other branch.

1. **Finish the changelog.** Move everything under `## [Unreleased]` in
   [CHANGELOG.md](../CHANGELOG.md) into a new `## [X.Y.Z] - YYYY-MM-DD` heading, leave an empty
   `## [Unreleased]` on top, and update the link definitions at the bottom.
2. **Bump the version in the four files above, in lock-step.** `pytest` will tell you if you missed
   one.
3. **Run the full local gate:**

   ```bash
   ruff check . && ruff format --check . && pytest
   ```

4. **Open a PR from `dev` into `main`** titled `Release X.Y.Z`.
5. **After merge, tag `main`** as `vX.Y.Z` and push the tag:

   ```bash
   git checkout main && git pull && git tag v0.1.0 && git push --tags
   ```

   Pushing a `v*` tag triggers `publish.yml` (runs the gate, then publishes to PyPI) and
   `bundle.yml` (builds the `.mcpb` and attaches it to the GitHub Release). `publish.yml` fails fast
   if the tag does not match `pyproject.toml`, which is why step 2 matters.

## Publishing credentials

`publish.yml` uses [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) — OpenID
Connect, no long-lived API token in repository secrets. It needs a one-time setup on PyPI:

1. On PyPI, go to the project (or *Publishing* → *Add a pending publisher* for a first release).
2. Add a GitHub publisher: owner `elias-ramzi`, repository `ClaudeCyclingMCP`, workflow
   `publish.yml`, environment `pypi`.
3. In the GitHub repo, create an environment named `pypi`.

Nothing else is stored anywhere. If you would rather use an API token, swap the `id-token: write`
permission for a `PYPI_API_TOKEN` secret and pass `password:` to the publish action.

## Keeping `dev` a superset of `main`

Merging the release PR adds a merge commit to `main` that `dev` lacks. With "require branches to be
up to date" on `main`, that one commit leaves the *next* release PR flagged `BEHIND` and
unmergeable, even though `dev` already holds all of `main`'s content. `back-merge.yml` pushes the
merge straight to `dev` after every push to `main`, so the gap never accumulates.

It pushes rather than opening a pull request deliberately: creating a PR from a workflow requires
the repository setting **Allow GitHub Actions to create and approve pull requests**, which is off by
default — without it the job fails with `GitHub Actions is not permitted to create or approve pull
requests`. A direct push needs only `contents: write`. If `dev` is ever protected, either exempt
this workflow or switch back to the PR route and enable that setting.

## Branch protection to configure

The workflows enforce policy only if GitHub is set to require them. On `main`:

- Require a pull request before merging.
- Require status checks to pass: `Lint, format, test (ubuntu-latest, 3.12)` and
  `Only dev can merge into main`.
- Require branches to be up to date before merging.

These are repository settings, not files, so they are not applied by cloning this repo.

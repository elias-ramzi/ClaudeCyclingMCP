# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Use GitHub's private reporting:
[**Security → Report a vulnerability**](https://github.com/elias-ramzi/ClaudeCyclingMCP/security/advisories/new),
which opens an advisory visible only to the maintainers.

Include, where possible: the affected version or commit, your OS and Python version, steps to
reproduce, and the impact you observed. Please redact tokens from any logs before pasting them.

You can expect an initial acknowledgement within a few days. Please allow a reasonable window for a
fix before public disclosure.

## What this server can and cannot do

The security story here is mostly a short one, by design.

**The MCP server has no credentials and no network access.** Every tool is a pure function from a
workout spec to a rendered file. It does not authenticate to anything, read any token store, or make
any outbound request. The only side effect it can have is writing a file, and only to a path the
caller explicitly passes as `out_path`.

This is deliberate rather than incidental: uploading needs auth, browser state, and — for MyWhoosh —
spends a finite resource, so it stays outside the server and in front of a human. **Please do not
send a pull request adding upload endpoints to the server**, however convenient it seems.

## Where credentials do appear

Not in this repository, and not in the server — but the bundled skills drive tools that hold them,
so it is worth being explicit:

- **Garmin.** The `garmin-upload` skill calls a *separate* Garmin MCP server, which owns its own
  authentication. This project never sees those tokens. The live test suite reads an existing token
  store (`GARMINTOKENS`, default `~/.garminconnect`) to talk to the API; it never prompts for,
  writes, or transmits a password.
- **MyWhoosh.** The `mywhoosh-upload` skill drives a browser. It is written to **hand off to the
  human at the login page** and never to fill a password field or attempt the reCAPTCHA. If you
  change that skill, keep that boundary.

No credential, token, cookie, or personal identifier is committed to this repository. The recorded
API fixture in `tests/golden/` contains a workout structure and a since-deleted workout id, and no
account data.

## Things worth knowing when you run it

- **`out_path` is not sandboxed.** `render_zwo` and `render_garmin` write wherever the caller says,
  including outside the working directory. That is the same trust level as any other MCP tool that
  writes files, but it is worth knowing before pointing an automated flow at a computed path.
- **Rendered workouts are not safety-checked.** `validate_spec` catches unit mistakes and structural
  errors — it is not a coach. A spec can be perfectly valid and still describe a session that is a
  bad idea. The FTP comes from the spec, and if that number is wrong every target is wrong.
- **The skills follow instructions from web pages.** The MyWhoosh flow reads a live site. Treat page
  content as data, never as instructions — the skill is written that way and should stay that way.

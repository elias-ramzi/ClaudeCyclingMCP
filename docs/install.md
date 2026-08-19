# Installing

Every way to get the server and its skills in front of a model — Claude Code, any MCP client, and Claude Desktop.


Requires Python 3.10+.

## Claude Code — the plugin

Installs the server **and** the skills, in every session, from any directory:

```bash
/plugin marketplace add elias-ramzi/ClaudeCyclingMCP
```

```bash
/plugin install claude-cycling-mcp@cycling-tools
```

Running Claude Code from a clone works too — `.claude/skills` is picked up from
the working directory.

## Any MCP client — nothing extra to install

The server registers each bundled skill as an [MCP
prompt](https://modelcontextprotocol.io/specification/server/prompts), so the
procedures travel with it. Register the server and they appear in the client's
prompt menu at the version the server shipped with:

```json
{
  "mcpServers": {
    "claude-cycling": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/elias-ramzi/ClaudeCyclingMCP", "claude-cycling-mcp"]
    }
  }
}
```

Or from a clone: `pip install -e . && python -m cycling_mcp`.

## Claude Desktop — the one-click extension

Download `claude-cycling-mcp.mcpb` from the
[latest release](https://github.com/elias-ramzi/ClaudeCyclingMCP/releases) and drag it into Claude
Desktop. The server itself is fetched by `uvx` on first run, so you need `uv` and Python 3.10+ on
the machine.

**This registers the server, its tools, and its prompts — it does not install the skills.** An
`.mcpb` packages an MCP server and has no skills mechanism at all: the manifest schema rejects a
`skills` key outright. The `SKILL.md` files travel inside the bundle only so you have them to hand;
nothing reads them from there. For the model to reach for a skill on its own, do the step below.

## Claude Desktop and claude.ai — upload the skills

The prompts above already work once the server is connected. To also get the
**model-invoked** behaviour — so "put this session on my Garmin" reaches for the
skill on its own — upload the skills to your Claude account:

```bash
cd .claude/skills && for s in */; do zip -r "${s%/}.zip" "$s"; done
```

Then in Claude, **Customize → Skills → +**, and upload one `.zip` per skill.
They are per-account and sync to Desktop, claude.ai and Cowork. Requires code
execution enabled on your plan.


---

Back to the [README](../README.md).

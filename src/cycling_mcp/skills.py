"""Load the bundled skills so the server can offer them as MCP prompts.

`.claude/skills` is read by Claude Code alone. Exposing the same `SKILL.md`
bodies as MCP prompts is how they reach every other client — Claude Desktop,
Cursor — without anyone uploading anything: they travel with the server.

The prompts carry instructions, nothing more. The server still has no upload
capability of its own; the procedures are followed by the client, using its own
tools and with the human in the loop.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ENV_SKILLS_DIR = "CYCLING_MCP_SKILLS_DIR"

_KEY_LINE = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$")
_BLOCK_SCALARS = {">", ">-", ">+", "|", "|-", "|+"}


@dataclass(frozen=True)
class Skill:
    """One bundled skill: its frontmatter identity plus the instruction body."""

    name: str
    description: str
    body: str


def _skills_dir() -> Path:
    """Where the bundled skills live.

    Checked in order: an explicit override, the copy packaged into the wheel,
    then the repository layout (so a clone or an editable install works).
    """
    override = os.environ.get(ENV_SKILLS_DIR, "").strip()
    if override:
        return Path(override).expanduser()

    packaged = Path(__file__).parent / "_skills"
    if packaged.is_dir():
        return packaged

    return Path(__file__).resolve().parents[2] / ".claude" / "skills"


def parse_frontmatter(block: str) -> dict[str, str]:
    """Read the `key: value` pairs from a YAML frontmatter block.

    Deliberately minimal rather than a YAML dependency, but it does handle the
    folded and literal block scalars (`>-`, `|`) that a long skill description
    needs — a parser that only took the rest of the line would read `>-` as the
    description and advertise the skill with nonsense.
    """
    values: dict[str, str] = {}
    lines = block.split("\n")
    index = 0

    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.startswith(("#", " ", "\t", "-")):
            continue

        match = _KEY_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()

        if value in _BLOCK_SCALARS:
            folded = value.startswith(">")
            collected: list[str] = []
            while index < len(lines):
                nxt = lines[index]
                if nxt.strip() and not nxt.startswith((" ", "\t")):
                    break
                collected.append(nxt.strip())
                index += 1
            while collected and not collected[-1]:
                collected.pop()
            joined = " ".join(c for c in collected if c) if folded else "\n".join(collected)
            values[key] = joined.strip()
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value

    return values


def parse_skill(text: str) -> Skill | None:
    """Parse one SKILL.md. None when it lacks a usable name, description or body."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None

    fields = parse_frontmatter("\n".join(lines[1:end]))
    body = "\n".join(lines[end + 1 :]).strip()
    name, description = fields.get("name"), fields.get("description")
    if not name or not description or not body:
        return None
    return Skill(name=name, description=description, body=body)


def load_skills(directory: Path | None = None) -> list[Skill]:
    """Load every bundled skill, sorted by name for a stable prompt list.

    Returns an empty list (warning on stderr) when the directory is missing, so
    a stripped-down install still starts and serves its tools.
    """
    root = directory or _skills_dir()
    try:
        entries = sorted(entry for entry in root.iterdir() if entry.is_dir())
    except OSError as exc:
        print(f"[claude-cycling-mcp] skills not loaded from {root}: {exc}", file=sys.stderr)
        return []

    skills: list[Skill] = []
    for entry in entries:
        path = entry / "SKILL.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue  # a directory without a SKILL.md simply isn't a skill
        skill = parse_skill(text)
        if skill is None:
            print(
                f"[claude-cycling-mcp] skipped {path}: missing name/description frontmatter",
                file=sys.stderr,
            )
            continue
        skills.append(skill)
    return skills


def build_skill_message(skill: Skill, session: str | None = None) -> str:
    """Frame a skill body as an instruction being received, not a document being read.

    A prompt arrives as an ordinary user message, so without this framing the
    model can mistake a procedure for reference material and summarise it
    instead of following it.
    """
    if session and session.strip():
        scope = f"The session to build is: {session.strip()}"
    else:
        scope = (
            "Ask what session to build if that is not already clear from the "
            "conversation, and confirm the FTP before rendering."
        )
    return (
        f'Follow the "{skill.name}" procedure below, using this server\'s tools.\n'
        f"{scope}\n\n---\n\n{skill.body}"
    )

"""The bundled skills, and the frontmatter parsing that advertises them."""

from pathlib import Path

import pytest

from cycling_mcp.skills import build_skill_message, load_skills, parse_frontmatter, parse_skill

SKILLS_DIR = Path(__file__).resolve().parents[1] / ".claude" / "skills"


UPLOAD_SKILLS = ("garmin-upload", "mywhoosh-upload")


def test_every_bundled_skill_loads():
    names = [s.name for s in load_skills(SKILLS_DIR)]
    assert names == ["coaching", "garmin-upload", "mywhoosh-upload"]


def test_frontmatter_name_matches_the_directory():
    """Claude Code matches skills by directory; the prompt uses the slug."""
    for skill in load_skills(SKILLS_DIR):
        assert (SKILLS_DIR / skill.name / "SKILL.md").exists()


def test_descriptions_survive_the_folded_block_scalar():
    """`description: >-` spans lines.

    A parser that took the rest of the key line would advertise every skill as
    ">-", so both would be invisible to the model.
    """
    for skill in load_skills(SKILLS_DIR):
        assert not skill.description.startswith(">")
        assert len(skill.description) > 100
        assert "\n" not in skill.description


def test_upload_descriptions_trigger_beyond_the_word_upload():
    """They must fire on how someone actually describes wanting a session."""
    for skill in load_skills(SKILLS_DIR):
        if skill.name not in UPLOAD_SKILLS:
            continue
        lowered = skill.description.lower()
        assert "create" in lowered
        assert "add" in lowered
        assert "send" in lowered


def test_the_coaching_description_triggers_on_talking_about_training():
    """Nobody asks their coach to "invoke the coaching skill".

    The phrasings below are how the request actually arrives, and each one has
    to be recognisable in the description or the skill never fires.
    """
    skill = next(s for s in load_skills(SKILLS_DIR) if s.name == "coaching")
    lowered = skill.description.lower()
    for phrasing in ("this week", "missed", "form", "fatigue", "ftp", "race"):
        assert phrasing in lowered, phrasing


def test_bodies_are_the_instructions_not_the_frontmatter():
    for skill in load_skills(SKILLS_DIR):
        assert skill.body.startswith("#")
        assert "description:" not in skill.body.split("\n")[0]
        assert len(skill.body) > 1000


# --------------------------------------------------------------------------
# frontmatter parsing
# --------------------------------------------------------------------------


def test_folded_scalar_joins_lines_with_spaces():
    fields = parse_frontmatter("name: demo\ndescription: >-\n  one two\n  three four")
    assert fields == {"name": "demo", "description": "one two three four"}


def test_literal_scalar_keeps_newlines():
    fields = parse_frontmatter("description: |\n  line one\n  line two")
    assert fields["description"] == "line one\nline two"


def test_plain_scalar_and_quotes():
    fields = parse_frontmatter('name: demo\ntitle: "quoted: value"')
    assert fields["name"] == "demo"
    assert fields["title"] == "quoted: value"


def test_colon_in_a_description_is_not_a_key_split():
    fields = parse_frontmatter("description: Use this: it works")
    assert fields["description"] == "Use this: it works"


def test_skill_without_frontmatter_is_rejected():
    assert parse_skill("# Just a document\n\nNo frontmatter here.") is None


def test_skill_without_a_body_is_rejected():
    assert parse_skill("---\nname: x\ndescription: y\n---\n") is None


def test_skill_missing_description_is_rejected():
    assert parse_skill("---\nname: x\n---\n\n# Body\n\ntext") is None


def test_unterminated_frontmatter_is_rejected():
    assert parse_skill("---\nname: x\ndescription: y\n\n# Body") is None


def test_missing_directory_returns_empty_rather_than_raising(tmp_path):
    """A stripped install must still start and serve its tools."""
    assert load_skills(tmp_path / "nope") == []


def test_directory_without_a_skill_md_is_skipped(tmp_path):
    (tmp_path / "not-a-skill").mkdir()
    assert load_skills(tmp_path) == []


# --------------------------------------------------------------------------
# prompt framing
# --------------------------------------------------------------------------


@pytest.fixture
def skill():
    """An upload skill specifically — the prompt framing differs per skill."""
    return next(s for s in load_skills(SKILLS_DIR) if s.name == "garmin-upload")


def test_message_frames_the_body_as_an_instruction(skill):
    message = build_skill_message(skill)
    assert message.startswith(f'Follow the "{skill.name}" procedure below')
    assert skill.body in message


def test_message_carries_the_session_when_given(skill):
    message = build_skill_message(skill, "3x10 sweet spot at 232 W")
    assert "The session to build is: 3x10 sweet spot at 232 W" in message


def test_message_asks_when_no_session_given(skill):
    message = build_skill_message(skill, "   ")
    assert "Ask what session to build" in message
    assert "confirm the FTP" in message


def test_packaged_skills_are_found_without_the_repo(tmp_path, monkeypatch):
    """The wheel ships skills at cycling_mcp/_skills; the loader must find them."""
    import cycling_mcp.skills as module

    packaged = tmp_path / "_skills" / "demo"
    packaged.mkdir(parents=True)
    (packaged / "SKILL.md").write_text(
        "---\nname: demo\ndescription: A demo skill for testing.\n---\n\n# Demo\n\nDo the thing.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "__file__", str(tmp_path / "skills.py"))
    monkeypatch.delenv(module.ENV_SKILLS_DIR, raising=False)
    assert [s.name for s in load_skills()] == ["demo"]


def test_env_override_wins(tmp_path, monkeypatch):
    custom = tmp_path / "custom" / "override"
    custom.mkdir(parents=True)
    (custom / "SKILL.md").write_text(
        "---\nname: override\ndescription: Overridden skill.\n---\n\n# Override\n\nBody.\n",
        encoding="utf-8",
    )
    from cycling_mcp.skills import ENV_SKILLS_DIR

    monkeypatch.setenv(ENV_SKILLS_DIR, str(tmp_path / "custom"))
    assert [s.name for s in load_skills()] == ["override"]


# --------------------------------------------------------------------------
# get_skill — how a model retrieves a procedure it was asked for by name
# --------------------------------------------------------------------------


def test_get_skill_lists_when_no_name_given():
    import json as _json

    from cycling_mcp.server import get_skill

    result = _json.loads(get_skill())
    assert result["ok"] is True
    assert sorted(s["name"] for s in result["skills"]) == [
        "coaching",
        "garmin-upload",
        "mywhoosh-upload",
    ]


def test_get_skill_returns_the_full_procedure():
    import json as _json

    from cycling_mcp.server import get_skill

    result = _json.loads(get_skill("mywhoosh-upload"))
    assert result["ok"] is True
    assert result["name"] == "mywhoosh-upload"
    # The credit-spending gate must survive into what the model actually reads.
    assert "EXPORT TO MYWHOOSH" in result["instructions"]
    assert "slot credit" in result["instructions"]
    assert len(result["instructions"]) > 5000


def test_get_skill_is_case_insensitive_and_forgiving_of_spacing():
    import json as _json

    from cycling_mcp.server import get_skill

    assert _json.loads(get_skill("  MyWhoosh-Upload "))["name"] == "mywhoosh-upload"


def test_unknown_skill_says_what_is_available():
    import json as _json

    from cycling_mcp.server import get_skill

    result = _json.loads(get_skill("nope"))
    assert result["ok"] is False
    assert "mywhoosh-upload" in result["available"]


def test_the_coaching_skill_keeps_the_rules_that_are_the_point():
    """The distilled coaching rules are why this skill exists.

    A rewrite that loses them leaves a skill that reads well and coaches
    nothing, and nothing else in the repo would notice.
    """
    body = next(s for s in load_skills(SKILLS_DIR) if s.name == "coaching").body
    for rule in (
        "A missed session is lost",
        "Quality goes at the end of a long ride",
        "Check the FTP before you emit a single target",
        "Always propose. Never push without explicit agreement",
        "Stop producing weekly plans",
    ):
        assert rule in body, rule


def test_a_skill_can_say_what_its_prompt_argument_means():
    """The defaults are written for the upload skills.

    A coaching prompt framed as "ask what session to build" opens in the wrong
    place — the first move there is to read the athlete's file, not to design a
    workout.
    """
    skills = {skill.name: skill for skill in load_skills(SKILLS_DIR)}
    coaching = skills["coaching"]
    assert "get_profile" in build_skill_message(coaching)
    assert "what session to build" not in build_skill_message(coaching)
    assert "What the athlete asked: I could not ride Tuesday" in build_skill_message(
        coaching, "I could not ride Tuesday"
    )


def test_a_skill_without_the_override_keeps_the_upload_framing():
    skills = {skill.name: skill for skill in load_skills(SKILLS_DIR)}
    message = build_skill_message(skills["garmin-upload"])
    assert "Ask what session to build" in message
    assert "confirm the FTP" in message

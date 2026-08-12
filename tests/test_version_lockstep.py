"""The version lives in four places; a partial bump must fail here, not at publish.

`publish.yml` refuses to publish when the git tag disagrees with `pyproject.toml`.
That check only covers one of the four, so this test ties the other three to it —
making a half-finished bump a normal test failure instead of a broken release.
"""

import json
import re
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    # 3.10 has no stdlib tomllib. `tomli` is a declared 3.10-only dev dependency,
    # so its absence is a broken environment rather than an expected condition —
    # fail loudly here instead of skipping and quietly losing the check.
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_version_is_semver(version):
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version


def test_package_dunder_version_matches(version):
    text = (ROOT / "src" / "cycling_mcp" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    assert match, "__version__ not found in src/cycling_mcp/__init__.py"
    assert match.group(1) == version


def test_plugin_manifest_matches(version):
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == version


def test_marketplace_entry_matches(version):
    marketplace = json.loads(
        (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    entries = [p for p in marketplace["plugins"] if p["name"] == "claude-cycling-mcp"]
    assert entries, "claude-cycling-mcp is missing from marketplace.json"
    assert entries[0]["version"] == version


def test_changelog_documents_this_version(version):
    """A release with no changelog entry is a release nobody can read."""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{version}]" in changelog, f"CHANGELOG.md has no '## [{version}]' section"


def test_mcpb_manifest_is_valid_json_and_named_consistently():
    """The .mcpb version is synced to the git tag by CI, so it is not lock-stepped here."""
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == plugin["name"]
    assert manifest["server"]["type"] == "python"


def test_plugin_skills_path_resolves():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    skills = (ROOT / plugin["skills"]).resolve()
    assert skills.is_dir(), skills
    found = sorted(p.name for p in skills.iterdir() if p.is_dir())
    assert found == ["garmin-upload", "mywhoosh-upload"]

"""Drive the server the way a client does: over stdio, through the schema.

These exist because two bugs shipped past a green in-process suite. Both were
type mismatches that only appear once arguments have been through JSON and
pydantic coercion — a digest that rejected every payload because 227.0 came
back as 227, and a snapshot check that reported "changed" because one side was
the string "72" and the other the float 72.0. Calling the Python function
directly cannot reproduce either: the coercion is the bug.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "golden"


class Client:
    """A minimal MCP stdio client — enough to call tools and read results."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "from cycling_mcp.server import main; main()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=ROOT,
        )
        self._id = 0
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            }
        )
        self._read()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _next(self) -> int:
        self._id += 1
        return self._id

    def _send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def _read(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError("server closed the connection")
        return json.loads(line)

    def call(self, tool: str, **arguments):
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next(),
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        result = self._read()["result"]
        text = "".join(block.get("text", "") for block in result["content"])
        if result.get("isError"):
            return {"_error": text}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def close(self) -> None:
        self.proc.kill()


@pytest.fixture(scope="module")
def client():
    c = Client()
    yield c
    c.close()


@pytest.fixture(scope="module")
def spec():
    path = ROOT / "tests" / "golden" / "sweetspot-3x10.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_snapshot_check_refuses_an_unchanged_header(client, spec):
    """The bug this file exists for.

    A scraped Training Load arrives as text; `training_load` is typed numeric
    and `before_training_load` was typed string, so the two sides of the same
    number were compared as "72" against "72.0" and reported as a change. The
    tool then said safe_to_export on the exact silent no-op it guards against —
    and a caller who followed the skill and captured a snapshot was worse off
    than one who skipped it, since omitting it at least produced a warning.
    """
    result = client.call(
        "verify_mywhoosh_import",
        spec=spec,
        workout_time="1:10:00",
        training_load=70,
        before_workout_time="1:10:00",
        before_training_load="70",
    )
    assert result["safe_to_export"] is False, result
    assert any("import did nothing" in p for p in result["problems"])


@pytest.mark.parametrize(
    "before_load,after_load",
    [("70", 70), (70, "70"), (70, 70), ("70", "70"), (70.0, "70"), ("70.0", 70)],
)
def test_the_snapshot_check_survives_every_number_shape(client, spec, before_load, after_load):
    """Whichever side arrives as text, the same reading is the same reading."""
    result = client.call(
        "verify_mywhoosh_import",
        spec=spec,
        workout_time="1:10:00",
        training_load=after_load,
        before_workout_time="1:10:00",
        before_training_load=before_load,
    )
    assert result["safe_to_export"] is False, (before_load, after_load, result)


def test_a_genuinely_changed_header_still_passes(client, spec):
    result = client.call(
        "verify_mywhoosh_import",
        spec=spec,
        workout_time="1:10:00",
        training_load=70,
        before_workout_time="45:00",
        before_training_load="40",
    )
    assert result["safe_to_export"] is True, result


def test_the_check_shows_what_it_compared(client, spec):
    """A bare boolean is not evidence the comparison happened. This one was
    wrong for a week while looking exactly like a passing check."""
    result = client.call(
        "verify_mywhoosh_import",
        spec=spec,
        workout_time="1:10:00",
        training_load=70,
        before_workout_time="45:00",
        before_training_load="40",
    )
    check = next(c for c in result["checks"] if c["check"] == "changed_from_snapshot")
    assert check["before"] == {"workout_time": "45:00", "training_load": "40"}
    assert check["after"] == {"workout_time": "1:10:00", "training_load": 70}


def test_a_retyped_payload_passes_the_digest(client, spec):
    """227.0 rendered, 227 retyped — the same payload, and it must digest so."""
    rendered = client.call("render_garmin", spec=spec)
    retyped = json.loads(json.dumps(rendered["payload"]))
    checked = client.call(
        "check_garmin_payload", payload=retyped, expected_digest=rendered["payload_digest"]
    )
    assert checked["matches_rendered"] is True, checked


def test_every_tool_answers_over_stdio(client, spec):
    """No tool here should ever be slow or silent; three sessions reported
    otherwise and every time the request had not reached the server."""
    assert client.call("server_info")["version"]
    assert client.call("validate_spec", spec=spec)["valid"] is True
    assert "Sweet Spot" in client.call("describe_spec", spec=spec)
    assert client.call("render_zwo", spec=spec)["ok"] is True
    assert client.call("render_garmin", spec=spec)["ok"] is True
    assert client.call("spec_schema")["schema"]
    assert client.call("get_skill", name="mywhoosh-upload")


def test_a_missing_snapshot_blocks_over_stdio(client, spec):
    """Two checks that both pass on a no-op are not evidence of success."""
    result = client.call(
        "verify_mywhoosh_import", spec=spec, workout_time="1:10:00", training_load=70
    )
    assert result["safe_to_export"] is False
    assert any("did not run" in p for p in result["problems"])


def test_the_library_card_that_actually_landed(client, spec):
    """The real card from the 2026-08-21 export, in the card's own formats.

    "1h 18m" not "78:00", and the name rendered with U+00D7 while the ASCII
    form was uploaded.
    """
    result = client.call(
        "verify_mywhoosh_library_entry",
        spec={**spec, "filename": "Tempo-3x14"},
        name="Tempo-3×14",
        duration="1h 10m",
        tss=70,
        intensity_factor=0.77,
    )
    assert result["landed"] is True, result
    assert result["problems"] == []


def test_a_library_card_for_a_different_workout_is_caught(client, spec):
    result = client.call(
        "verify_mywhoosh_library_entry",
        spec={**spec, "filename": "Tempo-3x14"},
        name="Sweet-Spot-3x11",
        duration="1h 08m",
    )
    assert result["landed"] is False
    assert any("different workout" in p for p in result["problems"])

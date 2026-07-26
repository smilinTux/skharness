"""ClaudeCodeHarness read-only session plane, over a FAKE tmux runner.

No real tmux, no real claude: the injectable argv runner returns canned output
and a tmp historical dir stands in for ~/.skcapstone/agents/<agent>/sessions/.
"""
import json

import pytest

from skharness.events import EventType
from skharness.harness import Harness, SessionDescriptor
from skharness.harnesses.claude_code import (
    ClaudeCodeHarness,
    new_lines,
    parse_windows,
    scan_historical,
)


def test_parse_windows_skips_monitor_and_blanks():
    out = "monitor\t1700000000\nlumina-abc12345\t1700000100\nopus-deadbeef\t1700000200\n\n"
    got = parse_windows(out, host=".158")
    ids = [s.sid for s in got]
    assert ids == ["lumina-abc12345", "opus-deadbeef"]
    assert got[0].harness == "claude-code"
    assert got[0].host == ".158"
    assert got[0].state == "running"
    assert got[0].last_activity == 1700000100.0
    assert isinstance(got[0], SessionDescriptor)


def test_parse_windows_handles_missing_activity_field():
    got = parse_windows("lumina-abc12345\n", host=".158")
    assert got[0].sid == "lumina-abc12345"
    assert got[0].last_activity == 0.0


def test_new_lines_returns_only_the_tail():
    assert new_lines("a\nb\n", "a\nb\nc\nd\n") == ["c", "d"]
    assert new_lines("", "x\ny\n") == ["x", "y"]
    assert new_lines("a\nb\n", "a\nb\n") == []


def test_scan_historical_reads_agent_session_dirs(tmp_path):
    root = tmp_path / "agents"
    sdir = root / "lumina" / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "sess-001.json").write_text(json.dumps({"id": "sess-001"}))
    (sdir / "sess-002.json").write_text(json.dumps({"id": "sess-002"}))
    got = scan_historical(root, host=".158")
    assert {s.sid for s in got} == {"lumina/sess-001", "lumina/sess-002"}
    assert all(s.state == "ended" for s in got)
    assert all(s.harness == "claude-code" for s in got)


def test_scan_historical_missing_root_is_empty(tmp_path):
    assert scan_historical(tmp_path / "nope", host=".158") == []


def test_is_a_harness_and_declares_pty_session_plane():
    h = ClaudeCodeHarness(runner=lambda argv: "")
    assert isinstance(h, Harness)
    assert h.name == "claude-code"
    caps = h.capabilities()
    assert caps["session_plane"] is True
    assert caps["headless_api"] == "pty"
    assert caps["task_plane"] is False
    assert caps["hot_set_model"] is False


@pytest.mark.asyncio
async def test_list_sessions_merges_live_then_historical(tmp_path):
    root = tmp_path / "agents"
    sdir = root / "lumina" / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "old.json").write_text("{}")

    def runner(argv):
        assert argv[0] == "tmux"
        assert "list-windows" in argv
        return "monitor\t1\nlumina-abc12345\t1700000100\n"

    h = ClaudeCodeHarness(runner=runner, sessions_root=root, host=".158")
    got = await h.list_sessions()
    ids = [s.sid for s in got]
    assert ids[0] == "lumina-abc12345"          # live first
    assert "lumina/old" in ids                   # historical after
    assert got[0].state == "running"


@pytest.mark.asyncio
async def test_stream_emits_status_then_new_capture_lines(tmp_path):
    captures = iter([
        "line1\nline2\n",          # first poll
        "line1\nline2\nline3\n",   # second poll adds line3
    ])

    def runner(argv):
        if "capture-pane" in argv:
            return next(captures)
        return ""

    h = ClaudeCodeHarness(runner=runner, sessions_root=tmp_path, host=".158",
                          poll_interval=0.0, max_polls=2)
    out = [e async for e in h.stream("lumina-abc12345")]
    assert out[0].type == EventType.STATUS
    texts = [e.text for e in out if e.type == EventType.ASSISTANT_TEXT]
    assert texts == ["line1", "line2", "line3"]


@pytest.mark.asyncio
async def test_stream_rejects_bad_sid_charset(tmp_path):
    h = ClaudeCodeHarness(runner=lambda argv: "", sessions_root=tmp_path,
                          max_polls=1)
    out = [e async for e in h.stream("bad;name$(x)")]
    assert len(out) == 1
    assert out[0].type == EventType.STATUS
    assert "invalid" in out[0].text.lower()

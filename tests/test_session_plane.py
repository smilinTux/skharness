"""SessionDescriptor (the one record) + FakeHarness session-plane double.

Pins the skcode P0 read-only session plane onto the unified Harness contract:
SessionDescriptor carries the fields the skcode ADR names, FakeHarness is the CI
double the daemon tests drive, and the read-only subset (list_sessions + stream)
works without any real tmux.
"""
import pytest

from skharness.events import EventType, SessionEvent
from skharness.harness import FakeHarness, Harness, SessionDescriptor


def test_session_descriptor_fields_and_defaults():
    sd = SessionDescriptor(sid="lumina-abc12345", host=".158", harness="claude-code",
                           repo="skharness", branch="main", model="ornith-tiny",
                           state="running", last_activity=42.0,
                           last_message="on it", quality="sandbox")
    assert sd.sid == "lumina-abc12345"
    assert sd.host == ".158"
    assert sd.harness == "claude-code"
    assert sd.state == "running"
    assert sd.quality == "sandbox"


def test_session_descriptor_defaults_allow_spawn_shape():
    # All fields default, so the same record serves a spawn request (no sid yet).
    sd = SessionDescriptor(host=".158", harness="claude-code", repo="skharness")
    assert sd.sid == ""
    assert sd.state == "running"
    assert sd.last_activity == 0.0
    assert sd.quality == "sandbox"


def test_session_descriptor_to_dict_and_roundtrip():
    sd = SessionDescriptor(sid="s1", host=".158", harness="fake", repo="r",
                           branch="b", model="m", state="ended", last_activity=1.0,
                           last_message="done", quality="full")
    d = sd.to_dict()
    assert d["sid"] == "s1"
    assert d["state"] == "ended"
    assert SessionDescriptor.from_dict(d) == sd


def test_session_descriptor_from_dict_ignores_unknown_keys():
    sd = SessionDescriptor.from_dict({"sid": "s1", "extra": "nope"})
    assert sd.sid == "s1"


def test_fake_is_a_harness_and_declares_session_plane():
    fh = FakeHarness()
    assert isinstance(fh, Harness)
    assert fh.name == "fake"
    caps = fh.capabilities()
    assert caps["session_plane"] is True
    assert caps["task_plane"] is False


def test_fake_has_no_write_wiring_of_its_own():
    # Read-only P0: the fake does not override any write verb of the plane.
    fh = FakeHarness()
    for verb in ("spawn", "inject", "set_model", "archive"):
        # the base gated-raise default is inherited, not a fake write path
        assert getattr(type(fh), verb) is getattr(Harness, verb)


@pytest.mark.asyncio
async def test_fake_list_sessions_returns_seeded():
    seeded = [SessionDescriptor(sid="s1", host=".158", harness="fake", repo="r")]
    fh = FakeHarness(sessions=seeded)
    got = await fh.list_sessions()
    assert [s.sid for s in got] == ["s1"]


@pytest.mark.asyncio
async def test_fake_stream_yields_seeded_events_in_order():
    evs = [SessionEvent(type=EventType.STATUS, text="attached"),
           SessionEvent(type=EventType.ASSISTANT_TEXT, text="hello")]
    fh = FakeHarness(
        sessions=[SessionDescriptor(sid="s1", host=".158", harness="fake", repo="r")],
        events={"s1": evs},
    )
    out = [e async for e in fh.stream("s1")]
    assert [e.text for e in out] == ["attached", "hello"]


@pytest.mark.asyncio
async def test_fake_stream_unknown_sid_yields_nothing():
    fh = FakeHarness()
    out = [e async for e in fh.stream("nope")]
    assert out == []

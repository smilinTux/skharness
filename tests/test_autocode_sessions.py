"""AutocodeSessionRegistry: autocode runs register as skcode sessions
(card C-1 AC3, spec 2026-08-11 section 5.1).
"""
from __future__ import annotations

from skharness.autocode.sessions import AutocodeSessionRegistry


def test_register_creates_a_running_autocode_session(tmp_path):
    reg = AutocodeSessionRegistry(root=tmp_path)
    desc = reg.register(sid="autocode-r1-t1", repo="skharness", host=".158")
    assert desc.sid == "autocode-r1-t1"
    assert desc.harness == "autocode"
    assert desc.source == "autocode"
    assert desc.state == "running"
    assert desc.repo == "skharness"


def test_register_persists_and_get_roundtrips(tmp_path):
    reg = AutocodeSessionRegistry(root=tmp_path)
    reg.register(sid="autocode-r1-t1", repo="skharness")
    got = reg.get("autocode-r1-t1")
    assert got is not None
    assert got.sid == "autocode-r1-t1"
    assert got.source == "autocode"


def test_get_unknown_sid_returns_none(tmp_path):
    reg = AutocodeSessionRegistry(root=tmp_path)
    assert reg.get("nope") is None


def test_update_changes_state_and_message(tmp_path):
    reg = AutocodeSessionRegistry(root=tmp_path)
    reg.register(sid="s1")
    updated = reg.update("s1", state="finalized", last_message="PR opened")
    assert updated.state == "finalized"
    assert updated.last_message == "PR opened"
    assert reg.get("s1").state == "finalized"


def test_update_unknown_sid_is_a_clean_noop(tmp_path):
    reg = AutocodeSessionRegistry(root=tmp_path)
    assert reg.update("nope", state="finalized") is None


def test_end_marks_ended_without_deleting(tmp_path):
    reg = AutocodeSessionRegistry(root=tmp_path)
    reg.register(sid="s1")
    reg.end("s1", last_message="escalated")
    got = reg.get("s1")
    assert got.state == "ended"
    assert got.last_message == "escalated"


def test_list_returns_all_registered_sessions(tmp_path):
    reg = AutocodeSessionRegistry(root=tmp_path)
    reg.register(sid="s1", repo="a")
    reg.register(sid="s2", repo="b")
    sids = {s.sid for s in reg.list()}
    assert sids == {"s1", "s2"}
    assert all(s.source == "autocode" for s in reg.list())


def test_list_empty_root_returns_empty(tmp_path):
    reg = AutocodeSessionRegistry(root=tmp_path / "does-not-exist")
    assert reg.list() == []


def test_session_and_event_files_coexist_under_the_same_sid_dir(tmp_path):
    """The event archive (events.jsonl) and the session descriptor
    (session.json) share the same per-sid directory; neither clobbers the
    other."""
    from skharness.events import EventType, SessionEvent
    from skharness.session_events import SessionEventStore

    reg = AutocodeSessionRegistry(root=tmp_path)
    store = SessionEventStore(root=tmp_path)
    reg.register(sid="s1", repo="skharness")
    store.append("s1", SessionEvent(type=EventType.STATUS, text="started"),
                source="autocode")
    assert (tmp_path / "s1" / "session.json").is_file()
    assert (tmp_path / "s1" / "events.jsonl").is_file()
    assert reg.get("s1").source == "autocode"

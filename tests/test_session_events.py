"""SessionEventStore: append-time seq assignment + capped JSONL archive
(skcode Code-section card C-1, spec 2026-08-11 section 5.3).
"""
from __future__ import annotations

import json

from skharness.events import EventType, SessionEvent
from skharness.session_events import SessionEventStore


def _ev(text="x"):
    return SessionEvent(type=EventType.ASSISTANT_TEXT, text=text, ts=1.0)


def test_append_assigns_monotonic_seq_per_session(tmp_path):
    store = SessionEventStore(root=tmp_path)
    a = store.append("s1", _ev("a"))
    b = store.append("s1", _ev("b"))
    c = store.append("s1", _ev("c"))
    assert [a.seq, b.seq, c.seq] == [1, 2, 3]


def test_append_seq_counters_are_independent_per_session(tmp_path):
    store = SessionEventStore(root=tmp_path)
    store.append("s1", _ev())
    a2 = store.append("s1", _ev())
    b1 = store.append("s2", _ev())
    assert a2.seq == 2
    assert b1.seq == 1


def test_append_stamps_sid_and_source(tmp_path):
    store = SessionEventStore(root=tmp_path)
    ev = store.append("s1", _ev(), source="autocode")
    assert ev.sid == "s1"
    assert ev.source == "autocode"


def test_append_does_not_mutate_the_caller_supplied_event(tmp_path):
    store = SessionEventStore(root=tmp_path)
    original = _ev("keep me")
    store.append("s1", original, source="autocode")
    assert original.seq == 0
    assert original.sid == ""
    assert original.source == "interactive"


def test_seq_resets_when_the_daemon_restarts(tmp_path):
    """The explicit, load-bearing behavior: seq is IN-MEMORY ONLY and resets to
    1 on a fresh process, even though the prior process's events (seq 1..3)
    are still sitting on disk under the same sid. This is not a bug to hide:
    it is why the client's dedup/scroll-anchor key is (sid, seq, ts), never
    seq alone (spec 5.1)."""
    first_process = SessionEventStore(root=tmp_path)
    first_process.append("s1", _ev("a"))
    first_process.append("s1", _ev("b"))
    e3 = first_process.append("s1", _ev("c"))
    assert e3.seq == 3

    # A NEW SessionEventStore instance simulates the daemon restarting: a
    # fresh process, a fresh (empty) in-memory seq counter, over the SAME
    # persisted root/sid.
    second_process = SessionEventStore(root=tmp_path)
    e_after_restart = second_process.append("s1", _ev("d"))
    assert e_after_restart.seq == 1          # reset, not continued from 4

    # The persisted archive still holds all four rows (three pre-restart at
    # seq 1..3, one post-restart ALSO at seq 1): seq is only unique within one
    # process's lifetime for one sid, which is exactly the trap (sid, seq, ts)
    # dedup exists to handle.
    rows = second_process._read_all("s1")
    assert [r["seq"] for r in rows] == [1, 2, 3, 1]


def test_events_persist_to_the_per_session_jsonl_file(tmp_path):
    store = SessionEventStore(root=tmp_path)
    store.append("s1", _ev("hello"))
    path = tmp_path / "s1" / "events.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["text"] == "hello"
    assert row["seq"] == 1
    assert row["sid"] == "s1"


def test_persist_false_never_touches_disk(tmp_path):
    store = SessionEventStore(root=tmp_path, persist=False)
    ev = store.append("s1", _ev("hello"))
    assert ev.seq == 1                        # seq assignment still works
    assert not (tmp_path / "s1").exists()     # but nothing was written


def test_archive_is_size_capped(tmp_path):
    # A tiny cap so a handful of small events already forces a trim.
    store = SessionEventStore(root=tmp_path, max_bytes=300)
    for i in range(50):
        store.append("s1", _ev(f"event number {i}"))
    path = tmp_path / "s1" / "events.jsonl"
    assert path.stat().st_size <= 300
    # The newest events survive the trim; the oldest are evicted.
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[-1]["text"] == "event number 49"
    assert rows[0]["seq"] > 1                 # earliest rows were dropped


def test_read_page_defaults_to_newest_events_within_limit(tmp_path):
    store = SessionEventStore(root=tmp_path)
    for i in range(10):
        store.append("s1", _ev(str(i)))
    page = store.read_page("s1", limit=3)
    assert [r["seq"] for r in page] == [8, 9, 10]


def test_read_page_before_seq_pages_backward(tmp_path):
    store = SessionEventStore(root=tmp_path)
    for i in range(10):
        store.append("s1", _ev(str(i)))
    page = store.read_page("s1", before_seq=8, limit=3)
    assert [r["seq"] for r in page] == [5, 6, 7]


def test_read_page_unknown_sid_returns_empty(tmp_path):
    store = SessionEventStore(root=tmp_path)
    assert store.read_page("nope") == []


def test_read_page_ascending_order(tmp_path):
    store = SessionEventStore(root=tmp_path)
    for i in range(5):
        store.append("s1", _ev(str(i)))
    page = store.read_page("s1", limit=100)
    assert [r["seq"] for r in page] == [1, 2, 3, 4, 5]

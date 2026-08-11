from skharness.events import EventType, SessionEvent, SOURCE_AUTOCODE, SOURCE_INTERACTIVE


def test_event_defaults():
    e = SessionEvent(type=EventType.ASSISTANT_TEXT, text="hello")
    assert e.type == EventType.ASSISTANT_TEXT
    assert e.text == "hello"
    assert e.ts == 0.0
    assert e.data == {}
    # v2 fields default so a caller that never touches them (every existing
    # emitter) still gets a valid, additive shape.
    assert e.seq == 0
    assert e.sid == ""
    assert e.source == SOURCE_INTERACTIVE


def test_event_to_dict_serializes_enum_value():
    e = SessionEvent(type=EventType.NEEDS_INPUT, text="approve?", ts=12.5,
                     data={"kind": "tool"})
    d = e.to_dict()
    # v2: seq/sid/source are ADDITIVE on top of the original four keys. No
    # existing key (type/text/ts/data) is renamed or retyped; the old iframe
    # client ignores the three it does not recognize.
    assert d == {"type": "needs_input", "text": "approve?", "ts": 12.5,
                 "data": {"kind": "tool"}, "seq": 0, "sid": "", "source": "interactive"}


def test_event_to_dict_carries_v2_fields_when_set():
    e = SessionEvent(type=EventType.TOOL_CALL, text="Edit", seq=7, sid="s-1",
                     source=SOURCE_AUTOCODE)
    d = e.to_dict()
    assert d["seq"] == 7
    assert d["sid"] == "s-1"
    assert d["source"] == "autocode"
    # the four original fields are untouched in shape/name.
    assert set(d) == {"type", "text", "ts", "data", "seq", "sid", "source"}


def test_event_dict_roundtrip():
    e = SessionEvent(type=EventType.STATUS, text="attached", ts=1.0)
    assert SessionEvent.from_dict(e.to_dict()) == e


def test_event_dict_roundtrip_with_v2_fields():
    e = SessionEvent(type=EventType.STATUS, text="attached", ts=1.0,
                     seq=3, sid="s-42", source=SOURCE_AUTOCODE)
    assert SessionEvent.from_dict(e.to_dict()) == e


def test_event_from_dict_ignores_unknown_keys_still():
    # An old-shaped payload (no seq/sid/source) still parses, defaulting the
    # new fields, exactly as a new daemon reading an old persisted line would.
    e = SessionEvent.from_dict({"type": "status", "text": "x", "ts": 1.0,
                                "data": {}, "totally": "unknown"})
    assert e.seq == 0 and e.sid == "" and e.source == SOURCE_INTERACTIVE


def test_event_data_is_not_shared_between_instances():
    a = SessionEvent(type=EventType.STATUS)
    b = SessionEvent(type=EventType.STATUS)
    a.data["x"] = 1
    assert b.data == {}

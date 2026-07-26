from skharness.events import EventType, SessionEvent


def test_event_defaults():
    e = SessionEvent(type=EventType.ASSISTANT_TEXT, text="hello")
    assert e.type == EventType.ASSISTANT_TEXT
    assert e.text == "hello"
    assert e.ts == 0.0
    assert e.data == {}


def test_event_to_dict_serializes_enum_value():
    e = SessionEvent(type=EventType.NEEDS_INPUT, text="approve?", ts=12.5,
                     data={"kind": "tool"})
    d = e.to_dict()
    assert d == {"type": "needs_input", "text": "approve?", "ts": 12.5,
                 "data": {"kind": "tool"}}


def test_event_dict_roundtrip():
    e = SessionEvent(type=EventType.STATUS, text="attached", ts=1.0)
    assert SessionEvent.from_dict(e.to_dict()) == e


def test_event_data_is_not_shared_between_instances():
    a = SessionEvent(type=EventType.STATUS)
    b = SessionEvent(type=EventType.STATUS)
    a.data["x"] = 1
    assert b.data == {}

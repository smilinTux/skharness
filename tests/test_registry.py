from skharness.registry import SessionRegistry
from skharness.session import Session, SessionStatus


def test_add_get_list(tmp_path):
    r = SessionRegistry(path=tmp_path / "sessions.json")
    r.add(Session(id="s1", agent="lumina", repo="/r"))
    assert r.get("s1").agent == "lumina"
    assert len(r.live()) == 1


def test_end_removes_from_live(tmp_path):
    r = SessionRegistry(path=tmp_path / "sessions.json")
    r.add(Session(id="s1", agent="lumina", repo="/r"))
    r.set_status("s1", SessionStatus.ENDED)
    assert r.live() == []
    assert r.get("s1").status == SessionStatus.ENDED


def test_persists(tmp_path):
    p = tmp_path / "sessions.json"
    SessionRegistry(path=p).add(Session(id="s1", agent="a", repo="/r"))
    assert SessionRegistry(path=p).get("s1") is not None

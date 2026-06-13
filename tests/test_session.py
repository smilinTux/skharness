from skharness.session import Session, SessionStatus


def test_session_defaults():
    s = Session(id="s1", agent="lumina", repo="/repo")
    assert s.status == SessionStatus.SPAWNING
    assert s.worktree == ""
    assert s.tmux == ""
    assert s.web_url == ""


def test_session_dict_roundtrip():
    s = Session(id="s1", agent="lumina", repo="/repo", worktree="/wt",
                tmux="sk-s1", web_url="http://x", status=SessionStatus.RUNNING)
    assert Session.from_dict(s.to_dict()) == s

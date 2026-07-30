"""skcode-hostd read-only daemon: 3 data routes, capauth-gated, zero write surface.

Driven by FakeHarness + FastAPI TestClient (no real tmux, no real bind).
"""
import types as _t

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from skharness.daemon import build_daemon_app
from skharness.events import EventType, SessionEvent
from skharness.harness import FakeHarness, SessionDescriptor
from skharness.autocode.types import GateResult, RepoSpec


def _harness():
    sessions = [
        SessionDescriptor(sid="lumina-abc12345", host=".158", harness="fake",
                          repo="skharness", branch="main", model="ornith-tiny",
                          last_message="on it"),
    ]
    events = {
        "lumina-abc12345": [
            SessionEvent(type=EventType.STATUS, text="attached"),
            SessionEvent(type=EventType.ASSISTANT_TEXT, text="hello world"),
        ]
    }
    return FakeHarness(sessions=sessions, events=events)


def _client(verifier=lambda token: token == "good"):
    app = build_daemon_app(harness=_harness(), verify_caller=verifier)
    return TestClient(app)


def test_hosts_self_reports_identity():
    c = _client()
    r = c.get("/api/v1/hosts/self", headers={"authorization": "Bearer good"})
    assert r.status_code == 200
    assert r.json()["host"] == ".158"
    assert r.json()["ok"] is True


def test_list_sessions_returns_rows():
    c = _client()
    r = c.get("/api/v1/sessions", headers={"authorization": "Bearer good"})
    assert r.status_code == 200
    rows = r.json()["sessions"]
    assert rows[0]["sid"] == "lumina-abc12345"
    assert rows[0]["last_message"] == "on it"


def test_get_one_session_ok_and_404():
    c = _client()
    h = {"authorization": "Bearer good"}
    assert c.get("/api/v1/sessions/lumina-abc12345", headers=h).status_code == 200
    assert c.get("/api/v1/sessions/nope", headers=h).status_code == 404


def test_read_routes_require_auth():
    c = _client()
    assert c.get("/api/v1/sessions").status_code == 401
    assert c.get("/api/v1/sessions",
                 headers={"authorization": "Bearer bad"}).status_code == 403
    assert c.get("/api/v1/sessions/lumina-abc12345").status_code == 401


def test_no_write_surface_except_ratify():
    c = _client()
    h = {"authorization": "Bearer good"}
    # Ratify is the ONE allowed POST (grade-only, never merges). Route EXISTS, so
    # the method is not rejected: 501 here only because this bare client wires no
    # resolve_ratify. The key invariant: it is NOT 405 Method Not Allowed.
    r = c.post("/api/v1/sessions/lumina-abc12345/ratify", json={}, headers=h)
    assert r.status_code != 405
    assert r.status_code == 501
    # Every OTHER write must still be gone (no spawn/inject/kill/dispatch in P0).
    assert c.post("/api/v1/sessions", json={}, headers=h).status_code == 405
    assert c.delete("/api/v1/sessions/lumina-abc12345", headers=h).status_code == 405
    assert c.put("/api/v1/sessions/lumina-abc12345", json={}, headers=h).status_code == 405
    assert c.post("/api/v1/sessions/lumina-abc12345/inject", json={},
                  headers=h).status_code == 404
    assert c.post("/api/v1/dispatch", json={}, headers=h).status_code == 404
    assert c.delete("/api/v1/hosts/self", headers=h).status_code == 405


def test_ws_stream_delivers_events_with_token():
    c = _client()
    with c.websocket_connect(
        "/api/v1/sessions/lumina-abc12345/stream?token=good"
    ) as ws:
        first = ws.receive_json()
        second = ws.receive_json()
        assert first["type"] == "status"
        assert second["type"] == "assistant_text"
        assert second["text"] == "hello world"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()   # server closes after the stream ends


def test_ws_stream_rejects_missing_or_bad_token():
    c = _client()
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/v1/sessions/lumina-abc12345/stream"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(
            "/api/v1/sessions/lumina-abc12345/stream?token=bad"
        ):
            pass


def test_static_client_served_unauthenticated():
    c = _client()
    r = c.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_module_manifest_served_unauthenticated_with_operator_facet():
    # Public discovery metadata: no bearer required, and it carries both facets.
    c = _client()
    r = c.get("/.well-known/skworld-module.json")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "skcode"
    assert body["auth"]["audience"] == "skcode"
    assert body["operator"]["proposedStandardActions"] == [
        "restart-hostd",
        "archive-stale-session",
    ]
    # URLs resolve against the request origin (the TestClient base).
    assert body["entry"]["url"].endswith("/app")


# ---- POST /sessions/{sid}/ratify : grade-only, capauth-gated, never merges ---

class _GradingHarness(FakeHarness):
    """FakeHarness plus a task-plane grade(), so the daemon can drive real ratify."""

    def __init__(self, *, sessions, grade_result):
        super().__init__(sessions=sessions, events={})
        self._gr = grade_result

    def grade(self, brief):
        return self._gr


def _five_complete():
    return GateResult(score=5, passed=True,
                      notes="done <promise>COMPLETE</promise>", artifact="pr")


def _ratify_client(mocker, *, grade_result, ci_status="green", cov=0.95):
    """A daemon wired for ratify: gradable harness, a resolver, recorded audit +
    needs_input events, and the CI/coverage twins + git subprocess faked out."""
    sessions = [SessionDescriptor(sid="lumina-abc12345", host=".158",
                                  harness="fake", repo="skharness", branch="wt/x")]
    harness = _GradingHarness(sessions=sessions, grade_result=grade_result)
    repo = RepoSpec(name="skharness", path="/repos/skharness", base_branch="main",
                    integration_branch="develop", test_cmd="pytest", ci="none")

    calls = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return _t.SimpleNamespace(stdout="", stderr="", returncode=0)

    mocker.patch("skharness.autocode.engineering.subprocess.run", side_effect=fake_run)
    mocker.patch("skharness.autocode.ratify.subprocess.run", side_effect=fake_run)
    mocker.patch("skharness.autocode.ratify.external_ci_verdict", return_value=ci_status)
    mocker.patch("skharness.autocode.ratify.diff_coverage", return_value=cov)

    events, audits = [], []
    app = build_daemon_app(
        harness=harness, verify_caller=lambda t: t == "good",
        resolve_ratify=lambda s: (repo, f"/wt/{s.sid}", ["accept"]),
        emit_event=lambda sid, ev: events.append((sid, ev)),
        audit_log=audits.append)
    return TestClient(app), events, audits, calls


def test_ratify_requires_auth(mocker):
    c, _, _, _ = _ratify_client(mocker, grade_result=_five_complete())
    assert c.post("/api/v1/sessions/lumina-abc12345/ratify", json={}).status_code == 401
    assert c.post("/api/v1/sessions/lumina-abc12345/ratify", json={},
                  headers={"authorization": "Bearer bad"}).status_code == 403


def test_ratify_pass_returns_gateresult(mocker):
    c, events, audits, _ = _ratify_client(mocker, grade_result=_five_complete())
    r = c.post("/api/v1/sessions/lumina-abc12345/ratify", json={},
               headers={"authorization": "Bearer good"})
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True and body["score"] == 5
    assert "<promise>" not in body["notes"]        # promise stripped
    assert events == []                             # a pass needs no operator
    assert any("PASS" in a for a in audits)


def test_ratify_fail_emits_needs_input_and_audit(mocker):
    grade = GateResult(score=3, passed=False, notes="gaps remain", artifact=None)
    c, events, audits, _ = _ratify_client(mocker, grade_result=grade, ci_status="red")
    r = c.post("/api/v1/sessions/lumina-abc12345/ratify", json={},
               headers={"authorization": "Bearer good"})
    assert r.status_code == 200
    assert r.json()["passed"] is False
    assert len(events) == 1
    sid, ev = events[0]
    assert sid == "lumina-abc12345"
    assert ev.type == EventType.NEEDS_INPUT
    assert any("FAIL" in a for a in audits)


def test_ratify_never_merges(mocker):
    c, _, _, calls = _ratify_client(mocker, grade_result=_five_complete())
    c.post("/api/v1/sessions/lumina-abc12345/ratify", json={},
           headers={"authorization": "Bearer good"})
    joined = [" ".join(c) for c in calls]
    assert calls, "expected ratify to stage/diff via git"
    assert not any("merge" in c for c in joined)
    assert not any("commit" in c for c in joined)
    assert not any("push" in c for c in joined)


def test_ratify_unknown_session_404(mocker):
    c, _, _, _ = _ratify_client(mocker, grade_result=_five_complete())
    r = c.post("/api/v1/sessions/nope/ratify", json={},
               headers={"authorization": "Bearer good"})
    assert r.status_code == 404

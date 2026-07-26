"""skcode-hostd read-only daemon: 3 data routes, capauth-gated, zero write surface.

Driven by FakeHarness + FastAPI TestClient (no real tmux, no real bind).
"""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from skharness.daemon import build_daemon_app
from skharness.events import EventType, SessionEvent
from skharness.harness import FakeHarness, SessionDescriptor


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


def test_no_write_surface():
    c = _client()
    h = {"authorization": "Bearer good"}
    # These routes must not exist at all (no spawn/inject/kill/dispatch in P0).
    assert c.post("/api/v1/sessions", json={}, headers=h).status_code == 405
    assert c.delete("/api/v1/sessions/lumina-abc12345", headers=h).status_code == 405
    assert c.post("/api/v1/sessions/lumina-abc12345/inject", json={},
                  headers=h).status_code == 404
    assert c.post("/api/v1/dispatch", json={}, headers=h).status_code == 404


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

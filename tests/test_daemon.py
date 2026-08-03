"""skcode-hostd read-only daemon: 3 data routes, capauth-gated, zero write surface.

Driven by FakeHarness + FastAPI TestClient (no real tmux, no real bind).
"""
import types as _t

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from skharness.autocode.types import GateResult, RepoSpec
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


def test_write_surface_is_ratify_and_inject_only():
    c = _client()
    h = {"authorization": "Bearer good"}
    # Ratify is a grade-only POST (never merges). Route EXISTS, so the method is
    # not rejected: 501 here only because this bare client wires no resolve_ratify.
    # The key invariant: it is NOT 405 Method Not Allowed.
    r = c.post("/api/v1/sessions/lumina-abc12345/ratify", json={}, headers=h)
    assert r.status_code != 405
    assert r.status_code == 501
    # inject is now a capauth-gated WRITE surface (P1): the route EXISTS (so it is
    # NOT 404 and NOT 405) and fails closed without a valid token. Prove existence
    # + gating via the no-token path, which returns 401 BEFORE harness.inject runs.
    r = c.post("/api/v1/sessions/lumina-abc12345/inject", json={"text": "hi"})
    assert r.status_code == 401
    # No implicit session-collection write, no kill (DELETE), no replace (PUT).
    assert c.post("/api/v1/sessions", json={}, headers=h).status_code == 405
    assert c.delete("/api/v1/sessions/lumina-abc12345", headers=h).status_code == 405
    assert c.put("/api/v1/sessions/lumina-abc12345", json={}, headers=h).status_code == 405
    # Dispatch (P2 RCE surface) EXISTS now, but this bare client wires NO authz PDP
    # and NO audit sink, so it fails CLOSED (501, not 404/405): dispatch can never
    # actuate without both configured. The dedicated gate matrix is in test_dispatch.
    r = c.post("/api/v1/dispatch", json={}, headers=h)
    assert r.status_code not in (404, 405)
    assert r.status_code == 501
    assert c.delete("/api/v1/hosts/self", headers=h).status_code == 405


# ---- POST /sessions/{sid}/inject : session-plane write surface, capauth-gated -

class _RecordingInjectHarness(FakeHarness):
    """FakeHarness plus an inject() that records the call, so the daemon can drive
    the P1 write surface with no real tmux PTY. Honors the read-only-double
    invariant: FakeHarness itself still overrides NO write verb; the write path is
    proven here on a subclass (mirrors _GradingHarness adding grade())."""

    def __init__(self, *, sessions):
        super().__init__(sessions=sessions, events={})
        self.injected: list[tuple[str, str]] = []

    async def inject(self, sid, text):
        self.injected.append((sid, text))
        return {"sid": sid, "injected": True}


def _inject_harness():
    return _RecordingInjectHarness(sessions=[
        SessionDescriptor(sid="lumina-abc12345", host=".158", harness="fake"),
    ])


def test_inject_requires_auth_and_never_actuates_under_deny_all():
    # verify_caller=lambda t: False is the P0 deny-all verifier still in force:
    # every caller is denied, so the write surface is inert until R2.4.
    harness = _inject_harness()
    c = TestClient(build_daemon_app(harness=harness, verify_caller=lambda t: False))
    # no token -> 401 (before the verifier even runs)
    assert c.post("/api/v1/sessions/lumina-abc12345/inject",
                  json={"text": "hi"}).status_code == 401
    # a bad token -> 403 via the deny-all verifier
    assert c.post("/api/v1/sessions/lumina-abc12345/inject", json={"text": "hi"},
                  headers={"authorization": "Bearer bad"}).status_code == 403
    # nothing actuated: harness.inject was never reached
    assert harness.injected == []


def test_inject_calls_harness_with_body_text_when_authorized():
    harness = _inject_harness()
    c = TestClient(build_daemon_app(harness=harness,
                                    verify_caller=lambda t: t == "good"))
    r = c.post("/api/v1/sessions/lumina-abc12345/inject",
               json={"text": "run the tests"},
               headers={"authorization": "Bearer good"})
    assert r.status_code == 200
    assert r.json() == {"sid": "lumina-abc12345", "injected": True}
    assert harness.injected == [("lumina-abc12345", "run the tests")]


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


def test_app_serves_real_client_not_placeholder():
    # /app must serve the packaged read-only client, never the "not installed"
    # placeholder that build_daemon_app falls back to when client/index.html is
    # missing. This is what the SKWorld shell's Code tab embeds over the funnel.
    c = _client()
    r = c.get("/app")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    html = r.text
    assert "Read-only client not installed yet" not in html   # not the placeholder
    assert '<meta name="skcode-client" content="read-only">' in html
    assert "skcode" in html
    # It consumes the read routes (sessions list + WS tail).
    assert "/api/v1/sessions" in html
    assert "/stream" in html


def test_served_client_has_no_write_control_affordances():
    # The client exposes exactly TWO gated write affordances: the New-session
    # POST /api/v1/dispatch and the follow-up POST /api/v1/sessions/<sid>/inject.
    # Every VIEW path stays GET / read-only WS. This guards the "no OTHER RCE
    # surface" invariant against a future edit that quietly reintroduces a
    # ratify/kill/spawn control, a third POST, or a PUT/DELETE.
    c = _client()
    html = c.get("/app").text.lower()
    for token in ("ratify", "spawn", "kill",
                  "'post'", '"put"', "'put'", '"delete"', "'delete'",
                  "method: 'post'", "onclick="):
        assert token not in html, f"write-control affordance leaked into client: {token}"
    # Exactly two permitted writes (dispatch + inject); no third POST.
    assert html.count('method: "post"') == 2
    assert "/api/v1/dispatch" in html
    assert "/inject" in html


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


# ---- read/write SCOPE SPLIT (R2.4): view needs skcode.stream, actuate needs --
# ---- skcode.inject. Enabling the verifier with a read-only token grants view --
# ---- WITHOUT arming keystroke-inject. Driven with a scope-carrying AuthContext,
# ---- exactly what the real capauth verifier returns. --------------------------

from skharness.auth import AuthContext

_H = {"authorization": "Bearer tok"}


def _ctx_verifier(*scopes):
    """A real (scope-carrying) verifier: any token yields the given scopes.

    Mirrors build_capauth_verifier's return type (AuthContext), so the daemon's
    read/write scope split is exercised end-to-end at the gate."""
    ctx = AuthContext(scopes=frozenset(scopes))
    return lambda token: ctx


def test_scope_split_read_only_token_reads_but_cannot_inject():
    # A token that carries ONLY skcode.stream: full read view, zero write.
    read_only = _ctx_verifier("skcode.stream")

    reads = TestClient(build_daemon_app(harness=_harness(), verify_caller=read_only))
    assert reads.get("/api/v1/hosts/self", headers=_H).status_code == 200
    assert reads.get("/api/v1/sessions", headers=_H).status_code == 200
    assert reads.get("/api/v1/sessions/lumina-abc12345", headers=_H).status_code == 200

    # inject is 403 (insufficient scope) and NOTHING actuates.
    harness = _inject_harness()
    wc = TestClient(build_daemon_app(harness=harness, verify_caller=read_only))
    r = wc.post("/api/v1/sessions/lumina-abc12345/inject", json={"text": "hi"},
                headers=_H)
    assert r.status_code == 403
    assert harness.injected == []


def test_scope_split_read_only_token_streams_but_not_inject_ws():
    # WS stream is a READ route: a stream-scoped token opens it (delivers events),
    # while it still cannot ratify (a write-class action). Auth 403s BEFORE _ratify
    # runs, so the resolver/grade path is never reached (no subprocess to mock).
    read_only = _ctx_verifier("skcode.stream")
    c = TestClient(build_daemon_app(harness=_harness(), verify_caller=read_only))
    with c.websocket_connect(
        "/api/v1/sessions/lumina-abc12345/stream?token=tok"
    ) as ws:
        assert ws.receive_json()["type"] == "status"
        assert ws.receive_json()["type"] == "assistant_text"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()

    sessions = [SessionDescriptor(sid="lumina-abc12345", host=".158",
                                  harness="fake", repo="skharness", branch="wt/x")]
    app = build_daemon_app(
        harness=_GradingHarness(sessions=sessions, grade_result=_five_complete()),
        verify_caller=read_only,
        resolve_ratify=lambda s: (None, "", []))
    r = TestClient(app).post("/api/v1/sessions/lumina-abc12345/ratify", json={},
                             headers=_H)
    assert r.status_code == 403


def test_scope_split_write_token_injects_and_reads():
    # An operator token carries BOTH scopes (skcode.stream + skcode.inject), the
    # way a full skcode grant is minted: it can inject AND read.
    write = _ctx_verifier("skcode.stream", "skcode.inject")

    harness = _inject_harness()
    wc = TestClient(build_daemon_app(harness=harness, verify_caller=write))
    r = wc.post("/api/v1/sessions/lumina-abc12345/inject",
                json={"text": "run the tests"}, headers=_H)
    assert r.status_code == 200
    assert r.json() == {"sid": "lumina-abc12345", "injected": True}
    assert harness.injected == [("lumina-abc12345", "run the tests")]

    reads = TestClient(build_daemon_app(harness=_harness(), verify_caller=write))
    assert reads.get("/api/v1/sessions", headers=_H).status_code == 200
    assert reads.get("/api/v1/hosts/self", headers=_H).status_code == 200


def test_scope_split_deny_all_denies_read_and_write():
    # Flag off == deny-all verifier: it carries no scopes and returns False, so
    # EVERY route is 403 regardless of the scope the route asks for.
    deny_all = lambda token: False

    harness = _inject_harness()
    c = TestClient(build_daemon_app(harness=harness, verify_caller=deny_all))
    assert c.get("/api/v1/sessions", headers=_H).status_code == 403
    assert c.get("/api/v1/hosts/self", headers=_H).status_code == 403
    assert c.post("/api/v1/sessions/lumina-abc12345/inject", json={"text": "x"},
                  headers=_H).status_code == 403
    assert harness.injected == []
    # WS with a token but deny-all: closed (WebSocketDisconnect on connect).
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(
            "/api/v1/sessions/lumina-abc12345/stream?token=tok"
        ):
            pass

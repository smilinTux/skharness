"""skcode-hostd read-only daemon: 3 data routes, capauth-gated, zero write surface.

Driven by FakeHarness + FastAPI TestClient (no real tmux, no real bind).
"""
import dataclasses as _dc
import types as _t

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from skharness.auth import AuthContext
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


# ---- SessionEvent v2: seq/sid/source stamped at the one point every live -----
# ---- event passes through the daemon (card C-1, spec 5.1) --------------------

def test_ws_stream_stamps_seq_sid_source_on_every_event():
    c = _client()
    with c.websocket_connect(
        "/api/v1/sessions/lumina-abc12345/stream?token=good"
    ) as ws:
        first = ws.receive_json()
        second = ws.receive_json()
        # seq is per-session monotonic, assigned in append order.
        assert first["seq"] == 1
        assert second["seq"] == 2
        assert first["sid"] == "lumina-abc12345"
        assert second["sid"] == "lumina-abc12345"
        # this harness's sessions are the ordinary read-only session plane, so
        # they carry the default source (interactive), never autocode.
        assert first["source"] == "interactive"
        assert second["source"] == "interactive"
        # the original v1 fields are still exactly as before (additive only).
        assert first["type"] == "status"
        assert second["type"] == "assistant_text"
        assert second["text"] == "hello world"


def test_ws_stream_uses_autocode_source_when_session_is_registered_autocode():
    autocode_sessions = [
        SessionDescriptor(sid="lumina-abc12345", host=".158", harness="autocode",
                          source="autocode"),
    ]
    app = build_daemon_app(harness=_harness(), verify_caller=lambda t: t == "good",
                           list_autocode_sessions=lambda: autocode_sessions)
    c = TestClient(app)
    with c.websocket_connect(
        "/api/v1/sessions/lumina-abc12345/stream?token=good"
    ) as ws:
        assert ws.receive_json()["source"] == "autocode"


# ---- GET /sessions/{sid}/events : archive paging (spec 5.3) ------------------

def test_sessions_events_route_requires_read_scope():
    c = _client()
    assert c.get("/api/v1/sessions/lumina-abc12345/events").status_code == 401
    assert c.get("/api/v1/sessions/lumina-abc12345/events",
                 headers={"authorization": "Bearer bad"}).status_code == 403


def test_sessions_events_route_pages_the_archive(tmp_path):
    from skharness.session_events import SessionEventStore

    store = SessionEventStore(root=tmp_path)
    for i in range(5):
        store.append("lumina-abc12345", SessionEvent(type=EventType.ASSISTANT_TEXT,
                                                      text=str(i)))
    app = build_daemon_app(harness=_harness(), verify_caller=lambda t: t == "good",
                           event_store=store)
    c = TestClient(app)
    h = {"authorization": "Bearer good"}

    r = c.get("/api/v1/sessions/lumina-abc12345/events?limit=2", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["sid"] == "lumina-abc12345"
    assert [e["seq"] for e in body["events"]] == [4, 5]

    r2 = c.get("/api/v1/sessions/lumina-abc12345/events?before_seq=4&limit=2",
              headers=h)
    assert [e["seq"] for e in r2.json()["events"]] == [2, 3]


def test_sessions_events_route_empty_without_a_persisting_store():
    # No event_store configured (the daemon default): archive paging is always
    # an empty page, never an error, since nothing was ever archived.
    c = _client()
    r = c.get("/api/v1/sessions/lumina-abc12345/events",
              headers={"authorization": "Bearer good"})
    assert r.status_code == 200
    assert r.json() == {"sid": "lumina-abc12345", "events": []}


# ---- GET /jobs : cron ledger view (spec section 8, card C-8) -----------------

def test_jobs_route_requires_read_scope():
    c = _client()
    assert c.get("/api/v1/jobs").status_code == 401
    assert c.get("/api/v1/jobs", headers={"authorization": "Bearer bad"}).status_code == 403


def test_jobs_route_empty_without_a_jobs_provider():
    # No list_jobs configured (the daemon default): an empty list, never an
    # error -- a host with no cron ledger known yet is a valid, unremarkable
    # state, not a fault.
    c = _client()
    r = c.get("/api/v1/jobs", headers={"authorization": "Bearer good"})
    assert r.status_code == 200
    assert r.json() == {"jobs": []}


def test_jobs_route_reports_rows_from_the_configured_provider():
    from skharness.jobs import JobRun

    rows = [
        JobRun(job="drchiro-ingest", host="noroc2027", last_start="2026-08-11T22:30:01-04:00",
              status="ok", dur_s=1.5, tail="0 new", staleness_s=120.0, stale=False,
              stale_threshold_s=1800.0),
    ]
    app = build_daemon_app(harness=_harness(), verify_caller=lambda t: t == "good",
                           list_jobs=lambda: rows)
    c = TestClient(app)
    r = c.get("/api/v1/jobs", headers={"authorization": "Bearer good"})
    assert r.status_code == 200
    body = r.json()["jobs"]
    assert len(body) == 1
    assert body[0]["job"] == "drchiro-ingest"
    assert body[0]["status"] == "ok"
    assert body[0]["stale"] is False


def test_jobs_route_has_no_mutating_verb():
    # Card C-8's hard rule: "no run-now, cancel, or any mutating job action
    # exists in this card". Only GET is wired for /jobs; POST/PUT/DELETE are
    # all Method Not Allowed (the route path exists only for GET).
    c = _client()
    h = {"authorization": "Bearer good"}
    assert c.post("/api/v1/jobs", json={}, headers=h).status_code == 405
    assert c.put("/api/v1/jobs", json={}, headers=h).status_code == 405
    assert c.delete("/api/v1/jobs", headers=h).status_code == 405


# ---- GET /watchdog/digest : published skwatchdog digest (card C-14a) --------

def test_digest_route_requires_read_scope():
    c = _client()
    assert c.get("/api/v1/watchdog/digest").status_code == 401
    assert c.get("/api/v1/watchdog/digest",
                 headers={"authorization": "Bearer bad"}).status_code == 403


def test_digest_route_404_without_a_digest_provider():
    # No read_digest configured (the daemon default): a clean 404, "no digest
    # has been published yet" -- never a 200 with a fabricated empty digest
    # that would look exactly like a real quiet day.
    c = _client()
    r = c.get("/api/v1/watchdog/digest", headers={"authorization": "Bearer good"})
    assert r.status_code == 404


def test_digest_route_404_when_the_provider_reports_nothing_published():
    app = build_daemon_app(harness=_harness(), verify_caller=lambda t: t == "good",
                           read_digest=lambda: None)
    c = TestClient(app)
    r = c.get("/api/v1/watchdog/digest", headers={"authorization": "Bearer good"})
    assert r.status_code == 404


def test_digest_route_serves_the_provider_bytes_byte_for_byte():
    payload = (b'{"date": "2026-08-14", "headline": "quiet day", "problems": [], '
               b'"notable": [], "info_counts": {}}')
    app = build_daemon_app(harness=_harness(), verify_caller=lambda t: t == "good",
                           read_digest=lambda: payload)
    c = TestClient(app)
    r = c.get("/api/v1/watchdog/digest", headers={"authorization": "Bearer good"})
    assert r.status_code == 200
    assert r.content == payload
    assert r.json()["date"] == "2026-08-14"


def test_digest_route_serves_malformed_bytes_as_is_never_a_500():
    # A corrupt on-disk artifact is a DIFFERENT fact than "not published yet"
    # (card C-14a's hard rule): served with 200, unexamined, so the client's
    # own JSON parse fails distinctly rather than the server ever 500ing or
    # silently substituting a fake empty digest.
    broken = b"{not valid json"
    app = build_daemon_app(harness=_harness(), verify_caller=lambda t: t == "good",
                           read_digest=lambda: broken)
    c = TestClient(app)
    r = c.get("/api/v1/watchdog/digest", headers={"authorization": "Bearer good"})
    assert r.status_code == 200
    assert r.content == broken


def test_digest_route_has_no_mutating_verb():
    # Read-only surface, mirroring /jobs: only GET is wired, POST/PUT/DELETE
    # are all Method Not Allowed.
    c = _client()
    h = {"authorization": "Bearer good"}
    assert c.post("/api/v1/watchdog/digest", json={}, headers=h).status_code == 405
    assert c.put("/api/v1/watchdog/digest", json={}, headers=h).status_code == 405
    assert c.delete("/api/v1/watchdog/digest", headers=h).status_code == 405


# ---- autocode runs register as sessions (source=autocode, spec 5.1 / AC3) ---

def test_list_sessions_merges_registered_autocode_runs():
    autocode_sessions = [
        SessionDescriptor(sid="autocode-r1-t1", host=".158", harness="autocode",
                          repo="skharness", state="running", source="autocode"),
    ]
    app = build_daemon_app(harness=_harness(), verify_caller=lambda t: t == "good",
                           list_autocode_sessions=lambda: autocode_sessions)
    c = TestClient(app)
    r = c.get("/api/v1/sessions", headers={"authorization": "Bearer good"})
    sids = {s["sid"] for s in r.json()["sessions"]}
    assert sids == {"lumina-abc12345", "autocode-r1-t1"}
    row = next(s for s in r.json()["sessions"] if s["sid"] == "autocode-r1-t1")
    assert row["source"] == "autocode"


def test_get_session_finds_a_registered_autocode_run():
    autocode_sessions = [
        SessionDescriptor(sid="autocode-r1-t1", host=".158", harness="autocode",
                          source="autocode"),
    ]
    app = build_daemon_app(harness=_harness(), verify_caller=lambda t: t == "good",
                           list_autocode_sessions=lambda: autocode_sessions)
    c = TestClient(app)
    r = c.get("/api/v1/sessions/autocode-r1-t1", headers={"authorization": "Bearer good"})
    assert r.status_code == 200
    assert r.json()["source"] == "autocode"


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
    assert body["health"].endswith("/api/v1/hosts/self")
    # entry.url is gone: the shell mounts the native skcode_client package and
    # no longer routes a legacy embed, so the manifest must not advertise one.
    assert "url" not in body["entry"]
    assert body["entry"]["flutter_package"] == "skcode_client"


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
    def deny_all(token):
        return False

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


# ---- CR-6.2 C2/C8: inject/ratify PDP enrollment-mode floor (decide) -----------
# The scope gate proves POSSESSION of skcode.inject; the PDP floor proves the
# subject is VERIFIED. When the daemon is wired with an inject authorizer (as the
# live serve.build_inject_authorizer does), inject/ratify route through decide()
# and a deny is 403, audited, with the harness never reached.


@_dc.dataclass
class _Ob:
    kind: str = "audit"
    data: dict = _dc.field(default_factory=dict)


@_dc.dataclass
class _Dec:
    allow: bool
    reason: str = ""
    obligations: list = _dc.field(default_factory=list)


def _allow_inject(record=None):
    def _a(subject, resource, context):
        if record is not None:
            record.append((subject, resource, context))
        return _Dec(allow=True, reason="granted",
                    obligations=[_Ob(data={"decision": "allow", "subject": subject})])
    return _a


def _deny_inject(record=None):
    def _a(subject, resource, context):
        if record is not None:
            record.append((subject, resource, context))
        return _Dec(allow=False, reason="insufficient enrollment mode",
                    obligations=[_Ob(data={"decision": "deny", "subject": subject})])
    return _a


def test_inject_without_the_verified_capability_is_refused_403():
    # A subject that passes the scope gate but is BELOW the verified floor: the PDP
    # denies, inject is 403, audited, and the harness inject() is never reached.
    harness = _inject_harness()
    harness._spawned_sids = getattr(harness, "_spawned_sids", set())
    audits, calls = [], []
    write = _ctx_verifier("skcode.stream", "skcode.inject")
    app = build_daemon_app(harness=harness, verify_caller=write,
                           authorize_inject=_deny_inject(calls), audit_log=audits.append)
    r = TestClient(app).post("/api/v1/sessions/lumina-abc12345/inject",
                             json={"text": "hi"}, headers=_H)
    assert r.status_code == 403
    assert r.json()["detail"] == "inject not authorized"
    assert harness.injected == []            # never actuated
    assert calls and calls[0][1]["sid"] == "lumina-abc12345"   # PDP consulted
    assert any("deny" in a for a in audits)


def test_inject_with_the_verified_capability_actuates_and_audits_subject_and_hash():
    import hashlib
    harness = _inject_harness()
    audits, calls = [], []
    write = _ctx_verifier("skcode.stream", "skcode.inject")
    # _ctx_verifier carries no subject; the audit records "unknown-device" then.
    app = build_daemon_app(harness=harness, verify_caller=write,
                           authorize_inject=_allow_inject(calls), audit_log=audits.append)
    text = "run the tests"
    r = TestClient(app).post("/api/v1/sessions/lumina-abc12345/inject",
                             json={"text": text}, headers=_H)
    assert r.status_code == 200
    assert harness.injected == [("lumina-abc12345", text)]
    blob = " ".join(audits)
    # CR-6.2 C3: audit carries the subject + a CONTENT HASH (never the raw text).
    assert "skcode.inject" in blob and "allow" in blob
    assert hashlib.sha256(text.encode()).hexdigest() in blob
    assert text not in blob                  # the raw keystrokes are NOT logged


def test_inject_floor_absent_falls_back_to_scope_only():
    # No inject authorizer wired (a test double / legacy build): the shipped scope
    # gate stands alone and a scoped token still injects (no PDP regression).
    harness = _inject_harness()
    write = _ctx_verifier("skcode.stream", "skcode.inject")
    app = build_daemon_app(harness=harness, verify_caller=write)
    r = TestClient(app).post("/api/v1/sessions/lumina-abc12345/inject",
                             json={"text": "x"}, headers=_H)
    assert r.status_code == 200
    assert harness.injected == [("lumina-abc12345", "x")]


def test_ratify_below_the_verified_floor_is_refused_403(mocker):
    # ratify carries the same skcode.inject floor: a below-floor subject is 403 and
    # the grade path is never reached.
    sessions = [SessionDescriptor(sid="lumina-abc12345", host=".158",
                                  harness="fake", repo="skharness", branch="wt/x")]
    write = _ctx_verifier("skcode.stream", "skcode.inject")
    app = build_daemon_app(
        harness=_GradingHarness(sessions=sessions, grade_result=_five_complete()),
        verify_caller=write, authorize_inject=_deny_inject(),
        resolve_ratify=lambda s: (None, "", []), audit_log=lambda s: None)
    r = TestClient(app).post("/api/v1/sessions/lumina-abc12345/ratify", json={},
                             headers=_H)
    assert r.status_code == 403


# ---- card C-13: POST /sessions/{sid}/deny, the honest refusal route ----------
# The needs_input banner's Approve calls the real ratify route; Deny used to be a
# POST to /inject carrying a literal "n", which actuates nothing reliable while
# returning 200. This route gives Deny the SAME standing as Approve: the same
# SCOPE_WRITE bearer scope and the same skcode.inject PDP floor, and a response
# that distinguishes "refused" from "could not refuse".


class _DenyingHarness(FakeHarness):
    """FakeHarness plus a deny() that RECORDS the sid and returns the honest,
    idempotent-shaped result the contract requires, so the daemon deny route is
    driven with no real tmux/process. (FakeHarness leaves deny at the base gated
    raise; the deny path is proven here on a subclass, mirroring the
    inject/cancel/grade doubles.)"""

    def __init__(self, *, live=None, running=True):
        super().__init__(sessions=[
            SessionDescriptor(sid="lumina-abc12345", host=".158", harness="fake"),
        ], events={})
        self.denied: list[str] = []
        self._live = set(live) if live is not None else {"lumina-abc12345"}
        self._running = running

    async def deny(self, sid):
        self.denied.append(sid)
        if sid not in self._live:
            return {"sid": sid, "denied": False,
                    "reason": "no live session (already ended or never running)"}
        return {"sid": sid, "denied": True, "interrupted": self._running,
                "reason": ("in-flight turn interrupted; session refused (not resumable)"
                           if self._running else
                           "nothing in flight to interrupt; session refused (not resumable)")}


def test_deny_requires_auth_and_never_actuates_under_deny_all():
    harness = _DenyingHarness()
    c = TestClient(build_daemon_app(harness=harness, verify_caller=lambda t: False))
    assert c.post("/api/v1/sessions/lumina-abc12345/deny").status_code == 401
    assert c.post("/api/v1/sessions/lumina-abc12345/deny",
                  headers={"authorization": "Bearer bad"}).status_code == 403
    assert harness.denied == []             # nothing actuated


def test_deny_read_only_token_is_403_insufficient_scope():
    # Deny is a WRITE action: viewing a session never arms refusing it.
    harness = _DenyingHarness()
    app = build_daemon_app(harness=harness, verify_caller=_ctx_verifier("skcode.stream"))
    r = TestClient(app).post("/api/v1/sessions/lumina-abc12345/deny", headers=_H)
    assert r.status_code == 403
    assert harness.denied == []


def test_deny_below_the_verified_floor_is_refused_403_and_audited():
    """PDP-GATED, not merely present: a subject that passes the scope gate but
    sits below the verified enrollment floor is 403, audited, and the harness is
    never reached. This is the same floor ratify (Approve) carries."""
    harness = _DenyingHarness()
    audits, calls = [], []
    write = _ctx_verifier("skcode.stream", "skcode.inject")
    app = build_daemon_app(harness=harness, verify_caller=write,
                           authorize_inject=_deny_inject(calls), audit_log=audits.append)
    r = TestClient(app).post("/api/v1/sessions/lumina-abc12345/deny", headers=_H)
    assert r.status_code == 403
    assert r.json()["detail"] == "inject not authorized"
    assert harness.denied == []                                 # never actuated
    assert calls and calls[0][1]["sid"] == "lumina-abc12345"    # PDP consulted
    assert calls[0][1]["action"] == "deny"                      # on the deny action
    assert any("deny" in a for a in audits)


def test_deny_with_the_verified_capability_actuates_and_audits():
    harness = _DenyingHarness()
    audits, calls = [], []
    write = _ctx_verifier("skcode.stream", "skcode.inject")
    app = build_daemon_app(harness=harness, verify_caller=write,
                           authorize_inject=_allow_inject(calls), audit_log=audits.append)
    r = TestClient(app).post("/api/v1/sessions/lumina-abc12345/deny", headers=_H)
    assert r.status_code == 200
    assert r.json()["denied"] is True
    assert r.json()["interrupted"] is True
    assert harness.denied == ["lumina-abc12345"]
    blob = " ".join(audits)
    assert "skcode.deny" in blob and "allow" in blob


def test_deny_that_did_not_take_effect_is_not_reported_as_success():
    """THE rule of card C-13. An unknown / already-finished session is a clean
    200 no-op that says denied: FALSE with a reason, never a bare success the
    operator would read as "refused"."""
    harness = _DenyingHarness(live=[])
    write = _ctx_verifier("skcode.stream", "skcode.inject")
    app = build_daemon_app(harness=harness, verify_caller=write,
                           authorize_inject=_allow_inject(), audit_log=lambda s: None)
    r = TestClient(app).post("/api/v1/sessions/never-existed/deny", headers=_H)
    assert r.status_code == 200
    body = r.json()
    assert body["denied"] is False
    assert "no live session" in body["reason"]
    assert harness.denied == ["never-existed"]


def test_deny_reports_refused_but_not_interrupted_distinctly():
    """Refused-and-stopped and refused-with-nothing-left-to-stop are different
    facts; the client can tell them apart without guessing."""
    harness = _DenyingHarness(running=False)
    write = _ctx_verifier("skcode.stream", "skcode.inject")
    app = build_daemon_app(harness=harness, verify_caller=write,
                           authorize_inject=_allow_inject(), audit_log=lambda s: None)
    body = TestClient(app).post("/api/v1/sessions/lumina-abc12345/deny",
                                headers=_H).json()
    assert body["denied"] is True
    assert body["interrupted"] is False
    assert "nothing in flight" in body["reason"]


def test_deny_audit_records_the_refusal_outcome_not_just_the_decision():
    """The audit must answer BOTH questions: was the caller allowed to refuse
    (decision) and did the refusal take effect (denied/interrupted). A record
    carrying only "allow" would hide exactly the failure this card is about."""
    import json as _json

    harness = _DenyingHarness(live=[])
    audits = []
    write = _ctx_verifier("skcode.stream", "skcode.inject")
    app = build_daemon_app(harness=harness, verify_caller=write,
                           authorize_inject=_allow_inject(), audit_log=audits.append)
    TestClient(app).post("/api/v1/sessions/gone-forever/deny", headers=_H)
    records = [_json.loads(a) for a in audits if "skcode.deny" in a]
    assert records and records[0]["decision"] == "allow"     # authorization outcome
    assert records[0]["denied"] is False                     # refusal outcome
    assert records[0]["sid"] == "gone-forever"


def test_deny_floor_absent_falls_back_to_scope_only():
    # No inject authorizer wired (a test double / legacy build): the shipped scope
    # gate stands alone and a scoped token still denies (no PDP regression).
    harness = _DenyingHarness()
    write = _ctx_verifier("skcode.stream", "skcode.inject")
    app = build_daemon_app(harness=harness, verify_caller=write)
    r = TestClient(app).post("/api/v1/sessions/lumina-abc12345/deny", headers=_H)
    assert r.status_code == 200
    assert harness.denied == ["lumina-abc12345"]

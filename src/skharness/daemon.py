"""skcode-hostd daemon (skcode remote-control, spec 8.1).

One host daemon owning ONE Harness (its session plane). Exposes the three
capauth-gated read data routes (list sessions, get one, stream one over WS), one
grade-only write-ish route (POST /sessions/{sid}/ratify), and the P1 session
INJECT write surface (POST /sessions/{sid}/inject: send operator text into a
running session as keystrokes). Ratify runs the autocode twin gate over the
session's existing worktree diff and NEVER merges, commits, or pushes; inject
only sends keystrokes into the session's PTY. There is still NO
spawn/kill/dispatch route. Every gated route fails closed on the shared
skharness.auth bearer: with the P0 deny-all verifier still in force, every caller
is 401/403 and NOTHING actuates, so inject is inert in prod until the real
verifier lands (R2.4). Bind a Tailscale IP only (serve.py enforces this).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from skharness.auth import Verifier, check_token, require_bearer
from skharness.autocode import ratify as _ratify
from skharness.autocode.types import RepoSpec
from skharness.events import EventType, SessionEvent
from skharness.harness import Harness, SessionDescriptor, SpawnRejected
from skharness.jobs import JobRun
from skharness.manifest import skcode_module_manifest
from skharness.session_events import SessionEventStore

# The dispatch authorization seam (spec 7.4): given the authenticated subject and
# the request resource, return a decision exposing ``.allow`` (bool) and, ideally,
# ``.obligations`` (each carrying an audit record). This is exactly the shape of
# ``capauth.authz.decide(...)``'s Decision; serve.py wires the real PDP. The daemon
# NEVER decides policy itself: no authorizer configured => dispatch is denied.
Authorizer = Callable[[str, dict, dict], Any]

# Advisory targets provider for GET /dispatch/targets: returns the repos/harnesses/
# hosts/profiles a device MAY target here. Advisory only; /dispatch re-enforces.
TargetsProvider = Callable[[], dict]

# Emergency-brake predicate: True => dispatch is paused (returns 503). Injected so
# the persisted pause flag (operator_cli pause-dispatch) drives it.
PausePredicate = Callable[[], bool]

# A host resolves one of its sessions to the (repo, worktree, acceptance) triple
# ratify needs; None means the session has no gradable worktree. Injected so the
# daemon stays free of any repo-map/worktree convention of its own.
RatifyResolver = Callable[[SessionDescriptor], "tuple[RepoSpec, str, list[str]] | None"]

# Autocode orchestrator runs registering themselves as sessions (source=autocode,
# spec 5.1, card C-1 AC3): a provider of the CURRENT live/recent autocode-run
# SessionDescriptors, merged into GET /sessions and GET /sessions/{sid} alongside
# the harness's own (interactive) rows. None (the default) merges nothing, so a
# bare test double sees exactly the harness's sessions, unchanged.
AutocodeSessionsProvider = Callable[[], "list[SessionDescriptor]"]

# Cron/scheduler ledger view (spec section 8, card C-8): a provider of the CURRENT
# JobRun rows, read fresh on every call (the ledger is a live, append-only file
# owned by the scheduler, not something this daemon caches). None (the default)
# reports no jobs known, so a bare test double never touches any real ledger path.
JobsProvider = Callable[[], "list[JobRun]"]

# Scope split on the remote-control surface (R2.4): a verified skcode token must
# carry SCOPE_READ to view (list/get/stream), and SCOPE_WRITE to actuate
# (inject/ratify). Enabling the verifier with a read-only token grants view
# WITHOUT arming keystroke-inject. The deny-all default carries no scopes and so
# denies both regardless.
SCOPE_READ = "skcode.stream"
SCOPE_WRITE = "skcode.inject"
# The THIRD scope, for the RCE surface: spawning a NEW session (spec 7.4). It is
# strictly above inject: a device may stream and inject into existing sessions
# without ever being able to spawn a new one. Dispatch requires this scope AND a
# separate authz allow decision AND an audit sink (all fail closed).
SCOPE_DISPATCH = "skcode.dispatch"

# The capauth capability the inject/ratify PDP decides on (CR-6.2 C2/C8). Same
# string as SCOPE_WRITE; seeded VERIFIED in capauth.authz.DEFAULT_RULES (C3), so
# routing inject/ratify through decide() enforces the enrollment-mode floor in
# code, not only at token issuance.
INJECT_CAPABILITY = SCOPE_WRITE


# --------------------------------------------------------------------------- #
# Route-coverage classification (SKWorld Authorization Model L1.3; CR-6.2 C3).
# Every HTTP/WS route this daemon serves is exactly ONE class: PUBLIC (no bearer)
# or gated on a named scope. The coverage-completeness test enumerates the LIVE
# app route table and asserts every served route is declared here, so a new gated
# route can never ship unclassified (the same class of gap the skchat dataplane
# coverage gate closes). ``skcode.stream`` is scope-only (read/view, no PDP
# decision); ``skcode.inject`` and ``skcode.dispatch`` are additionally PDP-decided
# and carry a capauth DEFAULT_RULES row.
# --------------------------------------------------------------------------- #
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/.well-known/skworld-module.json"),
    ("GET", "/"),
    ("GET", "/app"),
})

#: (METHOD, path_format) -> required scope for every gated route. "WS" is the
#: websocket stream (browsers cannot set headers, so its token rides the query).
ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/hosts/self"): SCOPE_READ,
    ("GET", "/api/v1/sessions"): SCOPE_READ,
    ("GET", "/api/v1/sessions/{sid}"): SCOPE_READ,
    ("GET", "/api/v1/sessions/{sid}/events"): SCOPE_READ,
    ("GET", "/api/v1/jobs"): SCOPE_READ,
    ("GET", "/api/v1/dispatch/targets"): SCOPE_DISPATCH,
    ("POST", "/api/v1/sessions/{sid}/ratify"): SCOPE_WRITE,
    ("POST", "/api/v1/sessions/{sid}/inject"): SCOPE_WRITE,
    ("POST", "/api/v1/dispatch"): SCOPE_DISPATCH,
    ("POST", "/api/v1/sessions/{sid}/cancel"): SCOPE_DISPATCH,
    ("WS", "/api/v1/sessions/{sid}/stream"): SCOPE_READ,
}

#: Scopes that route through the capauth PDP (decide) and so REQUIRE a rule row in
#: capauth.authz.DEFAULT_RULES. ``skcode.stream`` is deliberately excluded (it is a
#: scope-only read capability with no PDP decision).
PDP_SCOPES: frozenset[str] = frozenset({SCOPE_WRITE, SCOPE_DISPATCH})


def classify_route(method: str, path: str) -> tuple[str | None, str | None]:
    """Classify a served route as ("public", None) | ("gated", scope) | (None, None).

    (None, None) means UNCLASSIFIED, which the coverage test treats as a failure:
    a gated route with no declared scope is unsafe to ship (it might not be gated
    at all, or be gated on an unknown scope).
    """
    key = (method.upper(), path)
    if key in PUBLIC_ROUTES:
        return ("public", None)
    scope = ROUTE_SCOPES.get(key)
    if scope is not None:
        return ("gated", scope)
    return (None, None)


def _content_hash(text: str | None) -> str:
    """A sha256 hex digest of injected content for the audit line (CR-6.2 C3).

    The raw keystrokes are NEVER logged (a session may be sent secrets); the audit
    records only this digest + a length, so an inject is attributable to WHAT was
    sent (comparable across lines) without disclosing the content itself.
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


_CLIENT_DIR = Path(__file__).parent / "client"
_PLACEHOLDER = (
    "<!doctype html><meta charset=utf-8><title>skcode-hostd</title>"
    "<h1>skcode-hostd</h1><p>Read-only client not installed yet.</p>"
)


def _client_html(client_dir: Path | None) -> str:
    base = client_dir if client_dir is not None else _CLIENT_DIR
    index = Path(base) / "index.html"
    if index.exists():
        return index.read_text(encoding="utf-8")
    return _PLACEHOLDER


def build_daemon_app(
    *,
    harness: Harness,
    verify_caller: Verifier,
    host_id: str = ".158",
    client_dir: Path | None = None,
    resolve_ratify: RatifyResolver | None = None,
    emit_event: Callable[[str, SessionEvent], None] | None = None,
    audit_log: Callable[[str], None] | None = None,
    authorize_dispatch: Authorizer | None = None,
    authorize_inject: Authorizer | None = None,
    dispatch_targets: TargetsProvider | None = None,
    dispatch_paused: PausePredicate | None = None,
    event_store: SessionEventStore | None = None,
    list_autocode_sessions: AutocodeSessionsProvider | None = None,
    list_jobs: JobsProvider | None = None,
) -> FastAPI:
    app = FastAPI(title="skcode-hostd")
    # SessionEvent v2 (spec 5.1, card C-1): assigns seq/sid/source at append and
    # (when the caller configures a persisting store) archives to the capped
    # per-session JSONL. Defaults to an in-memory-only store (persist=False) so
    # a bare test double, or a daemon nobody wired persistence for, still gets
    # correct seq assignment without ever touching disk; serve.py wires a real
    # persisting store for the live daemon.
    store = event_store if event_store is not None else SessionEventStore(persist=False)

    def _session_source(sid: str) -> str:
        if list_autocode_sessions is not None:
            for s in list_autocode_sessions():
                if s.sid == sid:
                    return s.source or "autocode"
        return "interactive"

    async def _all_sessions() -> list[SessionDescriptor]:
        rows = list(await harness.list_sessions())
        if list_autocode_sessions is not None:
            rows = rows + list(list_autocode_sessions())
        return rows

    def _auth(authorization: str | None, required_scope: str | None = None) -> None:
        require_bearer(authorization, verify_caller, required_scope)

    def _authed_context(authorization: str | None, required_scope: str | None):
        """Enforce the bearer + scope gate AND return the verified auth context.

        ``require_bearer`` raises 401/403 on any failure, so control only returns
        here for an ALLOWED caller; we then re-verify to recover the AuthContext
        (verifiers are pure) so the dispatch route can read the subject fqid off it.
        """
        token = require_bearer(authorization, verify_caller, required_scope)
        return verify_caller(token)

    def _subject(auth_result) -> str:
        subj = getattr(auth_result, "subject", None)
        return subj.strip() if isinstance(subj, str) and subj.strip() else "unknown-device"

    def _emit_audit(record: dict) -> None:
        # audit_log is a plain str sink; serialize the structured record.
        if audit_log is not None:
            audit_log(json.dumps(record, default=str, sort_keys=True))

    def _emit_decision_obligations(decision) -> None:
        # Honour the PDP's audit obligations (spec 7.4: every decision, allow OR
        # deny, carries an audit obligation the PEP must write).
        for ob in getattr(decision, "obligations", None) or []:
            data = getattr(ob, "data", None)
            if data is None and hasattr(ob, "model_dump"):
                data = ob.model_dump()
            _emit_audit({"kind": getattr(ob, "kind", "audit"), "data": data or {}})

    def _enforce_inject_floor(subject: str, resource: dict) -> None:
        """CR-6.2 C2/C8: the PDP enrollment-mode floor for the write surface.

        When an inject authorizer is configured (production wiring), route the
        inject/ratify write through ``capauth.authz.decide`` the way dispatch does,
        requiring the verified-tier ``skcode.inject`` capability. A deny is 403 and
        audited; the actuation is never reached. When no authorizer is configured
        (test doubles that exercise only the shipped scope split), the scope gate
        stands alone and this is a no-op. The live daemon always wires the
        authorizer (serve.build_inject_authorizer), so the floor is enforced in
        code there, not only at token issuance.
        """
        if authorize_inject is None:
            return
        decision = authorize_inject(subject, resource, {})
        _emit_decision_obligations(decision)
        if not bool(getattr(decision, "allow", False)):
            _emit_audit({"event": "skcode.inject", "subject": subject,
                         "resource": resource, "decision": "deny",
                         "reason": getattr(decision, "reason", "")})
            raise HTTPException(403, "inject not authorized")

    @app.get("/.well-known/skworld-module.json")
    async def module_manifest(request: Request):
        # Public discovery metadata (NO bearer): the shell reads the manifest to
        # learn skcode's entry, nav, and required auth audience/scopes BEFORE it
        # has a token. It carries no secrets. URLs are origin-relative to the
        # request, so they resolve against wherever this host actually answers.
        return JSONResponse(skcode_module_manifest(str(request.base_url)))

    @app.get("/api/v1/hosts/self")
    async def hosts_self(authorization: str | None = Header(default=None)):
        _auth(authorization, SCOPE_READ)
        return JSONResponse({"host": host_id, "harness": harness.name, "ok": True})

    @app.get("/api/v1/sessions")
    async def list_sessions(authorization: str | None = Header(default=None)):
        _auth(authorization, SCOPE_READ)
        rows = await _all_sessions()
        return JSONResponse({"sessions": [s.to_dict() for s in rows]})

    @app.get("/api/v1/sessions/{sid}")
    async def get_session(sid: str, authorization: str | None = Header(default=None)):
        _auth(authorization, SCOPE_READ)
        for s in await _all_sessions():
            if s.sid == sid:
                return JSONResponse(s.to_dict())
        raise HTTPException(404, "session not found")

    @app.get("/api/v1/sessions/{sid}/events")
    async def session_events(sid: str, before_seq: int | None = None, limit: int = 100,
                             authorization: str | None = Header(default=None)):
        # Archive paging over the capped per-session JSONL (spec 5.3): the client
        # reconnect/scrollback path, same read scope as the live WS tail. When the
        # daemon was built with no persisting event_store (persist=False, the
        # default here), this is simply always empty -- there is nothing archived
        # to page through, never an error.
        _auth(authorization, SCOPE_READ)
        rows = store.read_page(sid, before_seq=before_seq, limit=limit)
        return JSONResponse({"sid": sid, "events": rows})

    @app.get("/api/v1/jobs")
    async def list_jobs_route(authorization: str | None = Header(default=None)):
        # Read-only view over the cron/scheduler ledger (spec section 8, card
        # C-8). Same read scope as sessions/events: it is a VIEW, never a store,
        # so it needs no write scope and no PDP decision. hostd owns none of this
        # data -- the scheduler does -- and no run-now/cancel/retry action exists
        # here or anywhere else in this daemon (deliberately deferred). When no
        # jobs provider is wired (bare test doubles, or a host with no cron
        # ledger yet), this reports an empty list rather than 404/500: "no jobs
        # known yet" is a valid, unremarkable state.
        _auth(authorization, SCOPE_READ)
        rows: list[JobRun] = list_jobs() if list_jobs is not None else []
        return JSONResponse({"jobs": [r.to_dict() for r in rows]})

    @app.post("/api/v1/sessions/{sid}/ratify")
    async def ratify_session(sid: str, authorization: str | None = Header(default=None)):
        # The ONE write-ish route: grade-only. It runs the autocode twin gate over
        # the session's EXISTING worktree diff. It never merges/commits/pushes (see
        # skharness.autocode.ratify), so it does not modify the repo. It is still
        # a WRITE-class action (it actuates a grade over the session), so it needs
        # SCOPE_WRITE: a read-only token cannot trigger it. CR-6.2 C2/C8: it also
        # passes the PDP mode floor (verified skcode.inject) when the authorizer is
        # wired, exactly like inject.
        auth = _authed_context(authorization, SCOPE_WRITE)
        _enforce_inject_floor(_subject(auth), {"host": host_id, "sid": sid, "action": "ratify"})
        session = next((s for s in await harness.list_sessions() if s.sid == sid), None)
        if session is None:
            raise HTTPException(404, "session not found")
        if resolve_ratify is None:
            raise HTTPException(501, "ratify is not configured on this host")
        ctx = resolve_ratify(session)
        if ctx is None:
            raise HTTPException(409, "session has no gradable worktree")
        repo, worktree, acceptance = ctx
        result = _ratify(repo, worktree, acceptance, harness)   # grade only, no merge
        if audit_log is not None:
            audit_log(f"ratify {sid} {'PASS' if result.passed else 'FAIL'} "
                      f"score={result.score}")
        if not result.passed:
            # A failed gate needs an operator: emit a needs_input event (this drives
            # the sk-alert push in production).
            if emit_event is not None:
                emit_event(sid, SessionEvent(
                    type=EventType.NEEDS_INPUT,
                    text=f"ratify failed for {sid} (score={result.score})",
                    data={"sid": sid, "score": result.score, "passed": False,
                          "notes": result.notes}))
        return JSONResponse({"sid": sid, "score": result.score,
                             "passed": result.passed, "notes": result.notes,
                             "artifact": result.artifact, "mode": result.mode})

    @app.post("/api/v1/sessions/{sid}/inject")
    async def inject_session(sid: str, request: Request,
                             authorization: str | None = Header(default=None)):
        # The P1 session WRITE surface: send operator text into a running session
        # as keystrokes. Gated exactly like the other bearer routes and fails
        # closed BEFORE any actuation: with the P0 deny-all verifier a request
        # with no/invalid token is 401/403 and harness.inject is never reached.
        # A read-only (skcode.stream) token is 403 here too: inject needs
        # SCOPE_WRITE, so enabling view never arms keystroke-inject.
        auth = _authed_context(authorization, SCOPE_WRITE)
        subject = _subject(auth)
        # CR-6.2 C2/C8: PDP mode floor. When configured, inject routes through
        # capauth.authz.decide requiring the verified-tier skcode.inject
        # capability; a deny is 403 (audited) and the harness is never reached.
        _enforce_inject_floor(subject, {"host": host_id, "sid": sid, "action": "inject"})
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = (body or {}).get("text", "")
        result = await harness.inject(sid, text)
        # CR-6.2 C3: enrich the inject audit with the subject + a content HASH
        # (never the raw keystrokes) so the action is attributable to WHO + WHAT
        # without recording secrets typed into a session.
        _emit_audit({
            "event": "skcode.inject",
            "subject": subject,
            "sid": sid,
            "decision": "allow",
            "injected": bool(result.get("injected")),
            "reason": result.get("reason", ""),
            "content_sha256": _content_hash(text),
            "content_len": len(text or ""),
        })
        return JSONResponse(result)

    @app.get("/api/v1/dispatch/targets")
    async def dispatch_targets_route(authorization: str | None = Header(default=None)):
        # ADVISORY ONLY (spec 4.2): tells a dispatch-capable device which repos /
        # harnesses / hosts / profiles it may target here, so the UI never offers a
        # forbidden action. It is NOT the enforcement point: POST /dispatch
        # re-derives and re-checks everything server-side and never trusts the
        # client's target choice. Gated on the dispatch scope, so a view/inject-only
        # device sees nothing to dispatch.
        _auth(authorization, SCOPE_DISPATCH)
        base = {"harnesses": [harness.name], "hosts": [host_id],
                "repos": [], "profiles": ["sandbox", "full"]}
        if dispatch_targets is not None:
            base.update(dispatch_targets() or {})
        return JSONResponse({"advisory": True, **base})

    @app.post("/api/v1/dispatch")
    async def dispatch_route(request: Request,
                             authorization: str | None = Header(default=None)):
        # The RCE surface. Fail-closed gate order (spec 7.4):
        #   0. emergency brake: if dispatch is paused, 503 REGARDLESS of auth, and
        #      nothing downstream (auth, authz, spawn) runs.
        if dispatch_paused is not None and dispatch_paused():
            raise HTTPException(503, "dispatch paused")
        #   1. authn + scope: no token => 401, wrong/insufficient scope => 403.
        auth = _authed_context(authorization, SCOPE_DISPATCH)
        #   2. audit + authz MUST be configured, else deny (never allow blind).
        if audit_log is None:
            raise HTTPException(501, "audit sink not configured; dispatch denied")
        if authorize_dispatch is None:
            raise HTTPException(501, "authz PDP not configured; dispatch denied")
        #   3. parse + shape the request (auth is NOT input validation).
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        repo = str(body.get("repo", "") or "")
        branch = str(body.get("branch", "") or "")
        profile = str(body.get("profile", "sandbox") or "sandbox")
        permission_mode = str(body.get("permission_mode", "manual") or "manual")
        # Session mode: "direct" (print/one-shot) or "interactive" (stays open).
        # Validate here (400) so a bad mode never reaches spawn; this is input
        # validation only and does NOT change any auth/authz/scope gate above.
        mode = str(body.get("mode", "direct") or "direct")
        if mode not in ("direct", "interactive"):
            raise HTTPException(400, f"invalid mode {mode!r} (want 'direct' or 'interactive')")
        prompt = str(body.get("prompt", "") or "")
        subject = _subject(auth)
        resource = {"host": host_id, "repo": repo, "branch": branch, "profile": profile}
        #   4. authz decision (the allow gate) + audit ALWAYS (allow or deny).
        decision = authorize_dispatch(subject, resource, {"permission_mode": permission_mode})
        _emit_decision_obligations(decision)
        allow = bool(getattr(decision, "allow", False))
        _emit_audit({
            "event": "skcode.dispatch",
            "subject": subject,
            "decision": "allow" if allow else "deny",
            "request": {"harness": str(body.get("harness", "") or ""), "host": host_id,
                        "repo": repo, "branch": branch, "profile": profile,
                        "permission_mode": permission_mode, "mode": mode,
                        "model": str(body.get("model", "") or ""),
                        "prompt_len": len(prompt)},
            "reason": getattr(decision, "reason", ""),
        })
        if not allow:
            raise HTTPException(403, "dispatch not authorized")
        #   5. spawn. The harness re-runs the RCE input guards (allowlist / branch /
        #      charset) and fails closed with SpawnRejected => 400 (never a 5xx),
        #      so a bad repo/branch/name never reaches a subprocess.
        desc = SessionDescriptor(
            host=host_id, harness=str(body.get("harness", "") or harness.name),
            repo=repo, branch=branch, model=str(body.get("model", "") or ""),
            quality=profile, permission_mode=permission_mode, mode=mode)
        try:
            session = await harness.spawn(desc, prompt=prompt)
        except SpawnRejected as exc:
            _emit_audit({"event": "skcode.dispatch.rejected", "subject": subject,
                         "resource": resource, "reason": str(exc)})
            raise HTTPException(400, f"spawn rejected: {exc}")
        _emit_audit({"event": "skcode.dispatch.spawned", "subject": subject,
                     "sid": session.sid, "resource": resource})
        return JSONResponse({"sid": session.sid, "status": session.status,
                             "branch": session.branch, "profile": profile, "mode": mode})

    @app.post("/api/v1/sessions/{sid}/cancel")
    async def cancel_session(sid: str, authorization: str | None = Header(default=None)):
        # Cancel a LIVE session (spec section 8). It rides the DISPATCH scope
        # through the SAME PDP decision path as POST /dispatch: a caller needs
        # skcode.dispatch AND an authz allow, exactly like spawning a new
        # session, so a read-only or inject-only token can never cancel. Fail
        # closed like dispatch: no audit sink or no authz PDP configured => 501,
        # never a silent allow.
        auth = _authed_context(authorization, SCOPE_DISPATCH)
        if audit_log is None:
            raise HTTPException(501, "audit sink not configured; cancel denied")
        if authorize_dispatch is None:
            raise HTTPException(501, "authz PDP not configured; cancel denied")
        subject = _subject(auth)
        resource = {"host": host_id, "sid": sid, "action": "cancel"}
        decision = authorize_dispatch(subject, resource, {})
        _emit_decision_obligations(decision)
        allow = bool(getattr(decision, "allow", False))
        _emit_audit({
            "event": "skcode.cancel",
            "subject": subject,
            "sid": sid,
            "decision": "allow" if allow else "deny",
            "reason": getattr(decision, "reason", ""),
        })
        if not allow:
            raise HTTPException(403, "cancel not authorized")
        # Idempotent + safe by construction: harness.cancel returns a clean
        # {"cancelled": False, "reason": ...} for an unknown/already-finished
        # session rather than raising, so this route never 500s on a stale sid.
        result = await harness.cancel(sid)
        return JSONResponse(result)

    @app.websocket("/api/v1/sessions/{sid}/stream")
    async def stream(websocket: WebSocket, sid: str):
        # Browsers cannot set headers on a WebSocket, so the token rides the query
        # string. Fail closed (close 1008) BEFORE accept on a bad/missing token.
        token = websocket.query_params.get("token")
        if not check_token(token, verify_caller, SCOPE_READ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        source = _session_source(sid)
        try:
            async for ev in harness.stream(sid):
                # SessionEvent v2 (spec 5.1): assign seq/sid/source at append,
                # here at the one point every live event actually passes through
                # the daemon, then (when persistence is configured) archive it.
                stamped = store.append(sid, ev, source=source)
                await websocket.send_json(stamped.to_dict())
        except WebSocketDisconnect:
            return
        await websocket.close()

    @app.get("/", response_class=HTMLResponse)
    @app.get("/app", response_class=HTMLResponse)
    async def client_page():
        return HTMLResponse(_client_html(client_dir))

    return app

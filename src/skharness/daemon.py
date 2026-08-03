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
from skharness.manifest import skcode_module_manifest

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
    dispatch_targets: TargetsProvider | None = None,
    dispatch_paused: PausePredicate | None = None,
) -> FastAPI:
    app = FastAPI(title="skcode-hostd")

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
        rows = await harness.list_sessions()
        return JSONResponse({"sessions": [s.to_dict() for s in rows]})

    @app.get("/api/v1/sessions/{sid}")
    async def get_session(sid: str, authorization: str | None = Header(default=None)):
        _auth(authorization, SCOPE_READ)
        for s in await harness.list_sessions():
            if s.sid == sid:
                return JSONResponse(s.to_dict())
        raise HTTPException(404, "session not found")

    @app.post("/api/v1/sessions/{sid}/ratify")
    async def ratify_session(sid: str, authorization: str | None = Header(default=None)):
        # The ONE write-ish route: grade-only. It runs the autocode twin gate over
        # the session's EXISTING worktree diff. It never merges/commits/pushes (see
        # skharness.autocode.ratify), so it does not modify the repo. It is still
        # a WRITE-class action (it actuates a grade over the session), so it needs
        # SCOPE_WRITE: a read-only token cannot trigger it.
        _auth(authorization, SCOPE_WRITE)
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
        _auth(authorization, SCOPE_WRITE)
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = (body or {}).get("text", "")
        result = await harness.inject(sid, text)
        if audit_log is not None:
            audit_log(f"inject {sid} {'OK' if result.get('injected') else 'NOOP'}")
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

    @app.websocket("/api/v1/sessions/{sid}/stream")
    async def stream(websocket: WebSocket, sid: str):
        # Browsers cannot set headers on a WebSocket, so the token rides the query
        # string. Fail closed (close 1008) BEFORE accept on a bad/missing token.
        token = websocket.query_params.get("token")
        if not check_token(token, verify_caller, SCOPE_READ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            async for ev in harness.stream(sid):
                await websocket.send_json(ev.to_dict())
        except WebSocketDisconnect:
            return
        await websocket.close()

    @app.get("/", response_class=HTMLResponse)
    @app.get("/app", response_class=HTMLResponse)
    async def client_page():
        return HTMLResponse(_client_html(client_dir))

    return app

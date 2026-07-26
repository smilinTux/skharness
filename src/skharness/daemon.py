"""skcode-hostd - read-only P0 daemon (skcode remote-control, spec 8.1).

One host daemon owning ONE Harness (its session plane). Exposes the three
capauth-gated read data routes (list sessions, get one, stream one over WS) plus
one grade-only write-ish route: POST /sessions/{sid}/ratify. Ratify runs the
autocode twin gate over the session's existing worktree diff and NEVER merges,
commits, or pushes, so the daemon stays safe. There is still NO
spawn/inject/kill/dispatch route: the mutating write surface does not exist in
P0. Bind a Tailscale IP only (serve.py enforces this). The bearer gate is the
shared skharness.auth (same as the gateway).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from skharness.auth import Verifier, check_token, require_bearer
from skharness.autocode import ratify as _ratify
from skharness.autocode.types import RepoSpec
from skharness.events import EventType, SessionEvent
from skharness.harness import Harness, SessionDescriptor

# A host resolves one of its sessions to the (repo, worktree, acceptance) triple
# ratify needs; None means the session has no gradable worktree. Injected so the
# daemon stays free of any repo-map/worktree convention of its own.
RatifyResolver = Callable[[SessionDescriptor], "tuple[RepoSpec, str, list[str]] | None"]

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
) -> FastAPI:
    app = FastAPI(title="skcode-hostd")

    def _auth(authorization: str | None) -> None:
        require_bearer(authorization, verify_caller)

    @app.get("/api/v1/hosts/self")
    async def hosts_self(authorization: str | None = Header(default=None)):
        _auth(authorization)
        return JSONResponse({"host": host_id, "harness": harness.name, "ok": True})

    @app.get("/api/v1/sessions")
    async def list_sessions(authorization: str | None = Header(default=None)):
        _auth(authorization)
        rows = await harness.list_sessions()
        return JSONResponse({"sessions": [s.to_dict() for s in rows]})

    @app.get("/api/v1/sessions/{sid}")
    async def get_session(sid: str, authorization: str | None = Header(default=None)):
        _auth(authorization)
        for s in await harness.list_sessions():
            if s.sid == sid:
                return JSONResponse(s.to_dict())
        raise HTTPException(404, "session not found")

    @app.post("/api/v1/sessions/{sid}/ratify")
    async def ratify_session(sid: str, authorization: str | None = Header(default=None)):
        # The ONE write-ish route: grade-only. It runs the autocode twin gate over
        # the session's EXISTING worktree diff. It never merges/commits/pushes (see
        # skharness.autocode.ratify), so it does not modify the repo.
        _auth(authorization)
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

    @app.websocket("/api/v1/sessions/{sid}/stream")
    async def stream(websocket: WebSocket, sid: str):
        # Browsers cannot set headers on a WebSocket, so the token rides the query
        # string. Fail closed (close 1008) BEFORE accept on a bad/missing token.
        token = websocket.query_params.get("token")
        if not check_token(token, verify_caller):
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

"""skcode-hostd - read-only P0 daemon (skcode remote-control, spec 8.1).

One host daemon owning ONE Harness (its session plane). Exposes exactly three
capauth-gated data routes (list sessions, get one, stream one over WS) plus a
static read-only client page. There is NO spawn/inject/kill/dispatch route: the
write surface does not exist in P0. Bind a Tailscale IP only (serve.py enforces
this). The bearer gate is the shared skharness.auth (same as the gateway).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from skharness.auth import Verifier, check_token, require_bearer
from skharness.harness import Harness

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

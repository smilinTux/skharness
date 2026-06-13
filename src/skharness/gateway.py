"""skharness gateway — capauth-gated FastAPI over the SessionManager. Bind to a
Tailscale IP only (never a public port). `verify_caller` is the auth seam: a real
capauth verifier in production, a fake in tests."""

from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from skharness.manager import SessionManager

Verifier = Callable[[str], bool]


def build_app(*, manager: SessionManager, verify_caller: Verifier) -> FastAPI:
    app = FastAPI(title="skharness")

    def _auth(authorization: str | None) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        token = authorization[len("Bearer "):].strip()
        if not token:
            raise HTTPException(401, "missing bearer token")
        if not verify_caller(token):
            raise HTTPException(403, "unauthorized")

    @app.get("/sessions")
    async def list_sessions(authorization: str | None = Header(default=None)):
        _auth(authorization)
        return JSONResponse({"sessions": [s.to_dict() for s in manager.list()]})

    @app.post("/sessions")
    async def spawn(request: Request, authorization: str | None = Header(default=None)):
        _auth(authorization)
        body = await request.json()
        agent = (body.get("agent") or "").strip()
        repo = (body.get("repo") or "").strip()
        if not (agent and repo):
            raise HTTPException(400, "agent and repo required")
        s = await manager.spawn(agent=agent, prompt=body.get("prompt", ""), repo=repo)
        return JSONResponse(s.to_dict())

    @app.get("/sessions/{sid}/attach")
    async def attach(sid: str, authorization: str | None = Header(default=None)):
        _auth(authorization)
        url = manager.attach_url(sid)
        if url is None:
            raise HTTPException(404, "session not found or ended")
        return JSONResponse({"session_id": sid, "web_url": url})

    @app.delete("/sessions/{sid}")
    async def kill(sid: str, authorization: str | None = Header(default=None)):
        _auth(authorization)
        await manager.kill(sid)
        return JSONResponse({"ok": True, "session_id": sid})

    return app

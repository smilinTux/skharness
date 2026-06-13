# skharness P0 — Session Manager Core

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Repo: `skharness` (new). Run tests from repo root: `~/.skenv/bin/python -m pytest tests/ -q`.

**Goal:** The CI-testable core of skharness — a `Session` model, a json-backed `SessionRegistry`, a `Spawner` seam with a `FakeSpawner`, a `SessionManager`, and a capauth-gated FastAPI gateway (list/spawn/kill/attach) — all unit-tested with no tmux/ttyd/worktrees (the real `TmuxSpawner` is P1).

**Architecture:** `SessionManager` ties `SessionRegistry` (persistence) + a `Spawner` (creates the worker). The `Spawner` is injectable: `FakeSpawner` for CI, `TmuxSpawner` (P1) for real. The `gateway` is a FastAPI app over the manager, gated by an injectable capauth verifier. No hardware/infra needed for P0.

**Tech Stack:** Python 3.10+, `fastapi` + `pydantic` (already in the skenv from skchat/skcomms), `pytest`. Line 99, ruff (E,F,I,N,W; E501 ignored — no `;` one-liners).

**Spec:** `docs/superpowers/specs/2026-06-13-skharness-design.md` (§4 components, §5 testing).

---

## Task 0: Scaffold + editable install

**Files:** Create `pyproject.toml`, `src/skharness/__init__.py`, `tests/__init__.py`.

- [ ] **Step 1:** Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "skharness"
version = "0.1.0"
description = "Sovereign phone-drives-my-agent-swarm harness"
requires-python = ">=3.10"
dependencies = ["fastapi>=0.110", "pydantic>=2.0"]

[project.optional-dependencies]
dev = ["pytest>=7.0", "httpx>=0.27"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.ruff]
line-length = 99
[tool.ruff.lint]
select = ["E", "F", "I", "N", "W"]
ignore = ["E501"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2:** Create `src/skharness/__init__.py`:

```python
"""skharness — sovereign orchestration harness: spawn isolated agent sessions,
drive them from a phone over the tailnet, no Big-Tech broker. P0 = the CI-testable
session-manager core. See docs/superpowers/specs/2026-06-13-skharness-design.md.
"""

__all__ = []
```

and an empty `tests/__init__.py`.

- [ ] **Step 3:** Editable install: `~/.skenv/bin/pip install -e /home/cbrd21/clawd/skcapstone-repos/skharness` → installs skharness + fastapi/httpx if missing. Verify: `~/.skenv/bin/python -c "import skharness; print('ok')"` → `ok`.

- [ ] **Step 4:** `git add -A && git commit -m "feat: scaffold skharness package"` (the repo was `git init`'d; set an initial commit).

---

## Task 1: `Session` model

**Files:** Create `src/skharness/session.py`. Test `tests/test_session.py`.

- [ ] **Step 1: Failing test** — `tests/test_session.py`:

```python
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
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** `session.py`:

```python
"""Session model — one isolated agent worker."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class SessionStatus(str, Enum):
    SPAWNING = "spawning"
    RUNNING = "running"
    ENDED = "ended"


@dataclass(eq=True)
class Session:
    id: str
    agent: str
    repo: str
    worktree: str = ""
    tmux: str = ""
    web_url: str = ""
    status: SessionStatus = SessionStatus.SPAWNING
    created_at: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        d = dict(d)
        d["status"] = SessionStatus(d.get("status", "spawning"))
        known = {"id", "agent", "repo", "worktree", "tmux", "web_url",
                 "status", "created_at"}
        return cls(**{k: v for k, v in d.items() if k in known})
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat: Session model`.

---

## Task 2: `SessionRegistry` (json-backed)

**Files:** Create `src/skharness/registry.py`. Test `tests/test_registry.py`.

- [ ] **Step 1: Failing test** — `tests/test_registry.py`:

```python
from skharness.registry import SessionRegistry
from skharness.session import Session, SessionStatus


def test_add_get_list(tmp_path):
    r = SessionRegistry(path=tmp_path / "sessions.json")
    r.add(Session(id="s1", agent="lumina", repo="/r"))
    assert r.get("s1").agent == "lumina"
    assert len(r.live()) == 1


def test_end_removes_from_live(tmp_path):
    r = SessionRegistry(path=tmp_path / "sessions.json")
    r.add(Session(id="s1", agent="lumina", repo="/r"))
    r.set_status("s1", SessionStatus.ENDED)
    assert r.live() == []
    assert r.get("s1").status == SessionStatus.ENDED


def test_persists(tmp_path):
    p = tmp_path / "sessions.json"
    SessionRegistry(path=p).add(Session(id="s1", agent="a", repo="/r"))
    assert SessionRegistry(path=p).get("s1") is not None
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** `registry.py`:

```python
"""SessionRegistry — track/persist agent sessions (json now; a coord/skmem-pg
adapter is P3 behind this same interface)."""

from __future__ import annotations

import json
from pathlib import Path

from skharness.session import Session, SessionStatus

_DEFAULT = Path.home() / ".skharness" / "sessions.json"


class SessionRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else _DEFAULT
        self._s: dict[str, Session] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for d in raw.get("sessions", []):
            try:
                s = Session.from_dict(d)
                self._s[s.id] = s
            except (TypeError, ValueError):
                continue

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"sessions": [s.to_dict() for s in self._s.values()]},
                       indent=2), encoding="utf-8")

    def add(self, s: Session) -> None:
        self._s[s.id] = s
        self._save()

    def get(self, sid: str) -> Session | None:
        return self._s.get(sid)

    def update(self, s: Session) -> None:
        self._s[s.id] = s
        self._save()

    def set_status(self, sid: str, status: SessionStatus) -> None:
        s = self._s.get(sid)
        if s is not None:
            s.status = status
            self._save()

    def live(self) -> list[Session]:
        return [s for s in self._s.values() if s.status != SessionStatus.ENDED]
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat: json-backed SessionRegistry`.

---

## Task 3: `Spawner` seam + `FakeSpawner`

**Files:** Create `src/skharness/spawner.py`. Test `tests/test_spawner.py`.

- [ ] **Step 1: Failing test** — `tests/test_spawner.py`:

```python
import pytest

from skharness.spawner import FakeSpawner
from skharness.session import Session


@pytest.mark.asyncio
async def test_fake_spawn_returns_worktree_tmux_weburl():
    sp = FakeSpawner()
    s = Session(id="s1", agent="lumina", repo="/r")
    out = await sp.spawn(s, prompt="do x")
    assert out.worktree.endswith("s1")
    assert out.tmux == "sk-s1"
    assert out.web_url.startswith("http")
    assert sp.spawned == ["s1"]


@pytest.mark.asyncio
async def test_fake_kill_records():
    sp = FakeSpawner()
    await sp.kill("s1")
    assert sp.killed == ["s1"]
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** `spawner.py`:

```python
"""Spawner seam (spec §4): create/kill a worker. FakeSpawner for CI; TmuxSpawner
(P1) does git-worktree + tmux + ttyd/sshx for real."""

from __future__ import annotations

from abc import ABC, abstractmethod

from skharness.session import Session


class Spawner(ABC):
    @abstractmethod
    async def spawn(self, session: Session, *, prompt: str) -> Session:
        """Create the worker (worktree + tmux + web-terminal); return the session
        with worktree/tmux/web_url filled in."""

    @abstractmethod
    async def kill(self, session_id: str) -> None: ...


class FakeSpawner(Spawner):
    """In-memory spawner for CI — records calls, returns deterministic handles."""

    def __init__(self, base_url: str = "http://tailnet:7700") -> None:
        self._base = base_url
        self.spawned: list[str] = []
        self.killed: list[str] = []

    async def spawn(self, session: Session, *, prompt: str) -> Session:
        session.worktree = f"/wt/{session.id}"
        session.tmux = f"sk-{session.id}"
        session.web_url = f"{self._base}/t/{session.id}"
        self.spawned.append(session.id)
        return session

    async def kill(self, session_id: str) -> None:
        self.killed.append(session_id)
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat: Spawner seam + FakeSpawner`.

---

## Task 4: `SessionManager`

**Files:** Create `src/skharness/manager.py`. Test `tests/test_manager.py`.

- [ ] **Step 1: Failing test** — `tests/test_manager.py`:

```python
import pytest

from skharness.manager import SessionManager
from skharness.registry import SessionRegistry
from skharness.session import SessionStatus
from skharness.spawner import FakeSpawner


def _mgr(tmp_path):
    return SessionManager(registry=SessionRegistry(path=tmp_path / "s.json"),
                          spawner=FakeSpawner())


@pytest.mark.asyncio
async def test_spawn_registers_running_session_with_attach_url(tmp_path):
    m = _mgr(tmp_path)
    s = await m.spawn(agent="lumina", prompt="do x", repo="/r")
    assert s.status == SessionStatus.RUNNING
    assert s.web_url.startswith("http")
    assert m.attach_url(s.id) == s.web_url
    assert any(x.id == s.id for x in m.list())


@pytest.mark.asyncio
async def test_kill_ends_session(tmp_path):
    m = _mgr(tmp_path)
    s = await m.spawn(agent="a", prompt="p", repo="/r")
    await m.kill(s.id)
    assert m.registry.get(s.id).status == SessionStatus.ENDED
    assert m.list() == []


@pytest.mark.asyncio
async def test_attach_unknown_returns_none(tmp_path):
    assert _mgr(tmp_path).attach_url("nope") is None
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** `manager.py`:

```python
"""SessionManager — ties registry + spawner. Spawn creates an isolated worker and
registers it RUNNING; kill tears it down and marks it ENDED."""

from __future__ import annotations

import os
import time

from skharness.registry import SessionRegistry
from skharness.session import Session, SessionStatus
from skharness.spawner import Spawner


class SessionManager:
    def __init__(self, *, registry: SessionRegistry, spawner: Spawner) -> None:
        self.registry = registry
        self.spawner = spawner

    async def spawn(self, *, agent: str, prompt: str, repo: str) -> Session:
        sid = os.urandom(6).hex()
        s = Session(id=sid, agent=agent, repo=repo, created_at=time.time())
        self.registry.add(s)
        s = await self.spawner.spawn(s, prompt=prompt)
        s.status = SessionStatus.RUNNING
        self.registry.update(s)
        return s

    async def kill(self, session_id: str) -> None:
        await self.spawner.kill(session_id)
        self.registry.set_status(session_id, SessionStatus.ENDED)

    def list(self) -> list[Session]:
        return self.registry.live()

    def attach_url(self, session_id: str) -> str | None:
        s = self.registry.get(session_id)
        return s.web_url if s and s.status != SessionStatus.ENDED else None
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat: SessionManager`.

---

## Task 5: capauth-gated FastAPI gateway

**Files:** Create `src/skharness/gateway.py`. Test `tests/test_gateway.py`.

- [ ] **Step 1: Failing test** — `tests/test_gateway.py`:

```python
from fastapi.testclient import TestClient

from skharness.gateway import build_app
from skharness.manager import SessionManager
from skharness.registry import SessionRegistry
from skharness.spawner import FakeSpawner


def _client(tmp_path, *, verifier=lambda token: token == "good"):
    mgr = SessionManager(registry=SessionRegistry(path=tmp_path / "s.json"),
                         spawner=FakeSpawner())
    return TestClient(build_app(manager=mgr, verify_caller=verifier))


def test_spawn_list_attach_kill_flow(tmp_path):
    c = _client(tmp_path)
    h = {"authorization": "Bearer good"}
    r = c.post("/sessions", json={"agent": "lumina", "prompt": "do x", "repo": "/r"},
               headers=h)
    assert r.status_code == 200
    sid = r.json()["id"]
    assert r.json()["status"] == "running"

    assert any(s["id"] == sid for s in c.get("/sessions", headers=h).json()["sessions"])
    att = c.get(f"/sessions/{sid}/attach", headers=h)
    assert att.status_code == 200 and att.json()["web_url"].startswith("http")
    assert c.delete(f"/sessions/{sid}", headers=h).status_code == 200
    assert c.get("/sessions", headers=h).json()["sessions"] == []


def test_unauthenticated_rejected(tmp_path):
    c = _client(tmp_path)
    assert c.get("/sessions").status_code == 401                       # no token
    assert c.get("/sessions", headers={"authorization": "Bearer bad"}).status_code == 403
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** `gateway.py`:

```python
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
        token = authorization[len("Bearer "):]
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
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat: capauth-gated FastAPI gateway`.

---

## Final verification

- [ ] **Full suite:** `~/.skenv/bin/python -m pytest tests/ -q` → all pass.
- [ ] **Lint:** `~/.skenv/bin/ruff check src/skharness/ tests/` → no errors.

## What P0 delivers

The CI-tested heart of skharness: spawn an (isolated, via the seam) agent session,
register it, get its web-terminal attach url, list and kill sessions — all behind a
capauth-gated tailnet REST gateway, with zero tmux/ttyd/worktree dependency (the
`FakeSpawner` stands in). **P1** swaps in the real `TmuxSpawner` (git worktree + tmux
+ ttyd/sshx) on `.158`; **P2** is the Flutter session-switcher; **P3** wires `pi` +
the coord board. The sovereign "phone drives my swarm" — built on what SKWorld owns.

> **P1 input-validation requirement (RCE guard):** the real `TmuxSpawner` feeds
> `repo`/`agent` into `git worktree`/`tmux`/`ttyd`. It MUST use argv-list `subprocess`
> (no `shell=True`), allow-list `repo` roots, and constrain `agent`/session names to
> `[A-Za-z0-9_-]+`. See spec §7 "P1 TmuxSpawner mandate" — capauth gating is not a
> substitute for validating this input.

# skcode P0 - Read-only Code MVP (skcode-hostd on .158)

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:test-driven-development for every task and superpowers:subagent-driven-development to execute the plan. Repo: `skharness` (extend, do NOT fork). Run tests from the repo root `/home/cbrd21/clawd/skcapstone-repos/skharness` with:
>
> ```bash
> ~/.skenv/bin/python -m pytest tests/ -q
> ```
>
> Lint with: `~/.skenv/bin/ruff check src/skharness/ tests/`

**Goal:** Ship the P0 MVP from spec §8.1: read-only Code on ONE host (.158). List the live claude-code agent sessions and stream one of them read-only. No inject, no dispatch, no model switch, no kill. Concretely: grow skharness with a `Harness` interface (the read-only subset of spec §3.1: `list_sessions()` + `stream(sid)`), a `claude-code` PTY/tmux adapter that lists + tails the `skchat-agents` tmux windows that jarvis-heartbeat and skharness already create plus the historical `~/.skcapstone/agents/<agent>/sessions/` dirs, a `FakeHarness` for CI, typed `SessionEvent` and `HarnessSession` models, the `skcode-hostd` daemon (promoted skharness gateway) exposing `GET /api/v1/sessions`, `GET /api/v1/sessions/{sid}`, and `WS /api/v1/sessions/{sid}/stream` capauth-gated with ZERO write surface, and a single self-contained static web page client. The daemon boots and is usable with zero shell.

**Architecture:** The daemon (`daemon.py`, the promoted skharness `gateway.py`) is the trust boundary. It owns one `Harness` instance and exposes only read-only, capauth-gated `/api/v1` routes over it. The `Harness` ABC is the read-only subset of the spec §3.1 seam (`list_sessions`, `stream`); `ClaudeCodeHarness` implements it over tmux + the historical sessions dir with an injectable `runner` (so unit tests never touch real tmux), and `FakeHarness` is the CI double that drives every daemon test. The capauth gate is extracted from the existing `gateway.py` into a shared `auth.py` and reused by both apps (no reimplementation). `serve.py` binds a Tailscale IP only (refuses `0.0.0.0`) on port `:9390`. The static client (`client/index.html`) talks ONLY to the daemon API. There is no spawn/inject/kill code path in this MVP: the `Session`/`SessionManager`/`Spawner` machinery is left untouched and simply not wired into the daemon, so the write surface does not exist.

**Tech Stack:** Python 3.10+ (`requires-python = ">=3.10"`). `fastapi` (0.135 in skenv) + `pydantic` (runtime). `uvicorn` + `websockets` for serving (both already in skenv). Tests: `pytest` (9.0.2) + `pytest-asyncio` (1.3.0, explicit `@pytest.mark.asyncio` marks, matching the existing suite) + `httpx` + FastAPI `TestClient` (its `websocket_connect` covers the WS route). No new heavy deps. Interpreter and tools: `~/.skenv/bin/python`, `~/.skenv/bin/pytest`, `~/.skenv/bin/ruff`, `~/.skenv/bin/pip`. Ruff: line-length 99, lint `E,F,I,N,W`, ignore `E501` (matches the existing `pyproject.toml`).

## Global Constraints (verbatim, non-negotiable)

- **NO em dashes and NO en dashes** anywhere: not in code, comments, docstrings, the static page, docs, or commit messages. Use commas, colons, parentheses, or a new sentence for asides. Regular hyphens `-` are fine (ranges like `9384-9389`, `[A-Za-z0-9_-]+`).
- **Read-only ONLY.** No endpoint may spawn, inject, kill, rename, archive, set a model, or write anything. The daemon exposes exactly three data routes (`GET /api/v1/sessions`, `GET /api/v1/sessions/{sid}`, `WS /api/v1/sessions/{sid}/stream`) plus the static client route. The security review is a SEPARATE later task; P0 must present zero write surface, and a test asserts write methods return 405/404.
- **TDD, skharness convention.** Every task: write a failing test, run it and see it fail, implement, run and see it pass, commit. Tests run from the repo root with `~/.skenv/bin/python -m pytest tests/ -q`. The `Harness` seam means the daemon + routes are tested with `FakeHarness` (no real tmux or claude in unit tests). The `ClaudeCodeHarness` is tested with an injected fake `runner` (no real tmux subprocess).
- **Reuse, do not rebuild.** Reuse skharness's existing `SessionRegistry`, `SessionManager`, `Spawner`, `Session` (leave them untouched, unused by the read-only daemon) and the capauth gate (extract the existing `gateway.py` bearer check into `auth.py` and have BOTH `gateway.py` and `daemon.py` use it). Do NOT start a new repo. Do NOT reimplement the registry or the gate.
- **Port `:9390`** for `skcode-hostd` HTTP/WS. **CONFIRMED NOT FREE at authoring time:** `skcomms.transports.broker_server` currently binds `0.0.0.0:9390` on this host (pid seen 2026-07-25). Because skcode binds a Tailscale IP only, a wildcard `0.0.0.0:9390` listener still owns the tailnet-IP:9390 address, so this is a hard conflict that a human must resolve before deploy (move the skcomms broker off 9390, or pick the next free port for skcode-hostd and record it in `~/.skcapstone/docs/PORTS.md`). The code keeps `9390` as the documented default and the value is overridable via `--port`; this conflict is a DEPLOY-TIME gate, not a code blocker, and is called out again in the Self-Review.
- **Standalone.** `skcode-hostd` boots and is fully usable with zero shell: its own capauth gate plus the self-contained static client. The static HTML page has no build step and no external asset fetches.
- **Bind a Tailscale IP only, never `0.0.0.0`, never a public Funnel port** (reuses the skharness rule and the sk-access `server.py` precedent). `serve.py` refuses `0.0.0.0`/`::`/empty.

**Spec:** `/home/cbrd21/clawd/docs/superpowers/specs/2026-07-25-skcode-remote-control-dispatch-design.md` (§1.1 reuse inventory, §3.1 Harness + SessionEvent, §4.2 API surface, §4.3 registry + stream, §8.1 MVP scope). Prior plan for the reused core: `docs/superpowers/plans/2026-06-13-skharness-p0-session-core.md`.

---

## File Structure (mapped onto the real skharness tree)

Existing files (READ, reused, mostly untouched):

```
src/skharness/
  __init__.py        # package docstring; __all__ = [] (leave)
  session.py         # Session, SessionStatus dataclass (UNCHANGED; not wired into daemon)
  registry.py        # SessionRegistry json store (UNCHANGED; available, not wired)
  spawner.py         # Spawner ABC + FakeSpawner (UNCHANGED; not wired)
  manager.py         # SessionManager (UNCHANGED; not wired)
  gateway.py         # existing capauth-gated FastAPI (REFACTORED in Task 2 to import auth.py; behavior identical)
tests/
  test_session.py test_registry.py test_spawner.py test_manager.py test_gateway.py  # (stay green)
```

New files created by this plan:

```
src/skharness/
  events.py                    # Task 1: EventType, SessionEvent
  auth.py                      # Task 2: Verifier, require_bearer, check_token (extracted gate)
  harness.py                   # Task 3: HarnessSession, Harness ABC (read-only subset), FakeHarness
  harnesses/__init__.py        # Task 4: (empty package marker)
  harnesses/claude_code.py     # Task 4: ClaudeCodeHarness (tmux + historical dir, injectable runner)
  daemon.py                    # Task 5: build_daemon_app (3 read-only /api/v1 routes + static)
  serve.py                     # Task 6: resolve_bind (refuse 0.0.0.0), main()
  __main__.py                  # Task 6: python -m skharness -> serve.main()
  client/index.html            # Task 7: self-contained read-only web client
tests/
  test_events.py               # Task 1
  test_auth.py                 # Task 2
  test_harness.py              # Task 3
  test_claude_code_harness.py  # Task 4
  test_daemon.py               # Task 5
  test_serve.py                # Task 6
  test_client.py               # Task 7
docs/superpowers/plans/2026-07-25-skcode-p0-readonly-mvp.md   # this file
```

---

## Task 0: Dependencies + packaging update

**Files:** Edit `pyproject.toml`. Create `src/skharness/harnesses/__init__.py` and `src/skharness/client/` (the client dir; `index.html` is written in Task 7, but the dir must exist and be packaged).

- [ ] **Step 1:** Edit `pyproject.toml`. Add serving runtime deps, declare `pytest-asyncio` in dev (it is used by the existing suite but was never declared), and add package-data so `client/index.html` ships. Replace the whole file with:

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "skharness"
version = "0.2.0"
description = "Sovereign phone-drives-my-agent-swarm harness (skcode-hostd)"
requires-python = ">=3.10"
dependencies = ["fastapi>=0.110", "pydantic>=2.0", "uvicorn>=0.27", "websockets>=12.0"]

[project.optional-dependencies]
dev = ["pytest>=7.0", "pytest-asyncio>=0.23", "httpx>=0.27"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
skharness = ["client/*.html"]

[tool.ruff]
line-length = 99
[tool.ruff.lint]
select = ["E", "F", "I", "N", "W"]
ignore = ["E501"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2:** Create the package dirs and markers:

```bash
mkdir -p /home/cbrd21/clawd/skcapstone-repos/skharness/src/skharness/harnesses
mkdir -p /home/cbrd21/clawd/skcapstone-repos/skharness/src/skharness/client
```

Create `src/skharness/harnesses/__init__.py` with exactly:

```python
"""skcode harness adapters (claude-code PTY/tmux for the P0 MVP)."""
```

- [ ] **Step 3:** Reinstall editable so the new deps + package-data register:

```bash
~/.skenv/bin/pip install -e /home/cbrd21/clawd/skcapstone-repos/skharness
```

Verify the deps import: `~/.skenv/bin/python -c "import fastapi, uvicorn, websockets, pytest_asyncio; print('ok')"` prints `ok`.

- [ ] **Step 4:** Confirm the existing suite still passes (nothing changed behaviorally):

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/ -q
```

Expect `20 passed`.

- [ ] **Step 5:** Commit:

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && git add -A && git commit -m "chore(skcode-p0): bump to 0.2.0, add serving deps + harnesses/client packaging"
```

Commit message trailer (append to every commit in this plan):

```
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## Task 1: `SessionEvent` + `EventType` (spec §3.1 typed stream)

**Files:** Create `src/skharness/events.py`. Test `tests/test_events.py`.

**Interfaces defined here (used by Tasks 3, 4, 5, 7):**

```python
class EventType(str, Enum):
    STATUS = "status"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    DIFF = "diff"
    NEEDS_INPUT = "needs_input"

@dataclass
class SessionEvent:
    type: EventType
    text: str = ""
    ts: float = 0.0
    data: dict = field(default_factory=dict)
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "SessionEvent": ...
```

- [ ] **Step 1: Failing test** - write `tests/test_events.py`:

```python
from skharness.events import EventType, SessionEvent


def test_event_defaults():
    e = SessionEvent(type=EventType.ASSISTANT_TEXT, text="hello")
    assert e.type == EventType.ASSISTANT_TEXT
    assert e.text == "hello"
    assert e.ts == 0.0
    assert e.data == {}


def test_event_to_dict_serializes_enum_value():
    e = SessionEvent(type=EventType.NEEDS_INPUT, text="approve?", ts=12.5,
                     data={"kind": "tool"})
    d = e.to_dict()
    assert d == {"type": "needs_input", "text": "approve?", "ts": 12.5,
                 "data": {"kind": "tool"}}


def test_event_dict_roundtrip():
    e = SessionEvent(type=EventType.STATUS, text="attached", ts=1.0)
    assert SessionEvent.from_dict(e.to_dict()) == e


def test_event_data_is_not_shared_between_instances():
    a = SessionEvent(type=EventType.STATUS)
    b = SessionEvent(type=EventType.STATUS)
    a.data["x"] = 1
    assert b.data == {}
```

- [ ] **Step 2: Run and see it FAIL:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_events.py -q
```

- [ ] **Step 3: Implement** `src/skharness/events.py`:

```python
"""SessionEvent - the typed, ordered event a Harness stream emits (spec 3.1).

Read-only in the P0 MVP: assistant text deltas, tool calls/results, diffs,
status transitions, and needs-input markers. No write payloads exist here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class EventType(str, Enum):
    STATUS = "status"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    DIFF = "diff"
    NEEDS_INPUT = "needs_input"


@dataclass
class SessionEvent:
    type: EventType
    text: str = ""
    ts: float = 0.0
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SessionEvent":
        d = dict(d)
        d["type"] = EventType(d.get("type", "status"))
        known = {"type", "text", "ts", "data"}
        return cls(**{k: v for k, v in d.items() if k in known})
```

- [ ] **Step 4: Run and see it PASS:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_events.py -q
```

- [ ] **Step 5: Commit:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && git add -A && git commit -m "feat(skcode-p0): typed SessionEvent + EventType"
```

---

## Task 2: Extract the capauth gate into `auth.py` (reuse, not rebuild)

**Files:** Create `src/skharness/auth.py`. Test `tests/test_auth.py`. Refactor `src/skharness/gateway.py` to import from `auth.py` (behavior identical, existing `tests/test_gateway.py` stays green).

**Interfaces defined here (used by Tasks 5 and, refactored, by `gateway.py`):**

```python
Verifier = Callable[[str], bool]
def require_bearer(authorization: str | None, verify_caller: Verifier) -> str:
    """Raise HTTPException(401) on missing/empty/non-Bearer, HTTPException(403)
    on a token the verifier rejects. Returns the validated token on success."""
def check_token(token: str | None, verify_caller: Verifier) -> bool:
    """Fail-closed token check for the WS query-param path: empty/whitespace
    token is False before the verifier is consulted."""
```

- [ ] **Step 1: Failing test** - write `tests/test_auth.py`:

```python
import pytest
from fastapi import HTTPException

from skharness.auth import check_token, require_bearer


def _v(token):
    return token == "good"


def test_require_bearer_accepts_good_token_and_returns_it():
    assert require_bearer("Bearer good", _v) == "good"


def test_require_bearer_missing_header_is_401():
    with pytest.raises(HTTPException) as ei:
        require_bearer(None, _v)
    assert ei.value.status_code == 401


def test_require_bearer_empty_token_is_401_before_verifier():
    # A verifier that accepts everything must still not see an empty token.
    with pytest.raises(HTTPException) as ei:
        require_bearer("Bearer ", lambda token: True)
    assert ei.value.status_code == 401


def test_require_bearer_non_bearer_scheme_is_401():
    with pytest.raises(HTTPException) as ei:
        require_bearer("Basic xyz", _v)
    assert ei.value.status_code == 401


def test_require_bearer_rejected_token_is_403():
    with pytest.raises(HTTPException) as ei:
        require_bearer("Bearer bad", _v)
    assert ei.value.status_code == 403


def test_check_token_fail_closed_on_empty():
    assert check_token("", lambda token: True) is False
    assert check_token(None, lambda token: True) is False
    assert check_token("   ", lambda token: True) is False


def test_check_token_true_only_when_verifier_accepts():
    assert check_token("good", _v) is True
    assert check_token("bad", _v) is False
```

- [ ] **Step 2: Run and see it FAIL:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_auth.py -q
```

- [ ] **Step 3: Implement** `src/skharness/auth.py`:

```python
"""Capauth bearer gate, shared by the skharness gateway and skcode-hostd.

`verify_caller` is the auth seam: a real capauth verifier in production, a fake
in tests. Fail closed on missing or empty tokens BEFORE the verifier runs.
"""

from __future__ import annotations

from typing import Callable

from fastapi import HTTPException

Verifier = Callable[[str], bool]


def require_bearer(authorization: str | None, verify_caller: Verifier) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(401, "missing bearer token")
    if not verify_caller(token):
        raise HTTPException(403, "unauthorized")
    return token


def check_token(token: str | None, verify_caller: Verifier) -> bool:
    if not token or not token.strip():
        return False
    return bool(verify_caller(token.strip()))
```

- [ ] **Step 4: Run and see it PASS:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_auth.py -q
```

- [ ] **Step 5: Refactor `gateway.py` to reuse the gate.** Replace the whole file `src/skharness/gateway.py` with (only the auth block changes; routes are byte-for-byte the same behavior):

```python
"""skharness gateway - capauth-gated FastAPI over the SessionManager. Bind to a
Tailscale IP only (never a public port). The bearer gate lives in auth.py and is
shared with skcode-hostd.
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from skharness.auth import Verifier, require_bearer
from skharness.manager import SessionManager


def build_app(*, manager: SessionManager, verify_caller: Verifier) -> FastAPI:
    app = FastAPI(title="skharness")

    def _auth(authorization: str | None) -> None:
        require_bearer(authorization, verify_caller)

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

- [ ] **Step 6: Run the FULL suite and see gateway tests stay green:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/ -q
```

Expect all prior tests plus `test_auth.py` and `test_events.py` passing.

- [ ] **Step 7: Commit:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && git add -A && git commit -m "refactor(skcode-p0): extract shared capauth bearer gate into auth.py"
```

---

## Task 3: `Harness` seam (read-only subset) + `HarnessSession` + `FakeHarness`

**Files:** Create `src/skharness/harness.py`. Test `tests/test_harness.py`.

This is the spec §3.1 interface reduced to the read-only P0 subset: `list_sessions()` and `stream(sid)`. The write methods from §3.1 (`inject`, `spawn`, `set_model`, `archive`, ...) are DELIBERATELY absent in P0 so no write surface can be wired. `stream` is declared as a plain method returning an `AsyncIterator[SessionEvent]` and implemented as an async generator.

**Interfaces defined here (used by Tasks 4, 5):**

```python
@dataclass
class HarnessSession:
    id: str
    harness: str                 # "claude-code" | "fake"
    agent: str
    host: str                    # node id, e.g. ".158"
    branch: str = ""
    model: str = ""
    status: str = "running"      # "running" | "idle" | "ended"
    last_message: str = ""
    last_event_at: float = 0.0
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "HarnessSession": ...

class Harness(ABC):
    name: str
    @abstractmethod
    async def list_sessions(self) -> list[HarnessSession]: ...
    @abstractmethod
    def stream(self, sid: str) -> AsyncIterator[SessionEvent]: ...

class FakeHarness(Harness):
    name = "fake"
    def __init__(self, *, sessions: list[HarnessSession] | None = None,
                 events: dict[str, list[SessionEvent]] | None = None) -> None: ...
```

- [ ] **Step 1: Failing test** - write `tests/test_harness.py`:

```python
import pytest

from skharness.events import EventType, SessionEvent
from skharness.harness import FakeHarness, Harness, HarnessSession


def test_harness_session_to_dict_and_roundtrip():
    hs = HarnessSession(id="lumina-abc123", harness="claude-code", agent="lumina",
                        host=".158", branch="main", model="ornith-tiny",
                        status="running", last_message="working on it",
                        last_event_at=42.0)
    d = hs.to_dict()
    assert d["id"] == "lumina-abc123"
    assert d["harness"] == "claude-code"
    assert d["status"] == "running"
    assert HarnessSession.from_dict(d) == hs


def test_harness_session_defaults():
    hs = HarnessSession(id="s1", harness="fake", agent="a", host=".158")
    assert hs.branch == "" and hs.model == "" and hs.status == "running"
    assert hs.last_message == "" and hs.last_event_at == 0.0


def test_fake_is_a_harness_and_has_no_write_methods():
    fh = FakeHarness()
    assert isinstance(fh, Harness)
    assert fh.name == "fake"
    # P0 read-only: none of the spec 3.1 write controls exist on the seam.
    for forbidden in ("inject", "spawn", "set_model", "archive", "kill", "fork"):
        assert not hasattr(fh, forbidden)


@pytest.mark.asyncio
async def test_fake_list_sessions_returns_seeded():
    seeded = [HarnessSession(id="s1", harness="fake", agent="lumina", host=".158")]
    fh = FakeHarness(sessions=seeded)
    got = await fh.list_sessions()
    assert [s.id for s in got] == ["s1"]


@pytest.mark.asyncio
async def test_fake_stream_yields_seeded_events_in_order():
    evs = [SessionEvent(type=EventType.STATUS, text="attached"),
           SessionEvent(type=EventType.ASSISTANT_TEXT, text="hello")]
    fh = FakeHarness(
        sessions=[HarnessSession(id="s1", harness="fake", agent="a", host=".158")],
        events={"s1": evs},
    )
    out = [e async for e in fh.stream("s1")]
    assert [e.text for e in out] == ["attached", "hello"]


@pytest.mark.asyncio
async def test_fake_stream_unknown_sid_yields_nothing():
    fh = FakeHarness()
    out = [e async for e in fh.stream("nope")]
    assert out == []
```

- [ ] **Step 2: Run and see it FAIL:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_harness.py -q
```

- [ ] **Step 3: Implement** `src/skharness/harness.py`:

```python
"""Harness seam - the read-only subset of the skcode control surface (spec 3.1).

P0 MVP deliberately exposes ONLY discovery + read streaming:
`list_sessions()` and `stream(sid)`. The write controls from the full spec
(inject, spawn, set_model, archive, fork) are absent so the daemon cannot wire a
write path. FakeHarness is the CI double that drives every daemon test.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import AsyncIterator

from skharness.events import SessionEvent


@dataclass
class HarnessSession:
    id: str
    harness: str
    agent: str
    host: str
    branch: str = ""
    model: str = ""
    status: str = "running"
    last_message: str = ""
    last_event_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HarnessSession":
        known = {"id", "harness", "agent", "host", "branch", "model", "status",
                 "last_message", "last_event_at"}
        return cls(**{k: v for k, v in d.items() if k in known})


class Harness(ABC):
    """One interface per host-local runtime. P0 read-only subset."""

    name: str = "harness"

    @abstractmethod
    async def list_sessions(self) -> list[HarnessSession]:
        """Live (and, for claude-code, historical) sessions on THIS host."""

    @abstractmethod
    def stream(self, sid: str) -> AsyncIterator[SessionEvent]:
        """Return an async iterator of ordered read-only SessionEvents."""


class FakeHarness(Harness):
    """In-memory harness for CI: seeded sessions + seeded per-sid event lists."""

    name = "fake"

    def __init__(
        self,
        *,
        sessions: list[HarnessSession] | None = None,
        events: dict[str, list[SessionEvent]] | None = None,
    ) -> None:
        self._sessions = list(sessions) if sessions else []
        self._events = dict(events) if events else {}

    async def list_sessions(self) -> list[HarnessSession]:
        return list(self._sessions)

    async def stream(self, sid: str) -> AsyncIterator[SessionEvent]:
        for ev in self._events.get(sid, []):
            yield ev
```

- [ ] **Step 4: Run and see it PASS:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_harness.py -q
```

- [ ] **Step 5: Commit:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && git add -A && git commit -m "feat(skcode-p0): read-only Harness seam + HarnessSession + FakeHarness"
```

---

## Task 4: `ClaudeCodeHarness` (tmux + historical dir, injectable runner)

**Files:** Create `src/skharness/harnesses/claude_code.py`. Test `tests/test_claude_code_harness.py`.

Implements the read-only `Harness` over the exact machinery jarvis-heartbeat + skharness already use: the `skchat-agents` tmux session, windows named `<agent>-<short_id>` (plus a `monitor` window that is skipped), tailed via `tmux capture-pane -p`, and the historical `~/.skcapstone/agents/<agent>/sessions/` dirs. All tmux calls go through an injectable `runner: Callable[[list[str]], str]` (argv list, never a shell string) so unit tests use a fake runner and never spawn tmux. Session id for a live session IS its tmux window name (unique within `skchat-agents`).

**Interfaces defined here (used by Tasks 5, 6):**

```python
Runner = Callable[[list[str]], str]   # run an argv list, return stdout; never shell=True

_SID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

def parse_windows(out: str, *, host: str) -> list[HarnessSession]:
    """Parse `tmux list-windows -F '#{window_name}\t#{window_activity}'` output.
    Skips the 'monitor' window and blank lines. agent = window name up to the
    last '-'. status defaults 'running'."""

def scan_historical(sessions_root: Path, *, host: str, limit: int = 50) -> list[HarnessSession]:
    """Enumerate ~/.skcapstone/agents/<agent>/sessions/*.json (or *.jsonl) as
    ended HarnessSessions (status='ended'), newest first, capped at `limit`."""

def new_lines(prev: str, cur: str) -> list[str]:
    """Lines present in `cur` beyond the common prefix of `prev` (naive tail
    diff for the read-only capture-pane poll)."""

class ClaudeCodeHarness(Harness):
    name = "claude-code"
    def __init__(self, *, host: str = ".158", tmux_session: str = "skchat-agents",
                 sessions_root: Path | None = None, runner: Runner | None = None,
                 poll_interval: float = 1.0, max_polls: int | None = None) -> None: ...
    async def list_sessions(self) -> list[HarnessSession]: ...
    async def stream(self, sid: str) -> AsyncIterator[SessionEvent]: ...
```

- [ ] **Step 1: Failing test** - write `tests/test_claude_code_harness.py`:

```python
import json

import pytest

from skharness.events import EventType
from skharness.harness import Harness
from skharness.harnesses.claude_code import (
    ClaudeCodeHarness,
    new_lines,
    parse_windows,
    scan_historical,
)


def test_parse_windows_skips_monitor_and_blanks():
    out = "monitor\t1700000000\nlumina-abc12345\t1700000100\nopus-deadbeef\t1700000200\n\n"
    got = parse_windows(out, host=".158")
    ids = [s.id for s in got]
    assert ids == ["lumina-abc12345", "opus-deadbeef"]
    assert got[0].agent == "lumina"
    assert got[0].harness == "claude-code"
    assert got[0].host == ".158"
    assert got[0].status == "running"
    assert got[0].last_event_at == 1700000100.0


def test_parse_windows_handles_missing_activity_field():
    got = parse_windows("lumina-abc12345\n", host=".158")
    assert got[0].id == "lumina-abc12345"
    assert got[0].last_event_at == 0.0


def test_new_lines_returns_only_the_tail():
    assert new_lines("a\nb\n", "a\nb\nc\nd\n") == ["c", "d"]
    assert new_lines("", "x\ny\n") == ["x", "y"]
    assert new_lines("a\nb\n", "a\nb\n") == []


def test_scan_historical_reads_agent_session_dirs(tmp_path):
    root = tmp_path / "agents"
    sdir = root / "lumina" / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "sess-001.json").write_text(json.dumps({"id": "sess-001"}))
    (sdir / "sess-002.json").write_text(json.dumps({"id": "sess-002"}))
    got = scan_historical(root, host=".158")
    assert {s.id for s in got} == {"lumina/sess-001", "lumina/sess-002"}
    assert all(s.status == "ended" for s in got)
    assert all(s.agent == "lumina" and s.harness == "claude-code" for s in got)


def test_scan_historical_missing_root_is_empty(tmp_path):
    assert scan_historical(tmp_path / "nope", host=".158") == []


def test_is_a_harness():
    assert isinstance(ClaudeCodeHarness(runner=lambda argv: ""), Harness)
    assert ClaudeCodeHarness(runner=lambda argv: "").name == "claude-code"


@pytest.mark.asyncio
async def test_list_sessions_merges_live_then_historical(tmp_path):
    root = tmp_path / "agents"
    sdir = root / "lumina" / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "old.json").write_text("{}")

    def runner(argv):
        assert argv[0] == "tmux"
        assert "list-windows" in argv
        return "monitor\t1\nlumina-abc12345\t1700000100\n"

    h = ClaudeCodeHarness(runner=runner, sessions_root=root, host=".158")
    got = await h.list_sessions()
    ids = [s.id for s in got]
    assert ids[0] == "lumina-abc12345"          # live first
    assert "lumina/old" in ids                   # historical after
    assert got[0].status == "running"


@pytest.mark.asyncio
async def test_stream_emits_status_then_new_capture_lines(tmp_path):
    captures = iter([
        "line1\nline2\n",          # first poll
        "line1\nline2\nline3\n",   # second poll adds line3
    ])

    def runner(argv):
        if "capture-pane" in argv:
            return next(captures)
        return ""

    h = ClaudeCodeHarness(runner=runner, sessions_root=tmp_path, host=".158",
                          poll_interval=0.0, max_polls=2)
    out = [e async for e in h.stream("lumina-abc12345")]
    assert out[0].type == EventType.STATUS
    texts = [e.text for e in out if e.type == EventType.ASSISTANT_TEXT]
    assert texts == ["line1", "line2", "line3"]


@pytest.mark.asyncio
async def test_stream_rejects_bad_sid_charset(tmp_path):
    h = ClaudeCodeHarness(runner=lambda argv: "", sessions_root=tmp_path,
                          max_polls=1)
    out = [e async for e in h.stream("bad;name$(x)")]
    assert len(out) == 1
    assert out[0].type == EventType.STATUS
    assert "invalid" in out[0].text.lower()
```

- [ ] **Step 2: Run and see it FAIL:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_claude_code_harness.py -q
```

- [ ] **Step 3: Implement** `src/skharness/harnesses/claude_code.py`:

```python
"""claude-code harness adapter (read-only P0).

Lists + tails the sessions that jarvis-heartbeat and skharness already create:
the `skchat-agents` tmux session with windows named `<agent>-<short_id>` (the
`monitor` window is skipped), tailed with `tmux capture-pane -p`, plus the
historical `~/.skcapstone/agents/<agent>/sessions/` dirs (spec 1.1).

All tmux calls go through an injectable argv `runner` (never shell=True), so
unit tests use a fake runner and never touch real tmux. Read-only: this adapter
has no spawn/inject/kill path.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from pathlib import Path
from typing import AsyncIterator, Callable

from skharness.events import EventType, SessionEvent
from skharness.harness import Harness, HarnessSession

Runner = Callable[[list[str]], str]

_SID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HARNESS = "claude-code"
_MONITOR_WINDOW = "monitor"
_DEFAULT_SESSIONS_ROOT = Path.home() / ".skcapstone" / "agents"


def _default_runner(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    return proc.stdout


def parse_windows(out: str, *, host: str) -> list[HarnessSession]:
    sessions: list[HarnessSession] = []
    for line in out.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        name = parts[0].strip()
        if not name or name == _MONITOR_WINDOW:
            continue
        activity = 0.0
        if len(parts) > 1 and parts[1].strip():
            try:
                activity = float(parts[1].strip())
            except ValueError:
                activity = 0.0
        agent = name.rsplit("-", 1)[0] if "-" in name else name
        sessions.append(HarnessSession(
            id=name, harness=_HARNESS, agent=agent, host=host,
            status="running", last_event_at=activity,
        ))
    return sessions


def scan_historical(sessions_root: Path, *, host: str, limit: int = 50) -> list[HarnessSession]:
    root = Path(sessions_root)
    if not root.exists():
        return []
    found: list[tuple[float, HarnessSession]] = []
    for agent_dir in sorted(root.iterdir()):
        sdir = agent_dir / "sessions"
        if not sdir.is_dir():
            continue
        for f in sdir.iterdir():
            if f.suffix not in (".json", ".jsonl"):
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                mtime = 0.0
            found.append((mtime, HarnessSession(
                id=f"{agent_dir.name}/{f.stem}", harness=_HARNESS,
                agent=agent_dir.name, host=host, status="ended",
                last_event_at=mtime,
            )))
    found.sort(key=lambda t: t[0], reverse=True)
    return [hs for _, hs in found[:limit]]


def new_lines(prev: str, cur: str) -> list[str]:
    prev_lines = prev.splitlines()
    cur_lines = cur.splitlines()
    i = 0
    while i < len(prev_lines) and i < len(cur_lines) and prev_lines[i] == cur_lines[i]:
        i += 1
    return cur_lines[i:]


class ClaudeCodeHarness(Harness):
    name = _HARNESS

    def __init__(
        self,
        *,
        host: str = ".158",
        tmux_session: str = "skchat-agents",
        sessions_root: Path | None = None,
        runner: Runner | None = None,
        poll_interval: float = 1.0,
        max_polls: int | None = None,
    ) -> None:
        self.host = host
        self.tmux_session = tmux_session
        self.sessions_root = Path(sessions_root) if sessions_root else _DEFAULT_SESSIONS_ROOT
        self._runner = runner or _default_runner
        self.poll_interval = poll_interval
        self.max_polls = max_polls

    async def list_sessions(self) -> list[HarnessSession]:
        out = self._runner([
            "tmux", "list-windows", "-t", self.tmux_session,
            "-F", "#{window_name}\t#{window_activity}",
        ])
        live = parse_windows(out, host=self.host)
        historical = scan_historical(self.sessions_root, host=self.host)
        return live + historical

    async def stream(self, sid: str) -> AsyncIterator[SessionEvent]:
        now = time.time()
        if not _SID_RE.match(sid):
            yield SessionEvent(type=EventType.STATUS, text="invalid session id", ts=now)
            return
        yield SessionEvent(type=EventType.STATUS, text="attached", ts=now)
        prev = ""
        polls = 0
        target = f"{self.tmux_session}:{sid}"
        while self.max_polls is None or polls < self.max_polls:
            cur = self._runner(["tmux", "capture-pane", "-p", "-t", target])
            for line in new_lines(prev, cur):
                yield SessionEvent(type=EventType.ASSISTANT_TEXT, text=line,
                                   ts=time.time())
            prev = cur
            polls += 1
            if self.max_polls is not None and polls >= self.max_polls:
                break
            await asyncio.sleep(self.poll_interval)
```

- [ ] **Step 4: Run and see it PASS:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_claude_code_harness.py -q
```

- [ ] **Step 5: Commit:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && git add -A && git commit -m "feat(skcode-p0): claude-code read-only harness (tmux + historical dir)"
```

---

## Task 5: `skcode-hostd` daemon (read-only `/api/v1` + WS + static client)

**Files:** Create `src/skharness/daemon.py`. Test `tests/test_daemon.py`.

Promotes the skharness gateway into `skcode-hostd`. It owns ONE `Harness` and exposes exactly three data routes plus the static client route. Reuses `auth.require_bearer` (HTTP) and `auth.check_token` (WS query param, since browsers cannot set headers on a WebSocket). No POST/DELETE/write route exists.

**Interfaces defined here (used by Tasks 6, 7):**

```python
def build_daemon_app(*, harness: Harness, verify_caller: Verifier,
                     host_id: str = ".158",
                     client_dir: Path | None = None) -> FastAPI:
    """skcode-hostd. Routes:
      GET  /api/v1/hosts/self               -> {host, harness, ok}
      GET  /api/v1/sessions                 -> {"sessions": [HarnessSession...]}
      GET  /api/v1/sessions/{sid}           -> HarnessSession | 404
      WS   /api/v1/sessions/{sid}/stream    -> SessionEvent frames (token query param)
      GET  /            and  GET /app        -> the static read-only client HTML
    All /api/v1 read routes require a Bearer token; the WS requires ?token=.
    No mutating route exists (zero write surface)."""
```

WS auth uses close code `1008` (policy violation) before `accept()` on a bad or missing token. The static client route is unauthenticated (it is inert HTML; every data call it makes is gated), and falls back to a built-in placeholder page if `client_dir` has no `index.html` yet (so this task is testable before Task 7 writes the real page).

- [ ] **Step 1: Failing test** - write `tests/test_daemon.py`:

```python
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from skharness.daemon import build_daemon_app
from skharness.events import EventType, SessionEvent
from skharness.harness import FakeHarness, HarnessSession


def _harness():
    sessions = [
        HarnessSession(id="lumina-abc12345", harness="fake", agent="lumina",
                       host=".158", branch="main", model="ornith-tiny",
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
    assert rows[0]["id"] == "lumina-abc12345"
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
```

- [ ] **Step 2: Run and see it FAIL:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_daemon.py -q
```

- [ ] **Step 3: Implement** `src/skharness/daemon.py`:

```python
"""skcode-hostd - the promoted skharness gateway, read-only P0 (spec 8.1).

One host daemon owning ONE Harness. Exposes exactly three capauth-gated data
routes (list sessions, get one, stream one over WS) plus a static read-only
client page. There is NO spawn/inject/kill/dispatch route: the write surface
does not exist in P0. Bind a Tailscale IP only (serve.py enforces this).
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
            if s.id == sid:
                return JSONResponse(s.to_dict())
        raise HTTPException(404, "session not found")

    @app.websocket("/api/v1/sessions/{sid}/stream")
    async def stream(websocket: WebSocket, sid: str):
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
```

- [ ] **Step 4: Run and see it PASS:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_daemon.py -q
```

- [ ] **Step 5: Commit:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && git add -A && git commit -m "feat(skcode-p0): skcode-hostd read-only daemon (list + get + WS stream, zero write surface)"
```

---

## Task 6: `serve.py` entrypoint (Tailscale-only bind, port 9390)

**Files:** Create `src/skharness/serve.py` and `src/skharness/__main__.py`. Test `tests/test_serve.py`.

`resolve_bind` refuses `0.0.0.0`, `::`, and empty (the skharness rule + sk-access precedent). `main()` wires a real capauth verifier in production; for P0 it accepts a verifier factory so the wiring stays testable without importing capauth into the unit test. Default port `9390` (see the Global Constraints port conflict note).

**Interfaces defined here:**

```python
DEFAULT_PORT = 9390
def resolve_bind(host: str | None) -> str:
    """Return host if it is a concrete address; raise SystemExit for
    0.0.0.0 / :: / empty (never bind a wildcard / public port)."""
def build_default_verifier() -> Verifier:
    """Placeholder capauth verifier hook. P0 returns a deny-all verifier so a
    misconfigured deploy fails closed; the real capauth wiring lands with the
    pairing work (spec 7.6), not in this read-only MVP."""
def main(argv: list[str] | None = None) -> None:
    """Parse --host (required, tailnet IP) --port (default 9390); build the
    claude-code harness + daemon app; uvicorn.run bound to the resolved host."""
```

- [ ] **Step 1: Failing test** - write `tests/test_serve.py`:

```python
import pytest

from skharness.serve import DEFAULT_PORT, build_default_verifier, resolve_bind


def test_default_port_is_9390():
    assert DEFAULT_PORT == 9390


def test_resolve_bind_accepts_a_concrete_ip():
    assert resolve_bind("100.64.0.1") == "100.64.0.1"


@pytest.mark.parametrize("bad", ["0.0.0.0", "::", "", None])
def test_resolve_bind_refuses_wildcard(bad):
    with pytest.raises(SystemExit):
        resolve_bind(bad)


def test_default_verifier_fails_closed():
    v = build_default_verifier()
    assert v("anything") is False
```

- [ ] **Step 2: Run and see it FAIL:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_serve.py -q
```

- [ ] **Step 3: Implement** `src/skharness/serve.py`:

```python
"""skcode-hostd runner. Binds a Tailscale IP ONLY (never 0.0.0.0), port 9390.

The port 9390 default has a KNOWN deploy-time conflict: skcomms broker_server
may already hold 0.0.0.0:9390 on this host. Resolve it before deploy (move the
broker, or pass --port). See the plan's Global Constraints.
"""

from __future__ import annotations

import argparse

from skharness.auth import Verifier
from skharness.daemon import build_daemon_app
from skharness.harnesses.claude_code import ClaudeCodeHarness

DEFAULT_PORT = 9390

_WILDCARD = {"0.0.0.0", "::"}


def resolve_bind(host: str | None) -> str:
    if not host or host.strip() in _WILDCARD:
        raise SystemExit(
            "skcode-hostd refuses to bind a wildcard/public address; "
            "pass a Tailscale IP via --host"
        )
    return host.strip()


def build_default_verifier() -> Verifier:
    # P0 placeholder: fail closed. Real capauth verification is wired with the
    # pairing work (spec 7.6); a read-only MVP must never accept a token blindly.
    def _deny_all(token: str) -> bool:
        return False

    return _deny_all


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="skcode-hostd")
    parser.add_argument("--host", required=True, help="Tailscale IP to bind")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host-id", default=".158", help="node id for hosts/self")
    args = parser.parse_args(argv)

    host = resolve_bind(args.host)
    harness = ClaudeCodeHarness(host=args.host_id)
    app = build_daemon_app(
        harness=harness,
        verify_caller=build_default_verifier(),
        host_id=args.host_id,
    )
    uvicorn.run(app, host=host, port=args.port)
```

- [ ] **Step 4:** Implement `src/skharness/__main__.py`:

```python
"""`python -m skharness --host <tailnet-ip>` starts skcode-hostd."""

from skharness.serve import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run and see it PASS:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_serve.py -q
```

- [ ] **Step 6: Commit:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && git add -A && git commit -m "feat(skcode-p0): serve entrypoint (tailnet-only bind, port 9390 default)"
```

---

## Task 7: Static read-only web client

**Files:** Create `src/skharness/client/index.html`. Test `tests/test_client.py`.

A single self-contained page (no build step, no external asset fetch, so it satisfies the standalone rule). It has fields for the daemon base URL + bearer token, lists sessions from `GET /api/v1/sessions`, and on clicking a row opens `WS /api/v1/sessions/{sid}/stream?token=...` and appends events to a live transcript. It is READ-ONLY: no inject box, no buttons that POST anything. The test asserts the page is served and references the read-only API paths and nothing that writes.

- [ ] **Step 1: Failing test** - write `tests/test_client.py`:

```python
from fastapi.testclient import TestClient

from skharness.daemon import build_daemon_app
from skharness.harness import FakeHarness


def _client():
    app = build_daemon_app(harness=FakeHarness(), verify_caller=lambda t: t == "good")
    return TestClient(app)


def test_real_client_page_is_served():
    r = _client().get("/")
    assert r.status_code == 200
    body = r.text
    # It is the real client, not the placeholder.
    assert "skcode" in body.lower()
    assert "/api/v1/sessions" in body
    assert "/stream?token=" in body


def test_client_page_is_read_only_no_write_verbs():
    body = _client().get("/").text.lower()
    # No injection / mutation surface in the read-only MVP client.
    assert "method: 'post'" not in body
    assert "method: \"post\"" not in body
    assert "/inject" not in body
    assert "/dispatch" not in body


def test_client_page_has_no_external_asset_fetch():
    body = _client().get("/").text.lower()
    assert "http://" not in body
    assert "https://" not in body
```

- [ ] **Step 2: Run and see it FAIL** (the placeholder page from Task 5 lacks these markers):

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_client.py -q
```

- [ ] **Step 3: Implement** `src/skharness/client/index.html` (self-contained, read-only, no external URLs):

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>skcode (read-only)</title>
<style>
  body { font-family: ui-monospace, monospace; margin: 0; background: #111; color: #ddd; }
  header { padding: 10px; background: #1b1b1b; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  input { background: #000; color: #ddd; border: 1px solid #444; padding: 6px; border-radius: 4px; }
  #base { width: 240px; }
  #token { width: 220px; }
  button { background: #264; color: #fff; border: 0; padding: 6px 12px; border-radius: 4px; cursor: pointer; }
  main { display: flex; height: calc(100vh - 52px); }
  #list { width: 320px; overflow-y: auto; border-right: 1px solid #333; }
  .row { padding: 10px; border-bottom: 1px solid #222; cursor: pointer; }
  .row:hover { background: #1e1e1e; }
  .row .name { color: #7cf; }
  .row .meta { color: #888; font-size: 12px; }
  #transcript { flex: 1; overflow-y: auto; padding: 12px; white-space: pre-wrap; }
  .ev { margin: 2px 0; }
  .ev.status { color: #6a6; }
  .ev.needs_input { color: #fc6; }
  .ev.tool_call, .ev.tool_result { color: #9ad; }
</style>
</head>
<body>
<header>
  <strong>skcode</strong> <span style="color:#888">read-only</span>
  <input id="base" placeholder="daemon base, e.g. //100.x.x.x:9390" value="">
  <input id="token" type="password" placeholder="capauth bearer token">
  <button id="refresh">List sessions</button>
</header>
<main>
  <div id="list"></div>
  <div id="transcript"></div>
</main>
<script>
"use strict";
var baseEl = document.getElementById("base");
var tokenEl = document.getElementById("token");
var listEl = document.getElementById("list");
var transcriptEl = document.getElementById("transcript");
var ws = null;

function base() {
  var b = baseEl.value.trim();
  if (!b) { b = "//" + location.host; }
  return b;
}

function apiOrigin() {
  var b = base();
  if (b.indexOf("//") === 0) { return location.protocol + b; }
  return b;
}

function wsOrigin() {
  var b = base();
  var scheme = (location.protocol === "https:") ? "wss:" : "ws:";
  if (b.indexOf("//") === 0) { return scheme + b; }
  return b.replace(/^https:/, "wss:").replace(/^http:/, "ws:");
}

function addEvent(ev) {
  var div = document.createElement("div");
  div.className = "ev " + (ev.type || "");
  div.textContent = ev.text || JSON.stringify(ev.data || {});
  transcriptEl.appendChild(div);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function openStream(sid) {
  if (ws) { ws.close(); ws = null; }
  transcriptEl.textContent = "";
  var url = wsOrigin() + "/api/v1/sessions/" + encodeURIComponent(sid)
          + "/stream?token=" + encodeURIComponent(tokenEl.value.trim());
  ws = new WebSocket(url);
  ws.onmessage = function (m) { try { addEvent(JSON.parse(m.data)); } catch (e) {} };
  ws.onclose = function () { addEvent({ type: "status", text: "(stream closed)" }); };
  ws.onerror = function () { addEvent({ type: "status", text: "(stream error)" }); };
}

function renderList(rows) {
  listEl.textContent = "";
  rows.forEach(function (s) {
    var row = document.createElement("div");
    row.className = "row";
    var name = document.createElement("div");
    name.className = "name";
    name.textContent = s.id + "  [" + (s.status || "") + "]";
    var meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = (s.agent || "") + " @ " + (s.host || "")
                     + "  " + (s.branch || "") + "  " + (s.model || "")
                     + "  " + (s.last_message || "");
    row.appendChild(name);
    row.appendChild(meta);
    row.onclick = function () { openStream(s.id); };
    listEl.appendChild(row);
  });
}

function refresh() {
  var xhr = new XMLHttpRequest();
  xhr.open("GET", apiOrigin() + "/api/v1/sessions");
  xhr.setRequestHeader("Authorization", "Bearer " + tokenEl.value.trim());
  xhr.onload = function () {
    if (xhr.status === 200) {
      renderList(JSON.parse(xhr.responseText).sessions || []);
    } else {
      listEl.textContent = "list failed: " + xhr.status;
    }
  };
  xhr.onerror = function () { listEl.textContent = "list failed (network)"; };
  xhr.send();
}

document.getElementById("refresh").onclick = refresh;
</script>
</body>
</html>
```

Note on the "no external URL" test: the page builds request origins from `location` and the operator-entered base at runtime, so the literal source contains no `http://` or `https://` string. The `.replace(/^https:/, ...)` calls use bare scheme tokens (`https:` with the colon, not `https://`), which do not match the forbidden `https://` / `http://` substrings the test scans for.

- [ ] **Step 4: Run and see it PASS:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/test_client.py -q
```

- [ ] **Step 5: Commit:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && git add -A && git commit -m "feat(skcode-p0): self-contained read-only static web client"
```

---

## Task 8: Docs, PORTS note, and final verification

**Files:** Edit `README.md`. (Do NOT create new stray docs; update the existing README only.)

- [ ] **Step 1:** Append a `## skcode-hostd (P0, read-only)` section to `README.md` documenting: the three routes, the `:9390` port and its known conflict with skcomms broker_server, the tailnet-only bind, and the run command:

```bash
~/.skenv/bin/python -m skharness --host <your-tailscale-ip> --port 9390 --host-id .158
```

State plainly: P0 is read-only, no spawn/inject/kill/dispatch, and the capauth verifier is a fail-closed placeholder until the pairing work (spec 7.6) wires real verification.

- [ ] **Step 2: Full suite green:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/python -m pytest tests/ -q
```

Expect the original 20 plus the new tests (test_events, test_auth, test_harness, test_claude_code_harness, test_daemon, test_serve, test_client) all passing.

- [ ] **Step 3: Lint clean:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && ~/.skenv/bin/ruff check src/skharness/ tests/
```

- [ ] **Step 4: Boot smoke (standalone guarantee).** Confirm the module starts and the static page serves without any shell inside it. Run the daemon on the loopback-substitute tailnet IP for a smoke check ONLY if a tailnet IP is available; otherwise assert the app builds:

```bash
~/.skenv/bin/python -c "
from skharness.daemon import build_daemon_app
from skharness.harnesses.claude_code import ClaudeCodeHarness
from skharness.serve import build_default_verifier
app = build_daemon_app(harness=ClaudeCodeHarness(host='.158'), verify_caller=build_default_verifier())
print('routes', sorted({r.path for r in app.routes if hasattr(r, 'path')}))
"
```

Expect the printed route set to be exactly the read-only surface: `/`, `/app`, `/api/v1/hosts/self`, `/api/v1/sessions`, `/api/v1/sessions/{sid}`, `/api/v1/sessions/{sid}/stream` (plus FastAPI's default `/openapi.json`, `/docs`, `/redoc`). Confirm NO `/api/v1/dispatch`, no POST/DELETE session routes.

- [ ] **Step 5: Commit:**

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skharness && git add -A && git commit -m "docs(skcode-p0): README skcode-hostd section + port conflict note"
```

---

## Self-Review

**Spec coverage (§8.1 deliverables):**

- `GET /api/v1/sessions` (list) -> Task 5 `list_sessions`, tested `test_list_sessions_returns_rows`.
- `GET /api/v1/sessions/{sid}` (one) -> Task 5 `get_session`, tested ok + 404.
- `WS /api/v1/sessions/{sid}/stream` (typed SessionEvent stream) -> Task 5 `stream`, tested delivery + auth reject.
- `Harness` interface, read-only subset (`list_sessions`, `stream`) -> Task 3, grows the existing `Spawner` ABC concept into a new `Harness` ABC (kept separate so the untouched `Spawner`/`SessionManager` spawn machinery stays out of the read-only daemon).
- claude-code PTY/tmux adapter over `skchat-agents` windows + historical `~/.skcapstone/agents/<agent>/sessions/` -> Task 4, tested with a fake runner.
- `FakeHarness` for CI -> Task 3.
- Typed `SessionEvent` + `HarnessSession` -> Tasks 1 and 3.
- `skcode-hostd` on `:9390`, capauth-gated, tailnet-only, zero write authority -> Tasks 5 (routes + gate) and 6 (bind guard, port). `test_no_write_surface` proves the write surface does not exist.
- Minimal self-contained static web client -> Task 7, no build step, no external asset fetch.
- Reuse: capauth gate extracted to `auth.py` and shared (Task 2); `SessionRegistry`/`SessionManager`/`Spawner`/`Session` left untouched and simply not wired.

**Placeholder scan:** No `TBD`, no `...` stub bodies, no `pass`-only implementations. Every code block is complete and runnable. The only intentional stand-ins are: (a) `build_default_verifier` returns a fail-closed deny-all verifier (the real capauth verifier is explicitly out of P0 scope, deferred to the pairing work spec 7.6, and failing closed is the safe default), and (b) the daemon's `_PLACEHOLDER` HTML, which is only reached before Task 7 writes the real page and is superseded by it.

**Type consistency (every referenced symbol defined in an earlier task, exact signatures):**

- `EventType`, `SessionEvent`, `SessionEvent.to_dict/from_dict` (Task 1) -> used by `harness.py` (Task 3), `claude_code.py` (Task 4), `daemon.py` (Task 5).
- `Verifier`, `require_bearer`, `check_token` (Task 2) -> used by `gateway.py` refactor (Task 2), `daemon.py` (Task 5), `serve.py` (Task 6).
- `HarnessSession`, `HarnessSession.to_dict/from_dict`, `Harness` ABC, `FakeHarness` (Task 3) -> used by `claude_code.py` (Task 4), `daemon.py` (Task 5), all daemon/client tests.
- `Runner`, `parse_windows`, `scan_historical`, `new_lines`, `ClaudeCodeHarness` (Task 4) -> used by `serve.py` (Task 6) and the smoke check (Task 8).
- `build_daemon_app(*, harness, verify_caller, host_id, client_dir)` (Task 5) -> used by `serve.py` (Task 6) and tests (Tasks 5, 7).
- `resolve_bind`, `build_default_verifier`, `DEFAULT_PORT`, `main` (Task 6) -> used by `__main__.py` (Task 6) and Task 8 smoke.

**Known deploy-time gate (surfaced, not a code defect):** port `:9390` is currently held by `skcomms.transports.broker_server` bound to `0.0.0.0:9390` on this host. Because skcode binds a specific tailnet IP, the wildcard listener still owns tailnet-IP:9390, so a bind would fail with `EADDRINUSE`. Resolve before deploy by moving the broker off 9390 or passing `--port <free>` and recording it in `~/.skcapstone/docs/PORTS.md`. This is called out in Global Constraints and the README (Task 8).

**Interface note vs the spec (for the controller):** spec §3.1 declares `stream` as `async def stream(...) -> AsyncIterator[SessionEvent]`. Implemented here as a plain `def`-signature abstractmethod returning `AsyncIterator[SessionEvent]`, realized by async-generator methods (`async def ... yield`). This is the correct Python idiom (an `async def` with `yield` is an async generator whose call returns the iterator without `await`); the consumer contract (`async for ev in harness.stream(sid)`) matches the spec exactly.

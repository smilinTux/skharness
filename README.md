# skharness

**Sovereign "phone-drives-my-agent-swarm" harness.**

Run many coding/work agents, each in an **isolated session**, and **drive them from a phone
over the tailnet** — the feel of Claude Code's remote control, but **self-hosted on SKWorld
infra with no Big-Tech broker**.

## Why
Claude Code Remote Control has great UX but routes phone↔laptop through Anthropic's relay —
it fails the sovereignty test. skharness replicates the *experience*, not the foundation, over
**Tailscale + capauth**. (Design basis: the 2026-06-13 harness deep-research — pi / cmux /
OpenCode / web-shells / Claude Code Remote Control. Full spec in
`docs/superpowers/specs/2026-06-13-skharness-design.md`.)

## Architecture
A capauth-gated FastAPI over a `SessionManager`:

| Module | Role |
|---|---|
| `gateway.py` | FastAPI app; **bind to a Tailscale IP only** (never a public port). `verify_caller` is the auth seam — a real capauth verifier in prod, a fake in tests. |
| `manager.py` | `SessionManager` — ties registry + spawner; spawn creates an isolated worker. |
| `spawner.py` | Spawner seam — `FakeSpawner` for CI, `TmuxSpawner` for real sessions. |
| `registry.py` | `SessionRegistry` — track/persist sessions (JSON now; coord-board / skmem-pg later). |
| `session.py` | `Session` model — one isolated agent worker. |

Mostly **reuse**: `pi` (MIT, multi-provider incl. local qwen) as orchestrator + the coord
board / skmem-pg as the session/task registry.

## Run
```bash
pip install -e .            # fastapi + pydantic
# build_app(manager=..., verify_caller=<capauth verifier>) -> FastAPI
# serve bound to the tailscale IP only; auth every call via capauth.
```

## Status
`v0.1.0`, P0 session core (spec + plan under `docs/superpowers/`). Mirror: smilinTux (private).

## skcode-hostd (P0, read-only)

`skcode-hostd` is the read-only remote-control daemon over the unified Harness
session plane. It owns ONE harness (the claude-code tmux adapter) and exposes
exactly three capauth-gated data routes plus a self-contained static client. There
is NO write surface: no spawn, inject, kill, dispatch, rename, archive, or model
switch. A test (`tests/test_daemon.py::test_no_write_surface`) proves POST/DELETE
return 405 and `/inject` / `/dispatch` return 404.

Routes:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/hosts/self` | host + harness identity |
| GET | `/api/v1/sessions` | list live + historical sessions |
| GET | `/api/v1/sessions/{sid}` | one session, or 404 |
| WS | `/api/v1/sessions/{sid}/stream` | typed `SessionEvent` stream (`?token=`) |
| GET | `/` and `/app` | the static read-only web client |

HTTP routes require a `Bearer` token; the WebSocket takes the token as a
`?token=` query param (browsers cannot set headers on a WS). The capauth verifier
in P0 is a fail-closed deny-all placeholder: real verification lands with the
pairing work (spec 7.6), so the daemon rejects every token until then by design.

Run (Tailscale IP only, never `0.0.0.0`):

```bash
~/.skenv/bin/python -m skharness --host <your-tailscale-ip> --port 9394 --host-id .158
```

skcode-hostd defaults to `:9394` (SKWorld platform spec R0.4). Port `:9390` is
owned by `skcomms.transports.broker_server` (its honest, documented default), so
the two no longer collide on a shared host. Override with `--port <free>` and
record it in `~/.skcapstone/docs/PORTS.md` if `:9394` is taken.

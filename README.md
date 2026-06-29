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

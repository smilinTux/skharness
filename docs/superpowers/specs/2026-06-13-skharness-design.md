# skharness — sovereign "phone drives my agent swarm"

**Date:** 2026-06-13
**Repo:** `skharness` (new)
**Status:** design (research-grounded) — ready for implementation plan
**Research basis:** the 2026-06-13 harness deep-research (pi/cmux/OpenCode/web-shells/Claude Code Remote Control)

---

## 1. Goal

A **sovereign** orchestration harness: run many coding/work agents, each in an
**isolated session**, **drive them from a phone over the tailnet**, with **no
Big-Tech broker**. Chef's stated shape: *"just the CLI in there, but different
agents/chats/sessions you can switch back and forth with"* — the feel of the Claude
Code app's remote control, but self-hosted on SKWorld infra.

**The one decided principle (from research):** Claude Code Remote Control is great
UX but **Anthropic-brokered** (routes phone↔laptop through Anthropic's relay) — it
fails the sovereignty test. So we replicate the *experience*, not the foundation,
over **Tailscale + capauth**.

## 2. Why this is mostly *reuse*

The research found there is no single off-the-shelf product, but a clear best path,
and almost every piece is something SKWorld already owns:

| Layer | Use | Already have |
|---|---|---|
| **Orchestrator** | `pi` (MIT, Linux, multi-provider incl. local qwen) | + the **coord board / skmem-pg** as the task/session registry |
| **Isolation** | **git-worktree per worker** | the `superpowers:using-git-worktrees` + `EnterWorktree` tooling |
| **Session spawn + tabs** | one **tmux session per agent** running its CLI | — |
| **Terminal-in-the-app** | a **web-terminal** over each tmux session: `ttyd` (cleanest) or **`sshx`** (E2E-encrypted, the sovereign pick) | — |
| **Remote gateway** | a tailnet HTTP/WS gateway, **QR-pairing + capauth + PTY-attach** (the `opencode-remote` pattern) | **capauth** (auth), **Tailscale** (transport), the parked **QR-pairing** design |
| **Mobile/web UI** | a **session-switcher** (list of agents → tap → live CLI) | the **Flutter `skchat-app`** (WebView per tab) |
| **Notifications** | "agent needs input" → push | **sk-alert / Telegram** |

So the genuinely-new code is small: the **session manager + the tailnet gateway**.
The terminal itself is delegated to ttyd/sshx (we don't reinvent xterm.js); the UI
is a Flutter tab-bar over WebViews; orchestration leans on pi + coord.

## 3. Architecture

```
📱 skchat-app (Flutter) — session-switcher: list agents → tap → live CLI (WebView → web-terminal)
        │  (over Tailscale, capauth-gated)
🛰️ skharness gateway (FastAPI on the tailnet)
        │  - SessionRegistry  (agent_id → {tmux, worktree, web-url, status}; backed by coord/skmem-pg)
        │  - spawn / list / kill / attach-url  (capauth-gated; QR-pair for new devices)
        │  - "needs-input" events → sk-alert/Telegram
        ▼
🤖 SessionManager  (spawns workers)
        │  - per worker: git WORKTREE (isolation) + tmux session running `pi`/`claude` CLI
        │  - a web-terminal (ttyd/sshx) fronts each tmux session  → the attach-url
        ▼
🧠 Orchestrator = pi (Opus-tier) decomposes work → spawns workers; task state in coord/skmem-pg
```

**Spawn = isolation.** Each worker gets `git worktree add` (its own branch) + a tmux
session running the agent CLI in that worktree + a web-terminal bound to it. Killing
a worker tears down all three.

**Sovereign remote.** The gateway binds a **Tailscale IP only** (never a public
port). New devices pair via **QR + capauth** (reusing the parked pairing design);
the session stream is `opencode-remote`-style PTY-attach. No Anthropic relay.

## 4. Components (the new code = `src/skharness/`)

| File | Responsibility |
|---|---|
| `session.py` | `Session` model (id, agent, worktree path, tmux name, web_url, status, created) |
| `registry.py` | `SessionRegistry` — track/persist sessions (json now; coord/skmem-pg adapter later) |
| `spawner.py` | `Spawner` ABC (`spawn`/`kill`/`attach_url`) + `FakeSpawner` (CI) + `TmuxSpawner` (real: worktree + tmux + ttyd) |
| `manager.py` | `SessionManager` — ties registry + spawner: `spawn(agent, prompt, repo)`, `list()`, `kill(id)`, `attach_url(id)` |
| `gateway.py` | FastAPI app — capauth-gated `/sessions` (list/spawn/kill), `/sessions/{id}/attach` (web-terminal url), pairing |
| `pairing.py` | QR/capauth device pairing for new phones (reuse the skcomms/skchat pairing pattern) |
| `events.py` | "needs-input"/status events → sk-alert/Telegram bridge |

The Flutter session-switcher lives in `skchat-app` (a new feature surface), not here.

## 5. Testing

CI-first, hardware/infra-free: the **`FakeSpawner`** (in-memory; records spawn/kill,
returns fake attach-urls) makes the `SessionManager` + `registry` + `gateway` fully
unit-testable with no tmux/ttyd/worktrees. The `TmuxSpawner` (real worktree+tmux+ttyd)
is written against the seam and integration-tested on `.158` later. Gateway tested
with FastAPI `TestClient` + a fake capauth verifier.

## 6. Phasing

| Phase | Deliverable |
|---|---|
| **P0 — session manager core (CI)** | `session`+`registry`+`Spawner` seam (`FakeSpawner`)+`manager`+`gateway` (capauth-gated REST, FastAPI TestClient). The buildable, CI-tested core. |
| **P1 — real spawner (.158)** | `TmuxSpawner`: `git worktree` + tmux + `ttyd`/`sshx` per agent; bind the gateway to the tailnet; live single-agent drive from a browser. |
| **P2 — Flutter switcher** | the session-switcher feature in `skchat-app` (tab list → WebView per session); QR-pair a phone; sk-alert "needs input". |
| **P3 — pi orchestrator + coord** | wire `pi` as the orchestrator, swap the registry's store to the **coord board / skmem-pg**, worktree-isolated fan-out. |
| **P4 — optional** | adopt `opencode serve` as a second backend (typed session API) for non-pi workers. |

## 7. Security & sovereignty notes

- **No public ingress** — gateway on Tailscale only; capauth-signed device pairing.
- **Isolation** — git worktree per worker; `pi` has no permission sandbox, so workers
  run with scoped cwd + tool allowlists (and containerize before any untrusted use).
- **No Anthropic broker** — the phone↔swarm path is entirely SKWorld (tailnet +
  gateway + web-terminal), unlike Claude Code Remote Control.
- **sshx option** — for E2E-encrypted, server-blind terminals, prefer `sshx` over
  `ttyd` in P1 (stronger sovereignty; the server never sees keystrokes).
- **P1 TmuxSpawner mandate (RCE guard):** never build shell strings from `repo`/`agent`
  — use argv-list `subprocess` (no `shell=True`); validate `repo` against an allow-list
  of known repo roots; constrain `agent`/tmux-session names to `[A-Za-z0-9_-]+`. capauth
  gating is not a substitute for input validation — a buggy/compromised authenticated
  client still reaches this code.

## 8. Open items folded into the plan

- `ttyd` vs `sshx` for P1 (decide at P1; `sshx` for E2E, `ttyd` for simplicity).
- Whether the gateway embeds the web-terminal or just brokers its url — P0 brokers
  the url; the Flutter WebView loads it directly over the tailnet.
- coord/skmem-pg registry adapter shape — P3 (P0 uses a json store behind the
  `SessionRegistry` interface).

# skharness

**Sovereign "phone-drives-my-agent-swarm" harness.**

Run many coding/work agents, each in an **isolated session**, **drive them from a phone
over the tailnet**, and let the **autocode engine** drive them unattended behind a merge
gate. The feel of Claude Code's remote control, self-hosted on SKWorld infra with no
Big-Tech broker.

**Operational docs: [SOP.md](./SOP.md).** Security posture and reporting:
[SECURITY.md](./SECURITY.md). Contributing: [CONTRIBUTING.md](./CONTRIBUTING.md).

## Why

Claude Code Remote Control has great UX but routes phone to laptop through Anthropic's
relay, which fails the sovereignty test. skharness replicates the *experience*, not the
foundation, over **Tailscale + capauth**. (Design basis: the 2026-06-13 harness
deep-research covering pi, cmux, OpenCode, web-shells, and Claude Code Remote Control.
Spec: `docs/superpowers/specs/2026-06-13-skharness-design.md`. That spec predates the
current write surface; where it disagrees with [SOP.md](./SOP.md), trust the SOP.)

## What is in here

Two things ship in one distribution:

1. **`skcode-hostd`**, the per-host daemon: a capauth-gated FastAPI over one Harness (the
   claude-code tmux adapter), bound to a Tailscale IP on port `9394`.
2. **The autocode engine** (`src/skharness/autocode/`): the assess/plan/build/grade/
   finalize loop, the twin gate, and the constitutional carve-out detector.
   `skos.autopilot` delegates to it.

## Architecture

| Module | Role |
|---|---|
| `serve.py` | `skcode-hostd` entry point. `resolve_bind()` refuses a wildcard bind; `select_verifier()` picks real capauth or the fail-closed deny-all. |
| `daemon.py` | Every HTTP/WS route, plus `PUBLIC_ROUTES` and `ROUTE_SCOPES`, the authoritative scope map. |
| `auth.py` | The bearer gate and the `AuthContext` scope carrier. Fail closed before the verifier runs. |
| `harnesses/claude_code.py` | The tmux harness and the `spawn()` guard, including the dispatch repo allowlist. |
| `operator_cli.py` | The `explain` / `observe` / `act` operator facet Atlas drives. |
| `autocode/orchestrator.py` | Engine phases 0-3, caps, kill switch. |
| `autocode/engineering.py` | Worktree, sandbox, grade, twin gate, `finalize()`. The merge choke point. |
| `autocode/protected.py` | Path-level carve-out detector. Fails closed: any manifest load failure protects everything. |
| `manager.py` / `registry.py` / `session.py` / `spawner.py` | The session-core primitives from the P0 design. |

Full diagram and entry-point tour: [SOP.md section 2](./SOP.md#2-architecture).

Target designs: [continual harness](./docs/architecture/continual-harness.md) defines
safe refinement and recovery; the [Evolution Arena](./docs/architecture/evolution-arena.md)
defines controlled multi-agent experiments, Pi/SKGateway execution, independent
verification, artifact lineage, and Pareto promotion. Both are explicitly proposed;
their documents distinguish implemented evidence from planned architecture.

## Run

```bash
pip install -e ".[dev]"

# tailnet IP only; a wildcard or blank host is refused by resolve_bind()
~/.skenv/bin/python -m skharness --host <your-tailscale-ip> --port 9394 --host-id .158
```

Managed deploy (systemd user unit, never auto-started):

```bash
./systemd/install.sh          # then provision ~/.config/skcode-hostd/skcode-hostd.env
systemctl --user enable --now skcode-hostd
```

See [SOP.md section 5](./SOP.md#5-release--deploy) for the full deploy, rollback, and the
two live drop-ins.

## Exposure

**Tailnet only. There is no public route.** No `:443` vhost, no Cloudflare Tunnel, no
Funnel. `serve.resolve_bind()` raises rather than binding `0.0.0.0` or `::`, and the unit
sources `--host` from `${SKCODE_HOSTD_TAILSCALE_IP}`, so a missing value **fails the unit
closed instead of exposing a port**. Do not add a fallback default.

Port `9394` is the ratified default (SKWorld platform spec R0.4). `:9390` belongs to
`skcomms.transports.broker_server`, hence the offset.

## Routes and the write surface

**This daemon has a real write surface.** Earlier revisions of this README claimed it did
not; that claim was stale and is corrected here.

| Method | Path | Required scope |
|---|---|---|
| GET | `/.well-known/skworld-module.json`, `/`, `/app` | public (static client and manifest) |
| GET | `/api/v1/hosts/self` | `skcode.stream` |
| GET | `/api/v1/sessions`, `/api/v1/sessions/{sid}`, `/api/v1/sessions/{sid}/events` | `skcode.stream` |
| GET | `/api/v1/jobs`, `/api/v1/watchdog/digest` | `skcode.stream` |
| WS | `/api/v1/sessions/{sid}/stream` | `skcode.stream` (token rides `?token=`) |
| GET | `/api/v1/dispatch/targets` | `skcode.dispatch` |
| POST | `/api/v1/sessions/{sid}/ratify` | `skcode.inject` (grades only, never merges) |
| POST | `/api/v1/sessions/{sid}/inject` | `skcode.inject` (keystrokes into a live PTY) |
| POST | `/api/v1/sessions/{sid}/deny` | `skcode.inject` |
| POST | `/api/v1/dispatch` | `skcode.dispatch` (spawns a NEW session: the RCE surface) |
| POST | `/api/v1/sessions/{sid}/cancel` | `skcode.dispatch` |

There is **no `/health` or `/healthz` route**. Use `GET /api/v1/hosts/self` or
`systemctl --user is-active skcode-hostd`.

Four gates stand in front of the write surface, each failing closed:

1. **Bearer** (`auth.require_bearer`): a missing or empty token is rejected before the
   verifier runs.
2. **capauth verification** (`serve.build_capauth_verifier`): the wire token is
   base64url-decoded, `import_token`ed, and checked with `verify_audience_token(t,
   "skcode")`. Any parse or verify failure denies. If capauth cannot be imported,
   `select_verifier()` falls back to **deny all**.
3. **Scope split**: `skcode.stream` reads, `skcode.inject` writes, `skcode.dispatch`
   spawns. A read-only token can view everything and actuate nothing.
4. **capauth PDP**: `inject` and `dispatch` additionally require a `capauth.authz.decide`
   allow at a `VERIFIED` enrollment floor. Dispatch also needs an audit sink (no sink
   means `501`), honours the pause flag (`503`), and restricts the `full` profile to an
   explicit subject allowlist.

`tests/test_route_coverage.py` enumerates the live route table and fails if any served
route is not classified as public or scope-gated, so a new gated route cannot ship
unclassified.

## Dispatch allowlist

`POST /api/v1/dispatch` can spawn a new agent session, which is remote code execution.
`SKCODE_DISPATCH_REPOS` (comma-separated absolute repo roots) is the last gate.
**Unset or empty means DENY ALL**, and the shipped env template deliberately omits the
key, so a fresh install can dispatch nothing.

**`skos` and `skharness` must never be added to it.** They are the self-modification
hazard: an agent dispatched into either could edit the very code that grades it. The
enforcement is the deployed env value alone; there is no code-level exclusion list. See
[SOP.md section 6](./SOP.md#6-configuration--usage).

## Crypto posture

`skharness` **generates, stores, and wraps no key material.** Its entire cryptographic
footprint is verification: one call into capauth's `verify_audience_token` over an
already-issued token, behind the `Verifier` seam in `src/skharness/auth.py`. It performs
no key exchange, no KEM, no signature generation. **No post-quantum claim is made or
implied here**; the posture of the tokens it verifies belongs to
[capauth](https://github.com/smilinTux/capauth).

The authorization model, the twin gate, and the carve-out detector are **operational but
not independently audited**. The sandbox is isolation for accidents, not a hostile-code
boundary: treat agent-authored code as untrusted input to review, not as contained.

## Test

```bash
python -m pytest tests/ -q
```

CI (`.github/workflows/ci.yml`) runs `lint`, `test`, `compat-3-10`, and `build`. The
`test` job installs the real siblings from git `main` (`skcoord` last, with `--upgrade`)
and **fails if the cross-repo round-trip test skips**, because a skipped gate reports
green while checking nothing. Details: [SOP.md section 4](./SOP.md#4-test).

## Version and license

The version comes from `setuptools-scm`, derived from the newest `v*.*.*` git tag, and a
release tag is cut automatically by `publish.yml` on a push to `main`. Read it with
`python -m setuptools_scm` in a checkout with tags. History: [CHANGELOG.md](./CHANGELOG.md).

Licensed **GPL-3.0-or-later**. See [LICENSE](./LICENSE).

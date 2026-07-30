# skharness Audit: Quality/Validation Discipline + skcode Integration

**Date:** 2026-07-25. **Auditor:** Opus (read-only audit, no code changed).
**Scope:** skharness `v0.2.0` at `/home/cbrd21/clawd/skcapstone-repos/skharness`, promoted toward `skcode-hostd` per the skcode design spec (`/home/cbrd21/clawd/docs/superpowers/specs/2026-07-25-skcode-remote-control-dispatch-design.md`).

---

## 1. What skharness is today

A small, clean Python package: a capauth-gated FastAPI over a session manager, built to spawn isolated agent workers and drive them from a phone over the tailnet (no Big-Tech relay).

Implemented modules (`src/skharness/`):

- `session.py`: `Session` dataclass + `SessionStatus` enum (spawning/running/ended).
- `registry.py`: `SessionRegistry`, a JSON-file store behind an interface (P3 swaps in a skcoord/skmem-pg store).
- `spawner.py`: `Spawner` ABC + `FakeSpawner`. `TmuxSpawner` (real worktree + tmux + web terminal) is named for P1 but NOT implemented.
- `manager.py`: `SessionManager` (`spawn`/`kill`/`list`/`attach_url`); ghost-session cleanup on spawn failure.
- `gateway.py`: capauth-gated FastAPI (`/sessions` list/spawn/attach/kill). This is the write-capable app.
- `auth.py`: `require_bearer` (HTTP 401/403 fail-closed) + `check_token` (WS query-param, fail-closed). Shared gate.
- `events.py`: `SessionEvent` + `EventType` (status, assistant_text, tool_call, tool_result, diff, needs_input): the typed read-only stream.
- `harnesses/__init__.py`: empty package marker.

Two design docs and one plan live under `docs/superpowers/`. The skcode P0 read-only MVP plan (`docs/superpowers/plans/2026-07-25-skcode-p0-readonly-mvp.md`) is fully authored (8 tasks) but only Tasks 0 to 2 are executed. The daemon, the `Harness` seam, the claude-code adapter, `serve.py`, `__main__.py`, and the static client (`harness.py`, `harnesses/claude_code.py`, `daemon.py`, `serve.py`, `client/index.html`) do NOT yet exist. State: 31 tests pass, ruff clean, on branch `feat/skcode-p0-readonly-mvp`.

Note: there are currently TWO parallel session abstractions. The older write-capable stack (`Session` / `SessionManager` / `Spawner` / `SessionRegistry` / `gateway.py`) and the newer read-only stack (`SessionEvent`, and the planned `Harness` / `HarnessSession` / `daemon.py`). The skcode spec (§3.1) says the `Spawner` "grows into" the `Harness`, but the P0 plan deliberately leaves the old stack untouched and unwired. That fork is the root of several findings below.

---

## 2. Question 1: does skharness have or offer built-in checking / code-quality / validation?

### 2a. Quality machinery skharness has for ITSELF (verdict: partial)

- **Tests: yes, and good ones.** 31 pytest tests, TDD-authored (the `.superpowers/sdd/` task reports document RED then GREEN then review per task). The security-critical paths are well covered: fail-closed on empty bearer before the verifier runs, every route requires auth, no ghost SPAWNING session on spawn failure, WS token reject. This is above-average discipline for a repo this young.
- **Lint: yes.** `pyproject.toml` configures ruff (`E,F,I,N,W`, line-length 99, ignore E501). Runs clean.
- **CI: NO.** No `.github/workflows`, no tox, no pre-commit, no fleet CI hook. Tests and lint are run by hand (`~/.skenv/bin/python -m pytest`, `~/.skenv/bin/ruff check`). Nothing enforces green on push, and nothing mechanically enforces the no-em-dash rule.
- **Typecheck: NO.** No mypy or pyright config, no `py.typed` marker, despite the code being fully type-annotated and meant to be imported by skcode-hostd.
- **Coverage: NO.** No coverage config or gate.

So the DEVELOPMENT of skharness used the superpowers discipline (SDD, TDD, self-review, task reports), but that discipline is a human process artifact, not codified CI that will keep holding as skcode piles on code.

### 2b. Does a SPAWNED agent session inherit or get offered the quality discipline? (verdict: ABSENT)

Traced end to end. It does not.

- **The spawn path carries no quality anything.** `SessionManager.spawn(agent, prompt, repo)` (`manager.py:19`) passes `prompt` through untouched to `Spawner.spawn`. `FakeSpawner.spawn` (`spawner.py:29`) only fills `worktree`/`tmux`/`web_url`. There is no step that injects skills, a TDD instruction, a review gate, or a verification requirement into the spawned environment or prompt. `TmuxSpawner` (the real one) is not written yet, so the pattern is not even reserved.
- **The live dispatch precedent (jarvis-heartbeat) also injects nothing.** `skchat/scripts/jarvis-heartbeat.py` `_build_prompt()` builds a plain task prompt, and `spawn_tmux_session()` exports only `SKAGENT`, `SKCAPSTONE_AGENT`, `SKCHAT_IDENTITY`, and `PATH` before running `claude`. No superpowers skills, no review gate, no TDD, no verify. The spec (§1.1) says jarvis-heartbeat becomes one caller of the daemon, so this gap is inherited by skcode unless the daemon adds the layer.
- **The planned claude-code adapter is read-only** (list + stream), so it does not spawn at all in P0.

The superpowers quality skills (`subagent-driven-development`, `requesting-code-review`, `test-driven-development`, `verification-before-completion` at `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/`) live only in the OPERATOR's own interactive Claude Code. A session spawned or dispatched by skharness/skcode inherits none of them. The discipline is absent from the spawned runtime, not merely un-enforced.

### 2c. Is there any validation / code-review / quality-gate capability exposed as a skill, endpoint, or method? (verdict: NO)

- **Harness seam (spec §3.1):** the verbs are `list_sessions`, `get_branch`, `list_models`, `stream`, `inject`, `spawn`, `set_model`, `background_tasks`, `archive`. There is no `review`, `validate`, `verify`, or `ratify` verb.
- **Daemon API (spec §4.2):** Code + Dispatch + Pairing routes. No `/review`, `/validate`, `/ratify`, or `/verify` route.
- **Profiles (spec §6):** `full` vs `sandbox` is a SECURITY scoping axis (which sk* surface a session may touch), not a quality axis. `permission_mode` (`manual`/`auto`) is per-action approval, not a review gate on the work product.
- The skgateway / skcoord / capauth integrations are identity, model routing, task cards, and authz. None is a quality layer.

### 2d. The gap vs Chef's intent (verdict: real and unaddressed)

Chef wants the validation/ratification discipline (implement -> review spec + quality -> fix -> final review -> verify) USED or OFFERED as a skill or built-in inside the skcode framework. Today:

- The discipline exists only as (1) superpowers skills in the operator's own interactive session and (2) the human-run SDD/plan process that built skharness.
- Nothing in skcode's runtime hands a spawned session those skills or that gate, and nothing lets an operator ask skcode to ratify a session's output.

That is the gap: skcode is being designed as a pure session spawner + streamer + dispatcher with a strong security layer and zero quality layer. The validation-as-a-skill capability is not partially present, it is entirely missing from the design. It should be added as an explicit axis before Dispatch (P2) ships, because P2 is the first phase where skcode spawns real coding work whose output nobody is gating.

---

## 3. Proposal: offer validation / ratification as a skill or built-in in skcode

Add a QUALITY axis alongside the existing security profile axis, using the same "enforced by wiring, not by asking" pattern the spec already uses for profiles (§6.2). Three complementary plug points, cheapest first.

### 3a. Dispatch-time quality profile (the wiring plug, do first)

Add a `quality` field to the `SessionDescriptor` (spec §2), e.g. `quality: "gated" | "none"`, defaulting to `gated` for any coding dispatch and `none` for eval/throwaway sandbox work. When the daemon composes the spawned harness environment (it already must, for the security profile), a `gated` session gets:

- the four superpowers quality skills made available to the spawned agent (skill dir on the skill path), and
- a system-prompt preamble that instructs the gate explicitly: use test-driven-development for each unit, request code review at completion (requesting-code-review), fix, final review, then verification-before-completion before claiming done.

This is pure environment composition in `skcode-hostd`, the exact place the spec already builds the profile environment (§6.2). It is the smallest change that closes the gap and it rides the machinery that has to exist anyway.

### 3b. A `ratify` Harness verb + daemon endpoint (the built-in plug, durable)

Grow the `Harness` seam with a read-then-judge verb and expose it on the daemon:

```
Harness.ratify(sid) -> RatifyResult          # runs the gate against the session's working diff
POST /api/v1/sessions/{sid}/ratify           # capauth-gated, emits an audit obligation
```

`ratify` spawns a reviewer subagent that applies `requesting-code-review` against the session's working diff plus a `verification-before-completion` pass, and returns a structured verdict (pass/fail + ranked findings). On fail it emits a `needs_input` SessionEvent (which already drives the sk-alert/Telegram push, spec §5.2), so ratification becomes a first-class, auditable, operator-visible capability rather than something a human remembers to run. It maps cleanly onto skcoord (a ratification card, §5.4) and onto the audit log (§7.4). This is the honest home for "ratify this session's work."

### 3c. Package the gate as a skskills skill (the distribution plug)

Wrap 3a's preamble and 3b's runner into a single versioned skill in `~/clawd/skskills/skills/` (for example `skcode-quality-gate`), so the discipline is fleet-wide, offered as a selectable option at dispatch, and reusable by skchat and skos, not hardcoded inside skcode-hostd. skcode consumes it; it is not a skcode-only asset.

**Recommendation:** ship 3a with Dispatch (P2), since it is nearly free once the profile-composition code exists, and land 3b + 3c as the durable built-in shortly after. Do not let P2 dispatch ship with no quality axis at all.

---

## 4. Question 2: optimizations, ranked by value

1. **Converge on ONE session abstraction before more code piles on the fork.** There are two parallel stacks: `Session`/`SessionManager`/`Spawner`/`gateway.py` (write-capable) and `SessionEvent` + the planned `Harness`/`HarnessSession`/`daemon.py` (read-only). Two Session models, two apps, two auth-wired entrypoints. skcode-hostd needs ONE descriptor (the spec's `SessionDescriptor`, §2) spanning list/stream/inject/spawn/model/archive/fork. Recommend making the `Harness` seam the single interface, folding `SessionDescriptor` in as the one record, and retiring `gateway.py` or reducing it to a thin alias, before P1 write code lands on top of the split. Every extra week of the fork multiplies the reconciliation cost.

2. **Evolve the auth gate from `bool` into an identity + scope + audit seam now.** `auth.py` `Verifier = Callable[[str], bool]` returns only pass/fail. The skcode security design (§7.4) requires `capauth.authz.decide(subject, capability, resource, context)` with per-device and per-repo scopes and a mandatory audit obligation. The current gate cannot express WHO the caller is, cannot do per-route capability checks, and cannot emit an audit record. Fine for P0 read-only, hard blocker for P2 dispatch. Recommend evolving the seam to `verify_caller(token) -> Principal | None` (identity + scopes) plus a `require_capability(principal, capability, resource)` helper NOW, so every route from P1 on is written against the real trust boundary rather than retrofitted.

3. **Harden the registry: atomic writes + do not silently drop state.** `registry.py` `_save()` does a full `write_text` on every mutation with no tmp-then-rename and no lock; `_load()` swallows `JSONDecodeError`/`OSError` and returns empty. Under a concurrent daemon (multiple sessions, multiple requests) plus the fleet's Syncthing sync (which has already caused conflict/corruption incidents on this fleet), a partial write can be read back as an empty session list, silently losing every session. Recommend atomic write (tmp file + `os.replace`) and at least logging on a load failure instead of returning empty silently. The interface is already clean for the P3 skcoord card-store swap, which is good, keep that.

4. **Add observability + a health route.** No structured logging, no per-call audit line, no metrics, and health is only a planned `hosts/self`. The spec (§7.4) demands an audit record per mutating call. Add a structured audit log line on every mutating route and a `/healthz` for the fleet doctor/soak (P3 rolls to four nodes).

5. **Single config source; kill the hardcoded `.158`.** `host_id` defaults to a literal `".158"` in the planned `daemon.py` and `serve.py`. For the four-node fleet roll that hardcode drifts. Move to one config source (env or a small pydantic `Settings`) so a node's identity, port, tmux session name, and sessions root come from one place.

6. **Packaging for the skcode role.** Add a `[project.scripts]` console entry (`skcode-hostd = "skharness.serve:main"`) instead of only `python -m skharness`; add a `py.typed` marker since skcode-hostd imports these types; and decide the package rename/alias story early (the package is still `skharness` while it is becoming skcode-hostd, an entry point named `skcode-hostd` is a cheap bridge). Also record the known `:9390` deploy conflict with `skcomms.transports.broker_server` in `~/.skcapstone/docs/PORTS.md` before deploy (already flagged in the plan).

7. **claude-code adapter fidelity + failure signaling (when Task 4 lands).** The planned default `runner` returns `proc.stdout` regardless of returncode, so a dead tmux server yields `""` and the harness reports zero sessions rather than "harness unavailable." Distinguish "no sessions" from "cannot reach tmux." Separately, the planned `new_lines()` prefix-diff over `tmux capture-pane` will mis-handle scrollback, wrapping, and Claude Code's TUI redraws, so the `assistant_text` stream will be noisy. Flag as a known-fidelity limit of the PTY floor; the typed pi/opencode server API is the real fix (spec §3.2 ceiling).

8. **Add CI.** A GitHub Actions (or fleet CI) job running pytest + ruff + mypy on push, plus a grep gate for em/en dashes, so the discipline that built skharness stays enforced as skcode grows. This is also the natural enforcement home for the no-write-surface invariant test the P0 plan already writes.

---

## 5. Prioritized action list

1. **Unify on one session abstraction** (the `Harness` seam + a single `SessionDescriptor`); retire or alias `gateway.py` and the `Spawner` stack before P1 write code lands. (Q2 #1)
2. **Upgrade the auth gate to a Principal + scopes + audit seam** and write all future routes against `require_capability`. Unblocks P2 dispatch and the audit story. (Q2 #2)
3. **Close the validation-as-a-skill gap:** add the dispatch-time `quality` profile wiring (3a) with P2, then the `ratify` Harness verb + `/api/v1/sessions/{sid}/ratify` endpoint (3b), packaged as a skskills skill (3c). (Q1 gap)
4. **Harden persistence + observability:** atomic registry writes, structured audit logging, `/healthz`, and CI (pytest + ruff + mypy + dash check). (Q2 #3, #4, #8)
5. **Finish the skcode-role polish:** console_scripts entry point, `py.typed`, single config source (kill hardcoded `.158`), and the claude-code adapter's "unavailable vs empty" + stream-fidelity handling. (Q2 #5, #6, #7)

**Single highest-priority recommendation:** unify on the one `Harness` seam + `SessionDescriptor` and, in the same pass, evolve the auth gate from `bool` to identity + scopes + audit. Everything else (dispatch, the security profiles, the quality/ratify layer, the fleet roll, the audit obligations) hangs off exactly those two seams. Doing this now is cheap; doing it after P1/P2 code has piled onto the current Spawner/gateway fork and the bool gate is the expensive path.

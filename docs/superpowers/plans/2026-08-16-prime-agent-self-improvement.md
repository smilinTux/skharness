# Prime-agent self-improvement: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans`. Each task below maps to exactly one coord card; claim the card before you start.

**Goal:** Build an honest outcome substrate for the autocode harness, implement `escalation_reason` (the one design-blessed feedback seam), and publish the written verdict refusing the closed learning loop.

**Architecture:** The terminal-state vocabulary lives only inside `engineering.py` and is destroyed on the way up, so `GateResult` gains an `outcome` field to carry it to the orchestrator, which writes one append-only row per item via the already-built-but-unwired `autopilot_cost.record_run`. Nothing learns; a human reads.

**Tech Stack:** Python 3.12, pytest, skharness `autocode` package, skcoord `Board`.

**Spec:** `docs/superpowers/specs/2026-08-16-prime-agent-self-improvement-design.md`

**Epic:** coord `935d4b61`

## Global Constraints

- **Never modify** `grading.py`, `sensitivity.py`, `buckets.py`, `data/joule-grade-vocabulary.json`, `golden-set-*.json`, `protected.py`, or `engineering.py`'s guardrail logic without human PR review. All are on the `_ALWAYS_PROTECTED` hard floor (`protected.py:29-51`).
- **`engineering.py` is protected.** Cards touching it are tagged `autopilot-untriaged` and require a human PR. Do not dispatch them to autopilot.
- **No new store.** Extend `autopilot_cost.record_run`. "Do NOT create a parallel grade store" is load-bearing in this ecosystem.
- **No backfill.** Historical rows carry no outcome vocabulary and nothing invents one.
- **`model_served` is Optional and never defaults to `model_requested`.**
- **Record from the orchestrator at every terminal state, never at settle.**
- **Negative controls or it did not happen.** Every new test must be made to fail on purpose once, against the current code or a permissive stub, and the red observed.
- **Commit as soon as the code is written and a fast check passes**, then run the full suite and amend. Never gate a commit on a background job.
- Commit trailer: `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`. No em dashes or en dashes anywhere.

---

## Wave 1: the substrate

Independent unless a dependency is stated. S1 unblocks S2; S2 and S3 unblock S4. Everything else in this wave is parallel.

| Card | Size/Risk | Title | Files |
|---|---|---|---|
| `fb7899fc` | S / med | S1: `GateResult` carries `outcome`, `tokens`, `cost_usd` | `types.py:121-129`, `tests/test_autopilot_types.py` |
| `c85b1f7b` | M / high | S2: populate outcome and usage at all five terminal returns | `engineering.py:399,428,440,453`, `direct.py:72`, `ratify.py:60` |
| `20710266` | M / med | S3: extend the `record_run` row | `autopilot_cost.py:78`, `tests/test_autopilot_cost.py` |
| `432b81b7` | M / high | S4: write one outcome row per item, every terminal state | `orchestrator.py:794,825-828,979` |
| `6ed2605c` | S / med | S5: a failed run's usage and cost must be recorded | `engineering.py:516,375` |
| `4eafea24` | S / med | S6: stop discarding the real grade at the pass return | `engineering.py:421,428` |
| `d44b9aba` | S / high | S7: retry count must survive to the recording point | `engineering.py:534-555` |
| `5989e9f1` | S / high | S8: a pass with a missing worktree wipes memory, then throws | `engineering.py:534,546,552` |
| `506782a4` | M / med | S9: close the memory asymmetry, record successes too | `failure_memory.py`, skcoord `coordination.py:507,558` |
| `146f70ac` | S / med | S10: `BuildUsage.model` records the adapter that actually ran | `joules.py:56,99`, `engineering.py:107-114` |
| `4b58afd1` | S / med | S11: the gated settle path has no double-settle guard | `engineering.py:555`, `direct.py:102`, `agentrun_bridge.py:351` |

**Wave 1 exit gate:** a deliberately failed build produces an outcome row carrying a distinct `outcome`, non-zero cost, and a non-zero retry count. If any one of those three is zero or constant, the wave is not done, because that is the exact shape of the censorship this epic exists to remove.

## Wave 2: the blessed seam

| Card | Size/Risk | Title |
|---|---|---|
| `9a7c0a86` | M / high | S12: implement `escalation_reason`, the one blessed feedback seam |

Reporting only. Nothing reads it to make a routing decision; wiring it into dispatch would be the autotuner card `09573989` AC6 forbids.

## Wave 3: the verdict

Runs in parallel with waves 1 and 2. Its content is already established by the research pass and does not depend on the substrate landing.

| Card | Size/Risk | Title |
|---|---|---|
| `dab87c81` | L / high | S13: the reward-gaming verdict, in writing. **Epic flagship** |
| `6ad3c9ab` | M / high | S14: the policy can never touch its own guardrails |
| `ba17d860` | S / low | S15: close B3 (shadow) as a documented decision |
| `d69aba73` | S / low | S16: close B4 (exploration budget) as a documented decision |

---

## Superseded cards

The original six B-cards are superseded and linked. Do not work them from the old framing.

| Original | Superseded by |
|---|---|
| `934a3c52` B0 | S1, S2, S4, S5, S6, S7 |
| `dfe2016e` B1 | S9 |
| `d502986d` B2 | S13 |
| `06a06524` B3 | S15, closed as a documented decision |
| `f81d8d2d` B4 | S16, closed as a documented decision |
| `aa5cc1b1` B5 | S14 |

## Coupling to epic 42770a62

That epic was decomposed into 8 parents and 23 leaves on 2026-08-16 and is not re-decomposed here. Two couplings:

- **`8967bf22` (A1, `RunRecord` schema, unstarted)** and S3 describe the same event. They must converge on one schema. Whoever starts second reads the other's card first.
- **A0 (which event store the run record binds to) is an open decision blocking A1 and A2.** It does not block this epic: `record_run` writes `SKAI_COST_DIR` JSONL, independent of the coordination-store question.

## Known traps

1. **A test that hand-injects a field the live path never produces.** This is how the skgateway bucket path passed its unit tests while raising a `ReferenceError` on every real request. Assert against the wired path.
2. **Asserting a pass writes a row.** That half already works. The load-bearing assertion is that a **failure** writes a row.
3. **Asserting `outcome` is populated.** A single constant satisfies that and destroys the vocabulary. Assert the five sites produce five **distinct** values.
4. **`_MAX_ROUNDS = 4` clips the retry distribution** exactly where under-grading would show. Do not read retry counts as an unbounded measure.
5. **`meta.autopilot.reverted` is a constant False** (card `1e5be0a7`: the merge record carries no sha, so revert can never fire). Any analysis reading "was this later reverted" is reading a dead sensor.
6. **Cards tagged `epic-child` are auto-selectable by `phase1_triage`**, which skips only `autopilot-untriaged`. The eight cards here that touch protected files or produce documents are already tagged. Do not remove that tag.

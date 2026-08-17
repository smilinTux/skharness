# Prime-agent self-improvement: the outcome substrate, the blessed seam, and the refusal

**Epic** `935d4b61`
**Written** 2026-08-16 by Lumina
**Status** design approved by Chef 2026-08-16; supersedes the epic's original framing
**Repo** skharness (with one skcoord card and one docs-only card)

---

## 0. Decisions locked by this design

These are settled. Re-open them only with a written reason on the card.

| # | Decision |
|---|---|
| D1 | The learned routing policy is **not built**. Cards B3 and B4 close as documented decisions, not as abandoned work. |
| D2 | `escalation_reason` is the **only** outcome-driven correction channel this epic builds. It was specified in the Joule Economy design (D2, soft ceiling) and never implemented. |
| D3 | Outcome recording is **append-only, from the orchestrator, at every terminal state**. Never at settle time. Inherited verbatim from card `09573989`. |
| D4 | The recorder is `autopilot_cost.record_run`, **extended**, never a new store. "Do NOT create a parallel grade store" is load-bearing in this ecosystem. |
| D5 | `GateResult` gains an `outcome` field. The terminal-state vocabulary must survive above `engineering.py`, where it is currently destroyed. |
| D6 | Every card touching `engineering.py` is risk `high` and cannot be autopilot-merged. `engineering.py` is on `_ALWAYS_PROTECTED`. This is a feature. |
| D7 | No backfill. Rows written before this lands carry no outcome vocabulary, and nothing invents one for them. |

---

## 1. Why the original epic was reshaped

Epic `935d4b61` asked whether the model-routing mapping should be *learned* from graded
outcomes rather than hand-written. The research pass on 2026-08-16 found that question
already answered, on four independent grounds, by decisions Chef approved on 2026-08-14
and by a red-team pass that ran on 2026-08-16.

**1. No autotuner.** Card `09573989`, acceptance criterion 6: a rubric change requires
human review, and `rubric_version` increments only when the golden set changes.

**2. No machine writes to routing policy.** `2026-08-08-model-ranking-routing-intelligence-arch.md`
section 10: the system suggests, humans commit.

**3. The rubric is already on the self-modification floor.** `protected.py:42-51`
unconditionally protects `grading.py`, `sensitivity.py`, `buckets.py`, the vocabulary and
`golden-set-*.json`, because an engine routed by that rubric could edit it and merge the
change behind a twin gate whose CI arm it satisfies with tests it wrote itself.

**4. The exploration slice was proposed and rejected.** That is card B4 by another name.
The recorded reasoning: the outcome instrument is a twin gate whose CI and coverage arms
are satisfied by tests the worker itself authors, so a down-classed pass measures the
gate's gameability rather than the model's sufficiency. Worse, the weak model's
misdiagnosis lands in `meta.autopilot.attempts` and is fed into the *control* arm's first
round, so the exploration arm corrupts the control arm through the card's failure memory.
The two arms are not independent and the counterfactual is not measurable this way.

And a fifth ground, which is a measurement fact rather than a policy choice. From
`09573989`: the grade causes the routing, so outcomes are not independent of predictions.
An accurate high-risk call produces more caution, which prevents the bad outcome, which
then reads as an overestimate. The prediction destroys its own falsifier. Supervised
calibration on this data is invalid by construction.

The epic's own acceptance criteria anticipated this: *"a documented decision NOT to close
the loop counts as success."* This design takes that outcome deliberately, and reinvests
the effort in the substrate, which no prohibition touches, and in the one seam the
approved design deliberately left open.

---

## 2. What the research corrected about the cards

Recorded here because the cards remain on the board and will be read again by someone else.

| Card claim | What the code says |
|---|---|
| B0: "the reward is a constant, score=5 hardcoded at 127 and 428, both on the PASS path" | Half right. `:428` is a real `GateResult` hardcode. `:127` is the `score` argument to `settle()`, a different defect: joules are minted at a constant regardless of grade. Separately, the *real* varying grade already exists and is persisted per round at `engineering.py:421` via `board.score_task`. The defect is that it is computed, stored, and then discarded two lines later. |
| B0: "the union store from 09573989 exists" | It does not. But `autopilot_cost.record_run` (`autopilot_cost.py:78`) is an append-only JSONL row of `{ts, date, card_id, repo, tokens, cost_usd, joules, passed, pr, run_id}`, never-raises, env-overridable, with roughly 45 tests. It is called only from `agentrun_bridge.py:316`. The gated path has never written a row. Most of the recorder is built and unwired. |
| Epic: "the orchestrator reaches several terminal states" | It reaches two. `orchestrator.py:802`/`:820` see only `passed` / not `passed`. `ci_red`, `no_op`, `salvage` and `direct_fail` are indistinguishable above `engineering.py`. This is why D5 exists. |
| Epic 42770a62: "the coordination store has exactly two writer values across 2,009 events" | Now 2,040 events across three: `lumina` (1,861), `noroc2027` (173), `lumina-skharness-graded` (6). The third encodes a dimension by concatenating it into the writer string, which is the convention-instead-of-fields failure A1 exists to prevent. |
| Epic 42770a62: "the unified card store at cards/ holds all of them" | `cards/` holds 11 directories against 4,853 in `tasks/`, each containing only `archive@noroc2027.jsonl`. Three stores at three populations, the newest near-empty. |
| Card A5: "the projections are wrong" | True, and worse than stated. `agents/` holds 114 files, 6 of them Syncthing `sync-conflict` copies, and one conflict copy carries four `completed_tasks` its canonical twin lacks. A projection read returns different answers depending on which duplicate wins. |

---

## 3. Live defects found during research, none of which had a card

1. **The cost cap does not cap.** `orchestrator.py:800` calls
   `ledger.add(getattr(result, "tokens", 0), getattr(result, "cost_usd", 0.0))`, but
   `GateResult` declares neither field, so the ledger always adds zero. The budget ceiling
   on the engineering path is inert. Anything claiming autopilot runs are cost-capped is
   wrong today.
2. **`BuildUsage.model` is false for two adapters.** `engineering.py:112-114` takes a
   fallback branch that never sets `model`, so it stays at its default literal
   `"claude-code"` even when pi ran the build.
3. **Failure memory is cleared, then the run throws.** `_archive_attempts` at `:535`
   precedes the worktree-missing `raise` at `:546`, and the second `if result.passed`
   guard sits at `:552`. A pass whose worktree vanished wipes the card's memory, settles
   nothing, and records only `finalize-failed`.
4. **No double-settle guard on the gated path.** `agentrun_bridge.py:351` has
   `already_settled`; `engineering.py:555` and `direct.py:102` have none. A finalize retry
   can double-mint.
5. **`escalation_reason` is unimplemented.** Zero occurrences in `src/`. The soft-ceiling
   half of Joule D2 exists on paper only, so there is no record of when a bigger model was
   used, which is precisely the signal the approved design designates as the training data
   that corrects a bad rubric.
6. **Graded dispatch is unarmed on both ends.** Only `pi` implements
   `supports_model_override()`; `claude_code` and `opencode` raise
   `ModelOverrideUnsupported`. No card carries a grade, and `buckets_enabled` is off at the
   gateway. The CHANGELOG's claim that graded dispatch replaced `harness_model` is
   aspirational; `config.py:58` is still the only live selector.

Defect 6 is why B3 would have had almost nothing to shadow.

---

## 4. Architecture

Three waves. Wave 1 is the substrate, wave 2 is the blessed seam, wave 3 is the written
verdict. Waves 1 and 2 are independent of each other; wave 3 depends on neither and can
run in parallel, since its content is already established.

### 4.1 The terminal-state seam

The vocabulary problem is the crux. `engineering.py` is the only layer that knows *which*
terminal state occurred; the orchestrator is the only layer that sees every item and
already holds `run_id`, the lock, and the item. Neither alone is sufficient.

Resolution: carry the vocabulary up in the result.

```
GateResult(score, passed, notes, artifact, mode)
  + outcome: str          # pass | ci_red | no_op | salvage | direct_fail
  + tokens: int = 0
  + cost_usd: float = 0.0
```

Five values for five sites. `ratify.py:60` also returns a `GateResult` but is called from
skcode's ratify endpoint rather than the orchestrator, so it produces no row and is out of
scope here. It must still populate `outcome` or the field becomes optional in practice,
which is how a vocabulary rots.

`outcome` is populated at each of the five terminal `return GateResult(...)` sites
(`engineering.py:399`, `:428`, `:440`, `:453`, `direct.py:72`). `tokens` and `cost_usd`
are populated from the accumulated `BuildUsage` at the same sites, which also repairs the
inert cost cap at `orchestrator.py:800` as a side effect rather than as a separate fix.

The orchestrator then writes exactly one `record_run` row per item, at the existing
single guarded write site (`orchestrator.py:825-828`), plus the three paths that bypass
it: `claim-raced` (`:798`), `off-node` (`run_once:979`), and the kill-switch and
budget-hit silent returns (`:775`, `:783`).

`record_run` gains the fields it lacks: `outcome`, `adapter`, `model_requested`,
`model_served`, `score`, `retries`, `quality_mode`, `work_grade`. Existing rows lack them
and stay lacking them, per D7.

### 4.2 Ordering repair

`_archive_attempts` must stop running before the recording point. The retry count is read
and stamped onto the outcome row *before* anything archives, so a card that passed on
round three records three rather than zero.

### 4.3 Symmetric memory

Successes cannot be stored in `meta.autopilot.attempts[]`: `clear_attempts` wipes that
array wholesale on the next pass, so a success would be destroyed by the very event that
created it. They go to a sibling key with their own renderer, since
`failure_memory._render` is failure-phrased in its literal text. The read discipline is
preserved exactly: distinct-newest dedup, a 3-entry bound, a 600-character ceiling,
oldest dropped first.

### 4.4 The blessed seam

`escalation_reason` is written whenever the served model exceeds the card's `model_class`
floor. Per Joule D2 the ceiling is soft but must be recorded, and the energy overage is
debited. The exit gate for this seam, from design section 9 phase 2, is that the
escalation rate per class is stable and **explainable by a human**, not merely predictive.
This is deliberately a reporting seam, not a control seam. Nothing reads it to make a
routing decision.

### 4.5 The guardrail assertion

B5 is unchanged and takes its shape from `skcapstone/fleet/drill.py:90`:
*"Always structural, never advisory."* The properties to copy are that the guard raises a
typed error rather than returning a falsy value, that it re-runs on every call rather than
trusting a value captured earlier, and that it never consults an ambient environment
variable for its target. The negative control is a test that attempts to make the routing
layer alter the twin gate, the CI requirement, the acceptance criteria or the ratification
step, and proves each attempt fails.

---

## 5. Testing

Every card carries a negative control. The standing rule in this repo is that "built"
means nothing unless it names the test that exercises the live path, and that a new test
must be made to fail on purpose once and observed red.

The specific traps this design must not fall into:

- A test that hand-injects a field the live path never produces. This is exactly how the
  skgateway bucket path passed its unit tests while raising a `ReferenceError` on every
  real request.
- Asserting the recorder writes a row on a pass. The load-bearing assertion is that a
  **failed** build produces a row, because that is the half the current stores censor.
- Asserting `outcome` is populated. The load-bearing assertion is that each of the five
  terminal sites produces a *distinct* value, since a single constant would satisfy a
  naive test and destroy the vocabulary.

---

## 6. What this design explicitly does not do

- It does not build a learned policy, a bandit, an autotuner, or an exploration budget.
- It does not write to `grading.py`, `sensitivity.py`, `buckets.py`, the vocabulary or the
  golden set. Those are on the protected floor.
- It does not re-derive `model_class`; that is computed once and read from the card.
- It does not touch the sovereignty axis. Sensitivity is never model-decided.
- It does not backfill attribution or outcome vocabulary onto historical rows.
- It does not make `escalation_reason` load-bearing for any routing decision.

---

## 7. Relationship to epic 42770a62

That epic was decomposed into 8 parents and 23 leaves at 19:09-19:40 UTC on 2026-08-16.
This design does not re-decompose it and does not duplicate it. Two couplings matter:

- **A1's `RunRecord`** and this design's extended `record_run` row describe the same
  event. They must converge on one schema rather than shipping two. A1 is unstarted, so
  the cheap resolution is that A1's schema absorbs the fields in section 4.1 and this
  epic's recorder emits it.
- **A0 is an open decision that blocks A1 and A2**: which event store the run record binds
  to. This design does not pre-empt it, because D4 already commits to `record_run`, whose
  storage is `SKAI_COST_DIR` JSONL and independent of the coordination store question.

Two hazards were flagged on that epic and are not yet fixed. They are restated here
because they will bite this epic too. All 31 of those cards carry `epic-child` and are
auto-selectable by `phase1_triage`, which skips only `autopilot-untriaged`. And
`autopilot-pi.yaml` maps only four repos, so making pi the default would quietly make most
of the epic's own cards unselectable. They would not error; they would stop being picked,
which reads as an idle board.

---

## 8. Open questions, carried not closed

1. Does A1's `RunRecord` absorb section 4.1's fields, or does `record_run` stay separate
   and A1 wrap it? Cheap either way, but it must be decided once.
2. Should the constant `score=5` passed to `settle()` (`engineering.py:127`) become the
   real grade? It changes minted joule values, so it is an economic change and not merely
   a correctness one.
3. `1e5be0a7` remains open: the merge record carries no sha, so revert can never fire on
   automerged work and `meta.autopilot.reverted` is a constant False. Any future
   measurement that reads "was this later reverted" is reading a dead sensor. Not in this
   epic's scope, but no outcome analysis should trust that field until it closes.

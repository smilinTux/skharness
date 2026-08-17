# B3 and B4 closure decisions

**Epic** `935d4b61`
**Cards closed** `06a06524` (B3, shadow the policy), `f81d8d2d` (B4, exploration budget)
**Coord cards recording this closure** S15 (`ba17d860`), S16 (`d69aba73`)
**Written** 2026-08-16 by Lumina
**Verified against** skharness `main` at commit `2e8affd` (`git log -1 --oneline`)

Context: epic `935d4b61` was reshaped on 2026-08-16 with Chef's approval. The full
reasoning lives in
`docs/superpowers/specs/2026-08-16-prime-agent-self-improvement-design.md` (decision D1:
"The learned routing policy is not built. Cards B3 and B4 close as documented decisions,
not as abandoned work."). This document is the write-up that decision requires, one
section per card.

Under the epic's own acceptance criteria, a documented decision not to close the loop
counts as success. Neither closure below is an abandonment.

---

## S15 / Card `06a06524`: B3, "shadow the policy"

**Decision: close as a documented decision. Do not build.**

B3 asked to run a learned routing policy in shadow against the incumbent
(`config.harness_model`), logging what the policy would have chosen without acting on it.
Two independent reasons make this not worth building right now, plus a discipline worth
keeping for later.

### Reason 1: there is almost nothing to shadow

Verified directly against skharness `main` (`2e8affd`):

- Only the `pi` adapter implements a working `supports_model_override()`. It returns
  `True` at `src/skharness/autocode/adapters/pi.py:55`.
- `BaseCliAdapter.supports_model_override()` defaults to `False` at
  `src/skharness/autocode/adapters/base.py:204`. Neither `claude_code.py` nor
  `opencode.py` overrides it, so both inherit the `False` default. `base.py:252`
  (`_run_raw`) checks that flag and raises `ModelOverrideUnsupported` rather than
  silently dropping a requested override when the adapter cannot honour it
  (`base.py:252-254`).
- `src/skharness/autocode/adapters/codex.py` is a fail-closed stub end to end
  (`CodexStubAdapter`, lines 1-34). Its `assess`, `run_task`, and `grade` methods all
  raise `HarnessUnavailable` unconditionally. There is no model-selection behavior to
  shadow at all on this adapter, because there is no working behavior on it.
- `src/skharness/autocode/config.py:58` declares `harness_model: str | None = None`,
  confirmed as still the one live, static model selector. Nothing in the codebase
  currently overrides it in a way three adapters could disagree about.

All four of these line references match exactly what was supplied for verification
(`pi.py:55`, `base.py:204`, `codex.py` as a stub, `config.py:58`); no correction needed
on this reason.

A shadow policy compares what it would have chosen against what actually ran. With one
adapter able to accept an override, two that fail closed, and one that is entirely
unimplemented, there is one live choice to shadow against itself. The comparison would
have nothing to measure.

### Reason 2: the policy it would shadow is not being built

Per the epic reshape (`2026-08-16-prime-agent-self-improvement-design.md`, decision D1
and section 1), the learned routing policy itself is not being built, on five independent
grounds: no autotuner is permitted on the grading rubric (`09573989` acceptance
criterion 6), no machine writes to routing policy
(`2026-08-08-model-ranking-routing-intelligence-arch.md` section 10), the rubric sits on
the self-modification protected floor (`protected.py:42-51`), the exploration slice
needed to gather counterfactual training data was proposed and rejected (this is B4,
closed separately below), and the outcome data is not independent of the routing
decisions it would train on (the grade-causes-routing feedback loop in `09573989`).
There is no policy left to shadow.

### What is preserved for later

The shadow-first rollout *shape* is correct discipline and this fleet already uses it
twice: signing off/permissive/enforce, and the profile gate off/shadow/enforce (shipped
off). If a learned routing policy is ever revisited, shadow-first is still the right way
to introduce it. B3's own warning on the card stands and is worth carrying forward
verbatim: "a policy that merely agrees with the incumbent has demonstrated nothing." A
shadow phase only earns its keep once there is enough live surface for disagreement to be
observable, which reason 1 above shows does not exist today.

### CHANGELOG correction

`CHANGELOG.md`, under `## [Unreleased]`, the first `### Added` block (lines 12-31 at the
verified commit) states that graded dispatch is "replacing the single static
`autocode.config.harness_model`." That is aspirational, not true of the shipped code: as
shown above, only `pi` can accept an override, no card currently carries a grade so
`bucket_for_payload` has nothing to route on, and the gateway's `buckets_enabled` is off.
`harness_model` remains the only live selector. The CHANGELOG entry has been corrected in
this same change to note the gap and point at this document, rather than claim a
replacement that has not happened.

---

## S16 / Card `f81d8d2d`: B4, "exploration budget"

**Decision: close as a documented decision. Do not build.**

B4 asked for an explicit, capped exploration budget: deliberately dispatching some cards
below their class floor to buy counterfactual data about whether a cheaper model would
have sufficed. This was already proposed and rejected by a red-team pass recorded on card
`09573989`, before B4 was filed. B4 does not cite that pass and would have rebuilt the
rejected thing. It is closed here with that reasoning attached so it cannot be
re-proposed from scratch without first reading why it was rejected.

### The red-team's reasoning (quoted from card `09573989`)

> Deliberately dispatching below the class floor to buy counterfactual data was proposed
> and rejected. The outcome instrument is a twin gate whose CI and coverage arms are
> satisfied by tests the worker itself wrote, so a down-classed pass measures the gate's
> gameability rather than the model's sufficiency, manufacturing exactly the downward
> pressure the design exists to prevent.

That is the first, on-its-own-sufficient reason: the measurement B4 wants to take would
not measure what it claims to measure. A down-classed pass proves the weak model could
satisfy a gate it helped write the tests for, not that it was competent to do the work.

### The subtler half, which is the one that actually kills it

The exploration arm is not independent of the control arm it is meant to be compared
against. When a down-classed dispatch fails or misdiagnoses the work, that failure is
written into `meta.autopilot.attempts` on the card. The next dispatch of that same card,
at its correct class, reads that failure memory as its round-one context. So the
exploration arm's mistakes are fed directly into the control arm's first round: the
exploration arm corrupts the control arm through the card's own failure memory. The two
arms are not independent, which means the counterfactual B4 wants (would a cheaper model
have sufficed, with the answer uncontaminated by the attempt to find out) is not
measurable by this method at all, no matter how the budget is capped or bounded. An
improved shadow-exploration variant was considered against this same objection and
dropped too.

### A third reason B4 did not know: the existing cap does not cap

B4 asks for "an explicit capped exploration budget." Verified directly against skharness
`main` (`2e8affd`):

- `src/skharness/autocode/orchestrator.py:800` calls
  `ledger.add(getattr(result, "tokens", 0), getattr(result, "cost_usd", 0.0))`.
- `GateResult` is declared at `src/skharness/autocode/types.py:122` (one line off the
  `~121` estimate supplied for verification; the class body itself starts at 123) with
  fields `score`, `passed`, `notes`, `artifact`, and `mode`. It declares neither `tokens`
  nor `cost_usd`.

Because `GateResult` never carries those fields, `getattr(result, "tokens", 0)` and
`getattr(result, "cost_usd", 0.0)` fall through to their defaults on every call, so
`CapLedger.add` always adds zero. The budget ceiling on the engineering path is inert
today. B4 would have built an explicit cap on top of a ceiling that already does not
work, without noticing the ceiling was inert, because nothing in B4's own scope would
have exercised it. Cards S1 (`fb7899fc`) and S2 (`c85b1f7b`) are the repair for this
defect; they are cited here, not duplicated.

### The sanctioned substitute (quoted from card `09573989`)

> Mine the fleet's natural quasi-experiments instead

Concretely: model assignment already varies with backend availability, and the salvage
and direct dispatch paths already produce off-gate outcomes, as does the `.100` outage
class of event. These are cases where a cheaper or different model ran the work for
reasons unrelated to a deliberate experiment, so they carry counterfactual signal without
the twin gate's gameability problem and without corrupting a card's own failure memory to
get it. This is recorded as the path forward if this question is revisited: look for
naturally occurring variation in the fleet's own operation before building a mechanism
that manufactures variation on purpose.

---

## Verification notes

Everything above marked "verified" was read directly from skharness `main` at `2e8affd`
during this write-up, not taken on faith from the source cards. Two small discrepancies
turned up against the line numbers supplied as a starting point, both noted inline above:

- `GateResult`'s class statement is at `types.py:122`, not `~121` as given; the field
  list itself begins the line after. Immaterial to the finding (no `tokens` or `cost_usd`
  field exists either way).
- Every other line reference supplied (`pi.py:55`, `base.py:204`, `config.py:58`,
  `orchestrator.py:800`) matched exactly with no correction needed.

Everything under "the red-team's reasoning," "the subtler half," and "the sanctioned
substitute" above is quoted verbatim from card `09573989`, not independently re-derived;
that card's own reasoning, not this document's, is the source of authority for those
claims.

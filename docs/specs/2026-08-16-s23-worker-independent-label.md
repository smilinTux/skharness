# S23: the worker-independent outcome label, and the demotion of human ratification

Card `33c50540`, epic `935d4b61`. Written 2026-08-16.

This document is the written half of card `33c50540`. It records two things:
what the shadow mutation label can and cannot tell you today, and the formal
disposition of human ratification as a control.

---

## 1. What the refusal was, and why it needed an owner

The epic refused to close a learning loop over the harness's own grades. The
load-bearing ground for that refusal, from the S13 verdict (`dab87c81`) section
4.6: the twin gate's CI and coverage arms are satisfied by tests THE WORKER
ITSELF AUTHORED, so a passing score partly measures the worker's ability to
write a passing test, and any learner trained on it ratchets downward.

The same document names what would DEFEAT that ground: an outcome label the
worker did not author. Section 3.4 records that none exists.

So the refusal was never a permanent judgement. It was a deferral with a named
reopen condition. But nobody owned building that condition, and **an ownerless
reopen condition is a permanent refusal wearing a provisional label**. That is
worse than either an honest permanent refusal or an honest attempt, because it
stops anyone re-examining the question while looking like it invites
re-examination.

`src/skharness/autocode/mutation.py` is that condition, built.

## 2. What was built

Mutation testing over CHANGED LINES, shadow only, recorded on the existing
outcome row (`autopilot_cost.record_run`, one append-only row per item per
terminal state). No second store.

The property that matters, and the only one: **the worker cannot author the
mutants**. The operator table lives in the harness, fixed. A worker can write a
test that satisfies coverage; it cannot write a test that satisfies a mutation
operator without actually asserting on the behaviour the operator changed. It
also cannot weaken the operators the way a diff can add an `omit` rule to a
coverage config.

Three states, `mutation_state` in `{survived_clean, mutants_survived,
unobserved}`. The asymmetry is deliberate and is the core of the design:

* a SURVIVOR is decisive even on a partial run. One mutant the worker's tests
  did not notice is a fact about the diff whether or not every other site was
  tried;
* the ABSENCE of a survivor over a capped, timed-out or otherwise incomplete run
  is `unobserved`, never `survived_clean`. It shows the sample was small, not
  that the diff is clean.

This is the same three-state discipline S12 adopted for `escalation_state`
(which reports `escalation_rate: null` beside `observed_fraction: 0.0` rather
than "0 percent escalation", because a two-state design would have been
indistinguishable from a healthy fleet). An epic that invented that rule and
then shipped a two-state mutation label would have learned nothing from itself.

The seven `mutation_*` columns are DERIVED inside `record_run` from the raw
probe report, following the house pattern (`escalation_state` from S12,
`grade_size`/`grade_risk`/`grade_sensitivity` from a peer session's PR #45). No
caller can stamp a state that disagrees with its own counts: a report carrying a
survivor cannot write a clean row.

## 3. Cost, and the sampling rule, stated plainly

Mutation testing costs one scoped test run per mutant. Measured on this card's
own diff in this repo: 102 mutable sites over 705 changed lines across four
files, and the scoped test command takes roughly 17 seconds. A COMPLETE run over
that diff would have been about 29 minutes of wall clock for one card.

That is too expensive to run on every build, and this document says so rather
than pretending otherwise. The bounds, all recorded on the row:

| bound | default | effect when hit |
| --- | --- | --- |
| changed lines only | always | a ten-line diff never triggers a repo-wide run |
| `max_mutants` | 20 | `complete=False` |
| `timeout_s` (wall clock) | 900 | `complete=False` |
| `per_mutant_timeout` | 120 | that mutant counts as `unobserved_mutants` |

**The sampling rule, explicitly:** when a diff has more mutable sites than the
cap allows, the probe takes an EVEN SPREAD across the diff (not the first N, so
a capped run at least looks at the whole change), and it sets `complete=False`.
The classifier then refuses to read that run as `survived_clean`. This is the
card's own requirement made structural: a sampled label reported as universal is
exactly the failure this epic exists to remove, so a sampled run **cannot**
report a universal verdict. It can only report a survivor (which is real) or
`unobserved` (which is honest).

A per-mutant timeout is `unobserved_mutants`, never `killed`. Counting a hung
mutant as killed would fold an unknown toward the good direction, and a suite
hanging for an environmental reason would then report as a clean sweep. That is
the two-state failure again, one level down.

A red baseline is `unobserved` with reason `baseline_red`. If the suite is
already failing, killed and survived mean nothing: every mutant "dies" for a
reason that has nothing to do with the mutation, and a red baseline would
otherwise report a perfect kill rate.

## 4. What this can tell you TODAY, and what it cannot

**It cannot yet tell you anything from the ledger.** Every live row records
`mutation_state: unobserved`, `mutation_unobserved_reason: not_run`. The reason
is structural and is stated here rather than buried: the ONE site that holds a
live worktree at grade time is `engineering.py`, which is on the protected floor
and outside this card's scope to modify. `record_outcome_row` runs at a terminal
state where the worktree may already be pruned, so it cannot run the probe
itself.

What IS wired: `GateResult.mutation_report` carries the raw report,
`record_outcome_row` forwards it, and `record_run` derives the state. The moment
an executor stamps a report, real rows appear with no further change to the
ledger. A test (`test_the_orchestrator_forwards_a_report_the_moment_one_exists`)
is the negative control proving that seam is live rather than merely absent.

**It can tell you something now, by hand.** The module carries its own entry
point (`python -m skharness.autocode.mutation --worktree ... --base ...`), so a
human or a follow-up card can produce real data over any branch today without
waiting on the executor wiring. That entry point writes no ledger row:
recording is the caller's decision, and a probe that wrote rows by itself would
be the second store this card forbids.

**The follow-up that closes the gap** is one call in `engineering.py`'s gate
round, beside the existing `diff_coverage(repo, wt, diff)`, stamping
`mutation_report` onto the returned `GateResult`. It is deliberately NOT done
here: `engineering.py` is on the protected floor and a sibling guard asserts
that floor is untouched.

**Also not done here, deliberately:** `mutation.py` itself should join
`protected._ALWAYS_PROTECTED`, for exactly the reason the coverage config is on
that floor (an engine that can move its own instrument does not need to satisfy
it). Adding it requires editing `protected.py`, which the same sibling guard is
currently asserting is untouched. It is recorded here so it is not forgotten,
not silently skipped.

## 5. Human ratification: FORMALLY DEMOTED

The gaming analysis leans hardest on human ratification as the mitigation that
makes a self-graded loop safe. S13 measured it answering at zero. That
measurement was independently reproduced on 2026-08-16 against the live GTD
store (`~/.skcapstone/coordination/gtd`):

| population | autopilot decisions | answered |
| --- | --- | --- |
| open (`waiting-for.json`) | 38 | **0** |
| archived (`archive.json`) | 82 | 20 |
| total | 120 | 20 (16.7 percent) |

Three further facts, measured today, that the headline numbers do not carry:

1. **Every answer ever recorded predates 2026-07-26.** The 20 answered items
   were created on 07-14 (11), 07-15 (4) and 07-26 (5). The oldest item in the
   open queue was created 2026-07-27. The control did not decline gradually. It
   stopped operating on 2026-07-26, and every decision raised since has gone
   unanswered.
2. **The morning digest is built and never sent.** Today's manifest
   (`autopilot-digest.json`, `digest_date: 2026-08-17`) exists with 100 items
   and `sent_at: null`. `digest.py`'s own docstring says it: "Sending the DM is
   Phase F; this module only builds and persists." The send arm was never built.
   This is the built-but-unwired class (card `bb536f68`, now with ten recorded
   instances) applied to a human process instead of to code, and the mechanism
   is the same one: a component exists, is exercised daily, produces an artifact
   nobody consumes, and its liveness is indistinguishable from its absence.
3. **The manifest counts archived items.** 100 = 38 open + 62 archived-and-never
   -answered. `build_manifest` filters on `source == "autopilot"` and
   `not answered` across every GTD file including `archive.json`, so even if the
   DM were sent it would present 100 numbered questions of which 62 are about
   decisions already closed by archival, and the reply-by-number index would not
   match the live queue.

### Disposition

**Human ratification is DEMOTED. It is removed from the list of controls any
analysis in this epic may lean on.** It is not a mitigation, it is a queue.

This is the honest disposition rather than "make it real", for a reason worth
stating: making it real is a notification system plus a human commitment to a
daily answering budget, and neither is in this card's scope or gift. Listing a
control that does not operate is worse than listing no control, because it lets
an analysis discharge a risk against something that has answered zero questions
in three weeks. Any future argument in this epic that a loop is safe "because a
human ratifies it" must first point at a non-zero answer rate measured after the
date on this document.

Two consequences follow immediately and are recorded so they are not lost:

* the shadow mutation label above must NOT be promoted to a gate on the strength
  of "a human reviews the merges anyway". Measured, they do not;
* `routing_guard._GATE_DECISION_FIELDS` still fences `ratified`,
  `ratification`, `ratify` and `human_review` against the routing layer. That
  fence stays: it is defense in depth against a future field of that name, and
  it costs nothing. It is a fence around a control that does not operate, which
  is fine, and it is not evidence that the control operates.

## 6. Where the reopen condition now stands

The card's success criterion was not "the loop is closed". It was that the
refusal becomes falsifiable: someone can point at real data and argue the reopen
condition is met, or point at it and argue it is not.

That is now possible, and the honest current reading is: **not met.** The
instrument exists, it produces a real verdict on a real diff (see section 3, and
the run recorded in the S23 report), and the ledger's own summary
(`autopilot_cost.mutation_summary()`) reports `observed_fraction: 0.0` and
`survival_rate: null` because no executor stamps a report yet. That is an
argument someone can now make against real data instead of against a gap.

The condition flips when two things are true and both are measurable:
`observed_fraction` on the ledger is materially above zero, and the survival
rate over those observed rows is low enough that a passing twin-gate score is
not mostly measuring the worker's own test-writing. Neither number can be argued
today; both can be argued the moment the executor stamps a report.

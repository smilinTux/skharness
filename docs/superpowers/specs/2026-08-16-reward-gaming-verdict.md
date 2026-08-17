# The reward-gaming verdict: what stops the loop learning to weaken its own gate

**Epic** `935d4b61` (flagship deliverable, card `dab87c81`)
**Written** 2026-08-16 by Lumina
**Status** decision recorded. This document is itself the gate named in card
`dab87c81` acceptance criterion 3: no policy may consume `GateResult.score`
while it stands.
**Pins to** skharness `2e8affdc19fdcff0b48a1fb9296ae416484725b5`. Every
`file:line` below was read at that commit. If you are reading later, re-derive
the line numbers before quoting them at anyone; see section 9 for a worked
example of a citation that had already drifted before this document was written.

---

## 0. What this document decides

The epic asked whether the model-routing policy should be *learned* from graded
outcomes. That question has a prerequisite: if a policy is going to be trained
on `GateResult.score`, something must stop the thing being scored from learning
to move the score instead of doing the work.

This document answers that prerequisite question, vector by vector, and then
records the refusal to close the loop with five grounds a future session can
check rather than re-derive.

The answer, in one sentence: **the score is protected against being weakened
mid-attempt, and it is not protected against being satisfied by artifacts the
worker itself authored, and there is currently no held-out check and no
functioning human ratification channel that would catch the difference.**

That is not a reason for despair. It is a reason the loop stays open. An open
loop with a human in it is not a degraded closed loop; it is the correct design
for a reward signal that partly measures the ability to write a passing test.

---

## 1. Verdict summary

| Vector | Is there a mechanism? | Where it lives |
|---|---|---|
| Weakening acceptance criteria mid-attempt | **Yes, structural.** The criteria are snapshotted into `item.payload` before round 1 and both the build brief and the grade brief read that same snapshot | `orchestrator.py:322-327`, `engineering.py:349`, `:371`, `:417` |
| Weakening acceptance criteria between attempts | **No.** Nothing prevents a worker from editing the card; the next dispatch reads the edited card | same, plus `skcoord/coordination.py` write path |
| Inflating coverage by padding unchanged lines | **Yes.** Coverage is computed over added lines only | `ci.py:127-148`, `:176-188` |
| Inflating coverage by diffing where coverage cannot see | **No.** A changed-line set that intersects no measured class returns `1.0` | `ci.py:187-188` |
| The CI arm | **No.** CI green is produced by running tests that live in the same diff being graded | `ci.py:115-123`, `engineering.py:229-238` |
| A held-out check the agent cannot see | **Does not exist.** Nothing in `src/` implements one | verified by absence, section 3.4 |
| Human ratification outside the loop | **Exists in code, not in practice.** The channel is `digest.queue_decision`; 38 items sit unanswered, oldest 20 days | `digest.py:113-131`, GTD `waiting-for.json` |
| Any policy consuming the score today | **None.** `.score` is read by the twin-gate predicate, one salvage branch, and report text | section 2.3 |

Four rows say "no". Two of them get the longest treatment: the CI arm, which is
the deepest problem in the design (section 3.3 addresses it directly rather than
mitigating it), and human ratification, where the gap between the code and the
observed behaviour is large enough to be worth measuring rather than asserting
(section 3.5).

---

## 2. What the reward actually is

Before arguing about gaming it, three facts about the signal, all read rather
than assumed.

### 2.1 The grader sees five things, and it does not run anything

`adapters/base.py:453-469` is the whole grade call. The instruction is

> "You are an independent grader. Score the diff 1-5 against the acceptance
> criteria and CI status. A 5 requires: every acceptance criterion met, tests
> present and passing, CI green."

and the payload it receives is exactly
`{task_id, diff, acceptance, ci_status, diff_coverage}` (`base.py:461-463`).

Note what is absent. The grader does not execute the tests. It does not see the
repository outside the diff. It is *told* `ci_status` and `diff_coverage` as
values. Its independence is real but narrow: `engineering.py:420` calls it in a
fresh session with no shared context with the build, so it does not inherit the
builder's reasoning. It is not independent of the builder's *artifacts*, because
the diff and the tests inside it are all the evidence it has.

The grader is smart enough to notice at least the crudest inconsistency. A live
decision item queued on 2026-07-27 for task `10f3d23b` records the grader saying,
of an empty diff, that the claimed CI status and diff coverage in the task data
are unsubstantiated and should not be trusted. So the grader does police the
gap between the reported signals and the visible work. That is evidence of a
partial defence, and it is a model behaviour, not a mechanism.

### 2.2 The twin gate is a conjunction of four things, one of which is a literal

`engineering.py:61-72`:

```python
def twin_gate_passed(gr, ci_status, cov, repo) -> bool:
    cov_ok = cov is not None and cov >= repo.min_diff_coverage
    return (gr.score == 5 and is_complete(gr.notes)
            and ci_status == "green" and cov_ok)
```

Four conjuncts: the model's 5, an independent `<promise>COMPLETE</promise>`
token, external CI green, and diff coverage at or above the repo floor. Note
`cov is not None`: a repo with no `coverage_cmd` can never pass the twin gate at
all, which is a fail-closed choice worth keeping.

On a pass, the returned score is a literal: `engineering.py:428` constructs
`GateResult(score=5, passed=True, ...)`. The *varying* grade is persisted a few
lines earlier at `:421` via `board.score_task(...)` and then discarded. So any
future calibration that reads `GateResult.score` off the pass path reads a
constant 5, not a grade. That defect is already recorded in the reshape design
(section 2, B0 row) and is repeated here because it changes what a naive
consumer would learn: nothing.

### 2.3 Nothing consumes the score as policy today

Verified by grep over `src/` at the pinned commit. `.score` is read at:

- `engineering.py:71` (the gate predicate itself)
- `engineering.py:437` (the salvage branch: `gr.score is None` plus CI green plus
  coverage met opens a human-reviewed PR, never an auto-merge)
- `engineering.py:453`, `ratify.py:60` (result construction)
- `orchestrator.py:805`, `:819`, `:824`, `daemon.py:372-382`,
  `agentrun_bridge.py:300`, `:383`, `:402` (journal entries, notifications and
  human-readable report text)

No routing decision, no rubric update, no model selection, no wallet amount
reads it. Card `dab87c81` acceptance criterion 3 asks that this remain true
while this card is open. It is true at the pinned commit.

---

## 3. The five vectors

### 3.1 Weakening the acceptance criteria

**Mechanism: yes, for the duration of an attempt. No, across attempts.**

The criteria are lifted off the card once, at `WorkItem` construction:
`orchestrator.py:322-323` maps `task["acceptance_criteria"]` into
`payload["acceptance"]`. `engineering.py:349` binds `p = item.payload` once, and
then *both* the build brief (`:371`) and the grade brief (`:417`) read
`p.get("acceptance", [])` from that same in-memory dict, on every one of the
four rounds.

The consequence is precise and worth stating exactly: a worker that edits its
own card mid-run does not change what it is graded against in that run. The
snapshot is taken before the first model call and is never re-read. Immutability
during the attempt is achieved by data flow, not by permission.

That is genuinely structural, and it is also narrower than it sounds. Three
limits:

1. **It is not enforced, it is incidental.** No test asserts the snapshot
   property. It holds because `p` is a local binding, and a future refactor that
   re-reads the card between rounds (to pick up, say, an operator's edit) would
   silently delete this defence. If this property is to be relied on, it needs a
   negative-control test that mutates the card mid-run and asserts the grade
   brief is unchanged. There is no such test today.
2. **It says nothing about attempt N+1.** A worker that weakens the card's
   criteria on round 4 of a failing run has weakened them for the next dispatch,
   which reads the card fresh. `_MAX_ROUNDS = 4` (`engineering.py:226`) makes a
   terminal non-pass a routine event, so "the next dispatch" is not a rare path.
3. **Criteria authored before the attempt is a convention, not a check.**
   Nothing in the harness compares the criteria it graded against to the criteria
   that existed when the card was created. Cards are edited by humans and by the
   decomposer, legitimately, all the time. There is no signed or hashed
   pre-attempt criteria record, so there is also no way to detect the
   illegitimate case.

**What would close it:** hash `payload["acceptance"]` at dispatch, store the
hash on the outcome row, and refuse to grade a card whose stored criteria hash
changed while an attempt was in flight. That is cheap and it is not built.

### 3.2 Inflating coverage

**Mechanism: partial. The changed-lines restriction works. The empty-intersection
default does not.**

The card's brief says coverage is "already measured on changed lines only" and
asks whether that suffices. It is true, and it does not suffice.

True part: `ci.py:127-148` (`_changed_lines`) parses the unified diff and keeps
only lines with a `+` prefix, mapped to post-image line numbers. `:176-186` then
walks the Cobertura report and counts a line only if it is in that added set. So
the classic inflation (write a large test that exercises a lot of pre-existing
code and claim the ratio) does not move this number. Only the worker's own added
lines are in the denominator.

The hole is the last line of the function, `ci.py:187-188`:

```python
total = covered + missed
return 1.0 if total == 0 else covered / total
```

If the added lines intersect **no** class in the coverage report, `total` is
zero and the function returns perfect coverage. That happens whenever the diff
lands in files the repo's `coverage_cmd` does not instrument: docs, YAML,
templates, shell scripts, JS in a Python-coverage repo, or simply a source tree
outside the `--cov` target. The 1.0 is not a lie about the code that was
measured; it is a claim about code that was never measured at all, and the twin
gate cannot tell the two apart because it sees only a float.

This is defensible as a default (a docs-only change should not be blocked by a
coverage floor) and it is exactly the shape of thing an optimiser finds. A
worker that wants a cheap 1.0 puts its diff where the instrument is not looking.

Two further facts about coverage as it is actually configured on this node, both
read from `~/.skcapstone/config/` on 2026-08-16:

- `autopilot.yaml` (`enabled: true`) maps 7 repos; 4 declare a `coverage_cmd`
  (`skos`, `skchat`, `skworld-app`, `skcapstone`) and 3 do not (`skgateway`,
  `skharness`, `skcoord`). For those 3, `diff_coverage` returns `None` at
  `ci.py:157-158` and `twin_gate_passed` can never be true. They are
  structurally PR-only.
- `autopilot-live.yaml` declares no `coverage_cmd` at all, for any of its 3
  repos.

**What would close it:** return `None` (the honest "no coverage signal" value
the function already uses at `:158` and `:170`) instead of `1.0` when the
intersection is empty, and let the repo config decide whether an uninstrumented
diff may pass. That is a one-line change to a file that is not on the protected
floor, and it is not this card's to make.

### 3.3 The CI arm, which is the deepest problem

**Mechanism: none. Stating it plainly, because this is the one that matters.**

`ci_status == "green"` is one of the four conjuncts of the twin gate, and it is
the one that feels like ground truth: an external process ran and returned zero.
It is not ground truth. It is the worker's own tests, run by the worker's own
CI command, over the worker's own diff.

The mechanics, verified:

- For a `local:` CI repo (6 of the 7 in `autopilot.yaml`; only `skworld-app`
  uses `github-actions`), `ci.py:115-123` runs
  the configured command with `cwd=worktree`, that is, in the tree containing
  the harness's uncommitted edits. Whatever test files the build round created
  are the test files that run.
- `engineering.py:229-238` documents that this is deliberate: the staging step
  explicitly includes new and untracked files, because otherwise "scoped CI +
  diff-coverage would never run the new tests (coverage reads ~0 on the new
  source). The twin gate then can NEVER pass". The design *requires* the
  worker's tests to be the tests, or nothing ever passes.
- With `ci_scope: changed`, `_scoped_cmd` (`ci.py:61-73`) appends the changed
  test targets to the command, narrowing the run further toward exactly what the
  worker just wrote.

So a green CI arm certifies: *the assertions this diff contains pass against the
code this diff contains.* That is a real and useful property. It catches
syntax errors, import breakage, and the large class of changes that do not do
what their author thought. It is not evidence that the acceptance criteria were
met, because the worker chose the assertions.

The consequence for learning is not "the signal is noisy". It is that the signal
is **partly a measurement of the worker's test-writing skill**, and a policy
trained to maximise it is trained, in part, to select for models that write
tests which pass. That is a different objective from the one the epic wanted, it
points in a plausible-looking direction, and it would improve monotonically
while the underlying work got worse. This is the same argument card `09573989`
used to reject the exploration slice, and it is the argument `protected.py:42-49`
gives for putting the rubric on the hard floor. It is one argument, applied
three times.

Two things do partially constrain it, and neither is a solution:

1. **GitHub-checks repos.** `autopilot.yaml` gives `skworld-app` `ci:
   github-actions`, and `engineering.py:588` additionally requires
   `_github_checks_verdict(...) == "green"` before any auto-merge. Those
   workflows are defined in the repo and run on GitHub's runners, so they are
   outside the worktree. But they are **inside the diff's reach**: the live
   manifest at `~/.skcapstone/fleet/objects/_protected.json` protects 10 globs
   (the detector, `engineering.py`, `fleet/store.py`, `fleet/signing.py`,
   `itil.py`, three `operator_seat` files, and the two object files) and
   `.github/workflows/**` is not among them, nor is it in the hard-coded
   `_ALWAYS_PROTECTED` floor at `protected.py:29-60`. A diff that edits the
   workflow that judges it is not path-flagged.
2. **The independent promise token.** `is_complete(gr.notes)`
   (`engineering.py:56-58`) requires the grader to emit
   `<promise>COMPLETE</promise>`, and the instruction at `base.py:459-460` binds
   that token to genuine completeness. This is a second, differently-shaped
   assertion by the grader. It constrains a lazy grader. It does not constrain a
   worker whose tests are weak, because the grader is reading the same weak
   tests and being told CI is green.

**What would close it:** nothing available cheaply. The honest options are (a) an
adversarial reviewer pass with its own budget that tries to break the change
rather than confirm it, (b) mutation testing over the changed lines, so a test
suite that asserts nothing scores zero, or (c) a human. Option (c) is what is in
place, and section 3.5 is about how well it actually works.

### 3.4 A held-out check the agent cannot see

**Does not exist.** Verified by absence: no occurrence of a held-out, holdout,
or seeded-probe mechanism anywhere in `skharness/src/` at the pinned commit. The
nearest three things are each something else:

- `skharness/tests/data/joule-economy-golden-set-v1.json` is on the protected
  floor (`protected.py:58-59`) and is a calibration reference for the *grading
  rubric*, not a check on any individual card's work. It is also not yet ground
  truth: read at the pinned commit, it holds 42 cards and its own top-level
  metadata is `rubric_version: 1`, `created: 2026-08-14`, `graded_by: "opus
  (pre-grade, awaiting Chef correction)"`. Forty-two of forty-two agreeing on
  the derivation rule proves the `max()` rule is self-consistent, not that the
  grades are right. Card `09573989` records that it becomes ground truth when
  Chef reviews it.
- `repo.advisory_checks` (`types.py:69`, consumed at `engineering.py:652`) marks
  CI checks that are explicitly *not* core gates. That is the opposite of a
  held-out check.
- The seeded known-secret cards in `09573989` acceptance criterion 5
  (report caught-over-seeded, not override rate) are the right shape for a
  held-out probe, and they apply to the sensitivity classifier, not to work
  quality. That card is open, so they are unbuilt.

A held-out check is the single mechanism that would most improve the situation
in section 3.3, because it is the only one on this list whose difficulty does
not scale with the worker's capability in the same direction as the worker's
incentive. Recording it here as the best available future work is more useful
than pretending a substitute exists.

### 3.5 Human ratification outside the loop

**Mechanism: present in code, and measurably not operating.**

The code path is real and it is a single funnel. `digest.queue_decision`
(`digest.py:113-131`) writes one item into the unified GTD through the
`skos.gtd_ingest` port with `source="autopilot"`, `context="@decide"`,
`status="waiting"`, and a `meta.decision` block carrying
`{qid, prompt, options, answered: false, answer: null, action_ref}`. Everything
funnels here: the carve-out hold (`engineering.py:573`), the auto-merge hold
(`:607`), the PR-only review request (`:615`), the direct-mode notice
(`direct.py:104`), and the orchestrator's escalations (`orchestrator.py:844`).
`resolver.py:47-80` is the answering half; it flips `answered` to true and
stamps `answered_at`.

The red-team's concern was that ratification candidates would arrive on the same
flooded channel as merge holds and degrade into one-keypress ratification. The
channel was measured for this document. The finding is worse than the concern,
and it is counted, not inferred:

- `~/.skcapstone/coordination/gtd/waiting-for.json` holds 42 items. 38 carry
  `source: "autopilot"` and a `decision` block. **All 38 have
  `answered: false`.** The oldest was queued 2026-07-27, the newest 2026-08-06.
  At the time of writing that is a 10 to 20 day untouched backlog.
- `archive.json` holds 82 autopilot decision items, of which 20 are answered.
  **62 left the queue without a recorded answer.** Archival, not decision, is the
  modal terminal state for a queued autopilot decision.
- The current digest manifest (`autopilot-digest.json`, `digest_date`
  2026-08-17) carries 100 items, zero answered, `sent_at: null`.

So the degradation the red team predicted is not hypothetical and it is not
one-keypress. It is zero-keypress. A ratification step that is 100% unanswered
over the observed window certifies nothing at all, and a design that counts it
as a control is counting something that does not run.

The important corollary, and the reason this is not a crisis today: **nothing
auto-merges either.** `engineering.py:560` gates auto-merge on
`repo.name in self.config.automerge_repos`, and `automerge_repos` is the empty
list in all four config files on this node (`autopilot.yaml`,
`autopilot-live.yaml`, `autopilot-canary.yaml`, `autopilot-pi.yaml`). Every
passing build opens a PR and stops. The unanswered queue is a backlog of PRs
awaiting review, not a backlog of merges that happened without one. The failure
mode is stalled work, not unreviewed merges.

That is exactly the asymmetry a fail-closed design should produce, and it is
also the reason a future session must not read "human ratification is in the
design" as "human ratification is a live control". What holds the line today is
not the decision queue being drained. It is that nothing reaches an integration
branch without a person running a merge by hand, because `automerge_repos` is
empty. Turn it on and this queue, at an observed answer rate of zero over the
window measured above, becomes the only thing standing between a self-graded
diff and the integration branch.

One further observation from the same code, recorded because it bears on the
reward specifically: `_settle_economics` runs at `engineering.py:555` on
`result.passed`, **before** the carve-out check at `:568-581` and before any
merge decision. So the joule reward for a build is minted at twin-gate pass, not
at merge. A diff held for touching a guardrail is held from merging and is paid
anyway. (Today the minted amount is computed with a hardcoded `score=5` at
`:127`, so it does not vary with grade; that constant is open question 2 in the
reshape design.)

---

## 4. The refusal, with five checkable grounds

The epic asked for a learned routing policy trained on graded outcomes. It is
refused. Each ground below names the artifact a reader can check. Four are
policy decisions already taken; the fifth is a measurement fact and is the
strongest of them.

### Ground 1: no autotuner. A rubric change requires human review.

**Check:** card `09573989`, acceptance criterion 6, verbatim: "No autotuner: a
rubric change requires human review, and `rubric_version` increments only when
the golden set changes."

**Weight:** strong as a policy, and it is a policy rather than a mechanism.
Nothing in code enforces it; it is a criterion on an open card. The enforcement
that *does* exist in code is ground 3.

### Ground 2: no machine writes routing policy.

**Check:** `skcapstone-repos/skgateway/docs/specs/2026-08-08-model-ranking-routing-intelligence-arch.md`,
section 10 (explicit non-goals), lines 611-612: "No machine writes to
`registry.yaml` model picks: the system *suggests* (rank API), humans (or an
explicit operator action) commit policy."

Note the path. The reshape design cites this as
`2026-08-08-model-ranking-routing-intelligence-arch.md section 10` without a
repo, and it lives in **skgateway/docs/specs/**, not in a `docs/superpowers/specs/`
directory. That cost a search; it is recorded so the next reader does not repeat
it.

**Weight:** strong and directly on point. A learned routing policy is, by
definition, a machine writing routing policy.

### Ground 3: the rubric is already on the self-modification hard floor.

**Check:** `skharness/src/skharness/autocode/protected.py`. The work-grade block
is lines **42 to 59**, comment at 42-49 and entries at 50-59:
`grading.py`, `sensitivity.py`, `buckets.py`,
`autocode/data/joule-grade-vocabulary.json`, and
`tests/data/joule-economy-golden-set-*.json`, each with two globs so a
repo-relative or an absolute path both match.

The comment at `:44-48` is the argument this whole document elaborates:

> Once a card's grade selects the model that does the work, the grade IS routing
> policy: it decides the capability floor and, through sensitivity, which trust
> zone may see the card's data. An engine that can rewrite the rubric that routes
> it, then auto-merge behind a twin gate whose CI arm it satisfies with tests it
> wrote itself, is the same self-modification hazard CR-6.2 already ruled on for
> skos and skharness.

**Correction to the received citation.** Card `dab87c81` and the reshape design
both cite this as `protected.py:42-51`. At the pinned commit, 42-51 covers the
comment plus `grading.py` only. `sensitivity.py`, `buckets.py`, the vocabulary
and the golden set are at 52-59 and fall outside the cited range. The claim is
right and the range is short by 8 lines. Cite **42-59**.

**Weight and honest limits.** This is the only ground with teeth in running code,
and its teeth are narrower than "the rubric cannot be modified":

- It is a **merge-time** gate, not a write-time one. `changed_paths_are_protected`
  is called at `engineering.py:568-571`, and it sits **inside `if automerge:`**.
  It does not stop a worker editing `grading.py`; it stops that edit from
  auto-merging. With `automerge_repos` empty everywhere, this check does not
  currently execute at all, because there is no auto-merge to intercept.
- The manifest half is unsigned. `~/.skcapstone/fleet/objects/_protected.json`
  carries `"signature": null` and a note that Chef will PGP-sign it when the
  ceremony lands, and `load_manifest` only fails closed on verification if a
  `verify` callable is passed, which no caller does. So today the unforgeable
  part of the floor is the hard-coded `_ALWAYS_PROTECTED` tuple, not the
  manifest. That is the correct fallback and it is why the tuple exists
  (`protected.py:10-14`), but a reader should not credit the manifest with more
  than it currently provides.

### Ground 4: the exploration slice was proposed and rejected.

**Check:** card `09573989`, the paragraph headed "EXPLORATION SLICE: DO NOT
BUILD". The recorded reasoning: deliberately dispatching below the class floor
to buy counterfactual data "measures the gate's gameability rather than the
model's sufficiency, manufacturing exactly the downward pressure the design
exists to prevent". The reshape design section 1 adds the second half: the weak
model's misdiagnosis lands in `meta.autopilot.attempts` and is fed into the
control arm's first round through `build_prior_feedback` (called at
`engineering.py:357`), so the two arms are not independent and the
counterfactual is not measurable this way.

Epic `935d4b61` acceptance criterion 4 asks for exactly this: "An exploration
budget and counterfactual logging exist, so the policy can be shown to beat the
heuristic rather than merely to have replaced it". Ground 4 is the recorded
answer that it should not be built. The cross-reference is the point of writing
it down: a future session reading the epic's criteria will find a criterion that
has been deliberately declined, and should find this paragraph rather than an
apparent oversight.

**Weight:** strong on its own terms, and it is the ground most likely to be
re-proposed, because it looks like cheap data. It is not cheap; it is
correlated data that flows into the arm it is supposed to be compared against.

### Ground 5: the prediction destroys its own falsifier.

**Check:** card `09573989`, the paragraph headed "RISK RATCHETS UP ONLY", quoted
here verbatim because it is the deepest point in the entire design:

> The grade causes the routing, so outcomes are not independent of predictions:
> an accurate high-risk call produces more caution, which prevents the bad
> outcome, which then looks like an overestimate. The prediction destroys its own
> falsifier.

This is a measurement fact, not a policy choice, and it is the one ground that
cannot be voted away. A high-risk grade raises the model class through
`model_class = CLASS[max(size_rank, risk_rank)]` (Joule design section 3.3), and
at `risk: crit` or `confidence < 0.6` it routes to Chef regardless of anything
else (same section). If that grade was correct, the caution it bought prevents the
incident, and the record shows a high-risk card that went fine, which any
supervised learner reads as an over-estimate and corrects downward. The
correction removes the caution. The next such card is the incident.

The rule that follows is already recorded on the card and is the only stable
one: **risk ratchets up automatically and never down.** A low-risk card that
caused an incident is clean evidence and may raise a grade. Quiet is not
evidence and may never lower one. Downward revision is a human decision with a
written reason. Card `09573989` acceptance criterion 3 requires a test proving a
quiet outcome cannot lower a risk grade.

Supervised calibration on `risk` is therefore invalid by construction, not
merely noisy. The card is careful to keep the three axes apart, and so is this
document: `size` is ordinal with a real post-hoc observable (effort) and
genuinely calibrates; `sensitivity` is deterministic rules, so the measurable
thing is coverage rather than model accuracy. Only `risk` is destroyed by its
own feedback. That distinction matters, because "we cannot calibrate risk" is
true and "we cannot calibrate anything" is not.

**Weight:** strongest of the five, and the least likely to be understood on a
skim. If a future session only checks one ground, check this one.

### The refusal is a success under the epic's own terms

Epic `935d4b61` acceptance criterion 5, verbatim: "The reward-gaming question is
answered in writing before any policy consumes `GateResult.score`, and a
documented decision NOT to close the loop counts as success."

This document is that answer. Cards B3 and B4 close as documented decisions.
The effort was reinvested in the outcome substrate, which no prohibition touches,
and in `escalation_reason`, the one correction channel the approved design
deliberately leaves open (reshape design D2 and section 4.4, tracing to Joule
Economy D2: hard floor, soft ceiling, escalation reasons recorded). That seam is
a **reporting** seam. Nothing reads it to make a routing decision, and its exit
gate (Joule design section 9, phase P2) is that the escalation rate per class is
stable and **explainable by a human**, not merely predictive. A gate stated in
terms of human explicability is not a gate a learner can optimise past, which is
why it was written that way.

---

## 5. Confounds that would corrupt a calibration even if the loop were closed

Recorded as the fifth item in card `dab87c81`'s material, and kept separate from
the five grounds because these are reasons the *data* is bad rather than reasons
the *loop* is wrong. Quoted from card `09573989` and spot-verified where cheap:

1. **Effort is confounded by cache.** `BuildUsage.from_claude_json`
   (`joules.py:98-106`) sums `input_tokens + cache_read_input_tokens +
   cache_creation_input_tokens` into one `input_tokens` field. Observed effort
   therefore tracks repo context size and cache behaviour more than reasoning
   difficulty. **Verified in code.**
2. **The distribution is clipped exactly where under-grading would show.**
   `_MAX_ROUNDS = 4` (`engineering.py:226`). A card that needed six rounds
   records four and a failure. **Verified in code.**
3. **Cross-path effort comparisons compare different definitions.** The bridge
   path includes grading cost; the gated path does not. **Quoted from
   `09573989`, not re-verified here.**
4. **The energy record is node-local and does not know it is partial.**
   `energy_log` is node-local SQLite (`db_path: ./data/metrics.db`) with no
   replication, so a calibration computed from it sees one node's work.
   **Quoted from `09573989`, not re-verified here.**
5. **Train and test must be disjoint.** Cards used for tuning must be disjoint
   from cards used for scoring, or every rubric version "improves" by regression
   to the mean. **Quoted from `09573989`.**

---

## 6. Carried, not closed

Two items are open and must not be silently dropped. Neither is in this card's
scope to fix; both invalidate specific future analyses, so they are recorded
where the analysis would be written.

### 6.1 A re-grade overwrites its predecessor with no history

`skcoord/src/skcoord/coordination.py`, `set_grade`. The docstring at lines
454-457 states the contract: "Idempotent: re-grading the same card replaces
`meta.grade` in place rather than appending or duplicating." The implementation
at `:481-496` is a whole-block assignment, `meta["grade"] = {...}`, carrying
`graded_by`, `grader_model`, `rubric_version`, `confidence` and `graded_at` for
the new grade only.

There is no prior-grade list and no event. The consequence: **a calibration
report could launder itself.** A process that grades a card, observes an outcome,
re-grades the card, and then reports agreement between grade and outcome would
be reporting agreement with a value it wrote after seeing the outcome, and the
card would carry no trace that an earlier grade ever existed. Nothing today does
this. The point is that nothing today would notice if something did.

Note this is a *different* store from `board.score_task` (`engineering.py:421`),
which does append a per-round record. The 1-5 output score has history; the
size/risk/sensitivity work grade does not.

**Minimum fix:** append prior values to `meta.grade_history[]` on overwrite, or
emit a card event. Either makes re-grading visible. This is red-team finding 4
and it remains open.

### 6.2 The revert sensor is dead, and a dead sensor reads as good news

Card `1e5be0a7`, open. The merge record is written at `engineering.py:596-600`
as `{"pr", "branch", "ts", "auto"}` with **no `sha`**. `_revert_impl` at
`:688-690` reads `meta.autopilot.merge` and raises `no recorded merge for
{task_id}` unless `merge.get("sha")` is present. So the sanctioned revert path
always fails on automerged work and `meta.autopilot.reverted` (written at `:704`)
can never be set. **Verified in code at the pinned commit.**

Note the second, independent reason the field is empty today: the merge record at
`:596-600` is written only inside the auto-merge branch, and `automerge_repos` is
empty in every config on this node (section 3.5), so `meta.autopilot.merge` is
not being written at all. Both roads lead to a constant `False`, which is exactly
why the field cannot be read as an outcome.

Two consequences, and the second is why this belongs in this document:

1. The operator has no working undo for anything autopilot auto-merged. The
   capability is documented and does not function.
2. `meta.autopilot.reverted` is a constant `False` for a broken-tool reason. Any
   future analysis that reads "was this work later reverted" as an outcome signal
   is reading a dead sensor, and would conclude that over-grading never bites,
   and would ratchet class floors downward on the strength of a field that is
   structurally incapable of ever being true.

**Citation drift, recorded deliberately.** Card `1e5be0a7` cites
`engineering.py:564` and `:655`. At `2e8affdc` those are `:596-600` and `:689`.
The card was created at 09:37 UTC on 2026-08-16 and the pinned commit (the merge
of PR #39, `feat/grade-bucket-dispatch`) is dated 09:20 local the same day, so
the line numbers went stale within hours of being written, without anyone
touching the defect they describe. This is the practical argument for the pinning
note at the top: cite a sha with a line, or expect the line to move.

**Until `1e5be0a7` closes, no outcome analysis may treat `reverted` as a
signal.** Joule Economy anti-gaming rule 4 ("rework claws back",
2026-08-14 design section 6.4) depends on detecting rework, and this is the
detector.

---

## 7. What would have to be true to reopen this

Not "never". A future session with a written reason may reopen, and these are
the conditions that would make the reopening honest rather than optimistic. They
are listed so that the argument has to be made in these terms rather than from
first principles again.

1. **An outcome signal not authored by the worker.** Mutation testing over
   changed lines, an adversarial reviewer with its own budget, or a held-out
   check the worker cannot see. Without one of these, section 3.3 stands and any
   trained policy partly optimises test-writing.
2. **A ratification channel with a measured answer rate above zero.** Section
   3.5's numbers are the baseline to beat. Re-measure `waiting-for.json` before
   claiming a human is in the loop.
3. **Grade history.** Section 6.1 closed, so a calibration cannot be checked
   against grades written after the outcome.
4. **A live revert sensor.** Section 6.2 closed, so "did this get reverted" means
   something.
5. **A golden set that is ground truth.** Chef-reviewed, per `09573989`. Today
   its metadata says `graded_by: "opus (pre-grade, awaiting Chef correction)"`.
6. **An argument that survives ground 5.** Grounds 1 through 4 are decisions and
   could be revisited by whoever made them. Ground 5 is a property of feedback
   under intervention, and reopening requires either an identification strategy
   (natural experiments where model assignment varied for reasons unrelated to
   the grade, which `09573989` suggests mining) or an explicit restriction to
   `size`, which is the one axis with a real post-hoc observable.

Meeting all six is a real project. Meeting fewer and shipping anyway is the
failure this document exists to prevent.

---

## 8. What this document does not claim

- It does not claim the harness is being gamed. No evidence of that was found,
  and no policy consumes the score, so there is currently nothing to game *for*.
  The argument is about what would happen if a reward were attached, not about
  present behaviour.
- It does not claim the twin gate is weak. It catches a great deal. It is
  claimed to be an insufficient basis for **training a policy**, which is a much
  narrower claim.
- It does not claim the grader is credulous. Section 2.1 records the grader
  correctly refusing to trust reported CI and coverage figures against an empty
  diff.
- It does not claim the protected floor fails. It claims the floor is a
  merge-time gate whose manifest half is unsigned, which is exactly what the
  floor's own docstring says it is.

---

## 9. Verification log

**Verified by reading at skharness `2e8affdc`, on 2026-08-16:**
`protected.py:10-14`, `:29-60`, `:92-114`;
`engineering.py:56-58`, `:61-72`, `:226`, `:229-238`, `:300-312`, `:349`, `:357`,
`:367`, `:371`, `:413-421`, `:428`, `:436-443`, `:453-456`, `:552-581`, `:588`,
`:596-600`, `:607-612`, `:688-690`, `:704`;
`ci.py:61-73`, `:115-124`, `:127-148`, `:151-188`;
`adapters/base.py:453-469`;
`types.py:66-76`, `:111-129`;
`digest.py:113-131`; `resolver.py:47-80`;
`orchestrator.py:303-327`, `:805`, `:819`, `:824`, `:844-846`;
`joules.py:92-106`; `config.py:17-21`, `:83`;
`tests/data/joule-economy-golden-set-v1.json` (42 cards, `graded_by` metadata).
Outside skharness: `skcoord/src/skcoord/coordination.py` `set_grade` (411-498);
`skgateway/docs/specs/2026-08-08-model-ranking-routing-intelligence-arch.md:605-613`;
`skcapstone/docs/superpowers/specs/2026-08-14-joule-economy-design.md` sections 0,
3.3, 3.4, 3.5, 6.4, 9.

**Measured on this node on 2026-08-16:** the four `~/.skcapstone/config/autopilot*.yaml`
files (repo maps, `coverage_cmd` presence, `automerge_repos` empty in all four);
`~/.skcapstone/fleet/objects/_protected.json` (10 globs, unsigned);
`~/.skcapstone/coordination/gtd/waiting-for.json` (42 items, 38 autopilot, 38
unanswered, 2026-07-27 to 2026-08-06); `archive.json` (82 autopilot decision
items, 20 answered); `autopilot-digest.json` (100 items, 0 answered).
These are node-local observations. Another node's synced config may differ, and
the GTD store is Syncthing-synced with visible sync-conflict copies in the same
directory, so treat the counts as a lower bound on the backlog rather than an
exact fleet figure.

**Quoted from another session, not independently re-derived:** the falsifier
argument and the three-axis split (card `09573989`); the cross-path effort
definition mismatch and the `energy_log` replication gap (same card); the
reshape design's corrections to the cards (its section 2); the claim that
`record_run` has roughly 45 tests and has never written a row from the gated
path (reshape design section 2).

**Corrections this document makes to its own sources:** `protected.py:42-51`
should be `42-59` (grounds, section 3); `engineering.py:564`/`:655` in card
`1e5be0a7` should be `:596-600`/`:689` (section 6.2); the 2026-08-08 ranking spec
lives in `skgateway/docs/specs/`, not under `docs/superpowers/specs/`
(ground 2).

---

## 10. Related material

- `2026-08-16-prime-agent-self-improvement-design.md` (skharness): the approved
  reshape. This document is the detailed argument behind its section 1.
- Card `09573989`: the grade-calibration design, the recording-point finding, the
  risk-ratchet rule, the rejected exploration slice, the golden-set caveat.
- Card `1e5be0a7`: the dead revert sensor. Open.
- Card `dab87c81`: this deliverable.
- Epic `935d4b61`: the parent epic, whose acceptance criterion 5 this satisfies.
- `2026-08-14-joule-economy-design.md` (skcapstone): D2 soft ceiling, D6
  grader-of-record is never the executor, section 6.4 anti-gaming rules, section
  9 phasing.
- `2026-08-08-model-ranking-routing-intelligence-arch.md` (skgateway) section 10:
  the system suggests, humans commit.

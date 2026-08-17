# S29: who is actually writing pytest rows into the live cost ledger

Card `60245d49`. Written BEFORE the fix, because the card asks for the
hypothesis to be confirmed or refuted first and a fix aimed at the wrong cause
would have looked successful.

## The hypothesis, and the verdict

The card proposed: `phase2_swarm` dispatches under a `ThreadPoolExecutor`, and a
worker thread that finishes AFTER its test returns writes once
`monkeypatch` has already restored `SKAI_COST_DIR`, so the write lands on the
real path.

**REFUTED.** Four independent observations, any one of which is sufficient:

1. **The executor is fully joined.** `phase2_swarm` uses
   `with ThreadPoolExecutor(max_workers=workers) as pool:` and then calls
   `f.result()` on every future inside the block. `f.result()` blocks until the
   worker finishes, and the context manager's `__exit__` calls
   `shutdown(wait=True)` on top of that. No worker can outlive the call, let
   alone the test.

2. **No threaded row has ever leaked.** The only rows the skharness suite writes
   from a worker thread come from
   `test_swarm_runs_items_concurrently_when_max_concurrent_gt_1`
   (threads `ThreadPoolExecutor-0_0..3`, cards `t-0 t-1 t-2 t-3`, run id `rp`).
   The live ledger contains **zero** rows with `run_id="rp"` and zero rows for
   `t-0` or `t-3`. If late threads were the mechanism, these are the rows that
   would leak, and they are exactly the rows that never do.

3. **The skharness suite does not write to production at all.** `record_run`
   was wrapped for a whole session and every call logged with its resolved
   target directory, the calling thread, and the current test id. Across three
   full-suite runs (including one with `HOME` redirected to a sandbox, so any
   production write would have been captured in a private file):
   **89 ledger writes, 0 to production, 0 from a non-main thread.**
   The sandbox ledger finished with 0 rows.

4. **The leaked rows are a byte-for-byte match for a different repo's suite.**

## What is actually happening

`~/clawd/skos` still carries the **pre-extraction copy** of the orchestrator
tests, `skos/tests/test_autopilot_orchestrator.py`, with the same `t-1 t-2 t-B
keep` cards and the same `r1 rc rr` run ids. Since the Wave 2 extraction,
`skos/autopilot/orchestrator.py` is a shim:

    from skharness.autocode.orchestrator import *

so those tests drive **this repo's engine** and therefore this repo's
`autopilot_cost.record_run`, which is why the leaked rows carry the full current
key set (`mutation_*`, `escalation_*`, `grader_model`) and look like they came
from a current checkout. They did: they came from current library code, driven
by another repo's tests.

`skos/tests/conftest.py` has **no** `_isolate_cost_dir` and **no**
`_isolate_joule_wallet`. Those fixtures were added to `skharness/tests/conftest.py`
and never to the copy that inherited the same engine.

Proof, with `HOME` redirected so nothing touched the live file:

    $ HOME=$SANDBOX python3 -m pytest tests/test_autopilot_orchestrator.py -q   # in ~/clawd/skos
    31 passed
    $ cat $SANDBOX/.skcapstone/autopilot-cost/ledger.jsonl
    t-1  r1  finalized   5
    t-2  r1  escalated   4
    t-1  r1  finalized   5
    t-1  rc  budget-hit  None
    t-2  rc  budget-hit  None
    t-B  rr  finalized   5
    keep r1  finalized   5

Seven rows, in that order, with those scores. Every leak burst in the live
ledger is that exact sequence, ten times over.

## Why the card's evidence pointed the wrong way

The card's measurement was a before/after row count on the shared file. That
measurement **cannot attribute a write**. Several agents run skharness and skos
suites concurrently on this box; the file grew during a full-suite run, and the
growth was read as the run's own output.

The reason a per-file run looked clean and the aggregate looked dirty is not
aggregation. It is **duration**. A single test file finishes in 5 to 9 seconds;
the full suite takes 60 to 180 seconds depending on load. The long run is
roughly twenty times more likely to overlap a concurrent skos run, so the leak
correlated with "full suite" while being caused by neither the suite nor its
size. Six suspect files were each cleared honestly, and all six were innocent.

This is the fleet's standing failure mode in a new shape: the observation was
real, the inference from it was not, because nothing in the observation
identified the writer.

## What the live ledger actually contains

    total rows                        253
    card_id "task-abc", repo skrender 183   (agent-run bridge fixtures)
    card_id t-1/t-2/t-B/keep, repo skos  70 (orchestrator fixtures, via skos)
    genuine rows                        0

Every row in the operator's cost ledger is pytest output. It has never recorded
a real run. Total recorded tokens 0, cost 0.00 USD, joules 0, which is what a
ledger made entirely of fixtures looks like and is the reason nothing downstream
ever noticed.

## What follows for the fix

A fixture in `skharness/tests/conftest.py` cannot protect a consumer repo's
suite, and adding one to skos would only move the same omission one repo along:
the next consumer of the shim inherits the writer and not the isolation. So the
guard belongs in the **writer**, next to the `ProductionWalletInTestError` guard
`joules.py` already carries for exactly this failure, and the session-level
fingerprint belongs on top of it as the net that fails the run if a row ever
appears anyway.

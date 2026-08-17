# Joule wallet fixture contamination, and the correction for it

Date: 2026-08-16
Status: correction published, wallet unmodified
Tool: `skharness.autocode.wallet_correction`
Sidecar: `~/.skcapstone/agents/lumina/wallet/transactions.correction.json`

## Read this first

**No joule analysis computed before 2026-08-16 can be trusted without applying
this correction.** That includes balance screenshots, P&L summaries, agent
leaderboards, efficiency ratios, treasury projections, and anything derived from
`balance_after` at any point on or after 2026-07-27. It is not a rounding issue:
about 52 percent of the ledger's rows and roughly two thirds of its headline
balance are pytest output.

## What happened

`joules.settle()` had no test-mode guard. `settle()` is the twin-gate PASS path
and the finalize tests exercise that path, so every run of the suite minted real,
well formed joules into the operator's live wallet at
`~/.skcapstone/agents/lumina/wallet/transactions.jsonl`.

The rows are individually valid. They carry a correct `proof_hash`, a plausible
amount, and a coherent `balance_after`. The only thing distinguishing them from
genuine earnings is the description: they name the pytest fixture task ref `t1`.
A wallet that is half test output looks exactly like a wallet that is real, which
is why this ran for roughly three weeks before anyone noticed.

The leak is FIXED and merged (card `b24c71b5`): `tests/conftest.py` now carries an
autouse `_isolate_joule_wallet` fixture that redirects `SKHARNESS_WALLET_HOME` to
a throwaway root for every test, and `assert_not_production_wallet_in_test()`
raises `ProductionWalletInTestError` if a test run ever resolves a production
wallet again. The last fixture row is `2026-08-17T01:48:06.760525+00:00`; this was
verified directly against the ledger rather than taken on trust, and zero rows
after it match the fixture signature.

## The measurement

Measured against the live ledger at 2,798 rows:

| quantity | value |
| --- | --- |
| ledger rows | 2,798 |
| fabricated mints | 1,452 (51.9 percent of rows) |
| fabricated joules | 108,900 |
| recorded balance | 164,293 |
| **corrected balance** | **55,393** |
| contaminated range | 2026-07-27T04:42:20 to 2026-08-17T01:48:06 |
| spends enabled by fabricated joules | 0 |

Every fabricated row is identical in shape: a `mint` of exactly 75 J from
counterparty `economy`, described `autocode task_complete t1`. 75 J is the fixture
card's constant (base 25 J x medium priority x the fixture quality score).

Two of those numbers are stable and two are not. **The fabricated count (1,452)
and the fabricated joules (108,900) are frozen**, because the leak is fixed and
no further fixture rows can be written. The recorded and corrected balances both
keep moving as genuine work lands, and the fabricated share of rows falls as the
ledger grows. Rerun the tool rather than quoting the balances above: the sidecar
records the row count it was computed against, so it always says what it covers.

## What was decided, and why

The operator chose to **mark and publish a correction factor**. He explicitly
rejected two alternatives:

- **Partitioning the file** (moving fabricated rows to a separate ledger). This
  rewrites an append-only file, breaks `balance_after` continuity at every
  removal point, and invalidates the 19-wallet reconciliation that was completed
  on 2026-08-14.
- **Deleting the rows.** Same objections, plus it destroys the evidence of the
  incident.

The wallet is append-only production state, synced by Syncthing to several
machines. An in-place edit would be a second unlogged corruption of the exact
ledger being corrected, and worse than the original bug because it would be
deliberate. **The tool therefore never opens the wallet for writing.**

## Why the correction is a per-row series, not a scalar

`balance_after` is a running total. The first fabricated mint at row 775 poisons
the balance on **every subsequent row**, genuine rows included, not merely the
fabricated ones. A single scalar "subtract 108,900" is correct for the CURRENT
balance and wrong for every historical point, which makes it actively misleading
for exactly the kind of trend analysis people run against a wallet.

The correction is therefore a series, one entry per row:

```
corrected_balance[i] = recorded_balance[i] - cumulative_fabricated[i]
```

Note the shape. It corrects the RECORDED balance rather than recomputing a
balance from the amounts. That is deliberate: the recorded balance is
authoritative and carries its own history, including a deliberate reconciling
entry. Recomputing from amounts would silently undo unrelated history as a side
effect of fixing this one defect. This correction fixes exactly one thing and
reports everything else it noticed.

## The classifier, and its limits

A row is classified fabricated only when ALL of the following hold:

- `description` is exactly `autocode task_complete t1`
- `kind` is `mint`, `amount` is exactly `75`, `counterparty` is `economy`
- `timestamp` is at or before the cutoff `2026-08-17T01:48:06.760525+00:00`

The conjunction matters. An over-matching classifier does not merely miss the
correction, it CORRUPTS it: every false positive subtracts real joules from every
later balance in the series.

**Could a genuine row match?** The risk is not zero, but it is bounded and small.
Genuine autocode mints use a different template that carries the card id,
`"[<id>] Task completed: <title>"`, so a real card cannot produce this exact
string through the normal code path. It would take a card whose description was
literally `autocode task_complete t1`, minting exactly 75 J, before the cutoff.
Measured: all 1,452 matches carry the identical string and amount, and ZERO rows
outside that set contain the token `t1` anywhere in their description. The cutoff
caps future exposure, since the leak is fixed and no further fixture rows can be
written.

## What is NOT recoverable

Stated plainly, because estimating any of these would be worse than admitting them.

1. **Which genuine builds ran during the contaminated window is recoverable; what
   the wallet would have driven had it held the true balance is not.** The joule
   economy is a feedback system. Autoscale, bucket selection, and escalation read
   the balance. Every such decision from 2026-07-27 onward was made against an
   inflated number. The correction reconstructs the balance; it cannot replay the
   decisions that balance influenced.

2. **Three continuity anomalies predate this correction and are NOT repaired by
   it.** The recorded balance is not a pure running total. At rows 2174/2175 and
   2754/2755, two concurrent `settle()` calls read the same balance and both wrote
   it back, a classic lost update: 25 J and 50 J of GENUINE earnings were never
   credited. Row 2384 is different, a deliberate `LEDGER RECONCILIATION` entry that
   adjusts the journal against an authoritative snapshot and correctly leaves the
   balance flat. The tool reports all three as `continuity_anomalies` and repairs
   none of them. **The 75 J of genuinely lost credit is not recoverable from this
   ledger alone** and would need the settlement journal to reconstruct.

3. **Whether any downstream system cached the inflated balance.** The correction
   covers this ledger. Anything that read it and stored a copy (dashboards,
   digests, memory entries, the skgraph knowledge graph) still holds the
   uncorrected number and is out of scope here.

One thing that IS recoverable and worth stating positively: **no spend was
enabled by fabricated joules.** The corrected balance never goes negative at any
point in the ledger, so every one of the 41 spends (3,832 J total) would still
have been affordable at the true balance. The contamination inflated the wallet;
it did not cause spending that could not have been paid for.

## How to apply the correction

```bash
# summary only, reads the wallet, writes nothing
PYTHONPATH=$PWD/src python -m skharness.autocode.wallet_correction

# publish the per-row series beside the wallet
PYTHONPATH=$PWD/src python -m skharness.autocode.wallet_correction --write-sidecar

# machine readable
PYTHONPATH=$PWD/src python -m skharness.autocode.wallet_correction --json
```

From Python:

```python
from skharness.autocode import wallet_correction as wc

correction = wc.correct_ledger(wc.load_ledger(path))
correction.corrected_balance          # 55393
correction.rows[i].corrected_balance  # the balance row i should have carried
```

The sidecar is JSON: a `summary` block, a `continuity_anomalies` list, and a
`rows` array carrying `index`, `timestamp`, `recorded_balance`,
`corrected_balance`, `cumulative_fabricated` and a `fabricated` flag per row.

## Testing note

The tests for this correction build their own ledgers in `tmp_path` and never
read or write the live wallet. A test suite that reached for the real ledger to
verify a correction to the real ledger would be a second instance of the very
mistake being corrected.

# ADR: Card lifecycle and RunRecord storage boundary

Status: accepted

Date: 2026-08-21

Decision card: `0d19118f`

Applies to: A1 `8967bf22`, A7 `5b88d88c`, A7.1 `9e81980d`, and A7.2 `cfc217c6`

## Context

The 2026-08-16 inventory found 4,827 cards in the legacy task/overlay paths and only
11 records in the then flag-gated unified CardStore. The legacy event shape had no
session, host, or actor field; CardStore already stamped writer and node. Those numbers
are the measured cutover input, not a claim about today's inventory. They show why
lifecycle migration must be explicit and must not be hidden inside RunRecord work.

SKHarness also already has an atomic autocode run journal at
`~/.skcapstone/coordination/autopilot/runs/<run_id>.json`, keyed by card under `items`.
Separately, `autopilot_cost` appends JSONL rows for cost analytics. Treating all three
as independent execution records would make attribution depend on which projection a
reader happened to choose.

## Decision

There are two canonical records with different ownership; no new store is introduced.

1. **Card lifecycle:** unified `skcoord.CardStore` remains canonical for card birth
   facts and lifecycle events. The `skcoord` coordination API owns CardStore writes
   now; any remaining legacy-only caller is moved by the dedicated `skcoord` cutover
   before legacy-write retirement. Legacy task JSON and overlay JSONL are
   migration/compatibility inputs whose retained history is preserved, but they gain
   no RunRecord or request-trajectory fields. Their remaining parity, migration,
   dual-write retirement, and rollback are separate from A1 and A7 work.
2. **Execution truth:** the existing autocode run journal is canonical for validated,
   immutable `RunRecord` entries. One logical record is identified by
   `(run_id, card_id, round)` and is appended under
   `items.<card_id>.run_records[]`. It is written from execution-time observations and
   is never reconstructed from CardStore, a board projection, or cost analytics.
3. **Trajectories:** prompts, completions, tool I/O, and raw request/response bodies do
   not enter CardStore, legacy coordination storage, or the run journal. They remain in
   the existing content-addressed evidence/trajectory artifacts. RunRecord contains
   only bounded typed provenance plus artifact hashes or pointers. Its ordered gateway
   request summaries are provenance, not a copy of the raw trajectory.
4. **Cost analytics:** `autopilot_cost` JSONL is a derived projection. After the A7
   writer lands, a row is emitted only from a successfully validated and durably written
   RunRecord and carries that record's content hash. Projection failure cannot change
   execution truth; it is observable and retryable by hash. Direct independent writes
   are deprecated and may instead be removed once consumers read RunRecord-derived
   analytics.

## Historical data and migration

Existing CardStore and legacy lifecycle facts are reconciled by the separate `skcoord`
migration. RunRecord writers must not use that reconciliation to manufacture execution
attribution.

Pre-RunRecord cost rows remain immutable historical analytics. They may retain only
facts present in the exact digest-bound source row. Agent, session, node, adapter,
requested or served model, grader model, effort, and gateway joins remain explicitly
unattributable. A single legacy row timestamp is a record timestamp; it is not copied
into unknown execution start/finish fields to imply a zero-duration run. Current card
state, filenames, timing proximity, host conventions, and agreement between projections
are not attribution evidence. No migration rewrites old rows or backfills those fields.

Migration to the A7 writer is forward-only:

1. validate the A1 schema and its historical-absence rules;
2. append the RunRecord atomically at the execution boundary;
3. derive the optional cost row from the durable record and bind it by content hash;
4. switch analytics readers only after hash/count reconciliation; and
5. retire independent cost writes while retaining old JSONL bytes as historical input.

## Failure and conflict semantics

- A RunRecord validation or journal-write failure never becomes a guessed record. It
  leaves the worker result unchanged, emits an observable controller error, and makes
  that attempt ineligible for claims requiring attributable execution evidence.
- A cost-projection failure does not invalidate the RunRecord. It is retried
  idempotently from the record hash and is reported as incomplete analytics.
- A missing trajectory or gateway join is recorded as absent. Disagreeing firsthand
  observations remain conflict evidence; neither state is resolved from a projection.
- Duplicate delivery of the same `(run_id, card_id, round)` and content hash is
  idempotent. The same key with different content is a conflict and must not overwrite
  either fact silently.
- CardStore failure does not cause lifecycle state to be reconstructed from the run
  journal, and run-journal failure does not cause execution truth to be reconstructed
  from CardStore.

## Reversibility

The decision is reversible without rewriting history. The A7 writer and derived-cost
emitter must be independently feature-gated. Disabling either stops new writes but
leaves CardStore, journals, and legacy JSONL unchanged. A future execution store may be
adopted only by copying validated records, comparing content hashes and counts, then
atomically switching the reader/writer boundary; the old journal remains a read-only
archive until retention policy permits removal. Card lifecycle migration remains
independent throughout.

## Consequences for A1 and A7

- **A1:** owns the pure immutable schema and validator, including explicit absence,
  conflict, source binding, and nullable unknown historical execution times. It has no
  writer and cannot treat projections as evidence.
- **A7/A7.1:** own the first execution-time journal append, uniqueness/idempotency,
  atomic crash behavior, and the derived-cost handoff. A writer failure is visible but
  never repaired by inference.
- **A7.2:** reads attribution from the validated journal record and verifies its content
  hash. It may join CardStore for card metadata and trajectory artifacts for evidence,
  but neither is allowed to replace missing RunRecord facts.

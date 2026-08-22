# Pi S/M/L v0.3.38 qualification preflight

## Reviewed remediation profile

The checked-in qualifier also accepts `--card-id c278b5c0 --card-id 400bf174`
for the two follow-up remediations from the 2026-08-22 run. This is a bounded
profile, not arbitrary CardStore execution: each ID has a reviewed phase
topology, path ownership, budget, and canonical `core.json` content hash in
`scripts/qualify-pi-swarm.py`. Unknown IDs are refused before Docker, worktree,
or worker admission. The profile remains preflight-only unless `--execute` is
explicitly supplied, never mutates the coordination board, and retains the
same host, image, source, clean-worktree, and cleanup gates as S/M/L.

Run-scoped Docker inventory is ownership-aware: valid resources from a different
run (including an existing `sbxproxy`/`sbxnet`) do not block admission, while
exact current-run IDs or run IDs still block cleanup sealing. Duplicate IDs with
conflicting ownership labels, malformed labels, and inspection failures remain
fail-closed.

Example (safe plan generation):

```bash
python scripts/qualify-pi-swarm.py \
  --card-id c278b5c0 --card-id 400bf174 \
  --image "$SKHARNESS_PI_SWARM_IMAGE" \
  --controller-commit "$SKHARNESS_PI_SWARM_CONTROLLER_COMMIT"
```

Date: 2026-08-21
Status: design and focused tests passed; final immutable-controller preflight and live
execution remain pending; no live worker was started by this evidence

## Frozen execution inputs

- Host: `cbrd21-laptop12thgenintelcore` (`.41`) only.
- Worker source: `2e8e4d89aac1967fb297c0558b311998a9bc1e9a` (`v0.3.38`).
- Controller source: supplied at runtime with `--controller-commit` after review. It is
  a separate, exact descendant commit containing the tracked qualifier, critical
  imported modules, and release record; it is not the frozen worker source above.
- Worker image:
  `ghcr.io/smilintux/skharness-pi-python-test@sha256:8e991c893e7553522369a35d10b78ae2e831eb62b9f127ba53a7dabd045e2c7d`.
- SKGateway: `http://100.86.156.5:18780/v1`.
- Requested model: `ornith-1.5-9b`.
- Driver schema: `skharness.pi-swarm.sml.v2`.
- Release record: `docs/evidence/pi-python-test-v0.3.38.release.json`, binding the
  exact source/tag/image plus successful publish, Cosign identity/issuer, and
  vulnerability-gate jobs.

| Card | Size | Canonical content hash | Frozen topology | Admission decision |
|---|---:|---|---|---|
| `0f34e285` | S | `sha256:e6a2971747260c4089f84b4b0cd5d9540321a7004f1ff8b717f175c05dc445d6` | builder | Safe for a provisional single-writer trial. |
| `5b88d88c` | M | `sha256:cecffcaa49b7b22e84d7425049fa3e00d80bee9648ebe1f1f2d94320a7287b26` | scout -> builder -> tester | Conditional: missing or superseded prerequisites must block before the builder. |
| `41077231` | L | `sha256:728d2c3af5cd438b1ee6780591f085ce6e0ef03c9ddac89b135d629ed80bb2df` | two parallel read-only scouts | No builder. The single mount cannot prove three repositories plus fresh transport bytes. |

These hashes cover normalized ID, title, description, acceptance criteria, and
dependencies from `/home/cbrd21/.skcapstone/cards/<id>/core.json`. A trial preflight
initially exposed that `coord kanban --json` omits acceptance criteria; the checked-in
driver now rejects the lossy view as an immutable input and validates canonical path,
ID, and digest instead. The original three hashes matched the canonical snapshots.

## Frozen budgets

| Worker | Contract wall | Pi wall | Controller reserve | Token limit | Tool calls | Assess / inspect / build / test seconds |
|---|---:|---:|---:|---:|---:|---|
| S builder | 540 | 360 | 180 | 65,536 | 24 | 20 / 50 / 190 / 100 |
| M scout | 180 | 180 | 0 | 32,768 | 16 | 20 / 154 / 5 / 1 |
| M builder | 480 | 300 | 180 | 98,304 | 40 | 20 / 55 / 185 / 40 |
| M tester | 240 | 240 | 0 | 32,768 | 20 | 20 / 40 / 1 / 179 |
| Each L scout | 150 | 150 | 0 | 32,768 | 12 | 20 / 124 / 5 / 1 |

The prompt text, readable/writable/protected paths, tool allowlists, controller tests,
and concurrency are serialized into the preflight manifest by
`scripts/qualify-pi-swarm.py`. The same per-worker Pi phase budget is passed to Pi and
to the enforcing supervisor. Builder contracts reserve 180 seconds inside their hard
lease deadline: 90 seconds pytest + 10 cleanup + 30 Ruff + 10 cleanup + six bounded
5-second Git commands + 10 seconds controller overhead. Pi receives only the remaining
360 seconds (S) or 300 seconds (M); heartbeats cannot extend either contract wall.

## Evidence contract

Running without `--execute` only validates inputs and writes the requested preflight
manifest. After separate operator authorization, live execution must use a new evidence
root. It writes:

- root `manifest.json` and `qualification-summary.json` with exact execution inputs and
  per-card disposition;
- per-card `swarm-state.json` and `a2a.jsonl` for durable admission, usage, cancellation,
  receipt/authorization, and parent-child records;
- per-contract Arena and attempt evidence, including terminal classification, bounded
  raw-output digests, typed measured/unknown model and usage observations, conservative
  reservation accounting, and structured scout findings;
- per-card `qualification.json` with identity, plan/hash, contracts, executed IDs,
  results, receipts, completion denial, failure reasons, worktree commit/status, and a
  literal `board_mutated: false` assertion; or a bounded `<card>-failure.json`;
- pre/post-card inventories of every `io.skharness.managed=true` Docker container and
  network, and `qualification-bundle-digest.json`, which hashes the retained bundle.

The driver requires a clean source checkout and creates a new detached linked worktree
at the frozen worker source for every card. The trusted controller, not Pi, owns any
provisional commit after exact path, predefined pytest, and Ruff checks in the same
digest with network disabled. Those validator containers use deterministic names,
managed lifecycle labels, explicit resource/wall limits, bounded exact-name cleanup,
and the same zero-inventory next-card gate as normal workers. Cancellation sets a
controller token, cancels Pi, and synchronously drains post-run validation and bounded
hookless/signing-disabled Git before a stop is acknowledged. Runtime quiescence is
required before candidate evidence is sealed. A timeout, nonzero cleanup, Docker
inventory uncertainty, controller drift, any labeled leftover, or unproven quiescence
prevents the next card from starting. The controller never pushes, merges, updates the
board, or provides completion attestation. Runtime cleanup owns worker/proxy/network
termination; detached worktrees remain for evidence review and must not be discarded
merely to make a failed trial appear clean.

Missing telemetry is not zero. Model, token, tool, duration, and cost observations stay
`null` with a bounded reason unless complete trusted artifacts prove them. Each unknown
budget dimension is charged at its contract reservation with
`accounting_basis: reservation`; that fail-closed charge is explicitly excluded from
performance comparison. When end-to-end controller elapsed time exceeds an observed Pi
duration, budget accounting charges the former as `controller_elapsed`; the Pi duration
remains unchanged and is the only value eligible for a model-latency comparison.

This driver is evidence for swarm execution, cleanup, and phase gating only. It has no
matched single-agent topology, independent quality decision, complete TTFE observation,
or normalized policy-denial measurement. It therefore cannot complete comparison card
`322f2d80`; raw Pi output, worker tests, and exit status remain non-authoritative.

An earlier lossy-view preflight returned the three hashes, but is superseded by the
canonical CardStore and exact-controller gates above. The final preflight must be run
from a clean committed checkout with its reviewed current `HEAD` passed explicitly;
this document does not claim that not-yet-created controller commit. Live S/M/L outcomes
remain intentionally absent until the final preflight passes and execution is separately
authorized.

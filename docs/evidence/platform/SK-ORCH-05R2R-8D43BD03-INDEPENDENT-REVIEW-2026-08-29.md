# SK-ORCH-05R2R independent review

Card: `8d43bd03`

Verdict: `BLOCKED`

Blocked on: `card`, referent `ac:2`

## Immutable identity

The independently fetched SKHarness objects reproduce the card identity:

- Candidate: `9ab43e783fa021a55aae3dbfd72d9ffd18c0723c`
- Parent: `90c7399fce642253e50877fba8e95ec4d7567b1d`
- Candidate tree: `6fc2868c406106f6297a72a974d15327cd31c23d`
- Canonical Git diff SHA-256: `bb9f034589f3707ac46a091e0f5f98a4c2ac165cc3dc090b8a0b532b3ffcdacd`
- Declared repair evidence commit: `3282858095f293fed62d24f392b59eed58548fcd`
- Declared repair evidence SHA-256: `e9a089a7c70a91d286f74b2118cef0194b3c400eb085100c332ab5da60c6bc90`

The candidate, parent, and tree are available and match. The declared repair evidence commit remains absent from the fetched SKHarness object store, so its declared evidence hash could not be independently recomputed.

## Independent tests

All execution used temporary state and injected fake runners. No process launcher, tmux command, existing worker, service, gateway, or live control was touched.

`PYTHONPATH=src python -m pytest -q tests/test_pi_spawn_control.py tests/test_pi_cockpit_spawn_control.py` passed with `32 passed`.

An independent probe reproduced both historical bypass classes:

| Probe | Parent | Candidate |
| --- | --- | --- |
| Inconsistent pause history with an open state | Fake runner reached | `StateUnavailableError`, fake runner not reached |
| Resume history with mismatched fence and open state | Fake runner reached | `StateUnavailableError`, fake runner not reached |

The same probe then placed an existing worker reservation one hour in the future and requested a second guarded process launch on the candidate. The candidate accepted the malformed history and reached the injected fake runner. The existing worker record and token remained untouched.

Probe script SHA-256: `3f2298278e250ca9ead6e9bcf5b887498fe68739c62f34bb7b2039a361066c80`

Probe result SHA-256: `b37ccb796234fd2d8f92c674a9b085c1662a16e8295bec1792e27a2906cbc14d`

## Exact blocker

Acceptance criterion 2 requires every time transition to be independently verified. The candidate validates top-level `created_at`, `updated_at`, and `last_operation.applied_at` against its clock, but only parses each worker `reserved_at`. It does not reject a worker reservation later than `updated_at` or later than the current clock. Consequently malformed future worker history permits a new guarded launch to reach the fake process runner.

This is an exact card blocker at `ac:2`. It also shows that the spawn boundary cannot be qualified as fail closed for all malformed histories under `ac:3`. No deployment or live-control authority is granted.

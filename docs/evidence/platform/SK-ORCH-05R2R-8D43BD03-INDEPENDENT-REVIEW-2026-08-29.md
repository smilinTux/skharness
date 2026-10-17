# SK-ORCH-05R2R independent review

Card: `8d43bd03`

Verdict: `BLOCKED`

Blocked on: `card`, referent `ac:2`

Reviewer: `pi-codex-chiap02-8d43bd03`

## Exact blocker

Acceptance criterion 2 requires every legal and illegal time transition to be independently verified. Frozen candidate `9ab43e783fa021a55aae3dbfd72d9ffd18c0723c` parses each worker `reserved_at` value but does not reject a value later than the injected current time. With a preexisting worker whose `reserved_at` is one hour in the future, both process and tmux guarded launches reached isolated fake runners. The preexisting worker token and record remained untouched. This is a candidate defect, not a missing dependency or human decision.

## Ownership and dependency

The CardStore fold showed exact owner `pi-codex-chiap02-8d43bd03` and status `doing` before work began. Dependency `eff95fb8` exists and is `done`. No other card was claimed or substituted.

## Immutable identity

Independently checked from local Git objects:

- candidate: `9ab43e783fa021a55aae3dbfd72d9ffd18c0723c`
- parent: `90c7399fce642253e50877fba8e95ec4d7567b1d`
- candidate tree: `6fc2868c406106f6297a72a974d15327cd31c23d`
- canonical binary diff SHA-256: `bb9f034589f3707ac46a091e0f5f98a4c2ac165cc3dc090b8a0b532b3ffcdacd`

The declared repair evidence commit `3282858095f293fed62d24f392b59eed58548fcd` was not available in the local object store, and origin did not expose the declared archive ref. Therefore the declared evidence SHA-256 `e9a089a7c70a91d286f74b2118cef0194b3c400eb085100c332ab5da60c6bc90` could not be independently recomputed. The exact executable candidate and parent were available and verified.

## Independent results

All probes used temporary state and an injected callable whose command was exactly `isolated-fake-launch --no-live-action`. No real subprocess, tmux command, worker, service, gateway, provider, or live controller was invoked or mutated.

| Probe | Parent | Candidate |
| --- | --- | --- |
| inconsistent pause replay, process boundary | fake runner reached | fail closed with `StateUnavailableError` |
| mismatched resume status, tmux boundary | fake runner reached | fail closed with `StateUnavailableError` |
| future worker `reserved_at`, process boundary | not needed for blocker | fake runner reached |
| future worker `reserved_at`, tmux boundary | not needed for blocker | fake runner reached |

For both future timestamp probes, the existing worker token and future timestamp remained unchanged.

Focused suites:

- parent: `11 passed`
- candidate: `32 passed`

The candidate suite exercises bootstrap, pause, drain, status, renew, expiry, resume, strict replay, owner, scope, fence, lease bounds, malformed and partial state, process and tmux guards, rollback cleanup, pause reservation race, concurrent pause, and concurrent resume versus pause. Passing focused tests do not cure the independently reproduced omitted worker-time invariant.

## Acceptance criteria

1. PASS. Both exact named bypass classes are red on the parent and fail closed on the candidate.
2. BLOCKED. A future worker reservation is accepted and permits both fake launch boundaries to be reached.
3. BLOCKED. Existing workers remain untouched, but process and tmux boundaries are not fail closed for that malformed history.
4. This artifact records exact `BLOCKED`. Coordination closure must move the card to Review, link this evidence and PR separately from lifecycle state, notify jarvis and lumina, and release the exact owner.

## Artifacts

- probe: `docs/evidence/platform/8d43bd03-independent-probe.py`
- probe SHA-256: `09e63dc49f04a40907bd73494658b7a3802f64e49234f17f742ad022a1e4b86c`
- result: `docs/evidence/platform/8d43bd03-independent-probe-result.json`
- result SHA-256: `97d2f4c3f0e070e50e963bcad944672a41b8e68d8fb1412cc16ee6cf628a9d9e`

## Authority

No deployment, merge, live control, live execution, automerge, WAKE-02 enablement, restart, gateway mutation, configuration mutation, credential use or disclosure, repository visibility change, or human signoff is granted.

# SK-ORCH-05R2R escalated independent review

Card: `8d43bd03`

Verdict: `BLOCKED`

Blocked on: `card`, referent `ac:2`

## Exact frozen candidate identity

The review reproduced the immutable identity requested by the card:

* Candidate: `9ab43e783fa021a55aae3dbfd72d9ffd18c0723c`
* Parent: `90c7399fce642253e50877fba8e95ec4d7567b1d`
* Candidate tree: `6fc2868c406106f6297a72a974d15327cd31c23d`
* Canonical `git diff HEAD^ HEAD` SHA-256: `bb9f034589f3707ac46a091e0f5f98a4c2ac165cc3dc090b8a0b532b3ffcdacd`
* Declared repair evidence commit: `3282858095f293fed62d24f392b59eed58548fcd`
* Declared repair evidence SHA-256: `e9a089a7c70a91d286f74b2118cef0194b3c400eb085100c332ab5da60c6bc90`

The candidate, parent, and tree exist locally and match. The canonical diff is 19,135 bytes and hashes exactly as declared. The declared repair evidence commit is absent from the local object store and its declared evidence hash could not be independently recomputed.

Dependency `eff95fb8` is complete in the folded CardStore and names the exact frozen candidate.

## Isolated verification

No real launcher was called. All launch boundary probes used the injected runner argument with the inert argv `isolated-fake-launch --no-live-action` and temporary state paths. No live process, tmux pane, existing runtime worker, gateway, service, or configuration was touched.

Focused candidate suites:

```text
PYTHONPATH=src python -m pytest -q tests/test_pi_spawn_control.py tests/test_pi_cockpit_spawn_control.py
32 passed in 0.26s
```

The suites cover bootstrap, pause, drain, status, renew, expiry, resume, replay, owner, scope, fence, lease, malformed and partial state, reservation cleanup, process and tmux final guards, existing workers, and serialized concurrent transitions.

Independent probe results:

| Transition | Parent | Candidate |
| --- | --- | --- |
| Inconsistent pause history with open state, process boundary | fake runner reached | `StateUnavailableError`, runner not reached |
| Mismatched resume history with open state, tmux boundary | fake runner reached | `StateUnavailableError`, runner not reached |
| Future existing worker `reserved_at`, process boundary | not required | fake runner reached |
| Future existing worker `reserved_at`, tmux boundary | not required | fake runner reached |

For both future time probes, the preexisting worker token and future timestamp remained byte for byte unchanged after the fake launch reservation was cleaned up.

Durable probe artifacts:

* `~/.skcapstone/evidence/work/8d43bd03/escalated-independent-probe.py`
* Probe SHA-256: `65a090f620cf7b93b9e0a504071ea73aff3538a79c795383d3d60361f38e4f1e`
* `~/.skcapstone/evidence/work/8d43bd03/escalated-independent-probe-result.json`
* Result SHA-256: `97d2f4c3f0e070e50e963bcad944672a41b8e68d8fb1412cc16ee6cf628a9d9e`

## Exact blocker

Acceptance criterion 2 requires every legal and illegal time transition to be independently verified. In frozen candidate `9ab43e7`, `SpawnControl._load` parses each worker `reserved_at` but does not require it to be at or before top level `updated_at` or the current clock. A syntactically valid worker reservation one hour in the future is therefore accepted. A second `guarded_run` then reaches an injected fake runner at both the process and tmux boundaries.

This contradicts acceptance criterion 2 as written and prevents qualification of criterion 3. The exact machine readable blocker is:

```text
blocked_on=card referent=ac:2
```

A successor commit `b7fa7edc432b3087d6c4213f06d1e233aacfd618` exists on the publication base and describes a worker time ordering repair. It is not the frozen candidate named by this card and was not substituted for it.

No deployment, restart, merge, live control, `live_execution`, automerge, WAKE-02 enablement, human signoff, or deployment authority is granted.

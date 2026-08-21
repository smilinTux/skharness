# Incident 1df443cb: corrected Pi swarm phase boundary on `.41`

Qualified at `2026-08-21T20:21Z` on `cbrd21-laptop12thgenintelcore` from
SKHarness commit `3bfe5b4a82d25cbf02d1174fb5f0fd6ec530bead`.

This evidence closes the specific fail-open incident in which an empty timed-out scout
result set admitted a builder. It does not claim that card `41077231` was implemented,
that the independent verifier approved it, or that the S/M/L quality evaluation is
complete.

## Runtime identity

- Node: `.41` (`192.168.0.41`), Intel/iGPU fleet node
- Kernel: `6.18.44-1-MANJARO`
- Worktree: `/home/cbrd21/clawd/worktrees/skharness-swarm-41077231`
- Image: `skharness-pi-python-test:main-a839928`
- Local image ID: `sha256:d8c793617ad5ed234bf17c4bb15ad2671758bee95aefbce8b5ff203e06a4350f`
- Container identity: `10001:10001`
- Requested model: `ornith-1.5-9b`
- Gateway route: `http://100.86.156.5:18780/v1`
- Trajectory: `pi-swarm-phaseauth-41077231-202005Z`

The partial Pi streams did not contain a provider-owned `responseModel`, so this run
does not claim the actual served model or backend. It never substitutes the requested
model for that missing fact. Card `d3c6377a` owns the canonical SKGateway join needed
for complete serving provenance.

## Scenario and trust boundary

The immutable plan contained two parallel read-only scouts, one builder dependent on
both scout results, and one tester dependent on the builder. Both scouts were asked to
find exact cross-repository source and live request/response evidence for card
`41077231`. Unavailable or incomplete evidence had to stop the plan at the scout phase.

The trusted controller—not Pi—owned the exact plan, global team budget, child leases,
phase receipts, one-use authorizations, A2A journal, cancellation, and completion gate.
No attestation was supplied for this deliberately blocked run.

## Measured result

| Worker | Duration | Tokens | Pi tool envelopes | Guarded discovery operations | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| `scout-evidence-v2` | 32.964 s | 5,376 | 4 | 9 | `blocked`; inspection ceiling 8 |
| `scout-harness-v2` | 62.164 s | 43,547 | 7 | 10 | `blocked`; inspection ceiling 8 |

Pi used compound shell invocations. The command-aware guard counted every `find`,
`grep`, `rg`, or `ls` subcommand, while the generic run metric counted one tool envelope
per shell invocation. The 9/10 versus 8 denials were therefore real budget enforcement,
not a regex token misclassified as a filesystem path.

The durable scheduler settled 48,923 tokens, 11 tool envelopes, and 96.229 seconds.
Neither child exceeded its token, envelope, wall-time, or cost contract; both stopped
because compound discovery operations crossed the separate inspection ceiling.

The decisive assertions all passed:

- assignment journal contains exactly the two planned scouts;
- executed worker set contains exactly the two planned scouts;
- `builder-v2` and `tester-v2` were never assigned or started;
- no phase receipt or downstream authorization was issued;
- completion was denied and the team ended `cancelled: true` with
  `phase_execution_failed`;
- both negative results and their usage remained in the content-checked scheduler
  checkpoint;
- no run-owned `arena-pi-*` or `arena-proxy-*` container remained; and
- Git status was empty before and after the run.

Six older `sbxproxy-*` containers were observed before this qualification and were not
attributed to it or deleted by this run.

## Evidence files

Raw evidence root on `.41`:

`/home/cbrd21/.skcapstone/qualification/pi-swarm-phaseauth-rerun-20260821/41077231-202005Z`

| Artifact | SHA-256 |
| --- | --- |
| `qualification.json` | `d71f6c6e079fb1d4fc6f817e0f75df92d5e003cd74e146d7de10c7b4912551d0` |
| `swarm-state.json` | `b8033bb2188d36157adb39ca942938dcfde2009046bfd899b85a07ce14547584` |
| `a2a.jsonl` | `1318944991f6299dee876c25eefad6df36c043892a19b6c295128916bb61a3e0` |
| Evidence-scout `run.json` | `cdac59daf2933da456dc9a79f59eaeb07a0d0a4d02f98fb13b21dc50d070266d` |
| Harness-scout `run.json` | `d2c767380acd608778f9dcbdaefcf06ad71d9de3fa214bcaddf30e9da197e116` |

The image tag has a local immutable image ID but this evidence does not claim a registry
digest, Cosign verification, or vulnerability scan for that tag. Those remain release
qualification facts, separate from the phase-boundary incident.

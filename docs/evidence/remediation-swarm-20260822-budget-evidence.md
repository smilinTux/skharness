# Remediation swarm budget evidence — 2026-08-22

The M scout profile now uses an aggregate token share, bounded per-request
generation, and a controller-enforced inspection ceiling. On the final run,
`400bf174` emitted a typed `NO_ACTION` disposition within its budget; the
orchestrator correctly withheld builder/tester admission.

The S preflight remained `review_required` in the two retry records because the
Qwen upstream returned `502 upstream unreachable` after two inspection calls.
This is infrastructure saturation evidence, not a terminal-parser or cleanup
failure. Both runs retained activity/trajectory/lease evidence and ended with
zero current-run resources. A future S retry must be performed only after the
Qwen capacity domain is below saturation; no completion decision is inferred
from the failed request.

References:

- `/home/cbrd21/.skcapstone/qualification/remediation-swarm-20260822-final-fix3/qualification.json`
- `/home/cbrd21/.skcapstone/qualification/remediation-swarm-20260822-s-retry2/qualification.json`
- Controller commit `5a3f587a1fbb5eccda9f1c878ea9c056ed44875b`
- Worker image `ghcr.io/smilintux/skharness-pi-python-test@sha256:8e991c893e7553522369a35d10b78ae2e831eb62b9f127ba53a7dabd045e2c7d`

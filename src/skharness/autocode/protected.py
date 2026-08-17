"""Carve-out detector: does a diff touch the operator seat's own guardrails?

The Operator Seat can rewrite almost anything in the ecosystem autonomously,
EXCEPT the machinery that constrains it: the freeze/kill-switch, the twin gate,
the signing/identity, the escalation/approval policy, and this detector itself.
A change touching any of those must ALWAYS escalate to a human, never auto-merge,
even at score 5 with green CI. Tests cannot catch a diff that removes the freeze
check (it can still pass), so this path-level gate is the backstop.

Fail-closed by construction: a missing, unreadable, unsigned, or empty manifest
protects EVERYTHING, so a tampered or absent manifest cannot open the gate. A
hard-coded floor (`_ALWAYS_PROTECTED`) keeps the core guardrail files protected
even under a valid-but-incomplete manifest, so the detector cannot be unprotected
by editing the manifest alone.
"""
from __future__ import annotations

import json
from fnmatch import fnmatch
from pathlib import Path

#: Manifest that protects everything. Returned on any load failure so the gate
#: fails closed (is_protected always True -> everything escalates).
_FAIL_CLOSED: dict = {"protected": ["**"], "_fail_closed": True}

#: The core guardrail files, protected regardless of what the manifest says, so
#: the leash cannot be loosened by omitting an entry from the manifest. Matched
#: against repo-relative or absolute paths (leading `*/` tolerates either).
_ALWAYS_PROTECTED: tuple[str, ...] = (
    "*/skharness/autocode/protected.py",     # this detector
    "*skharness/autocode/protected.py",
    "*/skharness/autocode/engineering.py",   # finalize: the automerge choke point
    "*skharness/autocode/engineering.py",
    "*/skcapstone/fleet/store.py",           # is_frozen + plane-file write rule
    "*skcapstone/fleet/store.py",
    "*/skcapstone/itil.py",                  # the approval fold (agent=='human' gate)
    "*skcapstone/itil.py",
    "*/objects/_protected.json",             # the manifest itself
    "*objects/_protected.json",
    "*/objects/_freeze.json",                # the kill switch
    "*objects/_freeze.json",
    # The work-grade policy. Once a card's grade selects the model that does the
    # work, the grade IS routing policy: it decides the capability floor and,
    # through sensitivity, which trust zone may see the card's data. An engine
    # that can rewrite the rubric that routes it, then auto-merge behind a twin
    # gate whose CI arm it satisfies with tests it wrote itself, is the same
    # self-modification hazard CR-6.2 already ruled on for skos and skharness.
    # These are guardrails, not features, so they sit on the hard-coded floor
    # rather than relying on a manifest entry someone can omit.
    "*/skharness/autocode/grading.py",        # the rubric text and the parser
    "*skharness/autocode/grading.py",
    "*/skharness/autocode/sensitivity.py",    # the deterministic exposure rules
    "*skharness/autocode/sensitivity.py",
    "*/skharness/autocode/buckets.py",        # grade -> trust-zone addressing
    "*skharness/autocode/buckets.py",
    "*/skharness/autocode/routing_guard.py",  # S14: routing cannot touch gate fields
    "*skharness/autocode/routing_guard.py",
    # THE WORKER-INDEPENDENT OUTCOME LABEL (S26, card 788425b8; module built by
    # S23, card 33c50540). The whole and only value of this label is that THE
    # WORKER CANNOT AUTHOR THE MUTANTS: the operator table lives in the harness,
    # fixed, which is what makes it independent of the tests the worker wrote.
    # A worker that can edit `mutation.py` can weaken its own independent grader,
    # and it can do so INVISIBLY, because a smaller operator table produces a
    # perfectly well-formed `survived_clean` row rather than an error. That is
    # the coverage-config hazard (S21) exactly: an engine that can move the
    # instrument does not need to satisfy it.
    #
    # Floor rather than manifest, for the same reason as the rubric: the
    # realistic failure is not that someone removes an entry, it is that nobody
    # ever adds one. The label is shadow-only today and gates nothing, so this
    # protects a measurement rather than a decision. It is on the floor
    # ANTICIPATING that a measurement is what a future decision would be built
    # from, and a floor added after the label starts mattering is a floor added
    # too late.
    "*/skharness/autocode/mutation.py",
    "*skharness/autocode/mutation.py",
    "*/autocode/data/joule-grade-vocabulary.json",   # the vendored enums
    "*autocode/data/joule-grade-vocabulary.json",
    "*/tests/data/joule-economy-golden-set-*.json",  # the calibration reference
    "*tests/data/joule-economy-golden-set-*.json",
    # The REVIEWED EXCEPTIONS to the grading floor (S25, card 3f6719e4). This
    # file is how an accepted change to grading.py / sensitivity.py / buckets.py
    # / the vocabulary / the golden set is recorded, so it is exactly as powerful
    # as the floor it excepts: anything that can write this list can write the
    # rubric. It is therefore floor itself, and a diff that adds an allowance can
    # never auto-merge, it always escalates. That is what makes the allowance
    # mechanism REVIEWED rather than automatic, structurally rather than by
    # convention.
    "*/tests/data/grading-floor-allowances.json",
    "*tests/data/grading-floor-allowances.json",
    # The COVERAGE INSTRUMENT'S OWN CONFIGURATION (S21, card 53b8c8be). The twin
    # gate's third arm measures the diff with `pytest --cov`; the files below
    # decide WHAT is measured. A diff that adds an `omit` rule blinds the
    # instrument that grades it, without touching CI and without appearing to
    # weaken anything. That is the rubric hazard again, one level down: an engine
    # that can move the instrument does not need to satisfy it. Coverage config
    # is therefore floor, not manifest, for the same reason the rubric is: the
    # realistic failure is not that someone removes an entry, it is that nobody
    # ever adds one.
    #
    # ACCEPTED COST, recorded deliberately: `pyproject.toml` and `conftest.py`
    # change often for reasons that have nothing to do with coverage, so under
    # auto-merge these globs will route ordinary dependency bumps to human
    # review. That is the conservative direction (the floor never blocks work, it
    # only refuses to merge it unattended) and auto-merge is off fleet-wide
    # today, so the live cost is zero. If the noise ever becomes real, narrow
    # these globs with a written reason; do not silently drop them.
    "*/.coveragerc", "*.coveragerc",
    "*/pyproject.toml", "*pyproject.toml",     # [tool.coverage.run] omit / [tool.pytest]
    "*/pytest.ini", "*pytest.ini",
    "*/setup.cfg", "*setup.cfg",               # [coverage:run] omit
    "*/tox.ini", "*tox.ini",
    "*/conftest.py", "*conftest.py",           # can disable plugins at import time
)


def load_manifest(root: str | Path, *, verify=None) -> dict:
    """Load `objects/_protected.json` under *root*, failing closed.

    Args:
        root: the fleet tree root containing `objects/_protected.json`.
        verify: optional callable(manifest_dict) -> bool for signature
            verification. When it returns False the manifest is rejected and
            the gate fails closed. Callers that want the real capauth check
            wired in (the production path) should go through `_manifest_for`,
            `matched_protected_paths`, or `changed_paths_are_protected`
            instead of calling this directly with `verify=None`, which skips
            verification entirely (kept for the manifest-shape unit tests
            below, which are not exercising signing).

    Returns:
        The manifest dict, or `_FAIL_CLOSED` (protects everything) on any
        failure: missing file, unreadable JSON, failed verification, or an
        empty/malformed `protected` list.
    """
    path = Path(root) / "objects" / "_protected.json"
    if not path.exists():
        return dict(_FAIL_CLOSED)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return dict(_FAIL_CLOSED)
    if verify is not None and not verify(data):
        return dict(_FAIL_CLOSED)
    if not isinstance(data, dict) or not isinstance(data.get("protected"), list) \
            or not data["protected"]:
        return dict(_FAIL_CLOSED)
    return data


def is_protected(changed_paths, manifest: dict) -> bool:
    """True when any changed path matches a protected glob.

    The core guardrail files (`_ALWAYS_PROTECTED`) are protected regardless of
    the manifest, so the gate cannot be opened by editing the manifest alone.
    """
    globs = list(manifest.get("protected", [])) + list(_ALWAYS_PROTECTED)
    return any(fnmatch(str(p), g) for p in changed_paths for g in globs)


def _default_verify(manifest: dict) -> bool:
    """The real capauth signature check (Card P6, coord `08963fbb`).

    Wired here rather than left `None`: this is the one production call path
    (`_manifest_for` -> `matched_protected_paths` /
    `changed_paths_are_protected`, which `engineering.py` finalize calls on
    every build) that used to pass no verifier at all, so a signed-looking
    but unsigned manifest was accepted as long as its JSON parsed. See
    `plane_trust` for the rollout gate (`SKFLEET_SIGNING`, default off, so
    this is a no-op True until an operator opts in) and the threat model.
    """
    from .plane_trust import payload_trusted

    return payload_trusted(manifest, label="_protected.json")


def _manifest_for(root: str | Path, *, verify=None) -> dict:
    """Bootstrap-safe manifest load, shared by the two finalize-facing helpers.

    When the manifest is present it adds its extra paths (and fails closed on
    tamper, via `load_manifest`, using the real capauth check unless a caller
    overrides `verify` for a test). When it is ABSENT the gate protects only
    the core rather than everything, so the autopilot keeps auto-merging
    normal work during the carve-out rollout instead of stalling fleet-wide.
    """
    mpath = Path(root) / "objects" / "_protected.json"
    if not mpath.exists():
        return {"protected": []}
    return load_manifest(root, verify=verify if verify is not None else _default_verify)


def matched_protected_paths(root: str | Path, changed_paths, *, verify=None) -> list[str]:
    """The changed paths that actually hit the floor, in input order.

    S17: the gate answered a bare bool, so a hold could say THAT it held but not
    WHAT it held on, and an evaluation that found nothing was indistinguishable
    from an evaluation that never ran. Returning the matches makes both the hold
    and the clean pass reviewable by a human who was not there.
    """
    manifest = _manifest_for(root, verify=verify)
    return [str(p) for p in changed_paths if is_protected([p], manifest)]


def changed_paths_are_protected(root: str | Path, changed_paths, *, verify=None) -> bool:
    """Finalize-facing gate, bootstrap-safe.

    The core guardrail files (`_ALWAYS_PROTECTED`) are ALWAYS protected, with or
    without a manifest, so essential protection needs no rollout. See
    `_manifest_for` for the manifest-absent behaviour.
    """
    return is_protected(changed_paths, _manifest_for(root, verify=verify))

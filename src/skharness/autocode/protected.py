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
)


def load_manifest(root: str | Path, *, verify=None) -> dict:
    """Load `objects/_protected.json` under *root*, failing closed.

    Args:
        root: the fleet tree root containing `objects/_protected.json`.
        verify: optional callable(manifest_dict) -> bool for signature
            verification (wired to capauth in the integration card). When it
            returns False the manifest is rejected and the gate fails closed.

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


def changed_paths_are_protected(root: str | Path, changed_paths) -> bool:
    """Finalize-facing gate, bootstrap-safe.

    The core guardrail files (`_ALWAYS_PROTECTED`) are ALWAYS protected, with or
    without a manifest, so essential protection needs no rollout. When the
    manifest is present it adds its extra paths (and fails closed on tamper, via
    `load_manifest`). When it is ABSENT the gate protects only the core rather
    than everything, so the autopilot keeps auto-merging normal work during the
    carve-out rollout instead of stalling fleet-wide.
    """
    mpath = Path(root) / "objects" / "_protected.json"
    manifest = load_manifest(root) if mpath.exists() else {"protected": []}
    return is_protected(changed_paths, manifest)

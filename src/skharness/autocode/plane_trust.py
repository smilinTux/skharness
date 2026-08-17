"""Plane-file signature trust (Card P6, coord `08963fbb`; spec sections 3 + 8).

`objects/_freeze.json` (the human kill switch) and `objects/_protected.json`
(the carve-out manifest, see `protected.py`) both carry a `writer.signature`
slot that nothing filled in and nothing checked: the live freeze card was
last written `capauth:lumina`, `signature: null`, and the manifest shipped
"Chef to PGP-sign when the signature ceremony lands" and stayed that way.
This module is the CHECKING half only. It does not sign; see
`sign_plane_files.py` for the human/operator-run tool that does, and read its
module docstring for why that step could not be completed unattended in this
change (the operator's private key is deliberately held offline).

Rollout mirrors Card 3.5's own `SKFLEET_SIGNING` flag (off | permissive |
enforce) instead of inventing a second knob for the same signing subsystem:

- **off** (default): behave exactly as before this module existed. A plane
  file's content is trusted at face value, unsigned or not. This is
  deliberate: today nothing anywhere signs a freeze or manifest write, so
  defaulting to "verify and reject" would flip a fleet-wide kill switch (and
  freeze all placement) the moment this code ships, before anyone chose to
  turn it on. "Unconfigured" therefore means "unchanged", the safe direction
  for a rollout that cannot yet complete the signing ceremony (root key
  offline; see `sign_plane_files.py`).
- **permissive**: verify and log loudly on unsigned/tampered/absent, but
  still trust the file's content. Lets an operator watch what enforce WOULD
  do before flipping to it.
- **enforce**: fail closed. Unsigned, tampered, absent, or unverifiable (no
  trust roster, capauth unavailable) all count as untrusted, with NO time-
  boxed grace period. For the manifest that means "protect everything"
  (`protected.py`'s existing `_FAIL_CLOSED`); for the freeze file it means
  "treat as frozen" (halt placement), the same "when in doubt, halt"
  direction `skcapstone.fleet.store.is_frozen` already takes for an
  unreadable file.

Threat model, stated plainly (do not oversell this):

- **Protects against**: the autopilot/autocode path, or any other
  programmatic writer, silently overwriting a plane file (a bug, a bad
  merge, a runaway script) and having that change trusted with no signal.
  Once enforced, an unsigned or tampered plane file is REJECTED, not
  silently honored.
- **Does NOT protect against**: an interactive shell running as the
  operator, or anything that can invoke `gpg`/capauth the way the operator
  can. Shell is root-equivalent on these nodes; whoever holds it can sign
  whatever they like, including a false freeze state. This closes the
  autopilot path, not the operator's own hands. See spec section 9: "No
  attempt to make the freeze card cryptographically AI-proof against an
  interactive shell."
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

__all__ = [
    "signing_mode",
    "trust_status",
    "payload_trusted",
    "path_trusted",
]


def signing_mode() -> str:
    """The shared Card 3.5 rollout mode (off | permissive | enforce).

    Soft-imported: no skcapstone sibling installed means signing was never
    wired at all, which is exactly "off" behavior (unchanged today).
    """
    try:
        from skcapstone.fleet.signing import signing_mode as _mode

        return _mode()
    except Exception:  # noqa: BLE001
        return "off"


def _verifier():
    """A capauth verifier over the local trust roster, or None.

    None covers every unusable case identically on purpose: skcapstone not
    installed, capauth not installed, no trusted keys enrolled, or a runtime
    error resolving any of those. A verifier that cannot run must produce the
    same "cannot trust this" outcome as a verifier that ran and failed,
    otherwise a broken import would quietly open the gate it exists to hold
    closed.
    """
    try:
        from skcapstone.fleet.signing import capauth_verifier

        return capauth_verifier()
    except Exception:  # noqa: BLE001
        return None


def trust_status(payload: dict) -> tuple[str, str]:
    """Classify a plane-file payload: verified | unsigned | invalid.

    Delegates the actual classification to `skcapstone.fleet.signing`'s own
    `verify_payload`, which already knows the `writer.signature` envelope
    shape and canonicalization used across the fleet store (spec, placement,
    freeze). Reused rather than re-implemented so there is exactly one
    canonicalization rule for "what bytes did the signature cover."
    """
    try:
        from skcapstone.fleet.signing import verify_payload
    except Exception as exc:  # noqa: BLE001
        return ("invalid", f"skcapstone.fleet.signing unavailable: {exc}")
    verifier = _verifier()
    if verifier is None:
        return ("invalid", "no trusted signer roster available")
    return verify_payload(payload, verifier)


def payload_trusted(payload: dict, *, label: str = "plane-file", warn: bool = True) -> bool:
    """True when *payload* may be trusted, per the current rollout mode.

    off: always True (unchanged pre-P6 behavior).
    permissive: True regardless, but logs on anything short of verified.
    enforce: True only when the signature verifies; everything else is False,
        with no grace period.
    """
    mode = signing_mode()
    if mode == "off":
        return True
    status, detail = trust_status(payload)
    if status == "verified":
        return True
    if warn:
        print(f"plane-trust[{mode}] {label}: {status} ({detail})", file=sys.stderr)
    return mode == "permissive"


def path_trusted(path: Path, *, label: Optional[str] = None, warn: bool = True) -> bool:
    """`payload_trusted`, reading *path* off disk first.

    A missing or unreadable file is untrusted the same way an invalid
    signature is: off still returns True (unchanged behavior), permissive
    warns and returns True, enforce returns False.
    """
    mode = signing_mode()
    tag = label or path.name
    if mode == "off":
        return True
    if not path.exists():
        if warn:
            print(f"plane-trust[{mode}] {tag}: absent", file=sys.stderr)
        return mode == "permissive"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if warn:
            print(f"plane-trust[{mode}] {tag}: unreadable ({exc})", file=sys.stderr)
        return mode == "permissive"
    return payload_trusted(payload, label=tag, warn=warn)

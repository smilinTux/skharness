"""Sign the plane control files: the human/operator-run half of Card P6
(coord `08963fbb`; spec sections 3 + 8). `plane_trust.py` is the other half
(the checking side, wired into the read paths); this module only writes.

WHY THE LIVE FILES ARE NOT SIGNED BY THIS CHANGE
--------------------------------------------------
The operator/root capauth identity (chef@skworld.io, current fingerprint
`ADAD14CCAC8D6D0BF5A4209DB994E78200BF6422`) is human-controlled custody.
That is not an obstacle to route around: it is the property this whole card
exists to protect, that the freeze card stays the one thing an autonomous
agent session cannot forge unattended. This script therefore:

  - Resolves the capauth home to sign with EXPLICITLY (`--capauth-home`, or
    `capauth.resolve_capauth_home()`, the agent-BLIND operator home), never
    through the acting-agent-first precedence
    `skcapstone.fleet.signing.capauth_signer()` uses for ordinary writes.
    That precedence is correct for an agent seat signing its own spec
    writes; it is exactly wrong here, where the whole point is "did the
    OPERATOR'S key sign this," not "did whichever identity is active sign
    this."
  - Reads that home's OWN public key fingerprint and refuses outright if it
    does not match `--expect-fingerprint` (default the root fingerprint
    above), so a stray or wrong key sitting in the resolved home cannot
    produce a signature this file would then claim is the operator's.
  - Refuses outright, with a plain explanation, when no private key is
    present (the expected, current state on this node) instead of doing
    anything silent or partial.

Run this on the node/session where the operator's private key is actually
loaded (or via the capauth Bunker remote signer), not as an autonomous agent.

WHAT IT DOES
------------
For each of `_freeze.json` and `_protected.json` under the fleet root:
  1. Load and parse the file (refuses to touch one it cannot parse).
  2. Normalize it onto the shared `writer: {role, identity, suite_id,
     signature}` envelope `skcapstone.fleet.signing` already canonicalizes
     and verifies (spec, placement, and freeze already use this shape;
     `_protected.json`'s legacy top-level `signer`/`signature` pair is
     migrated onto it here, so there is exactly one signing/verification
     rule for every plane file rather than a second bespoke one).
  3. Sign the canonical bytes with the verified operator key.
  4. Re-verify the freshly-written payload against the same local trust
     roster a reader will check (`plane_trust`/`capauth_verifier`), refusing
     to leave a file on disk whose own signature this process could not
     confirm.

Idempotent: re-running re-signs (fresh signature over the same content), it
does not error on an already-signed file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_IDENTITY = "capauth:chef@skworld.io"
DEFAULT_FINGERPRINT = "ADAD14CCAC8D6D0BF5A4209DB994E78200BF6422"


def _fleet_root() -> Path:
    from skcapstone.fleet.paths import default_paths

    return default_paths().root


def _resolve_operator_home(explicit: str | None) -> Path | None:
    """The operator's OWN capauth home, agent-blind, never agent-first.

    Deliberately bypasses `skcapstone.fleet.signing._capauth_home`'s
    acting-agent-first precedence: that precedence exists so an agent seat
    signs its own ordinary spec writes with its own key, which is exactly
    the wrong resolution here.
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get("CAPAUTH_HOME")
    if env:
        return Path(env)
    try:
        from capauth import resolve_capauth_home

        return resolve_capauth_home()
    except Exception:  # noqa: BLE001
        return None


def _fingerprint_of(path: Path) -> str | None:
    try:
        import pgpy

        key, _ = pgpy.PGPKey.from_file(str(path))
        return str(key.fingerprint).replace(" ", "")
    except Exception:  # noqa: BLE001
        return None


def _resolve_signer(home: Path, expect_fingerprint: str):
    """(signer_callable, error) for *home*, pinned to `expect_fingerprint`.

    Returns `(None, reason)` for every refusal case (no home, no public key,
    fingerprint mismatch, no private key, key/backend failure), so the
    caller has one branch to handle rather than several silent Nones.
    """
    if home is None:
        return None, "could not resolve a capauth home to sign with"
    public = home / "identity" / "public.asc"
    if not public.exists():
        return None, f"{home} has no identity/public.asc"
    fpr = _fingerprint_of(public)
    if fpr is None:
        return None, f"could not read a fingerprint from {public}"
    if fpr.upper() != expect_fingerprint.strip().upper():
        return None, (f"{public} fingerprint {fpr} does not match the expected "
                       f"operator fingerprint {expect_fingerprint}; refusing to "
                       f"sign with a key that is not the one this file must "
                       f"claim")
    private = home / "identity" / "private.asc"
    if not private.exists():
        return None, (f"{home} has no identity/private.asc (the operator "
                       f"private key is deliberately held OFFLINE per custody "
                       f"policy; sign via the Bunker remote signer, or run "
                       f"this on the node/session where the key is actually "
                       f"loaded)")
    try:
        from capauth.crypto import get_backend

        armor = private.read_text(encoding="utf-8")
        passphrase = os.environ.get("CAPAUTH_PASSPHRASE", "")
        backend = get_backend()

        def _sign(data: bytes) -> str:
            return backend.sign(data, armor, passphrase)

        return _sign, None
    except Exception as exc:  # noqa: BLE001
        return None, f"signing backend unavailable: {exc}"


def _normalize_protected(data: dict, *, identity: str) -> dict:
    """Migrate legacy top-level `signer`/`signature` onto the `writer` block.

    Preserves `version`, `note`, and `protected`; the legacy `signer` /
    `signature` fields are dropped once represented in `writer.identity` /
    `writer.signature`, so the file carries exactly one signed identity
    claim rather than two that could disagree.
    """
    from skcapstone.fleet.signing import SUITE_ID

    out = {k: v for k, v in data.items() if k not in ("signer", "signature", "writer")}
    out["writer"] = {
        "role": "operator",
        "identity": identity,
        "suite_id": SUITE_ID,
        "signature": None,
    }
    return out


def _normalize_freeze(data: dict, *, identity: str) -> dict:
    payload = dict(data)
    writer = dict(payload.get("writer") or {})
    writer["identity"] = identity
    writer["role"] = "operator"
    writer["signature"] = None
    payload["writer"] = writer
    return payload


def sign_one(path: Path, *, home: Path | None, identity: str,
             expect_fingerprint: str, dry_run: bool) -> bool:
    """Sign one plane file in place. Returns True on success (or a clean
    dry-run), False on any refusal -- every refusal prints why."""
    from skcapstone.fleet.signing import canonical_bytes, capauth_verifier, verify_payload

    if not path.exists():
        print(f"SKIP {path}: does not exist", file=sys.stderr)
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"REFUSE {path}: unreadable ({exc})", file=sys.stderr)
        return False

    payload = (_normalize_protected(data, identity=identity)
               if path.name == "_protected.json"
               else _normalize_freeze(data, identity=identity))

    signer, err = _resolve_signer(home, expect_fingerprint)
    if signer is None:
        print(f"REFUSE {path}: {err}", file=sys.stderr)
        return False

    payload["writer"]["signature"] = signer(canonical_bytes(payload))

    # Re-verify against the SAME roster a reader will check, before trusting
    # our own write: an unverifiable signature is worse than none, since it
    # LOOKS trustworthy without being checkable.
    # Self-check against the same explicitly selected operator home used to
    # sign.  The ordinary verifier is acting-agent aware; without this pin an
    # operator ceremony launched from an agent session signs with Chef but
    # attempts verification against that agent's roster.
    previous_home = os.environ.get("CAPAUTH_HOME")
    if home is not None:
        os.environ["CAPAUTH_HOME"] = str(home)
    try:
        verifier = capauth_verifier()
    finally:
        if previous_home is None:
            os.environ.pop("CAPAUTH_HOME", None)
        else:
            os.environ["CAPAUTH_HOME"] = previous_home
    if verifier is None:
        print(f"REFUSE {path}: signed, but no local trust roster available to "
              f"confirm it against; not writing an unverifiable file",
              file=sys.stderr)
        return False
    status, detail = verify_payload(payload, verifier)
    if status != "verified":
        print(f"REFUSE {path}: freshly-signed payload failed self-check "
              f"({status}: {detail}); not writing", file=sys.stderr)
        return False

    if dry_run:
        print(f"DRY-RUN {path}: would sign as {identity}")
        return True

    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                     encoding="utf-8")
    print(f"SIGNED {path} as {identity}")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--root", default=None,
                        help="fleet tree root (default: skcapstone's default_paths())")
    parser.add_argument("--capauth-home", default=None,
                        help="operator capauth home to sign with (default: "
                             "CAPAUTH_HOME env, else capauth.resolve_capauth_home())")
    parser.add_argument("--identity", default=DEFAULT_IDENTITY,
                        help=f"identity claim written into writer.identity "
                             f"(default: {DEFAULT_IDENTITY})")
    parser.add_argument("--expect-fingerprint", default=DEFAULT_FINGERPRINT,
                        help="refuse to sign unless the resolved home's public "
                             "key has exactly this fingerprint")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen without writing")
    args = parser.parse_args(argv)

    try:
        root = Path(args.root) if args.root else _fleet_root()
    except Exception as exc:  # noqa: BLE001
        print(f"cannot resolve fleet root: {exc}", file=sys.stderr)
        return 2

    home = _resolve_operator_home(args.capauth_home)
    objects = root / "objects"
    targets = [objects / "_freeze.json", objects / "_protected.json"]
    ok = True
    for path in targets:
        if not sign_one(path, home=home, identity=args.identity,
                        expect_fingerprint=args.expect_fingerprint,
                        dry_run=args.dry_run):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

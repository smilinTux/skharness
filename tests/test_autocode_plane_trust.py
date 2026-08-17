"""Plane-file signature trust (Card P6, coord `08963fbb`).

Uses the same fake_signer/fake_verifier pattern as skcapstone's own
`tests/fleet/test_signing.py` (sha256-digest stand-ins for gpg), monkeypatched
onto `skcapstone.fleet.signing.capauth_verifier` so these tests exercise the
real `verify_payload`/`canonical_bytes` classification without needing a real
PGP keypair or capauth home on disk.
"""
from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("skcapstone", reason="plane_trust delegates to skcapstone.fleet.signing")

from skcapstone.fleet import signing as fleet_signing  # noqa: E402

from skharness.autocode import plane_trust  # noqa: E402


def fake_signer(data: bytes) -> str:
    return "sig:" + hashlib.sha256(data).hexdigest()


def fake_verifier(data: bytes, sig: str) -> bool:
    return sig == "sig:" + hashlib.sha256(data).hexdigest()


def _signed_payload(extra: dict | None = None) -> dict:
    payload = {"protected": ["*/x.py"], "writer": {"identity": "capauth:chef@skworld.io",
                                                    "role": "operator", "signature": None}}
    if extra:
        payload.update(extra)
    payload["writer"]["signature"] = fake_signer(fleet_signing.canonical_bytes(payload))
    return payload


# --------------------------------------------------------------------------
# off (default): behavior is unchanged from before this module existed.
# --------------------------------------------------------------------------

def test_off_is_the_default(monkeypatch):
    monkeypatch.delenv(fleet_signing.SIGNING_ENV, raising=False)
    assert plane_trust.signing_mode() == "off"


def test_off_mode_trusts_unsigned_payload(monkeypatch):
    monkeypatch.delenv(fleet_signing.SIGNING_ENV, raising=False)
    assert plane_trust.payload_trusted({"protected": []}) is True


def test_off_mode_trusts_missing_path(monkeypatch, tmp_path):
    monkeypatch.delenv(fleet_signing.SIGNING_ENV, raising=False)
    assert plane_trust.path_trusted(tmp_path / "_freeze.json") is True


# --------------------------------------------------------------------------
# enforce: fail closed, no grace period.
# --------------------------------------------------------------------------

def test_enforce_mode_rejects_unsigned(monkeypatch):
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: fake_verifier)
    payload = {"protected": ["*/x.py"], "writer": {"identity": "capauth:chef@skworld.io",
                                                    "signature": None}}
    assert plane_trust.payload_trusted(payload, warn=False) is False


def test_enforce_mode_rejects_tampered(monkeypatch):
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: fake_verifier)
    payload = _signed_payload()
    payload["protected"] = ["*/evil.py"]  # mutated after signing
    assert plane_trust.payload_trusted(payload, warn=False) is False


def test_enforce_mode_trusts_a_valid_signature(monkeypatch):
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: fake_verifier)
    payload = _signed_payload()
    assert plane_trust.payload_trusted(payload, warn=False) is True


def test_enforce_mode_no_roster_fails_closed(monkeypatch):
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: None)
    payload = _signed_payload()
    assert plane_trust.payload_trusted(payload, warn=False) is False


def test_enforce_mode_path_missing_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: fake_verifier)
    assert plane_trust.path_trusted(tmp_path / "_freeze.json", warn=False) is False


def test_enforce_mode_path_unreadable_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: fake_verifier)
    bad = tmp_path / "_freeze.json"
    bad.write_text("{ not json")
    assert plane_trust.path_trusted(bad, warn=False) is False


def test_enforce_mode_path_with_valid_signature_is_trusted(monkeypatch, tmp_path):
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: fake_verifier)
    good = tmp_path / "_freeze.json"
    good.write_text(__import__("json").dumps(_signed_payload({"frozen": False})))
    assert plane_trust.path_trusted(good, warn=False) is True


# --------------------------------------------------------------------------
# permissive: verify and warn, but still trust (staging step before enforce).
# --------------------------------------------------------------------------

def test_permissive_mode_warns_and_still_trusts(monkeypatch, capsys):
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "permissive")
    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: fake_verifier)
    payload = {"protected": ["*/x.py"], "writer": {"identity": "capauth:chef@skworld.io",
                                                    "signature": None}}
    assert plane_trust.payload_trusted(payload) is True
    assert "unsigned" in capsys.readouterr().err

"""Card P6 (coord `08963fbb`): the human/operator-run signing tool.

Uses a REAL ephemeral Ed25519 keypair (capauth's PGPy backend), not the
sha256 stand-in the other P6 tests use, so this file is the one place the
actual sign/verify round trip through real PGP is exercised end to end.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("skcapstone", reason="sign_plane_files delegates to skcapstone.fleet.signing")
pytest.importorskip("pgpy", reason="requires a real PGP backend for the round trip")

from capauth.crypto import get_backend  # noqa: E402
from capauth.models import Algorithm  # noqa: E402

from skharness.autocode import sign_plane_files as spf  # noqa: E402

TEST_FPR_LABEL = "sign-plane-files-test"


@pytest.fixture
def operator_home(tmp_path, monkeypatch):
    """A throwaway capauth home holding one real Ed25519 keypair.

    `sign_one`'s post-write self-check calls `skcapstone.fleet.signing
    .capauth_verifier()`, which resolves its trust roster from CAPAUTH_HOME
    (or the acting agent's own home) independently of the `home=` argument
    used for the private half, so CAPAUTH_HOME is pointed at the same
    throwaway home to keep the two halves of the round trip talking about
    the same key -- exactly the discipline `sign_plane_files.py`'s docstring
    describes doing deliberately (agent-blind, not acting-agent-first).
    """
    backend = get_backend()
    bundle = backend.generate_keypair(TEST_FPR_LABEL, "spf-test@skworld.io", "",
                                      Algorithm.ED25519)
    home = tmp_path / "capauth-home"
    (home / "identity").mkdir(parents=True)
    (home / "identity" / "public.asc").write_text(bundle.public_armor)
    (home / "identity" / "private.asc").write_text(bundle.private_armor)
    monkeypatch.setenv("CAPAUTH_HOME", str(home))
    return home, bundle.fingerprint


@pytest.fixture
def fleet_root(tmp_path):
    root = tmp_path / "fleet"
    (root / "objects").mkdir(parents=True)
    return root


def _write(root, name, data):
    (root / "objects" / name).write_text(json.dumps(data))


# --------------------------------------------------------------------------
# Refusals: every one of these is the EXPECTED outcome on a node that does
# not hold the operator's private key (today's actual state -- see the
# module docstring on why the live files stay unsigned by this PR).
# --------------------------------------------------------------------------

def test_refuses_missing_file(fleet_root, operator_home, capsys):
    home, fpr = operator_home
    ok = spf.sign_one(fleet_root / "objects" / "_freeze.json", home=home,
                      identity="capauth:chef@skworld.io", expect_fingerprint=fpr,
                      dry_run=False)
    assert ok is False
    assert "does not exist" in capsys.readouterr().err


def test_refuses_wrong_fingerprint(fleet_root, operator_home, capsys):
    home, _real_fpr = operator_home
    _write(fleet_root, "_freeze.json", {"frozen": False,
                                         "writer": {"identity": "x", "signature": None}})
    ok = spf.sign_one(fleet_root / "objects" / "_freeze.json", home=home,
                      identity="capauth:chef@skworld.io",
                      expect_fingerprint="0000000000000000000000000000000000000000",
                      dry_run=False)
    assert ok is False
    assert "does not match the expected operator fingerprint" in capsys.readouterr().err


def test_refuses_when_private_key_absent(fleet_root, tmp_path, operator_home, capsys):
    """The offline-custody case: public key present (so the fingerprint
    check can even run), private key deliberately not on this node."""
    home, fpr = operator_home
    (home / "identity" / "private.asc").unlink()
    _write(fleet_root, "_freeze.json", {"frozen": False,
                                         "writer": {"identity": "x", "signature": None}})
    ok = spf.sign_one(fleet_root / "objects" / "_freeze.json", home=home,
                      identity="capauth:chef@skworld.io", expect_fingerprint=fpr,
                      dry_run=False)
    assert ok is False
    err = capsys.readouterr().err
    assert "OFFLINE" in err or "private.asc" in err


def test_refuses_no_home_resolved(fleet_root, capsys):
    _write(fleet_root, "_freeze.json", {"frozen": False,
                                         "writer": {"identity": "x", "signature": None}})
    ok = spf.sign_one(fleet_root / "objects" / "_freeze.json", home=None,
                      identity="capauth:chef@skworld.io",
                      expect_fingerprint="AAAA",
                      dry_run=False)
    assert ok is False
    assert "could not resolve" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The success path: what happens when the operator DOES run this with their
# real key loaded.
# --------------------------------------------------------------------------

def test_signs_and_self_verifies_freeze_file(fleet_root, operator_home):
    from skcapstone.fleet import signing as fleet_signing

    home, fpr = operator_home
    _write(fleet_root, "_freeze.json", {
        "frozen": False, "reason": "", "updatedAt": "2026-07-29T23:13:44Z",
        "writer": {"identity": "capauth:lumina@skworld.io", "node": "cli",
                   "role": "operator", "signature": None},
    })
    path = fleet_root / "objects" / "_freeze.json"
    ok = spf.sign_one(path, home=home, identity="capauth:chef@skworld.io",
                      expect_fingerprint=fpr, dry_run=False)
    assert ok is True

    on_disk = json.loads(path.read_text())
    assert on_disk["frozen"] is False                       # content untouched
    assert on_disk["writer"]["identity"] == "capauth:chef@skworld.io"
    assert on_disk["writer"]["signature"]                    # not null anymore

    def _verifier(data: bytes, sig: str) -> bool:
        backend = get_backend()
        public = (home / "identity" / "public.asc").read_text()
        return backend.verify(data, sig, public)

    status, detail = fleet_signing.verify_payload(on_disk, _verifier)
    assert status == "verified", detail


def test_signs_and_migrates_legacy_protected_json(fleet_root, operator_home):
    from skcapstone.fleet import signing as fleet_signing

    home, fpr = operator_home
    _write(fleet_root, "_protected.json", {
        "version": 1, "note": "bootstrap manifest",
        "protected": ["*skharness/autocode/protected.py"],
        "signer": "chef", "signature": None,   # legacy top-level shape
    })
    path = fleet_root / "objects" / "_protected.json"
    ok = spf.sign_one(path, home=home, identity="capauth:chef@skworld.io",
                      expect_fingerprint=fpr, dry_run=False)
    assert ok is True

    on_disk = json.loads(path.read_text())
    assert "signer" not in on_disk and "signature" not in on_disk   # migrated away
    assert on_disk["protected"] == ["*skharness/autocode/protected.py"]
    assert on_disk["writer"]["identity"] == "capauth:chef@skworld.io"
    assert on_disk["writer"]["signature"]

    def _verifier(data: bytes, sig: str) -> bool:
        backend = get_backend()
        public = (home / "identity" / "public.asc").read_text()
        return backend.verify(data, sig, public)

    status, _detail = fleet_signing.verify_payload(on_disk, _verifier)
    assert status == "verified"


def test_dry_run_does_not_write(fleet_root, operator_home):
    home, fpr = operator_home
    _write(fleet_root, "_freeze.json", {"frozen": False,
                                         "writer": {"identity": "x", "signature": None}})
    path = fleet_root / "objects" / "_freeze.json"
    before = path.read_text()
    ok = spf.sign_one(path, home=home, identity="capauth:chef@skworld.io",
                      expect_fingerprint=fpr, dry_run=True)
    assert ok is True
    assert path.read_text() == before


def test_main_signs_both_plane_files(fleet_root, operator_home, monkeypatch):
    home, fpr = operator_home
    _write(fleet_root, "_freeze.json", {"frozen": False,
                                         "writer": {"identity": "x", "signature": None}})
    _write(fleet_root, "_protected.json", {"protected": ["*x.py"],
                                            "signer": "chef", "signature": None})
    rc = spf.main(["--root", str(fleet_root), "--capauth-home", str(home),
                  "--expect-fingerprint", fpr])
    assert rc == 0
    freeze = json.loads((fleet_root / "objects" / "_freeze.json").read_text())
    protected = json.loads((fleet_root / "objects" / "_protected.json").read_text())
    assert freeze["writer"]["signature"]
    assert protected["writer"]["signature"]

"""Tests for the skcode-hostd capauth verifier (R2.4).

Two things are proven here:

1. Flag wiring (``select_verifier``): with ``SKCODE_REAL_VERIFIER`` unset/off the
   daemon still gets the P0 deny-all placeholder (a token that WOULD be valid is
   still rejected, so the default is byte-identical to today); only when the flag
   is explicitly ON is the real capauth verifier constructed.

2. The real verifier (``build_capauth_verifier``): accepts a valid skcode-audience
   token and fails CLOSED on a wrong-audience token, an expired/garbage token, and
   an unscoped (legacy audience=None) token.

The gpg handling mirrors capauth's own audience tests: ``sign=False`` and, where a
signed token is required to pass the signature half, ``capauth.tokens.verify_token``
is stubbed so ONLY the audience gate is exercised. A tmp capauth home is injected
via ``tmp_path`` so no real keyring / FS-of-record is touched.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from skharness import serve
from skharness.serve import (
    REAL_VERIFIER_ENV,
    build_capauth_verifier,
    build_default_verifier,
    select_verifier,
)

from capauth.tokens import (
    export_token,
    issue_token,
    mint_audience_token,
)


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """A minimal capauth home with an identity (no gpg needed)."""
    home = tmp_path / ".skcapstone"
    identity_dir = home / "identity"
    identity_dir.mkdir(parents=True)
    (home / "security").mkdir(parents=True)
    identity = {
        "name": "TestAgent",
        "email": "test@skcapstone.local",
        "fingerprint": "AABBCCDDEE1122334455AABBCCDDEE1122334455",
        "capauth_managed": True,
    }
    (identity_dir / "identity.json").write_text(json.dumps(identity))
    return home


def _wire(token) -> str:
    """Encode a SignedToken the way a caller puts it on the wire: base64url of
    export_token(...)."""
    return base64.urlsafe_b64encode(export_token(token).encode("utf-8")).decode("ascii")


def _skcode_token(agent_home: Path):
    return mint_audience_token(
        home=agent_home,
        subject="chef-session",
        audience="skcode",
        scopes=["skcode.stream", "skcode.inject"],
        sign=False,
    )


# --------------------------------------------------------------------------- #
# Flag wiring: default stays deny-all, byte-identical to today.
# --------------------------------------------------------------------------- #

class TestFlagWiring:
    def test_default_off_selects_deny_all(self, monkeypatch, agent_home):
        """Unset flag -> deny-all: even a would-be-valid token is rejected."""
        monkeypatch.delenv(REAL_VERIFIER_ENV, raising=False)
        # A token that the REAL verifier would accept (stub the signature half).
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        wire = _wire(_skcode_token(agent_home))

        verifier = select_verifier()
        assert verifier(wire) is False
        # Byte-identical to the P0 placeholder: rejects everything.
        assert verifier("anything") is False
        assert verifier("") is False

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "  ", "bogus"])
    def test_non_truthy_values_stay_deny_all(self, monkeypatch, agent_home, val):
        monkeypatch.setenv(REAL_VERIFIER_ENV, val)
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        wire = _wire(_skcode_token(agent_home))
        assert select_verifier()(wire) is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "Yes", "on"])
    def test_flag_on_selects_capauth_verifier(self, monkeypatch, val):
        """Truthy flag -> the capauth builder is used (not deny-all)."""
        sentinel = object()
        monkeypatch.setenv(REAL_VERIFIER_ENV, val)
        monkeypatch.setattr(serve, "build_capauth_verifier", lambda: (lambda t: sentinel))
        chosen = select_verifier()
        assert chosen("whatever") is sentinel

    def test_default_verifier_is_still_deny_all(self):
        """The placeholder itself is unchanged."""
        v = build_default_verifier()
        assert v("anything") is False


# --------------------------------------------------------------------------- #
# The real capauth verifier: accept valid, fail closed on everything else.
# --------------------------------------------------------------------------- #

class TestCapauthVerifier:
    def test_accepts_valid_skcode_token(self, agent_home, monkeypatch):
        # Isolate the audience gate: stub the signature/validity half True.
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        wire = _wire(_skcode_token(agent_home))
        verifier = build_capauth_verifier(home=agent_home)
        assert verifier(wire) is True

    def test_rejects_wrong_audience(self, agent_home, monkeypatch):
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        token = mint_audience_token(
            home=agent_home,
            subject="chef-session",
            audience="skchat",   # wrong audience
            scopes=["chat.read"],
            sign=False,
        )
        verifier = build_capauth_verifier(home=agent_home)
        assert verifier(_wire(token)) is False

    def test_rejects_unscoped_token(self, agent_home, monkeypatch):
        """A legacy token with audience=None fails closed."""
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        token = issue_token(
            home=agent_home,
            subject="chef-session",
            capabilities=["skcode.stream"],
            sign=False,
        )
        assert token.payload.audience is None
        verifier = build_capauth_verifier(home=agent_home)
        assert verifier(_wire(token)) is False

    def test_rejects_unsigned_token_via_real_verify(self, agent_home):
        """Matching audience but no signature: the REAL verify_token (not stubbed)
        returns False, so the verifier fails closed."""
        wire = _wire(_skcode_token(agent_home))   # sign=False -> no signature
        verifier = build_capauth_verifier(home=agent_home)
        assert verifier(wire) is False

    @pytest.mark.parametrize(
        "garbage",
        [
            "",
            "   ",
            "not base64 !!!",
            "@@@@",
            base64.urlsafe_b64encode(b"not json at all").decode("ascii"),
            base64.urlsafe_b64encode(b'{"not":"a token"}').decode("ascii"),
            base64.urlsafe_b64encode(b'{"skcapstone_token":"1.0"}').decode("ascii"),
        ],
    )
    def test_fails_closed_on_garbage(self, agent_home, garbage):
        verifier = build_capauth_verifier(home=agent_home)
        assert verifier(garbage) is False

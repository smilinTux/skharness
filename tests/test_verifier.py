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

# capauth is an optional sibling, not a declared skharness dependency (same policy
# as the needs_skcapstone hook in conftest.py). A module-level `from capauth...`
# turned its absence into a COLLECTION error, which took the whole suite down in
# any clean environment. Skip this module instead.
_tokens = pytest.importorskip("capauth.tokens")
export_token = _tokens.export_token
issue_token = _tokens.issue_token
mint_audience_token = _tokens.mint_audience_token

from skharness import serve  # noqa: E402  (must follow the optional-sibling guard)
from skharness.serve import (  # noqa: E402  (must follow the optional-sibling guard)
    FORCE_DENY_ENV,
    REAL_VERIFIER_ENV,
    build_capauth_verifier,
    build_default_verifier,
    select_verifier,
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
# Flag wiring (CR-3.2): real capauth verification by DEFAULT, deny-all fallback.
# --------------------------------------------------------------------------- #

class TestFlagWiring:
    def test_default_selects_real_capauth_verifier(self, monkeypatch, agent_home):
        """Unset flags -> the REAL capauth verifier is used (CR-3.2 flip).

        A would-be-valid token is now ACCEPTED (returns a scope-carrying
        AuthContext), proving the daemon no longer defaults to deny-all.
        """
        monkeypatch.delenv(REAL_VERIFIER_ENV, raising=False)
        monkeypatch.delenv(FORCE_DENY_ENV, raising=False)
        # Point the real verifier at the tmp capauth home and isolate the
        # audience gate (stub the signature/validity half True).
        monkeypatch.setattr(serve, "build_capauth_verifier",
                            lambda: build_capauth_verifier(home=agent_home))
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        wire = _wire(_skcode_token(agent_home))

        verifier = select_verifier()
        result = verifier(wire)
        assert result                                   # accepted (not deny-all)
        assert result.has_scope("skcode.stream") is True
        # Still fails closed on bad input.
        assert verifier("anything") is False
        assert verifier("") is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "Yes", "on"])
    def test_force_deny_all_selects_deny_all(self, monkeypatch, agent_home, val):
        """SKCODE_FORCE_DENY_ALL truthy -> deny-all, even a valid token rejected."""
        monkeypatch.setenv(FORCE_DENY_ENV, val)
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        wire = _wire(_skcode_token(agent_home))
        verifier = select_verifier()
        assert verifier(wire) is False
        assert verifier("anything") is False

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "  ", "bogus"])
    def test_non_truthy_force_deny_stays_real(self, monkeypatch, agent_home, val):
        """A non-truthy SKCODE_FORCE_DENY_ALL does NOT force deny-all."""
        monkeypatch.setenv(FORCE_DENY_ENV, val)
        sentinel = object()
        monkeypatch.setattr(serve, "build_capauth_verifier", lambda: (lambda t: sentinel))
        assert select_verifier()("whatever") is sentinel

    def test_capauth_unreachable_falls_back_to_deny_all(self, monkeypatch, agent_home):
        """FAIL-CLOSED (a): capauth import/construction fails -> deny-all fallback.

        When ``build_capauth_verifier`` cannot be constructed (capauth
        unreachable), ``select_verifier`` must fall back to deny-all rather than
        crash or fail open. Even a would-be-valid token is rejected.
        """
        monkeypatch.delenv(FORCE_DENY_ENV, raising=False)

        def _boom():
            raise ImportError("capauth unreachable")

        monkeypatch.setattr(serve, "build_capauth_verifier", _boom)
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        wire = _wire(_skcode_token(agent_home))
        verifier = select_verifier()
        assert verifier(wire) is False
        assert verifier("anything") is False

    def test_default_verifier_is_still_deny_all(self):
        """The placeholder itself is unchanged (the fallback still denies all)."""
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
        # The verifier now returns a scope-carrying AuthContext (truthy), NOT a
        # bare True, so routes can split read from write. _skcode_token grants
        # both scopes, so both are present.
        result = verifier(wire)
        assert result                                   # truthy -> caller accepted
        assert result.has_scope("skcode.stream") is True
        assert result.has_scope("skcode.inject") is True

    def test_verified_context_carries_only_granted_scopes(self, agent_home, monkeypatch):
        # A read-only token: verified (accepted) but WITHOUT the write scope, so
        # enabling the verifier grants view without arming keystroke-inject.
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        token = mint_audience_token(
            home=agent_home,
            subject="chef-session",
            audience="skcode",
            scopes=["skcode.stream"],   # read only
            sign=False,
        )
        verifier = build_capauth_verifier(home=agent_home)
        result = verifier(_wire(token))
        assert result
        assert result.has_scope("skcode.stream") is True
        assert result.has_scope("skcode.inject") is False

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


# --------------------------------------------------------------------------- #
# CR-3.2 fail-closed contract, explicit: the RCE gate DENIES on every failure
# mode and ALLOWS only a valid token. This is the security-critical guarantee.
# --------------------------------------------------------------------------- #

class TestFailClosed:
    def test_a_capauth_unreachable_verify_raises_denies(self, agent_home, monkeypatch):
        """(a) capauth unreachable: the verify call itself raises -> DENY.

        Even a well-formed, correct-audience token is rejected when the capauth
        verify path errors (keyring down, backend unreachable). The verifier
        swallows the exception and fails closed; it never lets the caller in.
        """
        def _boom(*a, **k):
            raise RuntimeError("capauth backend unreachable")

        # verify_audience_token calls capauth.tokens.verify_token internally for a
        # correct-audience token; make that raise to simulate the backend being
        # unreachable. The verifier must swallow it and DENY.
        monkeypatch.setattr("capauth.tokens.verify_token", _boom)
        wire = _wire(_skcode_token(agent_home))   # correct skcode audience
        verifier = build_capauth_verifier(home=agent_home)
        assert verifier(wire) is False

    def test_b_missing_token_denies(self, agent_home):
        """(b) missing/empty token -> DENY."""
        verifier = build_capauth_verifier(home=agent_home)
        assert verifier("") is False
        assert verifier("   ") is False
        assert verifier(None) is False

    def test_c_invalid_token_denies(self, agent_home):
        """(c) invalid token (garbage bearer) -> DENY."""
        verifier = build_capauth_verifier(home=agent_home)
        assert verifier("totally-not-a-token") is False
        assert verifier(
            base64.urlsafe_b64encode(b'{"not":"a token"}').decode("ascii")
        ) is False

    def test_d_valid_token_allows(self, agent_home, monkeypatch):
        """The one accept path: a valid, correct-audience token -> ALLOW."""
        # Isolate the audience gate (stub the signature/validity half True).
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        wire = _wire(_skcode_token(agent_home))
        verifier = build_capauth_verifier(home=agent_home)
        result = verifier(wire)
        assert result
        assert result.has_scope("skcode.stream") is True
        assert result.has_scope("skcode.inject") is True

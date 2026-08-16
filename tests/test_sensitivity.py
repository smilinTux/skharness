"""Deterministic sensitivity classifier: the DATA EXPOSURE axis of the work grade.

These tests assert the REFUSALS, not just the happy path. A classifier on this
axis is a security control, so the cases that matter are the ones where it must
not say `public`, must not be relaxed by a small diff, and must not silently
swallow a malformed human override.
"""

import pytest

from skharness.autocode.grading import SENSITIVITY_VALUES
from skharness.autocode.sensitivity import (
    OVERRIDE_KEY,
    SensitivityOverrideError,
    classify_sensitivity,
)


def _card(**fields):
    card = {"id": "t-1", "title": "", "description": "", "tags": [],
            "acceptance_criteria": []}
    card.update(fields)
    return card


# --------------------------------------------------------------------------
# contract
# --------------------------------------------------------------------------

def test_returns_vocabulary_value_and_nonempty_reasons():
    for card in (_card(title="tidy the changelog"),
                 _card(title="rotate the capauth signing key"),
                 _card()):
        value, reasons = classify_sensitivity(card)
        assert value in SENSITIVITY_VALUES
        assert reasons and all(isinstance(r, str) and r.strip() for r in reasons)


def test_deterministic_across_calls():
    card = _card(title="wire skvault unlock into skingest", tags=["repo:skingest"])
    first = classify_sensitivity(card)
    second = classify_sensitivity(card)
    assert first == second


def test_no_model_call_is_possible(monkeypatch):
    """The module must not reach a network or a subprocess to answer.

    Poisoning both escape hatches proves the answer is computed from the card
    text alone, which is what makes two runs of the same card route identically.
    """
    import socket
    import subprocess

    def _boom(*a, **kw):
        raise AssertionError("sensitivity classification must not call out")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    value, reasons = classify_sensitivity(_card(title="add a retry to the poller"))
    assert value == "internal" and reasons


# --------------------------------------------------------------------------
# NEGATIVE CONTROLS: a stub that always answers must fail these
# --------------------------------------------------------------------------

def test_ordinary_card_is_internal_not_public():
    """Fails against a stub that returns `public`.

    `public` asserts the payload could be posted publicly. No keyword match can
    support that claim, so the rules never infer it.
    """
    value, reasons = classify_sensitivity(
        _card(title="bump the ruff pin", description="ruff>=0.1 drifted the gate",
              tags=["repo:skharness"]))
    assert value == "internal"
    assert value != "public"
    assert any("never inferred" in r for r in reasons), reasons


@pytest.mark.parametrize("card", [
    _card(title="add a CHANGELOG entry"),
    _card(title="fix a typo in the README", tags=["repo:skos"]),
    _card(title="widen a test assertion", description="assert the list has 3 items"),
    _card(title="rename a local variable in the poller"),
    _card(title="drop a dead import", tags=["repo:skchat"]),
])
def test_rules_never_infer_public(card):
    """Sweep of benign cards: not one of them may come back `public`.

    A stub returning `public` fails every row here. A stub returning `secret`
    fails test_ordinary_card_is_internal_not_public above, so the pair pins the
    classifier from both sides.
    """
    value, _ = classify_sensitivity(card)
    assert value != "public"


def test_capauth_card_is_secret_even_when_the_change_is_trivial():
    """Size does not relax sensitivity.

    Fails against a stub returning `public` or `internal`. A one-character
    change to capauth still puts the repo's key handling in front of the model.
    """
    value, reasons = classify_sensitivity(_card(
        title="fix a typo in a capauth docstring",
        description="one word, no behaviour change at all",
        tags=["repo:capauth"]))
    assert value == "secret"
    assert any("capauth" in r for r in reasons)


def test_skvault_card_is_secret_even_when_the_change_is_trivial():
    value, reasons = classify_sensitivity(_card(
        title="reflow a comment", description="whitespace only", repo="skvault"))
    assert value == "secret"
    assert any("skvault" in r for r in reasons)


# --------------------------------------------------------------------------
# secret vocabulary
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "regenerate the operator revocation cert",
    "the KeePass database will not open",
    "reseed the TOTP entry for the bot",
    "stop logging the bearer token on 401",
    "the ~/.ssh/config on the builder is wrong",
    "import the PGP public keyring",
    "unwrap the Shamir shares",
    "the private key never made it to the node",
    "read the API key from the environment file",
    "the legal/medical corpus query returns nothing",
    "scrub the client_secret out of the history",
    "an app password would work here",
    "prekey bundles are not fanned out to new devices",
    "ML-DSA hybrid leg on issue_token",
    "PHI must never reach a remote provider",
    "the soul blueprint is loaded twice",
])
def test_credential_vocabulary_classifies_secret(text):
    value, reasons = classify_sensitivity(_card(title=text))
    assert value == "secret", reasons
    assert reasons


@pytest.mark.parametrize("pasted", [
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaA\n-----END",
    "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF\n",
    "creds are AKIAIOSFODNN7EXAMPLE for now",
    "token ghp_0123456789abcdefghijABCDEFGHIJ0123",
    "header: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27u",
])
def test_pasted_credential_literal_classifies_secret(pasted):
    """A card that QUOTES key material is secret whatever the work is about."""
    value, reasons = classify_sensitivity(
        _card(title="update the deploy notes", description=pasted))
    assert value == "secret", reasons
    assert any("pasted" in r for r in reasons)


def test_secret_signal_in_acceptance_criteria_is_seen():
    """The rules read every documented field, not only the title."""
    value, _ = classify_sensitivity(_card(
        title="finish the migration",
        acceptance_criteria=["the ssh key is rotated on every node"]))
    assert value == "secret"


# --------------------------------------------------------------------------
# fail closed
# --------------------------------------------------------------------------

def test_ambiguous_signal_fails_closed_to_secret_with_a_stated_reason():
    value, reasons = classify_sensitivity(_card(
        title="document the keystore layout", description="where things live"))
    assert value == "secret"
    assert any("stricter" in r for r in reasons), reasons


def test_empty_card_fails_closed_to_secret():
    value, reasons = classify_sensitivity({})
    assert value == "secret"
    assert any("no readable text" in r for r in reasons)


def test_non_dict_input_fails_closed_to_secret_without_raising():
    for bad in (None, "a card", 7, ["t-1"]):
        value, reasons = classify_sensitivity(bad)
        assert value == "secret"
        assert reasons


# --------------------------------------------------------------------------
# human override
# --------------------------------------------------------------------------

def test_override_public_wins_over_the_internal_default():
    value, reasons = classify_sensitivity(_card(
        title="publish the release notes", meta={OVERRIDE_KEY: "public"}))
    assert value == "public"
    assert any("override" in r for r in reasons)


def test_override_secret_wins_over_an_otherwise_internal_card():
    value, _ = classify_sensitivity(_card(
        title="tidy the changelog", meta={OVERRIDE_KEY: "secret"}))
    assert value == "secret"


def test_override_outranks_the_secret_rules():
    """An override is a deliberate, attributable human act, so it wins outright."""
    value, reasons = classify_sensitivity(_card(
        title="link to the public capauth README", tags=["repo:capauth"],
        meta={OVERRIDE_KEY: "public"}))
    assert value == "public"
    assert any("override" in r for r in reasons)


@pytest.mark.parametrize("bad", ["publik", "PUBLIC-ish", "high", "S", "", "  ", 3, True])
def test_invalid_override_raises_rather_than_falling_back(bad):
    """A typo'd override must be an ERROR.

    Falling back to the rules would leave a control that reports success while
    doing nothing, and the operator would never learn their marking was ignored.
    """
    with pytest.raises(SensitivityOverrideError):
        classify_sensitivity(_card(title="anything", meta={OVERRIDE_KEY: bad}))


def test_override_is_case_and_whitespace_tolerant():
    value, _ = classify_sensitivity(_card(
        title="tidy", meta={OVERRIDE_KEY: "  SECRET "}))
    assert value == "secret"


def test_absent_or_null_override_falls_through_to_the_rules():
    assert classify_sensitivity(_card(title="tidy", meta={}))[0] == "internal"
    assert classify_sensitivity(_card(title="tidy", meta={OVERRIDE_KEY: None}))[0] == "internal"
    assert classify_sensitivity(_card(title="tidy", meta="not-a-dict"))[0] == "internal"


def test_override_key_is_the_single_documented_location():
    """A near-miss key is NOT read; only meta.sensitivity_override counts."""
    value, _ = classify_sensitivity(_card(
        title="tidy the changelog", meta={"sensitivity": "public"}))
    assert value == "internal"

"""S14 (card 6ad3c9ab): the routing layer can never touch its own guardrails.

Scope is ROUTING only (buckets.py + adapters/base.py): which adapter, model
and effort tier attempts a card. Whatever chooses that must be structurally
incapable of changing whether the twin gate runs, whether CI must be green,
what the acceptance criteria are, or whether a human ratifies.

These are deliberately NEGATIVE-CONTROL heavy, following
test_autocode_buckets.py:118's pattern: a property that merely asserts the
guard exists proves nothing, so every property below is also run once
against a PERMISSIVE STUB and proven to fail. If it did not fail there, it
would not be measuring anything here either.
"""
from __future__ import annotations

from unittest import mock

import pytest

from skharness.autocode import buckets
from skharness.autocode.routing_guard import (
    _GATE_DECISION_FIELDS,
    _ROUTING_ONLY_FIELDS,
    RoutingScopeViolation,
    assert_routing_field,
)

_MISSING = object()   # sentinel: "this attribute did not exist before the attempt"


def _write_via_routing_layer(guard, obj, field, value):
    """Mimic attach_dispatch_model's write path with an INJECTED guard, so
    the property below can be run against the real guard and, as a negative
    control, against a permissive stub that never refuses anything.
    """
    guard(field)
    setattr(obj, field, value)


def _assert_gate_fields_are_refused(guard):
    """The property under test: for every gate-decision field, a routing
    write attempt is refused BEFORE any mutation happens. Factored out so it
    can be run against a permissive stub as a negative control on this test
    itself (test_the_gate_field_refusal_fails_against_a_permissive_stub).
    """
    for field in sorted(_GATE_DECISION_FIELDS):
        class Target:
            pass

        target = Target()
        before = getattr(target, field, _MISSING)
        # A plain try/except here, not pytest.raises: pytest.raises' own
        # failure exception (raised when nothing was raised) is NOT an
        # AssertionError in this pytest version, so it would slip straight
        # through the permissive-stub test's `pytest.raises(AssertionError)`
        # below instead of being caught by it. A manual check produces a
        # real AssertionError either way, which is what that outer test
        # needs to be able to catch.
        raised = False
        try:
            _write_via_routing_layer(guard, target, field, "sk-xl-public")
        except RoutingScopeViolation:
            raised = True
        assert raised, f"routing was allowed to write {field!r}: the guard did not refuse it"
        after = getattr(target, field, _MISSING)
        assert after == before, (
            f"routing was able to set {field!r} despite the guard raising "
            f"(before={before!r}, after={after!r})")


# -- the property itself, against the REAL guard ---------------------------------

def test_routing_layer_cannot_touch_any_gate_decision_field():
    _assert_gate_fields_are_refused(assert_routing_field)


def test_routing_field_allowlist_is_exactly_model():
    # The allowlist is the entire universe of what routing may ever write.
    # If this ever grows, it must grow on purpose, not by accretion.
    assert _ROUTING_ONLY_FIELDS == frozenset({"model"})


def test_the_routing_only_field_is_not_itself_a_gate_decision_field():
    # Sanity: the one field routing IS allowed to write must never collide
    # with the set of fields it is forbidden from writing. If it ever did,
    # the allowlist and the denylist would disagree about "model" itself.
    assert not (_ROUTING_ONLY_FIELDS & _GATE_DECISION_FIELDS)


def test_legitimate_routing_field_is_accepted():
    assert assert_routing_field("model") == "model"


# -- NEGATIVE CONTROL: the property fails against a permissive stub --------------

def test_the_gate_field_refusal_fails_against_a_permissive_stub():
    def permissive(name):          # never refuses anything: exactly the bug
        return name                # this guard exists to make impossible.

    with pytest.raises(AssertionError):
        _assert_gate_fields_are_refused(permissive)


# -- the REAL write path, not just the guard function in isolation ---------------
#
# A guard function that is correct but never called proves nothing about the
# production code. These monkeypatch buckets.DISPATCH_MODEL_ATTR -- the live
# constant attach_dispatch_model consults -- to a gate-decision field name,
# simulating exactly the failure this module exists to prevent: the routing
# constant itself getting compromised (bad merge, typo, tampering) so that
# the SAME call site that is supposed to write "model" writes something else.

@pytest.mark.parametrize("field", sorted(_GATE_DECISION_FIELDS))
def test_attach_dispatch_model_refuses_when_the_routing_constant_is_compromised(field):
    class Brief:
        pass

    b = Brief()
    before = getattr(b, field, _MISSING)
    with mock.patch.object(buckets, "DISPATCH_MODEL_ATTR", field):
        with pytest.raises(RoutingScopeViolation):
            buckets.attach_dispatch_model(b, "sk-xl-public")
    after = getattr(b, field, _MISSING)
    assert after == before, f"{field} was mutated via the compromised routing constant"


def test_attach_dispatch_model_still_works_for_the_real_routing_field():
    class Brief:
        pass

    b = Brief()
    buckets.attach_dispatch_model(b, "sk-m-internal")
    assert b.model == "sk-m-internal"


# -- NEGATIVE CONTROL: no ambient environment variable widens the guard ----------

def test_no_env_var_can_widen_the_guard(monkeypatch):
    # The guard consults no ambient environment variable for its allowlist.
    # Set the sort of variable an operator might plausibly export (and that
    # SKFLEET_ROOT-style tooling elsewhere in this fleet DOES respect) and
    # confirm it has exactly zero effect on what routing may touch.
    monkeypatch.setenv("SKHARNESS_ROUTING_ALLOWED_FIELDS", "score,ci_status,acceptance")
    monkeypatch.setenv("SKHARNESS_ROUTING_ALLOW_ALL", "1")
    monkeypatch.setenv("SKFLEET_ROOT", "/tmp/whatever")
    for field in ("score", "ci_status", "acceptance", "ratified"):
        with pytest.raises(RoutingScopeViolation):
            assert_routing_field(field)
    # and the allowlist itself is unmoved
    assert assert_routing_field("model") == "model"

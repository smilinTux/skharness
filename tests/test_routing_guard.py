"""S14 (card 6ad3c9ab): the routing layer can never touch its own guardrails.

Scope is ROUTING only (buckets.py + adapters/base.py): which adapter, model
and effort tier attempts a card. Whatever chooses that must be structurally
incapable of changing whether the twin gate runs, whether CI must be green,
what the acceptance criteria are, or whether a human ratifies.

HONESTY NOTE (fix round 1, adversarial review): this module has six test
FUNCTIONS, but only five of them are independent distinguishing observations,
and one of those five is itself a single mechanism run over a list of field
names, not five-or-twenty-one separate findings:

  1. test_the_guard_function_itself_refuses_every_gate_decision_field --
     the guard function in isolation, against a permissive-stub negative
     control (test 2). ONE mechanism (assert_routing_field), looped over
     every name in _GATE_DECISION_FIELDS so no single name is special-cased,
     but a failure on "score" and a failure on "ci_status" are the same code
     path taking a different string, not two different discoveries.
  2. test_the_gate_field_refusal_fails_against_a_permissive_stub -- proves
     observation 1 has a real failure mode (the permissive stub is used
     precisely so the property is not indistinguishable from a placebo).
  3. test_attach_dispatch_model_refuses_when_the_routing_constant_is_compromised
     -- the REAL production write path (not the guard in isolation),
     parametrized over every _GATE_DECISION_FIELDS name. Also ONE mechanism
     counted once per field name: 21 pytest cases, one finding.
  4. test_attach_dispatch_model_uses_the_checked_value_not_a_fresh_read --
     the fix-round-1 regression test for the check-then-read bug the
     reviewer found at buckets.py:210-211.
  5. test_the_guard_reads_no_environment_variable_at_all -- the general
     "no ambient env var can widen this" property, not three guessed names.

test_smoke_attach_dispatch_model_writes_model_for_the_real_routing_field is
explicitly a SMOKE test: it passes whether or not the guard exists at all
(it only proves the guard's addition did not break the happy path), so it is
not counted above and must not be read as guard coverage.
"""
from __future__ import annotations

import inspect
import re
from unittest import mock

import pytest

from skharness.autocode import buckets
from skharness.autocode import routing_guard as routing_guard_module
from skharness.autocode.routing_guard import (
    _GATE_DECISION_FIELDS,
    RoutingScopeViolation,
    assert_routing_field,
)

_MISSING = object()   # sentinel: "this attribute did not exist before the attempt"


def _write_via_injected_guard(guard, obj, field, value):
    """Mimic attach_dispatch_model's write path with an INJECTED guard
    function, exercising ONLY assert_routing_field's own logic, not the
    production call site (buckets.attach_dispatch_model; see
    test_attach_dispatch_model_refuses_when_the_routing_constant_is_compromised
    for that). Used to run the guard-function property against both the
    real guard and, as a negative control, a permissive stub.
    """
    guard(field)
    setattr(obj, field, value)


def _assert_the_guard_function_refuses_every_gate_field(guard):
    """The property under test: for every gate-decision field, the guard
    FUNCTION (not the production write path) refuses before any mutation
    happens. Factored out so it can be run against a permissive stub as a
    negative control on this test itself
    (test_the_gate_field_refusal_fails_against_a_permissive_stub).
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
            _write_via_injected_guard(guard, target, field, "sk-xl-public")
        except RoutingScopeViolation:
            raised = True
        assert raised, f"routing was allowed to write {field!r}: the guard did not refuse it"
        after = getattr(target, field, _MISSING)
        assert after == before, (
            f"routing was able to set {field!r} despite the guard raising "
            f"(before={before!r}, after={after!r})")


# -- observation 1: the guard FUNCTION, against the REAL guard -------------------

def test_the_guard_function_itself_refuses_every_gate_decision_field():
    _assert_the_guard_function_refuses_every_gate_field(assert_routing_field)


# -- observation 2: NEGATIVE CONTROL, the property fails against a permissive stub

def test_the_gate_field_refusal_fails_against_a_permissive_stub():
    def permissive(name):          # never refuses anything: exactly the bug
        return name                # this guard exists to make impossible.

    with pytest.raises(AssertionError):
        _assert_the_guard_function_refuses_every_gate_field(permissive)


# -- observation 3: the REAL write path, not just the guard function in isolation
#
# A guard function that is correct but never called proves nothing about the
# production code. These monkeypatch buckets.DISPATCH_MODEL_ATTR -- the live
# constant attach_dispatch_model consults -- to a gate-decision field name,
# simulating exactly the failure this module exists to prevent: the routing
# constant itself getting compromised (bad merge, typo, tampering) so that
# the SAME call site that is supposed to write "model" writes something else.
# ONE mechanism, parametrized over every field name; 21 pytest cases below
# are one finding, not 21.

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


# -- SMOKE test only: proves the guard's addition did not break the happy path.
# Note this PASSES with the guard removed entirely -- it has no failure mode
# that distinguishes a working guard from a missing one, so it is not one of
# the module's distinguishing observations. Kept as an ordinary regression
# check, not evidence of enforcement.

def test_smoke_attach_dispatch_model_writes_model_for_the_real_routing_field():
    class Brief:
        pass

    b = Brief()
    buckets.attach_dispatch_model(b, "sk-m-internal")
    assert b.model == "sk-m-internal"


# -- observation 4: check-then-read regression (fix round 1) ---------------------
#
# buckets.attach_dispatch_model used to do:
#     assert_routing_field(DISPATCH_MODEL_ATTR)
#     setattr(brief, DISPATCH_MODEL_ATTR, model_id)
# i.e. check-then-READ on the very global this guard exists to distrust
# (routing_guard.py's own docstring names "an attacker with module-level
# write access" as part of the threat model). Cards run under a
# ThreadPoolExecutor (orchestrator.py:836), so a mutation of
# DISPATCH_MODEL_ATTR from one thread is visible to another mid-call: the
# check could see "model" and the write could land on whatever the global
# was mutated to a moment later. The fix binds the checked value once
# (`field = assert_routing_field(...)`) and writes only that binding.
#
# This test fails against the check-then-read form: it lets the real check
# pass on the legitimate name, then mutates DISPATCH_MODEL_ATTR to a
# gate-decision field AS A SIDE EFFECT of that check returning, simulating a
# concurrent write landing between the check and the setattr. Verified by
# hand against the pre-fix code before this test was added: it wrote
# brief.score instead of brief.model.

def test_attach_dispatch_model_uses_the_checked_value_not_a_fresh_read(monkeypatch):
    real_assert = routing_guard_module.assert_routing_field

    def racing_assert(name):
        validated = real_assert(name)     # validates "model" for real
        # Simulate a concurrent mutation landing right after the check reads
        # the (still legitimate) value, before the write happens.
        monkeypatch.setattr(buckets, "DISPATCH_MODEL_ATTR", "score", raising=False)
        return validated

    monkeypatch.setattr(buckets, "assert_routing_field", racing_assert)

    class Brief:
        pass

    b = Brief()
    buckets.attach_dispatch_model(b, "sk-m-internal")
    assert getattr(b, "score", _MISSING) is _MISSING, (
        "check-then-read: the write re-read the mutated DISPATCH_MODEL_ATTR and "
        "landed on the twin gate's score field instead of the name that was "
        "actually validated")
    assert b.model == "sk-m-internal"


# -- observation 5: NEGATIVE CONTROL, no ambient environment variable, general ---
#
# Not three guessed names: a guard that consulted environment variable #4
# (one nobody thought to check) would still pass a test that only tries
# SKHARNESS_ROUTING_ALLOWED_FIELDS / SKHARNESS_ROUTING_ALLOW_ALL / SKFLEET_ROOT.
# Assert the general property instead -- the module contains no
# environment-reading call at all -- which holds regardless of what name
# anyone ever picks.

def test_the_guard_reads_no_environment_variable_at_all():
    src = inspect.getsource(routing_guard_module)
    # Word-boundary regex, not a bare substring: the module's own docstrings
    # legitimately say "environment variable" in prose, and a naive
    # "environ" in src` check false-positives on that ("environ" + "ment").
    # \benviron\b does not match inside "environment" (no boundary between
    # the shared "n" and "m"), so this only catches an actual os.environ /
    # "environ" identifier reference, which is what would matter.
    assert not re.search(r"\benviron\b", src), "routing_guard.py must never read os.environ"
    assert not re.search(r"\bgetenv\b", src), "routing_guard.py must never read an env var by name"
    # and the allowlist itself still behaves correctly
    assert assert_routing_field("model") == "model"

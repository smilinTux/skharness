"""Structural fence: the routing layer's write primitive may NEVER touch a
gate-decision field.

Card 6ad3c9ab (S14): whatever chooses ROUTING (which adapter, model, and
effort tier attempts a card) must be structurally incapable of changing
whether the twin gate runs, whether CI must be green, what the acceptance
criteria are, or whether a human ratifies.

Those four things are decided by fields living on GateResult, GradeBrief and
TaskBrief (see types.py and engineering.py's twin_gate_passed). The routing
layer's ONLY write into either brief is buckets.attach_dispatch_model's
``setattr(brief, DISPATCH_MODEL_ATTR, ...)``. That call is generic: it will
happily setattr ANY name it is given. Nothing about it stops
DISPATCH_MODEL_ATTR from ever being (by refactor, typo, or compromise)
"acceptance" or "ci_status" or "score" instead of "model" -- and if it were,
this exact routing path would silently overwrite the twin gate's own inputs
with a bucket id string, and nothing downstream would notice because the
write looks like every other legitimate one.

This module is the fence that makes that structurally impossible rather than
merely unlikely. Three properties, deliberately copied from
skcapstone.fleet.drill.UnsafeDrillRootError and this package's own
protected.py:

  - RAISES a typed error (RoutingScopeViolation) instead of returning a
    falsy value a caller could ignore, or a bool a caller could forget to
    check. See UnsafeDrillRootError's docstring: "Always structural, never
    advisory."
  - RE-RUNS on every call. The allowlist is not consulted once and cached; a
    routing write re-checks the attribute name every single time it happens,
    so a DISPATCH_MODEL_ATTR mutated after import (by a bad merge, a typo, or
    an attacker with module-level write access) is still caught on its very
    next use rather than trusted forever from a check made once at import.
    Same reasoning as drill.require_owned_root re-resolving the root on every
    call instead of trusting a value captured at construction.
  - Consults NO ambient environment variable, ever. Both field sets below are
    hardcoded frozensets. An env var here would be exactly the mechanism by
    which an implicit, unreviewed default widens what routing may touch --
    the same reasoning drill.py gives for never reading SKFLEET_ROOT: "an
    implicit default is how you delete the wrong tree."

The allowlist, not the denylist, is authoritative: `_ROUTING_ONLY_FIELDS` is
the entire universe of attribute names routing may ever set, and anything
not in it is refused, full stop -- exactly the fail-closed shape of
protected.py's `_FAIL_CLOSED` manifest (unrecognised input protects
everything, rather than an unrecognised input being let through).
`_GATE_DECISION_FIELDS` is carried alongside it only so the refusal message
can say WHY: a field routing tried to reach is named in the error rather
than the caller getting a bare "not allowed".
"""
from __future__ import annotations

#: The entire set of attribute names the routing layer's write primitive
#: (buckets.attach_dispatch_model) may ever set on a brief. One name: the
#: routing choice itself. Nothing else is ever legitimate, so nothing else is
#: ever allowed, regardless of what DISPATCH_MODEL_ATTR happens to say at
#: call time. Named by hand rather than derived from anything else in this
#: package, so nothing in engineering.py, types.py or buckets.py itself can
#: widen it by construction.
_ROUTING_ONLY_FIELDS: frozenset[str] = frozenset({"model"})

#: Fields that decide whether the twin gate passes (GateResult: score,
#: passed, notes, artifact, mode; the ci_status/diff_coverage that
#: twin_gate_passed reads off GradeBrief), what the acceptance criteria are
#: (GradeBrief.acceptance / TaskBrief.acceptance / an assess Verdict's
#: updated_acceptance), and whether a human ratifies (the escalate()/ratify()
#: path -- never itself a brief attribute today, named here anyway as
#: defense in depth against a future field of that name landing in the same
#: namespace). Listed explicitly, by hand, rather than derived from
#: `dataclasses.fields(GateResult)` et al: deriving the fence from the same
#: dataclasses it fences would make the fence only as wide as whatever those
#: dataclasses happen to declare today, and both types.py and engineering.py
#: are outside this card's scope to lock down structurally. A hand-kept list
#: can be wider than strictly necessary; it can never silently shrink when
#: someone adds a field elsewhere.
_GATE_DECISION_FIELDS: frozenset[str] = frozenset({
    "score", "passed", "notes", "artifact", "mode",              # GateResult
    "ci_status", "diff_coverage",                                 # CI requirement
    "acceptance", "acceptance_criteria", "updated_acceptance",    # acceptance criteria
    "verdict", "reason", "updated_description", "subtasks",       # assess Verdict
    "ratified", "ratification", "ratify",                         # human ratification
    "escalate", "escalated", "human_review", "automerge",         # ratification / merge policy
})


class RoutingScopeViolation(RuntimeError):
    """The routing layer tried to write a gate-decision field. Always
    structural, never advisory.

    Raised by :func:`assert_routing_field` for any attribute name outside
    `_ROUTING_ONLY_FIELDS`. There is no code path anywhere in this package
    that catches this exception and proceeds; it is meant to blow up the
    call stack exactly like `UnsafeDrillRootError` does, not to be logged
    and swallowed.
    """


def assert_routing_field(name: object) -> str:
    """Refuse any attribute name the routing layer has no business writing.

    Re-run this on every attempted write; never cache its result and never
    trust a name validated earlier in the call stack. `attach_dispatch_model`
    calls it immediately before its `setattr`, using the LIVE value of
    `DISPATCH_MODEL_ATTR` at call time, so a constant mutated after import is
    still caught on its very next use rather than trusted forever from the
    first check.

    Args:
        name: the attribute name about to be set.

    Returns:
        `name` unchanged, so a call site can wrap its write inline.

    Raises:
        RoutingScopeViolation: `name` is not in the routing-only allowlist.
            The message says whether it collides with a known gate-decision
            field, so a refusal names WHY rather than refusing silently.
    """
    if isinstance(name, str) and name in _ROUTING_ONLY_FIELDS:
        return name
    gate_hit = isinstance(name, str) and name in _GATE_DECISION_FIELDS
    raise RoutingScopeViolation(
        f"routing refused to set {name!r}: only {sorted(_ROUTING_ONLY_FIELDS)} "
        "may ever be written by the routing layer (which adapter, model, and "
        "effort tier attempts a card). " +
        (f"{name!r} is a gate-decision field (twin gate / CI requirement / "
         "acceptance criteria / human ratification) and must never be "
         "reachable from routing." if gate_hit else
         f"{name!r} is not a recognised routing field either.")
    )

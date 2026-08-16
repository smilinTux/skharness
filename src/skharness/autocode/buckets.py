"""Grade to skgateway BUCKET address: the sending half of graded model selection.

A card's work grade (produced upstream, see grading.py) carries a precomputed
``model_class`` plus a ``sensitivity``. skgateway addresses a routing bucket as an
OPENAI MODEL ID, so those two fields map mechanically onto ``sk-<class>-<sensitivity>``
and travel in the ordinary ``model`` field of the request. No protocol change, no new
header, no plumbing: every OpenAI-compatible client (pi included) already sends a
model string.

WHY THIS MODULE FAILS CLOSED, and it is the whole reason it exists
-----------------------------------------------------------------
skgateway's bucket grammar is::

    BUCKET_RE = /^sk-(s|m|l|xl)-(public|internal|secret)$/i

A string that MISSES that regex is not rejected. skgateway's ``isRegistryRouted()``
catches any ``sk-*`` id and falls through to ``defaults.role``, which on this fleet is
``sk-auto``, a difficulty classifier. So a single typo (``sk-xl-secrets``) returns a
cheerful HTTP 200 from an arbitrary model with NO sensitivity ceiling applied and no
503 to notice. One character silently discards every sovereignty guarantee.

Therefore every bucket id this module produces is validated against that exact regex
BEFORE it can leave, and anything that does not match raises rather than being sent.
There is deliberately no code path here that returns an unvalidated string.

Verified against skgateway ``src/policy/buckets.mjs`` on 2026-08-16. Bucket
resolution is gated behind ``routing.buckets_enabled``, which is OFF on the live
gateway, so end to end this is not armed yet. The sending side is built correct and
testable regardless.
"""
from __future__ import annotations

import re

#: Byte-for-byte the gateway's grammar (``src/policy/buckets.mjs``). Case-insensitive
#: on input, exactly as the gateway is; we nonetheless CONSTRUCT lowercase only, and
#: `validate_bucket` additionally refuses a non-lowercase id so the wire form has one
#: shape and a case-folding difference can never become a routing difference.
BUCKET_RE = re.compile(r"^sk-(s|m|l|xl)-(public|internal|secret)$", re.IGNORECASE)

#: The four model classes and the three sensitivity zones, lowercased for the wire.
BUCKET_CLASSES: tuple[str, ...] = ("s", "m", "l", "xl")
BUCKET_SENSITIVITIES: tuple[str, ...] = ("public", "internal", "secret")

#: Higher rank == TIGHTER ceiling. Used only to prove (in tests, and in the ungraded
#: floor below) that one bucket is not wider than another.
SENSITIVITY_RANK: dict[str, int] = {"public": 0, "internal": 1, "secret": 2}

#: The tightest sensitivity that exists. The ungraded floor uses it; see
#: `ungraded_floor_bucket`.
TIGHTEST_SENSITIVITY = "secret"

#: Attribute name a dispatcher attaches a per-call model id to on a TaskBrief /
#: GradeBrief. Kept here so the producer (engineering.py) and the consumer
#: (adapters/base.py) can never disagree about the key.
DISPATCH_MODEL_ATTR = "model"


class BucketError(ValueError):
    """A bucket id could not be constructed or does not match the gateway grammar.

    Raised rather than returning None on purpose. Returning None would fall the
    caller back to its statically configured model, which is precisely the silent
    widening this module exists to prevent: a corrupt grade must stop the dispatch,
    not quietly route it somewhere less restricted.
    """


def validate_bucket(bucket: object) -> str:
    """Return ``bucket`` unchanged iff it is a legal, lowercase gateway bucket id.

    Raises BucketError otherwise. This is the ONLY sanctioned exit for a bucket
    string; never send one that has not been through here. A typo'd id does not
    error at the gateway, it silently routes to the ``sk-auto`` default with no
    sensitivity ceiling, so the refusal has to happen locally.
    """
    if not isinstance(bucket, str):
        raise BucketError(f"bucket id must be a string, got {type(bucket).__name__}")
    if bucket != bucket.lower():
        raise BucketError(f"bucket id must be lowercase on the wire: {bucket!r}")
    if not BUCKET_RE.match(bucket):
        raise BucketError(
            f"refusing to send {bucket!r}: it does not match the skgateway bucket "
            "grammar sk-(s|m|l|xl)-(public|internal|secret). skgateway would NOT "
            "reject it; it would fall through to defaults.role (sk-auto) and answer "
            "200 from an arbitrary model with no sensitivity ceiling.")
    return bucket


def bucket_id(model_class: object, sensitivity: object) -> str:
    """Build ``sk-<class>-<sensitivity>`` from a grade's two routing fields.

    ``model_class`` is ALREADY derived upstream as ``CLASS[max(size_rank, risk_rank)]``.
    This function consumes it; it never re-derives or re-grades. Lowercases both
    halves, then validates. Raises BucketError on anything unmappable.
    """
    if not isinstance(model_class, str) or not isinstance(sensitivity, str):
        raise BucketError(
            f"model_class and sensitivity must both be strings, got "
            f"{type(model_class).__name__} and {type(sensitivity).__name__}")
    cls = model_class.strip().lower()
    zone = sensitivity.strip().lower()
    if cls not in BUCKET_CLASSES:
        raise BucketError(f"unknown model_class {model_class!r}; expected one of "
                          f"{BUCKET_CLASSES}")
    if zone not in BUCKET_SENSITIVITIES:
        raise BucketError(f"unknown sensitivity {sensitivity!r}; expected one of "
                          f"{BUCKET_SENSITIVITIES}")
    return validate_bucket(f"sk-{cls}-{zone}")


def work_grade(payload: object) -> dict | None:
    """Read a card's work grade off its payload.

    Local helper against the agreed contract: ``payload["work_grade"]`` is either
    None (the card is UNGRADED) or a COMPLETE dict with size, risk, sensitivity and
    model_class. Never partial. Anything that is not a dict reads as ungraded.
    """
    if not isinstance(payload, dict):
        return None
    grade = payload.get("work_grade")
    return grade if isinstance(grade, dict) else None


def bucket_for_grade(grade: object) -> str:
    """Map one complete work grade onto its validated bucket id.

    The mechanical mapping that is the entire point of ``model_class`` being
    precomputed. Raises BucketError when the grade is missing either routing field,
    so a corrupt grade refuses instead of widening.
    """
    if not isinstance(grade, dict):
        raise BucketError(f"work grade must be a dict, got {type(grade).__name__}")
    if "model_class" not in grade or "sensitivity" not in grade:
        raise BucketError(
            "work grade is missing model_class and/or sensitivity; the contract is a "
            f"COMPLETE grade or None, never partial. got keys: {sorted(grade)}")
    return bucket_id(grade["model_class"], grade["sensitivity"])


def bucket_for_payload(payload: object) -> str | None:
    """The bucket a card should be dispatched to, or None when it is UNGRADED.

    None does NOT mean "use a permissive default". It means the card is INELIGIBLE
    for graded dispatch: the caller sends no model override at all and the adapter
    runs on its statically configured sovereign model, byte-identical to the
    behaviour before graded routing existed. That matters right now because ZERO
    cards carry a grade, so every card takes this branch and nothing may change.

    The invariant that makes this safe: an ungraded card never CONSTRUCTS a bucket
    id, so it can never address a bucket at all, and a bucket is the only mechanism
    by which graded routing selects a model class or a sensitivity zone. It cannot
    reach a bucket a graded card could not. If a caller ever genuinely needs a
    bucket for an ungraded card, `ungraded_floor_bucket` is the only sanctioned
    source and it is pinned to the tightest zone that exists.
    """
    grade = work_grade(payload)
    if grade is None:
        return None
    return bucket_for_grade(grade)


def ungraded_floor_bucket() -> str:
    """The only bucket an UNGRADED card may ever be given: ``sk-s-secret``.

    Smallest class, tightest sensitivity ceiling. Provably not wider than any of
    the twelve buckets a graded card can reach, which is what makes "an absent
    grade never widens access" a property rather than a hope. Not used by the
    default dispatch path (that sends no override at all); it exists so any future
    caller that must name a bucket for an ungraded card has exactly one safe answer
    and no reason to invent a permissive default.
    """
    return bucket_id("s", TIGHTEST_SENSITIVITY)


def is_wider_than(bucket: str, other: str) -> bool:
    """True when ``bucket`` permits a LOOSER sensitivity zone than ``other``.

    Sensitivity is the sovereignty axis: a looser zone is what lets a third-party
    model see the work. Model class is a capability axis, not an access one, so it
    does not participate. Both arguments are validated first, so this can never be
    asked about an id that would have fallen through to sk-auto.
    """
    left = BUCKET_RE.match(validate_bucket(bucket))
    right = BUCKET_RE.match(validate_bucket(other))
    if left is None or right is None:                  # validate_bucket guarantees it
        raise BucketError(f"unparseable bucket pair: {bucket!r}, {other!r}")
    return SENSITIVITY_RANK[left.group(2).lower()] < SENSITIVITY_RANK[right.group(2).lower()]


def attach_dispatch_model(brief, model_id: str | None):
    """Attach a per-call model id (a validated bucket, or None) to a brief.

    Validates before attaching, so an unvalidated string cannot be parked on a brief
    and picked up later by the adapter. Returns the brief for chaining.
    """
    if model_id is not None:
        validate_bucket(model_id)
    setattr(brief, DISPATCH_MODEL_ATTR, model_id)
    return brief


def dispatch_model_of(brief) -> str | None:
    """The per-call model id a dispatcher attached to this brief, or None."""
    return getattr(brief, DISPATCH_MODEL_ATTR, None)

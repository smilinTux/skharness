"""The twin-gate grader's capability pin: a card cannot choose the model that grades it.

WHAT THIS SPLITS, and why it is a split rather than a replacement
----------------------------------------------------------------
A skgateway bucket id (``sk-<class>-<zone>``, see ``buckets.py``) carries TWO
independent facts that happen to travel in one string:

  * the SENSITIVITY zone, an ACCESS fact: which trust zone may see this text.
  * the model CLASS, a CAPABILITY fact: how strong the model doing the work is.

For the BUILD both legs come from the card's work grade and that is correct: the
card's own content decides both what may see it and how much muscle the work
needs.

For the GRADER only ONE of those legs may be inherited.

The half that was already right, and must not be broken. The grader reads the
DIFF, which carries the card's content, so it genuinely sits in the same
sensitivity zone as the build and MUST obey the same trust-zone ceiling. A card
graded ``secret`` whose grader ran in ``public`` would have leaked the very text
the grade was protecting. So the zone is inherited EXACTLY, never widened, and
``grader_bucket`` has no code path that can loosen it.

The half that was wrong. Letting the same bucket also carry the capability class
means the thing being graded selects the competence of its own examiner. A card
graded S/low routed its own quality gate to the weakest class in the fleet:
grade the work easy, get an easy grader. That is the closed loop decision D6 of
the Joule Economy design (2026-08-14, section 0, marked not open for
re-litigation) already ruled out: "Grader-of-record is never the executor. A
worker that writes its own listing posts itself an easy job with a fat bounty."
Section 3.5 of the same design applies the fixed-class rule to the phase0
assessor ("always runs at class M, fixed, regardless of what it is grading").
The twin-gate grader had no equivalent pin. This module is that pin.

WHY A CONSTANT AND NOT A FLOOR
------------------------------
A floor ("at least M") would still let the card push the class UP, and a card
that can move the dial at all can be optimized against: an agent that learns XL
graders are more lenient than M graders would grade its work XL. A constant has
no dial. Scoring a diff against a written rubric is the same fixed-class
classification job whatever the diff contains, so the grader does not need to
scale with the work and can be pinned once, exactly as ``GRADER_MODEL`` in
``orchestrator.py`` is pinned for the assess-time grader.

NOT A NEW ADDRESSING MECHANISM. Everything here is built out of ``buckets.py``'s
existing validated constructors, so the pinned id goes through the same
``validate_bucket`` refusal as any other. There is deliberately no path that
returns an unvalidated string, and no path that constructs a bucket for a card
that did not already have one: an UNGRADED card gets None in and None out, so it
still sends no model override at all and cannot reach a zone it could not
otherwise address.
"""
from __future__ import annotations

from .buckets import BUCKET_RE, BucketError, bucket_id, validate_bucket

#: The FIXED capability class every twin-gate grader runs at, whatever it grades.
#: Matches the phase0 assessor's pin in the approved design (section 3.5): class
#: M, chosen because rubric classification is a fixed-difficulty job and because
#: M is strong enough to read a diff critically without being the class an agent
#: would learn to steer toward. Changing this value is a policy change: it is the
#: single dial the card cannot touch, and it must stay a constant, never become a
#: function of anything the graded card carries.
GRADER_CAPABILITY_CLASS: str = "m"


def grader_bucket(build_bucket: str | None) -> str | None:
    """The bucket the twin-gate grader addresses, given the BUILD's bucket.

    Inherits the sensitivity zone exactly; replaces the capability class with the
    fixed pin. Returns None for None, which is the ungraded path and today's only
    live branch: no override is sent and the adapter runs on its statically
    configured sovereign model, byte-identical to the behaviour before graded
    routing existed.

    Raises BucketError on anything that is not a legal, lowercase gateway bucket
    id, rather than degrading to None. Degrading would fall the caller back to
    its static model and silently discard the ceiling the grade asked for, and a
    malformed ``sk-*`` id is NOT rejected by skgateway: it falls through to
    ``defaults.role`` (``sk-auto``) and answers 200 from an arbitrary model with
    no sensitivity ceiling at all.
    """
    if build_bucket is None:
        return None
    m = BUCKET_RE.match(validate_bucket(build_bucket))
    if m is None:                      # validate_bucket already guarantees a match
        raise BucketError(f"unparseable bucket: {build_bucket!r}")
    return bucket_id(GRADER_CAPABILITY_CLASS, m.group(2).lower())

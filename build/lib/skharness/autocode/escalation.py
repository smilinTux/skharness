"""escalation_reason: the ONE sanctioned feedback channel of the Joule Economy.

Joule Economy design 2026-08-14, decision D2 (section 0, marked not open for
re-litigation):

    "Below the class is refused. Above it is allowed but requires a written
     escalation_reason and the energy overage is debited. ESCALATION REASONS
     BECOME THE TRAINING DATA THAT CORRECTS A BAD RUBRIC."

Section 3.3: "Floor is hard. Ceiling is soft." Section 9, phase 2 exit gate:
"Escalation rate per class is stable and EXPLAINABLE", explainable by a HUMAN,
not merely predictive.

The epic that owns this module asked whether to LEARN the routing policy from
graded outcomes and answered no. This module is what replaces that: the design
already chose a correction channel and nobody had built it, so until now there
was no record anywhere of a run served by a bigger model than its card's floor
required.

THIS IS A REPORTING SEAM, NOT A CONTROL SEAM
--------------------------------------------
Nothing in this repo may read ``escalation_reason`` or ``escalation_state`` to
make a routing decision. A human reads them and decides whether the RUBRIC was
wrong; the rubric then changes under human review, and ``rubric_version``
increments only when the golden set changes. Feeding these fields back into
dispatch would be exactly the autotuner card 09573989 acceptance criterion 6
forbids, and it would close a loop whose only training signal comes from the
model that already benefited from escalating. ``tests/test_autocode_escalation.py``
section 5 proves the seam stays open, statically and behaviourally.

Accordingly this module is PURE: it reads two facts it is handed and returns a
verdict. It does no I/O, holds no state, imports nothing that can dispatch, and
never calls any function that CONSTRUCTS a routing address.

WHY THREE STATES AND NOT TWO, and this is the load-bearing decision
-------------------------------------------------------------------
The literal question "did the served model exceed the card's floor" is, right
now, usually UNANSWERABLE:

  * ``model_served`` is ALWAYS None on every row written today. Neither the
    orchestrator nor the agent-run bridge observes what skgateway actually
    served, and echoing ``model_requested`` into it would manufacture the exact
    fact the field exists to detect.
  * ZERO cards currently carry a work grade, so ``bucket_for_payload`` returns
    None for every card, every run uses the statically configured harness model,
    and there is no floor for anything to exceed.

A two-state escalated/within_floor split would have to force every unanswerable
row into one of the two, and whichever way it went would be a lie. Folded into
``within_floor`` it understates escalation and the resulting rate reads as good
news, which is precisely the failure mode this epic exists to remove. So there
are three:

  ``escalated``     the served class PROVABLY exceeds the floor
  ``within_floor``  it PROVABLY does not
  ``unobserved``    we could not tell

This is the same three-state discipline card A3.3 adopted for substitution
detection. It follows that any rate computed here reports ``observed_fraction``
next to it, and returns None rather than 0.0 when nothing was observed: a
denominator of zero is not a zero rate, and "0 percent escalation" over rows
nobody could see would be the most reassuring possible way to say nothing.

No em/en dashes anywhere (SKWorld hard rule).
"""
from __future__ import annotations

from .buckets import BUCKET_CLASSES, BUCKET_RE

#: The closed three-value vocabulary. Deliberately NOT merged into
#: types.GATE_OUTCOMES or orchestrator.TERMINAL_STATES: those answer "what did
#: the gate decide" and "how did this item end". This answers a third question,
#: "was the ceiling used", and a row carries all three independently.
ESCALATED = "escalated"
WITHIN_FLOOR = "within_floor"
UNOBSERVED = "unobserved"

ESCALATION_STATES: frozenset[str] = frozenset({ESCALATED, WITHIN_FLOOR, UNOBSERVED})

#: Capability rank of the four model classes, in the vocabulary's own order
#: (buckets.BUCKET_CLASSES, which is byte-for-byte the gateway's grammar).
#: Derived, never retyped, so a class can never be ranked here that the gateway
#: would not accept, and the order cannot drift from the routing side.
CLASS_RANK: dict[str, int] = {c: i for i, c in enumerate(BUCKET_CLASSES)}

#: The payload key a human writes their reason into.
REASON_KEY = "escalation_reason"


def served_class(model_served: object) -> str | None:
    """The model class a served-model id PROVABLY names, or None when unknowable.

    Only a well-formed skgateway bucket id (``sk-<class>-<sensitivity>``) names a
    class. A bare model id, which is what the static config sends and what every
    ungraded run therefore uses, does not: mapping "qwen3.6-32b" onto a class
    would require a table that does not exist, and inventing one here would put a
    guess into the corpus that D2 designates as rubric training data.

    A near-miss like ``sk-xl-secrets`` also returns None rather than "xl". That
    string is not a bucket; skgateway does not reject it, it falls through to
    ``defaults.role`` and answers 200 from an arbitrary model. Reading a class
    off it would report a ceiling that was never actually applied.
    """
    if not isinstance(model_served, str):
        return None
    m = BUCKET_RE.match(model_served.strip())
    if m is None:
        return None
    cls = m.group(1).lower()
    return cls if cls in CLASS_RANK else None


def floor_class(work_grade: object) -> str | None:
    """The card's ``model_class`` floor, or None when the card is UNGRADED.

    Reads the precomputed field off the grade. It never re-derives it from size
    and risk: ``model_class = CLASS[max(size_rank, risk_rank)]`` is computed
    upstream by ``grading.model_class_for`` and a second implementation here
    could disagree with the one that actually set the floor.

    None is returned for an ungraded card, a partial grade, or an unrecognised
    class. None means "there is no floor to exceed", which is why every such row
    classifies as ``unobserved`` rather than ``within_floor``: no floor is not a
    satisfied floor.
    """
    if not isinstance(work_grade, dict):
        return None
    raw = work_grade.get("model_class")
    if not isinstance(raw, str):
        return None
    cls = raw.strip().lower()
    return cls if cls in CLASS_RANK else None


def classify(work_grade: object, model_served: object) -> dict:
    """Compare what served against the card's floor. Returns the three fact keys.

    ``{"escalation_state", "escalation_floor_class", "escalation_served_class"}``.

    Both class fields are recorded even when the state is ``unobserved``, because
    the two halves go dark independently: a graded card whose served model was
    never observed still knows its floor, and that asymmetry is itself the thing
    a reader needs in order to know WHICH half to go instrument.

    A served class BELOW the floor is ``within_floor``, not a fourth state. Below
    the floor is a refusal question ("floor is hard") owned upstream by the
    dispatch path, and it must never inflate an escalation rate.

    Total: never raises. Any garbage on either side degrades to ``unobserved``,
    which is the honest reading of "this input told us nothing".
    """
    try:
        floor = floor_class(work_grade)
        served = served_class(model_served)
        if floor is None or served is None:
            state = UNOBSERVED
        elif CLASS_RANK[served] > CLASS_RANK[floor]:
            state = ESCALATED
        else:
            state = WITHIN_FLOOR
        return {"escalation_state": state, "escalation_floor_class": floor,
                "escalation_served_class": served}
    except Exception:      # noqa: BLE001 - a telemetry verdict never breaks a build
        return unobserved_row(reason=None, with_reason=False)


def clean_reason(reason: object) -> str | None:
    """The human's written reason, trimmed, or None when there is not one.

    Whitespace-only and non-strings are None. There is deliberately NO fallback
    that synthesises a sentence from the classes involved: a machine-written
    reason would poison the exact corpus D2 designates as the training data that
    corrects a bad rubric, and it would make an escalation nobody justified
    indistinguishable from one somebody did.
    """
    if not isinstance(reason, str):
        return None
    cleaned = reason.strip()
    return cleaned or None


def reason_from_payload(payload: object) -> str | None:
    """Read the operator-written ``escalation_reason`` off a card payload.

    This is the whole authoring channel: a human who deliberately wants a bigger
    model than the rubric asked for writes their justification on the card. The
    value is carried to the ledger and never consulted by anything that picks a
    model.
    """
    if not isinstance(payload, dict):
        return None
    return clean_reason(payload.get(REASON_KEY))


def unobserved_row(*, reason: object = None, with_reason: bool = True) -> dict:
    """The four-key row for "we could not tell", used as the safe degradation.

    Exists so a failure inside the escalation math still writes the keys with an
    honest value. An ABSENT key would read identically to a pre-S12 row, and the
    NO BACKFILL rule means those rows must stay distinguishable from new ones.
    """
    row = {"escalation_state": UNOBSERVED, "escalation_floor_class": None,
           "escalation_served_class": None}
    if with_reason:
        row[REASON_KEY] = clean_reason(reason)
    return row


def escalation_row(work_grade: object, model_served: object,
                   reason: object = None) -> dict:
    """The complete four-key escalation record for one ledger row.

    The reason is carried whatever the state, including ``unobserved``: what a
    human wrote about the rubric is evidence about the rubric regardless of
    whether this particular run could observe which model served it.
    """
    row = classify(work_grade, model_served)
    row[REASON_KEY] = clean_reason(reason)
    return row


# --------------------------------------------------------------------------- #
# Reporting: the phase 2 exit gate wants a rate PER CLASS, and wants to know   #
# how much of that rate it is entitled to believe.                            #
# --------------------------------------------------------------------------- #


def _empty_stratum() -> dict:
    return {"rows": 0, ESCALATED: 0, WITHIN_FLOOR: 0, UNOBSERVED: 0,
            "escalated_without_reason": 0}


def _finish(stratum: dict) -> dict:
    """Add the derived numbers, and refuse to invent a rate out of nothing.

    ``escalation_rate`` is escalated over OBSERVED rows, never over all rows. A
    denominator that silently includes unobserved rows understates escalation by
    however dark the fleet happens to be, and today it would be dark enough to
    report zero forever.

    ``escalation_rate`` is None, NOT 0.0, when nothing was observed. Zero is a
    measurement; None is the absence of one, and the two must not print the same.
    ``observed_fraction`` sits next to the rate so the number always travels with
    the size of the window it was computed through.
    """
    observed = stratum[ESCALATED] + stratum[WITHIN_FLOOR]
    rows = stratum["rows"]
    stratum["observed"] = observed
    stratum["observed_fraction"] = (observed / rows) if rows else 0.0
    stratum["escalation_rate"] = (stratum[ESCALATED] / observed) if observed else None
    return stratum


def escalation_rates(rows) -> dict:
    """Escalation rate per model class, stratified, over an iterable of rows.

    Returns::

        {"by_class": {"<floor_class>": {rows, escalated, within_floor,
                                        unobserved, observed, observed_fraction,
                                        escalation_rate, escalated_without_reason}},
         "totals":    {same keys, over all rows that HAVE a floor class},
         "ungraded_rows": int}

    Stratified per class rather than blended because a single number cannot
    answer the question the exit gate asks. An XL-floor card escalating is nearly
    meaningless (there is barely any ceiling left above it); an S-floor card
    escalating constantly says the S rubric is wrong. Averaged together those two
    facts cancel.

    ``ungraded_rows`` counts rows with no floor class at all, which is every row
    today and every row written before this module existed. They are held apart
    rather than dropped: a reader must be able to see that the graded strata are
    computed over a small corner of the ledger. NO BACKFILL, an old row carrying
    none of these keys reads as ungraded and unobserved, never as within_floor.

    Never raises: a malformed row is skipped, not fatal.
    """
    by_class: dict[str, dict] = {}
    totals = _empty_stratum()
    ungraded = 0
    try:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            cls = row.get("escalation_floor_class")
            if not isinstance(cls, str) or cls not in CLASS_RANK:
                ungraded += 1
                continue
            state = row.get("escalation_state")
            if state not in ESCALATION_STATES:
                state = UNOBSERVED
            stratum = by_class.setdefault(cls, _empty_stratum())
            for target in (stratum, totals):
                target["rows"] += 1
                target[state] += 1
                if state == ESCALATED and clean_reason(row.get(REASON_KEY)) is None:
                    target["escalated_without_reason"] += 1
    except Exception:      # noqa: BLE001 - a report bug must never break a caller
        pass
    return {"by_class": {c: _finish(s) for c, s in sorted(by_class.items())},
            "totals": _finish(totals), "ungraded_rows": ungraded}

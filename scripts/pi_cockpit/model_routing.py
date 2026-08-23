"""Per-card skgateway model routing for the manual pi cockpit.

ONE small function, ``choose_model``, decides which skgateway model id a
card's build+grade rounds address. It is deliberately NOT the skharness
graded-dispatch bucket system (``autocode/buckets.py``): that system requires
a stored ``meta.grade`` (size/risk/sensitivity/model_class) that today's
cards do not carry, addresses gateway BUCKETS (``sk-s-public`` etc, gated
behind ``routing.buckets_enabled`` which is OFF on the live gateway), and
would refuse every plain Claude model id this cockpit sends. This function
instead picks a concrete, already-proven-reachable Claude model id and hands
it straight to ``PiAdapter(model=...)`` as its STATIC model, exactly the
"PiAdapter instantiates clean" construction verified end to end (2026-08-23:
pi -> skgateway -> claude-code-api -> claude --print -> Chef's first-party
subscription, model claude-sonnet-5, returned "FROM41").

Reachable via the same proven `anthropic` provider on skgateway
(100.108.59.57:18780), confirmed present in `GET /v1/models` on 2026-08-23:
  claude-haiku-4-5-20251001, claude-sonnet-5, claude-opus-5

RATIONALE (read this before disagreeing with a pane's chosen model -- that is
the point of printing it):
  - A card tagged ``size-S`` AND doc/test-only (``docs``, ``documentation``,
    or ``testing``) is small in scope AND low in reasoning depth: writing a
    doc paragraph or a scoped test doesn't need deep multi-file reasoning.
    Cheapest tier, UNLESS a high-risk tag below also applies (a doc card that
    documents a guardrail, for instance, still deserves care).
  - Any high-risk tag (touches an epic's shape, an incident, security,
    guardrail/carve-out surface, or the operator seat) always wins to the
    strong tier regardless of size: these are the cards where a wrong
    "5/5 COMPLETE" promise is expensive. So is a card with no size tag at
    all (untriaged -- treat the unknown as big) or a priority of
    ``critical``, or a size of M/L/XL.
  - Everything else -- a small, plain, core-logic card with no red flags --
    gets the mid tier: this is the one model this route has actually been
    proven against end to end, so it is the safe, cheap-enough default for
    "just a normal small fix".

Chef sees BOTH the chosen model AND this reasoning printed in the pane
(``run_card.py`` prints ``reason`` verbatim), so he can override by hand
(``--model`` on run_card.py) the moment he disagrees.
"""
from __future__ import annotations

# Concrete model ids, not gateway buckets. All three share the same
# `anthropic` provider route on skgateway; only claude-sonnet-5 has been
# independently verified end to end as of 2026-08-23, so it is the default
# ("mid") tier rather than the cheap one.
CHEAP_MODEL = "claude-haiku-4-5-20251001"
MID_MODEL = "claude-sonnet-5"
STRONG_MODEL = "claude-opus-5"

#: A card carrying any of these is doc/test-only in SHAPE (subject to the
#: high-risk override below).
_DOC_TEST_TAGS = frozenset({"docs", "documentation", "testing"})

#: A card carrying any of these always gets the strong tier, size and
#: doc/test shape notwithstanding: getting these wrong is the expensive
#: failure mode, not the slow one.
_HIGH_RISK_TAGS = frozenset({
    "epic", "epic-child", "incident", "security", "guardrail",
    "operator-seat", "carve-out",
})

_KNOWN_SIZES = ("S", "M", "L", "XL")


def _card_size(tags: frozenset[str]) -> str | None:
    """The card's ``size-<X>`` tag value, or None when it carries none."""
    for tag in tags:
        if tag.startswith("size-"):
            value = tag.split("-", 1)[1].upper()
            if value in _KNOWN_SIZES:
                return value
    return None


def choose_model(card: dict) -> tuple[str, str]:
    """Return ``(model_id, reason)`` for one coord card's build+grade rounds.

    ``card`` is a coord task as a plain dict (``Task.model_dump()`` shape):
    reads only ``tags`` and ``priority``, both optional. ``reason`` is meant
    to be printed verbatim in the pane -- it names every fact this function
    used, not just the tier it picked, so a human skimming the pane can
    audit the decision without reading this module.
    """
    tags = frozenset(card.get("tags") or ())
    priority = str(card.get("priority") or "medium").strip().lower()
    size = _card_size(tags)

    risk_hits = sorted(tags & _HIGH_RISK_TAGS)
    doc_test_hits = sorted(tags & _DOC_TEST_TAGS)

    if size == "S" and doc_test_hits and not risk_hits:
        return CHEAP_MODEL, (
            f"cheap tier: size-S + doc/test tag(s) {doc_test_hits}, "
            "no high-risk tag"
        )

    if risk_hits or priority == "critical" or size in (None, "M", "L", "XL"):
        why = (risk_hits
               or ([f"priority={priority}"] if priority == "critical" else [])
               or [f"size={size or 'untagged (treat unknown as big)'}"])
        return STRONG_MODEL, f"strong tier: {', '.join(why)}"

    return MID_MODEL, (
        f"mid tier: size-{size}, core logic (no doc/test tag), "
        f"priority={priority}, no high-risk tag -- the proven default route"
    )

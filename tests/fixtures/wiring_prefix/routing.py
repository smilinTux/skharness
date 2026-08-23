"""Graded model dispatch (instance 6).

``routing.buckets_enabled`` is OFF at the gateway, so ``dispatch_graded`` is
reached only behind a flag that is off everywhere. The default selector is the
only live one.
"""
from __future__ import annotations

buckets_enabled = False


def _route_by_grade(card: object) -> str | None:
    return getattr(card, "grade", None)


def _route_default(card: object) -> str:
    return "sk-default"


def dispatch_graded(card: object) -> str | None:
    return _route_by_grade(card)


def dispatch(card: object) -> str | None:
    if buckets_enabled:
        return dispatch_graded(card)
    return _route_default(card)

"""skcode-hostd route-coverage completeness gate (CR-6.2 C3).

Mirrors skchat's dataplane coverage gate: enumerate the LIVE FastAPI route table
and assert every served route is classified as exactly ONE of public / gated-with-
scope. A new gated route that skips classification breaks CI the same day, not at
the enable flip. It also asserts the two PDP-decided scopes (skcode.inject,
skcode.dispatch) each have a capauth DEFAULT_RULES row, so ``decide`` never fails
closed on "unknown capability" once the surface is enabled.
"""

from __future__ import annotations

from starlette.routing import WebSocketRoute

from skharness.daemon import (
    PDP_SCOPES,
    PUBLIC_ROUTES,
    ROUTE_SCOPES,
    build_daemon_app,
    classify_route,
)
from skharness.harness import FakeHarness


def _app():
    return build_daemon_app(
        harness=FakeHarness(sessions=[], events={}), verify_caller=lambda t: False
    )


# FastAPI auto-mounts these interactive-docs endpoints; they are framework
# defaults, not part of skcode-hostd's declared surface, so the coverage gate for
# OUR routes excludes them (they serve the OpenAPI schema / Swagger UI, no secrets).
_FRAMEWORK_DOC_PATHS = frozenset(
    {
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }
)


def _served_routes() -> list[tuple[str, str]]:
    """Every served (METHOD, path_format) on the real app. Websocket routes report
    as method "WS"; HEAD/OPTIONS are auto-added by Starlette and carry no auth
    semantics of their own, so they are skipped; the FastAPI framework doc routes
    are excluded (see :data:`_FRAMEWORK_DOC_PATHS`)."""
    out: list[tuple[str, str]] = []
    for route in _app().routes:
        path = getattr(route, "path_format", None) or getattr(route, "path", None)
        if not path or path in _FRAMEWORK_DOC_PATHS:
            continue
        if isinstance(route, WebSocketRoute):
            out.append(("WS", path))
            continue
        for method in getattr(route, "methods", None) or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            out.append((method.upper(), path))
    return out


def test_every_served_route_is_classified():
    """COMPLETENESS: every served route is public OR gated-with-a-known-scope."""
    unclassified: list[str] = []
    for method, path in _served_routes():
        klass, _scope = classify_route(method, path)
        if klass is None:
            unclassified.append(f"{method} {path}")
    assert not unclassified, (
        "served routes with no declared class (unsafe to enable the RCE surface):\n  "
        + "\n  ".join(sorted(unclassified))
    )


def test_no_dead_route_declarations():
    """DRIFT GUARD: every declared route (public or gated) is actually served."""
    served = set(_served_routes())
    declared = set(PUBLIC_ROUTES) | set(ROUTE_SCOPES)
    dead = declared - served
    assert not dead, f"declared routes that are not served (typo / removed route): {sorted(dead)}"


def test_public_and_gated_maps_are_disjoint():
    """A route is never BOTH public and gated (single class)."""
    both = set(PUBLIC_ROUTES) & set(ROUTE_SCOPES)
    assert not both, f"routes classified as BOTH public and gated: {sorted(both)}"


def test_pdp_scopes_have_capauth_rule_rows():
    """KNOWN CAPABILITIES ONLY: every PDP-decided scope (inject, dispatch) has a
    rule row in capauth.authz.DEFAULT_RULES, so decide() never fails closed on an
    unknown capability once inject/dispatch are enabled."""
    from capauth.authz import DEFAULT_RULES

    missing = sorted(PDP_SCOPES - set(DEFAULT_RULES))
    assert not missing, f"PDP scopes with no capauth rule row: {missing}"


def test_stream_scope_is_read_not_pdp():
    """skcode.stream is a scope-only read capability: it gates the WS stream and is
    NOT one of the PDP-decided scopes (no capauth rule row expected)."""
    from capauth.authz import DEFAULT_RULES

    assert ROUTE_SCOPES[("WS", "/api/v1/sessions/{sid}/stream")] == "skcode.stream"
    assert "skcode.stream" not in PDP_SCOPES
    assert "skcode.stream" not in DEFAULT_RULES


def test_the_three_write_and_rce_routes_are_gated_on_the_right_scopes():
    """Regression pins for the load-bearing CR-6.2 routes."""
    assert classify_route("POST", "/api/v1/sessions/{sid}/inject") == ("gated", "skcode.inject")
    assert classify_route("POST", "/api/v1/sessions/{sid}/ratify") == ("gated", "skcode.inject")
    assert classify_route("POST", "/api/v1/dispatch") == ("gated", "skcode.dispatch")
    assert classify_route("GET", "/api/v1/dispatch/targets") == ("gated", "skcode.dispatch")
    # discovery + client are public (no bearer)
    assert classify_route("GET", "/.well-known/skworld-module.json") == ("public", None)
    assert classify_route("GET", "/app") == ("public", None)


def test_cancel_route_is_gated_on_dispatch_scope():
    """Card C-6: POST /sessions/{sid}/cancel rides the SAME scope as dispatch
    (spec section 8: "it rides the dispatch scope through the same PDP decision
    path as dispatch"), not a new/unclassified scope."""
    from capauth.authz import DEFAULT_RULES

    assert classify_route("POST", "/api/v1/sessions/{sid}/cancel") == ("gated", "skcode.dispatch")
    assert ROUTE_SCOPES[("POST", "/api/v1/sessions/{sid}/cancel")] == "skcode.dispatch"
    # skcode.dispatch is a PDP-decided scope with an existing rule row (shared
    # with dispatch/dispatch-targets); cancel needs no NEW capauth rule.
    assert "skcode.dispatch" in PDP_SCOPES
    assert "skcode.dispatch" in DEFAULT_RULES


def test_deny_route_is_gated_on_the_inject_scope_like_its_approve_twin():
    """Card C-13: POST /sessions/{sid}/deny is the REFUSAL half of the
    needs_input banner, so it rides the SAME scope and the SAME PDP capability
    as its Approve twin (ratify) and as inject: skcode.inject, PDP-decided.
    Refusing must never cost the caller more than approving, and it must never
    cost less either."""
    from capauth.authz import DEFAULT_RULES

    assert classify_route("POST", "/api/v1/sessions/{sid}/deny") == ("gated", "skcode.inject")
    assert ROUTE_SCOPES[("POST", "/api/v1/sessions/{sid}/deny")] == "skcode.inject"
    # exactly the classification ratify carries (the symmetry the card is about)
    assert (
        ROUTE_SCOPES[("POST", "/api/v1/sessions/{sid}/deny")]
        == ROUTE_SCOPES[("POST", "/api/v1/sessions/{sid}/ratify")]
    )
    # skcode.inject is a PDP-decided scope with an existing rule row; deny needs
    # no NEW capauth rule and is never a scope-only (undecided) route.
    assert "skcode.inject" in PDP_SCOPES
    assert "skcode.inject" in DEFAULT_RULES


def test_sessions_events_archive_route_is_gated_on_stream_scope():
    """Card C-1 (SessionEvent v2 + archive paging): the new GET .../events route
    is a READ route, same scope as the list/get/WS-stream routes, not a PDP-
    decided one (no capauth rule row required for it)."""
    from capauth.authz import DEFAULT_RULES

    assert classify_route("GET", "/api/v1/sessions/{sid}/events") == ("gated", "skcode.stream")
    assert ROUTE_SCOPES[("GET", "/api/v1/sessions/{sid}/events")] == "skcode.stream"
    assert "skcode.stream" not in PDP_SCOPES
    assert "skcode.stream" not in DEFAULT_RULES


def test_jobs_route_is_gated_on_stream_scope_and_read_only():
    """Card C-8 (jobs view over the cron ledger, spec section 8): GET /jobs is a
    READ-only view, same scope as the sessions/events routes, and is NOT one of
    the PDP-decided scopes -- it decides nothing, it only reports. There is no
    mutating jobs route (no run-now/cancel/retry) anywhere in ROUTE_SCOPES."""
    from capauth.authz import DEFAULT_RULES

    assert classify_route("GET", "/api/v1/jobs") == ("gated", "skcode.stream")
    assert ROUTE_SCOPES[("GET", "/api/v1/jobs")] == "skcode.stream"
    assert "skcode.stream" not in PDP_SCOPES
    assert "skcode.stream" not in DEFAULT_RULES
    # Card C-8's hard rule: "no run-now, cancel, or any mutating job action
    # exists in this card". Pin it: no declared route path contains "jobs"
    # other than this one GET.
    jobs_routes = [key for key in ROUTE_SCOPES if "jobs" in key[1]]
    assert jobs_routes == [("GET", "/api/v1/jobs"), ("GET", "/api/v1/arena/jobs")]
    assert all(
        method == "GET" and ROUTE_SCOPES[(method, path)] == "skcode.stream"
        for method, path in jobs_routes
    )


def test_digest_route_is_gated_on_stream_scope_and_read_only():
    """Card C-14a (skwatchdog digest view, answering C-14): GET
    /watchdog/digest is a READ-only view, same scope as sessions/jobs, and is
    NOT one of the PDP-decided scopes -- it decides nothing, it only reports.
    There is no mutating digest route (no publish/regenerate/delete) anywhere
    in ROUTE_SCOPES."""
    from capauth.authz import DEFAULT_RULES

    assert classify_route("GET", "/api/v1/watchdog/digest") == ("gated", "skcode.stream")
    assert ROUTE_SCOPES[("GET", "/api/v1/watchdog/digest")] == "skcode.stream"
    assert "skcode.stream" not in PDP_SCOPES
    assert "skcode.stream" not in DEFAULT_RULES
    digest_routes = [key for key in ROUTE_SCOPES if "digest" in key[1]]
    assert digest_routes == [("GET", "/api/v1/watchdog/digest")]

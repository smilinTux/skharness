"""A6.3 live half: the join against a real gateway (card c7aea2e0).

WHAT THIS FILE NEEDS, STATED SO IT CANNOT PASS BY ACCIDENT
----------------------------------------------------------
A reachable skgateway AND a readable metrics store. Both, not either. If either
is missing the whole module skips with a reason naming what was missing and how
to supply it, and it NEVER degrades into a weaker check that passes anyway.

Set ``SKHARNESS_REQUIRE_LIVE_GATEWAY=1`` to turn every skip in this file into a
failure. That exists because a skip is a silence, and on an operator box where
the gateway is supposed to be up, silence is the wrong answer. CI has no
gateway, so CI skips, and the fixture suite in ``test_attribution_join.py``
carries the join logic there with no gateway involved at all.

WHAT IT COSTS TO RUN
--------------------
Two real completions of a few tokens each, against whatever ``sk-default``
resolves to. They land in the real ``metrics.db`` as two real rows. That is
inherent: the assertion is about what the production store records, so the only
way to make it is to record something. The prompts are trivial and the token
ceiling is small.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import pytest

from skharness.autocode import attribution as attr

GATEWAY_URL_ENV = "SKHARNESS_GATEWAY_URL"
DEFAULT_GATEWAY_URL = "http://localhost:18780"

#: Turn every skip in this module into a failure. A skip is the correct answer
#: when there is genuinely no gateway; it is the wrong answer on a box where one
#: is meant to be running, and nothing else would tell the difference.
REQUIRE_ENV = "SKHARNESS_REQUIRE_LIVE_GATEWAY"

MODEL = "sk-default"
PROMPT = "Reply with the single word: ok"
MAX_TOKENS = 8

#: The gateway writes request_log, token_usage, cost_log and energy_log after the
#: response is finished, so a read immediately after the call can see a partial
#: record. Poll rather than sleep a fixed amount.
SETTLE_TIMEOUT_S = 20.0
SETTLE_POLL_S = 0.25


def _skip(reason: str):
    """Skip loudly, or fail when the operator has declared a gateway required."""
    if (os.environ.get(REQUIRE_ENV) or "").strip() not in ("", "0", "false"):
        pytest.fail(f"{REQUIRE_ENV} is set, so this is a failure and not a skip: "
                    f"{reason}")
    pytest.skip(f"live attribution join not measured: {reason}")


def _gateway_url() -> str:
    return (os.environ.get(GATEWAY_URL_ENV) or DEFAULT_GATEWAY_URL).rstrip("/")


def _post(headers: dict) -> tuple[dict, dict]:
    """One completion. Returns ``(response_headers, body)``.

    Response headers are captured because a DIRECT HTTP caller can read
    ``x-sk-req-id`` and join without querying the store at all. The sandboxed
    harness path cannot (``Sandbox.spawn`` returns stdout only), which is the
    other half of what this file proves.
    """
    payload = json.dumps({"model": MODEL, "max_tokens": MAX_TOKENS,
                          "messages": [{"role": "user", "content": PROMPT}]})
    request = urllib.request.Request(
        f"{_gateway_url()}/v1/chat/completions", data=payload.encode(),
        headers={"content-type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.loads(response.read().decode())
        return {k.lower(): v for k, v in response.headers.items()}, body


def _await_row(req_id: str, want_settled: bool = True) -> attr.GatewayRows:
    """Poll the read-only store until the request row is fully written."""
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    rows = attr.fetch_rows(req_id)
    while time.monotonic() < deadline:
        rows = attr.fetch_rows(req_id)
        settled = rows.request_log is not None and (
            not want_settled or rows.request_log.get("status_code") is not None)
        if settled and rows.token_usage:
            return rows
        time.sleep(SETTLE_POLL_S)
    return rows


@pytest.fixture(scope="module")
def live():
    """A matched PAIR of real calls: one attributed, one deliberately not.

    The pair is the whole design. A single attributed call proves only that some
    id reached the row; the second call is identical in model, prompt, ceiling
    and gateway, and differs ONLY by the absence of the two headers. Anything
    that shows up in one and not the other is therefore caused by the headers
    and by nothing else.
    """
    store = attr.metrics_db_path()
    if not store.exists():
        _skip(f"no gateway metrics store at {store} (set {attr.METRICS_DB_ENV})")
    try:
        urllib.request.urlopen(f"{_gateway_url()}/health", timeout=5).read()
    except (urllib.error.URLError, OSError) as exc:
        _skip(f"no gateway answering at {_gateway_url()} ({exc}); "
              f"set {GATEWAY_URL_ENV}")

    session_id = f"a63-join-{int(time.time())}-{os.getpid()}"
    card_id = "c7aea2e0"
    try:
        sent_headers, _ = _post({attr.SESSION_HEADER: session_id,
                                 attr.CARD_HEADER: card_id})
        control_headers, _ = _post({})
    except (urllib.error.URLError, OSError) as exc:
        _skip(f"the gateway is up but the completion failed ({exc}); "
              f"an unserved call cannot demonstrate a join")

    attributed_req = sent_headers.get(attr.REQ_ID_HEADER)
    control_req = control_headers.get(attr.REQ_ID_HEADER)
    if not attributed_req or not control_req:
        pytest.fail(
            f"the gateway answered without {attr.REQ_ID_HEADER}. That header is "
            f"how a direct caller joins; without it this test cannot locate the "
            f"row it just created, and must not pretend otherwise.")

    return {
        "session_id": session_id,
        "card_id": card_id,
        "attributed": {"req_id": attributed_req, "headers": sent_headers,
                       "rows": _await_row(attributed_req)},
        "control": {"req_id": control_req, "headers": control_headers,
                    "rows": _await_row(control_req)},
    }


def test_the_session_id_we_sent_is_on_the_gateway_row(live):
    verdict = attr.verify_join(
        attr.SentIds(session_id=live["session_id"], card_id=live["card_id"],
                     req_id=live["attributed"]["req_id"]),
        attr.join_rows(live["attributed"]["rows"]))
    assert verdict.ok, verdict.summary()
    assert verdict.session_id == attr.MATCH, verdict.summary()
    assert verdict.card_id == attr.MATCH, verdict.summary()
    assert verdict.attributed is True


def test_the_backend_that_served_it_is_recoverable(live):
    join = attr.join_rows(live["attributed"]["rows"])
    assert join.backend_served is not None, (
        f"no per-request table named a backend for {join.req_id}: "
        f"sources={join.backend_sources} conflict={join.backend_conflict}")
    assert join.backend_sources, "a backend with no named source is a claim"
    # and it agrees with what the gateway told the caller on the response
    header_backend = live["attributed"]["headers"].get(attr.BACKEND_HEADER)
    if header_backend:
        assert join.backend_served == header_backend


def test_the_run_is_joinable_by_req_id_from_the_session_id_alone(live):
    """The SANDBOXED path, which is the one the harness actually uses.

    ``Sandbox.spawn`` returns only stdout, so no response header reaches the
    adapter. What the harness has is the session id it chose, and that has to be
    enough to find its own row.
    """
    found = attr.find_req_ids_for_session(live["session_id"])
    assert found == (live["attributed"]["req_id"],), (
        f"session {live['session_id']} should name exactly the one request it "
        f"made; the store returned {found}")


def test_the_headerless_control_is_null_on_both_sides(live):
    join = attr.join_rows(live["control"]["rows"])
    assert join.found is True, "the control call was made and must have a row"
    assert join.session_id is None, (
        f"a call that sent NO {attr.SESSION_HEADER} came back attributed to "
        f"{join.session_id!r}. Something is stamping a default, which makes "
        f"every attributed row unfalsifiable.")
    assert join.card_id is None

    verdict = attr.verify_join(attr.SentIds(), join)
    assert verdict.ok is True, verdict.summary()
    assert verdict.attributed is False
    assert verdict.session_id == attr.ABSENT_AS_SENT


def test_the_two_live_calls_differ_only_in_attribution(live):
    """The control's power comes from being otherwise identical. If the two
    calls differed in model or backend, the difference in session id would have
    a second candidate explanation."""
    a = attr.join_rows(live["attributed"]["rows"])
    c = attr.join_rows(live["control"]["rows"])
    assert a.model_requested == c.model_requested == MODEL
    assert a.status_code == c.status_code == 200
    assert a.backend_served == c.backend_served
    assert a.req_id != c.req_id
    # the one axis that differs, in both directions
    assert a.session_id is not None and c.session_id is None
    assert a.card_id is not None and c.card_id is None


def test_the_served_model_reaches_the_http_caller_but_never_the_store(live):
    """Pins the boundary this join cannot cross, so nobody later mistakes the
    gateway store for a source of the served model.

    The gateway DOES tell a direct HTTP caller what served the call, on
    ``x-sk-model-served``. That header cannot cross ``Sandbox.spawn``, and no
    column in the store holds the value, so from this path the served model is
    unobserved. It is observable in pi's stdout as ``responseModel``, which is
    card 04970a6e and a different route entirely.
    """
    served = live["attributed"]["headers"].get(attr.MODEL_SERVED_HEADER)
    join = attr.join_rows(live["attributed"]["rows"])
    assert join.model_requested == MODEL
    assert join.model_served is None
    assert join.model_served_reason == attr.MODEL_SERVED_UNOBSERVED

    if not served:
        return
    # No column anywhere in the store holds this value, whatever it is.
    stored = {row.get("model") for row in
              (live["attributed"]["rows"].token_usage
               + live["attributed"]["rows"].cost_log
               + live["attributed"]["rows"].energy_log)}
    stored.add(join.model_requested)
    if served != MODEL:
        # `sk-default` is an alias that resolves at route time, so this is the
        # normal case, and it is exactly the substitution the store cannot see.
        assert served not in stored, (
            f"the store holds the served model {served!r} after all; this test "
            f"and attribution.MODEL_SERVED_UNOBSERVED are now both out of date "
            f"and the join should be reading it instead of recording a gap.")

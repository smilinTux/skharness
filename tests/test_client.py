from fastapi.testclient import TestClient

from skharness.daemon import build_daemon_app
from skharness.harness import FakeHarness


def _client():
    app = build_daemon_app(harness=FakeHarness(), verify_caller=lambda t: t == "good")
    return TestClient(app)


def test_real_client_page_is_served():
    r = _client().get("/")
    assert r.status_code == 200
    body = r.text
    # It is the real client, not the placeholder.
    assert "skcode" in body.lower()
    assert "Read-only client not installed yet" not in body
    assert "/api/v1/sessions" in body
    # The read-only WS tail route (no manual ?token= paste field: the client
    # follows the pairing model and shows an honest empty-state on 401).
    assert "/stream" in body


def test_client_view_paths_stay_read_only():
    body = _client().get("/").text.lower()
    # The compose panel adds exactly ONE write affordance: the gated POST
    # /api/v1/dispatch. No other mutation surface exists (no inject / ratify /
    # kill), and the ONLY POST in the whole page is that single dispatch call.
    assert "/inject" not in body
    assert "/ratify" not in body
    assert "/kill" not in body
    assert "method: 'post'" not in body
    assert body.count('method: "post"') == 1
    assert "/api/v1/dispatch" in body


def test_client_page_has_gated_dispatch_compose():
    # The New-session compose panel dispatches via the gated POST and reads its
    # advisory allowlist from the targets route, both reusing the same wire token.
    body = _client().get("/").text
    assert "/api/v1/dispatch/targets" in body
    assert 'method: "POST"' in body
    assert "authHeaders" in body
    # The compose panel exists in the markup.
    assert 'id="compose"' in body


def test_client_page_has_no_external_asset_fetch():
    body = _client().get("/").text.lower()
    assert "http://" not in body
    assert "https://" not in body


def test_client_reads_wire_token_from_url_and_injects_it():
    # The parent shell hands a capauth audience=skcode wire token to this client
    # via the iframe URL as ?token=<wire>. The client must read it and attach it
    # as an Authorization: Bearer header on the HTTP read routes and as ?token=
    # on the WS tail (a browser cannot set headers on a WebSocket).
    body = _client().get("/").text
    assert 'URLSearchParams(location.search).get("token")' in body
    assert 'Authorization" ' in body or '"Authorization"' in body
    assert "Bearer " in body
    # The WS URL carries the token as a query param.
    assert '"token="' in body


def test_token_seam_stays_read_only():
    # The wire token rides the GET read routes, the read-only WS tail, and the
    # single gated dispatch write. It must NOT enable any other mutation verb.
    body = _client().get("/").text.lower()
    assert "/inject" not in body
    assert "/ratify" not in body
    assert "/kill" not in body

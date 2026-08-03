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


def test_client_page_is_read_only_no_write_verbs():
    body = _client().get("/").text.lower()
    # No injection / mutation surface in the read-only MVP client.
    assert "method: 'post'" not in body
    assert 'method: "post"' not in body
    assert "/inject" not in body
    assert "/dispatch" not in body


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
    # Injecting a token must NOT add any write verb: the token only rides the
    # existing GET read routes and the read-only WS tail.
    body = _client().get("/").text.lower()
    assert "method: 'post'" not in body
    assert 'method: "post"' not in body
    assert "/inject" not in body
    assert "/ratify" not in body

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
    assert "/api/v1/sessions" in body
    assert "/stream?token=" in body


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

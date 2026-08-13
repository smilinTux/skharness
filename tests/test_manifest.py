"""skcode's SKWorld module manifest: shape, origin-relative URLs, operator facet."""

from skharness.manifest import AUDIENCE, SCHEMA_VERSION, skcode_module_manifest


def test_manifest_ui_facet_shape():
    m = skcode_module_manifest("http://100.64.0.1:9394/")
    assert m["schemaVersion"] == SCHEMA_VERSION
    assert m["id"] == "skcode"
    # Grade A since card C-18: packages/skcode_client in skworld-app is the real
    # native module and the shell registry mounts it at /code.
    assert m["grade"] == "A"
    assert m["entry"]["flutter_package"] == "skcode_client"
    # entry.url is deliberately KEPT alongside flutter_package so the legacy web
    # client stays reachable at /code/legacy. C-10's parity check found two
    # capabilities the native pane does not cover yet (C-16, C-17), so removing
    # this would strand the operator with no fallback.
    assert m["entry"]["url"].endswith("/app")
    assert m["nav"] == {"icon": "terminal", "order": 30, "label": "Code"}
    assert m["deeplinkPrefix"] == "skworld://skcode/"
    assert m["memory"] == {"opt_in": False}


def test_urls_are_origin_relative_and_not_double_slashed():
    m = skcode_module_manifest("http://host:9394/")
    assert m["entry"]["url"] == "http://host:9394/app"
    assert m["health"] == "http://host:9394/api/v1/hosts/self"
    # A base without a trailing slash yields the same (no missing/extra slash).
    m2 = skcode_module_manifest("http://host:9394")
    assert m2["entry"]["url"] == "http://host:9394/app"


def test_auth_facet_declares_audience_and_scopes():
    m = skcode_module_manifest("http://host/")
    assert m["auth"]["audience"] == AUDIENCE == "skcode"
    assert m["auth"]["scopes"] == ["skcode.stream", "skcode.inject", "skcode.dispatch"]


def test_operator_facet_matches_the_skcode_adapter_contract():
    op = skcode_module_manifest("http://host/")["operator"]
    assert op["contractVersion"] == 1
    assert op["cli"] == "skcode-hostd operator"
    assert op["repos"] == ["skharness"]
    # Mirrors operator_seat/skcode_adapter.py CONDITIONS and its standard actions.
    assert op["conditions"] == [
        "HostdReady",
        "SessionsHealthy",
        "RegistryConsistent",
        "AuthEnforced",
    ]
    assert op["proposedStandardActions"] == ["restart-hostd", "archive-stale-session"]
